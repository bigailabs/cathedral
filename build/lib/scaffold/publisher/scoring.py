"""Open-window solve claims + the flat row value.

Row values are flat 1.0 — the rows are the public AUDIT TRAIL, not the
economics. Miner economics (recency window, proportional pay, multi-lane
composition, burn) live in weights.py, which serves ONE signed final number
per miner at /v1/validator/weights/next. The coverage scoring policy that
briefly lived here (solved/minted ratios) is deleted: it floored everyone at
0.1 in prod and was rejected in STRATEGY.md — differentiation comes from the
arena, not volume ratios on disposable challenges.

  * submit mode `open_window` (default — matches live since 2026-06-04):
    a challenge accepts one solve per distinct hotkey while active; each solve
    gets its true first-seen rank. `lock_wins` preserves the legacy
    winner-take-all for tests/back-compat.

Anti-arms-race by construction: a hotkey can solve each board challenge at
most once, so there is a hard ceiling, not an open-ended volume race.
"""
from __future__ import annotations

import os

from .store import Store

SUBMIT_MODE_ENV = "CATHEDRAL_SUBMIT_MODE"            # open_window | lock_wins


def submit_mode() -> str:
    mode = os.environ.get(SUBMIT_MODE_ENV, "open_window").strip().lower()
    return mode if mode in ("open_window", "lock_wins") else "open_window"


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


def weighted_score_for(store: Store, miner_hotkey: str) -> float:
    """The row value validators see in the audit feed — flat, always. Economics
    are composed in weights.py from the solve ledger, not encoded in rows."""
    return 1.0
