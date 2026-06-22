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

Implementation: pure ASGI middleware (NOT BaseHTTPMiddleware).
BaseHTTPMiddleware buffers the full response body before forwarding it, which
serializes concurrent requests and causes 20-30s stalls under load.  A pure
ASGI middleware calls the downstream app directly with the original send
callable — zero buffering, zero concurrency overhead.

Security note: current implementation keys pre-auth rate limiting by client IP,
not claimed hotkey, because signatures are verified later in endpoint handlers.

Usage (in build_app):
    from .ratelimit import RateLimitMiddleware
    app.add_middleware(RateLimitMiddleware)
"""
from __future__ import annotations

import os
import time
import threading

from starlette.types import ASGIApp, Receive, Scope, Send

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
    # Strip legacy prefix if present (middleware runs before strip, so check both).
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


def _get_header(headers: list, name: bytes) -> str | None:
    """Extract a header value from raw ASGI headers list."""
    name_lower = name.lower()
    for k, v in headers:
        if k.lower() == name_lower:
            return v.decode("latin-1", errors="replace")
    return None


def _client_ip_from_scope(scope: Scope) -> str:
    """Best-effort client IP from ASGI scope headers or direct connection."""
    headers = scope.get("headers", [])
    xff = _get_header(headers, b"x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    client = scope.get("client")
    if client:
        return client[0]
    return "unknown"


class RateLimitMiddleware:
    """Pure ASGI rate-limit middleware — no BaseHTTPMiddleware buffering.

    Passes the original scope/receive/send straight to the downstream app
    for allowed requests (zero overhead).  For 429 responses, sends the
    rejection directly using ASGI primitives without touching the downstream
    app at all.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Pass websocket / lifespan scopes straight through.
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_exempt(path):
            await self.app(scope, receive, send)
            return

        limit = _ratelimit_rpm()
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        key = _client_ip_from_scope(scope)

        if not _state.check(key, limit):
            body = b"rate_limited"
            retry_after = str(_WINDOW_SECS).encode()
            limit_str = str(limit).encode()
            window_str = f"{_WINDOW_SECS}s".encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", retry_after),
                    (b"x-ratelimit-limit", limit_str),
                    (b"x-ratelimit-window", window_str),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": body,
                "more_body": False,
            })
            return

        await self.app(scope, receive, send)
