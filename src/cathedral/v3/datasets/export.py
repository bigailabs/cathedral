"""Training-export skeleton for the v3 catalog.

Three output streams, each one JSONL:

  * sft_success.jsonl      : high-score complete traces (no oracle)
  * failure_analysis.jsonl : failed/low-score rows WITH oracle (PRIVATE)
  * rm_pairs.jsonl         : (chosen, rejected) pairs per challenge_id_public

The bundle parser (Hermes turns -> prompt/completion) is not built
yet. Until it lands, prompt/completion fields carry a placeholder
sentinel; everything else (provenance, firewall, pair generation) is
wired through so the catalog can begin emitting rows immediately.

Firewall contract (locked here, mirrored in tests):
  * hidden_oracle ONLY in failure_analysis.jsonl.
  * Oracle fields ``culprit_file`` / ``culprit_symbol`` / ``line_range``
    / ``required_failure_keywords`` MUST NOT appear in sft_success.jsonl
    or rm_pairs.jsonl, by string-presence assertion.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cathedral.v3.datasets.catalog import (
    CatalogRow,
    CatalogValidationError,
    validate_catalog_row,
)

SFT_SUCCESS_SCHEMA: str = "cathedral.v3.sft_success/1"
FAILURE_ANALYSIS_SCHEMA: str = "cathedral.v3.failure_analysis/1"
RM_PAIRS_SCHEMA: str = "cathedral.v3.rm_pairs/1"

# Sentinel that future bundle-parsing work will replace. Tests look for
# this string to assert the skeleton is wired correctly.
_PROMPT_PLACEHOLDER: str = "<<PROMPT_NOT_YET_PARSED>>"
_COMPLETION_PLACEHOLDER: str = "<<COMPLETION_NOT_YET_PARSED>>"

# Minimum score gap between chosen and rejected for an RM pair to be
# worth training on. Pairs below this are dropped.
_RM_PAIR_MIN_DELTA: float = 0.25


ScoreRecordLoader = Callable[[str], dict[str, Any]]


def _iter_catalog_rows(catalog_path: Path) -> list[CatalogRow]:
    rows: list[CatalogRow] = []
    with catalog_path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
                rows.append(validate_catalog_row(raw))
            except (json.JSONDecodeError, CatalogValidationError):
                # Skip malformed lines. The scanner is responsible for
                # only writing valid rows; bad lines in the catalog are
                # data corruption and the export pass should not crash.
                continue
    return rows


def _load_score(loader: ScoreRecordLoader, score_uri: str) -> dict[str, Any] | None:
    try:
        return loader(score_uri)
    except Exception:
        # Loader failures (FileNotFoundError, network blip, decode error)
        # mean we skip the row. Export must be re-runnable without
        # partial-write damage.
        return None


def _write_jsonl(out_path: Path, rows: list[dict[str, Any]]) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    return len(rows)


def export_sft_success(
    catalog_path: Path,
    score_record_loader: ScoreRecordLoader,
    out_path: Path,
) -> int:
    catalog_rows = _iter_catalog_rows(catalog_path)
    out_rows: list[dict[str, Any]] = []
    for crow in catalog_rows:
        if not crow.distillation_ready:
            continue
        score = _load_score(score_record_loader, crow.score_uri)
        if score is None:
            continue
        out_rows.append(
            {
                "schema": SFT_SUCCESS_SCHEMA,
                "eval_run_id": crow.eval_run_id,
                "task_type": crow.task_type,
                "prompt": _PROMPT_PLACEHOLDER,
                "completion": _COMPLETION_PLACEHOLDER,
                "source_bundle_uri": crow.bundle_uri,
                "source_score_uri": crow.score_uri,
                "source_manifest_uri": crow.manifest_uri,
                "weighted_score": crow.weighted_score,
            }
        )
    return _write_jsonl(out_path, out_rows)


def export_failure_analysis(
    catalog_path: Path,
    score_record_loader: ScoreRecordLoader,
    out_path: Path,
) -> int:
    catalog_rows = _iter_catalog_rows(catalog_path)
    out_rows: list[dict[str, Any]] = []
    for crow in catalog_rows:
        if crow.distillation_ready:
            continue
        score = _load_score(score_record_loader, crow.score_uri)
        if score is None:
            continue
        out_rows.append(
            {
                "schema": FAILURE_ANALYSIS_SCHEMA,
                "eval_run_id": crow.eval_run_id,
                "task_type": crow.task_type,
                "failure_reason": score.get("failure_reason"),
                "claim": score.get("claim"),
                # PRIVATE: oracle round-trips into this file by design.
                # This file MUST NOT leave private storage.
                "hidden_oracle": dict(score.get("hidden_oracle") or {}),
                "source_bundle_uri": crow.bundle_uri,
                "source_score_uri": crow.score_uri,
                "weighted_score": crow.weighted_score,
            }
        )
    return _write_jsonl(out_path, out_rows)


def export_rm_pairs(
    catalog_path: Path,
    score_record_loader: ScoreRecordLoader,
    out_path: Path,
) -> int:
    catalog_rows = _iter_catalog_rows(catalog_path)

    # Group catalog rows by challenge_id_public, which we have to read
    # from the score_record (the catalog row itself doesn't carry it).
    groups: dict[str, list[tuple[CatalogRow, dict[str, Any]]]] = {}
    for crow in catalog_rows:
        score = _load_score(score_record_loader, crow.score_uri)
        if score is None:
            continue
        cid_pub = score.get("challenge_id_public")
        if not isinstance(cid_pub, str) or not cid_pub:
            continue
        groups.setdefault(cid_pub, []).append((crow, score))

    out_rows: list[dict[str, Any]] = []
    for cid_pub, entries in groups.items():
        if len(entries) < 2:
            continue
        # Sort by score descending so we walk the highest scorers first.
        entries.sort(key=lambda pair: pair[0].weighted_score, reverse=True)
        for i, (high_row, _high_score) in enumerate(entries):
            for low_row, _low_score in entries[i + 1 :]:
                delta = high_row.weighted_score - low_row.weighted_score
                if delta < _RM_PAIR_MIN_DELTA:
                    continue
                out_rows.append(
                    {
                        "schema": RM_PAIRS_SCHEMA,
                        "challenge_id_public": cid_pub,
                        "chosen": {
                            "eval_run_id": high_row.eval_run_id,
                            "weighted_score": high_row.weighted_score,
                            "source_score_uri": high_row.score_uri,
                        },
                        "rejected": {
                            "eval_run_id": low_row.eval_run_id,
                            "weighted_score": low_row.weighted_score,
                            "source_score_uri": low_row.score_uri,
                        },
                    }
                )
    return _write_jsonl(out_path, out_rows)


__all__ = [
    "FAILURE_ANALYSIS_SCHEMA",
    "RM_PAIRS_SCHEMA",
    "SFT_SUCCESS_SCHEMA",
    "ScoreRecordLoader",
    "export_failure_analysis",
    "export_rm_pairs",
    "export_sft_success",
]
