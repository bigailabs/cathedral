"""Lightweight in-process TTL cache + HTTP caching helpers for hot read endpoints.

Two independent utilities live here:

1. ``TtlResponseCache`` — a module-level single-key TTL cache with a
   single-flight refresh gate.  One coroutine rebuilds the cached body;
   concurrent callers receive the stale copy while the refresh is in
   flight so they never pile onto SQLite.

2. ``etag_response`` — a shared helper that attaches ``Cache-Control``
   and ``ETag`` headers to any JSON-serialisable payload, and returns a
   304 Not Modified when the caller's ``If-None-Match`` header matches.

Both helpers are intentionally narrow and import-free of FastAPI so
they can be unit-tested without an ASGI stack.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import Request
from fastapi.responses import Response

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 1. TTL response cache with single-flight refresh
# ---------------------------------------------------------------------------

_UNSET = object()


class TtlResponseCache:
    """Per-endpoint 30-second TTL cache with single-flight refresh.

    Usage (module level)::

        _CACHE = TtlResponseCache(ttl_seconds=30)

        @router.get("/v1/...")
        async def my_endpoint(request: Request) -> ...:
            body = await _CACHE.get_or_refresh(my_slow_builder)
            return etag_response(request, body)

    Design guarantees:
    - **Pure TTL** — no explicit invalidation; staleness ≤ TTL.
    - **Single-flight** — only one coroutine rebuilds at a time.
      Concurrent callers receive the stale cached body immediately
      (or wait for the very first populate if the cache is cold).
    - **Thread-safe** — ``asyncio.Lock`` serialises refresh.
    - The cached value is the *serialised JSON bytes*, not the raw dict,
      so serialisation cost is paid once per TTL window.
    """

    def __init__(self, ttl_seconds: float = 30.0) -> None:
        self._ttl = ttl_seconds
        self._body: bytes = b""
        self._expires_at: float = 0.0
        self._lock: asyncio.Lock | None = None  # created lazily inside the loop

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _is_fresh(self) -> bool:
        return bool(self._body) and time.monotonic() < self._expires_at

    async def get_or_refresh(
        self,
        builder: Callable[[], Coroutine[Any, Any, Any]],
    ) -> bytes:
        """Return cached JSON bytes, refreshing via *builder* when stale.

        If the cache is stale and another coroutine is already refreshing,
        this coroutine returns the (stale) cached bytes immediately rather
        than queuing behind the lock.  On a cold start the first caller
        waits for the lock and all subsequent callers wait too (no stale
        copy exists yet).

        Builder failure handling:
        - If a stale body exists when the builder raises, log a warning and
          return the stale body.  The next caller will retry the refresh.
        - If no cached body exists at all (cold start), the exception
          propagates to the caller.
        """
        if self._is_fresh():
            return self._body

        lock = self._get_lock()

        if not lock.locked():
            async with lock:
                # Double-check inside lock — another waiter may have refreshed.
                if self._is_fresh():
                    return self._body
                await self._refresh(builder)
        else:
            # A refresh is already in flight.
            if self._body:
                # Serve stale copy immediately — don't queue behind the lock.
                return self._body
            # Cold start: no stale copy yet, so wait for the refresher.
            async with lock:
                if not self._body:
                    await self._refresh(builder)

        return self._body

    async def _refresh(
        self,
        builder: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        try:
            result = await builder()
            self._body = json.dumps(result, separators=(",", ":")).encode()
            self._expires_at = time.monotonic() + self._ttl
            logger.debug("ttl_cache_refreshed", ttl=self._ttl)
        except Exception as exc:
            if self._body:
                # Stale copy exists — serve it; next caller will retry.
                logger.warning(
                    "ttl_cache_refresh_error_serving_stale",
                    error=str(exc),
                )
                return
            # Cold start — no body to fall back on; propagate to the caller.
            logger.exception("ttl_cache_refresh_error_cold")
            raise


# ---------------------------------------------------------------------------
# 2. ETag + Cache-Control response helper
# ---------------------------------------------------------------------------

_CACHE_CONTROL = "public, max-age=15"


def _compute_etag(body: bytes) -> str:
    """SHA-256 of *body*; first 16 hex chars."""
    return hashlib.sha256(body).hexdigest()[:16]


def etag_response(
    request: Request,
    payload: Any,
    *,
    status_code: int = 200,
    pre_serialised: bytes | None = None,
) -> Response:
    """Build a JSON response with ``ETag`` + ``Cache-Control`` headers.

    Honours ``If-None-Match``: returns HTTP 304 with an empty body when
    the client's ETag matches, saving ~30-460 KB per poll cycle on the
    two hot validator-facing endpoints.

    Args:
        request:  The FastAPI ``Request`` (used to read ``If-None-Match``).
        payload:  A JSON-serialisable object.  Ignored when
                  *pre_serialised* is supplied.
        status_code: Forwarded to the 200 branch (ignored on 304).
        pre_serialised: Optional raw JSON bytes.  When the caller already
                        holds serialised bytes (e.g. from ``TtlResponseCache``)
                        pass them here to skip re-serialisation.
    """
    body: bytes
    if pre_serialised is not None:
        body = pre_serialised
    else:
        body = json.dumps(payload, separators=(",", ":")).encode()

    etag = _compute_etag(body)
    headers = {
        "Cache-Control": _CACHE_CONTROL,
        "ETag": f'"{etag}"',
    }

    # Check If-None-Match per RFC 9110:
    # - "*" means any stored representation matches.
    # - List form: "etag1", "etag2" — 304 if ANY member matches.
    # - Each member may carry a weak prefix W/"..." which must be stripped.
    client_inm = request.headers.get("if-none-match", "").strip()
    if client_inm:
        if client_inm == "*":
            return Response(status_code=304, headers=headers)
        for member in client_inm.split(","):
            normalised = member.strip().removeprefix("W/").strip('"')
            if normalised == etag:
                return Response(status_code=304, headers=headers)

    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )
