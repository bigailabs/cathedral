from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.publisher import repository
from cathedral.publisher import weight_policy as weight_policy_module
from cathedral.publisher.weight_policy import (
    WeightPolicyProducerConfig,
    WeightPolicyStore,
    latest_policy_scores_by_hotkey,
    produce_weight_policy_once,
    run_weight_policy_producer,
)
from cathedral.validator.db import connect


def _private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex("11" * 32))


@pytest.mark.asyncio
async def test_weight_policy_versions_are_durable_and_monotonic(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await _seed_ranked_submission(conn, "agent-a", "hk-a", current_score=0.72)
        store = WeightPolicyStore()
        config = WeightPolicyProducerConfig(valid_for_secs=3600)
        issued = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)

        first = await produce_weight_policy_once(
            conn,
            store,
            _private_key(),
            config=config,
            issued_at=issued,
        )
        await repository.update_submission_score(
            conn,
            "agent-a",
            current_score=0.73,
            current_rank=1,
        )
        same_millisecond = await produce_weight_policy_once(
            conn,
            store,
            _private_key(),
            config=config,
            issued_at=issued,
        )
        await repository.update_submission_score(
            conn,
            "agent-a",
            current_score=0.74,
            current_rank=1,
        )
        clock_rollback = await produce_weight_policy_once(
            conn,
            store,
            _private_key(),
            config=config,
            issued_at=issued - timedelta(days=1),
        )

        assert same_millisecond.policy_version == first.policy_version + 1
        assert clock_rollback.policy_version == same_millisecond.policy_version + 1
        assert same_millisecond.vector_id != first.vector_id
        assert clock_rollback.vector_id != same_millisecond.vector_id
        stored = await store.get()
        assert stored is not None
        assert stored.policy_version == clock_rollback.policy_version
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_weight_policy_limit_applies_after_task_family_blending(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await _seed_ranked_submission(conn, "agent-base", "hk-base", current_score=0.90)
        await _seed_ranked_submission(conn, "agent-task-only", "hk-task-only")
        await repository.insert_eval_run(
            conn,
            id="task-family-run-1",
            submission_id="agent-task-only",
            epoch=1,
            round_index=0,
            polaris_agent_id="ssh-hermes:hk-task-only",
            polaris_run_id="synthetic_boolean_v1:task-family-run-1",
            task_json={"task_type": "synthetic_boolean_v1"},
            output_card_json={},
            output_card_hash="hash-task-family-run-1",
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=datetime.now(UTC),
            duration_ms=1,
            errors=None,
            cathedral_signature="sig",
            eval_output_schema_version=5,
        )

        scores = await latest_policy_scores_by_hotkey(
            conn,
            limit=1,
            task_family_weights={"synthetic_boolean_v1": 1.0},
        )

        assert scores == {"hk-task-only": 1.0}
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_disable_legacy_base_scores_skips_agent_submissions(tmp_path) -> None:
    """With disable_legacy_base_scores=True, legacy v1 ranked rows do not
    contribute to the signed vector. Only task-family lane rows pay."""
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        # Legacy ranked submission with no SAT samples - should NOT appear
        # in scores when disable_legacy_base_scores is True.
        await _seed_ranked_submission(conn, "agent-legacy", "hk-legacy-only", current_score=0.85)
        # SAT-participating hotkey with a schema-5 eval row.
        await _seed_ranked_submission(conn, "agent-sat", "hk-sat-participant")
        await repository.insert_eval_run(
            conn,
            id="sat-run-1",
            submission_id="agent-sat",
            epoch=1,
            round_index=0,
            polaris_agent_id="ssh-hermes:hk-sat-participant",
            polaris_run_id="synthetic_boolean_v1:sat-run-1",
            task_json={"task_type": "synthetic_boolean_v1"},
            output_card_json={},
            output_card_hash="hash-sat-run-1",
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=datetime.now(UTC),
            duration_ms=1,
            errors=None,
            cathedral_signature="sig",
            eval_output_schema_version=5,
        )

        # With legacy disabled, only the SAT hotkey appears.
        sat_only = await latest_policy_scores_by_hotkey(
            conn,
            limit=10,
            task_family_weights={"synthetic_boolean_v1": 1.0},
            disable_legacy_base_scores=True,
        )
        assert sat_only == {"hk-sat-participant": 1.0}
        assert "hk-legacy-only" not in sat_only

        # Default (legacy enabled) sanity check: legacy hotkey is present.
        with_legacy = await latest_policy_scores_by_hotkey(
            conn,
            limit=10,
            task_family_weights={"synthetic_boolean_v1": 1.0},
        )
        assert "hk-legacy-only" in with_legacy
        assert with_legacy["hk-legacy-only"] == 0.85
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_disable_legacy_propagates_through_locked_production_path(monkeypatch) -> None:
    """Regression guard: the state_write_lock branch in produce_weight_policy_once
    must pass disable_legacy_base_scores through to latest_policy_scores_by_hotkey.

    Previously only the no-lock branch wired the kwarg, so production
    (which always passes db_write_lock) silently ignored the flag.
    """
    store = WeightPolicyStore()
    lock = asyncio.Lock()
    stop = asyncio.Event()
    captured_kwargs: dict = {}

    async def fake_scores(*_args, **kwargs) -> dict[str, float]:
        captured_kwargs.update(kwargs)
        return {}

    async def fake_next_version(_conn, *, issued_at: datetime) -> int:
        stop.set()
        return int(issued_at.timestamp() * 1000)

    monkeypatch.setattr(weight_policy_module, "latest_policy_scores_by_hotkey", fake_scores)
    monkeypatch.setattr(weight_policy_module, "_next_policy_version", fake_next_version)

    await run_weight_policy_producer(
        object(),
        store,
        _private_key(),
        config=WeightPolicyProducerConfig(
            interval_secs=3600,
            valid_for_secs=3600,
            disable_legacy_base_scores=True,
        ),
        stop=stop,
        db_write_lock=lock,
    )

    assert captured_kwargs.get("disable_legacy_base_scores") is True, (
        "production locked path must pass disable_legacy_base_scores through; "
        f"got kwargs={captured_kwargs}"
    )


@pytest.mark.asyncio
async def test_weight_policy_producer_locks_snapshot_and_state_write(monkeypatch) -> None:
    store = WeightPolicyStore()
    lock = asyncio.Lock()
    stop = asyncio.Event()
    events: list[str] = []

    async def fake_scores(*_args, **_kwargs) -> dict[str, float]:
        events.append("read_locked")
        assert lock.locked()
        return {"hk-a": 0.9}

    async def fake_next_version(_conn, *, issued_at: datetime) -> int:
        events.append("state_write_locked")
        assert lock.locked()
        stop.set()
        return int(issued_at.timestamp() * 1000)

    monkeypatch.setattr(weight_policy_module, "latest_policy_scores_by_hotkey", fake_scores)
    monkeypatch.setattr(weight_policy_module, "_next_policy_version", fake_next_version)

    await run_weight_policy_producer(
        object(),
        store,
        _private_key(),
        config=WeightPolicyProducerConfig(interval_secs=3600, valid_for_secs=3600),
        stop=stop,
        db_write_lock=lock,
    )

    assert events == ["read_locked", "state_write_locked"]
    assert await store.get() is not None


@pytest.mark.asyncio
async def test_weight_policy_snapshot_waits_for_shared_writer_rollback(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    lock = asyncio.Lock()
    store = WeightPolicyStore()
    try:
        await _seed_ranked_submission(conn, "agent-a", "hk-a", current_score=0.72)

        await lock.acquire()
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute(
            "UPDATE agent_submissions SET current_score = ? WHERE id = ?",
            (0.99, "agent-a"),
        )

        task = asyncio.create_task(
            produce_weight_policy_once(
                conn,
                store,
                _private_key(),
                config=WeightPolicyProducerConfig(valid_for_secs=3600),
                issued_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
                state_write_lock=lock,
            )
        )
        await asyncio.sleep(0)
        assert not task.done()

        await conn.rollback()
        lock.release()
        vector = await task

        weights = {entry.miner_hotkey: entry.weight for entry in vector.weights}
        assert weights == {"hk-a": 0.72}
    finally:
        if lock.locked():
            lock.release()
        await conn.close()


async def _seed_ranked_submission(
    conn,
    submission_id: str,
    miner_hotkey: str,
    *,
    current_score: float | None = None,
) -> None:
    await repository.insert_card_definition(
        conn,
        id="eu-ai-act",
        display_name="EU AI Act",
        jurisdiction="EU",
        topic="AI policy",
        description="desc",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    await repository.insert_agent_submission(
        conn,
        id=submission_id,
        miner_hotkey=miner_hotkey,
        card_id="eu-ai-act",
        bundle_blob_key=f"blob-{submission_id}",
        bundle_hash=f"hash-{submission_id}",
        bundle_size_bytes=10,
        encryption_key_id="key",
        bundle_signature="sig",
        display_name=submission_id,
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=f"fp-{submission_id}",
        similarity_check_passed=True,
        rejection_reason=None,
        status="queued",
        submitted_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        first_mover_at=None,
    )
    if current_score is None:
        await conn.execute(
            "UPDATE agent_submissions SET status = 'ranked' WHERE id = ?",
            (submission_id,),
        )
        await conn.commit()
    else:
        await repository.update_submission_score(
            conn,
            submission_id,
            current_score=current_score,
            current_rank=1,
        )
