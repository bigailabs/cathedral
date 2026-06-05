"""Lane C — encoding submission (the encoding subnet).

Miners ENCODE a pinned (contract, property) and submit the encoding's verdict:
either a counterexample that breaks the property (a bug) or a proof there is
none in-band. Graded SAT (bug found, counterexample self-verifies) / UNSAT (no
in-band bug, proven) / TIMEOUT.

BOUNDED TO BUG-FINDING (the v1.2 cliff bound). The property is a `mul`-then-
`div` round-trip (the real Substrate<->EVM balance bridge shape from the
EVM-SMT write-up), but the WIDTH is kept small and in-band so every instance
is tractable. We mint find-a-planted-bug challenges; we NEVER mint
prove-safety-at-full-256-bit-width challenges — those land on the cliff and are
permanent timeouts that carry no signal.

Faithfulness gates (synthesis on the gauntlet-v0 mutation evidence + the
EVM-SMT cliff write-up — NOT verified v1.1/v1.2 code results), and crucially
they catch DIFFERENT attacks, neither alone sufficient:

  * counterexample check — a submitted "bug" must INDEPENDENTLY violate the
    property (cheap self-verification). Hidden truth is used only to ACCEPT a
    find, never to DENY a "safe" claim (denying safety is the cliff).
  * equivalence / vacuity gate — to earn a "safe" verdict the submitted encode
    must FLAG a battery of known-buggy probes (`_encode_passes_equivalence`).
    A vacuous/unsound encode that everyone agrees on passes consensus but fails
    here. This catches COLLECTIVE BLINDNESS. The DURABLE tooth: structural
    held-out rotation is weak against a reasoner; this needs real work.
  * windowed traps — lane-planted, in-band, NEVER-near-cliff known bugs. A
    "safe" on a trap is a definitive miss -> 0. (Near-cliff traps would
    false-nuke honest miners who legitimately time out, so we never plant them.)
  * consensus (in scaffold.consensus, run by the validator across the round) —
    a peer's verified counterexample REFUTES every "safe" peer. This catches the
    cross-miner OUTLIER / sound-but-incomplete miss that the equivalence gate
    passes. Consensus alone misses collective blindness; the equivalence gate
    alone misses the incomplete miss. Both are required.
"""
from __future__ import annotations

import hashlib
import math
import random

from ..contract import (
    GenerateCtx, HiddenMetadata, Outcome, PublicProblem, ScoreResult,
    Submission, VerifierResult,
)
from .. import grading, timing

FAMILY_ID = "encoding_v1"
SCHEMA_VERSION = 1
K = 1000                       # the non-power-of-2 scale constant (10^3 stand-in)
MAX_INBAND_WIDTH = 12          # hard cap: never near the bit-blast cliff
_MUTANTS = ["off_by_one", "wrong_const", "overflow", "flip_cmp"]


def _roundtrip(mutation: str, x: int, width: int, bugx: int) -> int:
    """Model intoSubstrate(intoEvm(x)). The mutation plants the bug."""
    if mutation == "none":
        return (x * K) // K
    if mutation == "off_by_one":
        return (x * K) // K + (1 if x == bugx else 0)
    if mutation == "wrong_const":
        return (x * K) // (K + 1)
    if mutation == "overflow":
        m = 1 << width
        return ((x * K) % m) // K
    if mutation == "flip_cmp":
        return (x * K) // K + 1           # property is x==rt; this breaks it widely
    return (x * K) // K


def _property_holds(mutation: str, x: int, width: int, bugx: int) -> bool:
    return _roundtrip(mutation, x, width, bugx) == x


def _find_counterexample(mutation: str, width: int, bugx: int) -> int | None:
    """Independent, bounded scan — tractable by construction (width <= 12)."""
    for x in range(1 << width):
        if not _property_holds(mutation, x, width, bugx):
            return x
    return None


# --- equivalence / vacuity gate ------------------------------------------
# Probe a SUBMITTED encode against a battery of known-buggy inputs. A faithful
# encode must FLAG them (predict the property is violated); a vacuous/unsound
# encode flags nothing. This is the "needs solving" tooth: you cannot earn a
# "safe" verdict with an encode that does not actually distinguish buggy inputs.
# (Real system: run the solver on the submitted CNF/SMT for each probe; here we
# model the encode's behavior. The point is the GATE, not the transport.)
_EQUIV_PROBES = [("off_by_one", 3, 3), ("wrong_const", 5, 0), ("overflow", None, 0)]


def _encode_flags(encode: str, mutation: str, x: int, width: int, bugx: int) -> bool:
    if encode == "vacuous":
        return False                       # unsound: never flags anything
    return not _property_holds(mutation, x, width, bugx)


def _encode_passes_equivalence(encode: str, width: int) -> bool:
    for mutation, x, bugx in _EQUIV_PROBES:
        xi = (1 << width) - 1 if x is None else x
        if not _encode_flags(encode, mutation, xi, width, bugx):
            return False
    return True


_Z3_OK = None


def have_z3() -> bool:
    """True if the real z3 encoder is usable (z3 importable). Cached."""
    global _Z3_OK
    if _Z3_OK is None:
        try:
            import z3  # noqa: F401
            _Z3_OK = True
        except Exception:
            _Z3_OK = False
    return _Z3_OK


class EncodingLane:
    family_id = FAMILY_ID
    schema_version = SCHEMA_VERSION
    supports_consensus = True              # the validator runs a cross-miner pass

    # ---- REAL z3 backend (used when z3 is present, e.g. on Stitch) ----------
    def _mint_z3(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        from . import encoding_real as ER
        rng = random.Random(ctx.seed)
        width = min(ER.MAX_WIDTH, max(ER.MIN_WIDTH, 32 + 2 * ctx.tier))  # in-band [30,48]
        roll = rng.random()
        is_trap = roll < 0.15
        mutation = "none" if (roll < 0.45 and not is_trap) else rng.choice(ER.MUTATIONS)
        # Solve-hard trigger: the fault fires only when low_k(mix(s)) == T, where
        # mix is a splitmix/murmur finalizer (odd multipliers M1,M2). The
        # xorshifts make the preimage have no closed form -> the only general way
        # to find a witness is to run a solver. k grows with tier -> rarer
        # preimage -> harder -> worth more.
        m1 = rng.randrange(3, 1 << min(width, 31)) | 1            # odd mix mult
        m2 = rng.randrange(3, 1 << min(width, 31)) | 1           # odd mix mult
        trig_k = min(width - 2, 8 + 2 * ctx.tier)                 # difficulty knob
        if mutation == "none":
            trig_t = 0
        else:
            # anchor T on a real seed s0 (anywhere in the full 2^W domain) so an
            # in-trigger witness is guaranteed to EXIST for off_by_one (and very
            # likely for the others); z3 is the final arbiter of is_buggy below.
            s0 = rng.randrange(0, 1 << width)
            trig_t = ER.mix_ref(s0, width, m1, m2) % (1 << trig_k)
        r = ER.check(width, mutation, m1, m2, trig_k, trig_t)     # z3: ground truth
        is_buggy = r["status"] == "sat"
        rarity = round(trig_k / width, 4)                         # 0..1 hardness
        task_id = hashlib.sha256(f"{FAMILY_ID}:z3:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={"property_id": "modular_roundtrip", "backend": "z3",
                          "width": width, "mutation": mutation,
                          "round_const": ER.round_const(width),
                          # full contract is PUBLIC: the mixing trigger gate is
                          # part of the code the miner audits + solves.
                          "trigger": {"kind": "mix", "m1": m1, "m2": m2,
                                      "k": trig_k, "t": trig_t},
                          "task": "find_counterexample_or_prove_inband_safe"},
            time_limit_seconds=300)
        hidden = HiddenMetadata(
            task_id=task_id, generator_version="roundtrip-z3/2",
            hidden_payload={"backend": "z3", "mutation": mutation, "width": width,
                            "m1": m1, "m2": m2, "trig_k": trig_k, "trig_t": trig_t,
                            "is_buggy": is_buggy, "witness": r.get("counterexample"),
                            "rarity": rarity, "is_trap": is_trap})
        return problem, hidden

    def _validate_z3(self, problem, hidden, submission) -> VerifierResult:
        from . import encoding_real as ER
        h = hidden.hidden_payload
        ans = submission.answer
        verdict = ans.get("verdict")
        encode = ans.get("encode", "faithful")
        # rarity rides along for score(); wall_ms does NOT come from the miner —
        # speed is server-measured and passed into score() (see scaffold.timing).
        det = {"is_trap": h["is_trap"], "mutation": h["mutation"], "verdict": verdict,
               "encode": encode, "backend": "z3", "rarity": h.get("rarity", 0.0)}
        if verdict not in ("bug", "safe"):
            return VerifierResult(False, Outcome.INVALID, 0.0, "no_verdict", det)
        if verdict == "bug":
            # REAL z3 check: the counterexample must trigger the fault AND break
            # the property (a guessed constant almost never satisfies the trigger)
            if not ER.verify_counterexample(h["width"], h["mutation"], h["m1"], h["m2"],
                                            h["trig_k"], h["trig_t"], ans.get("counterexample")):
                return VerifierResult(True, Outcome.INVALID, 0.0, "bad_counterexample", det)
            return VerifierResult(True, Outcome.SAT, 1.0, None, {**det, "cex": ans.get("counterexample")})
        if encode == "vacuous":
            return VerifierResult(True, Outcome.INVALID, 0.0, "unsound_encode", det)
        # a trap punishes a "safe" claim ONLY when a real in-trigger witness
        # actually exists (z3-confirmed) — never false-nuke an honestly-safe one.
        if h["is_trap"] and h.get("is_buggy"):
            return VerifierResult(True, Outcome.INVALID, 0.0, "failed_windowed_trap", det)
        if not ans.get("solved"):
            return VerifierResult(True, Outcome.TIMEOUT, 0.0, "unproven_safe", det)
        return VerifierResult(True, Outcome.UNSAT, 0.0, "no_bug_unrewarded", det)

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        if have_z3():
            return self._mint_z3(ctx)
        rng = random.Random(ctx.seed)
        width = min(MAX_INBAND_WIDTH, 6 + ctx.tier)        # in-band, never near cliff
        # ~1/3 safe base, ~1/6 trap, rest random mutants
        roll = rng.random()
        is_trap = roll < 0.15
        if roll < 0.45 and not is_trap:
            mutation = "none"
        else:
            mutation = rng.choice(_MUTANTS)
        bugx = rng.randrange(1, 1 << width)
        task_id = hashlib.sha256(f"{FAMILY_ID}:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={
                "property_id": "substrate_evm_roundtrip",
                "width": width, "scale": K,
                # the miner must encode THIS contract (mutation revealed as the
                # contract variant) and search for a counterexample. The work is
                # the search/proof, not guessing which mutant it is.
                "contract": {"mutation": mutation, "bugx": bugx},
                "task": "find_counterexample_or_prove_inband_safe",
            },
            time_limit_seconds=300,
        )
        cex = _find_counterexample(mutation, width, bugx)
        hidden = HiddenMetadata(
            task_id=task_id, generator_version="roundtrip-mut/1",
            hidden_payload={"mutation": mutation, "is_buggy": cex is not None,
                            "witness": cex, "is_trap": is_trap, "width": width,
                            "bugx": bugx},
        )
        return problem, hidden

    def validate_submission(
        self, problem: PublicProblem, hidden: HiddenMetadata, submission: Submission
    ) -> VerifierResult:
        if hidden.hidden_payload.get("backend") == "z3":
            return self._validate_z3(problem, hidden, submission)
        h = hidden.hidden_payload
        ans = submission.answer
        verdict = ans.get("verdict")                       # "bug" | "safe"
        encode = ans.get("encode", "faithful")
        det = {"is_trap": h["is_trap"], "mutation": h["mutation"],
               "verdict": verdict, "encode": encode}
        if verdict not in ("bug", "safe"):
            return VerifierResult(False, Outcome.INVALID, 0.0, "no_verdict", det)

        if verdict == "bug":
            # NO positive weight on the non-z3 fallback. The verified-artifact
            # invariant requires an INDEPENDENT z3 re-check (ER.verify_counter-
            # example); the toy `_property_holds` model isn't that, and its bug
            # is at a PUBLIC bugx (trivially submittable). So without z3 a "bug"
            # claim can't be confirmed -> it earns nothing and is not a FIND
            # (so it can't refute peers via consensus either). z3 is REQUIRED to
            # earn in Lane C; this path exists only for offline dev/smoke runs.
            return VerifierResult(True, Outcome.INVALID, 0.0, "z3_required_for_verified_find", det)

        # verdict == "safe": NOT oracle-adjudicated from hidden is_buggy on
        # ordinary instances (proving safety at width is the cliff). Three checks:
        #   1. equivalence / vacuity gate — the encode must do real work
        if not _encode_passes_equivalence(encode, h["width"]):
            return VerifierResult(True, Outcome.INVALID, 0.0, "unsound_encode", det)
        #   2. windowed trap — a lane-planted, in-band, never-near-cliff known
        #      bug; a "safe" here is a definitive miss
        if h["is_trap"]:
            return VerifierResult(True, Outcome.INVALID, 0.0, "failed_windowed_trap", det)
        #   3. must claim to have actually solved the bounded instance
        if not ans.get("solved"):
            return VerifierResult(True, Outcome.TIMEOUT, 0.0, "unproven_safe", det)
        # A sound encode that survives traps + claims solved is ACCEPTED (not
        # bad-faith) but earns NO positive weight: "safe" cannot be independently
        # verified offline (proving safety at width is the cliff), and a bare
        # `solved` flag is self-asserted. Positive weight requires a VERIFIED
        # artifact (a counterexample). raw_metric=0 -> score 0. Consensus still
        # flags it as an outlier if a peer exhibits a real counterexample.
        return VerifierResult(True, Outcome.UNSAT, 0.0, "no_bug_unrewarded", det)

    def score(self, problem: PublicProblem, verifier: VerifierResult,
              *, wall_ms: float | None = None) -> ScoreResult:
        d = verifier.details
        # A VERIFIED find is rewarded on WITNESS QUALITY: every real find earns a
        # correctness floor (0.4), plus speed (faster verified solve wins) and
        # rarity (a witness in a rarer trigger band is worth more — a guesser
        # can't stumble onto it). Bounded to [0.4, 1.0]; non-finds score 0.
        # speed uses SERVER-MEASURED wall_ms (scaffold.timing), never a miner field.
        if verifier.parsed_ok and verifier.outcome == Outcome.SAT \
                and math.isfinite(verifier.raw_metric) and verifier.raw_metric > 0:
            wm = wall_ms if wall_ms is not None else problem.time_limit_seconds * 1000
            speed = grading.speed_bonus(wm, timing.reference_ms(problem))
            rarity = max(0.0, min(1.0, float(d.get("rarity", 0.0))))
            score = round(min(1.0, 0.4 + 0.3 * speed + 0.3 * rarity), 6)
            return ScoreResult(score, None,
                               {"base": 1.0, "speed": speed, "rarity": rarity,
                                "trap": float(d.get("is_trap", False))})
        # everything else (unproven-safe, trap miss, unsound, bad cex) -> 0
        sr = grading.grade(verifier, wall_ms=0.0,
                           time_limit_ms=problem.time_limit_seconds * 1000,
                           speed_aware=False)
        return ScoreResult(sr.weighted_score, sr.rejection_reason,
                           {"trap": float(d.get("is_trap", False))})
