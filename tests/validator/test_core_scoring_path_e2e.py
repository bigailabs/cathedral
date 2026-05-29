"""Core scoring path, end to end — no infra, no keys, no chain.

Exercises the whole PAR-2 brain in one test:
  publisher signs a schema-6 fact row  (build_signed_task_family_row)
    -> validator verifies the signature (verify_eval_output_signature)
    -> validator stores the v6 facts     (upsert_pulled_eval)
    -> PAR-2 merit per operator          (par2_merit_per_operator)
    -> operator->uid + burn + normalize  (apply_burn, normalize)
    -> the on-chain weight vector.

This is the "does the new scoring actually work" check that the Stitch/testnet
run would also do, but here it's pure + deterministic.
"""

from __future__ import annotations

import math

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.chain import apply_burn, normalize
from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import TASK_FAMILY_SCHEMA_VERSION_V6, build_signed_task_family_row
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import (
    par2_merit_per_operator,
    upsert_pulled_eval,
    verify_eval_output_signature,
)

FAMILY = "synthetic_boolean_v1"
EPOCH_SALT = "epoch_1:synthetic_boolean_v1"


def _signed_v6(sk, *, eval_run_id, operator, task_id, solve_rank, w_c):
    """A publisher-signed schema-6 fact row, as the open-window submit path emits."""
    problem = PublicProblem(
        task_family=FAMILY,
        schema_version=1,
        task_id=task_id,
        difficulty_tier=1,
        public_input={"format": "dimacs", "cnf": "p cnf 1 1\n1 0\n"},
        time_limit_seconds=60,
    )
    submission = Submission(
        task_id=task_id, miner_hotkey=operator, answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    )
    verifier = VerifierResult(parsed_ok=True, raw_metric=1.0, details={"clauses_satisfied": 1})
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    return build_signed_task_family_row(
        eval_run_id=eval_run_id,
        submission_id=f"sub-{eval_run_id}",
        agent_display_name="m",
        miner_hotkey=operator,
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-29T20:00:00.000Z",
        signer=EvalSigner(sk),
        epoch_salt=EPOCH_SALT,
        schema_version=TASK_FAMILY_SCHEMA_VERSION_V6,
        challenge_value=w_c,
        solve_rank=solve_rank,
        solved=True,
        operator=operator,
    )


@pytest.mark.asyncio
async def test_core_scoring_path_sign_verify_pull_score_burn(tmp_path) -> None:
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key()
    conn = await connect(str(tmp_path / "v.db"))
    try:
        # Two challenges. opA wins challenge A (rank 1) AND solves B (sole);
        # opB is rank 2 on A. w_c = 10 each.
        rows = [
            _signed_v6(sk, eval_run_id="a-A", operator="opA", task_id="A", solve_rank=1, w_c=10.0),
            _signed_v6(sk, eval_run_id="b-A", operator="opB", task_id="A", solve_rank=2, w_c=10.0),
            _signed_v6(sk, eval_run_id="a-B", operator="opA", task_id="B", solve_rank=1, w_c=10.0),
        ]

        # 1) every signed fact verifies under the publisher key (the contract),
        # 2) and lands in the validator's pull store with its v6 facts.
        for row in rows:
            verify_eval_output_signature(row, pub)  # raises on tamper
            await upsert_pulled_eval(conn, eval_run=row, miner_hotkey=str(row["miner_hotkey"]))

        # 3) PAR-2 merit per operator over the closed suite.
        merit = await par2_merit_per_operator(conn, since_days=3650, alpha=0.5)
        assert math.isclose(sum(merit.values()), 20.0, rel_tol=1e-9)  # budget conserved (2 * 10)
        assert merit["opA"] > merit["opB"]  # coverage + rank-1 dominance

        # 4) operator -> uid, then the real on-chain transform: burn + normalize.
        uid_of = {"opA": 1, "opB": 2}
        scored = [(uid_of[op], m) for op, m in merit.items()]
        vector = dict(normalize(apply_burn(scored, burn_uid=204, forced_burn_percentage=50.0)))

        # The final weight vector: sums to 1, burn uid takes its 50%, miners
        # split the rest by PAR-2 merit (opA ahead of opB), and a tampered
        # fact never reaches weight (verified above).
        assert math.isclose(sum(vector.values()), 1.0, rel_tol=1e-9)
        assert math.isclose(vector[204], 0.5, rel_tol=1e-9)
        assert math.isclose(vector[1] + vector[2], 0.5, rel_tol=1e-9)
        assert vector[1] > vector[2] > 0.0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_core_scoring_path_tampered_fact_is_rejected(tmp_path) -> None:
    sk = Ed25519PrivateKey.generate()
    row = _signed_v6(sk, eval_run_id="t1", operator="opA", task_id="A", solve_rank=1, w_c=10.0)
    # Forge a better solve_rank after signing — the signature must reject it,
    # so a tampered PAR-2 fact can never enter the weight vector.
    row["solve_rank"] = 99
    from cathedral.validator.pull_loop import PullVerificationError

    with pytest.raises(PullVerificationError):
        verify_eval_output_signature(row, sk.public_key())
