from datetime import UTC, datetime

import pytest

from cathedral.chain.client import Metagraph, MinerNode, WeightStatus
from cathedral.chain.mock import MockChain
from cathedral.validator import weight_loop
from cathedral.validator.db import connect
from cathedral.validator.health import Health
from cathedral.validator.pull_loop import upsert_pulled_eval


def test_v3_bug_isolation_weight_env_overrides_config(monkeypatch) -> None:
    monkeypatch.setenv("CATHEDRAL_V3_BUG_ISOLATION_WEIGHT", "0.05")
    assert weight_loop._resolve_v3_bug_isolation_weight(0.0) == pytest.approx(0.05)


def test_v3_bug_isolation_weight_invalid_env_keeps_config(monkeypatch) -> None:
    monkeypatch.setenv("CATHEDRAL_V3_BUG_ISOLATION_WEIGHT", "not-a-number")
    assert weight_loop._resolve_v3_bug_isolation_weight(0.01) == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_disabled_weight_loop_computes_without_submitting(tmp_path, monkeypatch) -> None:
    conn = await connect(str(tmp_path / "validator.db"))
    now = datetime.now(UTC).isoformat()
    await upsert_pulled_eval(
        conn,
        eval_run={"id": "run-1", "weighted_score": 0.75, "ran_at": now},
        miner_hotkey="hotkey-1",
    )

    chain = MockChain(
        Metagraph(
            block=7,
            miners=(
                MinerNode(uid=0, hotkey="burn-hotkey", last_update_block=1),
                MinerNode(uid=42, hotkey="hotkey-1", last_update_block=1),
            ),
        )
    )
    health = Health()
    info_events: list[tuple[str, dict]] = []

    class FakeLogger:
        def info(self, event: str, **fields: object) -> None:
            info_events.append((event, fields))

        def warning(self, event: str, **fields: object) -> None:
            pass

        def debug(self, event: str, **fields: object) -> None:
            pass

    monkeypatch.setattr(weight_loop, "logger", FakeLogger())

    stop = weight_loop.asyncio.Event()
    task = weight_loop.asyncio.create_task(
        weight_loop.run_weight_loop(
            conn,
            chain,
            health,
            interval_secs=60,
            disabled=True,
            burn_uid=0,
            forced_burn_percentage=98.0,
            stop=stop,
        )
    )
    try:
        for _ in range(50):
            snapshot = await health.get()
            if snapshot.last_weight_set_at is not None:
                break
            await weight_loop.asyncio.sleep(0.02)
        else:
            raise AssertionError("disabled weight loop did not complete one dry-run tick")

        snapshot = await health.get()
        assert snapshot.weight_status is WeightStatus.DISABLED
        assert snapshot.current_block == 7
        assert chain.last_weights == []
        assert (
            "weights_pre_burn",
            {
                "total_hotkeys": 1,
                "mapped_hotkeys": 1,
                "positive_hotkeys": 1,
                "unmapped_count": 0,
                "unmapped_sample": [],
                "positive_sample": [(42, 0.75)],
            },
        ) in info_events
        assert any(
            event == "weights_set"
            and fields["status"] == WeightStatus.DISABLED.value
            and fields["uids"] == [42, 0]
            for event, fields in info_events
        )
    finally:
        stop.set()
        await weight_loop.asyncio.wait_for(task, timeout=1)
        await conn.close()


@pytest.mark.asyncio
async def test_weight_loop_waits_for_backfill_event_before_first_tick(
    tmp_path, monkeypatch
) -> None:
    """run_weight_loop must NOT publish weights before pull_loop signals
    that its first 7-day backfill drained successfully.

    A freshly-upgraded validator with recent rows in pulled_eval_runs
    would otherwise publish a vector computed from a half-hydrated
    window during the seconds the pull loop is still walking the older
    end of the backfill.
    """
    conn = await connect(str(tmp_path / "validator.db"))
    chain = MockChain(
        Metagraph(
            block=1,
            miners=(MinerNode(uid=0, hotkey="burn-hotkey", last_update_block=1),),
        )
    )
    health = Health()
    backfill_event = weight_loop.asyncio.Event()
    stop = weight_loop.asyncio.Event()

    task = weight_loop.asyncio.create_task(
        weight_loop.run_weight_loop(
            conn,
            chain,
            health,
            interval_secs=60,
            disabled=True,
            burn_uid=0,
            forced_burn_percentage=98.0,
            stop=stop,
            initial_backfill_complete=backfill_event,
            initial_backfill_timeout_secs=5.0,
        )
    )
    try:
        # Give the loop a generous slice to do work — it should be
        # blocked on the event and produce zero `last_weight_set_at`.
        await weight_loop.asyncio.sleep(0.3)
        snapshot = await health.get()
        assert snapshot.last_weight_set_at is None, (
            "weight loop must NOT have published before initial_backfill_complete is set"
        )

        # Signal backfill complete; loop should now run a tick.
        backfill_event.set()
        for _ in range(50):
            snapshot = await health.get()
            if snapshot.last_weight_set_at is not None:
                break
            await weight_loop.asyncio.sleep(0.02)
        else:
            raise AssertionError("weight loop did not run a tick after backfill_event was set")
    finally:
        stop.set()
        await weight_loop.asyncio.wait_for(task, timeout=2)
        await conn.close()


@pytest.mark.asyncio
async def test_weight_loop_falls_through_after_backfill_timeout(tmp_path, monkeypatch) -> None:
    """If the backfill event never fires (broken pull loop), the weight
    loop must fall through after its timeout rather than hang forever.

    Better to publish a possibly-thin vector than to publish no vector
    at all — operators can fix the pull loop separately.
    """
    conn = await connect(str(tmp_path / "validator.db"))
    chain = MockChain(
        Metagraph(
            block=1,
            miners=(MinerNode(uid=0, hotkey="burn-hotkey", last_update_block=1),),
        )
    )
    health = Health()
    backfill_event = weight_loop.asyncio.Event()  # never set
    stop = weight_loop.asyncio.Event()

    task = weight_loop.asyncio.create_task(
        weight_loop.run_weight_loop(
            conn,
            chain,
            health,
            interval_secs=60,
            disabled=True,
            burn_uid=0,
            forced_burn_percentage=98.0,
            stop=stop,
            initial_backfill_complete=backfill_event,
            initial_backfill_timeout_secs=0.1,  # tight timeout for the test
        )
    )
    try:
        for _ in range(100):
            snapshot = await health.get()
            if snapshot.last_weight_set_at is not None:
                break
            await weight_loop.asyncio.sleep(0.02)
        else:
            raise AssertionError("weight loop should fall through after backfill timeout")
    finally:
        stop.set()
        await weight_loop.asyncio.wait_for(task, timeout=2)
        await conn.close()
