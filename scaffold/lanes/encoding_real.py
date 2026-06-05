"""Lane C — REAL encoding (z3), with NON-TRIVIAL planted witnesses.

Encodes a bridge round-trip property as QF_BV and checks it with z3. We use a
CLEAN MODULAR round-trip so the property is faithful over the ENTIRE 2^W input
domain (the earlier `*10^9`-with-overflow model left only a handful of valid
in-band inputs — e.g. s < 4 at W=32 — which made s=0 a trivial universal
witness and left no room for a rarity knob). The faithful map:

    intoEvm(s)        = s * K            (mod 2^W),  K odd  -> a bijection
    intoSubstrate(e)  = e * Kinv         (mod 2^W),  Kinv = K^-1 mod 2^W
    property:  intoSubstrate(intoEvm(s)) == s        holds for ALL s

WHY THE TRIGGER PREDICATE. A globally-on mutation ("always add 1") has a
trivial universal witness (s=0 breaks it everywhere), so a miner that blindly
submits a constant earns full credit with no work. The fault is therefore GATED
behind a per-instance modular predicate on s:

    trigger(s)  :=  low_k_bits( s * C )  ==  T          (C odd, T,k per-instance)

C odd => s -> (s*C mod 2^k) is a bijection on the low k bits, so EXACTLY
2^(W-k) values of s satisfy the trigger, spread unpredictably across the FULL
domain. The whole contract (K, C, T, k) is PUBLIC — a miner encodes it and
SOLVES for a satisfying s. There is no universal constant: a guesser submitting
0/1 almost never satisfies the trigger, while z3 finds a witness instantly.
Rarity = 2^-k is the difficulty knob (bigger k -> rarer witness -> worth more,
see lane.score). Production should harden `trigger` to a non-invertible
predicate (hash) so it resists hand-solving too; the modular form here is fast
and already defeats the constant-witness exploit.

Mutations (all gated by the trigger, all with an in-trigger witness):
    none           faithful everywhere                       -> UNSAT (safe)
    off_by_one     s2 = s + 1               when trigger(s)  -> SAT (any trig s)
    wrong_const    s2 = e * (Kinv + 2)      when trigger(s)  -> SAT
    truncate_low   s2 = s & ~1              when trigger(s)  -> SAT (odd trig s)

z3 is imported lazily (present on Stitch; absent elsewhere -> raises clearly).
"""
from __future__ import annotations

from functools import lru_cache

MUTATIONS = ("off_by_one", "wrong_const", "truncate_low")
# native-width modular multiplies stay tractable well past this; <= 52 keeps it
# clearly below the bit-blast cliff. Widths outside this are rejected.
MIN_WIDTH, MAX_WIDTH = 30, 48


def round_const(width: int) -> int:
    """Deterministic odd round-trip constant K for a given width (invertible mod
    2^W since it is odd). Public — part of the contract the miner audits."""
    return (0x9E3779B1 | 1) & ((1 << width) - 1) | 1


def _build(width: int, mutation: str, trig_c: int, trig_k: int, trig_t: int):
    if not isinstance(width, int) or not (MIN_WIDTH <= width <= MAX_WIDTH):
        raise ValueError(f"width {width} out of in-band range [{MIN_WIDTH},{MAX_WIDTH}]")
    if mutation not in ("none",) + MUTATIONS:
        raise ValueError(f"unknown mutation {mutation!r}")
    if not (isinstance(trig_k, int) and 0 <= trig_k <= width):
        raise ValueError(f"trig_k {trig_k} out of range [0,{width}]")
    if not (isinstance(trig_c, int) and isinstance(trig_t, int)):
        raise ValueError("trig_c / trig_t must be ints")
    if trig_k and not (0 <= trig_t < (1 << trig_k)):
        raise ValueError(f"trig_t {trig_t} out of range [0,2^{trig_k})")
    from z3 import BitVec, BitVecVal, Extract, Solver
    W = width
    mod = 1 << W
    K = round_const(W)
    Kinv = pow(K, -1, mod)
    s = BitVec("s", W)
    sol = Solver()
    e = s * BitVecVal(K, W)                      # intoEvm  (mod 2^W)
    back = e * BitVecVal(Kinv, W)                # intoSubstrate -> == s, all s
    if mutation == "none":
        s2 = back
    else:
        if mutation == "off_by_one":
            mut = back + BitVecVal(1, W)
        elif mutation == "wrong_const":
            mut = e * BitVecVal((Kinv + 2) % mod, W)   # wrong inverse
        else:  # truncate_low
            mut = back & BitVecVal((~1) & (mod - 1), W)
        if trig_k:
            tval = Extract(trig_k - 1, 0, s * BitVecVal(trig_c, W))
            from z3 import If
            s2 = If(tval == BitVecVal(trig_t, trig_k), mut, back)
        else:
            s2 = mut                            # k=0 -> trigger always on
    sol.add(s2 != s)                            # counterexample query
    return sol, s


@lru_cache(maxsize=8192)
def check(width: int, mutation: str, trig_c: int, trig_k: int, trig_t: int) -> dict:
    """z3 on the real BV encoding with the trigger gate. Returns
    {status: sat|unsat, ...}; sat -> a witness s exists (a real, non-trivial
    counterexample). Deterministic in the inputs -> memoized (one solve per
    instance, reused across all miners + verification)."""
    sol, s = _build(width, mutation, trig_c, trig_k, trig_t)
    status = str(sol.check())
    out = {"status": status, "width": width, "mutation": mutation,
           "trig_c": trig_c, "trig_k": trig_k, "trig_t": trig_t}
    if status == "sat":
        out["counterexample"] = sol.model()[s].as_long()
    return out


@lru_cache(maxsize=8192)
def verify_counterexample(width: int, mutation: str, trig_c: int, trig_k: int,
                          trig_t: int, s_value: int) -> bool:
    """Independently confirm a submitted s really triggers the fault AND breaks
    the property under the real encoding — the Lane C correctness gate. A value
    that misses the trigger (e.g. a guessed constant) pins to an unsatisfiable
    system and returns False."""
    if not isinstance(s_value, int) or not (0 <= s_value < (1 << width)):
        return False
    from z3 import BitVecVal
    sol, s = _build(width, mutation, trig_c, trig_k, trig_t)
    sol.add(s == BitVecVal(s_value, width))
    return str(sol.check()) == "sat"
