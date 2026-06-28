"""TRACK 1: durable async admission for the PRIVATE (pm-*) submit lane.

The public lane already had durable admission (test_submit_admission.py); this
suite exercises the per-miner lane that carries the live traffic. It asserts the
hard guarantees the orchestrator named:

* DEFAULT-OFF legacy contract is byte-for-byte preserved (flags unset -> the
  inline synchronous pm path runs exactly as before, no durable receipt row).
* When pm-async is on, a pm-* submit does only cheap work and returns 202 +
  receipt; the worker re-materializes the miner's own CNF, verifies, and records
  the ranked/rejected result into the SAME ledger scoring reads.
* Idempotent replay returns the SAME receipt (no second attempt / no double pay).
* Fairness: the worker claims pending pm-* rows in received_at order.
* No double payout: a second worker tick (or a post-verify replay) never adds a
  second per_miner_solves claim or a second signed feed pair.
* SHADOW mode: the inline result stays authoritative for payout while the worker
  re-verifies into shadow_* columns and logs any async-vs-inline divergence — no
  payout change.
* Queue visibility metrics populate (pending count, oldest age, worker lag,
  accepted/sec, rejected/sec).
"""
from __future__ import annotations

import base64
import json

import pytest
from starlette.testclient import TestClient

from scaffold.publisher import per_miner as pm
from scaffold.publisher import submit_admission
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes, sha256_hex
from scaffold.publisher.store import Store


SIGNING_KEY_HEX = "11" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
_SEED_SECRET = "pm-async-test-stable-seed"


def _keypair(uri="//PMAsyncMiner"):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _now_iso():
    from datetime import datetime, timezone
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _pm_env(monkeypatch, *, pm_async=False, shadow=False, verify_on=True,
            service_role="all", require_worker=False):
    """Enable per-miner + (optionally) pm-async / shadow. The submit-async flag is
    the public gate the pm flag rides on top of, so it is always set when pm-async
    is requested."""
    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SEED_SECRET", _SEED_SECRET)
    monkeypatch.setenv("CATHEDRAL_PERMINER_ALLOTMENT_T1", "4")
    monkeypatch.setenv("CATHEDRAL_PERMINER_ALLOTMENT_T2", "4")
    monkeypatch.delenv("CATHEDRAL_PERMINER_SHADOW", raising=False)
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "test-admin-token")
    # SHADOW is a MODE OF pm-async: the async path runs in parallel while the inline
    # result stays authoritative. So shadow implies the pm-async chain is wired on
    # (the gate just routes inline-first instead of returning a 202). pm-async rides
    # on the public submit-async flag, so that is set too.
    pm_chain_on = pm_async or shadow
    monkeypatch.setenv(
        "CATHEDRAL_SUBMIT_ASYNC_ENABLED", "true" if pm_chain_on else "false")
    monkeypatch.setenv(
        "CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED", "true" if pm_chain_on else "false")
    monkeypatch.setenv("CATHEDRAL_PM_ASYNC_SHADOW", "true" if shadow else "false")
    monkeypatch.setenv("CATHEDRAL_ASYNC_VERIFY_ENABLED", "true" if verify_on else "false")
    monkeypatch.setenv(
        "CATHEDRAL_SUBMIT_ASYNC_REQUIRE_WORKER",
        "true" if require_worker else "false")
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", service_role)


def _build(tmp_path, monkeypatch, **env):
    _pm_env(monkeypatch, **env)
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    store = Store(db)
    return app, store


def _pm_solution_for(kp, *, tier=1, seq=0):
    """Generate the miner's own (challenge_id, valid DIMACS body) for this epoch.

    In tests there is no coldkey map, so the scoring identity == the hotkey, which
    is exactly what generate_instance keys the CNF on."""
    epoch = pm.current_epoch()
    cid, _cnf, assignment = pm.generate_instance(kp.ss58_address, epoch, tier, seq)
    body = "s SATISFIABLE\nv " + " ".join(str(x) for x in assignment) + " 0\n"
    return cid, body


def _bad_solution_for(kp, *, tier=1, seq=0):
    """A challenge id this miner owns, but with a flipped (unsatisfying) witness."""
    epoch = pm.current_epoch()
    cid, _cnf, assignment = pm.generate_instance(kp.ss58_address, epoch, tier, seq)
    flipped = [-x for x in assignment]
    body = "s SATISFIABLE\nv " + " ".join(str(x) for x in flipped) + " 0\n"
    return cid, body


def _submit(client, kp, *, challenge_id, solution, extra_headers=None,
            submitted_at=None):
    sol_sha = sha256_hex(solution)
    ts = submitted_at or _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE, card_id=_FAMILY, miner_hotkey=kp.ss58_address,
        submitted_at=ts, challenge_id=challenge_id, dimacs_solution_sha256=sol_sha)
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    headers = {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        "/v1/agents/submit",
        data={"card_id": _FAMILY, "challenge_id": challenge_id,
              "dimacs_solution": solution, "submitted_at": ts},
        headers=headers,
    )


def _pm_read_headers(kp, *, submitted_at=None):
    ts = submitted_at or _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE, card_id=_FAMILY, miner_hotkey=kp.ss58_address,
        submitted_at=ts, challenge_id="", dimacs_solution_sha256="")
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _metrics(client):
    r = client.get(
        "/v1/admin/synthetic-boolean/submit-metrics",
        headers={"Authorization": "Bearer test-admin-token"})
    assert r.status_code == 200, r.text
    return r.json()


def _solves(store, cid, hotkey):
    return store.query(
        "SELECT COUNT(*) AS n FROM per_miner_solves "
        "WHERE challenge_id=? AND miner_hotkey=?", (cid, hotkey))[0]["n"]


def _feed_pairs(store, hotkey):
    return store.query(
        "SELECT COUNT(*) AS n FROM eval_runs WHERE miner_hotkey=?", (hotkey,))[0]["n"]


# ---------------------------------------------------------------------------
# DEFAULT-OFF legacy contract: byte-for-byte inline synchronous pm path.
# ---------------------------------------------------------------------------
def test_pm_legacy_sync_path_unchanged_when_pm_async_off(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=False)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "ranked"
    assert out["solve_rank"] == 1
    assert "eval_run_id" in out
    assert out["challenge_id"] == cid
    # inline path records the solve directly (one distinct-solver claim)
    assert _solves(store, cid, kp.ss58_address) == 1
    # inline path never creates a durable async receipt row
    assert store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                       "WHERE idempotency_key IS NOT NULL")[0]["n"] == 0


def test_pm_legacy_reject_contract_unchanged_when_off(tmp_path, monkeypatch):
    # A bad witness rejects with 400 + the exact rejection-reason header inline.
    app, store = _build(tmp_path, monkeypatch, pm_async=False)
    kp = _keypair()
    cid, bad = _bad_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=bad)
    assert r.status_code == 400, r.text
    assert r.headers.get("X-Cathedral-Rejection-Reason") in {
        "solution_unsatisfied", "witness_check_failed"}
    assert _solves(store, cid, kp.ss58_address) == 0
    assert _feed_pairs(store, kp.ss58_address) == 0


def test_pm_cnf_fetch_recovers_without_assignment_row_or_legacy_scan_flag(
        tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=False)
    monkeypatch.setenv("CATHEDRAL_PERMINER_LEGACY_ID_SCAN", "0")
    kp = _keypair("//PMFetchNoAssignment")

    with TestClient(app) as client:
        headers = _pm_read_headers(kp)
        listed = client.get(
            "/v1/synthetic-boolean/per-miner/challenges",
            headers=headers,
        )
        assert listed.status_code == 200, listed.text
        item = listed.json()["items"][-1]
        before = store.query(
            "SELECT COUNT(*) AS n FROM per_miner_assignments "
            "WHERE challenge_id=?",
            (item["challenge_id"],),
        )[0]["n"]

        cnf = client.get(
            "/v1/synthetic-boolean/per-miner/cnf",
            params={"challenge_id": item["challenge_id"]},
            headers=headers,
        )

    assert before == 0
    assert cnf.status_code == 200, cnf.text
    assert cnf.headers["X-Perminer-Challenge-Id"] == item["challenge_id"]
    assert int(cnf.headers["X-Perminer-Tier"]) == int(item["tier"])
    assert int(cnf.headers["X-Perminer-Seq"]) == int(item["seq"])
    after = store.query(
        "SELECT COUNT(*) AS n FROM per_miner_assignments WHERE challenge_id=?",
        (item["challenge_id"],),
    )[0]["n"]
    assert after == 1


# ---------------------------------------------------------------------------
# pm-async on: 202 admission + receipt, worker drains to ranked.
# ---------------------------------------------------------------------------
def test_pm_async_admission_returns_202_then_worker_ranks(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    received_at = "2026-06-27T00:00:01.000Z"
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
        assert r.status_code == 202, r.text
        rec = r.json()
        store.write(lambda conn: conn.execute(
            "UPDATE per_miner_attempts SET received_at_iso=? WHERE id=?",
            (received_at, rec["receipt_id"])))
        assert rec["schema"] == "cathedral.submit_receipt.v2"
        assert rec["status"] == "pending"
        assert rec["receipt_id"].startswith("sub_")
        assert rec["challenge_id"] == cid
        # nothing paid yet: inline did NO heavy work
        assert _solves(store, cid, kp.ss58_address) == 0
        # drain
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 1
        g = client.get(rec["receipt_url"]).json()
    assert g["status"] == "ranked"
    assert g["solve_rank"] == 1
    assert g.get("eval_run_id")
    # exactly one distinct-solver claim + one signed feed pair (v6 + v5 mirror)
    assert _solves(store, cid, kp.ss58_address) == 1
    assert _feed_pairs(store, kp.ss58_address) == 2
    solved_at = store.query(
        "SELECT solved_at_iso FROM per_miner_solves WHERE challenge_id=?",
        (cid,))[0]["solved_at_iso"]
    assert solved_at == received_at
    feed_rows = store.query("SELECT row_json FROM eval_runs WHERE miner_hotkey=?",
                            (kp.ss58_address,))
    assert {json.loads(r["row_json"])["ran_at"] for r in feed_rows} == {received_at}


def test_pm_async_falls_back_to_sync_without_worker_heartbeat(tmp_path, monkeypatch):
    app, store = _build(
        tmp_path, monkeypatch, pm_async=True, verify_on=False, require_worker=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ranked"
    assert r.json()["solve_rank"] == 1
    assert store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                       "WHERE idempotency_key IS NOT NULL")[0]["n"] == 0
    assert _solves(store, cid, kp.ss58_address) == 1


def test_pm_async_idempotent_replay_same_receipt_no_second_attempt(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r1 = _submit(client, kp, challenge_id=cid, solution=body)
        assert r1.status_code == 202
        rid = r1.json()["receipt_id"]
        # identical solution again before drain -> same receipt, 200 not 202
        r2 = _submit(client, kp, challenge_id=cid, solution=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["receipt_id"] == rid
    n = store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                    "WHERE idempotency_key IS NOT NULL")[0]["n"]
    assert n == 1


def test_pm_async_replay_after_verify_does_not_double_pay(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        _submit(client, kp, challenge_id=cid, solution=body)
        app.state.async_verify_tick(worker_id="t", batch_size=8)
        # replay the same solution AFTER it was already paid
        r2 = _submit(client, kp, challenge_id=cid, solution=body)
        assert r2.status_code == 200
        assert r2.json()["status"] == "ranked"
        # extra ticks must be no-ops
        app.state.async_verify_tick(worker_id="t", batch_size=8)
    assert _solves(store, cid, kp.ss58_address) == 1
    assert _feed_pairs(store, kp.ss58_address) == 2


def test_pm_async_second_tick_is_noop_no_double_payout(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        _submit(client, kp, challenge_id=cid, solution=body)
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 1
        # nothing left pending
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 0
    assert _solves(store, cid, kp.ss58_address) == 1
    assert _feed_pairs(store, kp.ss58_address) == 2


def test_pm_async_worker_rejects_bad_witness_no_payout(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, bad = _bad_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=bad)
        assert r.status_code == 202, r.text  # cheap checks pass; heavy verify deferred
        rid = r.json()["receipt_id"]
        app.state.async_verify_tick(worker_id="t", batch_size=8)
        g = client.get(f"/v1/agents/receipts/{rid}").json()
    assert g["status"] == "rejected"
    assert g["rejection_reason"] in {"solution_unsatisfied", "witness_check_failed"}
    assert _solves(store, cid, kp.ss58_address) == 0
    assert _feed_pairs(store, kp.ss58_address) == 0


def test_pm_async_oversized_body_is_413_never_queued(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_PM_SUBMIT_MAX_SOLUTION_BYTES", "64")
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, _body = _pm_solution_for(kp)
    big = "s SATISFIABLE\nv " + ("1 " * 500) + "0\n"
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=big)
    assert r.status_code == 413, r.text
    assert r.headers.get("X-Cathedral-Rejection-Reason") == "solution_too_large"
    # never persisted, never queued
    assert store.query("SELECT COUNT(*) AS n FROM per_miner_attempts "
                       "WHERE idempotency_key IS NOT NULL")[0]["n"] == 0


# ---------------------------------------------------------------------------
# Fairness: worker claims pm-* rows in received_at order.
# ---------------------------------------------------------------------------
def test_pm_async_claim_orders_by_received_at(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)

    def ins(rid, recv):
        def _do(conn):
            conn.execute(
                "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                "epoch, status, dimacs_solution_sha256, submitted_at, recorded_at_iso, "
                "signature, idempotency_key, received_at_iso, challenge_kind, "
                "solution_body, assignment_identity, attempt_count) "
                "VALUES (?, 'pm-t1-e1-x', 'hk', 0, 'pending', 'sha', ?, ?, ?, ?, ?, "
                "'per_miner', 'body', 'hk', 0)",
                (rid, recv, recv, "sig-" + rid, "idem-" + rid, recv))
        store.write(_do)
    ins("b", "2026-06-27T00:00:02.000Z")
    ins("a", "2026-06-27T00:00:01.000Z")
    ins("c", "2026-06-27T00:00:03.000Z")
    claimed = submit_admission.claim_pending(
        store, worker_id="w", now_iso="2026-06-27T01:00:00.000Z",
        lock_deadline_iso="2026-06-27T01:02:00.000Z", batch_size=3)
    assert [row["id"] for row in claimed] == ["a", "b", "c"]
    for row in claimed:
        assert row["status"] == "verifying"
        assert row["challenge_kind"] == "per_miner"


# ---------------------------------------------------------------------------
# SHADOW mode: inline authoritative, worker re-verifies into shadow_* only.
# ---------------------------------------------------------------------------
def test_pm_shadow_inline_authoritative_and_worker_writes_shadow_only(
        tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, shadow=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
        # inline path stays authoritative: synchronous 200 ranked (NOT a 202)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ranked"
        # inline already paid
        assert _solves(store, cid, kp.ss58_address) == 1
        inline_pairs = _feed_pairs(store, kp.ss58_address)
        # a shadow twin was queued (kind=per_miner_shadow)
        shadow = store.query(
            "SELECT * FROM per_miner_attempts WHERE challenge_kind=?",
            ("per_miner_shadow",))
        assert len(shadow) == 1
        assert shadow[0]["status"] == "pending"
        # worker drains the shadow twin
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 1
        after = store.query(
            "SELECT * FROM per_miner_attempts WHERE challenge_kind=?",
            ("per_miner_shadow",))
    # shadow verdict recorded in shadow_* columns
    assert after[0]["shadow_status"] == "ranked"
    assert after[0]["shadow_rejection_reason"] is None
    # NO payout change from the shadow path: still one solve, same feed pair count
    assert _solves(store, cid, kp.ss58_address) == 1
    assert _feed_pairs(store, kp.ss58_address) == inline_pairs


def test_pm_shadow_logs_divergence_without_changing_payout(tmp_path, monkeypatch, capsys):
    # Force an inline-vs-async divergence by stamping the shadow twin's inline
    # marker as a reject while the (valid) solution will async-verify to ranked.
    app, store = _build(tmp_path, monkeypatch, shadow=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
        assert r.status_code == 200
        before_solves = _solves(store, cid, kp.ss58_address)
        before_pairs = _feed_pairs(store, kp.ss58_address)
        # rewrite the shadow twin so its stamped inline verdict is "rejected"
        store.write(lambda conn: conn.execute(
            "UPDATE per_miner_attempts SET rejection_reason='solution_unsatisfied' "
            "WHERE challenge_kind='per_miner_shadow'"))
        capsys.readouterr()  # clear
        app.state.async_verify_tick(worker_id="t", batch_size=8)
    out = capsys.readouterr().out
    assert "pm_shadow_divergence" in out
    # divergence logging does not touch payout state
    assert _solves(store, cid, kp.ss58_address) == before_solves
    assert _feed_pairs(store, kp.ss58_address) == before_pairs


def test_pm_shadow_excluded_from_attempt_reason_stats(tmp_path, monkeypatch):
    # The default-off shadow diagnostic must never alter miner-facing attempt/reason
    # counts. Even after a shadow twin is written + verified, the reason-count query
    # for the miner ignores per_miner_shadow rows.
    app, store = _build(tmp_path, monkeypatch, shadow=True)
    kp = _keypair()
    cid, bad = _bad_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=bad)
        assert r.status_code == 400  # inline reject authoritative
        app.state.async_verify_tick(worker_id="t", batch_size=8)
    # the inline reject wrote ONE non-shadow attempt row; the shadow twin is excluded
    non_shadow = store.query(
        "SELECT COUNT(*) AS n FROM per_miner_attempts "
        "WHERE miner_hotkey=? AND (challenge_kind IS NULL OR challenge_kind!='per_miner_shadow')",
        (kp.ss58_address,))[0]["n"]
    shadow_rows = store.query(
        "SELECT COUNT(*) AS n FROM per_miner_attempts WHERE challenge_kind='per_miner_shadow'"
    )[0]["n"]
    assert shadow_rows == 1
    assert non_shadow == 1


# ---------------------------------------------------------------------------
# Queue visibility metrics.
# ---------------------------------------------------------------------------
def test_queue_metrics_populate(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        # empty queue first
        q0 = _metrics(client)["queue"]
        assert q0["total_pending"] == 0
        assert q0["pm_async_enabled"] is True
        # admit one pending pm submission
        _submit(client, kp, challenge_id=cid, solution=body)
        q1 = _metrics(client)["queue"]
        assert q1["total_pending"] == 1
        assert q1["by_status"]["pending"] == 1
        assert q1["admitted_in_window"] >= 1
        assert q1["admitted_per_sec"] > 0.0
        assert q1["oldest_received_at"] is not None
        assert q1["worker_lag_secs"] is not None and q1["worker_lag_secs"] >= 0.0
        assert "per_miner" in q1["by_kind"]
        assert q1["by_kind"]["per_miner"]["pending"] == 1
        # drain, then accepted rate shows up and pending clears
        app.state.async_verify_tick(worker_id="t", batch_size=8)
        q2 = _metrics(client)["queue"]
        assert q2["total_pending"] == 0
        assert q2["accepted_in_window"] >= 1
        assert q2["ranked_in_window"] >= 1
        assert q2["ranked_per_sec"] > 0.0


def test_submit_metrics_degrades_when_queue_queries_fail(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, pm_async=True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated queue query failure")

    monkeypatch.setattr(submit_admission, "queue_metrics", _boom)
    monkeypatch.setattr(submit_admission, "queue_rates", _boom)
    with TestClient(app) as client:
        q = _metrics(client)["queue"]
    assert q["metrics_status"] == "degraded"
    assert q["metrics_error"] == "queue_metrics_failed"
    assert q["rates_status"] == "degraded"
    assert q["total_pending"] is None


def test_queue_metrics_worker_lag_grows_with_oldest_pending(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, pm_async=True)

    def ins(rid, recv):
        def _do(conn):
            conn.execute(
                "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, "
                "epoch, status, dimacs_solution_sha256, submitted_at, recorded_at_iso, "
                "signature, idempotency_key, received_at_iso, challenge_kind, "
                "solution_body, assignment_identity, attempt_count) "
                "VALUES (?, 'pm-t1-e1-x', 'hk', 0, 'pending', 'sha', ?, ?, ?, ?, ?, "
                "'per_miner', 'body', 'hk', 0)",
                (rid, recv, recv, "sig-" + rid, "idem-" + rid, recv))
        store.write(_do)
    # an old pending row -> large positive worker lag
    ins("old", "2020-01-01T00:00:00.000Z")
    now = "2020-01-01T00:01:00.000Z"
    q = submit_admission.queue_metrics(store, now_iso=now)
    assert q["total_pending"] == 1
    assert q["oldest_received_at"] == "2020-01-01T00:00:00.000Z"
    assert q["worker_lag_secs"] == pytest.approx(60.0, abs=0.5)


# ---------------------------------------------------------------------------
# P2 (Track 1): shadow drains must NOT inflate the LIVE accepted/rejected rates.
# ---------------------------------------------------------------------------
def test_shadow_drain_does_not_inflate_live_accepted_rates(tmp_path, monkeypatch):
    # In SHADOW mode the inline path is authoritative and the worker re-verifies
    # the shadow twin into shadow_* (stamping a terminal status + verified_at_iso).
    # The headline accepted/rejected rates on the submit-metrics queue surface must
    # count only LIVE async kinds (public + per_miner); a shadow drain must show up
    # ONLY in the separate shadow_* rate fields, never the live ones — otherwise an
    # operator running shadow-only mode would think LIVE pm was draining.
    app, store = _build(tmp_path, monkeypatch, shadow=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        r = _submit(client, kp, challenge_id=cid, solution=body)
        assert r.status_code == 200, r.text  # inline authoritative
        # Drain the shadow twin (records into shadow_* + a terminal status).
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 1
        q = _metrics(client)["queue"]
    # The shadow twin verified to ranked -> it must NOT appear in the live counters.
    assert q["accepted_in_window"] == 0
    assert q["accepted_per_sec"] == 0.0
    assert q["rejected_in_window"] == 0
    assert q["rejected_per_sec"] == 0.0
    # ...but it IS visible in the dedicated shadow counters.
    assert q["shadow_accepted_in_window"] == 1
    assert q["shadow_accepted_per_sec"] > 0.0


def test_live_drain_inflates_live_rates_not_shadow(tmp_path, monkeypatch):
    # Mirror of the above for the LIVE lane: a live pm drain shows up in the live
    # accepted counters and NOT the shadow counters.
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    with TestClient(app) as client:
        _submit(client, kp, challenge_id=cid, solution=body)
        assert app.state.async_verify_tick(worker_id="t", batch_size=8) == 1
        q = _metrics(client)["queue"]
    assert q["accepted_in_window"] == 1
    assert q["accepted_per_sec"] > 0.0
    assert q["shadow_accepted_in_window"] == 0
    assert q["shadow_accepted_per_sec"] == 0.0


# ---------------------------------------------------------------------------
# P1 (Track 1): shadow/live idempotency MUST be namespaced — a SAME-payload retry
# after cutover creates a NEW live authoritative receipt, never a shadow replay.
# ---------------------------------------------------------------------------
def test_shadow_and_live_idempotency_keys_are_namespaced():
    # Unit-level proof of the fix: the shadow twin's idempotency key must differ
    # from the live key for the SAME (miner, challenge, solution) triple, so the
    # two rows can coexist in the UNIQUE idempotency_key index and a live lookup
    # can never match the shadow row.
    hk, cid, sha = "hk", "pm-t1-e1-x", "deadbeef"
    live = submit_admission.idempotency_key(hk, cid, sha)
    shadow = submit_admission.shadow_idempotency_key(hk, cid, sha)
    assert live != shadow


def test_admit_pending_with_namespaced_shadow_row_present_creates_fresh_live(
        tmp_path, monkeypatch):
    # With the fix, the shadow twin lives under the NAMESPACED shadow key, so it
    # coexists with the live key in the UNIQUE idempotency_key index. A live
    # admission for the SAME (miner, challenge, solution) uses the live key, finds
    # NO matching live row, and creates a fresh authoritative receipt — it never
    # replays the shadow row.
    app, store = _build(tmp_path, monkeypatch, pm_async=True)
    hk, cid, sha = "hk", "pm-t1-e1-x", "deadbeef"
    live_key = submit_admission.idempotency_key(hk, cid, sha)
    shadow_key = submit_admission.shadow_idempotency_key(hk, cid, sha)
    assert live_key != shadow_key  # the fix: keys are distinct

    # A drained shadow twin sitting in the ledger under the namespaced shadow key.
    def _ins_shadow(conn):
        conn.execute(
            "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, "
            "status, dimacs_solution_sha256, submitted_at, recorded_at_iso, "
            "signature, idempotency_key, received_at_iso, challenge_kind, "
            "solution_body, assignment_identity, attempt_count) "
            "VALUES ('shd_old', ?, ?, 0, 'ranked', ?, '2026-01-01T00:00:00.000Z', "
            "'2026-01-01T00:00:00.000Z', 'sig-shadow', ?, "
            "'2026-01-01T00:00:00.000Z', ?, NULL, ?, 0)",
            (cid, hk, sha, shadow_key, submit_admission.KIND_PER_MINER_SHADOW, hk))
    store.write(_ins_shadow)

    outcome, row = submit_admission.admit_pending(
        store, receipt_id="sub_live", idem_key=live_key, miner_hotkey=hk,
        challenge_id=cid, dimacs_solution_sha256=sha, dimacs_solution="body",
        submitted_at="2026-06-27T00:00:00.000Z",
        received_at_iso="2026-06-27T00:00:00.000Z", signature="sig-live",
        epoch=0, assignment_identity=hk)
    # NOT a replay of the shadow row: a fresh live receipt was created.
    assert outcome == "created"
    assert row["id"] == "sub_live"
    assert row["challenge_kind"] == submit_admission.KIND_PER_MINER
    # both rows coexist; the shadow twin was never touched
    rows = store.query(
        "SELECT id, challenge_kind FROM per_miner_attempts WHERE miner_hotkey=? "
        "ORDER BY id", (hk,))
    assert {r["id"] for r in rows} == {"shd_old", "sub_live"}


def test_same_payload_retry_after_shadow_cutover_creates_live_ranked_receipt(
        tmp_path, monkeypatch):
    # End-to-end cutover: a drained shadow twin for (miner, challenge, solution) is
    # present in the ledger (as it would be after running shadow mode), but the LIVE
    # lane has NOT yet paid this solution. After disabling shadow / enabling live,
    # the miner retries the SAME payload. It must get a NEW live authoritative
    # receipt (sub_ prefix) that RANKS, NOT a 200 replay of the shadow (shd_)
    # receipt, and there must be exactly one solve (no double pay).
    #
    # The shadow twin is seeded directly the way the (now-namespaced) shadow path
    # writes it, so this isolates the receipts-table idempotency cutover hole: the
    # live retry must never resolve to the shadow receipt.
    app_l, store = _build(tmp_path, monkeypatch, pm_async=True)
    kp = _keypair()
    cid, body = _pm_solution_for(kp)
    sol_sha = sha256_hex(body)
    shadow_key = submit_admission.shadow_idempotency_key(
        kp.ss58_address, cid, sol_sha)

    # Seed the drained shadow twin (namespaced shadow key, kind=shadow) — exactly
    # what survives in the ledger after shadow mode is run and turned off.
    def _seed_shadow(conn):
        conn.execute(
            "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, "
            "status, shadow_status, dimacs_solution_sha256, submitted_at, "
            "recorded_at_iso, verified_at_iso, signature, idempotency_key, "
            "received_at_iso, challenge_kind, solution_body, assignment_identity, "
            "attempt_count) "
            "VALUES ('shd_old', ?, ?, 0, 'ranked', 'ranked', ?, "
            "'2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', "
            "'2026-01-01T00:00:00.000Z', 'sig-shadow', ?, "
            "'2026-01-01T00:00:00.000Z', ?, NULL, ?, 0)",
            (cid, kp.ss58_address, sol_sha, shadow_key,
             submit_admission.KIND_PER_MINER_SHADOW, kp.ss58_address))
    store.write(_seed_shadow)
    assert _solves(store, cid, kp.ss58_address) == 0  # live lane has not paid

    # Live pm-async on, shadow off: retry the SAME payload.
    with TestClient(app_l) as client_l:
        r2 = _submit(client_l, kp, challenge_id=cid, solution=body)
        # MUST be a NEW live receipt (202), NOT a 200 replay of the shadow receipt.
        assert r2.status_code == 202, r2.text
        live_receipt = r2.json()
        assert live_receipt["receipt_id"].startswith("sub_")
        assert live_receipt["receipt_id"] != "shd_old"
        # drain the live receipt -> it ranks (the live lane is the first payer)
        assert app_l.state.async_verify_tick(worker_id="t", batch_size=8) >= 1
        g = client_l.get(live_receipt["receipt_url"]).json()
    assert g["status"] == "ranked", g
    assert g["receipt_id"].startswith("sub_")
    # exactly one solve from the LIVE lane (no double pay)
    assert _solves(store, cid, kp.ss58_address) == 1
    # the original shadow twin is untouched and was never replayed
    still_shadow = store.query(
        "SELECT id FROM per_miner_attempts WHERE id='shd_old'")
    assert len(still_shadow) == 1


# ---------------------------------------------------------------------------
# Startup WARNING: pm-async on but no drain worker configured.
# ---------------------------------------------------------------------------
def test_startup_warns_when_pm_async_on_but_verify_worker_disabled(
        tmp_path, monkeypatch, capsys):
    app, _ = _build(tmp_path, monkeypatch, pm_async=True, verify_on=False)
    with TestClient(app):
        pass
    out = capsys.readouterr().out
    assert "[verify] WARNING" in out
    assert "UNPAID" in out
