"""Scoring policy + open-window solve claims — publisher-side, validator-free.

Live validators (Path A) pull our signed rows and aggregate per-hotkey over a
7-day window IN VALIDATOR CODE. We cannot change that window without a
validator release — but we control every row VALUE they average. This module
is therefore the entire "miners who solve more earn more" mechanism:

  * submit mode `open_window` (default — matches live since 2026-06-04):
    a challenge accepts one solve per distinct hotkey while active; each solve
    gets its true first-seen rank. `lock_wins` preserves the legacy
    winner-take-all for tests/back-compat.
  * scoring policy `flat` (default — byte-faithful with live, weighted_score
    1.0) or `coverage`: weighted_score = the miner's distinct-challenge
    coverage over a trailing window, clamped to [floor, 1.0]. More solves →
    higher row values → higher validator 7-day mean → more weight. A miner
    who slows down emits lower-valued rows and decays; one who stops keeps a
    frozen tail bounded by the validators' 7-day window (STRATEGY.md).

Anti-arms-race by construction: a hotkey can solve each board challenge at
most once, so max coverage = challenges available in the window — there is a
hard ceiling, not an open-ended volume race. Sybil exposure (k hotkeys → k×)
is identical to today's flat 1.0; no regression.

Policy is env-gated so the v4 cutover ships byte-faithful (flat) and the
policy flips AFTER the swap, per deploy/RUNBOOK.md abort criteria.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .store import Store

# -- env knobs ---------------------------------------------------------------

SUBMIT_MODE_ENV = "CATHEDRAL_SUBMIT_MODE"            # open_window | lock_wins
SCORING_POLICY_ENV = "CATHEDRAL_SCORING_POLICY"      # flat | coverage
COVERAGE_WINDOW_HOURS_ENV = "CATHEDRAL_SCORING_COVERAGE_WINDOW_HOURS"
COVERAGE_FLOOR_ENV = "CATHEDRAL_SCORING_COVERAGE_FLOOR"


def submit_mode() -> str:
    mode = os.environ.get(SUBMIT_MODE_ENV, "open_window").strip().lower()
    return mode if mode in ("open_window", "lock_wins") else "open_window"


def scoring_policy() -> str:
    pol = os.environ.get(SCORING_POLICY_ENV, "flat").strip().lower()
    return pol if pol in ("flat", "coverage") else "flat"


def coverage_window_hours() -> float:
    try:
        return float(os.environ.get(COVERAGE_WINDOW_HOURS_ENV, "24"))
    except ValueError:
        return 24.0


def coverage_floor() -> float:
    """Every accepted solve is worth at least this much (a correct solve is
    never zero — correctness still pays; coverage scales it up)."""
    try:
        return min(1.0, max(0.0, float(os.environ.get(COVERAGE_FLOOR_ENV, "0.1"))))
    except ValueError:
        return 0.1


# -- open-window claim --------------------------------------------------------

def claim_solve(conn, challenge_id: str, miner_hotkey: str, now_iso: str) -> int | None:
    """Atomically claim a distinct (challenge, hotkey) solve inside the caller's
    write transaction. Returns the first-seen solve rank (1-based), or None if
    this hotkey already solved this challenge (idempotent dedup — the same
    property production's PAR-2 relies on)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
        "VALUES (?, ?, ?)", (challenge_id, miner_hotkey, now_iso))
    if not cur.rowcount:
        return None
    n = conn.execute(
        "SELECT COUNT(*) FROM lane_challenge_solves WHERE challenge_id=?",
        (challenge_id,)).fetchone()[0]
    return int(n)


# -- coverage policy ----------------------------------------------------------

def _iso_before(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def coverage_score(store: Store, miner_hotkey: str) -> float:
    """weighted_score = (distinct challenges this hotkey solved in the trailing
    window, INCLUDING the solve being scored) / (challenges created in the same
    window), clamped to [floor, 1.0].

    Solve everything the board offered → 1.0. Solve half → ~0.5. The
    denominator is what was actually mintable supply, so the score is a real
    work share, not an unbounded count. Falls back to 1.0 when the window has
    no minted challenges (fresh deploys, seeded-only stores)."""
    since = _iso_before(coverage_window_hours())
    solved = store.query(
        "SELECT COUNT(DISTINCT challenge_id) AS n FROM lane_challenge_solves "
        "WHERE miner_hotkey=? AND solved_at_iso > ?", (miner_hotkey, since))[0]["n"]
    available = store.query(
        "SELECT COUNT(*) AS n FROM lane_challenges WHERE created_at_iso > ?",
        (since,))[0]["n"]
    if available <= 0:
        return 1.0
    return min(1.0, max(coverage_floor(), solved / available))


def weighted_score_for(store: Store, miner_hotkey: str) -> float:
    """The row value the validators will average — policy-dispatched."""
    if scoring_policy() == "coverage":
        return round(coverage_score(store, miner_hotkey), 6)
    return 1.0
