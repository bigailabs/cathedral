"""Prebuilt dashboard state snapshot.

The dashboard endpoint is a read fan-in surface: leaderboard, PM health, queue
lag, weights freshness, endpoint pressure, and rejection reasons. Building that
on every request would recreate the slow dashboard path this reliability track is
trying to remove.

This module owns a timer-built, in-memory snapshot. The request path only reads
the last completed payload. It never runs the builders and never falls back to DB
queries when cold.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


SCHEMA = "cathedral.dashboard_state.v1"
SNAPSHOT_NAME = "dashboard-state"


def enabled() -> bool:
    return os.environ.get(
        "CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def refresh_secs() -> float:
    try:
        return max(1.0, float(
            os.environ.get("CATHEDRAL_DASHBOARD_SNAPSHOT_REFRESH_SECS", "10") or "10"
        ))
    except ValueError:
        return 10.0


def max_stale_secs() -> float:
    try:
        return max(0.0, float(
            os.environ.get("CATHEDRAL_DASHBOARD_SNAPSHOT_MAX_STALE_SECS", "120") or "120"
        ))
    except ValueError:
        return 120.0


def _slow_build_log_secs() -> float:
    try:
        return max(0.0, float(
            os.environ.get("CATHEDRAL_DASHBOARD_SNAPSHOT_SLOW_LOG_SECS", "1.0") or "1.0"
        ))
    except ValueError:
        return 1.0


def _now_iso_ms() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _snapshot_id(payload: dict[str, Any], built_at: str) -> str:
    material = json.dumps(
        {"built_at": built_at, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "dash_" + hashlib.sha256(material).hexdigest()[:24]


class DashboardStateSnapshot:
    """Timer-refreshed dashboard payload.

    The builder returns the expensive section payloads. refresh_once() stamps the
    provenance fields. get() returns a deep copy with age_seconds updated from
    monotonic time, which is cheap and does not touch external state.
    """

    def __init__(self, builder: Callable[[], dict[str, Any]]) -> None:
        self._builder = builder
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._built_monotonic = 0.0
        self._builds = 0
        self._build_errors = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def refresh_once(self) -> bool:
        started = time.monotonic()
        ok = False
        try:
            payload = dict(self._builder())
            built_at = _now_iso_ms()
            payload.setdefault("schema", SCHEMA)
            payload.setdefault("data_status", "ok")
            payload.setdefault("source_epoch", None)
            payload.setdefault("source_block", None)
            payload["built_at"] = built_at
            payload["age_seconds"] = 0.0
            payload["snapshot_id"] = _snapshot_id(payload, built_at)
            with self._lock:
                self._payload = payload
                self._built_monotonic = time.monotonic()
                self._builds += 1
            ok = True
        except Exception as exc:
            with self._lock:
                self._build_errors += 1
            print(f"[dashboard_snapshot] build_error error={exc!r}")
        finally:
            elapsed = time.monotonic() - started
            threshold = _slow_build_log_secs()
            if threshold > 0 and elapsed >= threshold:
                print(
                    "[dashboard_snapshot] slow_build "
                    f"ok={ok} elapsed={elapsed:.3f}s"
                )
        return ok

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            if self._payload is None:
                return None
            age = time.monotonic() - self._built_monotonic
            ceiling = max_stale_secs()
            if ceiling > 0 and age > ceiling:
                return None
            payload = copy.deepcopy(self._payload)
            payload["age_seconds"] = round(age, 3)
            payload["builds"] = self._builds
            payload["build_errors"] = self._build_errors
            return payload

    def start(self) -> None:
        if not enabled():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="dashboard-state-snapshot",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        self.refresh_once()
        while not self._stop_event.wait(refresh_secs()):
            self.refresh_once()


def unavailable_payload(status: str, *, reason: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "data_status": status,
        "reason": reason,
        "snapshot_id": None,
        "built_at": None,
        "age_seconds": None,
        "source_epoch": None,
        "source_block": None,
        "earnings_leaderboard": {"data_status": status, "miners": []},
        "pm_health": {"data_status": status},
        "queue_lag": {"data_status": status},
        "weights_freshness": {"data_status": status},
        "endpoint_pressure": {"data_status": status},
        "rejection_reasons": {"data_status": status, "items": []},
        "errors": [],
    }


def public_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the public dashboard view.

    The background snapshot may include operational pressure and queue timing
    that belongs on admin surfaces. The public endpoint stays useful for miners
    but does not publish internals that help time submit floods.
    """
    out = copy.deepcopy(payload)
    out["endpoint_pressure"] = {"data_status": "admin_only"}
    out["queue_lag"] = {"data_status": "admin_only"}
    sources = out.get("sources")
    if isinstance(sources, dict):
        sources["endpoint_pressure"] = "/v1/admin/synthetic-boolean/submit-metrics"
        sources["queue_lag"] = "/v1/admin/synthetic-boolean/submit-metrics"
    return out


def response_headers(payload: dict[str, Any]) -> dict[str, str]:
    status = str(payload.get("data_status") or "")
    if status in {"ok", "partial"}:
        max_age = int(os.environ.get("CATHEDRAL_DASHBOARD_SNAPSHOT_MAX_AGE", "2"))
        return {
            "Cache-Control": f"public, max-age={max_age}",
            "Access-Control-Allow-Origin": "*",
            "X-Cathedral-Snapshot": SNAPSHOT_NAME,
            "X-Cathedral-Snapshot-Id": str(payload.get("snapshot_id") or ""),
            "X-Cathedral-Snapshot-Built-At": str(payload.get("built_at") or ""),
            "X-Cathedral-Snapshot-Age-Secs": str(payload.get("age_seconds") or ""),
        }
    return {"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}


def section(
    name: str,
    builder: Callable[[], Any],
    fallback: Any,
    errors: list[dict[str, str]],
) -> Any:
    try:
        return builder()
    except Exception as exc:
        errors.append({"section": name, "error": type(exc).__name__})
        return fallback


def rejection_reason_counts(store, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT COALESCE(rejection_reason, 'unknown') AS reason, COUNT(*) AS n "
        "FROM per_miner_attempts "
        "WHERE status!='ranked' "
        "AND (challenge_kind IS NULL OR challenge_kind!='per_miner_shadow') "
        "GROUP BY COALESCE(rejection_reason, 'unknown') "
        "ORDER BY n DESC, reason LIMIT ?",
        (limit,),
    )
    return [
        {"reason": str(r["reason"]), "count": int(r["n"] or 0)}
        for r in rows
    ]
