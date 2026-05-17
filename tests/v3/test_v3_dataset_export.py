"""Unit tests for the v3 dataset training-export skeleton."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cathedral.v3.datasets.catalog import append_catalog_row, build_catalog_row
from cathedral.v3.datasets.export import (
    FAILURE_ANALYSIS_SCHEMA,
    RM_PAIRS_SCHEMA,
    SFT_SUCCESS_SCHEMA,
    export_failure_analysis,
    export_rm_pairs,
    export_sft_success,
)

# Hidden oracle values: present in score_records, MUST NOT appear in
# SFT or RM outputs. We keep them deliberately distinctive so a
# substring match in the JSONL bytes is unambiguous.
_ORACLE_CULPRIT_FILE = "src/sentinel_pkg/sentinel_module.py"
_ORACLE_CULPRIT_SYMBOL = "sentinel_function_oracle"
_ORACLE_LINE_RANGE = [42, 99]
_ORACLE_REQUIRED_KEYWORDS = ["oracle_keyword_alpha", "oracle_keyword_omega"]


def _score_record(
    *,
    eval_run_id: str,
    challenge_id_public: str,
    weighted_score: float,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "cathedral.v3.score_record/1",
        "eval_run_id": eval_run_id,
        "miner_hotkey": "5BugIsolationMinerHotkey",
        "agent_id": "sub-bug-isolation",
        "task_type": "bug_isolation_v1",
        "challenge_id_public": challenge_id_public,
        "corpus_row_id": "alpha_42",
        "corpus_version": "unknown",
        "scorer_version": "v3.bug_isolation/1",
        "cathedral_commit": "abc1234",
        "ran_at": "2026-05-17T07:05:00.000Z",
        "duration_ms": 1234,
        "claim": {"challenge_id": "ch_alpha_42"},
        "hidden_oracle": {
            "culprit_file": _ORACLE_CULPRIT_FILE,
            "culprit_symbol": _ORACLE_CULPRIT_SYMBOL,
            "line_range": list(_ORACLE_LINE_RANGE),
            "required_failure_keywords": list(_ORACLE_REQUIRED_KEYWORDS),
        },
        "score_parts": {
            "culprit_file": weighted_score,
            "culprit_symbol": weighted_score,
            "line_range": weighted_score,
            "failure_mode": weighted_score,
        },
        "weighted_score": weighted_score,
        "failure_reason": failure_reason,
        "repair_was_attempted": False,
        "package_blake3": "blake3hex",
        "manifest_hash": "manifesthex",
        "cathedral_signature": "deadbeef==",
    }


def _write_catalog(
    tmp_path: Path,
    entries: list[tuple[str, float, str]],  # (eval_run_id, score, challenge_id_public)
) -> tuple[Path, dict[str, dict[str, Any]]]:
    catalog_path = tmp_path / "catalog.jsonl"
    score_store: dict[str, dict[str, Any]] = {}
    for eval_run_id, score, cid_pub in entries:
        sr = _score_record(
            eval_run_id=eval_run_id,
            challenge_id_public=cid_pub,
            weighted_score=score,
            failure_reason=None if score >= 0.75 else "json_parse_error",
        )
        score_uri = f"s3://bucket/score-records/{eval_run_id}.score_record.json"
        bundle_uri = f"s3://bucket/eval-artifacts/{eval_run_id}.tar.gz.enc"
        manifest_uri = f"s3://bucket/eval-artifacts/{eval_run_id}.manifest.json"
        score_store[score_uri] = sr
        row = build_catalog_row(
            score_record=sr,
            score_uri=score_uri,
            bundle_uri=bundle_uri,
            manifest_uri=manifest_uri,
        )
        append_catalog_row(catalog_path, row)
    return catalog_path, score_store


def _loader(score_store: dict[str, dict[str, Any]]):
    def _load(uri: str) -> dict[str, Any]:
        if uri not in score_store:
            raise FileNotFoundError(uri)
        return score_store[uri]

    return _load


def test_export_skips_rows_without_score_records(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-a", 0.9, "ch_pub_a"),
            ("eid-b", 0.9, "ch_pub_b"),
            ("eid-c", 0.9, "ch_pub_c"),
        ],
    )
    # Drop one record so the loader raises for that uri.
    missing_uri = "s3://bucket/score-records/eid-b.score_record.json"
    del score_store[missing_uri]

    out = tmp_path / "sft_success.jsonl"
    written = export_sft_success(catalog_path, _loader(score_store), out)
    assert written == 2


def test_export_marks_high_score_complete_traces_as_sft_success(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-hi", 0.9, "ch_pub_hi"),
            ("eid-mid", 0.6, "ch_pub_mid"),
            ("eid-lo", 0.2, "ch_pub_lo"),
        ],
    )
    out = tmp_path / "sft_success.jsonl"
    written = export_sft_success(catalog_path, _loader(score_store), out)
    assert written == 1

    text = out.read_text(encoding="utf-8")
    assert "eid-hi" in text
    assert "eid-mid" not in text
    assert "eid-lo" not in text
    assert SFT_SUCCESS_SCHEMA in text


def test_export_excludes_oracle_from_sft_and_rm_files(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-1", 0.9, "ch_shared"),
            ("eid-2", 0.8, "ch_shared"),
            ("eid-3", 0.2, "ch_shared"),
        ],
    )

    sft_out = tmp_path / "sft_success.jsonl"
    export_sft_success(catalog_path, _loader(score_store), sft_out)
    sft_bytes = sft_out.read_bytes()

    rm_out = tmp_path / "rm_pairs.jsonl"
    export_rm_pairs(catalog_path, _loader(score_store), rm_out)
    rm_bytes = rm_out.read_bytes()

    forbidden_substrings = [
        b"culprit_file",
        b"culprit_symbol",
        b"line_range",
        b"required_failure_keywords",
        b"hidden_oracle",
        _ORACLE_CULPRIT_FILE.encode(),
        _ORACLE_CULPRIT_SYMBOL.encode(),
        _ORACLE_REQUIRED_KEYWORDS[0].encode(),
        _ORACLE_REQUIRED_KEYWORDS[1].encode(),
    ]
    for needle in forbidden_substrings:
        assert needle not in sft_bytes, f"oracle leaked into sft_success.jsonl: {needle!r}"
        assert needle not in rm_bytes, f"oracle leaked into rm_pairs.jsonl: {needle!r}"


def test_export_includes_oracle_in_failure_analysis(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-fail", 0.2, "ch_pub_fail"),
        ],
    )
    out = tmp_path / "failure_analysis.jsonl"
    written = export_failure_analysis(catalog_path, _loader(score_store), out)
    assert written == 1
    text = out.read_text(encoding="utf-8")
    assert FAILURE_ANALYSIS_SCHEMA in text
    assert "hidden_oracle" in text
    assert _ORACLE_CULPRIT_FILE in text
    assert _ORACLE_CULPRIT_SYMBOL in text
    for kw in _ORACLE_REQUIRED_KEYWORDS:
        assert kw in text


def test_export_keeps_source_artifact_hashes(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-keep", 0.95, "ch_keep_a"),
            ("eid-fail", 0.1, "ch_keep_b"),
        ],
    )
    sft_out = tmp_path / "sft_success.jsonl"
    export_sft_success(catalog_path, _loader(score_store), sft_out)
    sft_text = sft_out.read_text(encoding="utf-8")
    assert "source_score_uri" in sft_text
    assert "source_bundle_uri" in sft_text
    assert "s3://bucket/eval-artifacts/eid-keep.tar.gz.enc" in sft_text

    fail_out = tmp_path / "failure_analysis.jsonl"
    export_failure_analysis(catalog_path, _loader(score_store), fail_out)
    fail_text = fail_out.read_text(encoding="utf-8")
    assert "source_score_uri" in fail_text
    assert "source_bundle_uri" in fail_text


def test_rm_pairs_only_when_score_gap_large_enough(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-09", 0.9, "ch_shared"),
            ("eid-07", 0.7, "ch_shared"),
            ("eid-03", 0.3, "ch_shared"),
        ],
    )
    out = tmp_path / "rm_pairs.jsonl"
    written = export_rm_pairs(catalog_path, _loader(score_store), out)
    # Pairs:
    #   0.9 vs 0.7 = 0.2 -> skip
    #   0.9 vs 0.3 = 0.6 -> keep
    #   0.7 vs 0.3 = 0.4 -> keep
    assert written == 2
    text = out.read_text(encoding="utf-8")
    assert RM_PAIRS_SCHEMA in text
    assert text.count("\n") == 2


def test_rm_pairs_skips_single_entry_groups(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-only", 0.9, "ch_unique"),
        ],
    )
    out = tmp_path / "rm_pairs.jsonl"
    written = export_rm_pairs(catalog_path, _loader(score_store), out)
    assert written == 0


def test_export_failure_analysis_skips_distillation_ready(tmp_path: Path) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-good", 0.95, "ch_good"),
            ("eid-bad", 0.1, "ch_bad"),
        ],
    )
    out = tmp_path / "failure_analysis.jsonl"
    written = export_failure_analysis(catalog_path, _loader(score_store), out)
    assert written == 1
    text = out.read_text(encoding="utf-8")
    assert "eid-bad" in text
    assert "eid-good" not in text


@pytest.mark.parametrize("bad_uri", ["", None])
def test_rm_pairs_skips_records_with_no_challenge_id_public(
    tmp_path: Path, bad_uri: str | None
) -> None:
    catalog_path, score_store = _write_catalog(
        tmp_path,
        [
            ("eid-1", 0.9, "ch_real"),
            ("eid-2", 0.3, "ch_real"),
        ],
    )
    # Corrupt one score record's challenge_id_public.
    for uri, rec in score_store.items():
        if "eid-2" in uri:
            rec["challenge_id_public"] = bad_uri  # type: ignore[assignment]

    out = tmp_path / "rm_pairs.jsonl"
    written = export_rm_pairs(catalog_path, _loader(score_store), out)
    # Only one valid record remains for ch_real -> no pair possible.
    assert written == 0
