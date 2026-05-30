"""Tests for the validator's shadow PAR-2 aggregation (WS-SCORE.D)."""

from __future__ import annotations

import math

import pytest

from cathedral.validator.db import connect
from cathedral.validator.pull_loop import par2_merit_per_operator, upsert_pulled_eval


async def _put(conn, eid, operator, solve_rank, w_c, task_id_public, epoch_salt="e1"):
    """Insert one schema-6 pulled eval row (operator == miner_hotkey)."""
    await upsert_pulled_eval(
        conn,
        eval_run={
            "id": eid,
            "weighted_score": 1.0,
            "ran_at": "2026-05-29T20:00:00.000Z",
            "task_type": "synthetic_boolean_v1",
            "eval_output_schema_version": 6,
            "solve_rank": solve_rank,
            "challenge_value": w_c,
            "operator": operator,
            "task_id_public": task_id_public,
            "epoch_salt": epoch_salt,
        },
        miner_hotkey=operator,
    )


@pytest.mark.asyncio
async def test_par2_merit_coverage_and_rank(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    try:
        # Challenge A (w_c=10): O1 rank1, O2 rank2.  Challenge B (w_c=10): O1 rank1.
        await _put(conn, "a1", "O1", 1, 10.0, "tA")
        await _put(conn, "a2", "O2", 2, 10.0, "tA")
        await _put(conn, "b1", "O1", 1, 10.0, "tB")

        merit = await par2_merit_per_operator(conn, since_days=3650, alpha=0.5)
        # Budget conserved across the suite: 10 + 10 = 20.
        assert math.isclose(sum(merit.values()), 20.0, rel_tol=1e-9)
        # Coverage + rank: O1 (won both, rank-1 on each) >> O2 (one rank-2).
        assert merit["O1"] > merit["O2"]
        assert math.isclose(merit["O1"], 10.0 + 10.0 * 1.0 / (1.0 + 2 ** -0.5), rel_tol=1e-9)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_coldkey_dedup_defeats_sybil(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    try:
        # One operator splits into two hotkeys grabbing ranks 1 and 2 on the
        # same challenge; both resolve to coldkey C1. Lone honest h9 takes rank 3.
        await _put(conn, "s1", "h1", 1, 10.0, "tA")
        await _put(conn, "s2", "h2", 2, 10.0, "tA")
        await _put(conn, "s3", "h9", 3, 10.0, "tA")

        # Per-hotkey (no resolution): the sybil occupies ranks 1+2.
        per_hotkey = await par2_merit_per_operator(conn, since_days=3650, alpha=0.5)
        assert set(per_hotkey) == {"h1", "h2", "h9"}

        # With coldkey resolution: h1,h2 collapse to C1 at its best rank (1); the
        # budget splits between just C1 and h9 — the extra sybil slot earns nothing.
        resolved = await par2_merit_per_operator(
            conn, since_days=3650, alpha=0.5, coldkey_of={"h1": "C1", "h2": "C1"}
        )
        assert set(resolved) == {"C1", "h9"}
        assert math.isclose(sum(resolved.values()), 10.0, rel_tol=1e-9)
        assert resolved["C1"] > resolved["h9"]
        assert math.isclose(resolved["C1"], 10.0 * 1.0 / (1.0 + 2 ** -0.5), rel_tol=1e-9)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_v5_rows_are_ignored(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    try:
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "v5a",
                "weighted_score": 1.0,
                "ran_at": "2026-05-29T20:00:00.000Z",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": 5,
            },
            miner_hotkey="O1",
        )
        merit = await par2_merit_per_operator(conn, since_days=3650, alpha=0.5)
        assert merit == {}
    finally:
        await conn.close()
