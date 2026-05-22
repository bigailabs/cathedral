"""Publisher persistence tests for the bug_isolation_v1 lane."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.publisher import repository
from cathedral.publisher.reads import _eval_run_to_output
from cathedral.v3.corpus.schema import ChallengeRow
from cathedral.v3.publisher import (
    persist_bug_isolation_result,
    score_and_sign_bug_isolation_stdout,
)
from cathedral.validator.db import connect
from cathedral.validator.pull_loop import verify_eval_output_signature


class _FakeSigner:
    def __init__(self, sk: Ed25519PrivateKey) -> None:
        self._sk = sk


class _FakeBugIsolationRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_bug_isolation_challenge(self, *, challenge, miner_hotkey, submission):
        self.calls.append(
            {
                "challenge": challenge,
                "miner_hotkey": miner_hotkey,
                "submission": submission,
            }
        )
        return type(
            "BugIsolationRun",
            (),
            {
                "stdout": _stdout(),
                "duration_ms": 321,
                "trace": {"transport": "ssh-hermes"},
            },
        )()


class _FakeLog:
    def __init__(self) -> None:
        self.info_events: list[tuple[str, dict]] = []

    def info(self, event: str, **fields) -> None:
        self.info_events.append((event, fields))

    def warning(self, event: str, **fields) -> None:
        self.info_events.append((event, fields))


def _challenge() -> ChallengeRow:
    return ChallengeRow(
        id="pilot_alpha",
        repo="https://github.com/example/project",
        commit="a" * 40,
        issue_text="Calling parse_config with an empty section crashes.",
        culprit_file="src/project/config.py",
        culprit_symbol="parse_config",
        line_range=(40, 55),
        required_failure_keywords=("empty", "section", "crash"),
        difficulty="easy",
        bucket="input_validation",
        source_url="https://github.com/example/project/commit/" + "b" * 40,
    )


def _stdout(challenge_id: str = "ch_pilot_alpha") -> str:
    return (
        "```FINAL_ANSWER\n"
        "{\n"
        f'  "challenge_id": "{challenge_id}",\n'
        '  "culprit_file": "src/project/config.py",\n'
        '  "culprit_symbol": "parse_config",\n'
        '  "line_range": [40, 55],\n'
        '  "failure_mode": "empty section crash"\n'
        "}\n"
        "```"
    )


async def _seed_submission(conn) -> dict:
    await repository.insert_card_definition(
        conn,
        id="eu-ai-act",
        display_name="EU AI Act",
        jurisdiction="EU",
        topic="AI Act",
        description="Primary v1 card.",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    submitted_at = datetime(2026, 5, 16, 7, 0, 0, tzinfo=UTC)
    await repository.insert_agent_submission(
        conn,
        id="sub-bug-isolation",
        miner_hotkey="5BugIsolationMinerHotkey",
        card_id="eu-ai-act",
        bundle_blob_key="bundles/sub-bug-isolation.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name="Bug Isolation Miner",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint="fp-bug-isolation",
        similarity_check_passed=True,
        rejection_reason=None,
        status="ranked",
        submitted_at=submitted_at,
        submitted_at_iso="2026-05-16T07:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    seeded = await repository.get_agent_submission(conn, "sub-bug-isolation")
    assert seeded is not None
    return seeded


@pytest.mark.asyncio
async def test_bug_isolation_persist_is_write_and_read_gated(tmp_path) -> None:
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission = await _seed_submission(conn)
        challenge = _challenge()
        sk = Ed25519PrivateKey.generate()
        signed = score_and_sign_bug_isolation_stdout(
            challenge=challenge,
            submission=submission,
            stdout=_stdout(),
            ran_at_iso="2026-05-16T07:05:00.000Z",
            signer=_FakeSigner(sk),
            eval_run_id="00000000-0000-4000-8000-000000000301",
            epoch_salt="epoch_301",
        )

        await persist_bug_isolation_result(
            conn,
            submission=submission,
            challenge=challenge,
            signed=signed,
            epoch=301,
            round_index=0,
            duration_ms=1234,
            feed_enabled=False,
        )
        since = datetime(2000, 1, 1, tzinfo=UTC)
        assert await repository.list_eval_runs_recent(conn, since=since, include_v3=True) == []

        await persist_bug_isolation_result(
            conn,
            submission=submission,
            challenge=challenge,
            signed=signed,
            epoch=301,
            round_index=0,
            duration_ms=1234,
            trace_json={"transport": "ssh-hermes"},
            feed_enabled=True,
        )

        gated = await repository.list_eval_runs_recent(conn, since=since, include_v3=False)
        assert gated == []

        rows = await repository.list_eval_runs_recent(conn, since=since, include_v3=True)
        assert len(rows) == 1
        wire = _eval_run_to_output(rows[0], submission)
        verify_eval_output_signature(wire, sk.public_key())
        assert wire["eval_output_schema_version"] == 3
        assert wire["task_type"] == "bug_isolation_v1"
        assert wire["miner_hotkey"] == "5BugIsolationMinerHotkey"
        assert wire["weighted_score"] == pytest.approx(1.0)
        assert wire["challenge_id"] == "ch_pilot_alpha"
        assert wire["claim"]["challenge_id"] == "ch_pilot_alpha"
        assert wire["challenge_id_public"] == signed.row["challenge_id_public"]
        # epoch_salt is part of the v3 signed subset; the readback
        # path must surface it on the wire or a future regression in
        # _eval_run_to_output could drop it silently while signed
        # rows already on disk would then fail verification.
        assert wire["epoch_salt"] == "epoch_301"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_orchestrator_runs_bug_isolation_lane_when_feed_is_enabled(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_V3_FEED_ENABLED", "true")
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        from cathedral.eval import orchestrator as orchestrator_module
        from cathedral.eval.orchestrator import EvalOrchestrator

        submission = await _seed_submission(conn)
        challenge = _challenge()
        monkeypatch.setattr(
            orchestrator_module, "load_private_corpus", lambda: (challenge,)
        )
        sk = Ed25519PrivateKey.generate()
        signer = _FakeSigner(sk)
        runner = _FakeBugIsolationRunner()
        orch = EvalOrchestrator(
            db=conn,
            hippius=object(),
            polaris=runner,
            signer=signer,
            registry=object(),
        )

        await orch._maybe_run_v3_bug_isolation(
            submission=submission,
            runner=runner,
            epoch=301,
            round_index=2,
            log=_FakeLog(),
        )

        assert len(runner.calls) == 1
        assert runner.calls[0]["miner_hotkey"] == "5BugIsolationMinerHotkey"
        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repository.list_eval_runs_recent(conn, since=since, include_v3=True)
        assert len(rows) == 1
        wire = _eval_run_to_output(rows[0], submission)
        verify_eval_output_signature(wire, sk.public_key())
        assert wire["task_type"] == "bug_isolation_v1"
        assert wire["weighted_score"] == pytest.approx(1.0)
        assert wire["challenge_id"] == "ch_pilot_alpha"
    finally:
        await conn.close()
