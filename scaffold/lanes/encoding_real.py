"""Lane C — REAL encoding (z3), with SOLVE-HARD planted witnesses.

Encodes a bridge round-trip property as QF_BV and checks it with z3. We use a
CLEAN MODULAR round-trip so the property is faithful over the ENTIRE 2^W input
domain (the earlier `*10^9`-with-overflow model left only a handful of valid
in-band inputs — e.g. s < 4 at W=32 — which made s=0 a trivial universal
witness and left no room for a difficulty knob). The faithful map:

    intoEvm(s)        = s * K            (mod 2^W),  K odd  -> a bijection
    intoSubstrate(e)  = e * Kinv         (mod 2^W),  Kinv = K^-1 mod 2^W
    property:  intoSubstrate(intoEvm(s)) == s        holds for ALL s

WHY A SOLVE-HARD TRIGGER. A globally-on mutation ("always add 1") has a trivial
universal witness (s=0 breaks it everywhere). Gating the fault behind a *linear*
predicate (low_k(s*C)==T) fixes the constant exploit but is algebraically
invertible: a miner computes s = T*C^-1 mod 2^k by hand and never runs a solver.
For a LIVE subnet the witness must require an actual solve. So the fault is
gated behind a BIT-MIXING predicate:

    trigger(s)  :=  low_k_bits( mix(s) )  ==  T

    mix(x) = let x = (x ^ (x >> r1)) * M1        (mod 2^W)
                 x = (x ^ (x >> r2)) * M2        (mod 2^W)
             in  x ^ (x >> r1)

a splitmix/murmur-style finalizer (M1,M2 odd -> bijection, so a unique-per-bits
preimage set of size 2^(W-k) exists). The xorshifts break the linearity and
multiplicativity that a modular inverse would exploit, so there is no closed
form for the preimage: the only general way to find s with low_k(mix(s))==T is
to give the published encoding to a solver. The whole contract (K, M1, M2, T, k,
shifts) is PUBLIC — finding the witness is the work, and it is genuine solve
work. Rarity = 2^-k is the difficulty knob (bigger k -> rarer preimage -> harder
-> worth more, see lane.score).

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
# native-width modular multiplies (4 of them: 2 round-trip + 2 mix) stay
# tractable here; <= 44 keeps z3 comfortably fast for the live loop. Widths
# outside this are rejected.
MIN_WIDTH, MAX_WIDTH = 30, 44


def round_const(width: int) -> int:
    """Deterministic odd round-trip constant K for a given width (invertible mod
    2^W since it is odd). Public — part of the contract the miner audits."""
    return (0x9E3779B1 | 1) & ((1 << width) - 1) | 1


def _shifts(width: int) -> tuple[int, int]:
    return max(1, width // 2), max(1, width // 3)


def mix_ref(s: int, width: int, m1: int, m2: int) -> int:
    """Pure-python reference for the z3 `mix` (for anchoring T at mint time and
    for tests). Must stay bit-identical to `_mix` below."""
    mod = (1 << width) - 1 + 1
    mask = mod - 1
    r1, r2 = _shifts(width)
    x = s & mask
    x = ((x ^ (x >> r1)) * m1) & mask
    x = ((x ^ (x >> r2)) * m2) & mask
    x = (x ^ (x >> r1)) & mask
    return x


def _mix(x, width: int, m1: int, m2: int):
    from z3 import BitVecVal, LShR
    W = width
    r1, r2 = _shifts(W)
    x = (x ^ LShR(x, r1)) * BitVecVal(m1, W)
    x = (x ^ LShR(x, r2)) * BitVecVal(m2, W)
    return x ^ LShR(x, r1)


def _build(width: int, mutation: str, m1: int, m2: int, trig_k: int, trig_t: int):
    if not isinstance(width, int) or not (MIN_WIDTH <= width <= MAX_WIDTH):
        raise ValueError(f"width {width} out of in-band range [{MIN_WIDTH},{MAX_WIDTH}]")
    if mutation not in ("none",) + MUTATIONS:
        raise ValueError(f"unknown mutation {mutation!r}")
    if not (isinstance(trig_k, int) and 0 <= trig_k <= width):
        raise ValueError(f"trig_k {trig_k} out of range [0,{width}]")
    if not all(isinstance(v, int) for v in (m1, m2, trig_t)):
        raise ValueError("m1 / m2 / trig_t must be ints")
    if not (m1 & 1 and m2 & 1):
        raise ValueError("mix multipliers must be odd (bijection)")
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
            tval = Extract(trig_k - 1, 0, _mix(s, W, m1, m2))
            from z3 import If
            s2 = If(tval == BitVecVal(trig_t, trig_k), mut, back)
        else:
            s2 = mut                            # k=0 -> trigger always on
    sol.add(s2 != s)                            # counterexample query
    return sol, s


@lru_cache(maxsize=8192)
def check(width: int, mutation: str, m1: int, m2: int, trig_k: int, trig_t: int) -> dict:
    """z3 on the real BV encoding with the solve-hard trigger gate. Returns
    {status: sat|unsat, ...}; sat -> a witness s exists (a real counterexample
    that required solving the mixing to find). Deterministic in the inputs ->
    memoized (one solve per instance, reused across all miners + verification)."""
    sol, s = _build(width, mutation, m1, m2, trig_k, trig_t)
    status = str(sol.check())
    out = {"status": status, "width": width, "mutation": mutation,
           "m1": m1, "m2": m2, "trig_k": trig_k, "trig_t": trig_t}
    if status == "sat":
        out["counterexample"] = sol.model()[s].as_long()
    return out


def verify_counterexample(width: int, mutation: str, m1: int, m2: int,
                          trig_k: int, trig_t: int, s_value) -> bool:
    """Independently confirm a submitted s really triggers the fault AND breaks
    the property under the real encoding — the Lane C correctness gate. A value
    that misses the trigger (e.g. a guessed constant) pins to an unsatisfiable
    system and returns False.

    The public type/range check runs BEFORE the cached solve so a miner-supplied
    non-int (e.g. an unhashable list) can never reach @lru_cache and raise — a
    malformed submission is just an invalid counterexample, not a validator DoS.
    `bool` is excluded so True/False can't alias 1/0."""
    if isinstance(s_value, bool) or not isinstance(s_value, int) \
            or not (0 <= s_value < (1 << width)):
        return False
    return _verify_cached(width, mutation, m1, m2, trig_k, trig_t, s_value)


@lru_cache(maxsize=8192)
def _verify_cached(width: int, mutation: str, m1: int, m2: int,
                   trig_k: int, trig_t: int, s_value: int) -> bool:
    from z3 import BitVecVal
    sol, s = _build(width, mutation, m1, m2, trig_k, trig_t)
    sol.add(s == BitVecVal(s_value, width))
    return str(sol.check()) == "sat"
