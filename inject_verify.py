#!/usr/bin/env python3
"""inject_verify.py — gate for the additive inject lane (scaffold/publisher/
inject.py). Proves the load-bearing invariants WITHOUT touching live state:

  1. ISOLATION: injected challenges (distinct family_id) are NOT counted by the
     native refill loop, and native challenges are NOT counted by the inject lane.
  2. NON-INTERFERENCE: native retirement never touches injected challenges and
     vice-versa.
  3. IDENTIFIABILITY: injected challenge_ids carry the family label AND still
     parse to the correct tier (so scoring weights them correctly).
  4. SERVE PARITY: injected challenges are cnf_source='local' and active, so the
     board serve query returns them exactly like native ones.

Run:  python inject_verify.py      → expect "INJECT VERIFY PASS"
"""
from __future__ import annotations

import os
import sys
import tempfile

from scaffold.dimacs import gen_planted_3sat, verify_witness
from scaffold.publisher import inject, refill
from scaffold.publisher.app import seed_challenge
from scaffold.publisher.store import Store
from scaffold.publisher.weights import tier_from_challenge_id

FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    import hashlib

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    store = Store(tmp.name)

    family = "gentest"
    tier = 2  # not 1: tier_from_challenge_id defaults to 1 on parse failure, so a
              # non-1 tier makes the identifiability check actually meaningful.
    n_vars, n_clauses = 60, 255
    nat_cnf, _ = gen_planted_3sat(1, n_vars, n_clauses, method="biased")
    inj_seed = 0x1234_5678_9abc_def0 & ((1 << 63) - 1)
    inj_cnf, inj_planted = gen_planted_3sat(inj_seed, n_vars, n_clauses, method="biased")

    nat_cid = refill.mint_challenge_id("testblk", tier, 0)
    inj_cid = inject.inject_cid(tier, family, inj_seed)

    seed_challenge(store, challenge_id=nat_cid, tier=tier, cnf_text=nat_cnf, status="active")
    seed_challenge(store, challenge_id=inj_cid, tier=tier, cnf_text=inj_cnf,
                   status="active", family_id=family)

    print("0. SEED SECRECY (the public id must NOT reveal the planted answer)")
    # The injected challenge's public id and the public board fields (tier, family,
    # num_vars, num_clauses, cnf_sha256). An attacker reads the id suffix and tries
    # to use it as the seed — the way the OLD {seed:016x} id leaked.
    suffix = inj_cid.rsplit("-", 1)[-1]
    check("seed hex does not appear anywhere in the public id",
          f"{inj_seed:016x}" not in inj_cid)
    guessed_seed = int(suffix, 16)
    check("id suffix does not decode to the real seed",
          guessed_seed != inj_seed)
    guessed_cnf, guessed_planted = gen_planted_3sat(guessed_seed, n_vars, n_clauses, method="biased")
    check("CNF regenerated from the public id does NOT match the served CNF",
          hashlib.sha256(guessed_cnf.encode()).hexdigest()
          != hashlib.sha256(inj_cnf.encode()).hexdigest())
    check("planted answer derived from the public id does NOT solve the served CNF",
          not verify_witness(inj_cnf, guessed_planted))
    # sanity: the REAL planted answer does solve it (so the challenge is genuine)
    check("the real planted answer (secret) does solve the served CNF",
          verify_witness(inj_cnf, inj_planted))

    print("1. ISOLATION")
    check("native count sees only the native challenge",
          refill.active_local_count(store, tier) == 1)
    check("inject count sees only the injected challenge",
          inject.active_inject_count(store, tier, family) == 1)

    print("2. IDENTIFIABILITY")
    check("injected cid carries the family label", f"-{family}-" in inj_cid)
    check("injected cid parses to the correct tier",
          tier_from_challenge_id(inj_cid) == tier)
    check("native cid does NOT carry the family label", f"-{family}-" not in nat_cid)

    print("3. SERVE PARITY")
    served = store.query(
        "SELECT challenge_id, family_id, cnf_source FROM lane_challenges "
        "WHERE status='active' AND cnf_source='local'")
    served_ids = {r["challenge_id"] for r in served}
    check("both challenges are in the active local serve set",
          nat_cid in served_ids and inj_cid in served_ids)
    check("injected challenge is cnf_source='local'",
          all(r["cnf_source"] == "local" for r in served if r["challenge_id"] == inj_cid))

    print("4. NON-INTERFERENCE (retirement)")
    # record one distinct solve on the injected challenge, set the solver-cap to 1
    refill.record_solve(store, inj_cid, "5HotkeyAAA")
    os.environ["CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS"] = "1"
    inj_retired = inject.retire_inject_ready(store, tier, family)
    check("inject retirement retired the saturated injected challenge",
          inj_retired == 1)
    check("injected challenge is now retired",
          store.query("SELECT status FROM lane_challenges WHERE challenge_id=?",
                      (inj_cid,))[0]["status"] == "retired")
    check("native challenge is untouched by inject retirement",
          store.query("SELECT status FROM lane_challenges WHERE challenge_id=?",
                      (nat_cid,))[0]["status"] == "active")
    # native retirement must likewise never touch the injected family
    nat_retired = refill.retire_ready(store, tier)
    check("native retirement does not retire any injected challenge (none left active)",
          nat_retired == 0)

    print("5. FAMILY GUARD (fail closed on a dangerous family)")
    ok_native, _ = inject.family_is_safe("synthetic_boolean_v1")
    check("refuses the native family", ok_native is False)
    ok_blank, _ = inject.family_is_safe("")
    check("refuses an empty/invalid family", ok_blank is False)
    ok_good, _ = inject.family_is_safe("gentest")
    check("accepts a normal injected family", ok_good is True)

    print()
    if FAILS:
        print(f"INJECT VERIFY FAIL — {len(FAILS)} check(s): {', '.join(FAILS)}")
        return 1
    print("INJECT VERIFY PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
