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
import time
from datetime import datetime, timedelta, timezone

from .store import Store

# In-process cache for the coverage denominator (the max distinct-challenges
# solved by any single miner in the trailing window). Recomputing a
# GROUP-BY-MAX over the solves table on EVERY submit hammers the single
# SQLite connection (the bottleneck that wedged prod), so we memoize it and
# recompute at most every _COVERAGE_DENOM_TTL_SECS. Module-level so it is
# shared across requests in the single-process publisher.
_COVERAGE_DENOM_TTL_SECS = 60.0
_coverage_denom_cache: dict[str, tuple[float, int]] = {}  # window_key -> (computed_at, denom)

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


def coverage_denominator(store: Store, *, now: float | None = None) -> int:
    """The relative-to-top denominator: the MAX distinct-challenges solved by any
    single miner in the trailing window. The top solver therefore scores ≈1.0 and
    everyone else scales proportionally — independent of how many challenges the
    publisher minted (in prod ~92% expire untouched, so a mint-based denominator
    floored EVERYONE at the coverage floor and gave zero differentiation).

    Cached in-process for ≥_COVERAGE_DENOM_TTL_SECS: this is a GROUP-BY-MAX over
    the whole solves table, far too expensive to run on every submit against the
    one SQLite connection. `now` is injectable for tests; defaults to time.time().
    """
    now = time.time() if now is None else now
    window = coverage_window_hours()
    key = f"{window}"
    cached = _coverage_denom_cache.get(key)
    if cached is not None and (now - cached[0]) < _COVERAGE_DENOM_TTL_SECS:
        return cached[1]
    since = _iso_before(window)
    # max distinct challenges any single hotkey solved in the window.
    row = store.query(
        "SELECT MAX(c) AS m FROM ("
        "  SELECT COUNT(DISTINCT challenge_id) AS c FROM lane_challenge_solves "
        "  WHERE solved_at_iso > ? GROUP BY miner_hotkey)",
        (since,))
    denom = int(row[0]["m"] or 0)
    _coverage_denom_cache[key] = (now, denom)
    return denom


def _reset_coverage_denom_cache() -> None:
    """Test hook: drop the memoized denominator so a freshly-seeded store
    recomputes instead of serving a stale value."""
    _coverage_denom_cache.clear()


def coverage_score(store: Store, miner_hotkey: str, *, now: float | None = None) -> float:
    """weighted_score = (distinct challenges this hotkey solved in the trailing
    window, INCLUDING the solve being scored) / (max distinct challenges solved
    by ANY single miner in the same window), clamped to [floor, 1.0].

    Relative-to-top, NOT relative-to-mint: the busiest solver ≈ 1.0, a
    half-as-active one ≈ 0.5, independent of mint rate. In prod the publisher
    mints far more challenges than any miner can solve (92% expire untouched), so
    the old `solved / challenges_minted` denominator floored EVERYONE at the
    coverage floor and erased all differentiation — this fixes that.

    Falls back to 1.0 when no miner has solved anything in the window (fresh
    deploys, seeded-only stores) — a lone first solver is the top, score 1.0."""
    since = _iso_before(coverage_window_hours())
    solved = store.query(
        "SELECT COUNT(DISTINCT challenge_id) AS n FROM lane_challenge_solves "
        "WHERE miner_hotkey=? AND solved_at_iso > ?", (miner_hotkey, since))[0]["n"]
    denom = coverage_denominator(store, now=now)
    if denom <= 0:
        # No solves recorded in-window at all: the scoring miner (if any) is the
        # top by definition. An idle hotkey with 0 solves still floors below.
        return 1.0 if solved > 0 else coverage_floor()
    return min(1.0, max(coverage_floor(), solved / denom))


def weighted_score_for(store: Store, miner_hotkey: str) -> float:
    """The row value the validators will average — policy-dispatched."""
    if scoring_policy() == "coverage":
        return round(coverage_score(store, miner_hotkey), 6)
    return 1.0
