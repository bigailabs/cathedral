"""Single-active-challenge first-verified-lock-wins lock.

The launch model is: one active DIMACS CNF challenge, every miner
races the same problem, the first verified private solution to acquire
the lock wins, and later submissions for that challenge cannot replace
the winner.

This module provides the lock primitive that enforces that publisher
ordering rule. The lane verifier scores each submission in isolation;
the lock is the layer that decides which validated submission gets to
flip the active challenge into the LOCKED state.

The interface is small on purpose so an in-memory test fake and a
SQLite-backed production implementation both fit. A future
Supabase-backed adapter is deferred but expected to satisfy the same
interface.

Lock semantics:

* The lock is keyed by ``(family_id, challenge_id)``.
* ``try_lock`` is an atomic compare-and-set: it succeeds for the first
  verified winner that reaches the lock and returns the resulting
  record. Every later call for the same ``challenge_id`` returns
  ``None`` (lock held by someone else) even if its submission was
  independently valid.
* ``status`` and ``get_winner`` are read-only.

The SQLite implementation uses a single UPDATE..WHERE..RETURNING-style
two-step inside a transaction to guarantee atomicity under a single
writer. Multiple processes are not expected on the first launch path;
the publisher is the sole writer to its database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import aiosqlite

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
)


class ChallengeLockError(Exception):
    """Raised on schema or invariant violations."""


@dataclass(frozen=True)
class LockRecord:
    family_id: str
    challenge_id: str
    miner_hotkey: str
    eval_run_id: str
    weighted_score: float
    won_at_iso: str


class ChallengeLock(Protocol):
    async def try_lock(
        self,
        *,
        family_id: str,
        challenge_id: str,
        miner_hotkey: str,
        eval_run_id: str,
        weighted_score: float,
        won_at_iso: str,
        commit: bool = True,
    ) -> LockRecord | None:
        """Attempt to claim the lock. Returns the winning record if this
        call won; returns ``None`` if a winner was already recorded."""
        ...

    async def get_winner(self, *, family_id: str, challenge_id: str) -> LockRecord | None: ...


# --------------------------------------------------------------------------
# In-memory implementation (tests + dry-runs)
# --------------------------------------------------------------------------


class InMemoryChallengeLock:
    """Process-local fake. Suitable for tests of the lane verifier and
    of the launch loop's first-valid-wins behavior."""

    def __init__(self) -> None:
        self._winners: dict[tuple[str, str], LockRecord] = {}

    async def try_lock(
        self,
        *,
        family_id: str,
        challenge_id: str,
        miner_hotkey: str,
        eval_run_id: str,
        weighted_score: float,
        won_at_iso: str,
        commit: bool = True,
    ) -> LockRecord | None:
        key = (family_id, challenge_id)
        if key in self._winners:
            return None
        record = LockRecord(
            family_id=family_id,
            challenge_id=challenge_id,
            miner_hotkey=miner_hotkey,
            eval_run_id=eval_run_id,
            weighted_score=float(weighted_score),
            won_at_iso=won_at_iso,
        )
        self._winners[key] = record
        return record

    async def get_winner(self, *, family_id: str, challenge_id: str) -> LockRecord | None:
        return self._winners.get((family_id, challenge_id))


# --------------------------------------------------------------------------
# SQLite implementation
# --------------------------------------------------------------------------


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_challenge_winners (
    family_id       TEXT NOT NULL,
    challenge_id    TEXT NOT NULL,
    miner_hotkey    TEXT NOT NULL,
    eval_run_id     TEXT NOT NULL,
    weighted_score  REAL NOT NULL,
    won_at_iso      TEXT NOT NULL,
    PRIMARY KEY (family_id, challenge_id)
);
CREATE INDEX IF NOT EXISTS idx_lane_challenge_winners_family
    ON lane_challenge_winners(family_id);
"""


async def init_sqlite_challenge_lock(database_path: str) -> aiosqlite.Connection:
    """Open the publisher-side lock SQLite and apply schema. Same WAL
    pattern as the rest of the publisher. Caller owns the connection
    lifecycle."""
    conn = await aiosqlite.connect(database_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SQLITE_SCHEMA)
    await conn.commit()
    return conn


class SqliteChallengeLock:
    """SQLite-backed first-valid-wins lock.

    Atomicity comes from the PRIMARY KEY UNIQUE constraint plus
    ``INSERT OR IGNORE``: the first inserter wins, every later inserter
    is ignored and the row read-back reveals who actually owns the lock.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def try_lock(
        self,
        *,
        family_id: str,
        challenge_id: str,
        miner_hotkey: str,
        eval_run_id: str,
        weighted_score: float,
        won_at_iso: str,
        commit: bool = True,
    ) -> LockRecord | None:
        # INSERT OR IGNORE makes this a CAS: only one writer ever sees a
        # changed-rows count of 1. Reading the row back tells us whether
        # we were that writer.
        await self._conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_winners ("
            "family_id, challenge_id, miner_hotkey, eval_run_id, "
            "weighted_score, won_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
            (
                family_id,
                challenge_id,
                miner_hotkey,
                eval_run_id,
                float(weighted_score),
                won_at_iso,
            ),
        )
        if commit:
            await self._conn.commit()
        winner = await self.get_winner(family_id=family_id, challenge_id=challenge_id)
        if winner is None:
            raise ChallengeLockError("lock row missing immediately after INSERT OR IGNORE")
        if winner.eval_run_id == eval_run_id and winner.miner_hotkey == miner_hotkey:
            return winner
        return None

    async def get_winner(self, *, family_id: str, challenge_id: str) -> LockRecord | None:
        cur = await self._conn.execute(
            "SELECT family_id, challenge_id, miner_hotkey, eval_run_id, "
            "weighted_score, won_at_iso FROM lane_challenge_winners "
            "WHERE family_id = ? AND challenge_id = ?",
            (family_id, challenge_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return LockRecord(
            family_id=str(row[0]),
            challenge_id=str(row[1]),
            miner_hotkey=str(row[2]),
            eval_run_id=str(row[3]),
            weighted_score=float(row[4]),
            won_at_iso=str(row[5]),
        )


# --------------------------------------------------------------------------
# Source-and-lock helper: flips the active challenge to LOCKED if needed
# --------------------------------------------------------------------------


async def mark_source_locked_if_needed(
    *,
    source_conn: aiosqlite.Connection,
    family_id: str,
    challenge_id: str,
    now_iso: str,
) -> None:
    """Best-effort flip of an ACTIVE challenge into LOCKED status.

    This is idempotent: re-running on an already-LOCKED row is a no-op.
    It is intentionally separate from :meth:`SqliteChallengeLock.try_lock`
    so a deployment can keep the lock and the source in different
    SQLite files (e.g. one publisher-local, one fed by a future
    Supabase mirror).
    """
    await source_conn.execute(
        "UPDATE lane_challenges SET status = ?, updated_at_iso = ? "
        "WHERE family_id = ? AND challenge_id = ? AND status = ?",
        (
            CHALLENGE_STATUS_LOCKED,
            now_iso,
            family_id,
            challenge_id,
            CHALLENGE_STATUS_ACTIVE,
        ),
    )
    await source_conn.commit()


def serialize_audit(record: LockRecord) -> str:
    return json.dumps(
        {
            "family_id": record.family_id,
            "challenge_id": record.challenge_id,
            "miner_hotkey": record.miner_hotkey,
            "eval_run_id": record.eval_run_id,
            "weighted_score": record.weighted_score,
            "won_at_iso": record.won_at_iso,
        },
        sort_keys=True,
    )


__all__ = [
    "SQLITE_SCHEMA",
    "ChallengeLock",
    "ChallengeLockError",
    "InMemoryChallengeLock",
    "LockRecord",
    "SqliteChallengeLock",
    "init_sqlite_challenge_lock",
    "mark_source_locked_if_needed",
    "serialize_audit",
]
