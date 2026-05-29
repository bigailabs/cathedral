"""Tests for the open-window ranked-solve ledger (WS-SCORE.C data layer)."""

from __future__ import annotations

import pytest

from cathedral.publisher import repository as repo
from cathedral.validator.db import connect

FAMILY = "synthetic_boolean_v1"
CHALLENGE = "sat-t1-easy-001"


async def _conn(tmp_path):
    conn = await connect(str(tmp_path / "publisher.db"))
    await conn.executescript(repo.LANE_CHALLENGE_SOLVES_SCHEMA)
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_first_solver_gets_rank_one(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        res = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey="5HotkeyA",
            eval_run_id="run-a",
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        assert res.solve_rank == 1
        assert res.newly_recorded is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_subsequent_distinct_solvers_get_increasing_ranks(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        ranks = []
        for i, hk in enumerate(["5A", "5B", "5C"]):
            res = await repo.record_ranked_solve(
                conn,
                family_id=FAMILY,
                challenge_id=CHALLENGE,
                miner_hotkey=hk,
                eval_run_id=f"run-{i}",
                weighted_score=1.0,
                solved_at_iso="2026-05-29T20:00:00.000Z",
            )
            ranks.append(res.solve_rank)
        assert ranks == [1, 2, 3]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_resubmit_is_idempotent_no_second_row(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        first = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey="5A",
            eval_run_id="run-1",
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        again = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey="5A",
            eval_run_id="run-2",  # different eval run, same hotkey/challenge
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:05:00.000Z",
        )
        assert first.solve_rank == 1 and first.newly_recorded is True
        assert again.solve_rank == 1 and again.newly_recorded is False
        cur = await conn.execute(
            "SELECT COUNT(*) FROM lane_challenge_solves WHERE family_id=? AND challenge_id=?",
            (FAMILY, CHALLENGE),
        )
        assert (await cur.fetchone())[0] == 1  # no second row written
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ranks_are_per_challenge(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id="c1", miner_hotkey="5A",
            eval_run_id="r1", weighted_score=1.0, solved_at_iso="t",
        )
        other = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id="c2", miner_hotkey="5A",
            eval_run_id="r2", weighted_score=1.0, solved_at_iso="t",
        )
        assert other.solve_rank == 1  # independent ranking per challenge
    finally:
        await conn.close()
