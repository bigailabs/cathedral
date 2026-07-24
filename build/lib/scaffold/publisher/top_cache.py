"""Fast pre-aggregated miner ranking cache (KEYSTONE TASK — /v1/leaderboard/top).

The leaderboard aggregate query (24h window, GROUP BY miner_hotkey, JSON
aggregate) takes 15-16 seconds against ~6M rows. Running it per-request is
untenable. This module runs it ONCE per 45 seconds in a background thread and
serves the result from memory — every request is a pure in-memory dict copy, no
DB work.

The first build is synchronous (called in start()) so the very first request
after startup is fast, not cold.

The pattern mirrors board_cache.py: a TopCache class + a module-level registry
so build_app() can register the cache and start_all() can wire it to the store
in one line.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

# How often the background thread re-runs the aggregate query (seconds).
TOP_CACHE_INTERVAL_SECS = int(os.environ.get("CATHEDRAL_TOP_CACHE_INTERVAL_SECS", "120"))

# Window in hours — only 24h is implemented; other values fall back to this.
TOP_CACHE_WINDOW_H = 24
TOP_CACHE_LOCK_NAME = "cathedral:top-cache:refresh"


def _slow_build_log_secs() -> float:
    try:
        return float(os.environ.get("CATHEDRAL_TOP_CACHE_SLOW_LOG_SECS", "2.0") or "0")
    except ValueError:
        return 2.0


def enabled() -> bool:
    """Receipt leaderboard aggregation is optional production load.

    The default is off because the current payment leaderboard can be served
    from the in-memory signed weight vector. Receipt enrichment is useful for
    dashboards, but it must not compete with submit ingress on small runtimes.
    """
    return os.environ.get("CATHEDRAL_TOP_CACHE_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


class TopCache:
    """In-process cache of the pre-aggregated miner ranking.

    Background thread wakes every TOP_CACHE_INTERVAL_SECS and runs the heavy
    GROUP-BY aggregate; results are held in memory and served to all concurrent
    requests without touching the DB."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[dict[str, Any]] = []
        self._built_at_iso: str = ""
        self._window_h: int = TOP_CACHE_WINDOW_H
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._store = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> tuple[list[dict[str, Any]], str, int]:
        """Return (rows, built_at_iso, window_h). Pure in-memory — no DB."""
        with self._lock:
            return list(self._rows), self._built_at_iso, self._window_h

    def start(self, store) -> None:
        """Launch the background refresh loop; first build happens in that thread.

        We do NOT block here: the initial build takes ~15 seconds against 6M rows
        and would delay process startup + Railway health checks. Instead the
        background thread runs the first build immediately, and requests that arrive
        before it finishes return an empty list (the page shows its loading state).
        Subsequent requests (after the first 45 s cycle) are always fast.
        """
        if not enabled():
            return
        self._store = store
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop_with_immediate_build, name="top-cache-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _loop_with_immediate_build(self) -> None:
        """Thread target: build immediately, then loop every interval."""
        self._build()
        while not self._stop_event.wait(TOP_CACHE_INTERVAL_SECS):
            self._build()

    def _build(self) -> None:
        """Run the aggregate query and atomically swap the cached result."""
        store = self._store
        if store is None:
            return
        started = time.monotonic()
        rows_count = 0
        ok = False
        try:
            with store.advisory_lock(TOP_CACHE_LOCK_NAME) as acquired:
                if not acquired:
                    return
                if getattr(store, "_is_postgres", False) or store.backend == "postgres":
                    rows = self._build_postgres(store)
                else:
                    rows = self._build_sqlite(store)
            rows_count = len(rows)
            built_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
            with self._lock:
                self._rows = rows
                self._built_at_iso = built_at
                self._window_h = TOP_CACHE_WINDOW_H
            ok = True
        except Exception as exc:  # never crash the process
            print(f"[top_cache] build error (will retry): {exc!r}")
        finally:
            elapsed = time.monotonic() - started
            threshold = _slow_build_log_secs()
            if threshold > 0 and elapsed >= threshold:
                print(
                    "[top_cache] slow_build "
                    f"ok={ok} rows={rows_count} elapsed={elapsed:.3f}s"
                )

    def _build_postgres(self, store) -> list[dict[str, Any]]:
        sql = """
SELECT
    e.miner_hotkey,
    MAX(e.row_json::jsonb->>'agent_display_name') as display_name,
    COUNT(DISTINCT (e.row_json::jsonb->>'task_id_public')) as distinct_solves,
    SUM((e.row_json::jsonb->>'weighted_score')::float) as total_score,
    MAX(e.ran_at) as last_seen
FROM eval_runs e
WHERE e.ran_at >= to_char(
    NOW() AT TIME ZONE 'UTC' - INTERVAL '24 hours',
    'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'
)
GROUP BY e.miner_hotkey
ORDER BY total_score DESC
LIMIT 100
"""
        raw_rows = store.query(sql)
        return [
            {
                "miner_hotkey": r["miner_hotkey"],
                "display_name": r["display_name"],
                "distinct_solves": int(r["distinct_solves"] or 0),
                "total_score": float(r["total_score"] or 0.0),
                "last_seen": r["last_seen"],
            }
            for r in raw_rows
        ]

    def _build_sqlite(self, store) -> list[dict[str, Any]]:
        """SQLite fallback: fetch recent 1 000 rows and aggregate in Python.

        SQLite does not support the Postgres JSON operators or the INTERVAL
        syntax, so we pull a bounded window and crunch it in memory. Good
        enough for dev/test; never hits prod where Postgres runs."""
        import json as _json

        cutoff = datetime.now(timezone.utc)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT") + "00:00:00.000Z"
        # Derive a 24h cutoff string the same way the Postgres query does
        # (string comparison works because ran_at is ISO8601 UTC).
        from datetime import timedelta
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=TOP_CACHE_WINDOW_H)
        cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{cutoff_dt.microsecond // 1000:03d}Z"

        raw_rows = store.query(
            "SELECT miner_hotkey, ran_at, row_json FROM eval_runs "
            "WHERE ran_at >= ? ORDER BY ran_at DESC LIMIT 1000",
            (cutoff_str,),
        )

        agg: dict[str, dict[str, Any]] = {}
        for r in raw_rows:
            hk = r["miner_hotkey"]
            try:
                rj = _json.loads(r["row_json"])
            except Exception:
                continue
            ws = float(rj.get("weighted_score") or 0.0)
            tid = rj.get("task_id_public") or ""
            dn = rj.get("agent_display_name") or ""
            ran = r["ran_at"]
            if hk not in agg:
                agg[hk] = {
                    "miner_hotkey": hk,
                    "display_name": dn,
                    "task_ids": set(),
                    "total_score": 0.0,
                    "last_seen": ran,
                }
            entry = agg[hk]
            entry["total_score"] += ws
            if tid:
                entry["task_ids"].add(tid)
            if dn:
                entry["display_name"] = dn
            if ran > entry["last_seen"]:
                entry["last_seen"] = ran

        result = [
            {
                "miner_hotkey": v["miner_hotkey"],
                "display_name": v["display_name"],
                "distinct_solves": len(v["task_ids"]),
                "total_score": v["total_score"],
                "last_seen": v["last_seen"],
            }
            for v in agg.values()
        ]
        result.sort(key=lambda x: x["total_score"], reverse=True)
        return result[:100]


# --------------------------------------------------------------------------
# Process-global registry — mirrors board_cache.py pattern.
# build_app() calls register(cache) once; tests or multi-app setups can call
# start_all(store) to wire every registered cache to the same store.
# --------------------------------------------------------------------------
_registry: list[TopCache] = []
_registry_lock = threading.Lock()


def register(cache: TopCache) -> None:
    with _registry_lock:
        _registry.append(cache)


def start_all(store) -> None:
    """Start every registered TopCache against the given store."""
    with _registry_lock:
        caches = list(_registry)
    for c in caches:
        c.start(store)
