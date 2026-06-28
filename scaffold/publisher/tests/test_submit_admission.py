"""Tests for durable submit admission + async verification (Phase 3/4/5).

Covers: legacy contract preserved when off, 202 admission when on, idempotent
replay (no second attempt / no double payout), fairness ordering by received_at,
worker accept emits exactly one signed feed-row pair, reject path, backpressure
bounded wait, and the per-request sync override.
"""
from __future__ import annotations

import json
import time

import pytest
from starlette.testclient import TestClient

from scaffold.publisher import submit_admission
from scaffold.publisher.app import build_app, seed_challenge
from scaffold.publisher.auth import canonical_claim_bytes, sha256_hex
from scaffold.publisher.store import Store


SIGNING_KEY_HEX = "11" * 32
_FAMILY = "synthetic_boolean_v1"
# A trivially satisfiable 2-var CNF and a matching witness.
CNF = "p cnf 2 2\n1 2 0\n-1 2 0\n"
SOLUTION = "s SATISFIABLE\nv 1 2 0\n"
BAD_SOLUTION = "s SATISFIABLE\nv -1 -2 0\n"  # violates clause (1 2)
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _keypair():
    from bittensor_wallet import Keypair
    return Keypair.create_from_seed("0x" + "ab" * 32)


def _sign(kp, *, challenge_id, sol_sha, submitted_at):
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE, card_id=_FAMILY, miner_hotkey=kp.ss58_address,
        submitted_at=submitted_at, challenge_id=challenge_id,
        dimacs_solution_sha256=sol_sha)
    import base64
    return base64.b64encode(kp.sign(msg)).decode("ascii")


def _now_iso():
    from datetime import datetime, timezone
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _submit(client, kp, *, challenge_id, solution, extra_headers=None,
            path="/v1/agents/submit"):
    sol_sha = sha256_hex(solution)
    ts = _now_iso()
    sig = _sign(kp, challenge_id=challenge_id, sol_sha=sol_sha, submitted_at=ts)
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        path,
        data={"card_id": _FAMILY, "challenge_id": challenge_id,
              "dimacs_solution": solution, "submitted_at": ts},
        headers=headers,
    )


def _build(tmp_path, monkeypatch, *, async_on=False, verify_on=None, busy_wait="0.0",
           max_conc="24", hard_cap="8", service_role=None, require_worker=False):
    monkeypatch.setenv("CATHEDRAL_SUBMIT_ASYNC_ENABLED", "true" if async_on else "false")
    monkeypatch.setenv(
        "CATHEDRAL_SUBMIT_ASYNC_REQUIRE_WORKER",
        "true" if require_worker else "false")
    monkeypatch.setenv("CATHEDRAL_SUBMIT_BUSY_WAIT_SECS", busy_wait)
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MAX_CONCURRENCY", max_conc)
    monkeypatch.setenv("CATHEDRAL_SUBMIT_HARD_CAP", hard_cap)
    # Keep the verify-worker flag explicit so the startup misconfiguration WARNING
    # is deterministic. Default: unset (mirrors a fresh deploy that forgot it).
    if verify_on is None:
        monkeypatch.delenv("CATHEDRAL_ASYNC_VERIFY_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CATHEDRAL_ASYNC_VERIFY_ENABLED", "true" if verify_on else "false")
    if service_role is None:
        monkeypatch.delenv("CATHEDRAL_SERVICE_ROLE", raising=False)
    else:
        monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", service_role)
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    # build_app makes its own Store; reuse the same file path for seeding.
    store = Store(db)
    seed_challenge(store, challenge_id="c-1", tier=1, cnf_text=CNF)
    return app, store


# ---------------------------------------------------------------------------
# Legacy contract preserved (default off)
# ---------------------------------------------------------------------------
def test_legacy_sync_path_unchanged_when_async_off(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=False)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ranked"
    assert body["solve_rank"] == 1
    assert "eval_run_id" in body
    # legacy path does NOT create a durable receipt row
    assert store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                       "WHERE idempotency_key IS NOT NULL")[0]["n"] == 0


def test_legacy_prefixed_submit_path_still_reaches_submit_handler(
        tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=False)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(
            client, kp, challenge_id="c-1", solution=SOLUTION,
            path="/api/cathedral/v1/agents/submit")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ranked"
    assert body["solve_rank"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                       "WHERE idempotency_key IS NOT NULL")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Startup misconfiguration WARNING: async admission on but nothing drains it
# ---------------------------------------------------------------------------
def test_startup_warns_when_async_on_but_verify_worker_disabled(
        tmp_path, monkeypatch, capsys):
    # ASYNC admission on (202s flow) but the verify worker flag is unset -> no
    # process anywhere drains receipts. Must emit a loud UNPAID warning at startup.
    app, _ = _build(tmp_path, monkeypatch, async_on=True, verify_on=False)
    with TestClient(app):
        pass
    out = capsys.readouterr().out
    assert "[verify] WARNING" in out
    assert "CATHEDRAL_ASYNC_VERIFY_ENABLED" in out
    assert "UNPAID" in out


def test_startup_warns_when_async_on_and_role_does_not_run_worker(
        tmp_path, monkeypatch, capsys):
    # Verify worker enabled globally, but THIS process is a submit-only role that
    # does not run the worker loop. Still warn so an operator notices a missing
    # companion worker role rather than silently leaving receipts pending.
    app, _ = _build(tmp_path, monkeypatch, async_on=True, verify_on=True,
                    service_role="submit")
    with TestClient(app):
        pass
    out = capsys.readouterr().out
    assert "[verify] WARNING" in out
    assert "does not run the verify worker" in out


def test_startup_no_warning_when_async_off(tmp_path, monkeypatch, capsys):
    # Default-off: legacy synchronous path, no async receipts, no warning.
    app, _ = _build(tmp_path, monkeypatch, async_on=False)
    with TestClient(app):
        pass
    assert "[verify] WARNING" not in capsys.readouterr().out


def test_startup_no_warning_when_async_and_worker_both_on(
        tmp_path, monkeypatch, capsys):
    # Correctly configured worker-capable role: admission on AND worker on -> the
    # drain loop runs, so no misconfiguration warning.
    app, _ = _build(tmp_path, monkeypatch, async_on=True, verify_on=True,
                    service_role="worker")
    with TestClient(app):
        pass
    assert "[verify] WARNING" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 202 admission + receipt + idempotency
# ---------------------------------------------------------------------------
def test_async_admission_returns_202_pending_receipt(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        assert r.status_code == 202, r.text
        rec = r.json()
        assert rec["schema"] == "cathedral.submit_receipt.v2"
        assert rec["status"] == "pending"
        assert rec["receipt_id"].startswith("sub_")
        assert rec["challenge_id"] == "c-1"
        assert rec["dimacs_solution_sha256"] == sha256_hex(SOLUTION)
        assert rec["receipt_url"] == f"/v1/agents/receipts/{rec['receipt_id']}"
        # receipt endpoint resolves and echoes the same pending receipt
        g = client.get(rec["receipt_url"])
        assert g.status_code == 200
        assert g.json()["receipt_id"] == rec["receipt_id"]
        assert g.json()["status"] == "pending"
        assert g.json()["open"] is True
        assert g.json()["terminal"] is False


def test_receipt_exposes_verifying_state_after_worker_claim(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        rid = r.json()["receipt_id"]
        claimed = submit_admission.claim_pending(
            store,
            worker_id="w",
            now_iso="2026-06-27T00:00:00.000Z",
            lock_deadline_iso="2026-06-27T00:02:00.000Z",
            batch_size=1,
        )
        assert [row["id"] for row in claimed] == [rid]
        g = client.get(f"/v1/agents/receipts/{rid}").json()
    assert g["status"] == "verifying"
    assert g["open"] is True
    assert g["terminal"] is False


def test_idempotent_replay_returns_same_receipt_no_second_attempt(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r1 = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        assert r1.status_code == 202
        rid = r1.json()["receipt_id"]
        # exact same solution again -> idempotent replay, 200, same receipt id
        r2 = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        assert r2.status_code == 200, r2.text
        assert r2.json()["receipt_id"] == rid
    # exactly one durable attempt row exists
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 1


def test_receipt_unknown_id_is_404(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    with TestClient(app) as client:
        assert client.get("/v1/agents/receipts/sub_nope").status_code == 404


def test_sync_override_forces_legacy_200_even_when_async_on(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION,
                    extra_headers={"X-Cathedral-Submit-Mode": "sync"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ranked"


def test_public_async_oversized_body_is_413_never_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MAX_SOLUTION_BYTES", "10")
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
    assert r.status_code == 413, r.text
    assert r.headers["X-Cathedral-Rejection-Reason"] == "solution_too_large"
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 0


def test_public_async_fails_closed_without_worker_heartbeat(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True, require_worker=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
    assert r.status_code == 503, r.text
    assert r.headers["X-Cathedral-Rejection-Reason"] == "async_worker_unavailable"
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 0


# ---------------------------------------------------------------------------
# Worker verification: accept emits signed rows, no double payout
# ---------------------------------------------------------------------------
def test_worker_verifies_pending_to_ranked_and_emits_one_feed_pair(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    received_at = "2026-06-27T00:00:01.000Z"
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        rid = r.json()["receipt_id"]
        store.write(lambda conn: conn.execute(
            "UPDATE per_miner_attempts SET received_at_iso=? WHERE id=?",
            (received_at, rid)))
        # drive the worker tick manually (same closure the bg loop uses)
        processed = app.state.async_verify_tick(worker_id="t", batch_size=8)
        assert processed == 1
        g = client.get(f"/v1/agents/receipts/{rid}").json()
        assert g["status"] == "ranked"
        assert g["solve_rank"] == 1
        assert g["weighted_score"] is not None
        assert g.get("eval_run_id")
    # exactly one v6+v5 signed feed pair for this miner
    rows = store.query("SELECT id FROM eval_runs WHERE miner_hotkey=?",
                       (kp.ss58_address,))
    assert len(rows) == 2  # v6 + v5compat mirror
    # a second worker tick is a no-op (nothing pending) -> no double payout
    assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 0
    rows2 = store.query("SELECT id FROM eval_runs WHERE miner_hotkey=?",
                        (kp.ss58_address,))
    assert len(rows2) == 2
    # exactly one distinct-solver claim
    solves = store.query("SELECT COUNT(*) AS n FROM lane_challenge_solves "
                         "WHERE challenge_id='c-1'")[0]["n"]
    assert solves == 1
    solved_at = store.query(
        "SELECT solved_at_iso FROM lane_challenge_solves WHERE challenge_id='c-1'"
    )[0]["solved_at_iso"]
    assert solved_at == received_at
    feed_rows = store.query("SELECT row_json FROM eval_runs WHERE miner_hotkey=?",
                            (kp.ss58_address,))
    assert {json.loads(r["row_json"])["ran_at"] for r in feed_rows} == {received_at}


def test_replay_after_verification_does_not_double_pay(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        app.state.async_verify_tick(worker_id="t", batch_size=8)
        # replay same solution -> idempotent receipt, still ranked, no new attempt
        r2 = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
        assert r2.status_code == 200
        assert r2.json()["status"] == "ranked"
        # tick again to be safe
        app.state.async_verify_tick(worker_id="t", batch_size=8)
    rows = store.query("SELECT id FROM eval_runs WHERE miner_hotkey=?",
                       (kp.ss58_address,))
    assert len(rows) == 2  # still exactly one pair


def test_worker_rejects_bad_solution(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp = _keypair()
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id="c-1", solution=BAD_SOLUTION)
        assert r.status_code == 202
        rid = r.json()["receipt_id"]
        app.state.async_verify_tick(worker_id="t", batch_size=8)
        g = client.get(f"/v1/agents/receipts/{rid}").json()
    assert g["status"] == "rejected"
    assert g["rejection_reason"] == "solution_unsatisfied"
    # rejected -> no feed rows, no solve claim
    assert store.query("SELECT COUNT(*) AS n FROM eval_runs")[0]["n"] == 0


def test_queue_backpressure_rejects_new_admission_but_allows_replay(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SUBMIT_QUEUE_BACKPRESSURE_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_SUBMIT_QUEUE_MAX_PENDING", "1")
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp1 = _keypair()
    from bittensor_wallet import Keypair
    kp2 = Keypair.create_from_seed("0x" + "cd" * 32)
    submit_admission.record_worker_heartbeat(
        store, worker_id="worker:bp", service_role="worker",
        now_iso=_now_iso(), event="started")
    with TestClient(app) as client:
        r1 = _submit(client, kp1, challenge_id="c-1", solution=SOLUTION)
        assert r1.status_code == 202, r1.text
        rid = r1.json()["receipt_id"]
        replay = _submit(client, kp1, challenge_id="c-1", solution=SOLUTION)
        assert replay.status_code == 200, replay.text
        assert replay.json()["receipt_id"] == rid
        r2 = _submit(client, kp2, challenge_id="c-1", solution=SOLUTION)
    assert r2.status_code == 503, r2.text
    assert r2.headers["X-Cathedral-Rejection-Reason"] == "submit_queue_backpressure"
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 1


def test_queue_backpressure_does_not_shed_without_active_worker(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SUBMIT_QUEUE_BACKPRESSURE_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_SUBMIT_QUEUE_MAX_PENDING", "1")
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    kp1 = _keypair()
    from bittensor_wallet import Keypair
    kp2 = Keypair.create_from_seed("0x" + "ef" * 32)
    with TestClient(app) as client:
        r1 = _submit(client, kp1, challenge_id="c-1", solution=SOLUTION)
        r2 = _submit(client, kp2, challenge_id="c-1", solution=SOLUTION)
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 202, r2.text
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 2


def test_queue_backpressure_ignores_shadow_rows(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    now = _now_iso()
    submit_admission.record_worker_heartbeat(
        store, worker_id="worker:shadow", service_role="worker",
        now_iso=now, event="started")

    def _do(conn):
        conn.execute(
            "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
            "epoch, status, dimacs_solution_sha256, submitted_at, recorded_at_iso, "
            "signature, idempotency_key, received_at_iso, challenge_kind, "
            "solution_body, attempt_count) "
            "VALUES ('shadow-row', 'pm-test', 'hk', 0, 'pending', 'sha', ?, ?, "
            "'sig', 'shadow-key', ?, ?, ?, 0)",
            (now, now, now, submit_admission.KIND_PER_MINER_SHADOW, SOLUTION))
        return submit_admission.queue_backpressure_decision(
            conn, now_iso=now, max_pending=1, max_worker_lag_secs=1,
            worker_stale_secs=120)

    decision = store.write(_do)
    assert decision["active_workers"] == 1
    assert decision["pending"] == 0
    assert decision["limited"] is False


def test_worker_heartbeat_metrics_are_visible(tmp_path, monkeypatch):
    _app, store = _build(tmp_path, monkeypatch, async_on=True)
    now = "2026-06-27T00:00:00.000Z"
    submit_admission.record_worker_heartbeat(
        store, worker_id="worker:1", service_role="worker",
        now_iso=now, event="started")
    submit_admission.record_worker_heartbeat(
        store, worker_id="worker:1", service_role="worker",
        now_iso="2026-06-27T00:00:01.000Z", event="tick", processed=3)
    metrics = submit_admission.worker_metrics(
        store, now_iso="2026-06-27T00:00:02.000Z", stale_secs=120)
    assert metrics["active_workers"] == 1
    assert metrics["workers"][0]["worker_id"] == "worker:1"
    assert metrics["workers"][0]["processed_total"] == 3
    assert metrics["workers"][0]["last_batch_size"] == 3


# ---------------------------------------------------------------------------
# Fairness: claim order is received_at ascending
# ---------------------------------------------------------------------------
def test_claim_pending_orders_by_received_at(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    # insert three pending attempts out of received_at order
    def ins(rid, recv):
        def _do(conn):
            conn.execute(
                "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                "epoch, status, dimacs_solution_sha256, submitted_at, recorded_at_iso, "
                "signature, idempotency_key, received_at_iso, challenge_kind, "
                "solution_body, attempt_count) "
                "VALUES (?, 'c-1', 'hk', 0, 'pending', 'sha', ?, ?, ?, ?, ?, 'public', ?, 0)",
                (rid, recv, recv, "sig-" + rid, "idem-" + rid, recv, SOLUTION))
        store.write(_do)
    ins("b", "2026-06-27T00:00:02.000Z")
    ins("a", "2026-06-27T00:00:01.000Z")
    ins("c", "2026-06-27T00:00:03.000Z")
    claimed = submit_admission.claim_pending(
        store, worker_id="w", now_iso="2026-06-27T01:00:00.000Z",
        lock_deadline_iso="2026-06-27T01:02:00.000Z", batch_size=3)
    assert [row["id"] for row in claimed] == ["a", "b", "c"]
    # all are now verifying / locked
    for row in claimed:
        assert row["status"] == "verifying"
        assert row["locked_by"] == "w"


def test_claim_skips_already_locked_rows(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, async_on=True)
    def _do(conn):
        conn.execute(
            "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, "
            "status, dimacs_solution_sha256, submitted_at, recorded_at_iso, signature, "
            "idempotency_key, received_at_iso, challenge_kind, solution_body, "
            "attempt_count, locked_until_iso) "
            "VALUES ('x', 'c-1', 'hk', 0, 'verifying', 'sha', 't', 't', 's', 'i', "
            "'2026-06-27T00:00:01.000Z', 'public', ?, 1, '2026-06-27T02:00:00.000Z')",
            (SOLUTION,))
    store.write(_do)
    # lock not yet expired -> not claimable
    claimed = submit_admission.claim_pending(
        store, worker_id="w", now_iso="2026-06-27T01:00:00.000Z",
        lock_deadline_iso="2026-06-27T01:02:00.000Z", batch_size=5)
    assert claimed == []
    # after the lock expires, it is reclaimable (crash safety)
    reclaimed = submit_admission.claim_pending(
        store, worker_id="w2", now_iso="2026-06-27T03:00:00.000Z",
        lock_deadline_iso="2026-06-27T03:02:00.000Z", batch_size=5)
    assert [row["id"] for row in reclaimed] == ["x"]


# ---------------------------------------------------------------------------
# Idempotency key shape
# ---------------------------------------------------------------------------
def test_idempotency_key_is_sha256_of_triple():
    k = submit_admission.idempotency_key("hk", "c-1", "deadbeef")
    assert k == sha256_hex("hk\x00c-1\x00deadbeef")
    # different solution -> different key
    assert k != submit_admission.idempotency_key("hk", "c-1", "feedface")


# ---------------------------------------------------------------------------
# Phase 3 backpressure: bounded wait then 429
# ---------------------------------------------------------------------------
def test_backpressure_waits_then_accepts_when_slot_frees(tmp_path, monkeypatch):
    # hard cap 1 -> the gate serializes; a bounded wait should let a queued
    # submit through once the holder releases rather than instant-429.
    app, store = _build(tmp_path, monkeypatch, async_on=False,
                        busy_wait="0.4", max_conc="1", hard_cap="1")
    kp = _keypair()
    with TestClient(app) as client:
        # single sequential submit still succeeds (gate released between calls)
        r = _submit(client, kp, challenge_id="c-1", solution=SOLUTION)
    assert r.status_code == 200, r.text


def test_backpressure_bounded_wait_then_429_when_gate_exhausted(tmp_path, monkeypatch):
    """When the single submit slot is fully held by an in-flight verification, a
    concurrent submit waits up to the bounded window and THEN returns 429
    submit_busy_retry with Retry-After (hard ceiling preserved, just friendlier).

    We hold the slot by making one verification block, then fire a second submit
    from another thread and assert it 429s after waiting (not instantly)."""
    import threading
    app, store = _build(tmp_path, monkeypatch, async_on=False,
                        busy_wait="0.4", max_conc="1", hard_cap="1")

    in_verify = threading.Event()
    release = threading.Event()
    import scaffold.publisher.app as appmod
    real_verify = appmod.verify_dimacs_solution

    def _blocking_verify(cnf, sol):
        in_verify.set()
        release.wait(timeout=5)
        return real_verify(cnf, sol)

    monkeypatch.setattr(appmod, "verify_dimacs_solution", _blocking_verify)

    kp1 = _keypair()
    from bittensor_wallet import Keypair
    kp2 = Keypair.create_from_seed("0x" + "cd" * 32)

    results: dict[str, object] = {}
    with TestClient(app) as client:
        def _hold():
            results["hold"] = _submit(client, kp1, challenge_id="c-1", solution=SOLUTION)
        t = threading.Thread(target=_hold)
        t.start()
        assert in_verify.wait(timeout=5), "first submit never entered verification"
        # slot is now held; second submit must wait the bounded window then 429
        start = time.monotonic()
        r2 = _submit(client, kp2, challenge_id="c-1", solution=SOLUTION)
        elapsed = time.monotonic() - start
        release.set()
        t.join(timeout=5)

    assert r2.status_code == 429, r2.text
    assert r2.headers.get("retry-after") is not None
    assert r2.headers.get("x-cathedral-rejection-reason") == "submit_busy_retry"
    assert r2.headers.get("content-type") == "application/json"
    retry_payload = r2.json()
    assert retry_payload["detail"] == "submit_busy_retry"
    assert retry_payload["retry_after_seconds"] >= 1
    assert retry_payload["retry_at"].endswith("Z")
    assert elapsed >= 0.3, f"did not wait the bounded window (elapsed={elapsed:.3f}s)"
    # the held request still succeeded once it got the slot
    assert results["hold"].status_code == 200
