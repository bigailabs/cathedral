from __future__ import annotations

import importlib.util as ilu
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.publisher import build_task_family_prompt, extract_answer
from cathedral.lanes.sign import (
    TASK_FAMILY_SCHEMA_VERSION,
    TASK_FAMILY_SIGNED_KEYS,
    build_signed_task_family_row,
    public_task_id,
)
from cathedral.validator import pull_loop
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import latest_pulled_score_per_hotkey, upsert_pulled_eval

_ROOT = Path(__file__).resolve().parents[2]


class _Signer:
    def __init__(self, sk: Ed25519PrivateKey) -> None:
        self._sk = sk


def _load_v2_payload_module():
    name = "cathedral.eval.v2_payload"
    if name in sys.modules and hasattr(sys.modules[name], "_SIGNED_KEYS_BY_VERSION"):
        return sys.modules[name]
    path = _ROOT / "src" / "cathedral" / "eval" / "v2_payload.py"
    spec = ilu.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _problem() -> PublicProblem:
    return PublicProblem(
        task_family="synthetic_boolean_v1",
        schema_version=1,
        task_id="private-task-id-001",
        difficulty_tier=1,
        public_input={"format": "dimacs", "cnf": "p cnf 1 1\n1 0\n"},
        time_limit_seconds=60,
    )


def _signed_row() -> tuple[dict[str, object], Ed25519PrivateKey]:
    sk = Ed25519PrivateKey.generate()
    problem = _problem()
    submission = Submission(
        task_id=problem.task_id,
        miner_hotkey="5Miner",
        answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
    )
    verifier = VerifierResult(
        parsed_ok=True,
        raw_metric=1.0,
        details={"clauses_satisfied": 1, "clause_count": 1},
    )
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    row = build_signed_task_family_row(
        eval_run_id="run-task-family-1",
        submission_id="submission-1",
        agent_display_name="Boolean Miner",
        miner_hotkey="5Miner",
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-18T20:00:00.000Z",
        signer=_Signer(sk),
        epoch_salt="epoch_123:synthetic_boolean_v1",
    )
    return row, sk


def test_task_family_signed_row_verifies_without_raw_problem_or_answer() -> None:
    row, sk = _signed_row()

    assert row["eval_output_schema_version"] == TASK_FAMILY_SCHEMA_VERSION
    assert row["task_id_public"] == public_task_id(
        "private-task-id-001",
        epoch_salt="epoch_123:synthetic_boolean_v1",
    )
    assert "private-task-id-001" not in str(row)
    # No raw answer key leaks into the signed wire row.
    assert "dimacs_solution" not in str(row)
    assert "SATISFIABLE" not in str(row)
    assert "assignment" not in str(row)

    pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_keysets_match_publisher_and_validator() -> None:
    publisher_keys = _load_v2_payload_module()._SIGNED_KEYS_BY_VERSION
    assert publisher_keys[TASK_FAMILY_SCHEMA_VERSION] == TASK_FAMILY_SIGNED_KEYS
    assert pull_loop._SIGNED_KEYS_BY_VERSION[TASK_FAMILY_SCHEMA_VERSION] == (
        TASK_FAMILY_SIGNED_KEYS
    )


def test_task_family_signed_row_rejects_tampered_score() -> None:
    row, sk = _signed_row()
    row["weighted_score"] = 0.0

    with pytest.raises(pull_loop.PullVerificationError):
        pull_loop.verify_eval_output_signature(row, sk.public_key())


def test_task_family_answer_extraction_prefers_final_answer_block() -> None:
    stdout = """notes
```FINAL_ANSWER
{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}
```
"""
    assert extract_answer(stdout) == {"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}


def test_task_family_prompt_keeps_challenge_generic() -> None:
    prompt = build_task_family_prompt(_problem())

    assert "Capability: synthetic_boolean_v1" in prompt
    assert "FINAL_ANSWER" in prompt
    assert "p cnf 1 1" in prompt


@pytest.mark.asyncio
async def test_synthetic_boolean_weight_defaults_off_and_blends_when_enabled(tmp_path) -> None:
    conn = await connect(str(tmp_path / "validator.db"))
    try:
        now = "2026-05-18T20:00:00.000Z"
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-v1",
                "card_id": "eu-ai-act",
                "weighted_score": 0.80,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-boolean",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-boolean-only",
                "task_type": "synthetic_boolean_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-boolean-only",
        )

        disabled = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.0},
        )
        assert disabled["hk-mixed"] == pytest.approx(0.80)
        assert "hk-boolean-only" not in disabled

        enabled = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )
        assert enabled["hk-mixed"] == pytest.approx((0.80 * 0.95) + 0.05)
        assert enabled["hk-boolean-only"] == pytest.approx(0.05)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unknown_schema5_task_family_contributes_zero(tmp_path) -> None:
    """A schema-5 row whose task_type is not in the configured
    ``task_family_weights`` map must NOT be bucketed as v1 and must
    contribute zero to the miner's score.

    Regression for the schema-5 weighting blocker: previously an
    unconfigured task family would fall through to the v1 bucket and
    silently award v1-share emissions to a brand-new lane that the
    validator had not yet opted in to.
    """
    conn = await connect(str(tmp_path / "validator.db"))
    try:
        now = "2026-05-18T20:00:00.000Z"
        # Hotkey has only a schema-5 row from an unknown family --
        # nothing else to fall back to.
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-future-family",
                "task_type": "future_family_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-future-only",
        )
        # Mixed hotkey: has a real v1 score AND an unconfigured schema-5
        # row. The unknown family must NOT contribute; only v1 should.
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-future-family-mixed",
                "task_type": "future_family_v1",
                "eval_output_schema_version": TASK_FAMILY_SCHEMA_VERSION,
                "weighted_score": 1.0,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed-future",
        )
        await upsert_pulled_eval(
            conn,
            eval_run={
                "id": "eval-v1-baseline",
                "card_id": "eu-ai-act",
                "weighted_score": 0.50,
                "ran_at": now,
            },
            miner_hotkey="hk-mixed-future",
        )

        scores = await latest_pulled_score_per_hotkey(
            conn,
            since_days=7,
            # No weight configured for future_family_v1.
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )

        # hk-future-only had only the unknown-family row -> nothing.
        assert "hk-future-only" not in scores, (
            f"unknown schema-5 family must not score; got {scores.get('hk-future-only')!r}"
        )
        # hk-mixed-future gets its v1 score back unchanged. The
        # unknown-family row contributes zero, not v1-bucketed.
        assert scores["hk-mixed-future"] == pytest.approx(0.50), (
            f"unknown schema-5 family must not be bucketed as v1; got {scores['hk-mixed-future']}"
        )
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Read-rollback gate for the public feed
# --------------------------------------------------------------------------


async def _seed_v1_card_and_submission(conn) -> dict:
    """Minimal v1-side seed so eval_runs has a submission to join."""
    from cathedral.publisher import repository as repo

    await repo.insert_card_definition(
        conn,
        id="eu-ai-act",
        display_name="EU AI Act",
        jurisdiction="EU",
        topic="AI Act",
        description="primary v1 card",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    submitted_at = _dt(2026, 5, 18, 19, 0, 0, tzinfo=_UTC)
    await repo.insert_agent_submission(
        conn,
        id="sub-boolean-rails",
        miner_hotkey="5MinerRails",
        card_id="eu-ai-act",
        bundle_blob_key="bundles/sub-boolean-rails.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name="Boolean Miner",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint="fp-boolean",
        similarity_check_passed=True,
        rejection_reason=None,
        status="ranked",
        submitted_at=submitted_at,
        submitted_at_iso="2026-05-18T19:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    seeded = await repo.get_agent_submission(conn, "sub-boolean-rails")
    assert seeded is not None
    return seeded


@pytest.mark.asyncio
async def test_read_rollback_gate_excludes_schema5_when_flag_off(tmp_path) -> None:
    """``include_task_families=False`` must keep schema-5 (Task Family)
    rows out of the public read surface, the same way ``include_v3=False``
    keeps v3 bug-isolation rows out.

    This is the rollback gate behind ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED``:
    when the flag is off, ``/v1/leaderboard/recent`` must not surface
    schema-5 rows. When the flag is on, they may appear.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from cathedral.publisher import repository as repo

    # validator.db.connect runs the publisher-side schema migrations
    # in tests; same pattern used by tests/v3/test_publisher_bug_isolation.py.
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission = await _seed_v1_card_and_submission(conn)

        # Insert one schema-5 row (Task Family) via the repository insert
        # used by persist_task_family_result.
        ran_at = _dt(2026, 5, 18, 20, 0, 0, tzinfo=_UTC)
        await repo.insert_eval_run(
            conn,
            id="00000000-0000-4000-8000-000000000501",
            submission_id=str(submission["id"]),
            epoch=501,
            round_index=0,
            polaris_agent_id="ssh-hermes:5MinerRails",
            polaris_run_id="synthetic_boolean_v1:run-501",
            task_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "deadbeef",
            },
            output_card_json={
                "task_type": "synthetic_boolean_v1",
                "task_id_public": "deadbeef",
                "weighted_score": 1.0,
            },
            output_card_hash="a" * 64,
            score_parts={"binary_correct": 1.0},
            weighted_score=1.0,
            ran_at=ran_at,
            ran_at_iso="2026-05-18T20:00:00.000Z",
            duration_ms=42,
            errors=None,
            cathedral_signature="b64:stub",
            polaris_verified=False,
            trace_json=None,
            eval_output_schema_version=5,
        )

        since = _dt(2000, 1, 1, tzinfo=_UTC)

        # Flag off: schema-5 row hidden.
        gated = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=False,
            include_task_families=False,
        )
        assert all(r.get("eval_output_schema_version") != 5 for r in gated), (
            f"schema-5 row leaked into gated read: {gated!r}"
        )

        # Flag on: schema-5 row visible.
        with_families = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=False,
            include_task_families=True,
        )
        schema5_rows = [r for r in with_families if r.get("eval_output_schema_version") == 5]
        assert len(schema5_rows) == 1, (
            "expected exactly one schema-5 row when include_task_families=True, "
            f"got {with_families!r}"
        )
    finally:
        await conn.close()
