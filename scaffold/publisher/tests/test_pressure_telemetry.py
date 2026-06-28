from __future__ import annotations

import base64
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher.app import build_app, seed_challenge
from scaffold.publisher.auth import canonical_claim_bytes, sha256_hex
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher.store import Store

ADMIN_TOKEN = "pressure-admin"
FAMILY = "synthetic_boolean_v1"
EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
CNF = "p cnf 2 2\n1 2 0\n-1 2 0\n"
SOLUTION = "s SATISFIABLE\nv 1 2 0\n"


def _admin_headers():
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair():
    from bittensor_wallet import Keypair

    return Keypair.create_from_seed("0x" + "ab" * 32)


def _signed_submit_headers(kp, *, challenge_id: str, solution: str):
    submitted_at = _now_iso()
    sol_sha = sha256_hex(solution)
    msg = canonical_claim_bytes(
        bundle_hash=EMPTY_BUNDLE,
        card_id=FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=submitted_at,
        challenge_id=challenge_id,
        dimacs_solution_sha256=sol_sha,
    )
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": base64.b64encode(kp.sign(msg)).decode("ascii"),
        "X-Cathedral-Submitted-At": submitted_at,
        "User-Agent": "cathedral-test-miner/1.0",
        "X-Forwarded-For": "203.0.113.47",
    }, submitted_at


def _pressure(client):
    return client.get(
        "/v1/admin/synthetic-boolean/submit-metrics",
        headers=_admin_headers(),
    ).json()["pressure"]


def test_pressure_telemetry_attributes_429_without_raw_actor_secrets(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_BURST", "1")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC", "0.0001")

    app = build_app(
        database_path=str(tmp_path / "pressure-429.sqlite"),
        signing_key_hex=generate_test_key(),
    )
    with TestClient(app) as client:
        headers = {
            "X-Cathedral-Hotkey": "claimed-hk",
            "User-Agent": "cathedral-test-miner/1.0",
            "X-Forwarded-For": "203.0.113.47",
        }
        client.post("/v1/agents/submit", headers=headers)
        resp = client.post("/v1/agents/submit", headers=headers)
        assert resp.status_code == 429

        pressure = _pressure(client)

    assert pressure["enabled"] is True
    assert pressure["logging_sample_rate"] == 0.0
    assert pressure["total_pressure_events"] == 1
    top = pressure["top"][0]
    assert top["path"] == "/v1/agents/submit"
    assert top["status"] == 429
    assert top["reason"] == "abuse_rate_limited"
    assert top["ip_block"] == "203.0.113.0/24"
    assert top["user_agent_family"] == "cathedral-test-miner"
    assert top["claimed_hotkey_hash"]
    assert top["verified_hotkey_hash"] is None
    assert "claimed-hk" not in str(pressure)
    assert "203.0.113.47" not in str(pressure)


def test_pressure_telemetry_prefers_cf_connecting_ip_over_spoofed_xff(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_BURST", "1")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC", "0.0001")

    app = build_app(
        database_path=str(tmp_path / "pressure-cf-ip.sqlite"),
        signing_key_hex=generate_test_key(),
    )
    with TestClient(app) as client:
        headers = {
            "X-Cathedral-Hotkey": "claimed-hk",
            "User-Agent": "cathedral-test-miner/1.0",
            "CF-Connecting-IP": "198.51.100.55",
            "X-Forwarded-For": "203.0.113.47",
        }
        client.post("/v1/agents/submit", headers=headers)
        resp = client.post("/v1/agents/submit", headers=headers)
        assert resp.status_code == 429

        pressure = _pressure(client)

    top = pressure["top"][0]
    assert top["ip_block"] == "198.51.100.0/24"


def test_pressure_telemetry_marks_verified_hotkey_after_signature_check(
    tmp_path,
    monkeypatch,
):
    import scaffold.publisher.app as appmod

    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")

    db = str(tmp_path / "pressure-500.sqlite")
    app = build_app(database_path=db, signing_key_hex=generate_test_key())
    store = Store(db)
    seed_challenge(store, challenge_id="c-pressure", tier=1, cnf_text=CNF)

    def _boom(cnf_text, dimacs_solution):
        raise RuntimeError("synthetic verifier failure")

    monkeypatch.setattr(appmod, "verify_dimacs_solution", _boom)
    kp = _keypair()
    headers, submitted_at = _signed_submit_headers(
        kp,
        challenge_id="c-pressure",
        solution=SOLUTION,
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/v1/agents/submit",
            data={
                "card_id": FAMILY,
                "challenge_id": "c-pressure",
                "dimacs_solution": SOLUTION,
                "submitted_at": submitted_at,
            },
            headers=headers,
        )
        assert resp.status_code == 500

        pressure = _pressure(client)

    top = pressure["top"][0]
    assert top["path"] == "/v1/agents/submit"
    assert top["status"] == 500
    assert top["reason"] == "unhandled_exception"
    assert top["claimed_hotkey_hash"]
    assert top["verified_hotkey_hash"] == top["claimed_hotkey_hash"]
    assert kp.ss58_address not in str(pressure)


def test_submit_metrics_pressure_snapshot_is_reachable_on_submit_role(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "submit")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")

    app = build_app(
        database_path=str(tmp_path / "pressure-submit-role.sqlite"),
        signing_key_hex=generate_test_key(),
    )
    with TestClient(app) as client:
        resp = client.get(
            "/v1/admin/synthetic-boolean/submit-metrics",
            headers=_admin_headers(),
        )

    assert resp.status_code == 200
    assert resp.json()["pressure"]["schema"] == "cathedral.pressure_telemetry.v1"
