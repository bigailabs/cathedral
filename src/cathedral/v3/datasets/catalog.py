"""Private catalog row for v3 bug_isolation_v1 evals.

One catalog row per eval_run. Rows are append-only JSONL. The row is
deliberately minimal: it carries pointers (s3:// URIs) to the score
record, the encrypted bundle, and the signed manifest, plus the
fields a later training-export pass needs to filter (split,
distillation_ready) without round-tripping to storage.

The hidden oracle, raw challenge_id, and corpus_row_id do NOT live
in the catalog row. Those stay inside the score_record on private
storage, retrievable via ``score_uri``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CATALOG_SCHEMA: str = "cathedral.v3.catalog/1"

# Split thresholds on the first byte of sha256(eval_run_id).
# 0x00..0xCB inclusive -> train (204/256 = ~79.7%)
# 0xCC..0xE5 inclusive -> val   ( 26/256 = ~10.2%)
# 0xE6..0xFF inclusive -> test  ( 26/256 = ~10.2%)
_SPLIT_VAL_LOW: int = 0xCC
_SPLIT_TEST_LOW: int = 0xE6

# A trace is "distillation_ready" only when it both scored high and we
# actually captured a bundle to learn from. The 0.75 floor mirrors the
# v3 high-confidence cutoff used elsewhere in scoring.
_DISTILLATION_SCORE_FLOOR: float = 0.75


class CatalogValidationError(ValueError):
    """A dict failed validate_catalog_row."""


@dataclass(frozen=True)
class CatalogRow:
    schema: str
    eval_run_id: str
    bundle_uri: str | None
    manifest_uri: str | None
    score_uri: str
    task_type: str
    weighted_score: float
    split: str
    distillation_ready: bool
    tokenized_uri: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_for(eval_run_id: str) -> str:
    digest = hashlib.sha256(eval_run_id.encode("utf-8")).digest()
    first = digest[0]
    if first < _SPLIT_VAL_LOW:
        return "train"
    if first < _SPLIT_TEST_LOW:
        return "val"
    return "test"


def build_catalog_row(
    *,
    score_record: dict[str, Any],
    score_uri: str,
    bundle_uri: str | None,
    manifest_uri: str | None,
) -> CatalogRow:
    eval_run_id = str(score_record.get("eval_run_id") or "")
    if not eval_run_id:
        raise CatalogValidationError("score_record missing eval_run_id")
    task_type = str(score_record.get("task_type") or "")
    if not task_type:
        raise CatalogValidationError("score_record missing task_type")

    weighted_score = float(score_record.get("weighted_score") or 0.0)
    distillation_ready = bool(
        weighted_score >= _DISTILLATION_SCORE_FLOOR and bundle_uri is not None
    )

    return CatalogRow(
        schema=CATALOG_SCHEMA,
        eval_run_id=eval_run_id,
        bundle_uri=bundle_uri,
        manifest_uri=manifest_uri,
        score_uri=score_uri,
        task_type=task_type,
        weighted_score=weighted_score,
        split=split_for(eval_run_id),
        distillation_ready=distillation_ready,
        tokenized_uri=None,
    )


def validate_catalog_row(raw: dict[str, Any]) -> CatalogRow:
    required_types: dict[str, type | tuple[type, ...]] = {
        "schema": str,
        "eval_run_id": str,
        "bundle_uri": (str, type(None)),
        "manifest_uri": (str, type(None)),
        "score_uri": str,
        "task_type": str,
        "weighted_score": (int, float),
        "split": str,
        "distillation_ready": bool,
        "tokenized_uri": (str, type(None)),
    }
    for key, expected in required_types.items():
        if key not in raw:
            raise CatalogValidationError(f"missing field: {key}")
        if not isinstance(raw[key], expected):
            raise CatalogValidationError(
                f"field {key!r} has wrong type: {type(raw[key]).__name__}"
            )
    if raw["schema"] != CATALOG_SCHEMA:
        raise CatalogValidationError(
            f"schema mismatch: expected {CATALOG_SCHEMA!r}, got {raw['schema']!r}"
        )
    if raw["split"] not in {"train", "val", "test"}:
        raise CatalogValidationError(f"invalid split: {raw['split']!r}")
    return CatalogRow(
        schema=raw["schema"],
        eval_run_id=raw["eval_run_id"],
        bundle_uri=raw["bundle_uri"],
        manifest_uri=raw["manifest_uri"],
        score_uri=raw["score_uri"],
        task_type=raw["task_type"],
        weighted_score=float(raw["weighted_score"]),
        split=raw["split"],
        distillation_ready=bool(raw["distillation_ready"]),
        tokenized_uri=raw["tokenized_uri"],
    )


def append_catalog_row(catalog_path: Path, row: CatalogRow) -> None:
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    # Plain json.dumps (not cathedral.v1_types.canonical_json): catalog
    # rows are not signed, and canonical_json strips signature-shaped
    # keys we don't carry anyway. Sorted keys + compact separators keep
    # the file diff-friendly.
    line = json.dumps(row.to_dict(), sort_keys=True, separators=(",", ":"))
    with catalog_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


__all__ = [
    "CATALOG_SCHEMA",
    "CatalogRow",
    "CatalogValidationError",
    "append_catalog_row",
    "build_catalog_row",
    "split_for",
    "validate_catalog_row",
]
