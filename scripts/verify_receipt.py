#!/usr/bin/env python3
"""Zero-trust receipt verifier — "verify it yourself" artifact for Phase A of
the frontier cube-and-conquer plan
(deploy/V2_FRONTIER_CUBE_AND_CONQUER_PLAN_2026-07-01.md).

Given a CNF (file or stdin) and an assignment (signed literals), this script
independently re-checks the witness with the SAME generic function the
publisher's verify path uses (scaffold.dimacs.verify_witness) and prints
PASS/FAIL. It does not trust any server, does not call the network, and does
not need to know whether the CNF came from the planted path, the
combinatorial generator, or a real corpus file — verify_witness only ever
looks at the CNF text and the assignment.

Usage:
  # CNF from a file, assignment as a JSON array of signed ints
  scripts/verify_receipt.py --cnf instance.cnf --assignment '[1,-2,3,...]'

  # CNF from stdin, assignment from a file (one JSON array, or a receipt with
  # an "assignment" key produced by scripts/frontier_phase_a_demo.py)
  cat instance.cnf | scripts/verify_receipt.py --assignment-file receipt.json

Exit code: 0 on PASS, 1 on FAIL, 2 on a usage/input error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scaffold.dimacs import verify_witness  # noqa: E402


def _read_cnf(args: argparse.Namespace) -> str:
    if args.cnf:
        return Path(args.cnf).read_text(encoding="utf-8")
    data = sys.stdin.read()
    if not data.strip():
        raise SystemExit("no CNF supplied: pass --cnf FILE or pipe DIMACS text to stdin")
    return data


def _read_assignment(args: argparse.Namespace) -> list[int]:
    raw: str
    if args.assignment is not None:
        raw = args.assignment
    elif args.assignment_file:
        raw = Path(args.assignment_file).read_text(encoding="utf-8")
    else:
        raise SystemExit("no assignment supplied: pass --assignment JSON or --assignment-file FILE")
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        # Accept a receipt dict (e.g. from frontier_phase_a_demo.py) with an
        # "assignment" key, so this script composes directly with the demo.
        parsed = parsed.get("assignment")
    if not isinstance(parsed, list) or not all(isinstance(x, int) for x in parsed):
        raise SystemExit("assignment must be a JSON array of signed integers")
    return parsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cnf", help="path to a DIMACS CNF file (default: stdin)")
    ap.add_argument("--assignment", help="JSON array of signed literals, e.g. '[1,-2,3]'")
    ap.add_argument("--assignment-file", help="path to a JSON file: an array, or a receipt "
                    "dict with an 'assignment' key")
    args = ap.parse_args(argv)

    try:
        cnf_text = _read_cnf(args)
        assignment = _read_assignment(args)
    except SystemExit as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    ok = verify_witness(cnf_text, assignment)
    cnf_sha256 = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    print(f"cnf_sha256: {cnf_sha256}")
    print(f"n_literals: {len(assignment)}")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
