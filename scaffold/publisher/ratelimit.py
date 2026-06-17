"""Per-key in-process sliding-window rate limiter (anti-flood backpressure).

Purpose: secondary protection against miner floods that hammer CNF-fetch,
active-cnf, and submit endpoints.  This is NOT the primary fix (that is the
in-process immutable CNF cache in cnf_store.py) — it is defence-in-depth to
block hot-loop bugs and rogue miners before they touch the pool at all.

Design:
  * Key: X-Cathedral-Hotkey header when present, else client IP (as seen by
    the ASGI scope — may be a Railway proxy address, which still identifies
    a "lane").
  * Window: sliding 60-second count (cheap O(1) check: store (window_start,
    count) per key; reset on window expiry).
  * Limit: CATHEDRAL_RATELIMIT_RPM default 120 req/min/key.
    Set to 0 to disable entirely (e.g. during debugging or if miners complain).
  * On exceed: 429 + Retry-After: 60.
  * Exempt paths: /health, /v1/validator/weights/next (validators must never be
    throttled — they need the weights feed to set chain weights).
  * Cleanup: keys not seen in > WINDOW_SECS * 2 are pruned at a low rate to
    bound memory.  At 300 miners each with one key entry that is trivial.

Usage (in build_app):
    from .ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
"""
from __future__ import annotations

import os
import time
import threading

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

# Default: 120 req/min/key.  Set env to 0 to disable.
_DEFAULT_RPM = 120
_WINDOW_SECS = 60

# Paths that are NEVER rate-limited regardless of key.
_EXEMPT_SUFFIXES: tuple[str, ...] = (
    "/health",
    "/v1/validator/weights/next",
    "/.well-known/cathedral-jwks.json",
)

# Prefix for the legacy path-strip compat so exempt check works both ways.
_LEGACY_PREFIX = "/api/cathedral"


def _is_exempt(path: str) -> bool:
    # Strip legacy prefix if present (middleware runs after strip, but be safe).
    p = path.removeprefix(_LEGACY_PREFIX)
    return any(p == s or p.endswith(s) for s in _EXEMPT_SUFFIXES)


def _ratelimit_rpm() -> int:
    """Current effective RPM limit (reads env at call time so it is hot-reloadable
    via env on Railway without a redeploy)."""
    try:
        return int(os.environ.get("CATHEDRAL_RATELIMIT_RPM", str(_DEFAULT_RPM)))
    except ValueError:
        return _DEFAULT_RPM


class _WindowEntry:
    """Sliding-window state for one key: (window_start, count)."""
    __slots__ = ("window_start", "count", "last_seen")

    def __init__(self, now: float) -> None:
        self.window_start: float = now
        self.count: int = 0
        self.last_seen: float = now

    def tick(self, now: float) -> int:
        """Increment and return count within the current window."""
        if now - self.window_start >= _WINDOW_SECS:
            self.window_start = now
            self.count = 0
        self.count += 1
        self.last_seen = now
        return self.count


class _RateLimiterState:
    """Thread-safe per-key state store with periodic GC."""

    _GC_EVERY_N = 500          # GC check every N requests across all keys
    _GC_IDLE_TTL = _WINDOW_SECS * 4  # evict keys idle for 4 windows

    def __init__(self) -> None:
        self._entries: dict[str, _WindowEntry] = {}
        self._lock = threading.Lock()
        self._req_count = 0

    def check(self, key: str, limit: int) -> bool:
        """Return True if the request is ALLOWED, False if it should be 429'd."""
        if limit <= 0:
            return True  # disabled
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _WindowEntry(now)
                self._entries[key] = entry
            count = entry.tick(now)
            self._req_count += 1
            if self._req_count >= self._GC_EVERY_N:
                self._gc(now)
                self._req_count = 0
        return count <= limit

    def _gc(self, now: float) -> None:
        """Evict idle keys (called while holding the lock)."""
        cutoff = now - self._GC_IDLE_TTL
        stale = [k for k, e in self._entries.items() if e.last_seen < cutoff]
        for k in stale:
            del self._entries[k]


_state = _RateLimiterState()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Starlette/FastAPI middleware that enforces per-key RPM limits."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.scope.get("path", "")
        if _is_exempt(path):
            return await call_next(request)

        limit = _ratelimit_rpm()
        if limit <= 0:
            return await call_next(request)

        # Key preference: hotkey header (identifies a miner) > client IP.
        key = (
            request.headers.get("x-cathedral-hotkey")
            or request.headers.get("X-Cathedral-Hotkey")
            or _client_ip(request)
        )

        if not _state.check(key, limit):
            return Response(
                content="rate_limited",
                status_code=429,
                headers={
                    "Retry-After": str(_WINDOW_SECS),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Window": f"{_WINDOW_SECS}s",
                },
            )

        return await call_next(request)


def _client_ip(request: Request) -> str:
    """Best-effort client IP from forwarded headers or direct connection."""
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    client = getattr(request, "client", None)
    if client:
        return client.host
    return "unknown"
