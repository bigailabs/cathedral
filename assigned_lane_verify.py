"""Focused gate for assigned SAT and audit-shadow scoring.

Run:
    python assigned_lane_verify.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from scaffold.dimacs import gen_planted_3sat
from scaffold.publisher import seed_audit_challenge, seed_challenge
from scaffold.publisher.store import Store
from scaffold.publisher import weights


checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def set_env(**items: str) -> dict[str, str | None]:
    old = {k: os.environ.get(k) for k in items}
    for k, v in items.items():
        os.environ[k] = v
    return old


def restore_env(old: dict[str, str | None]) -> None:
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main() -> int:
    print("ASSIGNED LANE - evidence ledger contract")
    store = Store(":memory:")
    blob = "s SATISFIABLE\nv 1 -2 3 0\n"
    sol_sha = __import__("hashlib").sha256(blob.encode()).hexdigest()

    def _evidence(conn):
        conn.execute(
            "INSERT INTO per_miner_assignments(challenge_id, miner_hotkey, epoch, tier, seq, "
            "difficulty_weight, assigned_at_iso) VALUES ('pm-test', 'hkA', 1, 1, 0, 1.0, ?)",
            (now_iso(),),
        )
        conn.execute(
            "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, status, "
            "rejection_reason, dimacs_solution_sha256, submitted_at, recorded_at_iso, signature) "
            "VALUES ('attempt-1', 'pm-test', 'hkA', 1, 'ranked', NULL, ?, ?, ?, 'sig')",
            (sol_sha, now_iso(), now_iso()),
        )
        conn.execute(
            "INSERT INTO per_miner_witnesses(challenge_id, miner_hotkey, epoch, tier, seq, "
            "dimacs_solution_sha256, answer_hash, dimacs_solution, recorded_at_iso) "
            "VALUES ('pm-test', 'hkA', 1, 1, 0, ?, 'answerhash', ?, ?)",
            (sol_sha, blob, now_iso()),
        )
    store.write(_evidence)
    assignments = store.query("SELECT * FROM per_miner_assignments")
    attempts = store.query("SELECT * FROM per_miner_attempts")
    witnesses = store.query("SELECT * FROM per_miner_witnesses")
    ck("per-miner assignment row persists tier/seq", len(assignments) == 1 and assignments[0]["seq"] == 0)
    ck("per-miner attempt row persists status/hash", len(attempts) == 1 and attempts[0]["status"] == "ranked")
    ck("per-miner witness body persists for replay", len(witnesses) == 1 and witnesses[0]["dimacs_solution"] == blob)
    store.close()

    print("ASSIGNED LANE - coldkey-safe scoring")
    old = set_env(
        CATHEDRAL_PERMINER_ENABLED="1",
        CATHEDRAL_PERMINER_SHADOW="0",
        CATHEDRAL_PERMINER_REQUIRE_COLDKEY="1",
        CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE="1",
        CATHEDRAL_PERMINER_SCORE_TARGET="10",
    )
    try:
        store = Store(":memory:")
        epoch = __import__("scaffold.publisher.per_miner", fromlist=["current_epoch"]).current_epoch()

        def _seed(conn):
            conn.execute("CREATE TABLE IF NOT EXISTS coldkey_map (hotkey TEXT PRIMARY KEY, coldkey TEXT NOT NULL)")
            for hk, ckid in (("hkA1", "ckA"), ("hkA2", "ckA"), ("hkB", "ckB")):
                conn.execute("INSERT INTO coldkey_map(hotkey, coldkey) VALUES (?, ?)", (hk, ckid))
            for hk, cid, score in (
                ("hkA1", "pm-a1", 10.0),
                ("hkA2", "pm-a2", 10.0),
                ("hkB", "pm-b", 5.0),
            ):
                conn.execute(
                    "INSERT INTO per_miner_solves(challenge_id, miner_hotkey, epoch, tier, seq, "
                    "difficulty_weight, verified, solved_at_iso) VALUES (?, ?, ?, 1, 0, ?, 1, ?)",
                    (cid, hk, epoch, score, now_iso()),
                )
        store.write(_seed)
        coldkey_of = weights._load_coldkey_map(store)
        scored = weights.compose_scores(store, coldkey_of=coldkey_of)
        ck("same-coldkey hotkeys share one best assigned score",
           scored == {"hkA1": 0.5, "hkA2": 0.5, "hkB": 0.5})
        no_map = weights.compose_scores(store, coldkey_of=None)
        ck("assigned live scoring fails closed without coldkey map", no_map == {})
        store.close()
    finally:
        restore_env(old)

    print("AUDIT SHADOW - zero multiplier does not pay")
    old = set_env(CATHEDRAL_WEIGHTS_MODE="proportional")
    try:
        store = Store(":memory:")
        paid_cnf, _ = gen_planted_3sat(1, 10, 30)
        audit_cnf, _ = gen_planted_3sat(2, 10, 30)
        seed_challenge(store, challenge_id="sat-paid", tier=1, cnf_text=paid_cnf)
        seed_audit_challenge(
            store,
            challenge_id="audit-shadow",
            tier=1,
            cnf_text=audit_cnf,
            manifest={"cnf_id": "shadow"},
            score_multiplier=0.0,
        )

        def _solve(conn):
            conn.execute(
                "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
                "VALUES ('sat-paid', 'hkPaid', ?)",
                (now_iso(),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
                "VALUES ('audit-shadow', 'hkAuditOnly', ?)",
                (now_iso(),),
            )
        store.write(_solve)
        scored = weights.compose_scores(store)
        ck("audit-shadow solve is excluded from proportional weights", scored == {"hkPaid": 1.0})
        meta = store.query("SELECT * FROM audit_challenge_manifests WHERE challenge_id='audit-shadow'")
        ck("audit manifest metadata persisted", len(meta) == 1)
        store.close()
    finally:
        restore_env(old)

    passed = sum(1 for _, ok in checks if ok)
    print()
    if passed == len(checks):
        print(f"ASSIGNED LANE VERIFY: PASS all {passed} checks")
        return 0
    print(f"ASSIGNED LANE VERIFY: FAIL {len(checks) - passed}/{len(checks)} checks")
    for name, ok in checks:
        if not ok:
            print(f"   FAILED: {name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
