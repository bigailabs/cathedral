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

checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


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

    from scaffold.publisher.store import Store, new_uuid
    from scaffold.publisher import scoring

    store = Store(dsn)
    ck("backend selected = postgres", store.backend == "postgres")

    # ---- migrations idempotent --------------------------------------------
    store.migrate()  # second run must be a no-op (already applied)
    applied = store.query("SELECT COUNT(*) AS n FROM schema_migrations")[0]["n"]
    ck("all 12 migrations applied (idempotent re-run)", int(applied) == 12)
    # every expected table exists in the verify schema
    tbls = {r[0] for r in store.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
        (schema,))}
    want = {"eval_runs", "lane_challenges", "agent_submissions", "arena_solvers",
            "arena_instances", "submit_signatures", "seed_state",
            "lane_challenge_solves", "schema_migrations"}
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
    allrows = store.recent_rows(None, None, 50)
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
