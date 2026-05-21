"""Issue #155: publisher-side weight-policy surface tests.

Covers ``GET /v1/validator/weights/next``: 503 when no vector, 200 with
the wire shape when populated; plus the deterministic builder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cathedral.policy.signing import SignedWeightVector, verify_vector
from cathedral.publisher import repository
from cathedral.publisher.weight_policy import (
    WeightPolicyProducerConfig,
    WeightPolicyStore,
    build_and_sign,
    build_unsigned_vector,
    latest_policy_scores_by_hotkey,
    produce_weight_policy_once,
)
from cathedral.publisher.weight_policy import (
    router as weight_policy_router,
)
from cathedral.validator.db import connect


def _bare_app_with_store() -> tuple[FastAPI, WeightPolicyStore]:
    """Minimal FastAPI app + store wired the way the real publisher
    does - but without dragging in the heavy publisher lifespan."""
    app = FastAPI()
    store = WeightPolicyStore()
    app.state.weight_policy = store
    app.include_router(weight_policy_router)
    return app, store


def test_endpoint_returns_503_when_store_empty() -> None:
    app, _store = _bare_app_with_store()
    with TestClient(app) as client:
        resp = client.get("/v1/validator/weights/next")
        assert resp.status_code == 503
        assert "no weight vector" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_endpoint_returns_signed_vector_when_present() -> None:
    app, store = _bare_app_with_store()
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    vector = build_and_sign(
        {"hk-a": 0.4, "hk-b": 0.6},
        sk,
        vector_id="vec-test-1",
        policy_version=11,
        network="finney",
        netuid=39,
        key_id="pinned-key",
        policy_reason="test policy",
        burn_uid=204,
        forced_burn_percentage=95.0,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
        policy_metadata={"source": "unit"},
    )
    await store.set(vector)
    with TestClient(app) as client:
        resp = client.get("/v1/validator/weights/next")
        assert resp.status_code == 200
        body = resp.json()
        # Reparse the body and re-verify the signature end-to-end.
        roundtripped = SignedWeightVector.model_validate(body)
        verify_vector(roundtripped, public_key=pk, expected_key_id="pinned-key")
        assert roundtripped.vector_id == "vec-test-1"
        assert roundtripped.policy_version == 11
        assert roundtripped.generated_at == "2026-05-19T00:00:00.000Z"
        assert roundtripped.burn_snapshot.burn_uid == 204
        assert roundtripped.policy_hash
        assert len(roundtripped.weights) == 2


def test_endpoint_returns_503_when_store_attribute_missing() -> None:
    """If the app forgot to attach a store at all, the endpoint must
    refuse rather than crash. 503 lets the validator's poll loop treat
    it as a transient empty state."""
    app = FastAPI()
    app.include_router(weight_policy_router)
    with TestClient(app) as client:
        resp = client.get("/v1/validator/weights/next")
        assert resp.status_code == 503


def test_build_unsigned_vector_sorts_by_hotkey_for_determinism() -> None:
    v1 = build_unsigned_vector(
        {"hk-z": 0.1, "hk-a": 0.5, "hk-m": 0.3},
        vector_id="vec",
        policy_version=1,
        network="finney",
        netuid=39,
        key_id="k",
        policy_reason="test policy",
        burn_uid=204,
        forced_burn_percentage=95.0,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    v2 = build_unsigned_vector(
        [("hk-a", 0.5), ("hk-m", 0.3), ("hk-z", 0.1)],
        vector_id="vec",
        policy_version=1,
        network="finney",
        netuid=39,
        key_id="k",
        policy_reason="test policy",
        burn_uid=204,
        forced_burn_percentage=95.0,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    assert [w.miner_hotkey for w in v1.weights] == ["hk-a", "hk-m", "hk-z"]
    assert [w.miner_hotkey for w in v2.weights] == ["hk-a", "hk-m", "hk-z"]
    # Two equivalent inputs must produce structurally identical vectors.
    assert v1.model_dump() == v2.model_dump()


def test_build_unsigned_vector_drops_zero_and_negative() -> None:
    v = build_unsigned_vector(
        {"hk-a": 0.4, "hk-b": 0.0, "hk-c": -0.1},
        vector_id="vec",
        policy_version=1,
        network="finney",
        netuid=39,
        key_id="k",
        policy_reason="test policy",
        burn_uid=204,
        forced_burn_percentage=95.0,
        issued_at=datetime(2026, 5, 19, tzinfo=UTC),
    )
    assert [w.miner_hotkey for w in v.weights] == ["hk-a"]


def test_build_unsigned_vector_rejects_non_finite_score() -> None:
    with pytest.raises(ValueError):
        build_unsigned_vector(
            {"hk-a": float("nan")},
            vector_id="vec",
            policy_version=1,
            network="finney",
            netuid=39,
            key_id="k",
            policy_reason="test policy",
            burn_uid=204,
            forced_burn_percentage=95.0,
            issued_at=datetime(2026, 5, 19, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_producer_populates_store_from_ranked_scores(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
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
            id="agent-a",
            miner_hotkey="hk-a",
            card_id="eu-ai-act",
            bundle_blob_key="blob",
            bundle_hash="hash-a",
            bundle_size_bytes=10,
            encryption_key_id="key",
            bundle_signature="sig",
            display_name="Agent A",
            bio=None,
            logo_url=None,
            soul_md_preview=None,
            metadata_fingerprint="fp-a",
            similarity_check_passed=True,
            rejection_reason=None,
            status="queued",
            submitted_at=datetime(2026, 5, 19, tzinfo=UTC),
            first_mover_at=None,
        )
        await repository.update_submission_score(
            conn, "agent-a", current_score=0.72, current_rank=1
        )

        sk = Ed25519PrivateKey.generate()
        store = WeightPolicyStore()
        vector = await produce_weight_policy_once(
            conn,
            store,
            sk,
            config=WeightPolicyProducerConfig(
                network="finney",
                netuid=39,
                key_id="pinned-key",
                burn_uid=204,
                forced_burn_percentage=95.0,
                interval_secs=1500.0,
                valid_for_secs=1800.0,
            ),
            issued_at=datetime(2026, 5, 19, tzinfo=UTC),
        )

        stored = await store.get()
        assert stored is not None
        assert stored.vector_id == vector.vector_id
        verify_vector(stored, public_key=sk.public_key(), expected_key_id="pinned-key")
        assert stored.policy_reason == "publisher_current_ranked_scores"
        assert stored.burn_snapshot.burn_uid == 204
        assert stored.burn_snapshot.forced_burn_percentage == 95.0
        assert len(stored.policy_hash) == 64
        assert stored.policy_metadata["ranked_hotkeys"] == 1
        assert [(w.miner_hotkey, w.weight) for w in stored.weights] == [("hk-a", 0.72)]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_policy_scores_ignore_schema5_sat_rows_until_weight_enabled(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
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
        for submission_id, hotkey, current_score in [
            ("agent-a", "hk-a", 0.25),
            ("agent-b", "hk-b", 0.75),
        ]:
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
                current_score=current_score,
                current_rank=1,
            )

        for eval_id, submission_id, score in [
            ("sat-win", "agent-a", 1.0),
            ("sat-lose", "agent-b", 0.0),
        ]:
            await repository.insert_eval_run(
                conn,
                id=eval_id,
                submission_id=submission_id,
                epoch=1,
                round_index=0,
                polaris_agent_id=f"ssh-hermes:{submission_id}",
                polaris_run_id=f"synthetic_boolean_v1:{eval_id}",
                task_json={
                    "task_type": "synthetic_boolean_v1",
                    "task_id_public": f"public-{eval_id}",
                    "epoch_salt": "epoch_1:synthetic_boolean_v1",
                },
                output_card_json={
                    "task_type": "synthetic_boolean_v1",
                    "task_id_public": f"public-{eval_id}",
                    "weighted_score": score,
                },
                output_card_hash=f"hash-{eval_id}",
                score_parts={"binary_correct": score},
                weighted_score=score,
                ran_at=datetime.now(UTC),
                ran_at_iso=datetime.now(UTC).isoformat(),
                duration_ms=1,
                errors=None if score else ["challenge_already_locked"],
                cathedral_signature="sig",
                eval_output_schema_version=5,
            )

        scores = await latest_policy_scores_by_hotkey(conn)
        assert scores["hk-a"] == pytest.approx(0.25)
        assert scores["hk-b"] == pytest.approx(0.75)

        enabled = await latest_policy_scores_by_hotkey(
            conn,
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )
        assert enabled["hk-a"] == pytest.approx((0.25 * 0.95) + (1.0 * 0.05))
        assert enabled["hk-b"] == pytest.approx(0.75 * 0.95)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_policy_vector_metadata_records_task_family_weights(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
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
            id="agent-a",
            miner_hotkey="hk-a",
            card_id="eu-ai-act",
            bundle_blob_key="blob-a",
            bundle_hash="hash-a",
            bundle_size_bytes=10,
            encryption_key_id="key",
            bundle_signature="sig",
            display_name="agent-a",
            bio=None,
            logo_url=None,
            soul_md_preview=None,
            metadata_fingerprint="fp-a",
            similarity_check_passed=True,
            rejection_reason=None,
            status="queued",
            submitted_at=datetime(2026, 5, 19, tzinfo=UTC),
            first_mover_at=None,
        )
        await repository.update_submission_score(
            conn,
            "agent-a",
            current_score=0.25,
            current_rank=1,
        )
        await repository.insert_eval_run(
            conn,
            id="sat-win",
            submission_id="agent-a",
            epoch=1,
            round_index=0,
            polaris_agent_id="ssh-hermes:agent-a",
            polaris_run_id="synthetic_boolean_v1:sat-win",
            task_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "public-sat-win",
                "epoch_salt": "epoch_1:synthetic_boolean_v1",
            },
            output_card_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "public-sat-win",
                "weighted_score": 1.0,
            },
            output_card_hash="hash-sat-win",
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=datetime.now(UTC),
            ran_at_iso=datetime.now(UTC).isoformat(),
            duration_ms=1,
            errors=None,
            cathedral_signature="sig",
            eval_output_schema_version=5,
        )

        sk = Ed25519PrivateKey.generate()
        store = WeightPolicyStore()
        vector = await produce_weight_policy_once(
            conn,
            store,
            sk,
            config=WeightPolicyProducerConfig(
                network="finney",
                netuid=39,
                key_id="pinned-key",
                burn_uid=204,
                forced_burn_percentage=95.0,
                interval_secs=1500.0,
                valid_for_secs=1800.0,
                task_family_weights={"synthetic_boolean_v1": 0.05},
            ),
            issued_at=datetime(2026, 5, 19, tzinfo=UTC),
        )

        assert vector.policy_metadata["task_family_weights"] == {"synthetic_boolean_v1": 0.05}
        assert [(w.miner_hotkey, w.weight) for w in vector.weights] == [
            ("hk-a", pytest.approx((0.25 * 0.95) + (1.0 * 0.05)))
        ]
    finally:
        await conn.close()
