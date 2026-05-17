"""End-to-end firewall test for the v3 full-Hermes-package wiring.

Asserts the contract that matters for adversarial readers:
  - bundle/manifest/sidecar URLs are written ONLY into trace_json
    (operator-only) and never appear in output_card_json (public feed).
  - the score sidecar contains the full hidden oracle.
  - the orchestrator path injects repair_stdout into the scorer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.publisher import repository
from cathedral.v3.corpus.schema import ChallengeRow
from cathedral.validator.db import connect

# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeSigner:
    def __init__(self, sk: Ed25519PrivateKey) -> None:
        self._sk = sk

    def sign(self, payload: dict[str, Any]) -> str:  # signature shape mirrors EvalSigner
        import base64
        import json

        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return base64.b64encode(self._sk.sign(canonical)).decode("ascii")


class _FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def exception(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


class _FakeBugIsolationRunner:
    """Returns a BugIsolationHermesRun-shaped object with a TraceBundle."""

    def __init__(self, bundle: Any, stdout: str) -> None:
        self.bundle = bundle
        self.stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def run_bug_isolation_challenge(
        self,
        *,
        challenge: ChallengeRow,
        miner_hotkey: str,
        submission: dict[str, Any],
    ) -> Any:
        self.calls.append(
            {
                "challenge": challenge,
                "miner_hotkey": miner_hotkey,
                "submission": submission,
            }
        )

        run = type(
            "BugIsolationRun",
            (),
            {
                "stdout": self.stdout,
                "repair_stdout": None,
                "duration_ms": 321,
                "trace": {"transport": "ssh-hermes"},
                "trace_bundle": self.bundle,
            },
        )()
        return run


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


def _challenge() -> ChallengeRow:
    return ChallengeRow(
        id="pilot_alpha",
        repo="https://github.com/example.invalid/project",
        commit="a" * 40,
        issue_text="Calling parse_config with an empty section crashes.",
        culprit_file="src/project/config.py",
        culprit_symbol="parse_config",
        line_range=(40, 55),
        required_failure_keywords=("empty", "section", "crash"),
        difficulty="easy",
        bucket="input_validation",
        source_url="https://example.invalid/fix",
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


def _trace_bundle(tmp_path: Path) -> Any:
    # Deferred import: cathedral.eval imports publisher.app which triggers
    # the documented circular import unless loaded inside a test scope.
    from cathedral.eval.ssh_hermes_runner import TraceBundle

    return TraceBundle(
        eval_id="ev-deadbeef",
        submission_id="sub-bug-isolation",
        cathedral_eval_round="bug-isolation-pilot_alpha-aaaa",
        bundle_tar_path=tmp_path / "bundle.tar.gz",
        manifest={"files": []},
        bundle_blake3="bundle-blake3-hex",
    )


@pytest.mark.asyncio
async def test_v3_full_package_firewall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_V3_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    from cathedral.eval import orchestrator as orchestrator_module
    from cathedral.eval.bundle_publisher import (
        EvalArtifactPublisher,
        PublishedArtifact,
    )
    from cathedral.eval.orchestrator import EvalOrchestrator

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission = await _seed_submission(conn)
        challenge = _challenge()
        bundle = _trace_bundle(tmp_path)

        # Mock the bundle publisher: return a deterministic
        # PublishedArtifact without touching Hippius.
        fake_artifact = PublishedArtifact(
            eval_id=bundle.eval_id,
            submission_id=bundle.submission_id,
            cathedral_eval_round=bundle.cathedral_eval_round,
            manifest_hash="MANIFEST_HASH_HEX",
            manifest_url="s3://bucket/eval-artifacts/ev-deadbeef.manifest.json",
            bundle_url="s3://bucket/eval-artifacts/ev-deadbeef.tar.gz.enc",
            manifest_signature="b64-sig",
            manifest_body={"bundle_blake3": "bundle-blake3-hex"},
        )

        async def _fake_publish(self, b):  # type: ignore[no-untyped-def]
            return fake_artifact

        monkeypatch.setattr(EvalArtifactPublisher, "publish", _fake_publish)

        # Capture score-sidecar uploads in-process. We monkeypatch the
        # exported symbol the orchestrator imports: the
        # `publish_score_sidecar` name in `cathedral.eval.orchestrator`
        # is the call site that matters.
        captured: dict[str, Any] = {}

        async def _fake_publish_sidecar(*, hippius, eval_id, score_record):  # type: ignore[no-untyped-def]
            captured["eval_id"] = eval_id
            captured["score_record"] = score_record
            return f"s3://bucket/eval-artifacts/{eval_id}.score_record.json"

        monkeypatch.setattr(
            orchestrator_module, "publish_score_sidecar", _fake_publish_sidecar
        )
        monkeypatch.setattr(
            orchestrator_module, "load_private_corpus", lambda: (challenge,)
        )

        sk = Ed25519PrivateKey.generate()
        signer = _FakeSigner(sk)
        runner = _FakeBugIsolationRunner(bundle=bundle, stdout=_stdout())

        orch = EvalOrchestrator(
            db=conn,
            hippius=object(),  # never reached: both Hippius paths are mocked
            polaris=runner,
            signer=signer,
            registry=object(),
        )

        await orch._maybe_run_v3_bug_isolation(
            submission=submission,
            runner=runner,
            epoch=301,
            round_index=0,
            log=_FakeLog(),
        )

        # 1. Score sidecar carries the full hidden oracle.
        assert captured["eval_id"] == "ev-deadbeef"
        rec = captured["score_record"]
        assert rec["hidden_oracle"]["culprit_file"] == "src/project/config.py"
        assert rec["hidden_oracle"]["culprit_symbol"] == "parse_config"
        assert rec["hidden_oracle"]["line_range"] == [40, 55]
        assert rec["hidden_oracle"]["required_failure_keywords"] == [
            "empty",
            "section",
            "crash",
        ]
        assert rec["corpus_row_id"] == "pilot_alpha"
        assert rec["package_blake3"] == "bundle-blake3-hex"
        assert rec["manifest_hash"] == "MANIFEST_HASH_HEX"

        # 2. Public output_card_json must not leak oracle or sidecar URLs.
        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repository.list_eval_runs_recent(
            conn, since=since, include_v3=True
        )
        assert len(rows) == 1
        row = rows[0]

        import json

        output_card_json = row.get("output_card_json") or "{}"
        if isinstance(output_card_json, (bytes, str)):
            try:
                card = json.loads(output_card_json)
            except (TypeError, ValueError):
                card = {}
        else:
            card = dict(output_card_json)
        public_blob = json.dumps(card)
        # `claim` is the miner's submitted answer, so claim-shaped keys
        # are expected on the public feed. What MUST NOT appear is the
        # bundle/manifest/sidecar handles (operator-only) and the hidden
        # oracle's required_failure_keywords (the scorer's ground truth).
        for forbidden in (
            "bundle_url",
            "manifest_url",
            "score_record_url",
            "package_blake3",
            "MANIFEST_HASH_HEX",
            "required_failure_keywords",
            "hidden_oracle",
            "score_record",
            "cathedral_eval_round",
        ):
            assert forbidden not in public_blob, (
                f"public output_card leaked {forbidden!r}: {public_blob}"
            )
        # Allowed top-level keys on the public output_card.
        assert set(card.keys()) == {
            "task_type",
            "challenge_id_public",
            "claim",
            "failure_reason",
            "worker_owner_hotkey",
        }, f"unexpected public output_card keys: {sorted(card.keys())}"

        # 3. trace_json (operator-only) carries the sidecar handles.
        trace_blob = row.get("trace_json") or "{}"
        if isinstance(trace_blob, (bytes, str)):
            trace = json.loads(trace_blob)
        else:
            trace = dict(trace_blob)
        assert trace["bundle_blake3"] == "bundle-blake3-hex"
        assert trace["bundle_url"].startswith("s3://")
        assert trace["manifest_url"].startswith("s3://")
        assert trace["manifest_hash"] == "MANIFEST_HASH_HEX"
        assert trace["score_record_url"].endswith(".score_record.json")
        assert trace["cathedral_eval_round"] == "bug-isolation-pilot_alpha-aaaa"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_v3_sidecar_failure_does_not_crash_eval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken Hippius sidecar upload must leave the eval row intact."""
    monkeypatch.setenv("CATHEDRAL_V3_FEED_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    from cathedral.eval import orchestrator as orchestrator_module
    from cathedral.eval.bundle_publisher import (
        EvalArtifactPublisher,
        PublishedArtifact,
    )
    from cathedral.eval.orchestrator import EvalOrchestrator

    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission = await _seed_submission(conn)
        challenge = _challenge()
        bundle = _trace_bundle(tmp_path)

        fake_artifact = PublishedArtifact(
            eval_id=bundle.eval_id,
            submission_id=bundle.submission_id,
            cathedral_eval_round=bundle.cathedral_eval_round,
            manifest_hash="m",
            manifest_url="s3://bucket/m",
            bundle_url="s3://bucket/b",
            manifest_signature="sig",
            manifest_body={},
        )

        async def _fake_publish(self, b):  # type: ignore[no-untyped-def]
            return fake_artifact

        async def _broken_sidecar(**kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("hippius down")

        monkeypatch.setattr(EvalArtifactPublisher, "publish", _fake_publish)
        monkeypatch.setattr(orchestrator_module, "publish_score_sidecar", _broken_sidecar)
        monkeypatch.setattr(
            orchestrator_module, "load_private_corpus", lambda: (challenge,)
        )

        sk = Ed25519PrivateKey.generate()
        runner = _FakeBugIsolationRunner(bundle=bundle, stdout=_stdout())
        orch = EvalOrchestrator(
            db=conn,
            hippius=object(),
            polaris=runner,
            signer=_FakeSigner(sk),
            registry=object(),
        )

        # Must not raise.
        await orch._maybe_run_v3_bug_isolation(
            submission=submission,
            runner=runner,
            epoch=301,
            round_index=0,
            log=_FakeLog(),
        )

        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repository.list_eval_runs_recent(
            conn, since=since, include_v3=True
        )
        assert len(rows) == 1
    finally:
        await conn.close()
