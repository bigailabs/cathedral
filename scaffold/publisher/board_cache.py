"""Active-board broadcast cache (KEYSTONE TASK 3a — broadcast tier).

The board (the active challenge set) is ~51 entries changing ~hourly. Today every
miner poll runs `SELECT * FROM lane_challenges WHERE status='active'` against the
one DB connection — exactly the read pressure that starves writes and wedges the
service. The board is a BROADCAST, not a per-request query: build the JSON once,
serve it from memory to everyone, and put proper ETag / Cache-Control on it so an
edge/CDN holds it and the publisher's read load for the board goes to ~zero.

Invalidation: the snapshot is rebuilt only when the active set actually changes
(mint / retire / lock — anything that mutates lane_challenges.status) via
`invalidate()`, plus a short TTL safety net so a missed invalidation self-heals
within seconds rather than serving stale forever. Reads never touch the DB unless
the snapshot is missing or the TTL has lapsed.

The ETag is a hash of the rendered payload, so a CDN / conditional GET gets a
cheap 304 whenever the board hasn't changed — the common case at miner poll rates.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import Any, Callable

# Safety-net TTL: even with explicit invalidation on every mutation, rebuild at
# most this stale so a missed invalidate self-heals. Cheap (~51 rows) so a low
# value is fine; the whole point is that the EDGE caches, not that we never query.
BOARD_TTL_SECS = float(os.environ.get("CATHEDRAL_BOARD_TTL_SECS", "60"))

# What an edge/CDN is told to cache the board for. Short, because the board does
# change (~hourly) and a mint should show up quickly; the ETag makes the steady
# state a 304 anyway. Tunable per-deploy.
BOARD_CACHE_MAX_AGE = int(os.environ.get("CATHEDRAL_BOARD_CACHE_MAX_AGE", "15"))


class BoardCache:
    """In-process cached snapshot of the active board JSON.

    Construct with a `builder()` that returns the fully-rendered board dict
    (the same dict the route used to compute per request). The cache calls it
    only on a cold/expired/invalidated read, under a lock, and memoizes the
    payload bytes + ETag."""

    def __init__(self, builder: Callable[[], dict[str, Any]]) -> None:
        self._builder = builder
        self._lock = threading.Lock()
        self._payload: dict[str, Any] | None = None
        self._etag: str | None = None
        self._built_at: float = 0.0
        self._dirty = True
        self._rebuilds = 0  # observability: how many times the DB was actually hit
        self._refreshing = False

    def invalidate(self) -> None:
        """Mark the snapshot stale — next read rebuilds. Call on any mutation of
        the active set (mint / retire / lock)."""
        with self._lock:
            self._dirty = True

    def _fresh(self, now: float) -> bool:
        return (
            self._payload is not None
            and not self._dirty
            and (now - self._built_at) < BOARD_TTL_SECS
        )

    def get(self) -> tuple[dict[str, Any], str]:
        """Return (payload_dict, etag). Rebuilds from the DB only when cold,
        invalidated, or past the TTL; otherwise pure in-memory."""
        now = time.time()
        if self._fresh(now):
            return self._payload, self._etag  # type: ignore[return-value]
        if self._payload is not None and self._etag is not None:
            self._schedule_refresh()
            return self._payload, self._etag
        with self._lock:
            # re-check under lock (another thread may have just rebuilt)
            if self._fresh(time.time()):
                return self._payload, self._etag  # type: ignore[return-value]
            self._rebuild_locked()
            return self._payload, self._etag  # type: ignore[return-value]

    def warm_async(self) -> None:
        """Start a background refresh so the first miner read is not cold."""
        self._schedule_refresh()

    def _rebuild_locked(self) -> None:
        payload = self._builder()
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        etag = 'W/"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        self._payload = payload
        self._etag = etag
        self._built_at = time.time()
        self._dirty = False
        self._rebuilds += 1

    def _schedule_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True

        def _run() -> None:
            try:
                with self._lock:
                    if not self._fresh(time.time()):
                        self._rebuild_locked()
            finally:
                with self._lock:
                    self._refreshing = False

        threading.Thread(target=_run, name="board-cache-refresh", daemon=True).start()

    @property
    def rebuild_count(self) -> int:
        return self._rebuilds


def board_cache_headers(etag: str) -> dict[str, str]:
    """Cache-Control + ETag so an edge/CDN caches the board and conditional GETs
    short-circuit to 304 while it is unchanged."""
    return {
        "Cache-Control": f"public, max-age={BOARD_CACHE_MAX_AGE}",
        "ETag": etag,
    }


# --------------------------------------------------------------------------
# Process-global invalidation registry.
#
# Code that mutates the active set (seed_challenge on mint, the submit lock,
# the refill retire/mint loop) lives in several modules and does not hold a
# reference to the app's BoardCache. Rather than thread a callback through every
# one, a BoardCache registers itself here and mutators call `invalidate_all()`.
# Decoupled, cheap, and the TTL safety net covers anything that forgets to call.
# --------------------------------------------------------------------------
_registry: list[BoardCache] = []
_registry_lock = threading.Lock()


def register(cache: BoardCache) -> None:
    with _registry_lock:
        _registry.append(cache)


def invalidate_all() -> None:
    """Mark every registered board snapshot stale. Call after any mutation of
    lane_challenges.status (mint / retire / lock)."""
    with _registry_lock:
        caches = list(_registry)
    for c in caches:
        c.invalidate()
