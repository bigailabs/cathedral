#!/usr/bin/env python3
"""Phase A demo — "real combinatorial instance, solved and independently
verified" artifact (deploy/V2_FRONTIER_CUBE_AND_CONQUER_PLAN_2026-07-01.md).

Mints ONE real, unplanted combinatorial-SAT instance through the actual
production wiring point (scaffold.publisher.per_miner.generate_instance with
CATHEDRAL_V2_CHALLENGE_SOURCE=combinatorial), solves it locally with the
scaffold's tiny DPLL (scaffold.dimacs.solve_cnf — fine for these small,
demo-sized instances; a real miner would bring cadical/kissat), independently
re-checks the found assignment with verify_witness, and prints a compact
receipt anyone can re-verify with scripts/verify_receipt.py — zero trust in
this script or any server.

Exit code: 0 if solve+verify succeeded, 1 otherwise (nonzero on failure, so
this is CI/cron-safe as a smoke check).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Set the opt-in flag BEFORE importing per_miner-adjacent modules that read it
# lazily is not required (real_corpus.challenge_source() reads the env at call
# time, not import time) but we still set it up front for clarity.
os.environ.setdefault("CATHEDRAL_V2_CHALLENGE_SOURCE", "combinatorial")

from scaffold.dimacs import solve_cnf, verify_witness  # noqa: E402
from scaffold.publisher import per_miner  # noqa: E402
from scaffold.publisher import real_corpus  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hotkey", default="frontier-phase-a-demo",
                    help="demo hotkey used only to derive the wire challenge_id")
    ap.add_argument("--epoch", type=int, default=1, help="deterministic epoch key")
    ap.add_argument("--tier", type=int, default=1, choices=sorted(per_miner.TIERS),
                    help="tier (controls instance size)")
    ap.add_argument("--seq", type=int, default=0, help="deterministic sequence number")
    ap.add_argument("--source", default="combinatorial", choices=("combinatorial", "corpus"),
                    help="CATHEDRAL_V2_CHALLENGE_SOURCE value to demo")
    args = ap.parse_args(argv)

    os.environ["CATHEDRAL_V2_CHALLENGE_SOURCE"] = args.source

    kind = real_corpus.kind_for(args.epoch, args.tier, args.seq)
    t0 = time.monotonic()
    challenge_id, cnf_text, planted = per_miner.generate_instance(
        args.hotkey, args.epoch, args.tier, args.seq)
    gen_s = time.monotonic() - t0

    if planted is not None:
        # Should be impossible with source != "planted", but don't silently
        # trust it if it ever happens — that would mean this is NOT a real
        # unplanted instance.
        print("FAIL: generate_instance returned a planted assignment under a "
              "non-planted source", file=sys.stderr)
        return 1

    cnf_sha256 = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()

    t1 = time.monotonic()
    assignment = solve_cnf(cnf_text)
    solve_s = time.monotonic() - t1

    if assignment is None:
        print("FAIL: solve_cnf found no satisfying assignment (instance should "
              "be satisfiable by construction)", file=sys.stderr)
        return 1

    ok = verify_witness(cnf_text, assignment)

    receipt = {
        "challenge_id": challenge_id,
        "source": args.source,
        "kind": kind,
        "epoch": args.epoch,
        "tier": args.tier,
        "seq": args.seq,
        "cnf_sha256": cnf_sha256,
        "n_vars": len(assignment),
        "assignment": assignment,
        "verify_witness": ok,
        "gen_seconds": round(gen_s, 4),
        "solve_seconds": round(solve_s, 4),
    }
    print(json.dumps(receipt, indent=2))

    if not ok:
        print("FAIL: verify_witness rejected the solver's own assignment",
              file=sys.stderr)
        return 1

    print(f"PASS: real ({kind}) instance {challenge_id} solved and independently "
          f"verified in {solve_s:.3f}s (re-check with scripts/verify_receipt.py "
          f"--cnf <file> --assignment '{json.dumps(assignment)}')", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
