"""Lane C — REAL encoding (replaces the toy _roundtrip model).

Encodes the genuine Substrate<->EVM bridge round-trip property as QF_BV and
checks it with z3 — the same encoding as the EVM-SMT experiment
(~/experiments/evm-smt/gen_smt2.py), reused here. The property:

    for u64 s:  intoSubstrate(intoEvm(s)) == s   i.e.  (s * 10^9) / 10^9 == s
    with overflow-revert on the multiply.

A counterexample query (assert s2 != s) is SAT iff a bug exists. WIDTH is kept
IN-BAND (small) so it stays well below the ~52-bit bit-blast cliff and solves
fast — that is why traps are never near the cliff. Planted bugs are
ENCODING-LEVEL mutations (real, not a python toggle):

    none              the faithful round-trip            -> UNSAT (safe)
    wrong_const       divide by 10^9 + 1                 -> SAT (counterexample)
    off_by_one        s2 = (e / 10^9) + 1                -> SAT
    no_overflow_check drop the overflow guard            -> SAT (overflow cex)

z3 is imported lazily (present on Stitch; absent elsewhere -> raises clearly).
"""
from __future__ import annotations

from functools import lru_cache

DEC = 1_000_000_000
MUTATIONS = ("wrong_const", "off_by_one", "no_overflow_check")
# DEC = 10^9 needs >= 30 bits to represent; <= 52 keeps it below the bit-blast
# cliff (so it stays in-band and fast). Widths outside this are rejected.
MIN_WIDTH, MAX_WIDTH = 30, 52


def _build(width: int, mutation: str):
    if not isinstance(width, int) or not (MIN_WIDTH <= width <= MAX_WIDTH):
        raise ValueError(f"width {width} out of in-band range [{MIN_WIDTH},{MAX_WIDTH}]")
    if mutation not in ("none",) + MUTATIONS:
        raise ValueError(f"unknown mutation {mutation!r}")
    from z3 import (BitVec, BitVecVal, ZeroExt, Extract, UDiv, ULE, UGT, Not, Solver)
    W = width
    s = BitVec("s", W)
    divisor = DEC + 1 if mutation == "wrong_const" else DEC
    DECbv = BitVecVal(divisor, W)
    sol = Solver()
    ubound = (1 << min(W, 64)) - 1
    sol.add(ULE(s, BitVecVal(ubound, W)))
    s2w = ZeroExt(W, s)
    dec2w = ZeroExt(W, BitVecVal(DEC, W))     # multiply is always by the true DEC
    prod = s2w * dec2w
    hi = Extract(2 * W - 1, W, prod)
    e = Extract(W - 1, 0, prod)
    if mutation != "no_overflow_check":
        sol.add(Not(UGT(hi, BitVecVal(0, W))))  # overflow-revert guard
    s2 = UDiv(e, DECbv)
    if mutation == "off_by_one":
        s2 = s2 + BitVecVal(1, W)
    sol.add(s2 != s)                            # counterexample query
    return sol, s


@lru_cache(maxsize=8192)
def check(width: int, mutation: str = "none") -> dict:
    """Run z3 on the real BV encoding. Returns {status: sat|unsat, ...}.
    sat -> a counterexample 's' exists (a real bug); unsat -> safe in-band.
    Deterministic in (width, mutation) -> memoized (one z3 solve per instance,
    reused across all miners + verification)."""
    sol, s = _build(width, mutation)
    status = str(sol.check())
    out = {"status": status, "width": width, "mutation": mutation}
    if status == "sat":
        out["counterexample"] = sol.model()[s].as_long()
    return out


@lru_cache(maxsize=8192)
def verify_counterexample(width: int, mutation: str, s_value: int) -> bool:
    """Independently confirm a submitted counterexample really breaks the
    (mutated) property under the real encoding — the Lane C correctness gate."""
    # range-check first: BitVecVal silently wraps, so an out-of-range value
    # (e.g. 2**width) could otherwise alias a real counterexample.
    if not isinstance(s_value, int) or not (0 <= s_value < (1 << width)):
        return False
    from z3 import BitVecVal
    sol, s = _build(width, mutation)
    sol.add(s == BitVecVal(s_value, width))
    return str(sol.check()) == "sat"
