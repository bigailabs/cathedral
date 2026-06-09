"""Per-IP token-bucket rate limiting for the public read surface.

The publisher's reads (``/v1/synthetic-boolean/active-challenges``, the CNF
fetch, ``/v1/leaderboard/*``) are unauthenticated and cheap to *request* but
not cheap to *serve* — each one can touch the DB and, on file-backed CNFs,
read up to ~1.8 MB. Under a flood (a miner tight-polling the whole board, a
scraper, or outright abuse) the worker pool wedges and legitimate miners see
the board hang — which surfaces to them as "no new challenges". Railway/CF
also bill the wasted capacity.

This middleware sheds that load at the *front door*: a per-client-IP token
bucket evaluated before any route handler runs, so an abusive IP gets a cheap
``429`` instead of consuming a DB connection or the ``db_write_lock``.

Design notes:
- **In-process state.** The publisher runs single-instance (same assumption
  as :mod:`cathedral.publisher.rate_limit`), so a process-local dict is
  sufficient — no Redis.
- **Token bucket**, not fixed window: ``burst`` capacity refilled at ``rps``
  tokens/sec. Allows brief legitimate bursts (e.g. fetching several CNFs at
  once) while capping sustained abuse.
- **Client IP behind Railway.** Railway's Envoy edge sets
  ``x-envoy-external-address`` to the immediate external peer, which a client
  cannot forge by pre-seeding ``X-Forwarded-For``. We prefer that header, then
  fall back to the left-most ``X-Forwarded-For`` entry, then the raw socket
  peer. NOTE: ``X-Forwarded-For`` is client-controllable, so an attacker who
  rotates spoofed XFF values can still dilute per-IP accounting — the durable
  fix for that is a Cloudflare (or equivalent) proxy in front of Railway. This
  middleware is the in-app backstop, not a substitute for an edge WAF.
- **Bounded memory.** The bucket table is capped; idle entries are swept
  opportunistically and the table is hard-capped so a spoofed-IP flood can't
  grow it without bound (when full, unknown IPs are treated as limited —
  fail-closed under attack).

Everything is env-tunable so production can be retuned without a redeploy.
Disabled by default so tests and local runs are unaffected; set
``CATHEDRAL_IP_RATE_LIMIT_ENABLED=true`` in the deployed environment.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

logger = structlog.get_logger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    raw = default if raw is None else raw
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def env_kwargs() -> dict[str, object]:
    """Resolve constructor kwargs from the environment.

    Used by both ``add_middleware(IpRateLimitMiddleware, **env_kwargs())`` in
    the app factory and :meth:`IpRateLimitMiddleware.from_env`.
    """
    return {
        "rps": _env_float("CATHEDRAL_IP_RATE_LIMIT_RPS", 10.0),
        "burst": _env_float("CATHEDRAL_IP_RATE_LIMIT_BURST", 120.0),
        "allowlist": _env_csv("CATHEDRAL_IP_RATE_LIMIT_ALLOWLIST", ""),
        "exempt_path_prefixes": _env_csv(
            "CATHEDRAL_IP_RATE_LIMIT_EXEMPT_PATHS",
            "/health,/api/cathedral/health",
        ),
        "trusted_peer_header": os.environ.get(
            "CATHEDRAL_IP_RATE_LIMIT_TRUSTED_HEADER", "x-envoy-external-address"
        ),
        "max_tracked": _env_int("CATHEDRAL_IP_RATE_LIMIT_MAX_TRACKED", 100_000),
    }


def ip_rate_limit_enabled() -> bool:
    return os.environ.get("CATHEDRAL_IP_RATE_LIMIT_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


class _Bucket:
    """A single client's token bucket. Mutated in place; not thread-safe.

    The publisher event loop is single-threaded, so per-bucket mutation under
    ``asyncio`` needs no lock — middleware runs to completion between awaits.
    """

    __slots__ = ("last", "tokens")

    def __init__(self, tokens: float, last: float) -> None:
        self.tokens = tokens
        self.last = last


class IpRateLimitMiddleware:
    """ASGI middleware: per-IP token bucket over HTTP requests.

    Constructed with explicit numbers so it is unit-testable; the app factory
    reads the env defaults via :func:`from_env`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        rps: float,
        burst: float,
        allowlist: tuple[str, ...] = (),
        exempt_path_prefixes: tuple[str, ...] = (),
        trusted_peer_header: str = "x-envoy-external-address",
        max_tracked: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._rps = max(0.0, float(rps))
        self._burst = max(1.0, float(burst))
        self._allowlist = frozenset(allowlist)
        self._exempt = tuple(exempt_path_prefixes)
        self._trusted_header = trusted_peer_header.lower().encode("latin-1")
        self._max_tracked = max(1, int(max_tracked))
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._last_sweep = clock()

    @classmethod
    def from_env(cls, app: ASGIApp) -> IpRateLimitMiddleware:
        return cls(app, **env_kwargs())

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        # CORS preflight must always pass — the CORS middleware answers it.
        if method == "OPTIONS":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self._exempt):
            await self._app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        client_ip = self._client_ip(scope, headers)
        if client_ip in self._allowlist:
            await self._app(scope, receive, send)
            return

        now = self._clock()
        allowed, retry_after = self._consume(client_ip, now)
        if allowed:
            await self._app(scope, receive, send)
            return

        logger.warning(
            "ip_rate_limited",
            client_ip=client_ip,
            path=path,
            retry_after=round(retry_after, 2),
        )
        await self._send_429(send, retry_after)

    # -- internals ----------------------------------------------------------

    def _client_ip(self, scope: Scope, headers: list[tuple[bytes, bytes]]) -> str:
        hdr = {k: v for k, v in headers}
        peer = hdr.get(self._trusted_header)
        if peer:
            return peer.decode("latin-1").strip().split(":")[0]
        xff = hdr.get(b"x-forwarded-for")
        if xff:
            first = xff.decode("latin-1").split(",")[0].strip()
            if first:
                return first
        client = scope.get("client")
        if client:
            return str(client[0])
        return "unknown"

    def _consume(self, key: str, now: float) -> tuple[bool, float]:
        """Take one token. Returns ``(allowed, retry_after_seconds)``."""
        self._maybe_sweep(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self._max_tracked:
                # Table is full (likely a spoofed-IP flood). Fail closed for
                # new keys rather than grow without bound.
                retry = 1.0 / self._rps if self._rps > 0 else 60.0
                return False, retry
            bucket = _Bucket(tokens=self._burst, last=now)
            self._buckets[key] = bucket
        else:
            elapsed = now - bucket.last
            if elapsed > 0:
                bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rps)
                bucket.last = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0.0
        deficit = 1.0 - bucket.tokens
        retry = deficit / self._rps if self._rps > 0 else 60.0
        return False, retry

    def _maybe_sweep(self, now: float) -> None:
        """Drop fully-refilled idle buckets every ~30s to bound memory."""
        if now - self._last_sweep < 30.0:
            return
        self._last_sweep = now
        # A bucket idle long enough to be back at full capacity carries no
        # state worth keeping.
        if self._rps > 0:
            idle_cutoff = self._burst / self._rps
        else:
            idle_cutoff = 60.0
        stale = [k for k, b in self._buckets.items() if (now - b.last) > idle_cutoff]
        for k in stale:
            del self._buckets[k]

    async def _send_429(self, send: Send, retry_after: float) -> None:
        retry_secs = max(1, int(retry_after + 0.999))
        body = b'{"detail":"rate limited: too many requests"}'
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry_secs).encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# Re-exported for callers that want the message type without importing starlette.
__all__ = [
    "IpRateLimitMiddleware",
    "ip_rate_limit_enabled",
]
