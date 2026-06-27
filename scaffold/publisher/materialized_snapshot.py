"""Timer-built materialized snapshots for board + leaderboard-top reads
(Track 3 / item 6 — serve reads from a materialized snapshot).

The board and leaderboard-top read endpoints already sit behind in-process
caches (board_cache.py, top_cache.py). Those caches still rebuild on the request
path when cold/expired (board_cache) or expose a possibly-empty result before the
first timer build (top_cache). Under read pressure that coupling — a read that
triggers a build — is exactly what we want to remove from the hot path.

This module materializes the *fully-rendered response payload* of a read endpoint
on a fixed timer (like the signed weight vector's ~60s refresh) and serves the
last-materialized payload to the route with strict stale-while-revalidate
semantics: a read NEVER blocks on a build, and a failed build keeps serving the
previous good snapshot rather than erroring. The request path becomes a pure
in-memory dict copy decoupled from live handling.

DEFAULT-OFF: nothing here runs unless CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED is
truthy. When the flag is unset the snapshots are never started and the routes
fall through to their existing behavior, byte-for-byte.

Mirrors the registry pattern of board_cache.py / top_cache.py so build_app() can
register snapshots once and start_all(...) wires them to their builders in one
line. The serialized payload + ETag are also exposed so an edge worker can mirror
them to KV as a last-known-good (see deploy/edge-router/board-failover/).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable


# How often the background thread re-materializes each snapshot (seconds). The
# weight vector refreshes ~every 60s; the board + leaderboard-top change on a
# similar cadence, so 60s keeps the materialized payload well inside the read
# freshness budget while taking the build entirely off the request path.
SNAPSHOT_REFRESH_SECS = int(
    os.environ.get("CATHEDRAL_MATERIALIZED_SNAPSHOT_REFRESH_SECS", "60")
)

# Hard staleness ceiling. Even with stale-while-revalidate, refuse to serve a
# snapshot older than this so a wedged builder eventually surfaces rather than
# serving indefinitely-stale data. 0 disables the ceiling (serve any age).
SNAPSHOT_MAX_STALE_SECS = float(
    os.environ.get("CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_STALE_SECS", "900")
)


def enabled() -> bool:
    """Materialized snapshot serving is opt-in.

    Default-off: when unset, snapshots are never started and read routes keep
    their current behavior unchanged. Flip the flag per-deploy once the timer
    build is observed healthy."""
    return os.environ.get(
        "CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _slow_build_log_secs() -> float:
    try:
        return float(
            os.environ.get("CATHEDRAL_MATERIALIZED_SNAPSHOT_SLOW_LOG_SECS", "2.0") or "0"
        )
    except ValueError:
        return 2.0


def _now_iso_ms() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class MaterializedSnapshot:
    """A single timer-built, fully-rendered read payload.

    Construct with a `name` (for logs/headers) and a `builder()` that returns the
    final response dict the route would otherwise compute per request. A daemon
    thread re-runs `builder()` every SNAPSHOT_REFRESH_SECS and atomically swaps in
    the new payload + ETag. The route calls `get()`, which is pure in-memory and
    never triggers a build — stale-while-revalidate is the timer's job, not the
    reader's.
    """

    def __init__(self, name: str, builder: Callable[[], dict[str, Any]]) -> None:
        self._name = name
        self._builder = builder
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._etag: str | None = None
        self._built_at: float = 0.0  # monotonic
        self._built_at_iso: str = ""
        self._builds = 0
        self._build_errors = 0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
        """Return (payload, etag, meta) from the last materialized build, or None
        if there is no usable snapshot (cold, or past the hard staleness ceiling).

        Pure in-memory: NEVER builds on the request path. `None` is the route's
        signal to fall back to its live (cache-backed) handling — degrade to the
        existing path, not to an error."""
        with self._lock:
            if self._payload is None or self._etag is None:
                return None
            age = time.monotonic() - self._built_at
            if SNAPSHOT_MAX_STALE_SECS > 0 and age > SNAPSHOT_MAX_STALE_SECS:
                # Too stale to serve safely — let the route fall back to live.
                return None
            meta = {
                "snapshot": self._name,
                "built_at": self._built_at_iso,
                "age_secs": round(age, 3),
                "builds": self._builds,
                "build_errors": self._build_errors,
            }
            return dict(self._payload), self._etag, meta

    def serialized(self) -> tuple[bytes, str] | None:
        """Return (body_bytes, etag) of the last materialized payload, or None.

        Stable serialization (sorted keys, compact separators) so the ETag is the
        hash of exactly these bytes — the form an edge worker mirrors to KV as a
        last-known-good (see deploy/edge-router/board-failover/)."""
        with self._lock:
            if self._payload is None or self._etag is None:
                return None
            body = json.dumps(
                self._payload, sort_keys=True, separators=(",", ":")
            ).encode()
            return body, self._etag

    def start(self) -> None:
        """Launch the background refresh loop (idempotent). No-op when disabled.

        We do not block on the first build: it runs in the daemon thread, so a
        slow first build never delays process startup / health checks. Reads
        before the first build complete return None (route falls back to live)."""
        if not enabled():
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop_with_immediate_build,
            name=f"materialized-snapshot-{self._name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def build_count(self) -> int:
        return self._builds

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _loop_with_immediate_build(self) -> None:
        self.refresh_once()
        while not self._stop_event.wait(SNAPSHOT_REFRESH_SECS):
            self.refresh_once()

    def refresh_once(self) -> bool:
        """Run the builder once and atomically swap in the result.

        Returns True on a successful build. On failure the previous good snapshot
        is KEPT (stale-while-revalidate degrade-to-stale, never to error) and the
        error is counted + logged. Exposed for tests so the timer can be driven
        deterministically without sleeping."""
        started = time.monotonic()
        ok = False
        try:
            payload = self._builder()
            body = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode()
            etag = 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'
            with self._lock:
                self._payload = payload
                self._etag = etag
                self._built_at = time.monotonic()
                self._built_at_iso = _now_iso_ms()
                self._builds += 1
            ok = True
        except Exception as exc:  # never crash the timer; keep last good snapshot
            with self._lock:
                self._build_errors += 1
            print(f"[materialized_snapshot] {self._name} build error (keeping last good): {exc!r}")
        finally:
            elapsed = time.monotonic() - started
            threshold = _slow_build_log_secs()
            if threshold > 0 and elapsed >= threshold:
                print(
                    "[materialized_snapshot] slow_build "
                    f"name={self._name} ok={ok} elapsed={elapsed:.3f}s"
                )
        return ok


def snapshot_headers(etag: str, meta: dict[str, Any]) -> dict[str, str]:
    """Edge/CDN cache headers + observability markers for a materialized read.

    Mirrors board_cache_headers: a short shared-cache window with
    stale-while-revalidate so the edge holds the snapshot and conditional GETs
    short-circuit to 304 while it is unchanged."""
    max_age = int(os.environ.get("CATHEDRAL_MATERIALIZED_SNAPSHOT_MAX_AGE", "15"))
    swr = int(os.environ.get("CATHEDRAL_MATERIALIZED_SNAPSHOT_SWR_SECS", "1200"))
    return {
        "Cache-Control": f"public, max-age={max_age}, stale-while-revalidate={swr}",
        "ETag": etag,
        "X-Cathedral-Snapshot": str(meta.get("snapshot", "")),
        "X-Cathedral-Snapshot-Built-At": str(meta.get("built_at", "")),
        "X-Cathedral-Snapshot-Age-Secs": str(meta.get("age_secs", "")),
    }


# --------------------------------------------------------------------------
# Process-global registry — mirrors board_cache.py / top_cache.py.
# build_app() registers each snapshot once; start_all() starts every registered
# snapshot (no-op when the feature flag is off).
# --------------------------------------------------------------------------
_registry: list[MaterializedSnapshot] = []
_registry_lock = threading.Lock()


def register(snapshot: MaterializedSnapshot) -> None:
    with _registry_lock:
        _registry.append(snapshot)


def start_all() -> None:
    """Start every registered snapshot. No-op unless the feature flag is on."""
    if not enabled():
        return
    with _registry_lock:
        snapshots = list(_registry)
    for s in snapshots:
        s.start()


def stop_all() -> None:
    with _registry_lock:
        snapshots = list(_registry)
    for s in snapshots:
        s.stop()
