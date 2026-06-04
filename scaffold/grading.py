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


def speed_bonus(wall_ms: float, time_limit_ms: float) -> float:
    """Fastest-valid-wins curve in [0,1]: a solve that lands instantly scores
    ~1.0; one that just barely beats the limit scores ~0. Hardened against a
    miner-supplied wall_ms (negative / non-finite / over-limit) — those yield 0
    bonus, never a >1 or inf score. In deployment wall_ms is SERVER-measured;
    the scaffold trusts the field but clamps it."""
    if not math.isfinite(time_limit_ms) or time_limit_ms <= 0:
        return 0.0
    if not math.isfinite(wall_ms) or wall_ms < 0:
        return 0.0
    w = min(wall_ms, time_limit_ms)
    return round(max(0.0, 1.0 - w / time_limit_ms), 6)


def grade(
    verifier: VerifierResult,
    *,
    wall_ms: float,
    time_limit_ms: float,
    attested_ok: bool | None = None,
    speed_aware: bool = True,
) -> ScoreResult:
    """Fold a VerifierResult into a bounded score.

    attested_ok: result of the attestation check when the outcome requires one
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
    bonus = speed_bonus(wall_ms, time_limit_ms)
    if speed_aware:
        score = round(0.5 * base + 0.5 * base * bonus, 6)
    else:
        score = base
    score = max(0.0, min(1.0, score))
    reason = verifier.rejection_reason if score == 0.0 else None
    return ScoreResult(weighted_score=score, rejection_reason=reason,
                       score_parts={"base": base, "speed": bonus})
