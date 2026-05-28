"""Shadow-mode utilities for comparing legacy and v1.3 weight vectors."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

WeightInput = Mapping[int, float] | Sequence[tuple[int, float]]

SHADOW_WEIGHT_DIFFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_weight_diffs (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    rules_version_id            INTEGER,
    validator_hotkey            TEXT NOT NULL,
    network                     TEXT NOT NULL,
    netuid                      INTEGER NOT NULL,
    legacy_vector_sha256        TEXT NOT NULL,
    decentralized_vector_sha256 TEXT NOT NULL,
    abs_diff_sum                REAL NOT NULL,
    max_diff                    REAL NOT NULL,
    legacy_weight_count         INTEGER NOT NULL,
    decentralized_weight_count  INTEGER NOT NULL,
    blocker                     INTEGER NOT NULL DEFAULT 0 CHECK(blocker IN (0, 1)),
    details_json                TEXT NOT NULL DEFAULT '{}',
    computed_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_weight_diffs_computed_at
    ON shadow_weight_diffs(computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_weight_diffs_blocker
    ON shadow_weight_diffs(blocker, computed_at DESC);
"""


@dataclass(frozen=True)
class ShadowWeightDiff:
    legacy_vector_sha256: str
    decentralized_vector_sha256: str
    abs_diff_sum: float
    max_diff: float
    legacy_weight_count: int
    decentralized_weight_count: int
    blocker: bool
    details: dict[str, Any]


def _normalize_input(weights: WeightInput) -> dict[int, float]:
    items = weights.items() if isinstance(weights, Mapping) else weights
    out: dict[int, float] = {}
    for uid, weight in items:
        out[int(uid)] = float(weight)
    return out


def vector_sha256(weights: WeightInput) -> str:
    normalized = _normalize_input(weights)
    body = [[uid, normalized[uid]] for uid in sorted(normalized)]
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_shadow_weight_diff(
    legacy_weights: WeightInput,
    decentralized_weights: WeightInput,
    *,
    blocker_abs_diff_sum: float = 0.10,
) -> ShadowWeightDiff:
    legacy = _normalize_input(legacy_weights)
    decentralized = _normalize_input(decentralized_weights)
    uids = sorted(set(legacy) | set(decentralized))
    diffs = {uid: abs(legacy.get(uid, 0.0) - decentralized.get(uid, 0.0)) for uid in uids}
    abs_diff_sum = sum(diffs.values())
    max_diff = max(diffs.values(), default=0.0)
    top_diffs = sorted(diffs.items(), key=lambda item: (-item[1], item[0]))[:20]
    return ShadowWeightDiff(
        legacy_vector_sha256=vector_sha256(legacy),
        decentralized_vector_sha256=vector_sha256(decentralized),
        abs_diff_sum=abs_diff_sum,
        max_diff=max_diff,
        legacy_weight_count=len(legacy),
        decentralized_weight_count=len(decentralized),
        blocker=abs_diff_sum >= blocker_abs_diff_sum,
        details={
            "blocker_abs_diff_sum": blocker_abs_diff_sum,
            "top_diffs": [[uid, diff] for uid, diff in top_diffs],
        },
    )


def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


async def record_shadow_weight_diff(
    conn: aiosqlite.Connection,
    *,
    validator_hotkey: str,
    network: str,
    netuid: int,
    diff: ShadowWeightDiff,
    rules_version_id: int | None = None,
    computed_at: datetime | None = None,
) -> int:
    await conn.executescript(SHADOW_WEIGHT_DIFFS_SCHEMA)
    now_iso = _ms_iso(computed_at or datetime.now(UTC))
    cur = await conn.execute(
        """
        INSERT INTO shadow_weight_diffs (
            rules_version_id, validator_hotkey, network, netuid,
            legacy_vector_sha256, decentralized_vector_sha256,
            abs_diff_sum, max_diff,
            legacy_weight_count, decentralized_weight_count,
            blocker, details_json, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rules_version_id,
            validator_hotkey,
            network,
            int(netuid),
            diff.legacy_vector_sha256,
            diff.decentralized_vector_sha256,
            float(diff.abs_diff_sum),
            float(diff.max_diff),
            int(diff.legacy_weight_count),
            int(diff.decentralized_weight_count),
            1 if diff.blocker else 0,
            json.dumps(diff.details, sort_keys=True, separators=(",", ":")),
            now_iso,
        ),
    )
    await conn.commit()
    return int(cur.lastrowid)

