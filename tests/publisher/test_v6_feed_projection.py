"""Tests for the schema-6 (PAR-2) feed projection fix.

Covers:
1. Persist path: insert_ranked_eval_run stores the 4 v6 fields in task_json.
2. Serializer shape: _eval_run_to_output emits eval_output_schema_version=6
   and all 18 signed keys at the correct types.
3. Gold signature test: sign a real v6 row with an ed25519 key, store via the
   write path, serve through _eval_run_to_output, verify with
   verify_eval_output_signature + the matching public key. Must verify.
4. Unknown SAT-era schema raises ValueError (fail-loud sentinel).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import TASK_FAMILY_SCHEMA_VERSION_V6, build_signed_task_family_row
from cathedral.publisher import repository as repo
from cathedral.publisher.reads import _eval_run_to_output
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import verify_eval_output_signature

FAMILY = "synthetic_boolean_v1"
CHALLENGE = "sat-t1-gold-sig-001"

# The 18 keys that schema-6 signed rows must expose on the wire.
_V6_REQUIRED_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "agent_id",
        "agent_display_name",
        "miner_hotkey",
        "task_type",
        "task_id_public",
        "epoch_salt",
        "difficulty_tier",
        "weighted_score",
        "score_parts",
        "answer_hash",
        "verifier_details_hash",
        "rejection_reason",
        "ran_at",
        "challenge_value",
        "solve_rank",
        "solved",
        "operator",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        display_name="V6 Gold Miner",
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


def _build_v6_row(
    *,
    eval_run_id: str,
    submission_id: str,
    hotkey: str,
    solve_rank: int,
    w_c: float,
    signer: EvalSigner,
) -> dict:
    problem = PublicProblem(
        task_family=FAMILY,
        schema_version=1,
        task_id=CHALLENGE,
        difficulty_tier=1,
        public_input={"format": "dimacs", "cnf": "p cnf 1 1\n1 0\n"},
        time_limit_seconds=60,
    )
    submission = Submission(
        task_id=CHALLENGE,
        miner_hotkey=hotkey,
        answer={"dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
    )
    verifier = VerifierResult(parsed_ok=True, raw_metric=1.0, details={"clauses_satisfied": 1})
    score = ScoreResult(weighted_score=1.0, score_parts={"binary_correct": 1.0})
    return build_signed_task_family_row(
        eval_run_id=eval_run_id,
        submission_id=submission_id,
        agent_display_name="V6 Gold Miner",
        miner_hotkey=hotkey,
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso="2026-05-29T20:00:00.000Z",
        signer=signer,
        epoch_salt="epoch_gold:synthetic_boolean_v1",
        schema_version=TASK_FAMILY_SCHEMA_VERSION_V6,
        challenge_value=w_c,
        solve_rank=solve_rank,
        solved=True,
        operator=hotkey,
    )


# ---------------------------------------------------------------------------
# Test: persist path stores the 4 v6 fields in task_json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_ranked_eval_run_persists_v6_fields(tmp_path) -> None:
    """insert_ranked_eval_run must write challenge_value/solve_rank/solved/operator
    into task_json so the feed serializer can reconstruct the signed projection."""
    conn = await _conn(tmp_path)
    sk = Ed25519PrivateKey.generate()
    signer = EvalSigner(sk)
    sub_id = "sub-v6-persist-1"
    hotkey = "5V6PersistMiner"
    eval_run_id = "v6-persist-001"
    w_c = 2.5
    try:
        await _seed_submission(conn, sub_id=sub_id, hotkey=hotkey)
        ledger = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey=hotkey,
            eval_run_id=eval_run_id,
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:00:00.000Z",
        )
        assert ledger.solve_rank == 1

        row = _build_v6_row(
            eval_run_id=eval_run_id,
            submission_id=sub_id,
            hotkey=hotkey,
            solve_rank=ledger.solve_rank,
            w_c=w_c,
            signer=signer,
        )
        await repo.insert_ranked_eval_run(
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
        )

        cur = await conn.execute(
            "SELECT task_json FROM eval_runs WHERE id = ?", (eval_run_id,)
        )
        raw = (await cur.fetchone())[0]
        task_json = json.loads(raw)

        assert "challenge_value" in task_json, "challenge_value missing from task_json"
        assert "solve_rank" in task_json, "solve_rank missing from task_json"
        assert "solved" in task_json, "solved missing from task_json"
        assert "operator" in task_json, "operator missing from task_json"

        assert task_json["challenge_value"] == pytest.approx(w_c)
        assert task_json["solve_rank"] == 1
        assert task_json["solved"] is True
        assert task_json["operator"] == hotkey
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Test: serializer shape — all 18 keys + correct eval_output_schema_version
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eval_run_to_output_v6_shape(tmp_path) -> None:
    """_eval_run_to_output must emit eval_output_schema_version=6 and all 18 keys."""
    conn = await _conn(tmp_path)
    sk = Ed25519PrivateKey.generate()
    signer = EvalSigner(sk)
    sub_id = "sub-v6-shape-1"
    hotkey = "5V6ShapeMiner"
    eval_run_id = "v6-shape-001"
    w_c = 3.0
    try:
        await _seed_submission(conn, sub_id=sub_id, hotkey=hotkey)
        ledger = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey=hotkey,
            eval_run_id=eval_run_id,
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:00:00.000Z",
        )

        row = _build_v6_row(
            eval_run_id=eval_run_id,
            submission_id=sub_id,
            hotkey=hotkey,
            solve_rank=ledger.solve_rank,
            w_c=w_c,
            signer=signer,
        )
        await repo.insert_ranked_eval_run(
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
        )

        # Fetch through list_eval_runs_recent (same path the feed handler uses).
        since = datetime(2026, 5, 1, tzinfo=UTC)
        runs = await repo.list_eval_runs_recent(
            conn, since=since, include_v3=True, include_task_families=True
        )
        assert runs, "expected at least one row from list_eval_runs_recent"
        run = next((r for r in runs if r["id"] == eval_run_id), None)
        assert run is not None, f"eval_run {eval_run_id} not found in feed"

        # Build the sub dict the same way reads.py does (from the joined sub).
        cur2 = await conn.execute(
            "SELECT id, display_name, miner_hotkey, card_id, logo_url "
            "FROM agent_submissions WHERE id = ?",
            (sub_id,),
        )
        sub_row = await cur2.fetchone()
        assert sub_row is not None
        sub = {
            "id": sub_row[0],
            "display_name": sub_row[1],
            "miner_hotkey": sub_row[2],
            "card_id": sub_row[3],
            "logo_url": sub_row[4],
        }

        served = _eval_run_to_output(run, sub)

        assert served["eval_output_schema_version"] == 6, (
            f"expected eval_output_schema_version=6, got {served['eval_output_schema_version']}"
        )
        missing = _V6_REQUIRED_KEYS - set(served)
        assert not missing, f"served item missing v6 keys: {sorted(missing)}"

        # Spot-check types that must match the signed payload exactly.
        assert isinstance(served["solved"], bool), "solved must be bool"
        assert isinstance(served["solve_rank"], int), "solve_rank must be int"
        assert isinstance(served["challenge_value"], float), "challenge_value must be float"
        assert isinstance(served["operator"], str), "operator must be str"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Gold test: sign → store → serve → verify_eval_output_signature must pass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v6_signature_round_trip(tmp_path) -> None:
    """Gold signature test: end-to-end sign→store→serve→verify must succeed.

    This is the authoritative regression gate: if the served item's 18 signed
    fields are not byte-identical to what was signed, verify_eval_output_signature
    raises PullVerificationError.
    """
    conn = await _conn(tmp_path)
    # Generate a fresh key pair — simulates the publisher's eval signing key.
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    signer = EvalSigner(sk)

    sub_id = "sub-v6-sig-1"
    hotkey = "5V6SigMiner"
    eval_run_id = "v6-sig-001"
    w_c = 4.0
    try:
        await _seed_submission(conn, sub_id=sub_id, hotkey=hotkey)
        ledger = await repo.record_ranked_solve(
            conn,
            family_id=FAMILY,
            challenge_id=CHALLENGE,
            miner_hotkey=hotkey,
            eval_run_id=eval_run_id,
            weighted_score=1.0,
            solved_at_iso="2026-05-29T20:00:00.000Z",
        )

        row = _build_v6_row(
            eval_run_id=eval_run_id,
            submission_id=sub_id,
            hotkey=hotkey,
            solve_rank=ledger.solve_rank,
            w_c=w_c,
            signer=signer,
        )
        await repo.insert_ranked_eval_run(
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
        )

        # Re-read via list_eval_runs_recent — the same path the feed handler uses.
        since = datetime(2026, 5, 1, tzinfo=UTC)
        runs = await repo.list_eval_runs_recent(
            conn, since=since, include_v3=True, include_task_families=True
        )
        assert runs, "expected at least one row from list_eval_runs_recent"
        run = next((r for r in runs if r["id"] == eval_run_id), None)
        assert run is not None, f"eval_run {eval_run_id} not found in feed"

        cur2 = await conn.execute(
            "SELECT id, display_name, miner_hotkey, card_id, logo_url "
            "FROM agent_submissions WHERE id = ?",
            (sub_id,),
        )
        sub_row = await cur2.fetchone()
        assert sub_row is not None
        sub = {
            "id": sub_row[0],
            "display_name": sub_row[1],
            "miner_hotkey": sub_row[2],
            "card_id": sub_row[3],
            "logo_url": sub_row[4],
        }

        served = _eval_run_to_output(run, sub)

        # This is the gold assertion: the validator's verifier must accept.
        # PullVerificationError is raised on any mismatch.
        verify_eval_output_signature(served, pk)

        # Belt-and-suspenders: confirm schema_version is 6 in the served item.
        assert served["eval_output_schema_version"] == 6
        missing = _V6_REQUIRED_KEYS - set(served)
        assert not missing, f"served item missing v6 keys: {sorted(missing)}"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Test: unknown SAT-era schema raises ValueError (fail-loud sentinel)
# ---------------------------------------------------------------------------


def test_eval_run_to_output_unknown_sat_schema_raises() -> None:
    """A schema_version >= 5 with no matching branch must raise, not fall through."""
    run = {
        "id": "test-unknown-schema",
        "submission_id": "sub-x",
        "task_json": {},
        "output_card_json": {},
        "output_card_hash": "a" * 64,
        "score_parts": {},
        "weighted_score": 1.0,
        "ran_at": "2026-05-30T00:00:00.000Z",
        "cathedral_signature": "b64:stub",
        "eval_output_schema_version": 99,  # future unknown version
        "merkle_epoch": None,
    }
    sub = {
        "id": "sub-x",
        "display_name": "Test Miner",
        "miner_hotkey": "5TestMiner",
        "card_id": None,
        "logo_url": None,
    }
    with pytest.raises(ValueError, match="no branch for SAT-era schema_version=99"):
        _eval_run_to_output(run, sub)


def test_eval_run_to_output_schema_4_raises() -> None:
    """schema_version=4 (>= 5 check boundary below) should NOT raise — it falls
    through to the legacy v1 card path, which is correct for schema < 5."""
    # schema_version 4 is < 5 so it uses the legacy card path — no error.
    run = {
        "id": "test-schema-4",
        "submission_id": "sub-x",
        "task_json": {},
        "output_card_json": {},
        "output_card_hash": "a" * 64,
        "score_parts": {},
        "weighted_score": 0.5,
        "ran_at": "2026-05-30T00:00:00.000Z",
        "cathedral_signature": "b64:stub",
        "eval_output_schema_version": 4,
        "merkle_epoch": None,
        "eval_card_excerpt": None,
        "eval_artifact_manifest_hash": None,
        "eval_artifact_bundle_url": None,
        "eval_artifact_manifest_url": None,
        "output_card_hash": "a" * 64,
        "polaris_verified": 0,
        "polaris_attestation": None,
    }
    sub = {
        "id": "sub-x",
        "display_name": "Test Miner",
        "miner_hotkey": "5TestMiner",
        "card_id": "some-card",
        "logo_url": None,
    }
    # Should not raise — falls through to legacy v1 card shape.
    result = _eval_run_to_output(run, sub)
    assert "id" in result
