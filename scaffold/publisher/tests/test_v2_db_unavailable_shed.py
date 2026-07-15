"""Front-door shed for V2 DB connection pressure (open-v2 incident 2026-07-08).

Live all-miner traffic exhausted PG connection acquisition on the public origin:
psycopg2.OperationalError raised out of _pool.getconn during submit admission
(store.write) and receipt polling (store.query), surfacing as raw 500s and
readiness flaps. These tests pin the controlled behaviour:

- /v2/agents/submit-bitset and its receipt endpoint return a distinct 503
  (`v2_db_unavailable_retry`) with Retry-After when the DB is unavailable.
- Non-DB errors are NOT converted; they still propagate as 500s.
- Receipt polling has its own small concurrency gate
  (`receipt_poll_busy_retry`) so a poll flood cannot exhaust the DB pool.
"""
from __future__ import annotations

import base64
import sqlite3
import threading
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher import v2_pipeline
from scaffold.publisher import v2_bitset_submit
from scaffold.publisher import per_miner as pm
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes


SIGNING_KEY_HEX = "11" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"

# psycopg2-shaped errors without importing psycopg2 (optional dependency on
# sqlite deployments) — the shed matches by class name + module.
_PgOperationalError = type(
    "OperationalError", (Exception,), {"__module__": "psycopg2"})
_PgPoolError = type("PoolError", (Exception,), {"__module__": "psycopg2.pool"})


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _read_headers(kp, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE,
        card_id=_FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        challenge_id="",
        dimacs_solution_sha256="",
    )
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _bitset_headers(kp, body: dict, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        card_id=_FAMILY,
    )
    sig = base64.b64encode(
        kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _build(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "db-shed-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "8")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_NVARS_T1", "80")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_NCLAUSES_T1", "240")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_METHOD_T1", "biased")
    return build_app(
        database_path=str(tmp_path / "pub.sqlite"),
        signing_key_hex=SIGNING_KEY_HEX,
    )


def _build_submit(client: TestClient, uri: str):
    kp = _keypair(uri)
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200, board.text
    item = board.json()["items"][0]
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": base64.b64encode(
            v2_pipeline.encode_bitset_assignment(assignment)
        ).decode("ascii"),
    }
    return kp, body, _bitset_headers(kp, body)


def _assert_db_shed(resp) -> None:
    assert resp.status_code == 503, resp.text
    assert resp.headers["x-cathedral-rejection-reason"] == "v2_db_unavailable_retry"
    assert int(resp.headers["retry-after"]) >= 1
    payload = resp.json()
    assert payload["reason"] == "v2_db_unavailable_retry"
    assert payload["detail"] == "v2_db_unavailable_retry"
    assert payload["retry_after_seconds"] >= 1


def test_submit_bitset_sheds_503_on_db_read_pressure(tmp_path, monkeypatch):
    """psycopg2 OperationalError from the idempotency read -> controlled 503."""
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    _kp, body, headers = _build_submit(client, "//DbShedReadMiner")

    def _boom(sql, params=()):
        raise _PgOperationalError("could not connect to server: connection pressure")

    monkeypatch.setattr(app.state.v2_store, "query", _boom)
    resp = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    _assert_db_shed(resp)


def test_submit_bitset_sheds_503_on_db_write_pressure(tmp_path, monkeypatch):
    """Pool exhaustion during admit (store.write) -> controlled 503, and the
    same submit succeeds once the DB recovers."""
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    _kp, body, headers = _build_submit(client, "//DbShedWriteMiner")

    original_write = app.state.v2_store.write

    def _boom(fn):
        raise _PgPoolError("connection pool exhausted")

    monkeypatch.setattr(app.state.v2_store, "write", _boom)
    resp = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    _assert_db_shed(resp)

    # Recovery: the shed is transient, not a rejection — a retry admits.
    monkeypatch.setattr(app.state.v2_store, "write", original_write)
    retry = client.post("/v2/agents/submit-bitset", json=body, headers=headers)
    assert retry.status_code == 202, retry.text


def test_receipt_poll_sheds_503_on_db_pressure(tmp_path, monkeypatch):
    """sqlite backend variant: OperationalError('database is locked') on the
    receipt read -> controlled 503 instead of a raw 500."""
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app)

    def _boom(sql, params=()):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.state.v2_store, "query", _boom)
    resp = client.get("/v2/agents/submit-bitset/receipts/any-receipt-id")
    _assert_db_shed(resp)


def test_receipt_poll_non_db_errors_still_propagate(tmp_path, monkeypatch):
    """Only DB availability errors are shed; anything else stays a 500 so real
    bugs remain loud."""
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)

    def _boom(sql, params=()):
        raise ValueError("not a db availability problem")

    monkeypatch.setattr(app.state.v2_store, "query", _boom)
    resp = client.get("/v2/agents/submit-bitset/receipts/any-receipt-id")
    assert resp.status_code == 500


def test_receipt_poll_concurrency_gate_sheds_429(tmp_path, monkeypatch):
    """With the gate at 1, a second concurrent poll is shed with the distinct
    receipt_poll_busy_retry reason before it can touch the DB."""
    monkeypatch.setenv("CATHEDRAL_V2_RECEIPT_POLL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("CATHEDRAL_SUBMIT_BUSY_WAIT_SECS", "0")
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app)

    started = threading.Event()
    release = threading.Event()

    def _slow_query(sql, params=()):
        started.set()
        release.wait(timeout=10)
        return []

    monkeypatch.setattr(app.state.v2_store, "query", _slow_query)

    results: list = []
    holder = threading.Thread(
        target=lambda: results.append(
            client.get("/v2/agents/submit-bitset/receipts/held-slot")))
    holder.start()
    try:
        assert started.wait(timeout=10), "first poll never reached the store"
        shed = client.get("/v2/agents/submit-bitset/receipts/second-poll")
        assert shed.status_code == 429, shed.text
        assert shed.headers["x-cathedral-rejection-reason"] == "receipt_poll_busy_retry"
        assert shed.headers["retry-after"] == "1"
        assert shed.json()["reason"] == "receipt_poll_busy_retry"
    finally:
        release.set()
        holder.join(timeout=10)

    # The held request completes normally once released (empty store -> 404).
    assert results and results[0].status_code == 404
