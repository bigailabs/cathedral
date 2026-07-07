"""Flat per-miner results file publisher.

After each verify tick, we push a static JSON file per miner to Hippius so
miners can poll their own file directly from the edge instead of hitting the
origin status endpoint.

Key: ``results/<hotkey>.json``

JSON shape (schema: cathedral.v2.results.v1):
    {
      "schema": "cathedral.v2.results.v1",
      "miner_hotkey": "<ss58>",
      "epoch": 42,
      "updated_at": "<iso>",
      "total_verified": 3,
      "total_score": 2.5,
      "items": [
        {
          "challenge_id": "pm-t1-e42-...",
          "tier": 1,
          "seq": 0,
          "status": "verified",
          "weighted_score": 1.0,
          "verified_at": "<iso>"
        },
        ...
      ]
    }

Gate: CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED (default off).
If the env var is not set (or falsy) OR if HippiusPresign.from_env() returns
None (token/bucket not configured), publish_miner_results is a no-op.

This module never raises into the caller; every public function is
best-effort.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hippius_presign import HippiusPresign
    from .store import Store

_TRUTHY = {"1", "true", "yes", "on"}
_SCHEMA = "cathedral.v2.results.v1"


def results_publish_enabled() -> bool:
    return os.environ.get("CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED", "").strip().lower() in _TRUTHY


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def publish_miner_results(
    store: "Store",
    hip: "HippiusPresign",
    hotkey: str,
    epoch: int,
) -> str | None:
    """Query v2_submit_events for (hotkey, epoch), build a results JSON, and
    push it to Hippius at ``results/<hotkey>.json``.

    Returns the presigned GET url on success, None on any failure.
    Never raises.
    """
    try:
        raw_rows = store.query(
            "SELECT challenge_id, tier, seq, status, weighted_score, "
            "verified_at_iso, challenge_kind "
            "FROM v2_submit_events "
            "WHERE miner_hotkey=? AND epoch=? "
            "ORDER BY seq ASC",
            (hotkey, int(epoch)),
        )
        # store.query returns sqlite3.Row objects; convert to plain dicts so
        # .get() is available (mirrors the _row_to_dict pattern in v2_pipeline).
        rows = [{k: r[k] for k in r.keys()} for r in raw_rows]
        items: list[dict[str, Any]] = []
        total_verified = 0
        total_score = 0.0
        for r in rows:
            status = str(r.get("status") or "")
            score = float(r.get("weighted_score") or 0.0)
            items.append({
                "challenge_id": str(r.get("challenge_id") or ""),
                "tier": int(r.get("tier") or 0),
                "seq": int(r.get("seq") or 0),
                "status": status,
                "weighted_score": score if status == "verified" else 0.0,
                "verified_at": r.get("verified_at_iso"),
            })
            if status == "verified":
                total_verified += 1
                total_score += score

        payload: dict[str, Any] = {
            "schema": _SCHEMA,
            "miner_hotkey": hotkey,
            "epoch": int(epoch),
            "updated_at": _now_iso(),
            "total_verified": total_verified,
            "total_score": round(total_score, 6),
            "items": items,
        }
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        key = f"results/{hotkey}.json"
        url = hip.put(key, data, content_type="application/json; charset=utf-8", get_ttl=7200)
        return url
    except Exception as exc:
        print(f"[results_publisher] publish_failed hotkey={hotkey} epoch={epoch} error={exc!r}")
        return None


def publish_changed_miners(
    store: "Store",
    hip: "HippiusPresign",
    results: list[dict[str, Any]],
) -> None:
    """Given the flat list of verify results from one tick, publish each
    distinct (hotkey, epoch) exactly once.

    Best-effort: individual publish failures are logged but do not propagate.
    Silently returns if not enabled or hip is None.
    """
    if not results_publish_enabled():
        return
    if hip is None:
        return
    seen: set[tuple[str, int]] = set()
    for r in results:
        hk = str(r.get("miner_hotkey") or "")
        ep_raw = r.get("epoch")
        if not hk or ep_raw is None:
            continue
        key = (hk, int(ep_raw))
        if key in seen:
            continue
        seen.add(key)
        publish_miner_results(store, hip, hk, int(ep_raw))
