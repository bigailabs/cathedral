from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient

from scaffold.publisher import v2_bitset_submit, v2_pipeline
from scaffold.publisher.v2_lean_ingress import LeanIngressStore, build_ingress_app


ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "deploy" / "golden" / "v2_bitset_ingress_golden.json"
SECRET = "test-v2-lean-ingress-secret-not-live"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _plus_iso(secs: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str = "//V2LeanIngressTest"):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _client(tmp_path, *, secret: str = SECRET, skew: int = 300,
            max_unflushed_events: int = 100_000,
            metrics_token: str = "",
            ip_rpm: int = 6000) -> TestClient:
    store = LeanIngressStore(tmp_path / "ingress.sqlite3")
    app = build_ingress_app(
        store=store,
        submit_token_secret=secret,
        max_body_bytes=1024,
        timestamp_skew_secs=skew,
        max_unflushed_events=max_unflushed_events,
        max_storage_bytes=0,
        min_free_disk_bytes=0,
        max_unflushed_age_secs=0,
        metrics_token=metrics_token,
        metrics_ttl_secs=1.0,
        ip_rpm=ip_rpm,
    )
    return TestClient(app)


def _submit_body(kp, *, secret: str = SECRET, submitted_at: str | None = None,
                 challenge_id: str = "pm-t2-e495232-s7-test"):
    ts = submitted_at or _now_iso()
    assignment = [1, -2, 3, -4, 5, -6, 7, -8, 9, -10]
    raw = v2_pipeline.encode_bitset_assignment(assignment)
    token = v2_bitset_submit.mint_submit_token(
        secret=secret,
        miner_hotkey=kp.ss58_address,
        challenge_id=challenge_id,
        epoch=495232,
        tier=2,
        seq=7,
        nvars=10,
        cnf_sha256=hashlib.sha256(b"lean-ingress-test-cnf").hexdigest(),
        expires_at=_plus_iso(300),
    )
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "miner_hotkey": kp.ss58_address,
        "submitted_at": ts,
        "challenge_id": challenge_id,
        "submit_token": token,
        "assignment_encoding": v2_bitset_submit.ASSIGNMENT_ENCODING,
        "assignment_b64": base64.b64encode(raw).decode("ascii"),
    }
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        card_id="synthetic_boolean_v1",
    )
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }
    return body, headers


def test_lean_ingress_accepts_and_replays(tmp_path):
    client = _client(tmp_path)
    kp = _keypair()
    body, headers = _submit_body(kp)

    first = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert first.status_code == 202, first.text
    receipt = first.json()
    assert receipt["status"] == "received"
    assert receipt["terminal"] is False
    assert receipt["open"] is True
    assert receipt["weighted_score"] == 0.0
    assert receipt["idempotent_replay"] is False

    second = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert second.status_code == 200, second.text
    replay = second.json()
    assert replay["receipt_id"] == receipt["receipt_id"]
    assert replay["idempotent_replay"] is True

    fetched = client.get(receipt["receipt_url"])
    assert fetched.status_code == 200
    assert fetched.json()["receipt_id"] == receipt["receipt_id"]

    metrics = client.get("/v2/ingress/metrics").json()
    assert metrics["events"]["received"] == 1
    assert metrics["unflushed_events"] == 1


def test_lean_ingress_rejects_bad_token_before_event(tmp_path):
    client = _client(tmp_path)
    kp = _keypair()
    body, headers = _submit_body(kp)
    body["submit_token"] = body["submit_token"][:-4] + "xxxx"
    # Re-sign the mutated body so the token check is the failure, not signature.
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=headers["X-Cathedral-Submitted-At"],
        card_id="synthetic_boolean_v1",
    )
    headers["X-Cathedral-Signature"] = base64.b64encode(
        kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))
    ).decode("ascii")

    r = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_submit_token"
    metrics = client.get("/v2/ingress/metrics").json()
    assert metrics["total_events"] == 0
    assert metrics["rejects"]["invalid_submit_token"] == 1
    with client.app.state.store._connect() as conn:
        db_rejects = conn.execute("SELECT COUNT(*) AS n FROM reject_rollups_local").fetchone()["n"]
    assert db_rejects == 0


def test_lean_ingress_metrics_token_gate(tmp_path):
    client = _client(tmp_path, metrics_token="metrics-secret")
    denied = client.get("/v2/ingress/metrics")
    assert denied.status_code == 401
    allowed = client.get(
        "/v2/ingress/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["schema"] == "cathedral.v2.lean_ingress_metrics.v1"


def test_lean_ingress_ip_rate_limit_before_body_work(tmp_path):
    client = _client(tmp_path, ip_rpm=1)
    kp = _keypair()
    body, headers = _submit_body(kp)
    first = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert first.status_code == 202, first.text
    limited = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert limited.status_code == 429
    assert limited.json()["detail"] == "ingress_ip_rate_limited"


def test_lean_ingress_rejects_multi_worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    store = LeanIngressStore(tmp_path / "ingress.sqlite3")
    try:
        import pytest
        with pytest.raises(RuntimeError, match="WEB_CONCURRENCY=2"):
            build_ingress_app(store=store, submit_token_secret=SECRET)
    finally:
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)


def test_lean_ingress_exact_replay_bypasses_later_token_expiry(tmp_path, monkeypatch):
    client = _client(tmp_path)
    kp = _keypair()
    body, headers = _submit_body(kp)

    first = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert first.status_code == 202, first.text
    receipt_id = first.json()["receipt_id"]

    def _expired(*args, **kwargs):
        raise v2_bitset_submit.BitsetSubmitError("submit_token_expired")

    monkeypatch.setattr(v2_bitset_submit, "verify_submit_token", _expired)
    replay = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == receipt_id
    assert replay.json()["idempotent_replay"] is True


def test_lean_ingress_can_replace_existing_rejected_row(tmp_path):
    client = _client(tmp_path)
    kp = _keypair()
    body, headers = _submit_body(kp, challenge_id="pm-t2-e495232-s7-reject-retry")

    first = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert first.status_code == 202, first.text
    first_payload = first.json()
    first_sha = first_payload["assignment_sha256"]

    store = client.app.state.store
    idem = v2_bitset_submit.idempotency_key(
        miner_hotkey=kp.ss58_address,
        challenge_id=body["challenge_id"],
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE submit_events_local SET status='rejected', rejection_reason='witness_check_failed' "
            "WHERE idempotency_key=?",
            (idem,),
        )

    raw2 = bytes([0x00, 0x00])
    body["assignment_b64"] = base64.b64encode(raw2).decode("ascii")
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=headers["X-Cathedral-Submitted-At"],
        card_id="synthetic_boolean_v1",
    )
    headers["X-Cathedral-Signature"] = base64.b64encode(
        kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))
    ).decode("ascii")

    retry = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert retry.status_code == 202, retry.text
    retry_payload = retry.json()
    assert retry_payload["receipt_id"] == first_payload["receipt_id"]
    assert retry_payload["status"] == "received"
    assert retry_payload["assignment_sha256"] != first_sha
    assert retry_payload["idempotent_replay"] is False


def test_lean_ingress_backpressure_allows_replay_blocks_new_unique(tmp_path):
    client = _client(tmp_path, max_unflushed_events=1)
    kp = _keypair()
    body, headers = _submit_body(kp, challenge_id="pm-t2-e495232-s7-pressure-a")

    first = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert first.status_code == 202, first.text
    receipt_id = first.json()["receipt_id"]

    replay = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == receipt_id
    assert replay.json()["idempotent_replay"] is True

    body2, headers2 = _submit_body(kp, challenge_id="pm-t2-e495232-s7-pressure-b")
    blocked = client.post("/v2/agents/submit-bitset", json=body2, headers=headers2)
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "ingress_backlog_full"

    ready = client.get("/health/ready")
    assert ready.status_code == 503
    assert ready.json()["reason"] == "ingress_backlog_full"


def test_lean_ingress_body_cap_before_json_parse(tmp_path):
    client = _client(tmp_path)
    kp = _keypair()
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": "bad",
        "X-Cathedral-Submitted-At": _now_iso(),
        "Content-Type": "application/json",
    }
    r = client.post("/v2/agents/submit-bitset", content=b"{" + b"x" * 2000, headers=headers)
    assert r.status_code == 413
    assert r.json()["detail"] == "submit_bitset_body_too_large"


def test_lean_ingress_accepts_golden_vector(tmp_path):
    vector = json.loads(GOLDEN.read_text(encoding="utf-8"))
    client = _client(
        tmp_path,
        secret=vector["fake_submit_token_secret"],
        skew=10_000_000_000,
    )
    headers = {
        "X-Cathedral-Hotkey": vector["headers"]["X-Cathedral-Hotkey"],
        "X-Cathedral-Signature": vector["headers"]["X-Cathedral-Signature"],
        "X-Cathedral-Submitted-At": vector["headers"]["X-Cathedral-Submitted-At"],
    }
    r = client.post(
        "/v2/agents/submit-bitset",
        json=vector["normalized_submit_body"],
        headers=headers,
    )
    assert r.status_code == 202, r.text
    payload = r.json()
    assert payload["status"] == "received"
    assert payload["assignment_sha256"] == vector["assignment_sha256"]
    assert payload["cnf_sha256"] == vector["token_payload"]["cnf_sha256"]
