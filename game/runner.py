"""The miner's solver, executed BY THE PUBLISHER inside the sandbox.

This is the program that runs in the network-isolated, host-timed jail
(`scaffold/lanes/sandbox.py:run_solver`). It is the local stand-in for "the
solver running on a miner's SSH host": the publisher hands it one CNF, it must
return a DIMACS assignment, and the publisher trusts ONLY the host-measured wall
time and the independently-verified witness — never anything this script prints
about itself.

Behavior is selected by --behavior so one program models every archetype:

  honest / slow : actually solve the CNF (real work), then emit it.
  cheat         : emit a COMPLETE but non-satisfying assignment.
  copy          : emit a foreign assignment handed in via --answer-file
                  (a stolen answer for someone else's challenge).

Usage:
  python runner.py --cnf P --behavior B --delay-ms N [--answer-file P] [--repo R]
"""
from __future__ import annotations

import argparse
import sys
import time


def _emit(assignment: list[int]) -> None:
    sys.stdout.write("s SATISFIABLE\n")
    sys.stdout.write("v " + " ".join(str(x) for x in assignment) + " 0\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cnf", required=True)
    ap.add_argument("--behavior", default="honest")
    ap.add_argument("--delay-ms", type=float, default=0.0)
    ap.add_argument("--answer-file", default="")
    ap.add_argument("--repo", default="")
    args = ap.parse_args()

    if args.repo:
        sys.path.insert(0, args.repo)
    from scaffold.dimacs import solve_cnf, parse_cnf, verify_witness

    cnf = open(args.cnf, encoding="utf-8").read()
    n_vars, _ = parse_cnf(cnf)

    # Simulated solver effort: a slower solver genuinely takes longer, and the
    # HOST measures it. This is the only thing that separates fast from slow.
    if args.delay_ms > 0:
        time.sleep(args.delay_ms / 1000.0)

    if args.behavior == "copy":
        # Re-submit a stolen answer verbatim. It satisfies the victim's CNF, not
        # this miner's — the publisher's witness check will reject it.
        toks = open(args.answer_file, encoding="utf-8").read().split()
        _emit([int(t) for t in toks])
        return 0

    sol = solve_cnf(cnf) or []

    if args.behavior == "cheat":
        # A complete assignment that does NOT satisfy the formula. Flip until the
        # witness check fails, so "fast but wrong" is guaranteed wrong.
        bad = [abs(v) for v in range(1, n_vars + 1)]   # all-positive
        if verify_witness(cnf, bad):
            bad[0] = -bad[0]
        _emit(bad)
        return 0

    _emit(sol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
