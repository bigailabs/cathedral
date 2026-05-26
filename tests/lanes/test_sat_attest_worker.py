"""PR5: SAT async-attest worker pass + fail paths.

The worker drains ``attestation_status='pending'`` rows in ``eval_runs``,
calls a runner's ``attest`` method, and on success marks the already
signed row as attested. On failure, records the audit failure without
revoking the mathematically valid solve.

These tests inject a fake runner so we don't open real SSH connections.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from cathedral.eval.sat_attest_worker import (
    _AttestError,
    run_sat_attest_loop,
)
from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as LOCK_SCHEMA
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    ChallengeRecord,
    SqliteChallengeSource,
    ensure_sqlite_challenge_source_schema,
)
from cathedral.validator.db import connect

_FAMILY = "synthetic_boolean_v1"
_CHALLENGE = "pr5-attest-test"


class _PassingFakeRunner:
    """Runner whose ``attest`` always succeeds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def attest(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        return None


class _FailingFakeRunner:
    """Runner whose ``attest`` always raises _AttestError."""

    def __init__(self, reason: str = "ssh_auth_failed") -> None:
        self.reason = reason

    async def attest(self, **kwargs: Any) -> None:
        raise _AttestError(self.reason)


async def _setup_db(db_path: str) -> aiosqlite.Connection:
    """Open a publisher SQLite and seed the minimal state for attest."""
    conn = await connect(db_path)
    await ensure_sqlite_challenge_source_schema(conn)
    await conn.executescript(LOCK_SCHEMA)
    await conn.commit()

    # Seed card_definitions for FK on agent_submissions.
    await conn.execute(
        "INSERT OR IGNORE INTO card_definitions ("
        "id, display_name, jurisdiction, topic, description, "
        "eval_spec_md, source_pool, task_templates, scoring_rubric, "
        "refresh_cadence_hours, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            _FAMILY,
            "SAT",
            "global",
            "synthetic-boolean",
            "test",
            "test",
            "[]",
            "[]",
            "{}",
            24,
            "active",
            "2026-05-26T00:00:00.000Z",
            "2026-05-26T00:00:00.000Z",
        ),
    )
    await conn.commit()

    # Seed a locked challenge.
    source = SqliteChallengeSource(conn)
    await source.upsert(
        ChallengeRecord(
            challenge_id=_CHALLENGE,
            family_id=_FAMILY,
            tier=1,
            cnf_text="p cnf 1 1\n1 0\n",
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={"cnf_sha256": "x" * 64, "num_vars": 1, "num_clauses": 1},
        ),
        now_iso="2026-05-26T00:00:00.000Z",
        overwrite_status=True,
    )

    # Seed an agent_submission in the older rollout pending status; the worker
    # should still promote it cleanly.
    sub_id = "sub-attest-1"
    await conn.execute(
        """
        INSERT INTO agent_submissions (
            id, miner_hotkey, card_id, bundle_blob_key, bundle_hash,
            bundle_size_bytes, encryption_key_id, bundle_signature,
            display_name, metadata_fingerprint, similarity_check_passed,
            submitted_at, status, attestation_mode, discovery_only,
            ssh_host, ssh_port, ssh_user
        ) VALUES (
            ?, ?, ?, '', 'h', 0, '', 'sig', 'tester', '', 1,
            '2026-05-26T00:00:01.000Z', 'valid_attestation_pending',
            'ssh-probe', 0, 'host.example.com', 22, 'cathedral'
        )
        """,
        (sub_id, "hotkey-1", _FAMILY),
    )

    # Seed a pending eval_run + matching winner row, the way the
    # solve-on-submit path would have produced.
    eval_id = "eval-attest-1"
    await conn.execute(
        """
        INSERT INTO eval_runs (
            id, submission_id, epoch, round_index, polaris_agent_id,
            polaris_run_id, task_json, output_card_json, output_card_hash,
            score_parts, weighted_score, ran_at, duration_ms, errors,
            cathedral_signature, eval_output_schema_version, attestation_status
        ) VALUES (?, ?, 0, 0, '', '', ?, '{}', '', '{}', 1.0, ?, 0,
                  NULL, 'signed-direct-row', 5, 'pending')
        """,
        (
            eval_id,
            sub_id,
            json.dumps({"challenge_id": _CHALLENGE, "cnf_sha256": "x" * 64}),
            "2026-05-26T00:00:02.000Z",
        ),
    )
    await conn.execute(
        "INSERT INTO lane_challenge_winners ("
        "family_id, challenge_id, miner_hotkey, eval_run_id, "
        "weighted_score, won_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
        (_FAMILY, _CHALLENGE, "hotkey-1", eval_id, 1.0, "2026-05-26T00:00:02.000Z"),
    )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_attest_pass_promotes_to_attested(tmp_path: Path) -> None:
    conn = await _setup_db(str(tmp_path / "attest-pass.db"))
    try:
        runner = _PassingFakeRunner()
        signer = EvalSigner.from_env_hex("00" * 32)
        stop = asyncio.Event()
        lock = asyncio.Lock()

        # Run the loop as a task so we can cancel/stop it cleanly.
        loop_task = asyncio.create_task(
            run_sat_attest_loop(
                db=conn,
                signer=signer,
                db_write_lock=lock,
                stop=stop,
                runner_factory=lambda: runner,
                poll_interval_secs=0.05,
                max_concurrent=1,
            )
        )
        # Allow one full poll tick to run the attest.
        await asyncio.sleep(0.2)
        stop.set()
        try:
            await asyncio.wait_for(loop_task, timeout=2.0)
        except TimeoutError:
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass

        # Assert the runner saw exactly one call.
        assert len(runner.calls) == 1
        assert runner.calls[0]["ssh_host"] == "host.example.com"
        # Assert the eval_run was promoted.
        cur = await conn.execute(
            "SELECT attestation_status, cathedral_signature "
            "FROM eval_runs WHERE id = 'eval-attest-1'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "attested"
        assert row[1] is not None and len(row[1]) > 0
        # Assert the submission flipped to ranked.
        cur = await conn.execute(
            "SELECT status FROM agent_submissions WHERE id = 'sub-attest-1'"
        )
        sub_row = await cur.fetchone()
        assert sub_row is not None and sub_row[0] == "ranked"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_attest_fail_records_audit_failure_without_revoking(tmp_path: Path) -> None:
    conn = await _setup_db(str(tmp_path / "attest-fail.db"))
    try:
        runner = _FailingFakeRunner(reason="ssh_auth_failed: PermissionDenied")
        signer = EvalSigner.from_env_hex("11" * 32)
        stop = asyncio.Event()
        lock = asyncio.Lock()

        async def _stop_after_one_pass() -> None:
            await asyncio.sleep(0.1)
            stop.set()

        await asyncio.gather(
            run_sat_attest_loop(
                db=conn,
                signer=signer,
                db_write_lock=lock,
                stop=stop,
                runner_factory=lambda: runner,
                poll_interval_secs=0.05,
                max_concurrent=1,
            ),
            _stop_after_one_pass(),
        )

        # Eval row is now 'failed', but score and winner stay intact.
        cur = await conn.execute(
            "SELECT attestation_status, weighted_score, errors "
            "FROM eval_runs WHERE id = 'eval-attest-1'"
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == "failed"
        assert row[1] == 1.0
        assert "attest_failed" in row[2]
        # Winner row remains.
        cur = await conn.execute(
            "SELECT COUNT(*) FROM lane_challenge_winners "
            "WHERE eval_run_id = 'eval-attest-1'"
        )
        count_row = await cur.fetchone()
        assert count_row is not None and int(count_row[0]) == 1
        # Submission remains ranked/payable.
        cur = await conn.execute(
            "SELECT status, current_score FROM agent_submissions WHERE id = 'sub-attest-1'"
        )
        sub_row = await cur.fetchone()
        assert sub_row is not None
        assert sub_row[0] == "valid_attestation_pending"
        assert sub_row[1] is None
    finally:
        await conn.close()
