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
        answer={"assignment": {"1": True}},
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
{"assignment": {"1": true}}
```
"""
    assert extract_answer(stdout) == {"assignment": {"1": True}}


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
