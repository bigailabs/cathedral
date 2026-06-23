"""Game config — sets the per-miner generator knobs to small, fast, deterministic
shapes BEFORE the scaffold's per_miner helpers read them.

Import this module first. It only sets env defaults that are not already set, so
an operator can still override any of them from the outside.
"""
from __future__ import annotations

import os

# Fixed epoch so a game run is fully reproducible (no wall-clock dependence).
GAME_EPOCH = 1000

# A stable seed secret => pm-* ids and CNFs are reproducible across processes
# (required: per_miner fails closed without one when enabled). This is a LOCAL
# game secret, not a production key.
_DEFAULTS = {
    "CATHEDRAL_PERMINER_ENABLED": "1",
    "CATHEDRAL_PERMINER_SEED_SECRET": "cathedral-local-game-seed-v1",
    # Small allotment so a round is quick and the anti-copy scan is cheap.
    "CATHEDRAL_PERMINER_ALLOTMENT_T1": "2",
    "CATHEDRAL_PERMINER_ALLOTMENT_T2": "2",
    # Small, fast SAT shapes near the m/n ~ 4.26 phase transition.
    "CATHEDRAL_PERMINER_NVARS_T1": "40",
    "CATHEDRAL_PERMINER_NCLAUSES_T1": "170",
    "CATHEDRAL_PERMINER_NVARS_T2": "70",
    "CATHEDRAL_PERMINER_NCLAUSES_T2": "298",
    # tier-1 easy floor, tier-2 hard differentiator.
    "CATHEDRAL_PERMINER_METHOD_T1": "biased",
    "CATHEDRAL_PERMINER_METHOD_T2": "ajm",
    "CATHEDRAL_PERMINER_WEIGHT_T1": "1.0",
    "CATHEDRAL_PERMINER_WEIGHT_T2": "2.0",
}


def apply() -> None:
    for k, v in _DEFAULTS.items():
        os.environ.setdefault(k, v)


# Game-level knobs (not part of the scaffold).
LIVENESS_WINDOW_S = 90          # heartbeat must be newer than this to be "live"
# Speed-curve half-credit point per tier. Set near the real host wall time
# (sandbox spawn + solve), so honest-fast vs honest-slow separate visibly.
TIER_REFERENCE_MS = {1: 500.0, 2: 700.0}
TIER_REQUIRES_ATTEST = {1: False, 2: True}  # tier-2 is the gated (premium) compute lane
SOLVE_TIMEOUT_S = 20.0

apply()
