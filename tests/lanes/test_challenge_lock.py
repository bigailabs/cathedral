"""Tests for the publisher-side single-active-challenge final lock.

Covers the compare-and-set semantics for both the in-memory fake and
the SQLite-backed implementation. Receipt ordering chooses the winner;
the lock guarantees one and only one selected receipt can finalize a
given active challenge.
"""

from __future__ import annotations

from cathedral.lanes.challenge_lock import (
    InMemoryChallengeLock,
    SqliteChallengeLock,
    init_sqlite_challenge_lock,
    mark_source_locked_if_needed,
    serialize_audit,
)
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    SqliteChallengeSource,
    init_sqlite_challenge_source,
)

_FAMILY = "synthetic_boolean_v1"
_CHALLENGE = "toy-challenge-01"


# --------------------------------------------------------------------------
# In-memory
# --------------------------------------------------------------------------


async def test_in_memory_first_caller_wins() -> None:
    lock = InMemoryChallengeLock()
    first = await lock.try_lock(
        family_id=_FAMILY,
        challenge_id=_CHALLENGE,
        miner_hotkey="5MinerA",
        eval_run_id="run-A",
        weighted_score=1.0,
        won_at_iso="2026-05-19T00:00:00.000Z",
    )
    assert first is not None
    assert first.miner_hotkey == "5MinerA"
    assert first.eval_run_id == "run-A"

    second = await lock.try_lock(
        family_id=_FAMILY,
        challenge_id=_CHALLENGE,
        miner_hotkey="5MinerB",
        eval_run_id="run-B",
        weighted_score=1.0,
        won_at_iso="2026-05-19T00:01:00.000Z",
    )
    assert second is None

    winner = await lock.get_winner(family_id=_FAMILY, challenge_id=_CHALLENGE)
    assert winner is not None
    assert winner.miner_hotkey == "5MinerA"


async def test_in_memory_independent_challenges_lock_independently() -> None:
    lock = InMemoryChallengeLock()
    first = await lock.try_lock(
        family_id=_FAMILY,
        challenge_id="challenge-1",
        miner_hotkey="5MinerA",
        eval_run_id="run-A",
        weighted_score=1.0,
        won_at_iso="2026-05-19T00:00:00.000Z",
    )
    second = await lock.try_lock(
        family_id=_FAMILY,
        challenge_id="challenge-2",
        miner_hotkey="5MinerB",
        eval_run_id="run-B",
        weighted_score=1.0,
        won_at_iso="2026-05-19T00:00:01.000Z",
    )
    assert first is not None
    assert second is not None
    assert first.miner_hotkey == "5MinerA"
    assert second.miner_hotkey == "5MinerB"


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


async def test_sqlite_first_selected_winner_locks(tmp_path) -> None:
    conn = await init_sqlite_challenge_lock(str(tmp_path / "locks.db"))
    try:
        lock = SqliteChallengeLock(conn)
        first = await lock.try_lock(
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            miner_hotkey="5MinerA",
            eval_run_id="run-A",
            weighted_score=1.0,
            won_at_iso="2026-05-19T00:00:00.000Z",
        )
        assert first is not None
        assert first.miner_hotkey == "5MinerA"

        second = await lock.try_lock(
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            miner_hotkey="5MinerB",
            eval_run_id="run-B",
            weighted_score=1.0,
            won_at_iso="2026-05-19T00:05:00.000Z",
        )
        assert second is None

        # Winner stays the first selected receipt even if a later try
        # carries a higher (or equal) weighted_score: the final lock is
        # not best-of-all.
        third = await lock.try_lock(
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            miner_hotkey="5MinerC",
            eval_run_id="run-C",
            weighted_score=1.0,
            won_at_iso="2026-05-19T00:10:00.000Z",
        )
        assert third is None

        winner = await lock.get_winner(family_id=_FAMILY, challenge_id=_CHALLENGE)
        assert winner is not None
        assert winner.miner_hotkey == "5MinerA"
        assert winner.eval_run_id == "run-A"
    finally:
        await conn.close()


async def test_sqlite_lock_persists_across_reopen(tmp_path) -> None:
    db = str(tmp_path / "locks.db")
    conn = await init_sqlite_challenge_lock(db)
    try:
        lock = SqliteChallengeLock(conn)
        await lock.try_lock(
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            miner_hotkey="5MinerA",
            eval_run_id="run-A",
            weighted_score=1.0,
            won_at_iso="2026-05-19T00:00:00.000Z",
        )
    finally:
        await conn.close()

    reopened = await init_sqlite_challenge_lock(db)
    try:
        lock2 = SqliteChallengeLock(reopened)
        winner = await lock2.get_winner(family_id=_FAMILY, challenge_id=_CHALLENGE)
        assert winner is not None
        assert winner.miner_hotkey == "5MinerA"

        retry = await lock2.try_lock(
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            miner_hotkey="5MinerB",
            eval_run_id="run-B",
            weighted_score=1.0,
            won_at_iso="2026-05-19T01:00:00.000Z",
        )
        assert retry is None
    finally:
        await reopened.close()


async def test_mark_source_locked_flips_active_row(tmp_path) -> None:
    src_conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(src_conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id=_CHALLENGE,
                family_id=_FAMILY,
                tier=1,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={},
            )
        )
        await mark_source_locked_if_needed(
            source_conn=src_conn,
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            now_iso="2026-05-19T00:00:01.000Z",
        )
        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_LOCKED

        # Re-running on an already-locked row is a no-op.
        await mark_source_locked_if_needed(
            source_conn=src_conn,
            family_id=_FAMILY,
            challenge_id=_CHALLENGE,
            now_iso="2026-05-19T00:00:02.000Z",
        )
        rows_again = await src.list_for_family(_FAMILY)
        assert rows_again[0].status == CHALLENGE_STATUS_LOCKED
    finally:
        await src_conn.close()


async def test_sqlite_lock_and_promote_next_pending_challenge(tmp_path) -> None:
    src_conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(src_conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="challenge-1",
                family_id=_FAMILY,
                tier=1,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={},
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="challenge-2",
                family_id=_FAMILY,
                tier=1,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={},
            ),
            now_iso="2026-05-19T00:00:01.000Z",
        )

        promoted = await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id="challenge-1",
            now_iso="2026-05-19T00:00:02.000Z",
        )
        assert promoted is not None
        assert promoted.challenge_id == "challenge-2"
        assert promoted.status == CHALLENGE_STATUS_ACTIVE

        rows = await src.list_for_family(_FAMILY)
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("challenge-1", CHALLENGE_STATUS_LOCKED),
            ("challenge-2", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await src_conn.close()


async def test_sqlite_lock_can_promote_next_pending_challenge_within_tier(tmp_path) -> None:
    src_conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(src_conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="tier-1-active",
                family_id=_FAMILY,
                tier=1,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={},
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="tier-3-active",
                family_id=_FAMILY,
                tier=3,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={},
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="tier-3-next",
                family_id=_FAMILY,
                tier=3,
                cnf_text="p cnf 1 1\n-1 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={},
            ),
            now_iso="2026-05-19T00:00:01.000Z",
        )

        promoted = await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id="tier-3-active",
            now_iso="2026-05-19T00:00:02.000Z",
            active_scope="tier",
        )

        assert promoted is not None
        assert promoted.challenge_id == "tier-3-next"
        rows = await src.list_for_family(_FAMILY)
        assert {row.challenge_id: row.status for row in rows} == {
            "tier-1-active": CHALLENGE_STATUS_ACTIVE,
            "tier-3-active": CHALLENGE_STATUS_LOCKED,
            "tier-3-next": CHALLENGE_STATUS_ACTIVE,
        }
    finally:
        await src_conn.close()


def test_serialize_audit_is_stable() -> None:
    from cathedral.lanes.challenge_lock import LockRecord

    record = LockRecord(
        family_id=_FAMILY,
        challenge_id=_CHALLENGE,
        miner_hotkey="5MinerA",
        eval_run_id="run-A",
        weighted_score=1.0,
        won_at_iso="2026-05-19T00:00:00.000Z",
    )
    assert serialize_audit(record) == serialize_audit(record)
