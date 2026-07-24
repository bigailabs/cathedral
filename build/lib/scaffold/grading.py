"""Three-outcome grading — SHARED logic, built once, used by every lane.

Outcomes: SAT (witness self-verifies), UNSAT (proof cert checks), TIMEOUT
(neither closed in time), INVALID (malformed). This is where the
cost-minimizing attestation policy lives:

    SAT and UNSAT SELF-VERIFY (a witness / a DRAT cert is cheap to check),
    so they need NO attestation. Only a TIMEOUT claim is unfalsifiable
    locally — "I ran the solver to the limit and it didn't close" — so a
    TIMEOUT is the ONLY outcome that an attested run vouches for.

A lane decides the Outcome + raw_metric in verify; this turns that into a
bounded, speed-aware score. Keeping it here means no lane re-implements the
speed curve or the attest policy.
"""
from __future__ import annotations

import math

from .contract import Outcome, ScoreResult, VerifierResult


def attestation_required(outcome: Outcome) -> bool:
    """Cost-minimizing rule: attest TIMEOUTS only."""
    return outcome == Outcome.TIMEOUT


def speed_bonus(wall_ms: float, reference_ms: float) -> float:
    """Scale-free fastest-wins curve in (0,1): a solve at the challenge's
    REFERENCE (expected) time scores 0.5, an instant solve ~1.0, and one much
    slower decays toward 0 — bonus = ref / (ref + wall). Unlike a 1 - w/limit
    curve, this spreads the field even when solves are far under the hard limit
    (the limit is for liveness, not speed scaling). wall_ms is SERVER-measured
    (scaffold.timing); guards keep a bad value at 0, never >1 or inf."""
    if not math.isfinite(reference_ms) or reference_ms <= 0:
        return 0.0
    if not math.isfinite(wall_ms) or wall_ms < 0:
        return 0.0
    return round(reference_ms / (reference_ms + wall_ms), 6)


def grade(
    verifier: VerifierResult,
    *,
    wall_ms: float,
    time_limit_ms: float,
    reference_ms: float | None = None,
    attested_ok: bool | None = None,
    speed_aware: bool = True,
) -> ScoreResult:
    """Fold a VerifierResult into a bounded score.

    reference_ms: the challenge's expected solve time, the half-credit point of
                  the speed curve (server-supplied). Falls back to time_limit_ms
                  if absent (shallow, legacy curve).
    attested_ok:  result of the attestation check when the outcome requires one
                  (TIMEOUT). None means "not applicable / not checked".
    """
    if not verifier.parsed_ok or verifier.outcome == Outcome.INVALID:
        return ScoreResult(0.0, verifier.rejection_reason or "invalid")

    if verifier.outcome == Outcome.TIMEOUT:
        # A timeout always scores 0; the reason records WHY. The attest policy
        # applies only when attestation was actually attempted (attested_ok is
        # not None — an execution lane). A lane that uses TIMEOUT for its own
        # semantics (e.g. "claimed safe but didn't solve") keeps its reason.
        reason = verifier.rejection_reason
        if attested_ok is not None and not attested_ok:
            reason = reason or "timeout_not_attested"
        else:
            reason = reason or "timeout"
        return ScoreResult(0.0, reason, {"raw": verifier.raw_metric})

    # SAT / UNSAT: credit only for a finite raw_metric. A lane signals an
    # unverifiable artifact (stub UNSAT cert, unprovable-safe) by raw_metric=0,
    # so it lands here at score 0 with its own reason carried through.
    if not math.isfinite(verifier.raw_metric):
        return ScoreResult(0.0, "non_finite_metric")
    base = max(0.0, min(1.0, verifier.raw_metric))
    bonus = speed_bonus(wall_ms, reference_ms if reference_ms is not None else time_limit_ms)
    if speed_aware:
        score = round(0.5 * base + 0.5 * base * bonus, 6)
    else:
        score = base
    score = max(0.0, min(1.0, score))
    reason = verifier.rejection_reason if score == 0.0 else None
    return ScoreResult(weighted_score=score, rejection_reason=reason,
                       score_parts={"base": base, "speed": bonus})
