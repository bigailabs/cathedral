"""Unit tests for the v3 score-record builder."""

from __future__ import annotations

import pytest

from cathedral.v3.corpus.schema import ChallengeRow
from cathedral.v3.score_sidecar import (
    SCORE_RECORD_SCHEMA,
    SCORER_VERSION,
    build_score_record,
)


def _challenge(*, with_corpus_version: bool = False) -> ChallengeRow:
    row = ChallengeRow(
        id="alpha_42",
        repo="https://github.com/example.invalid/project",
        commit="a" * 40,
        issue_text="Calling parse_config with empty section crashes.",
        culprit_file="src/project/config.py",
        culprit_symbol="parse_config",
        line_range=(40, 55),
        required_failure_keywords=("empty", "section", "crash"),
        difficulty="easy",
        bucket="input_validation",
        source_url="https://example.invalid/fix",
    )
    if with_corpus_version:
        # ChallengeRow is frozen + extra='forbid'. We attach via object.__setattr__
        # only when the test wants to simulate a future-field corpus row.
        # For now the loader has no corpus_version, so build_score_record
        # must fall through to "unknown".
        object.__setattr__(row, "corpus_version", "2026-05-17a")
    return row


def _signed_row_success() -> dict:
    return {
        "id": "00000000-0000-4000-8000-000000000301",
        "agent_id": "sub-bug-isolation",
        "miner_hotkey": "5BugIsolationMinerHotkey",
        "task_type": "bug_isolation_v1",
        "challenge_id": "ch_alpha_42",
        "challenge_id_public": "abc123def456",
        "weighted_score": 1.0,
        "score_parts": {
            "culprit_file": 1.0,
            "culprit_symbol": 1.0,
            "line_range": 1.0,
            "failure_mode": 1.0,
        },
        "claim": {
            "challenge_id": "ch_alpha_42",
            "culprit_file": "src/project/config.py",
            "culprit_symbol": "parse_config",
            "line_range": [40, 55],
            "failure_mode": "empty section crash",
        },
        "ran_at": "2026-05-17T07:05:00.000Z",
        "cathedral_signature": "deadbeef==",
    }


def _signed_row_failure() -> dict:
    return {
        "id": "00000000-0000-4000-8000-000000000302",
        "agent_id": "sub-bug-isolation",
        "miner_hotkey": "5BugIsolationMinerHotkey",
        "task_type": "bug_isolation_v1",
        "challenge_id": "ch_alpha_42",
        "challenge_id_public": "abc123def456",
        "weighted_score": 0.0,
        "score_parts": {
            "culprit_file": 0.0,
            "culprit_symbol": 0.0,
            "line_range": 0.0,
            "failure_mode": 0.0,
        },
        "claim": {"challenge_id": "ch_alpha_42", "_failure_reason": "json_parse_error"},
        "ran_at": "2026-05-17T07:06:00.000Z",
        "cathedral_signature": "cafe==",
        "failure_reason": "json_parse_error",
    }


def _submission() -> dict:
    return {
        "id": "sub-bug-isolation",
        "miner_hotkey": "5BugIsolationMinerHotkey",
    }


def test_score_record_happy_path_carries_all_fields(monkeypatch) -> None:
    monkeypatch.setenv("CATHEDRAL_GIT_COMMIT", "abc1234")
    record = build_score_record(
        signed_row=_signed_row_success(),
        challenge=_challenge(),
        submission=_submission(),
        duration_ms=1234,
        repair_was_attempted=False,
        package_blake3="blake3hex",
        manifest_hash="manifesthex",
    )
    assert record["schema"] == SCORE_RECORD_SCHEMA
    assert record["scorer_version"] == SCORER_VERSION
    assert record["cathedral_commit"] == "abc1234"
    assert record["eval_run_id"] == "00000000-0000-4000-8000-000000000301"
    assert record["agent_id"] == "sub-bug-isolation"
    assert record["miner_hotkey"] == "5BugIsolationMinerHotkey"
    assert record["task_type"] == "bug_isolation_v1"
    assert record["challenge_id_public"] == "abc123def456"
    assert record["corpus_row_id"] == "alpha_42"
    assert record["duration_ms"] == 1234
    assert record["weighted_score"] == pytest.approx(1.0)
    assert record["repair_was_attempted"] is False
    assert record["failure_reason"] is None
    assert record["package_blake3"] == "blake3hex"
    assert record["manifest_hash"] == "manifesthex"
    assert record["cathedral_signature"] == "deadbeef=="
    # Hidden oracle round-trip.
    oracle = record["hidden_oracle"]
    assert oracle["culprit_file"] == "src/project/config.py"
    assert oracle["culprit_symbol"] == "parse_config"
    assert oracle["line_range"] == [40, 55]
    assert oracle["required_failure_keywords"] == ["empty", "section", "crash"]
    # Score parts mirrored from signed row.
    assert record["score_parts"]["culprit_file"] == pytest.approx(1.0)
    # Claim round-trip.
    assert record["claim"]["culprit_symbol"] == "parse_config"


def test_score_record_failure_row_zero_score_and_repair_attempted(monkeypatch) -> None:
    monkeypatch.delenv("CATHEDRAL_GIT_COMMIT", raising=False)
    record = build_score_record(
        signed_row=_signed_row_failure(),
        challenge=_challenge(),
        submission=_submission(),
        duration_ms=999,
        repair_was_attempted=True,
        package_blake3="bx",
        manifest_hash="mx",
    )
    assert record["weighted_score"] == pytest.approx(0.0)
    assert record["failure_reason"] == "json_parse_error"
    assert record["repair_was_attempted"] is True
    assert record["cathedral_commit"] == "unknown"
    # Claim may be partial: record carries whatever the signed row had.
    assert record["claim"]["_failure_reason"] == "json_parse_error"


def test_score_record_corpus_version_falls_back_when_missing() -> None:
    record = build_score_record(
        signed_row=_signed_row_success(),
        challenge=_challenge(),
        submission=_submission(),
        duration_ms=10,
        repair_was_attempted=False,
        package_blake3="b",
        manifest_hash="m",
    )
    # ChallengeRow has no corpus_version attribute today: the helper
    # must fall through to "unknown" rather than KeyError.
    assert record["corpus_version"] == "unknown"
