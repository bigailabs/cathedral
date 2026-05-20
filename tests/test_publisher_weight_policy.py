from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cathedral.policy.schemas import SignedWeightVector, verify_vector
from cathedral.publisher import repository
from cathedral.publisher.weight_policy import (
    FileBackedWeightPolicyStore,
    WeightPolicyProducerConfig,
    build_and_sign,
    build_unsigned_vector,
    latest_policy_scores_by_hotkey,
    produce_weight_policy_once,
)
from cathedral.publisher.weight_policy import router as weight_policy_router
from cathedral.validator.db import connect


def _app_with_store(store: FileBackedWeightPolicyStore) -> FastAPI:
    app = FastAPI()
    app.state.weight_policy = store
    app.include_router(weight_policy_router)
    return app


def test_endpoint_returns_503_when_no_vector(tmp_path) -> None:
    app = _app_with_store(FileBackedWeightPolicyStore(tmp_path / "current_vector.json"))
    with TestClient(app) as client:
        response = client.get("/v1/validator/weights/next")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_endpoint_serves_signed_vector_from_file_backed_store(tmp_path) -> None:
    store = FileBackedWeightPolicyStore(tmp_path / "current_vector.json")
    sk = Ed25519PrivateKey.generate()
    vector = build_and_sign(
        {"miner-hotkey": 0.5},
        sk,
        vector_id="vec-1",
        policy_version=10,
        network="finney",
        netuid=39,
        metagraph_block=100,
        key_id="pinned",
        burn_hotkey="burn-hotkey",
        burn_uid_snapshot=204,
        burn_share=0.95,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    await store.set(vector)

    loaded = FileBackedWeightPolicyStore(tmp_path / "current_vector.json")
    await loaded.load()
    app = _app_with_store(loaded)
    with TestClient(app) as client:
        response = client.get("/v1/validator/weights/next")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    roundtrip = SignedWeightVector.model_validate(response.json())
    verify_vector(roundtrip, public_key=sk.public_key(), expected_key_id="pinned")
    assert roundtrip.burn_hotkey == "burn-hotkey"
    assert roundtrip.burn_uid_snapshot == 204
    assert roundtrip.weights_by_hotkey["burn-hotkey"] == pytest.approx(0.95)


def test_build_unsigned_vector_applies_burn_hotkey_and_normalizes() -> None:
    vector = build_unsigned_vector(
        {"hk-b": 3.0, "hk-a": 1.0, "burn-hotkey": 99.0},
        vector_id="vec",
        policy_version=1,
        network="finney",
        netuid=39,
        metagraph_block=1,
        key_id="pinned",
        burn_hotkey="burn-hotkey",
        burn_uid_snapshot=204,
        burn_share=0.5,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    assert vector.weights_by_hotkey == {
        "burn-hotkey": pytest.approx(0.5),
        "hk-a": pytest.approx(0.125),
        "hk-b": pytest.approx(0.375),
    }
    assert sum(vector.weights_by_hotkey.values()) == pytest.approx(1.0)


def test_build_unsigned_vector_routes_empty_zero_burn_policy_to_burn_hotkey() -> None:
    vector = build_unsigned_vector(
        {},
        vector_id="vec",
        policy_version=1,
        network="finney",
        netuid=39,
        metagraph_block=1,
        key_id="pinned",
        burn_hotkey="burn-hotkey",
        burn_uid_snapshot=204,
        burn_share=0.0,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    assert vector.weights_by_hotkey == {"burn-hotkey": 1.0}


@pytest.mark.asyncio
async def test_producer_populates_store_from_ranked_scores(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await _insert_scored_submission(conn, "agent-a", "hk-a", 0.72)
        sk = Ed25519PrivateKey.generate()
        store = FileBackedWeightPolicyStore(tmp_path / "current_vector.json")
        vector = await produce_weight_policy_once(
            conn,
            store,
            sk,
            config=WeightPolicyProducerConfig(
                network="finney",
                netuid=39,
                key_id="pinned",
                burn_hotkey="burn-hotkey",
                burn_uid_snapshot=204,
                burn_share=0.95,
                metagraph_block=123,
            ),
            issued_at=datetime(2026, 5, 19, tzinfo=UTC),
        )
        stored = await store.get()
        assert stored is not None
        verify_vector(stored, public_key=sk.public_key(), expected_key_id="pinned")
        assert stored.vector_id == vector.vector_id
        assert stored.policy_hash
        assert stored.weights_by_hotkey["burn-hotkey"] == pytest.approx(0.95)
        assert stored.weights_by_hotkey["hk-a"] == pytest.approx(0.05)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_policy_scores_ignore_schema5_rows_until_weight_enabled(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await _insert_scored_submission(conn, "agent-a", "hk-a", 0.25)
        await repository.insert_eval_run(
            conn,
            id="sat-win",
            submission_id="agent-a",
            epoch=1,
            round_index=0,
            polaris_agent_id="ssh-hermes:agent-a",
            polaris_run_id="synthetic_boolean_v1:sat-win",
            task_json={"task_type": "synthetic_boolean_v1"},
            output_card_json={"weighted_score": 1.0},
            output_card_hash="hash-sat-win",
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=datetime.now(UTC),
            duration_ms=1,
            errors=None,
            cathedral_signature="sig",
            eval_output_schema_version=5,
        )

        disabled = await latest_policy_scores_by_hotkey(conn)
        assert disabled["hk-a"] == pytest.approx(0.25)

        enabled = await latest_policy_scores_by_hotkey(
            conn,
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )
        assert enabled["hk-a"] == pytest.approx((0.25 * 0.95) + (1.0 * 0.05))
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_policy_scores_apply_miner_overrides(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await _insert_scored_submission(conn, "agent-a", "hk-a", 0.72)
        scores = await latest_policy_scores_by_hotkey(
            conn,
            miner_overrides={"hk-a": 0.0, "hk-b": 0.4},
        )
        assert "hk-a" not in scores
        assert scores["hk-b"] == pytest.approx(0.4)
    finally:
        await conn.close()


async def _insert_scored_submission(conn, submission_id: str, hotkey: str, score: float) -> None:
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
        miner_hotkey=hotkey,
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
        submitted_at=datetime(2026, 5, 19, tzinfo=UTC),
        first_mover_at=None,
    )
    await repository.update_submission_score(
        conn,
        submission_id,
        current_score=score,
        current_rank=1,
    )
