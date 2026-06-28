"""Pre-auth abuse limiter wiring for hot SAT endpoints."""
from __future__ import annotations

from starlette.testclient import TestClient

from scaffold.publisher.app import build_app


def _client(tmp_path) -> TestClient:
    app = build_app(database_path=str(tmp_path / "abuse.db"), signing_key_hex="11" * 32)
    return TestClient(app)


def _enable(monkeypatch, *, ip_rpm: int, actor_rpm: int, base: int = 2, cap: int = 20) -> None:
    monkeypatch.setenv("CATHEDRAL_ABUSE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_ABUSE_IP_RPM", str(ip_rpm))
    monkeypatch.setenv("CATHEDRAL_ABUSE_ACTOR_RPM", str(actor_rpm))
    monkeypatch.setenv("CATHEDRAL_ABUSE_RETRY_BASE_SECS", str(base))
    monkeypatch.setenv("CATHEDRAL_ABUSE_RETRY_MAX_SECS", str(cap))
    monkeypatch.delenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", raising=False)


def test_actor_limit_covers_legacy_submit_and_escalates_retry_after(tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=100, actor_rpm=1, base=2, cap=9)
    client = _client(tmp_path)
    headers = {
        "X-Forwarded-For": "198.51.100.21",
        "X-Cathedral-Hotkey": "actor-legacy-submit",
    }

    first = client.post("/api/cathedral/v1/agents/submit", headers=headers)
    assert first.headers.get("X-Cathedral-Rejection-Reason") != "actor_rate_limited"

    second = client.post("/api/cathedral/v1/agents/submit", headers=headers)
    assert second.status_code == 429
    assert second.text == "actor_rate_limited"
    assert second.headers["X-Cathedral-Rejection-Reason"] == "actor_rate_limited"
    assert second.headers["Retry-After"] == "2"

    third = client.post("/api/cathedral/v1/agents/submit", headers=headers)
    assert third.status_code == 429
    assert third.headers["X-Cathedral-Rejection-Reason"] == "actor_rate_limited"
    assert third.headers["Retry-After"] == "4"


def test_actor_limit_does_not_block_a_different_claimed_actor(tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=100, actor_rpm=1)
    client = _client(tmp_path)
    ip = "198.51.100.22"

    client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "actor-a",
    })
    limited = client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "actor-a",
    })
    assert limited.status_code == 429
    assert limited.headers["X-Cathedral-Rejection-Reason"] == "actor_rate_limited"

    other = client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "actor-b",
    })
    assert other.headers.get("X-Cathedral-Rejection-Reason") != "actor_rate_limited"


def test_actor_limit_is_scoped_to_ip_to_prevent_cross_ip_hotkey_lockout(
        tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=100, actor_rpm=1)
    client = _client(tmp_path)
    victim = "known-public-hotkey"

    client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": "198.51.100.30",
        "X-Cathedral-Hotkey": victim,
    })
    limited = client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": "198.51.100.30",
        "X-Cathedral-Hotkey": victim,
    })
    assert limited.status_code == 429
    assert limited.headers["X-Cathedral-Rejection-Reason"] == "actor_rate_limited"

    other_ip = client.post("/v1/agents/submit", headers={
        "X-Forwarded-For": "198.51.100.31",
        "X-Cathedral-Hotkey": victim,
    })
    assert other_ip.headers.get("X-Cathedral-Rejection-Reason") != "actor_rate_limited"


def test_ip_limit_spans_per_miner_listing_and_cnf_paths(tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=2, actor_rpm=100, base=3)
    client = _client(tmp_path)
    ip = "198.51.100.23"

    r1 = client.get("/v1/synthetic-boolean/per-miner/challenges", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "ip-a",
    })
    assert r1.headers.get("X-Cathedral-Rejection-Reason") != "ip_rate_limited"

    r2 = client.get("/v1/synthetic-boolean/per-miner/cnf?challenge_id=pm-test", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "ip-b",
    })
    assert r2.headers.get("X-Cathedral-Rejection-Reason") != "ip_rate_limited"

    r3 = client.get("/v1/synthetic-boolean/per-miner/challenges", headers={
        "X-Forwarded-For": ip,
        "X-Cathedral-Hotkey": "ip-c",
    })
    assert r3.status_code == 429
    assert r3.text == "ip_rate_limited"
    assert r3.headers["X-Cathedral-Rejection-Reason"] == "ip_rate_limited"
    assert r3.headers["Retry-After"] == "3"


def test_cf_connecting_ip_takes_precedence_over_spoofed_xff(tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=1, actor_rpm=100)
    client = _client(tmp_path)
    headers = {
        "CF-Connecting-IP": "198.51.100.40",
        "X-Forwarded-For": "203.0.113.250",
        "X-Cathedral-Hotkey": "cf-ip",
    }
    first = client.get("/v1/synthetic-boolean/per-miner/challenges", headers=headers)
    assert first.headers.get("X-Cathedral-Rejection-Reason") != "ip_rate_limited"
    second = client.get(
        "/v1/synthetic-boolean/per-miner/challenges",
        headers={**headers, "X-Forwarded-For": "203.0.113.251"},
    )
    assert second.status_code == 429
    assert second.headers["X-Cathedral-Rejection-Reason"] == "ip_rate_limited"


def test_abuse_limiter_default_off_even_with_low_limits(tmp_path, monkeypatch):
    monkeypatch.delenv("CATHEDRAL_ABUSE_LIMIT_ENABLED", raising=False)
    monkeypatch.setenv("CATHEDRAL_ABUSE_IP_RPM", "1")
    monkeypatch.setenv("CATHEDRAL_ABUSE_ACTOR_RPM", "1")
    client = _client(tmp_path)

    headers = {
        "X-Forwarded-For": "198.51.100.24",
        "X-Cathedral-Hotkey": "default-off",
    }
    for _ in range(3):
        resp = client.post("/v1/agents/submit", headers=headers)
        assert resp.headers.get("X-Cathedral-Rejection-Reason") not in {
            "ip_rate_limited",
            "actor_rate_limited",
        }


def test_abuse_limiter_does_not_touch_validator_weight_url(tmp_path, monkeypatch):
    _enable(monkeypatch, ip_rpm=1, actor_rpm=1)
    client = _client(tmp_path)
    headers = {
        "X-Forwarded-For": "198.51.100.25",
        "X-Cathedral-Hotkey": "not-a-target",
    }

    for _ in range(3):
        resp = client.get("/v1/validator/weights/next", headers=headers)
        assert resp.headers.get("X-Cathedral-Rejection-Reason") not in {
            "ip_rate_limited",
            "actor_rate_limited",
        }
