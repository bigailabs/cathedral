"""Tests for the open-window ranked-solve path (WS-SCORE.C data layer).

Covers the ledger (record_ranked_solve) and the eval_run artifact insert
(insert_ranked_eval_run), the open-window analogue of atomic_claim_winner —
no single-winner lock, no challenge status flip, every valid solver ranked.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import TASK_FAMILY_SCHEMA_VERSION_V6, build_signed_task_family_row
from cathedral.publisher import repository as repo
from cathedral.validator.db import connect

FAMILY = "synthetic_boolean_v1"
CHALLENGE = "sat-t1-easy-001"


async def _conn(tmp_path):
    conn = await connect(str(tmp_path / "publisher.db"))
    await conn.executescript(CHALLENGE_LOCK_SCHEMA)
    await conn.executescript(repo.EVAL_RUN_SOLUTIONS_SCHEMA)
    await conn.executescript(repo.LANE_CHALLENGE_SOLVES_SCHEMA)
    await conn.commit()
    return conn


async def _seed_submission(conn, *, sub_id: str, hotkey: str) -> None:
    await repo.insert_card_definition(
        conn,
        id=FAMILY,
        display_name="SAT lane",
        jurisdiction="-",
        topic="SAT",
        description="SAT registration substrate",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    await repo.insert_agent_submission(
        conn,
        id=sub_id,
        miner_hotkey=hotkey,
        card_id=FAMILY,
        bundle_blob_key=f"bundles/{sub_id}.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name="Open Window Miner",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=f"fp-{sub_id}",
        similarity_check_passed=True,
        rejection_reason=None,
        status="pending_solution",
        submitted_at=datetime(2026, 5, 29, 19, 0, 0, tzinfo=UTC),
        submitted_at_iso="2026-05-29T19:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )


def _v6_row(*, eval_run_id: str, submission_id: str, hotkey: str, solve_rank: int, w_c: float):
    sk = Ed25519PrivateKey.generate()
    problem = PublicProblem(
        task_family=FAMILY,
        schema_version=1,
        task_id=CHALLENGE,
        difficulty_tier=1,
        public_input={"format": "dimacs", "cnf": "p cnf 1 1\n1 0\n"},
        time_limit_seconds=60,
    )
    submission = Submission(
        task_id=CHALLENGE, miner_hotkey=hotkey, answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"}
    )
    verifier = VerifierResult(parsed_ok=True, raw_metric=1.0, details={"clauses_satisfied": 1})
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    return build_signed_task_family_row(
        eval_run_id=eval_run_id,
        submission_id=submission_id,
        agent_display_name="Open Window Miner",
        miner_hotkey=hotkey,
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-29T20:00:00.000Z",
        signer=EvalSigner(sk),
        epoch_salt="epoch_1:synthetic_boolean_v1",
        schema_version=TASK_FAMILY_SCHEMA_VERSION_V6,
        challenge_value=w_c,
        solve_rank=solve_rank,
        solved=True,
        operator=hotkey,
    )


@pytest.mark.asyncio
async def test_first_solver_gets_rank_one(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        res = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id=CHALLENGE, miner_hotkey="5HotkeyA",
            eval_run_id="run-a", weighted_score=1.0, solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        assert res.solve_rank == 1 and res.newly_recorded is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_subsequent_distinct_solvers_get_increasing_ranks(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        ranks = []
        for i, hk in enumerate(["5A", "5B", "5C"]):
            res = await repo.record_ranked_solve(
                conn, family_id=FAMILY, challenge_id=CHALLENGE, miner_hotkey=hk,
                eval_run_id=f"run-{i}", weighted_score=1.0,
                solved_at_iso="2026-05-29T20:00:00.000Z",
            )
            ranks.append(res.solve_rank)
        assert ranks == [1, 2, 3]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_resubmit_is_idempotent_no_second_row(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        first = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id=CHALLENGE, miner_hotkey="5A",
            eval_run_id="run-1", weighted_score=1.0, solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        again = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id=CHALLENGE, miner_hotkey="5A",
            eval_run_id="run-2", weighted_score=1.0, solved_at_iso="2026-05-29T20:05:00.000Z",
        )
        assert first.solve_rank == 1 and first.newly_recorded is True
        assert again.solve_rank == 1 and again.newly_recorded is False
        cur = await conn.execute(
            "SELECT COUNT(*) FROM lane_challenge_solves WHERE family_id=? AND challenge_id=?",
            (FAMILY, CHALLENGE),
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_ranks_are_per_challenge(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id="c1", miner_hotkey="5A",
            eval_run_id="r1", weighted_score=1.0, solved_at_iso="t",
        )
        other = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id="c2", miner_hotkey="5A",
            eval_run_id="r2", weighted_score=1.0, solved_at_iso="t",
        )
        assert other.solve_rank == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_insert_ranked_eval_run_writes_artifact_and_ranks_submission(tmp_path) -> None:
    conn = await _conn(tmp_path)
    try:
        sub_id = "sub-ow-1"
        hotkey = "5OpenWindowMiner"
        await _seed_submission(conn, sub_id=sub_id, hotkey=hotkey)

        ledger = await repo.record_ranked_solve(
            conn, family_id=FAMILY, challenge_id=CHALLENGE, miner_hotkey=hotkey,
            eval_run_id="ow-eval-1", weighted_score=1.0, solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        assert ledger.solve_rank == 1

        row = _v6_row(
            eval_run_id="ow-eval-1", submission_id=sub_id, hotkey=hotkey,
            solve_rank=ledger.solve_rank, w_c=3.0,
        )
        eval_run_id = await repo.insert_ranked_eval_run(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey=hotkey,
            submission_id=sub_id,
            cnf_sha256="ab" * 32,
            dimacs_solution_sha256="cd" * 32,
            ran_at_iso="2026-05-29T20:00:00.000Z",
            signed_row=row,
            epoch=1,
            round_index=0,
            time_limit_seconds=300,
            dimacs_solution="s SATISFIABLE\nv 1 0\n",
        )
        assert eval_run_id == "ow-eval-1"

        # eval_run persisted at schema 6.
        cur = await conn.execute(
            "SELECT eval_output_schema_version, weighted_score FROM eval_runs WHERE id=?",
            ("ow-eval-1",),
        )
        er = await cur.fetchone()
        assert er is not None and int(er[0]) == 6 and float(er[1]) == 1.0

        # DIMACS sidecar retained (#242).
        cur = await conn.execute(
            "SELECT COUNT(*) FROM eval_run_solutions WHERE eval_run_id=?", ("ow-eval-1",)
        )
        assert (await cur.fetchone())[0] == 1

        # submission marked ranked; challenge NOT locked (no winners row).
        cur = await conn.execute("SELECT status FROM agent_submissions WHERE id=?", (sub_id,))
        assert (await cur.fetchone())[0] == "ranked"
        cur = await conn.execute(
            "SELECT COUNT(*) FROM lane_challenge_winners WHERE family_id=? AND challenge_id=?",
            (FAMILY, CHALLENGE),
        )
        assert (await cur.fetchone())[0] == 0  # open-window does not lock a single winner
    finally:
        await conn.close()
