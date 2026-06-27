"""Validator weight-feed health thresholds (single source of truth).

Pure, dependency-free threshold logic shared by:

  - the read-only ``/v1/admin/validator-health`` endpoint (scaffold.publisher.app)
  - the standalone release gate (scripts/validator_release_gate.py)

Keeping the numbers here means the live health surface and the pre-deploy gate
can never silently disagree on what "fresh" means. Values come straight from
``deploy/RELIABILITY_UPGRADE_PLAN.md`` (Vector Freshness Thresholds + Phase 7).

All thresholds are seconds. The chain constants (tempo) are verified against
finney: SN39 tempo = 360 blocks = 72 min @ 12 s/block.
"""

from __future__ import annotations

from typing import Any

# ---- chain constants (verified on finney 2026-06-27) ----------------------
BLOCK_SECONDS = 12.0
TEMPO_BLOCKS = 360
TEMPO_SECONDS = TEMPO_BLOCKS * BLOCK_SECONDS  # 4320 s == 72 min

# ---- signed-vector freshness (now - generated_at) -------------------------
# healthy <= 2 min; warn > 5 min; page > 10 min; hard stale ceiling = 1 tempo.
VECTOR_HEALTHY_SECONDS = 120.0
VECTOR_WARN_SECONDS = 300.0
VECTOR_PAGE_SECONDS = 600.0
VECTOR_HARD_CEILING_SECONDS = TEMPO_SECONDS

# ---- UID200 (our validator) on-chain update age ---------------------------
# healthy <= 5 min; warn > 10 min; page > 20 min.
UID200_HEALTHY_SECONDS = 300.0
UID200_WARN_SECONDS = 600.0
UID200_PAGE_SECONDS = 1200.0

# ---- release-gate pass/fail limits ----------------------------------------
# These are the hard limits the validator release gate enforces (see the plan's
# "Validator release gate" checklist). They are intentionally the *warn* edges:
# the gate fails a deploy at the first sign of staleness, not at the page edge.
GATE_VECTOR_MAX_AGE_SECONDS = 300.0   # signed-vector age <= 5 min
GATE_UID200_MAX_AGE_SECONDS = 600.0   # UID200 update age <= 10 min
GATE_WEIGHTS_FEED_MAX_5XX = 0         # 0x 5xx on the weight feed


def classify_age(
    age_seconds: float | None,
    *,
    healthy: float,
    warn: float,
    page: float,
) -> str:
    """Map an age (seconds) to one of: ok | warn | page | unknown.

    ``None`` (no timestamp available / cold process) is ``unknown`` — a missing
    signal is not silently treated as healthy.
    """
    if age_seconds is None:
        return "unknown"
    if age_seconds > page:
        return "page"
    if age_seconds > warn:
        return "warn"
    if age_seconds <= healthy:
        return "ok"
    # Between healthy and warn: degraded but not yet alerting.
    return "ok"


def vector_status(age_seconds: float | None) -> dict[str, Any]:
    """Health classification for the signed-vector age."""
    level = classify_age(
        age_seconds,
        healthy=VECTOR_HEALTHY_SECONDS,
        warn=VECTOR_WARN_SECONDS,
        page=VECTOR_PAGE_SECONDS,
    )
    over_ceiling = age_seconds is not None and age_seconds > VECTOR_HARD_CEILING_SECONDS
    return {
        "age_seconds": age_seconds,
        "level": level,
        "over_hard_ceiling": over_ceiling,
        "healthy_seconds": VECTOR_HEALTHY_SECONDS,
        "warn_seconds": VECTOR_WARN_SECONDS,
        "page_seconds": VECTOR_PAGE_SECONDS,
        "hard_ceiling_seconds": VECTOR_HARD_CEILING_SECONDS,
    }


def uid200_status(age_seconds: float | None) -> dict[str, Any]:
    """Health classification for UID200's on-chain update age."""
    level = classify_age(
        age_seconds,
        healthy=UID200_HEALTHY_SECONDS,
        warn=UID200_WARN_SECONDS,
        page=UID200_PAGE_SECONDS,
    )
    return {
        "age_seconds": age_seconds,
        "level": level,
        "healthy_seconds": UID200_HEALTHY_SECONDS,
        "warn_seconds": UID200_WARN_SECONDS,
        "page_seconds": UID200_PAGE_SECONDS,
    }
