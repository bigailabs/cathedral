"""Tests for the SAT active-board fill loop (publisher/sat_fill.py).

The loop promotes pending->active up to a per-tier target so the board
holds N concurrent challenges (the "flood"). Runs against a real SQLite
challenge source so the tier_multi promotion path is exercised.
"""

from __future__ import annotations

import dataclasses
import os
from unittest import mock

import pytest

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    CHALLENGE_STATUS_RETIRED,
    ChallengeRecord,
    SqliteChallengeSource,
    init_sqlite_challenge_source,
)
from cathedral.publisher import repository as repo
from cathedral.publisher import sat_fill
from cathedral.publisher.app import AsyncDbWriteLock

# Real write gate (wraps a threading.Lock); reusable across `async with`.
_LOCK = AsyncDbWriteLock()

_FAMILY = "synthetic_boolean_v1"
_NOW = "2026-05-31T00:00:00.000Z"


def _pending(challenge_id: str, *, tier: int, idx: int) -> ChallengeRecord:
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=tier,
        cnf_text=f"p cnf 1 1\n{idx + 1} 0\n",
        status=CHALLENGE_STATUS_PENDING,
        audit_metadata={"kind": "random_3sat"},
    )


def _active(challenge_id: str, *, tier: int, idx: int) -> ChallengeRecord:
    return dataclasses.replace(
        _pending(challenge_id, tier=tier, idx=idx),
        status=CHALLENGE_STATUS_ACTIVE,
    )


async def _seed_pending(source, ids: list[str], *, tier: int) -> None:
    for i, cid in enumerate(ids):
        await source.upsert(_pending(cid, tier=tier, idx=i))


async def _active_count(source, *, tier: int) -> int:
    return sum(1 for rec in await source.list_active(_FAMILY) if rec.tier == tier)


async def _status(source, challenge_id: str) -> str:
    cur = await source._conn.execute(
        "SELECT status FROM lane_challenges WHERE challenge_id = ?",
        (challenge_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def test_fill_promotes_up_to_target(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _seed_pending(src, [f"t1-{i}" for i in range(5)], tier=1)

        cfg = sat_fill.FillConfig(tiers=(1,), default_target=3, target_overrides={})
        summary = await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)

        assert summary["promoted"] == 3
        assert await _active_count(src, tier=1) == 3
    finally:
        await conn.close()


async def test_fill_is_idempotent_at_target(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _seed_pending(src, [f"t1-{i}" for i in range(5)], tier=1)
        cfg = sat_fill.FillConfig(tiers=(1,), default_target=3, target_overrides={})

        await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)
        # Second tick: already at target, nothing more to promote.
        summary2 = await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)

        assert summary2["promoted"] == 0
        assert await _active_count(src, tier=1) == 3
    finally:
        await conn.close()


async def test_fill_refills_after_a_slot_drains(tmp_path) -> None:
    """A won/locked challenge frees a slot; the next tick refills it."""
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _seed_pending(src, [f"t1-{i}" for i in range(5)], tier=1)
        cfg = sat_fill.FillConfig(tiers=(1,), default_target=3, target_overrides={})

        await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)
        assert await _active_count(src, tier=1) == 3

        # Simulate a winner-take-all lock: flip one active row to locked.
        actives = [r for r in await src.list_active(_FAMILY) if r.tier == 1]
        locked = dataclasses.replace(actives[0], status=CHALLENGE_STATUS_LOCKED)
        await src.upsert(locked, overwrite_status=True)
        assert await _active_count(src, tier=1) == 2

        summary = await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)
        assert summary["promoted"] == 1
        assert await _active_count(src, tier=1) == 3
    finally:
        await conn.close()


async def test_open_window_retirement_ages_out_active_then_refills(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await src.upsert(
            _active("old-active", tier=1, idx=0),
            now_iso="2026-05-30T22:59:59.000Z",
            overwrite_status=True,
        )
        await _seed_pending(src, ["fresh-1", "fresh-2"], tier=1)
        cfg = sat_fill.FillConfig(tiers=(1,), default_target=1, target_overrides={})

        with mock.patch.dict(os.environ, {"CATHEDRAL_OPEN_WINDOW_ENABLED": "true"}):
            with mock.patch.object(sat_fill, "_now_iso", return_value=_NOW):
                summary = await sat_fill.run_one_tick(
                    source=src,
                    config=cfg,
                    db_write_lock=_LOCK,
                )

        assert summary["retired"] == 1
        assert summary["promoted"] == 1
        assert await _status(src, "old-active") == CHALLENGE_STATUS_RETIRED
        assert await _active_count(src, tier=1) == 1
    finally:
        await conn.close()


async def test_open_window_retirement_saturates_by_distinct_solvers(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        await conn.executescript(repo.LANE_CHALLENGE_SOLVES_SCHEMA)
        await conn.commit()
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await src.upsert(
            _active("saturated-active", tier=1, idx=0),
            now_iso="2026-05-30T23:30:00.000Z",
            overwrite_status=True,
        )
        await _seed_pending(src, ["fresh-1", "fresh-2"], tier=1)
        await conn.executemany(
            """
            INSERT INTO lane_challenge_solves (
                family_id, challenge_id, miner_hotkey, eval_run_id,
                solve_rank, weighted_score, solved_at_iso
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    _FAMILY,
                    "saturated-active",
                    f"5Solver{i:02d}",
                    f"eval-{i:02d}",
                    i + 1,
                    1.0,
                    _NOW,
                )
                for i in range(64)
            ],
        )
        await conn.commit()
        cfg = sat_fill.FillConfig(tiers=(1,), default_target=1, target_overrides={})

        with mock.patch.dict(os.environ, {"CATHEDRAL_OPEN_WINDOW_ENABLED": "true"}):
            with mock.patch.object(sat_fill, "_now_iso", return_value=_NOW):
                summary = await sat_fill.run_one_tick(
                    source=src,
                    config=cfg,
                    db_write_lock=_LOCK,
                )

        assert summary["retired"] == 1
        assert summary["promoted"] == 1
        assert await _status(src, "saturated-active") == CHALLENGE_STATUS_RETIRED
        assert await _active_count(src, tier=1) == 1
    finally:
        await conn.close()


async def test_fill_loop_forwards_lock_and_promotes(tmp_path) -> None:
    """Drive run_fill_loop itself (not just run_one_tick) for one tick.

    Guards the wiring bug where the loop's run_one_tick call could drop the
    required db_write_lock — which would crash every tick and promote nothing.
    """
    import asyncio

    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _seed_pending(src, [f"t1-{i}" for i in range(5)], tier=1)
        cfg = sat_fill.FillConfig(
            tiers=(1,), default_target=3, target_overrides={}, interval_seconds=10
        )
        stop = asyncio.Event()
        task = asyncio.create_task(
            sat_fill.run_fill_loop(
                source=src, config=cfg, stop=stop, db_write_lock=_LOCK
            )
        )
        # First tick runs before the interval wait; give it a moment, then stop.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if await _active_count(src, tier=1) == 3:
                break
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert await _active_count(src, tier=1) == 3
    finally:
        await conn.close()


async def test_fill_per_tier_targets(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "c.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _seed_pending(src, [f"t1-{i}" for i in range(4)], tier=1)
        await _seed_pending(src, [f"t2-{i}" for i in range(4)], tier=2)

        cfg = sat_fill.FillConfig(
            tiers=(1, 2), default_target=1, target_overrides={2: 3}
        )
        await sat_fill.run_one_tick(source=src, config=cfg, db_write_lock=_LOCK)

        assert await _active_count(src, tier=1) == 1
        assert await _active_count(src, tier=2) == 3
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_config_from_env_defaults() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        cfg = sat_fill.config_from_env()
    assert cfg.interval_seconds == 60
    assert cfg.tiers == (1, 2, 3)
    assert cfg.default_target == 1


def test_config_from_env_overrides() -> None:
    env = {
        "CATHEDRAL_SAT_FILL_INTERVAL_SECONDS": "30",
        "CATHEDRAL_SAT_FILL_TIERS": "1,2",
        "CATHEDRAL_SAT_ACTIVE_PER_TIER": "20",
        "CATHEDRAL_SAT_FILL_TARGETS": '{"2": 5}',
    }
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = sat_fill.config_from_env()
    assert cfg.interval_seconds == 30
    assert cfg.tiers == (1, 2)
    assert cfg.default_target == 20
    assert cfg.target_for(1) == 20
    assert cfg.target_for(2) == 5


def test_config_rejects_short_interval() -> None:
    with mock.patch.dict(
        os.environ, {"CATHEDRAL_SAT_FILL_INTERVAL_SECONDS": "5"}, clear=True
    ):
        with pytest.raises(ValueError, match="must be >= 10"):
            sat_fill.config_from_env()


def test_fill_enabled_flag() -> None:
    with mock.patch.dict(os.environ, {"CATHEDRAL_SAT_FILL_ENABLED": "true"}, clear=True):
        assert sat_fill.fill_enabled() is True
    with mock.patch.dict(os.environ, {}, clear=True):
        assert sat_fill.fill_enabled() is False


def test_target_for_tier_shared_resolution() -> None:
    """The win-site refill and the loop must resolve the SAME target/kind."""
    env = {
        "CATHEDRAL_SAT_ACTIVE_PER_TIER": "15",
        "CATHEDRAL_SAT_FILL_TARGETS": '{"3": 4}',
        "CATHEDRAL_SAT_FILL_KIND": "random_3sat",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert sat_fill.target_for_tier(1) == 15  # default
        assert sat_fill.target_for_tier(3) == 4  # per-tier override
        assert sat_fill.fill_kind() == "random_3sat"
        # And config_from_env agrees (same source of truth).
        cfg = sat_fill.config_from_env()
        assert cfg.target_for(1) == 15
        assert cfg.target_for(3) == 4
        assert cfg.kind == "random_3sat"
