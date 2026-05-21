from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.eval import orchestrator as orchestrator_module
from cathedral.eval.orchestrator import EvalOrchestrator
from cathedral.eval.polaris_runner import StubPolarisRunner
from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA
from cathedral.lanes.challenge_lock import InMemoryChallengeLock, SqliteChallengeLock
from cathedral.lanes.challenge_receipts import RECEIPT_STATUS_INVALID
from cathedral.lanes.challenge_receipts import SQLITE_SCHEMA as CHALLENGE_RECEIPT_SCHEMA
from cathedral.lanes.challenge_receipts import SqliteChallengeReceiptStore
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
        received_at = (
            self._received_at.pop(0)
            if self._received_at
            else f"2026-05-20T12:00:{self._counter:02d}.000Z"
        )
        self._counter += 1
        if receipt_callback is not None:
            await receipt_callback(stdout, received_at)
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
        monkeypatch.setattr(
            publisher_app,
            "_LATEST_CTX",
            PublisherContext(
                db=conn,
                hippius=StubHippiusClient(),
                polaris=StubPolarisRunner(),
                signer=EvalSigner(Ed25519PrivateKey.generate()),
                registry=_Registry(),  # type: ignore[arg-type]
            ),
        )

        advanced = await orchestrator_module._run_once_async()
        assert advanced == 0
        assert isinstance(captured["task_family_fetch_token_store"], SqliteFetchTokenStore)
        assert isinstance(captured["task_family_receipt_store"], SqliteChallengeReceiptStore)
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
