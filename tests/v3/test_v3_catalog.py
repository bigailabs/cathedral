"""Unit tests for the v3 dataset catalog row + writer."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from cathedral.v3.datasets.catalog import (
    CATALOG_SCHEMA,
    CatalogRow,
    CatalogValidationError,
    append_catalog_row,
    build_catalog_row,
    split_for,
    validate_catalog_row,
)


def _score_record(
    *,
    eval_run_id: str = "00000000-0000-4000-8000-000000000301",
    weighted_score: float = 0.9,
) -> dict:
    return {
        "schema": "cathedral.v3.score_record/1",
        "eval_run_id": eval_run_id,
        "miner_hotkey": "5BugIsolationMinerHotkey",
        "agent_id": "sub-bug-isolation",
        "task_type": "bug_isolation_v1",
        "challenge_id_public": "abc123def456",
        "corpus_row_id": "alpha_42",
        "corpus_version": "unknown",
        "scorer_version": "v3.bug_isolation/1",
        "cathedral_commit": "abc1234",
        "ran_at": "2026-05-17T07:05:00.000Z",
        "duration_ms": 1234,
        "claim": {"challenge_id": "ch_alpha_42"},
        "hidden_oracle": {
            "culprit_file": "src/project/config.py",
            "culprit_symbol": "parse_config",
            "line_range": [40, 55],
            "required_failure_keywords": ["empty", "section", "crash"],
        },
        "score_parts": {
            "culprit_file": 1.0,
            "culprit_symbol": 1.0,
            "line_range": 1.0,
            "failure_mode": 1.0,
        },
        "weighted_score": weighted_score,
        "failure_reason": None,
        "repair_was_attempted": False,
        "package_blake3": "blake3hex",
        "manifest_hash": "manifesthex",
        "cathedral_signature": "deadbeef==",
    }


def _valid_dict_row() -> dict:
    return {
        "schema": CATALOG_SCHEMA,
        "eval_run_id": "00000000-0000-4000-8000-000000000301",
        "bundle_uri": "s3://bucket/eval-artifacts/x.tar.gz.enc",
        "manifest_uri": "s3://bucket/eval-artifacts/x.manifest.json",
        "score_uri": "s3://bucket/score-records/x.score_record.json",
        "task_type": "bug_isolation_v1",
        "weighted_score": 0.9,
        "split": "train",
        "distillation_ready": True,
        "tokenized_uri": None,
    }


def test_catalog_row_validates_happy_path() -> None:
    row = validate_catalog_row(_valid_dict_row())
    assert isinstance(row, CatalogRow)
    assert row.eval_run_id == "00000000-0000-4000-8000-000000000301"
    assert row.task_type == "bug_isolation_v1"
    assert row.distillation_ready is True


def test_catalog_row_rejects_missing_fields() -> None:
    raw = _valid_dict_row()
    del raw["eval_run_id"]
    with pytest.raises(CatalogValidationError, match="eval_run_id"):
        validate_catalog_row(raw)


def test_catalog_row_rejects_wrong_schema_string() -> None:
    raw = _valid_dict_row()
    raw["schema"] = "cathedral.v2.something/1"
    with pytest.raises(CatalogValidationError, match="schema"):
        validate_catalog_row(raw)


def test_split_for_is_deterministic_and_distributed() -> None:
    counts = {"train": 0, "val": 0, "test": 0}
    ids: list[str] = []
    n = 2000
    for _ in range(n):
        eid = secrets.token_hex(16)
        ids.append(eid)
        s = split_for(eid)
        counts[s] += 1

    # Determinism: same id always same split.
    for eid in ids[:50]:
        assert split_for(eid) == split_for(eid)

    # Distribution within tolerance. Expected ~80/10/10. Allow +/- 4pp.
    assert 0.76 <= counts["train"] / n <= 0.84
    assert 0.06 <= counts["val"] / n <= 0.14
    assert 0.06 <= counts["test"] / n <= 0.14


def test_append_catalog_row_writes_one_line_jsonl(tmp_path: Path) -> None:
    catalog_path = tmp_path / "subdir" / "catalog.jsonl"
    rows = [
        build_catalog_row(
            score_record=_score_record(eval_run_id=f"id-{i}", weighted_score=0.9),
            score_uri=f"s3://bucket/s/{i}.json",
            bundle_uri=f"s3://bucket/b/{i}.tar.gz.enc",
            manifest_uri=f"s3://bucket/m/{i}.manifest.json",
        )
        for i in range(3)
    ]
    for r in rows:
        append_catalog_row(catalog_path, r)

    text = catalog_path.read_text(encoding="utf-8")
    lines = [line for line in text.split("\n") if line]
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert parsed["schema"] == CATALOG_SCHEMA
        # Round-trips through validate_catalog_row cleanly.
        validate_catalog_row(parsed)


def test_high_score_complete_trace_marks_distillation_ready() -> None:
    high = build_catalog_row(
        score_record=_score_record(eval_run_id="hi", weighted_score=0.9),
        score_uri="s3://bucket/s/hi.json",
        bundle_uri="s3://bucket/b/hi.tar.gz.enc",
        manifest_uri="s3://bucket/m/hi.manifest.json",
    )
    assert high.distillation_ready is True

    low = build_catalog_row(
        score_record=_score_record(eval_run_id="lo", weighted_score=0.5),
        score_uri="s3://bucket/s/lo.json",
        bundle_uri="s3://bucket/b/lo.tar.gz.enc",
        manifest_uri="s3://bucket/m/lo.manifest.json",
    )
    assert low.distillation_ready is False

    # High score but no bundle -> still not distillation-ready.
    no_bundle = build_catalog_row(
        score_record=_score_record(eval_run_id="nb", weighted_score=0.95),
        score_uri="s3://bucket/s/nb.json",
        bundle_uri=None,
        manifest_uri=None,
    )
    assert no_bundle.distillation_ready is False


def test_catalog_preserves_source_artifact_hashes(tmp_path: Path) -> None:
    """The catalog row deliberately does not embed package_blake3 /
    cathedral_signature. Those live on the score_record. The contract
    we lock here: the catalog row MUST carry score_uri so a reader can
    retrieve the source artifact hashes from the score_record itself.
    """
    score_record = _score_record(weighted_score=0.9)
    score_record["cathedral_signature"] = "sig=="
    score_record["package_blake3"] = "blake3hex"

    score_uri = "s3://bucket/score-records/x.score_record.json"
    row = build_catalog_row(
        score_record=score_record,
        score_uri=score_uri,
        bundle_uri="s3://bucket/b/x.tar.gz.enc",
        manifest_uri="s3://bucket/m/x.manifest.json",
    )

    # Hashes are NOT inlined.
    raw = row.to_dict()
    assert "cathedral_signature" not in raw
    assert "package_blake3" not in raw
    # But the pointer is reachable, and the source record still has them.
    assert row.score_uri == score_uri
    fake_storage = {score_uri: score_record}
    fetched = fake_storage[row.score_uri]
    assert fetched["cathedral_signature"] == "sig=="
    assert fetched["package_blake3"] == "blake3hex"


def test_build_catalog_row_requires_eval_run_id() -> None:
    bad = _score_record()
    bad["eval_run_id"] = ""
    with pytest.raises(CatalogValidationError, match="eval_run_id"):
        build_catalog_row(
            score_record=bad,
            score_uri="s3://x",
            bundle_uri=None,
            manifest_uri=None,
        )
