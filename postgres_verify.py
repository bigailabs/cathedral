"""postgres_verify.py — integration gate for the Postgres backend of the thin
publisher Store (KEYSTONE TASK 2).

Connects to a REAL Postgres (DATABASE_URL) and proves the dual-backend Store
behaves identically to SQLite on the paths the publisher relies on:

  * migrations apply (idempotent — re-run is a no-op),
  * insert_row OR-IGNORE semantics (rowcount 1 then 0 on the same id),
  * seed_state durable upsert (set / overwrite / read-back),
  * recent_rows tuple-cursor pull (strict (ran_at, id) ordering),
  * scoring.claim_solve distinct-solver claim (rank, then None on re-claim),
  * INSERT OR REPLACE upsert translation (lane_challenges via seed_challenge-shape),
  * the seed_live board mirror's ON CONFLICT(challenge_id) DO UPDATE path,
  * MVCC: two concurrent connections from the pool don't deadlock on a write.

It runs in an ISOLATED Postgres schema (default `pgverify`, override with
PGVERIFY_SCHEMA) which it DROPs and recreates, so it never touches `public` /
the app's real tables and is safe to re-run against the live keystone DB.

Run:
    DATABASE_URL=postgresql://… python postgres_verify.py
Needs only psycopg2 (no fastapi/bittensor) — the Store's PG path is pure stdlib.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tempfile

checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def _write_fixture_verifier() -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    f.write(
        "import json, sys\n"
        "evidence_path, request_path, capacity_path, result_path = sys.argv[1:5]\n"
        "evidence = json.load(open(evidence_path, encoding='utf-8'))\n"
        "request = json.load(open(request_path, encoding='utf-8'))\n"
        "capacity = json.load(open(capacity_path, encoding='utf-8'))\n"
        "request_ok = evidence.get('evidence_request_id') == request.get('request_id')\n"
        "capacity_ok = capacity.get('gpu_short_ref') == 'h200' and capacity.get('gpu_count') == 8\n"
        "ok = request_ok and capacity_ok\n"
        "result = {'ok': ok, 'verified': ok, 'verifier': 'fixture-tdx-gpu', "
        "'proof': 'fixture_dcap_nvidia', 'tdx_verified': ok, 'gpu_verified': ok, "
        "'gpu_claims_match': ok, 'report_data_match': ok, 'debug_disabled': ok}\n"
        "with open(result_path, 'w', encoding='utf-8') as out:\n"
        "    json.dump(result, out)\n"
        "sys.exit(0 if ok else 1)\n"
    )
    f.close()
    return f.name


def main() -> int:
    base_url = os.environ.get("DATABASE_URL")
    if not base_url or base_url.split("://", 1)[0] not in ("postgres", "postgresql"):
        print("postgres_verify: DATABASE_URL (postgres[ql]://…) required", file=sys.stderr)
        return 2

    schema = os.environ.get("PGVERIFY_SCHEMA", "pgverify")

    import psycopg2
    # 1) Drop + recreate the isolated verify schema on a raw connection.
    raw = psycopg2.connect(base_url, connect_timeout=15)
    raw.autocommit = True
    with raw.cursor() as c:
        c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        c.execute(f'CREATE SCHEMA {schema}')
    raw.close()
    print(f"PG VERIFY — isolated schema '{schema}' on real Postgres")

    # 2) Point the Store at that schema via libpq options=-c search_path.
    sep = "&" if "?" in base_url else "?"
    dsn = f"{base_url}{sep}options=" + "-c%20search_path%3D" + schema

    from scaffold.publisher.store import Store, new_uuid, _MIGRATIONS_PG
    from scaffold.publisher import scoring, tee_gpu

    store = Store(dsn)
    ck("backend selected = postgres", store.backend == "postgres")
    with store.advisory_lock("postgres_verify_lock") as first_lock:
        with store.advisory_lock("postgres_verify_lock") as competing_lock:
            ck("postgres advisory lock excludes competing holder",
               first_lock is True and competing_lock is False)
    with store.advisory_lock("postgres_verify_lock") as reacquired_lock:
        ck("postgres advisory lock releases after context", reacquired_lock is True)

    # ---- migrations idempotent --------------------------------------------
    store.migrate()  # second run must be a no-op (already applied)
    applied = store.query("SELECT COUNT(*) AS n FROM schema_migrations")[0]["n"]
    ck(
        f"all {len(_MIGRATIONS_PG)} postgres migrations applied (idempotent re-run)",
        int(applied) == len(_MIGRATIONS_PG),
    )
    # every expected table exists in the verify schema
    tbls = {r[0] for r in store.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
        (schema,))}
    want = {"eval_runs", "lane_challenges", "agent_submissions", "arena_solvers",
            "arena_instances", "submit_signatures", "seed_state",
            "lane_challenge_solves", "weight_policy_state", "per_miner_solves",
            "per_miner_attempts", "per_miner_assignments", "per_miner_witnesses",
            "audit_challenge_manifests", "coldkey_map", "tee_gpu_capacity",
            "tee_gpu_capacity_events", "attest_nonces", "attestations",
            "schema_migrations"}
    ck(f"all core tables present ({len(want & tbls)}/{len(want)})", want.issubset(tbls))

    # ---- insert_row OR IGNORE ---------------------------------------------
    rid = new_uuid()
    row = {"id": rid, "ran_at": "2026-06-11T00:00:00.000Z",
           "eval_output_schema_version": 6, "miner_hotkey": "5Hk_test",
           "task_type": "synthetic_boolean_v1"}
    n1 = store.insert_row(row)
    n2 = store.insert_row(row)  # same id -> OR IGNORE -> 0
    ck("insert_row new -> 1", n1 == 1)
    ck("insert_row dup (OR IGNORE) -> 0", n2 == 0)
    ck("count_rows reflects single insert", store.count_rows() == 1)

    # ---- recent_rows tuple cursor -----------------------------------------
    # add two more rows with the SAME ran_at, distinct ids -> exercises the
    # (ran_at, id) secondary ordering.
    ids = sorted([new_uuid(), new_uuid()])
    for i in ids:
        store.insert_row({**row, "id": i, "ran_at": "2026-06-11T00:00:01.000Z"})
    allrows = store.recent_rows("1970-01-01T00:00:00+00:00", "", 50)
    ck("recent_rows returns all 3 rows ordered", len(allrows) == 3)
    ck("recent_rows ascending (ran_at, id)",
       allrows[0]["ran_at"] <= allrows[1]["ran_at"] <= allrows[2]["ran_at"])
    # cursor-pull strictly after the first row's tuple
    after = store.recent_rows(allrows[0]["ran_at"], allrows[0]["id"], 50)
    ck("recent_rows tuple cursor excludes the cursor row", len(after) == 2)

    # ---- seed_state durable upsert ----------------------------------------
    store.set_seed_state("wm", "v1")
    store.set_seed_state("wm", "v2")  # ON CONFLICT(key) DO UPDATE
    ck("seed_state upsert read-back = latest", store.get_seed_state("wm") == "v2")
    ck("seed_state missing key -> None", store.get_seed_state("nope") is None)

    # ---- TEE GPU capacity PG upsert/update path ---------------------------
    evidence_request = tee_gpu.create_evidence_request(
        store,
        owner_hotkey="5PgHotkey",
        node_id="pg-h200-0",
        actor="pgverify",
        ttl_secs=60,
    )
    tee_body = {
        "node_id": "pg-h200-0",
        "gpu_short_ref": "h200",
        "gpu_count": 8,
        "hourly_cost": 2.75,
        "agent_api": "http://203.0.113.30:32000",
        "tee_kind": "intel_tdx",
        "tdx_claimed": True,
        "gpu_cc_claimed": True,
        "operator_use_authorized": True,
        "status": "active",
        "chutes_server_name": "worker-pg-h200-0",
        "attestation": {
            "evidence_request_id": evidence_request["request_id"],
            "tdx_quote_b64": "fixture-quote",
            "gpu_evidence_json": {"cc_mode": "on"},
        },
    }
    tee_rec = tee_gpu.create_capacity(
        store, tee_body, owner_hotkey="5PgHotkey", actor="pgverify",
        event_type="pg_created", allow_requested_status=True)
    ck("tee_gpu_capacity create on PG eligible", tee_rec["preflight_status"] == "eligible")
    ck("tee_gpu_capacity admin create can be active on PG", tee_rec["status"] == "active")
    tee_upd = tee_gpu.update_capacity_admin(
        store, tee_rec["capacity_id"], {"status": "paused", "admin_note": "pg-ok"})
    ck("tee_gpu_capacity admin update on PG", tee_upd is not None and tee_upd["status"] == "paused")
    tee_again = tee_gpu.create_capacity(
        store, {**tee_body, "status": "active"}, owner_hotkey="5PgHotkey",
        actor="pgverify", event_type="pg_resubmitted", preserve_admin_fields=True)
    ck("tee_gpu_capacity resubmit preserves admin state on PG",
       tee_again["status"] == "paused" and tee_again["admin_note"] == "pg-ok")
    try:
        tee_gpu.list_capacity_on_chutes(store, tee_rec["capacity_id"])
        blocked_before_crypto = False
    except tee_gpu.HTTPException as e:
        blocked_before_crypto = (
            e.status_code == 400
            and isinstance(e.detail, dict)
            and "cryptographic_attestation_required" in e.detail.get("blockers", [])
        )
    ck("tee_gpu_capacity Chutes dry-run on PG requires crypto first", blocked_before_crypto)

    old_verifier = os.environ.get("CATHEDRAL_TEE_GPU_VERIFY_CMD")
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = f"{sys.executable} {_write_fixture_verifier()}"
    try:
        tee_verified = tee_gpu.verify_capacity_evidence(store, tee_rec["capacity_id"])
    finally:
        if old_verifier is None:
            os.environ.pop("CATHEDRAL_TEE_GPU_VERIFY_CMD", None)
        else:
            os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = old_verifier
    ck(
        "tee_gpu_capacity crypto verifier updates PG evidence",
        tee_verified is not None
        and tee_gpu.admin_record(tee_verified)["evidence"]["status"] == "cryptographically_verified",
    )
    tee_active = tee_gpu.update_capacity_admin(
        store,
        tee_rec["capacity_id"],
        {"status": "active"},
    )
    ck("tee_gpu_capacity can reactivate after crypto on PG",
       tee_active is not None and tee_active["status"] == "active")

    old_hotkey_path = os.environ.get("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH")
    os.environ["CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH"] = "/tmp/chutes.hotkey"
    try:
        tee_dry = tee_gpu.list_capacity_on_chutes(store, tee_rec["capacity_id"])
    finally:
        if old_hotkey_path is None:
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH", None)
        else:
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH"] = old_hotkey_path
    ck("tee_gpu_capacity Chutes dry-run on PG",
       tee_dry["status"] == "dry_run" and tee_dry["executed"] is False)
    tee_metrics = tee_gpu.capacity_metrics(store)
    ck("tee_gpu_capacity production-ready GPU count on PG", tee_metrics["active_gpus"] == 0)
    ck(
        "tee_gpu_capacity production-ready hourly cost on PG",
        tee_metrics["active_listed_hourly_cost"] == 0.0,
    )
    ck(
        "tee_gpu_capacity active candidate GPU count on PG",
        tee_metrics["admin_active_candidate_gpus"] == 8,
    )
    ck(
        "tee_gpu_capacity active candidate hourly cost on PG",
        tee_metrics["admin_active_candidate_hourly_cost"] == 22.0,
    )
    tee_rows = tee_gpu.list_capacity(store, owner_hotkey="5PgHotkey")
    tee_events = store.query(
        "SELECT COUNT(*) AS n FROM tee_gpu_capacity_events WHERE capacity_id=?",
        (tee_rec["capacity_id"],))[0]["n"]
    ck("tee_gpu_capacity list on PG", len(tee_rows) == 1)
    ck("tee_gpu_capacity audit events on PG", int(tee_events) >= 4)

    # ---- scoring.claim_solve distinct claim -------------------------------
    cid = "sat-pg-1"
    def _c1(conn):
        return scoring.claim_solve(conn, cid, "hkA", "2026-06-11T00:00:02.000Z")
    def _c1b(conn):
        return scoring.claim_solve(conn, cid, "hkA", "2026-06-11T00:00:03.000Z")
    def _c2(conn):
        return scoring.claim_solve(conn, cid, "hkB", "2026-06-11T00:00:04.000Z")
    r1 = store.write(_c1)
    rdup = store.write(_c1b)   # same (challenge, hotkey) -> None
    r2 = store.write(_c2)
    ck("claim_solve first solver rank = 1", r1 == 1)
    ck("claim_solve same hotkey re-claim -> None", rdup is None)
    ck("claim_solve second distinct solver rank = 2", r2 == 2)

    # ---- INSERT OR REPLACE translation (lane_challenges upsert) -----------
    def _replace(conn, status):
        conn.execute(
            "INSERT OR REPLACE INTO lane_challenges(challenge_id, family_id, tier, "
            "cnf_text, cnf_sha256, cnf_bytes, num_vars, num_clauses, status, "
            "score_multiplier, difficulty_label, designated_solver_digest, created_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sat-rep-1", "synthetic_boolean_v1", 1, "p cnf 1 1\n1 0\n",
             "deadbeef", 10, 1, 1, status, 1.0, None, None,
             "2026-06-11T00:00:05.000Z"))
    store.write(lambda c: _replace(c, "active"))
    store.write(lambda c: _replace(c, "locked"))  # OR REPLACE -> upsert on PK
    got = store.query("SELECT status FROM lane_challenges WHERE challenge_id=?", ("sat-rep-1",))
    ck("INSERT OR REPLACE upserts (status overwritten)", len(got) == 1 and got[0]["status"] == "locked")

    # ---- seed_live board mirror ON CONFLICT DO UPDATE ---------------------
    def _board(conn, status):
        conn.execute(
            "INSERT INTO lane_challenges(challenge_id, family_id, tier, cnf_text, "
            "cnf_sha256, cnf_bytes, num_vars, num_clauses, status, score_multiplier, "
            "difficulty_label, designated_solver_digest, created_at_iso, cnf_source, "
            "cnf_url, updated_at_iso) "
            "VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, NULL, ?, 'external', ?, ?) "
            "ON CONFLICT(challenge_id) DO UPDATE SET "
            "  status=excluded.status, cnf_sha256=excluded.cnf_sha256, "
            "  cnf_bytes=excluded.cnf_bytes, num_vars=excluded.num_vars, "
            "  num_clauses=excluded.num_clauses, score_multiplier=excluded.score_multiplier, "
            "  difficulty_label=excluded.difficulty_label, cnf_url=excluded.cnf_url, "
            "  updated_at_iso=excluded.updated_at_iso "
            "WHERE lane_challenges.cnf_source='external'",
            ("sat-ext-1", "synthetic_boolean_v1", 2, "abcd", 4, 2, 3, status, 1.0,
             "easy", "2026-06-11T00:00:06.000Z", "/cnf", "2026-06-11T00:00:06.000Z"))
    store.write(lambda c: _board(c, "active"))
    store.write(lambda c: _board(c, "retired"))
    ext = store.query("SELECT status, cnf_source FROM lane_challenges WHERE challenge_id=?", ("sat-ext-1",))
    ck("board mirror ON CONFLICT DO UPDATE (external row updated)",
       len(ext) == 1 and ext[0]["status"] == "retired" and ext[0]["cnf_source"] == "external")

    # ---- MVCC: concurrent pooled writes don't wedge -----------------------
    errors: list[str] = []
    def worker(tag: str):
        try:
            for k in range(20):
                store.write(lambda conn, kk=k, t=tag: conn.execute(
                    "INSERT OR IGNORE INTO submit_signatures(signature, seen_at) VALUES (?, ?)",
                    (f"{t}-{kk}", "2026-06-11T00:00:07.000Z")))
        except Exception as e:  # pragma: no cover
            errors.append(f"{tag}: {e!r}")
    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - t0
    sigcount = store.query("SELECT COUNT(*) AS n FROM submit_signatures")[0]["n"]
    ck("MVCC concurrent writes: no errors", not errors)
    ck("MVCC concurrent writes: all 80 rows committed", int(sigcount) == 80)
    ck(f"MVCC concurrent writes finished promptly ({elapsed:.1f}s < 30s)", elapsed < 30)

    store.close()

    # ---- cleanup the verify schema ----------------------------------------
    raw = psycopg2.connect(base_url, connect_timeout=15)
    raw.autocommit = True
    with raw.cursor() as c:
        c.execute(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
    raw.close()

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print()
    if passed == total:
        print(f"POSTGRES VERIFY: PASS all {total} checks")
        return 0
    print(f"POSTGRES VERIFY: FAIL {total - passed}/{total} checks")
    for name, ok in checks:
        if not ok:
            print(f"   FAILED: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
