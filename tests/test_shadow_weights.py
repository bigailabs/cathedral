from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from cathedral.validator.shadow_weights import (
    compute_shadow_weight_diff,
    record_shadow_weight_diff,
    vector_sha256,
)


def test_vector_sha256_is_order_stable() -> None:
    assert vector_sha256([(2, 0.25), (1, 0.75)]) == vector_sha256(
        {1: 0.75, 2: 0.25}
    )


def test_compute_shadow_weight_diff_flags_blocker() -> None:
    diff = compute_shadow_weight_diff(
        [(1, 0.9), (2, 0.1)],
        [(1, 0.4), (3, 0.6)],
        blocker_abs_diff_sum=0.10,
    )

    assert diff.abs_diff_sum == pytest.approx(1.2)
    assert diff.max_diff == pytest.approx(0.6)
    assert diff.legacy_weight_count == 2
    assert diff.decentralized_weight_count == 2
    assert diff.blocker is True
    assert diff.details["diff_level"] == "blocker"
    assert diff.details["top_diffs"][0] == [3, pytest.approx(0.6)]


def test_compute_shadow_weight_diff_flags_investigate_tier() -> None:
    diff = compute_shadow_weight_diff(
        [(1, 0.50), (2, 0.50)],
        [(1, 0.485), (2, 0.515)],
    )

    assert diff.abs_diff_sum == pytest.approx(0.03)
    assert diff.blocker is False
    assert diff.details["diff_level"] == "investigate"


def test_compute_shadow_weight_diff_flags_clean_tier() -> None:
    diff = compute_shadow_weight_diff(
        [(1, 0.50), (2, 0.50)],
        [(1, 0.497), (2, 0.503)],
    )

    assert diff.abs_diff_sum == pytest.approx(0.006)
    assert diff.blocker is False
    assert diff.details["diff_level"] == "clean"


@pytest.mark.asyncio
async def test_record_shadow_weight_diff(tmp_path) -> None:
    conn = await aiosqlite.connect(tmp_path / "validator.db")
    try:
        diff = compute_shadow_weight_diff([(1, 1.0)], [(1, 0.98), (2, 0.02)])
        row_id = await record_shadow_weight_diff(
            conn,
            validator_hotkey="5validator",
            network="finney",
            netuid=39,
            rules_version_id=7,
            diff=diff,
            computed_at=datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
        )

        cur = await conn.execute(
            "SELECT id, rules_version_id, validator_hotkey, network, netuid, "
            "abs_diff_sum, blocker, computed_at "
            "FROM shadow_weight_diffs"
        )
        row = await cur.fetchone()
        assert row == (
            row_id,
            7,
            "5validator",
            "finney",
            39,
            pytest.approx(0.04),
            0,
            "2026-05-28T12:00:00.000Z",
        )
    finally:
        await conn.close()
