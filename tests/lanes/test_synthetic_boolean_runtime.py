from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import blake3
import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval import orchestrator as orchestrator_module
from cathedral.eval.orchestrator import EvalOrchestrator
from cathedral.eval.polaris_runner import StubPolarisRunner
from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.lanes.challenge_lock import (
    SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA,
)
from cathedral.lanes.challenge_lock import InMemoryChallengeLock, SqliteChallengeLock
from cathedral.lanes.challenge_receipts import (
    RECEIPT_STATUS_EXPIRED,
    RECEIPT_STATUS_INVALID,
    RECEIPT_STATUS_UNVERIFIED,
    RECEIPT_STATUS_VALID,
    RECEIPT_STATUS_VERIFYING,
    SqliteChallengeReceiptStore,
)
from cathedral.lanes.challenge_receipts import (
    SQLITE_SCHEMA as CHALLENGE_RECEIPT_SCHEMA,
)
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)
from cathedral.lanes.challenge_source import (
    SQLITE_SCHEMA as CHALLENGE_SOURCE_SCHEMA,
)
from cathedral.lanes.contract import GenerateCtx
from cathedral.lanes.publisher import score_and_sign_task_family_stdout
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1
from cathedral.publisher import repository
from cathedral.publisher.app import PublisherContext
from cathedral.publisher.reads import _eval_run_to_output
from cathedral.storage.hippius import StubHippiusClient
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import (
    latest_pulled_score_per_hotkey,
    upsert_pulled_eval,
    verify_eval_output_signature,
)

_TEST_PUBLIC_BASE_URL = "https://api.cathedral.test"


class _Registry:
    pass


@dataclass
class _HermesResult:
    stdout: str
    duration_ms: int = 1
    trace: dict[str, Any] | None = None
    trace_bundle: object | None = None


class _FailingTerminalStatusReceiptStore(SqliteChallengeReceiptStore):
    async def update_status(self, *args: Any, **kwargs: Any):
        if kwargs.get("status") in {RECEIPT_STATUS_VALID, RECEIPT_STATUS_INVALID}:
            raise RuntimeError("terminal status failed")
        return await super().update_status(*args, **kwargs)


class _RaceLostChallengeLock:
    def __init__(self) -> None:
        self.try_lock_calls = 0

    async def get_winner(self, *, family_id: str, challenge_id: str) -> None:
        return None

    async def try_lock(self, **_kwargs: Any) -> None:
        self.try_lock_calls += 1
        return None


def test_receipt_answer_hash_never_parses_miner_stdout(monkeypatch) -> None:
    stdout = "malformed" + ("}" * 100_000)

    def fail_if_parse_attempted(_stdout: str) -> dict[str, Any]:
        raise AssertionError("receipt hashing must not parse stdout")

    monkeypatch.setattr(
        orchestrator_module,
        "extract_answer",
        fail_if_parse_attempted,
        raising=False,
    )

    assert orchestrator_module._receipt_answer_hash(stdout) == blake3.blake3(
        stdout.encode("utf-8")
    ).hexdigest()


class _SolvingRunner(StubPolarisRunner):
    """Test stub that pretends to be a miner.

    Under the CNF URL transport, the miner-facing surface no longer
    inlines the CNF body in ``public_input``: it carries ``cnf_url`` +
    ``cnf_sha256`` and the miner is expected to fetch the body. The
    stub records the URL + sha256 so tests can assert on what crosses
    the wire, and uses ``num_vars`` (still in ``public_input``) to
    fabricate a satisfying assignment without needing the body itself.
    """

    def __init__(self, *, received_at: list[str] | None = None) -> None:
        super().__init__()
        self.task_ids: list[str] = []
        self.cnf_urls: list[str] = []
        self.cnf_sha256s: list[str] = []
        self._received_at = list(received_at or [])
        self._counter = 0
        self.receipt_callback_started: list[asyncio.Event] = []
        self.receipt_callback_finished: list[asyncio.Event] = []

    async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
        problem = kwargs["problem"]
        self.task_ids.append(str(problem.task_id))
        public_input = problem.public_input
        self.cnf_urls.append(str(public_input["cnf_url"]))
        self.cnf_sha256s.append(str(public_input["cnf_sha256"]))
        num_vars = int(public_input["num_vars"])
        literals = " ".join(str(i) for i in range(1, num_vars + 1))
        stdout = f'```FINAL_ANSWER\n{{"dimacs_solution": "s SATISFIABLE\\nv {literals} 0\\n"}}\n```'
        receipt_callback = kwargs.get("receipt_callback")
        callback_index = self._counter
        received_at = (
            self._received_at.pop(0)
            if self._received_at
            else f"2026-05-20T12:00:{self._counter:02d}.000Z"
        )
        self._counter += 1
        if receipt_callback is not None:
            if callback_index < len(self.receipt_callback_started):
                self.receipt_callback_started[callback_index].set()
            await receipt_callback(stdout, received_at)
            if callback_index < len(self.receipt_callback_finished):
                self.receipt_callback_finished[callback_index].set()
        return _HermesResult(stdout=stdout, trace={})


class _UnsatisfyingRunner(_SolvingRunner):
    async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
        problem = kwargs["problem"]
        self.task_ids.append(str(problem.task_id))
        public_input = problem.public_input
        self.cnf_urls.append(str(public_input["cnf_url"]))
        self.cnf_sha256s.append(str(public_input["cnf_sha256"]))
        stdout = (
            "```FINAL_ANSWER\n"
            '{"dimacs_solution": "s SATISFIABLE\\nv -1 0\\n"}'
            "\n```"
        )
        receipt_callback = kwargs.get("receipt_callback")
        received_at = (
            self._received_at.pop(0)
            if self._received_at
            else f"2026-05-20T12:00:{self._counter:02d}.000Z"
        )
        self._counter += 1
        if receipt_callback is not None:
            await receipt_callback(stdout, received_at)
        return _HermesResult(stdout=stdout, trace={})


class _DelayedSolvingRunner(_SolvingRunner):
    def __init__(self, *, received_at: list[str], delays_by_submission: dict[str, float]) -> None:
        super().__init__(received_at=received_at)
        self._delays_by_submission = delays_by_submission

    async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
        problem = kwargs["problem"]
        self.task_ids.append(str(problem.task_id))
        public_input = problem.public_input
        self.cnf_urls.append(str(public_input["cnf_url"]))
        self.cnf_sha256s.append(str(public_input["cnf_sha256"]))
        num_vars = int(public_input["num_vars"])
        literals = " ".join(str(i) for i in range(1, num_vars + 1))
        stdout = f'```FINAL_ANSWER\n{{"dimacs_solution": "s SATISFIABLE\\nv {literals} 0\\n"}}\n```'
        receipt_callback = kwargs.get("receipt_callback")
        received_at = self._received_at.pop(0)
        if receipt_callback is not None:
            await receipt_callback(stdout, received_at)
        delay = self._delays_by_submission.get(str(kwargs["submission"]["id"]), 0.0)
        if delay:
            await asyncio.sleep(delay)
        return _HermesResult(stdout=stdout, trace={})


class _ScriptedDelayedRunner(_SolvingRunner):
    def __init__(
        self,
        *,
        received_at: list[str],
        delays_by_submission: dict[str, float],
        solutions_by_submission: dict[str, str],
    ) -> None:
        super().__init__(received_at=received_at)
        self._delays_by_submission = delays_by_submission
        self._solutions_by_submission = solutions_by_submission

    async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
        problem = kwargs["problem"]
        submission_id = str(kwargs["submission"]["id"])
        self.task_ids.append(str(problem.task_id))
        public_input = problem.public_input
        self.cnf_urls.append(str(public_input["cnf_url"]))
        self.cnf_sha256s.append(str(public_input["cnf_sha256"]))
        solution = self._solutions_by_submission[submission_id]
        stdout = f'```FINAL_ANSWER\n{{"dimacs_solution": {json.dumps(solution)}}}\n```'
        receipt_callback = kwargs.get("receipt_callback")
        received_at = self._received_at.pop(0)
        if receipt_callback is not None:
            await receipt_callback(stdout, received_at)
        delay = self._delays_by_submission.get(submission_id, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return _HermesResult(stdout=stdout, trace={})


async def _seed_submission(conn, *, submission_id: str, miner_hotkey: str) -> dict[str, Any]:
    existing = await repository.get_card_definition(conn, "eu-ai-act")
    if existing is None:
        await repository.insert_card_definition(
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

    await repository.insert_agent_submission(
        conn,
        id=submission_id,
        miner_hotkey=miner_hotkey,
        card_id="eu-ai-act",
        bundle_blob_key=f"bundles/{submission_id}.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name=f"Boolean Miner {miner_hotkey[-1]}",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=f"fp-{submission_id}",
        similarity_check_passed=True,
        rejection_reason=None,
        status="ranked",
        submitted_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        submitted_at_iso="2026-05-19T12:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    seeded = await repository.get_agent_submission(conn, submission_id)
    assert seeded is not None
    return seeded


async def _seed_sat_registration(
    conn,
    *,
    submission_id: str,
    miner_hotkey: str,
    status: str = "pending_check",
) -> dict[str, Any]:
    existing = await repository.get_card_definition(conn, "synthetic_boolean_v1")
    if existing is None:
        await repository.insert_card_definition(
            conn,
            id="synthetic_boolean_v1",
            display_name="Synthetic Boolean",
            jurisdiction="task-family",
            topic="SAT",
            description="SAT lane",
            eval_spec_md="spec",
            source_pool=[],
            task_templates=[],
            scoring_rubric={},
        )
    await repository.insert_agent_submission(
        conn,
        id=submission_id,
        miner_hotkey=miner_hotkey,
        card_id="synthetic_boolean_v1",
        bundle_blob_key="",
        bundle_hash="",
        bundle_size_bytes=0,
        encryption_key_id="",
        bundle_signature="",
        display_name=f"SAT Miner {miner_hotkey[-1]}",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=f"fp-{submission_id}",
        similarity_check_passed=True,
        rejection_reason=None,
        status=status,
        submitted_at=datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        submitted_at_iso="2026-05-20T12:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    seeded = await repository.get_agent_submission(conn, submission_id)
    assert seeded is not None
    return seeded


async def _store_valid_sat_receipt(
    receipt_store: SqliteChallengeReceiptStore,
    *,
    family_id: str,
    challenge_id: str,
    receipt_id: str,
    miner_hotkey: str,
    received_at_iso: str,
    signed: Any,
    epoch: int,
    round_index: int,
) -> Any:
    receipt = await receipt_store.record_receipt(
        family_id=family_id,
        challenge_id=challenge_id,
        submission_id=receipt_id,
        miner_hotkey=miner_hotkey,
        received_at_iso=received_at_iso,
        answer_hash=f"answer:{receipt_id}",
        recorded_at_iso=received_at_iso,
    )
    await receipt_store.update_status(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
        status=RECEIPT_STATUS_VERIFYING,
        now_iso=receipt.received_at_iso,
    )
    await receipt_store.attach_result(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
        eval_run_id=str(signed.row["id"]),
        signed_row=signed.row,
        trace_json={},
        duration_ms=1,
        epoch=epoch,
        round_index=round_index,
        now_iso=str(signed.row["ran_at"]),
    )
    await receipt_store.update_status(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
        status=RECEIPT_STATUS_VALID,
        now_iso=str(signed.row["ran_at"]),
        verifier_details_hash=str(signed.row["verifier_details_hash"]),
    )
    stored = await receipt_store.get(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
    )
    assert stored is not None
    return stored


@pytest.mark.asyncio
async def test_run_once_wires_cnf_url_token_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The test-friendly run_once path must match production token wiring."""
    from cathedral.publisher import app as publisher_app

    conn = await connect(str(tmp_path / "publisher.db"))
    captured: dict[str, Any] = {}

    class _CapturingOrchestrator:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def evaluate_one(self, submission: dict[str, Any]) -> None:
            raise AssertionError(f"unexpected eval call: {submission}")

        async def _on_retryable_failure(
            self,
            submission: dict[str, Any],
            log: Any,
            reason: str,
        ) -> None:
            raise AssertionError(f"unexpected retry handler call: {submission} {log} {reason}")

    try:
        monkeypatch.setenv("CATHEDRAL_PUBLIC_BASE_URL", _TEST_PUBLIC_BASE_URL)
        monkeypatch.setattr(orchestrator_module, "EvalOrchestrator", _CapturingOrchestrator)
        ctx = PublisherContext(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
        )
        monkeypatch.setattr(publisher_app, "_LATEST_CTX", ctx)

        advanced = await orchestrator_module._run_once_async()
        assert advanced == 0
        assert isinstance(captured["task_family_fetch_token_store"], SqliteFetchTokenStore)
        assert isinstance(captured["task_family_receipt_store"], SqliteChallengeReceiptStore)
        assert captured["db_write_lock"] is ctx.db_write_lock
        assert captured["public_base_url"] == _TEST_PUBLIC_BASE_URL
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_runtime_uses_one_active_formula_and_first_valid_lock(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        lock = SqliteChallengeLock(conn)
        tokens = SqliteFetchTokenStore(conn)
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=lock,
            task_family_fetch_token_store=tokens,
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        await orch._maybe_run_task_family_lanes(
            submission=sub_a,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
        )
        await orch._maybe_run_task_family_lanes(
            submission=sub_b,
            runner=runner,
            epoch=123,
            round_index=1,
            log=log,
        )

        assert runner.task_ids == ["active-boolean-001", "active-boolean-002"]
        # CNF body no longer crosses the public wire; the URL transport
        # carries cnf_url + cnf_sha256 instead. The URLs should reference
        # the configured public base and the per-challenge id; sha256s
        # should match the CNF bodies the publisher seeded.
        import hashlib

        expected_sha = [
            hashlib.sha256(b"p cnf 1 1\n1 0\n").hexdigest(),
            hashlib.sha256(b"p cnf 2 2\n1 0\n2 0\n").hexdigest(),
        ]
        assert runner.cnf_sha256s == expected_sha
        assert all(url.startswith(_TEST_PUBLIC_BASE_URL) for url in runner.cnf_urls)
        assert "active-boolean-001" in runner.cnf_urls[0]
        assert "active-boolean-002" in runner.cnf_urls[1]
        assert "?t=" in runner.cnf_urls[0]
        assert "?t=" in runner.cnf_urls[1]

        first = (await repository.list_eval_runs_for_submission(conn, "sub-a"))[0]
        second = (await repository.list_eval_runs_for_submission(conn, "sub-b"))[0]
        assert first["weighted_score"] == pytest.approx(1.0)
        assert first["errors"] is None
        assert second["weighted_score"] == pytest.approx(1.0)
        assert second["errors"] is None
        assert first["task_json"]["task_id_public"] != "active-boolean-001"
        assert second["task_json"]["task_id_public"] != "active-boolean-002"
        assert "task_id" not in first["task_json"]
        assert "task_id" not in second["task_json"]
        assert "p cnf" not in str(first["output_card_json"])
        assert "SATISFIABLE" not in str(first["output_card_json"])

        winner = await lock.get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerA"
        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_LOCKED),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_evaluate_one_runs_sat_registration_without_legacy_card_bundle(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_sat_registration(
            conn, submission_id="sat-sub-a", miner_hotkey="5SatMinerA"
        )
        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "sat-registration-test"},
            )
        )
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        await orch.evaluate_one(sub)

        refreshed = await repository.get_agent_submission(conn, "sat-sub-a")
        assert refreshed is not None
        assert refreshed["status"] == "ranked"
        rows = await repository.list_eval_runs_for_submission(conn, "sat-sub-a")
        assert [(row["eval_output_schema_version"], row["weighted_score"]) for row in rows] == [
            (5, 1.0)
        ]
        assert runner.task_ids == ["active-boolean-001"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_repeated_unresolved_attempts_get_distinct_receipts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        runner = _UnsatisfyingRunner(
            received_at=[
                "2026-05-20T12:00:00.000Z",
                "2026-05-20T12:00:01.000Z",
            ]
        )
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        await orch._maybe_run_task_family_lanes(
            submission=sub_a,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
        )
        await orch._maybe_run_task_family_lanes(
            submission=sub_a,
            runner=runner,
            epoch=123,
            round_index=1,
            log=log,
        )

        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert len(receipts) == 2
        assert {receipt.status for receipt in receipts} == {RECEIPT_STATUS_INVALID}
        assert len({receipt.submission_id for receipt in receipts}) == 2
        assert all(receipt.submission_id != "sub-a" for receipt in receipts)
        assert all(receipt.submission_id.startswith("sub-a:attempt:") for receipt in receipts)
        assert all(receipt.signed_row is not None for receipt in receipts)
        assert {receipt.signed_row["agent_id"] for receipt in receipts if receipt.signed_row} == {
            "sub-a"
        }

        rows = await repository.list_eval_runs_for_submission(conn, "sub-a")
        assert len(rows) == 2
        assert all(row["weighted_score"] == pytest.approx(0.0) for row in rows)
        assert [row["errors"] for row in rows] == [
            ["solution_unsatisfied"],
            ["solution_unsatisfied"],
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_batch_snapshot_keeps_poll_batch_on_one_formula(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=InMemoryChallengeLock(),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await orch._maybe_run_task_family_lanes(
            submission=sub_a,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
            problem_overrides=snapshot,
        )
        await orch._maybe_run_task_family_lanes(
            submission=sub_b,
            runner=runner,
            epoch=123,
            round_index=1,
            log=log,
            problem_overrides=snapshot,
        )

        assert runner.task_ids == ["active-boolean-001", "active-boolean-001"]
        # Batch snapshot: both miners in the same poll batch see the same
        # CNF URL and the same fetch token. Snapshot must not re-mint
        # between miners.
        assert runner.cnf_urls[0] == runner.cnf_urls[1]
        assert runner.cnf_sha256s[0] == runner.cnf_sha256s[1]

        rows_a = await repository.list_eval_runs_for_submission(conn, "sub-a")
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        scored = rows_a + rows_b
        assert sorted(row["weighted_score"] for row in scored) == [0.0, 1.0]
        assert [row.get("errors") for row in scored].count(["challenge_already_locked"]) == 1
        assert [row.get("errors") for row in scored].count(None) == 1

        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_first_submitted_valid_wins_when_later_finishes_first(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        received_base = datetime.now(UTC)
        runner = _DelayedSolvingRunner(
            received_at=[
                orchestrator_module._ms_iso(received_base),
                orchestrator_module._ms_iso(received_base + timedelta(seconds=1)),
            ],
            delays_by_submission={"sub-a": 0.05, "sub-b": 0.0},
        )
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await asyncio.gather(
            orch._maybe_run_task_family_lanes(
                submission=sub_a,
                runner=runner,
                epoch=123,
                round_index=0,
                log=log,
                problem_overrides=snapshot,
            ),
            orch._maybe_run_task_family_lanes(
                submission=sub_b,
                runner=runner,
                epoch=123,
                round_index=1,
                log=log,
                problem_overrides=snapshot,
            ),
        )

        rows_a = await repository.list_eval_runs_for_submission(conn, "sub-a")
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert rows_a[0]["weighted_score"] == pytest.approx(1.0)
        assert rows_a[0]["errors"] is None
        assert rows_b[0]["weighted_score"] == pytest.approx(0.0)
        assert rows_b[0]["errors"] == ["challenge_already_locked"]

        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerA"

        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_invalid_receipt_unblocks_waiting_valid_winner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        received_base = datetime.now(UTC)
        runner = _ScriptedDelayedRunner(
            received_at=[
                orchestrator_module._ms_iso(received_base),
                orchestrator_module._ms_iso(received_base + timedelta(seconds=1)),
            ],
            delays_by_submission={"sub-a": 0.05, "sub-b": 0.0},
            solutions_by_submission={
                "sub-a": "s SATISFIABLE\nv -1 0\n",
                "sub-b": "s SATISFIABLE\nv 1 0\n",
            },
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await asyncio.gather(
            orch._maybe_run_task_family_lanes(
                submission=sub_a,
                runner=runner,
                epoch=123,
                round_index=0,
                log=log,
                problem_overrides=snapshot,
            ),
            orch._maybe_run_task_family_lanes(
                submission=sub_b,
                runner=runner,
                epoch=123,
                round_index=1,
                log=log,
                problem_overrides=snapshot,
            ),
        )

        rows_a = await repository.list_eval_runs_for_submission(conn, "sub-a")
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0]["weighted_score"] == pytest.approx(0.0)
        assert rows_a[0]["errors"] == ["solution_unsatisfied"]
        assert rows_b[0]["weighted_score"] == pytest.approx(1.0)
        assert rows_b[0]["errors"] is None

        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [
            RECEIPT_STATUS_INVALID,
            RECEIPT_STATUS_VALID,
        ]

        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerB"

        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_reconcile_expires_stale_blocker_and_finalizes_valid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")
    # Test seeds a receipt at now-120s expecting expiration. Pin the timeout
    # short so this test stays independent of the production default.
    monkeypatch.setattr(
        "cathedral.lanes.synthetic_boolean_v1.DEFAULT_TIME_LIMIT_SECONDS", 60
    )
    monkeypatch.setattr(
        "cathedral.eval.orchestrator.DEFAULT_TIME_LIMIT_SECONDS", 60, raising=False
    )

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        record = ChallengeRecord(
            challenge_id="active-boolean-001",
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="p cnf 1 1\n1 0\n",
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={"source": "toy-runtime-test"},
        )
        await source.upsert(record)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        signer = EvalSigner(Ed25519PrivateKey.generate())
        tokens = SqliteFetchTokenStore(conn)
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=tokens,
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )
        announced = await orch._announce_synthetic_boolean_problem(
            record,
            log=structlog.get_logger("test"),
            family_id="synthetic_boolean_v1",
        )
        assert announced is not None
        problem, hidden = announced
        now = datetime.now(UTC)

        await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
            submission_id="sub-a:attempt:stale",
            miner_hotkey="5MinerA",
            received_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=120)),
            answer_hash="stale-answer",
            recorded_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=120)),
        )
        valid_receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
            submission_id="sub-b:attempt:valid",
            miner_hotkey="5MinerB",
            received_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=30)),
            answer_hash="valid-answer",
            recorded_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=30)),
        )
        signed = score_and_sign_task_family_stdout(
            lane=SyntheticBooleanV1(),
            problem=problem,
            hidden=hidden,
            submission_row=sub_b,
            stdout='```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```',
            ran_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=20)),
            signer=signer,
            eval_run_id="run-waiting-valid",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        await receipt_store.update_status(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=valid_receipt.received_at_iso,
        )
        await receipt_store.attach_result(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            eval_run_id=str(signed.row["id"]),
            signed_row=signed.row,
            trace_json={},
            duration_ms=1,
            epoch=123,
            round_index=1,
            now_iso=str(signed.row["ran_at"]),
        )
        await receipt_store.update_status(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            status=RECEIPT_STATUS_VALID,
            now_iso=str(signed.row["ran_at"]),
            verifier_details_hash=str(signed.row["verifier_details_hash"]),
        )
        assert await receipt_store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        ) is None

        finalized = await orch.reconcile_sat_receipts(log=structlog.get_logger("test"))

        assert finalized == 1
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [
            RECEIPT_STATUS_EXPIRED,
            RECEIPT_STATUS_VALID,
        ]
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_b) == 1
        assert rows_b[0]["weighted_score"] == pytest.approx(1.0)
        assert rows_b[0]["errors"] is None

        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerB"
        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_reconcile_expires_stale_verifier_and_finalizes_valid(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        record = ChallengeRecord(
            challenge_id="active-boolean-001",
            family_id="synthetic_boolean_v1",
            tier=0,
            cnf_text="p cnf 1 1\n1 0\n",
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata={"source": "toy-runtime-test"},
        )
        await source.upsert(record)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        signer = EvalSigner(Ed25519PrivateKey.generate())
        tokens = SqliteFetchTokenStore(conn)
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=tokens,
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )
        announced = await orch._announce_synthetic_boolean_problem(
            record,
            log=structlog.get_logger("test"),
            family_id="synthetic_boolean_v1",
        )
        assert announced is not None
        problem, hidden = announced
        now = datetime.now(UTC)

        stale_receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
            submission_id="sub-a:attempt:stale",
            miner_hotkey="5MinerA",
            received_at_iso=orchestrator_module._ms_iso(now - timedelta(minutes=20)),
            answer_hash="stale-answer",
            recorded_at_iso=orchestrator_module._ms_iso(now - timedelta(minutes=20)),
        )
        await receipt_store.update_status(
            family_id=stale_receipt.family_id,
            challenge_id=stale_receipt.challenge_id,
            submission_id=stale_receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=orchestrator_module._ms_iso(now - timedelta(minutes=19)),
        )
        valid_receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
            submission_id="sub-b:attempt:valid",
            miner_hotkey="5MinerB",
            received_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=30)),
            answer_hash="valid-answer",
            recorded_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=30)),
        )
        signed = score_and_sign_task_family_stdout(
            lane=SyntheticBooleanV1(),
            problem=problem,
            hidden=hidden,
            submission_row=sub_b,
            stdout='```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```',
            ran_at_iso=orchestrator_module._ms_iso(now - timedelta(seconds=20)),
            signer=signer,
            eval_run_id="run-waiting-valid",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        await receipt_store.update_status(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=valid_receipt.received_at_iso,
        )
        await receipt_store.attach_result(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            eval_run_id=str(signed.row["id"]),
            signed_row=signed.row,
            trace_json={},
            duration_ms=1,
            epoch=123,
            round_index=1,
            now_iso=str(signed.row["ran_at"]),
        )
        await receipt_store.update_status(
            family_id=valid_receipt.family_id,
            challenge_id=valid_receipt.challenge_id,
            submission_id=valid_receipt.submission_id,
            status=RECEIPT_STATUS_VALID,
            now_iso=str(signed.row["ran_at"]),
            verifier_details_hash=str(signed.row["verifier_details_hash"]),
        )
        assert await receipt_store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        ) is None

        finalized = await orch.reconcile_sat_receipts(log=structlog.get_logger("test"))

        assert finalized == 1
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [
            RECEIPT_STATUS_EXPIRED,
            RECEIPT_STATUS_VALID,
        ]
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_b) == 1
        assert rows_b[0]["weighted_score"] == pytest.approx(1.0)
        assert rows_b[0]["errors"] is None

        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerB"
        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_LOCKED),
            ("active-boolean-002", CHALLENGE_STATUS_ACTIVE),
        ]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_marks_receipt_verifying_before_scoring(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    db_path = tmp_path / "publisher.db"
    conn = await connect(str(db_path))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        original_score = orchestrator_module.score_and_sign_task_family_stdout
        statuses_during_score: list[str] = []

        def score_after_simulated_expiry(*args: Any, **kwargs: Any):
            problem = kwargs["problem"]
            now_iso = orchestrator_module._ms_iso(datetime.now(UTC))
            with sqlite3.connect(db_path) as raw:
                raw.execute(
                    "UPDATE lane_challenge_receipts "
                    "SET status = ?, rejection_reason = ?, updated_at_iso = ?, "
                    "resolved_at_iso = ? "
                    "WHERE challenge_id = ? AND status = ?",
                    (
                        RECEIPT_STATUS_EXPIRED,
                        "receipt_timed_out",
                        now_iso,
                        now_iso,
                        problem.task_id,
                        RECEIPT_STATUS_UNVERIFIED,
                    ),
                )
                raw.commit()
                row = raw.execute(
                    "SELECT status FROM lane_challenge_receipts WHERE challenge_id = ?",
                    (problem.task_id,),
                ).fetchone()
            statuses_during_score.append(str(row[0]) if row is not None else "missing")
            return original_score(*args, **kwargs)

        monkeypatch.setattr(
            orchestrator_module,
            "score_and_sign_task_family_stdout",
            score_after_simulated_expiry,
        )

        received_base = datetime.now(UTC) - timedelta(minutes=5)
        runner = _SolvingRunner(
            received_at=[orchestrator_module._ms_iso(received_base)],
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await orch._maybe_run_task_family_lanes(
            submission=sub,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
            problem_overrides=snapshot,
        )

        assert statuses_during_score == [RECEIPT_STATUS_VERIFYING]
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [RECEIPT_STATUS_VALID]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_marks_receipt_verifying_before_post_run_collection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        receipt_store = SqliteChallengeReceiptStore(conn)
        statuses_after_expiry_tick: list[str] = []
        orch: EvalOrchestrator

        class _PostStdoutCollectionRunner(_SolvingRunner):
            async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
                problem = kwargs["problem"]
                self.task_ids.append(str(problem.task_id))
                public_input = problem.public_input
                self.cnf_urls.append(str(public_input["cnf_url"]))
                self.cnf_sha256s.append(str(public_input["cnf_sha256"]))
                stdout = (
                    "```FINAL_ANSWER\n"
                    '{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'
                    "\n```"
                )
                receipt_callback = kwargs.get("receipt_callback")
                assert receipt_callback is not None
                await receipt_callback(
                    stdout,
                    orchestrator_module._ms_iso(datetime.now(UTC) - timedelta(minutes=5)),
                )

                await orch._expire_stale_sat_receipts_for_challenge(
                    receipt_store=receipt_store,
                    family_id="synthetic_boolean_v1",
                    challenge_id=str(problem.task_id),
                    problem=problem,
                    now_iso=orchestrator_module._ms_iso(datetime.now(UTC)),
                )
                receipts = await receipt_store.list_for_challenge(
                    family_id="synthetic_boolean_v1",
                    challenge_id=str(problem.task_id),
                )
                statuses_after_expiry_tick.extend(receipt.status for receipt in receipts)
                return _HermesResult(stdout=stdout, trace={})

        runner = _PostStdoutCollectionRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await orch._maybe_run_task_family_lanes(
            submission=sub,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
            problem_overrides=snapshot,
        )

        assert statuses_after_expiry_tick == [RECEIPT_STATUS_VERIFYING]
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [RECEIPT_STATUS_VALID]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_refreshes_receipt_heartbeat_during_post_run_work(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")
    monkeypatch.setattr(
        orchestrator_module,
        "_SAT_VERIFYING_STALE_TIMEOUT",
        timedelta(milliseconds=60),
    )
    monkeypatch.setattr(
        orchestrator_module,
        "_SAT_RECEIPT_HEARTBEAT_INTERVAL",
        timedelta(milliseconds=10),
    )

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        receipt_store = SqliteChallengeReceiptStore(conn)
        heartbeat_observed: list[bool] = []
        statuses_after_expiry_tick: list[str] = []
        orch: EvalOrchestrator

        class _SlowPostStdoutRunner(_SolvingRunner):
            async def run_task_family_challenge(self, **kwargs: Any) -> _HermesResult:
                problem = kwargs["problem"]
                stdout = (
                    "```FINAL_ANSWER\n"
                    '{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}'
                    "\n```"
                )
                receipt_callback = kwargs.get("receipt_callback")
                assert receipt_callback is not None
                await receipt_callback(
                    stdout,
                    orchestrator_module._ms_iso(datetime.now(UTC) - timedelta(minutes=5)),
                )

                receipt = (await receipt_store.list_for_challenge(
                    family_id="synthetic_boolean_v1",
                    challenge_id=str(problem.task_id),
                ))[0]
                first_heartbeat = receipt.updated_at_iso
                observed = False
                deadline = asyncio.get_running_loop().time() + 0.5
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                    refreshed = await receipt_store.get(
                        family_id="synthetic_boolean_v1",
                        challenge_id=str(problem.task_id),
                        submission_id=receipt.submission_id,
                    )
                    if refreshed is not None and refreshed.updated_at_iso != first_heartbeat:
                        observed = True
                        break
                heartbeat_observed.append(observed)

                await orch._expire_stale_sat_receipts_for_challenge(
                    receipt_store=receipt_store,
                    family_id="synthetic_boolean_v1",
                    challenge_id=str(problem.task_id),
                    problem=problem,
                    now_iso=orchestrator_module._ms_iso(datetime.now(UTC)),
                )
                receipts = await receipt_store.list_for_challenge(
                    family_id="synthetic_boolean_v1",
                    challenge_id=str(problem.task_id),
                )
                statuses_after_expiry_tick.extend(receipt.status for receipt in receipts)
                return _HermesResult(stdout=stdout, trace={})

        runner = _SlowPostStdoutRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await orch._maybe_run_task_family_lanes(
            submission=sub,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
            problem_overrides=snapshot,
        )

        assert heartbeat_observed == [True]
        assert statuses_after_expiry_tick == [RECEIPT_STATUS_VERIFYING]
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [RECEIPT_STATUS_VALID]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_file_backed_scoring_runs_off_event_loop(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        cnf_text = "p cnf 2 2\n1 0\n2 0\n"
        cnf_path = tmp_path / "active.cnf"
        cnf_path.write_text(cnf_text, encoding="utf-8")
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-file-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="",
                cnf_path=str(cnf_path),
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={
                    "source": "toy-runtime-test",
                    "storage": "file",
                    "cnf_sha256": hashlib.sha256(cnf_text.encode("utf-8")).hexdigest(),
                    "num_vars": 2,
                    "num_clauses": 2,
                },
            )
        )

        event_loop_thread_id = threading.get_ident()
        score_thread_ids: list[int] = []
        original_score = orchestrator_module.score_and_sign_task_family_stdout

        def recording_score(*args: Any, **kwargs: Any):
            score_thread_ids.append(threading.get_ident())
            return original_score(*args, **kwargs)

        monkeypatch.setattr(
            orchestrator_module,
            "score_and_sign_task_family_stdout",
            recording_score,
        )

        receipt_store = SqliteChallengeReceiptStore(conn)
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        await orch._maybe_run_task_family_lanes(
            submission=sub,
            runner=runner,
            epoch=123,
            round_index=0,
            log=log,
            problem_overrides=snapshot,
        )

        assert score_thread_ids
        assert all(thread_id != event_loop_thread_id for thread_id in score_thread_ids)
        receipts = await receipt_store.list_for_challenge(
            family_id="synthetic_boolean_v1",
            challenge_id="active-file-boolean-001",
        )
        assert [receipt.status for receipt in receipts] == [RECEIPT_STATUS_VALID]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_serializes_winner_finalization_on_shared_connection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )

        original_persist = orchestrator_module.persist_task_family_result
        winner_transaction_open = asyncio.Event()
        release_winner_transaction = asyncio.Event()
        blocked_once = False
        second_callback_started = asyncio.Event()
        second_callback_finished = asyncio.Event()

        async def gated_persist(*args: Any, **kwargs: Any) -> None:
            nonlocal blocked_once
            await original_persist(*args, **kwargs)
            if kwargs.get("commit") is False and not blocked_once:
                blocked_once = True
                winner_transaction_open.set()
                await release_winner_transaction.wait()

        monkeypatch.setattr(orchestrator_module, "persist_task_family_result", gated_persist)

        received_base = datetime.now(UTC)
        runner = _SolvingRunner(
            received_at=[
                orchestrator_module._ms_iso(received_base),
                orchestrator_module._ms_iso(received_base + timedelta(seconds=1)),
            ],
        )
        runner.receipt_callback_started = [asyncio.Event(), second_callback_started]
        runner.receipt_callback_finished = [asyncio.Event(), second_callback_finished]
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        log = structlog.get_logger("test")
        snapshot = await orch.snapshot_task_family_batch_problems(log=log)
        task_a = asyncio.create_task(
            orch._maybe_run_task_family_lanes(
                submission=sub_a,
                runner=runner,
                epoch=123,
                round_index=0,
                log=log,
                problem_overrides=snapshot,
            )
        )
        await asyncio.wait_for(winner_transaction_open.wait(), timeout=1.0)
        task_b = asyncio.create_task(
            orch._maybe_run_task_family_lanes(
                submission=sub_b,
                runner=runner,
                epoch=123,
                round_index=1,
                log=log,
                problem_overrides=snapshot,
            )
        )
        await asyncio.wait_for(second_callback_started.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        assert not second_callback_finished.is_set()
        release_winner_transaction.set()
        await asyncio.gather(task_a, task_b)
        assert second_callback_finished.is_set()

        rows_a = await repository.list_eval_runs_for_submission(conn, "sub-a")
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_a) == 1
        assert len(rows_b) == 1
        assert rows_a[0]["weighted_score"] == pytest.approx(1.0)
        assert rows_b[0]["weighted_score"] == pytest.approx(0.0)
        assert rows_b[0]["errors"] == ["challenge_already_locked"]

        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is not None
        assert winner.miner_hotkey == "5MinerA"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_resolved_loser_publish_waits_for_db_write_lock(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        signer = EvalSigner(Ed25519PrivateKey.generate())
        receipt_store = SqliteChallengeReceiptStore(conn)
        shared_write_lock = asyncio.Lock()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_receipt_store=receipt_store,
            db_write_lock=shared_write_lock,
        )
        lane = SyntheticBooleanV1()
        problem, hidden = lane.generate(
            GenerateCtx(
                seed=1,
                tier=0,
                issued_at_iso="2026-05-20T00:00:00.000Z",
            )
        )
        stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```'
        winner_signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub_a,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:02.000Z",
            signer=signer,
            eval_run_id="winner-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        loser_signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub_b,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:03.000Z",
            signer=signer,
            eval_run_id="loser-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )

        winner_receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            submission_id="sub-a:attempt:winner",
            miner_hotkey="5MinerA",
            received_at_iso="2026-05-20T00:00:01.000Z",
            answer_hash="winner-answer",
            recorded_at_iso="2026-05-20T00:00:01.000Z",
        )
        await receipt_store.update_status(
            family_id=winner_receipt.family_id,
            challenge_id=winner_receipt.challenge_id,
            submission_id=winner_receipt.submission_id,
            status=RECEIPT_STATUS_VALID,
            now_iso=str(winner_signed.row["ran_at"]),
            verifier_details_hash=str(winner_signed.row["verifier_details_hash"]),
        )
        loser_receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            submission_id="sub-b:attempt:loser",
            miner_hotkey="5MinerB",
            received_at_iso="2026-05-20T00:00:02.000Z",
            answer_hash="loser-answer",
            recorded_at_iso="2026-05-20T00:00:02.000Z",
        )
        await receipt_store.update_status(
            family_id=loser_receipt.family_id,
            challenge_id=loser_receipt.challenge_id,
            submission_id=loser_receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=loser_receipt.received_at_iso,
        )
        await receipt_store.attach_result(
            family_id=loser_receipt.family_id,
            challenge_id=loser_receipt.challenge_id,
            submission_id=loser_receipt.submission_id,
            eval_run_id=str(loser_signed.row["id"]),
            signed_row=loser_signed.row,
            trace_json={},
            duration_ms=1,
            epoch=123,
            round_index=1,
            now_iso=str(loser_signed.row["ran_at"]),
        )
        await receipt_store.update_status(
            family_id=loser_receipt.family_id,
            challenge_id=loser_receipt.challenge_id,
            submission_id=loser_receipt.submission_id,
            status=RECEIPT_STATUS_VALID,
            now_iso=str(loser_signed.row["ran_at"]),
            verifier_details_hash=str(loser_signed.row["verifier_details_hash"]),
        )
        winner = await receipt_store.get(
            family_id=winner_receipt.family_id,
            challenge_id=winner_receipt.challenge_id,
            submission_id=winner_receipt.submission_id,
        )
        assert winner is not None

        original_persist = orchestrator_module.persist_task_family_result
        persist_started = asyncio.Event()
        persist_finished = asyncio.Event()

        async def signaling_persist(*args: Any, **kwargs: Any) -> None:
            persist_started.set()
            await original_persist(*args, **kwargs)
            persist_finished.set()

        monkeypatch.setattr(orchestrator_module, "persist_task_family_result", signaling_persist)

        await shared_write_lock.acquire()
        try:
            publish_task = asyncio.create_task(
                orch._publish_resolved_sat_losers(
                    receipt_store=receipt_store,
                    winner=winner,
                    problem=problem,
                    reason="challenge_already_locked",
                )
            )
            await asyncio.sleep(0.05)
            assert not persist_started.is_set()
        finally:
            shared_write_lock.release()

        await asyncio.wait_for(persist_finished.wait(), timeout=1.0)
        await publish_task

        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_b) == 1
        assert rows_b[0]["weighted_score"] == pytest.approx(0.0)
        assert rows_b[0]["errors"] == ["challenge_already_locked"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_lock_timestamp_uses_commit_time_for_cnf_grace(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        signer = EvalSigner(Ed25519PrivateKey.generate())
        lane = SyntheticBooleanV1()
        problem, hidden = lane.generate(
            GenerateCtx(
                seed=1,
                tier=0,
                issued_at_iso="2026-05-20T00:00:00.000Z",
            )
        )
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id=problem.task_id,
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "lock-time-test"},
            ),
            now_iso="2026-05-20T00:00:00.000Z",
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_receipt_store=receipt_store,
        )
        stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```'
        old_score_time = "2026-05-20T00:00:02.000Z"
        signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub,
            stdout=stdout,
            ran_at_iso=old_score_time,
            signer=signer,
            eval_run_id="winner-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        await _store_valid_sat_receipt(
            receipt_store,
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            receipt_id="sub-a:attempt:winner",
            miner_hotkey="5MinerA",
            received_at_iso="2026-05-20T00:00:01.000Z",
            signed=signed,
            epoch=123,
            round_index=0,
        )

        before_finalize = datetime.now(UTC) - timedelta(seconds=1)
        finalized = await orch._finalize_ready_sat_winner(
            receipt_store=receipt_store,
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            problem=problem,
            log=structlog.get_logger("test"),
        )

        assert finalized is True
        cur = await conn.execute(
            "SELECT status, updated_at_iso FROM lane_challenges WHERE challenge_id = ?",
            (problem.task_id,),
        )
        status, updated_at_iso = await cur.fetchone()
        assert status == CHALLENGE_STATUS_LOCKED
        assert updated_at_iso != old_score_time
        updated_at = datetime.fromisoformat(str(updated_at_iso).replace("Z", "+00:00"))
        assert updated_at >= before_finalize
        winner = await SqliteChallengeLock(conn).get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
        )
        assert winner is not None
        assert winner.won_at_iso == updated_at_iso
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_reconcile_locked_challenge_publishes_losers_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        sub_b = await _seed_submission(conn, submission_id="sub-b", miner_hotkey="5MinerB")
        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        signer = EvalSigner(Ed25519PrivateKey.generate())
        lane = SyntheticBooleanV1()
        problem, hidden = lane.generate(
            GenerateCtx(
                seed=1,
                tier=0,
                issued_at_iso="2026-05-20T00:00:00.000Z",
            )
        )
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id=problem.task_id,
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "loser-recovery-test"},
            )
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )
        stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```'
        winner_signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub_a,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:02.000Z",
            signer=signer,
            eval_run_id="winner-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        loser_signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub_b,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:03.000Z",
            signer=signer,
            eval_run_id="loser-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        await _store_valid_sat_receipt(
            receipt_store,
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            receipt_id="sub-a:attempt:winner",
            miner_hotkey="5MinerA",
            received_at_iso="2026-05-20T00:00:01.000Z",
            signed=winner_signed,
            epoch=123,
            round_index=0,
        )
        await _store_valid_sat_receipt(
            receipt_store,
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            receipt_id="sub-b:attempt:loser",
            miner_hotkey="5MinerB",
            received_at_iso="2026-05-20T00:00:02.000Z",
            signed=loser_signed,
            epoch=123,
            round_index=1,
        )

        original_publish = orch._publish_resolved_sat_losers

        async def failing_publish(**_kwargs: Any) -> int:
            raise RuntimeError("loser publish failed")

        monkeypatch.setattr(orch, "_publish_resolved_sat_losers", failing_publish)
        with pytest.raises(RuntimeError, match="loser publish failed"):
            await orch._finalize_ready_sat_winner(
                receipt_store=receipt_store,
                family_id="synthetic_boolean_v1",
                challenge_id=problem.task_id,
                problem=problem,
                log=structlog.get_logger("test"),
            )

        cur = await conn.execute(
            "SELECT status FROM lane_challenges WHERE challenge_id = ?",
            (problem.task_id,),
        )
        assert (await cur.fetchone())[0] == CHALLENGE_STATUS_LOCKED
        assert len(await repository.list_eval_runs_for_submission(conn, "sub-a")) == 1
        assert await repository.list_eval_runs_for_submission(conn, "sub-b") == []

        monkeypatch.setattr(orch, "_publish_resolved_sat_losers", original_publish)
        finalized = await orch.reconcile_sat_receipts(log=structlog.get_logger("test"))

        assert finalized == 1
        rows_b = await repository.list_eval_runs_for_submission(conn, "sub-b")
        assert len(rows_b) == 1
        assert rows_b[0]["weighted_score"] == pytest.approx(0.0)
        assert rows_b[0]["errors"] == ["challenge_already_locked"]

        cur = await conn.execute(
            "SELECT losers_published_at_iso FROM lane_challenges WHERE challenge_id = ?",
            (problem.task_id,),
        )
        assert (await cur.fetchone())[0] is not None

        async def fail_if_locked_reannounce(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("completed locked challenge was reconciled again")

        monkeypatch.setattr(
            orch,
            "_announce_synthetic_boolean_problem",
            fail_if_locked_reannounce,
        )
        assert await orch.reconcile_sat_receipts(log=structlog.get_logger("test")) == 0
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_reconcile_marks_legacy_receiptless_lock_done(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="legacy-locked-no-receipts",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_LOCKED,
                audit_metadata={"source": "legacy-upgrade-test"},
            )
        )
        receipt_store = SqliteChallengeReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_receipt_store=receipt_store,
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        async def fail_if_announced(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("receipt-less locked challenge should not be re-announced")

        monkeypatch.setattr(orch, "_announce_synthetic_boolean_problem", fail_if_announced)

        finalized = await orch.reconcile_sat_receipts(log=structlog.get_logger("test"))

        assert finalized == 0
        cur = await conn.execute(
            "SELECT losers_published_at_iso FROM lane_challenges WHERE challenge_id = ?",
            ("legacy-locked-no-receipts",),
        )
        assert (await cur.fetchone())[0] is not None
        assert await source.list_locked_needing_loser_reconciliation(
            "synthetic_boolean_v1"
        ) == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_receipt_result_and_terminal_status_are_atomic(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        signer = EvalSigner(Ed25519PrivateKey.generate())
        receipt_store = _FailingTerminalStatusReceiptStore(conn)
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_receipt_store=receipt_store,
        )
        lane = SyntheticBooleanV1()
        problem, hidden = lane.generate(
            GenerateCtx(
                seed=1,
                tier=0,
                issued_at_iso="2026-05-20T00:00:00.000Z",
            )
        )
        stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```'
        signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:02.000Z",
            signer=signer,
            eval_run_id="valid-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            submission_id="sub-a:attempt:valid",
            miner_hotkey="5MinerA",
            received_at_iso="2026-05-20T00:00:01.000Z",
            answer_hash="valid-answer",
            recorded_at_iso="2026-05-20T00:00:01.000Z",
        )
        await receipt_store.update_status(
            family_id=receipt.family_id,
            challenge_id=receipt.challenge_id,
            submission_id=receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=receipt.received_at_iso,
        )

        with pytest.raises(RuntimeError, match="terminal status failed"):
            await orch._finalize_sat_receipt_ordered_result(
                receipt_store=receipt_store,
                receipt=receipt,
                lane=lane,
                problem=problem,
                hidden=hidden,
                submission=sub,
                hermes_run=_HermesResult(stdout=stdout, trace={}),
                signed=signed,
                epoch=123,
                round_index=0,
                epoch_salt="epoch_123:synthetic_boolean_v1",
                log=structlog.get_logger("test"),
            )

        stored = await SqliteChallengeReceiptStore(conn).get(
            family_id=receipt.family_id,
            challenge_id=receipt.challenge_id,
            submission_id=receipt.submission_id,
        )
        assert stored is not None
        assert stored.status == RECEIPT_STATUS_VERIFYING
        assert stored.eval_run_id is None
        assert stored.signed_row is None
        assert stored.trace_json is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_receipt_lock_race_publishes_selected_winner_as_loser(
    tmp_path,
) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()

        signer = EvalSigner(Ed25519PrivateKey.generate())
        receipt_store = SqliteChallengeReceiptStore(conn)
        lock = _RaceLostChallengeLock()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=StubPolarisRunner(),
            signer=signer,
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_lock=lock,  # type: ignore[arg-type]
            task_family_receipt_store=receipt_store,
        )
        lane = SyntheticBooleanV1()
        problem, hidden = lane.generate(
            GenerateCtx(
                seed=1,
                tier=0,
                issued_at_iso="2026-05-20T00:00:00.000Z",
            )
        )
        stdout = '```FINAL_ANSWER\n{"dimacs_solution": "s SATISFIABLE\\nv 1 0\\n"}\n```'
        signed = score_and_sign_task_family_stdout(
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=sub,
            stdout=stdout,
            ran_at_iso="2026-05-20T00:00:02.000Z",
            signer=signer,
            eval_run_id="race-lost-run",
            epoch_salt="epoch_123:synthetic_boolean_v1",
        )
        assert signed.row["weighted_score"] == pytest.approx(1.0)
        receipt = await receipt_store.record_receipt(
            family_id="synthetic_boolean_v1",
            challenge_id=problem.task_id,
            submission_id="sub-a:attempt:valid",
            miner_hotkey="5MinerA",
            received_at_iso="2026-05-20T00:00:01.000Z",
            answer_hash="valid-answer",
            recorded_at_iso="2026-05-20T00:00:01.000Z",
        )
        await receipt_store.update_status(
            family_id=receipt.family_id,
            challenge_id=receipt.challenge_id,
            submission_id=receipt.submission_id,
            status=RECEIPT_STATUS_VERIFYING,
            now_iso=receipt.received_at_iso,
        )

        await orch._finalize_sat_receipt_ordered_result(
            receipt_store=receipt_store,
            receipt=receipt,
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission=sub,
            hermes_run=_HermesResult(stdout=stdout, trace={}),
            signed=signed,
            epoch=123,
            round_index=0,
            epoch_salt="epoch_123:synthetic_boolean_v1",
            log=structlog.get_logger("test"),
        )

        assert lock.try_lock_calls == 1
        rows = await repository.list_eval_runs_for_submission(conn, "sub-a")
        assert len(rows) == 1
        assert rows[0]["id"] == "race-lost-run"
        assert rows[0]["weighted_score"] == pytest.approx(0.0)
        assert rows[0]["errors"] == ["challenge_already_locked"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_publisher_row_pulls_into_validator_weight_input(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    publisher_conn = await connect(str(tmp_path / "publisher.db"))
    validator_conn = await connect(str(tmp_path / "validator.db"))
    try:
        sub = await _seed_submission(
            publisher_conn,
            submission_id="sub-a",
            miner_hotkey="5MinerA",
        )
        await publisher_conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await publisher_conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await publisher_conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await publisher_conn.commit()
        source = SqliteChallengeSource(publisher_conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "local-e2e"},
            )
        )

        signing_key = Ed25519PrivateKey.generate()
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=publisher_conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(signing_key),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=SqliteChallengeLock(publisher_conn),
            task_family_fetch_token_store=SqliteFetchTokenStore(publisher_conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(publisher_conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )
        await orch._maybe_run_task_family_lanes(
            submission=sub,
            runner=runner,
            epoch=123,
            round_index=0,
            log=structlog.get_logger("test"),
        )

        eval_run = (await repository.list_eval_runs_for_submission(publisher_conn, "sub-a"))[0]
        wire = _eval_run_to_output(eval_run, sub)
        verify_eval_output_signature(wire, signing_key.public_key())
        await upsert_pulled_eval(
            validator_conn,
            eval_run=wire,
            miner_hotkey=str(wire["miner_hotkey"]),
        )

        disabled = await latest_pulled_score_per_hotkey(
            validator_conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.0},
        )
        enabled = await latest_pulled_score_per_hotkey(
            validator_conn,
            since_days=7,
            task_family_weights={"synthetic_boolean_v1": 0.05},
        )

        assert "5MinerA" not in disabled
        assert enabled["5MinerA"] == pytest.approx(0.05)
    finally:
        await publisher_conn.close()
        await validator_conn.close()


@pytest.mark.asyncio
async def test_synthetic_boolean_does_not_promote_if_persist_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_IDS", "synthetic_boolean_v1")

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        sub_a = await _seed_submission(conn, submission_id="sub-a", miner_hotkey="5MinerA")

        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_RECEIPT_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-001",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 1 1\n1 0\n",
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        await source.upsert(
            ChallengeRecord(
                challenge_id="active-boolean-002",
                family_id="synthetic_boolean_v1",
                tier=0,
                cnf_text="p cnf 2 2\n1 0\n2 0\n",
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"source": "toy-runtime-test"},
            )
        )
        lock = SqliteChallengeLock(conn)
        runner = _SolvingRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=StubHippiusClient(),
            polaris=runner,
            signer=EvalSigner(Ed25519PrivateKey.generate()),
            registry=_Registry(),  # type: ignore[arg-type]
            task_family_challenge_source=source,
            task_family_challenge_lock=lock,
            task_family_fetch_token_store=SqliteFetchTokenStore(conn),
            task_family_receipt_store=SqliteChallengeReceiptStore(conn),
            public_base_url=_TEST_PUBLIC_BASE_URL,
        )

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("persist failed")

        monkeypatch.setattr(orchestrator_module, "persist_task_family_result", boom)

        with pytest.raises(RuntimeError, match="persist failed"):
            await orch._maybe_run_task_family_lanes(
                submission=sub_a,
                runner=runner,
                epoch=123,
                round_index=0,
                log=structlog.get_logger("test"),
            )

        winner = await lock.get_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="active-boolean-001",
        )
        assert winner is None
        rows = await source.list_for_family("synthetic_boolean_v1")
        assert [(row.challenge_id, row.status) for row in rows] == [
            ("active-boolean-001", CHALLENGE_STATUS_ACTIVE),
            ("active-boolean-002", CHALLENGE_STATUS_PENDING),
        ]
    finally:
        await conn.close()
