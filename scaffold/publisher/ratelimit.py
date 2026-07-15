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

Security note: RateLimitMiddleware keys pre-auth rate limiting by client IP, not
claimed hotkey, because signatures are verified later in endpoint handlers.
AbuseLimitMiddleware has an optional claimed-hotkey bucket, but treats it only
as an abuse hint, never as verified identity or fairness.

Usage (in build_app):
    from .ratelimit import AbuseLimitMiddleware, RateLimitMiddleware
    app.add_middleware(AbuseLimitMiddleware)
    app.add_middleware(RateLimitMiddleware)
"""
from __future__ import annotations

import os
import time
import threading

# Sentinel client-IP when no trustworthy address can be derived (railway mode,
# forwarded chain shorter than the trusted-hop count). Limiters fail OPEN on it
# rather than bucket such requests together (which on Railway would key every
# request on the edge's internal IP and 429 the whole fleet). Not a real IP, so
# it never collides with a client value.
UNRESOLVED_IP = "\x00unresolved"

# Observability: count fail-open events (railway mode couldn't derive a
# trustworthy client IP). In normal operation the edge always appends the
# peer, so this stays 0. A rising value means the edge stopped appending on
# some path and the limiter is silently un-throttling it — surface it via
# metrics so an edge regression is loud, not invisible.
_unresolved_ip_count = 0
_unresolved_ip_lock = threading.Lock()


def _note_unresolved_ip() -> None:
    global _unresolved_ip_count
    with _unresolved_ip_lock:
        _unresolved_ip_count += 1


def unresolved_ip_count() -> int:
    """Total fail-open (un-derivable client IP) events since process start."""
    return _unresolved_ip_count

from starlette.types import ASGIApp, Receive, Scope, Send

# Default: 120 req/min/key.  Set env to 0 to disable.
_DEFAULT_RPM = 120
_WINDOW_SECS = 60

# Paths that are NEVER rate-limited regardless of key.
_EXEMPT_SUFFIXES: tuple[str, ...] = (
    "/health",
    "/v1/validator/weights/next",
    "/v1/admin/validator-health",
    "/v1/admin/synthetic-boolean/submit-metrics",
    "/.well-known/cathedral-jwks.json",
    # Per-miner assignment reads are hot-path signed GETs.  They have their
    # own bounded concurrency gate in app.py; the coarse per-IP RPM limiter
    # unfairly throttles legitimate high-throughput miners behind one egress IP.
    "/v1/synthetic-boolean/per-miner/challenges",
    "/v1/synthetic-boolean/per-miner/cnf",
    # V2 fast-path per-miner reads are the same class of hot signed GET and
    # carry the same pm_read_hard_cap concurrency gate. They were missing here,
    # so on the un-proxied v2 origin the coarse 120rpm global limiter (keyed on
    # a shared upstream IP) throttled the whole fast-path fleet into one bucket.
    "/v2/synthetic-boolean/per-miner/challenges",
    "/v2/synthetic-boolean/per-miner/cnf",
    "/v2/synthetic-boolean/per-miner/cnf-access",
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


# ---------------------------------------------------------------------------
# Cheap pre-auth abuse limiter for the hottest SAT endpoints.
#
# This is deliberately separate from RateLimitMiddleware and the submit
# per-hotkey fairness limiter:
#   * IP and claimed-hotkey are pre-auth hints only; no fairness semantics.
#   * It only protects paths that can get hammered before expensive work.
#   * Default off so rollout is an operator decision.
# ---------------------------------------------------------------------------
IP_ABUSE_REASON = "ip_rate_limited"
ACTOR_ABUSE_REASON = "actor_rate_limited"
_ABUSE_DEFAULT_IP_RPM = 600
_ABUSE_DEFAULT_ACTOR_RPM = 240
_ABUSE_DEFAULT_RETRY_BASE_SECS = 2
_ABUSE_DEFAULT_RETRY_MAX_SECS = 60
_ABUSE_TARGETS: set[tuple[str, str]] = {
    ("POST", "/v1/agents/submit"),
    ("GET", "/v1/synthetic-boolean/per-miner/cnf"),
    ("GET", "/v1/synthetic-boolean/per-miner/challenges"),
    ("GET", "/v2/synthetic-boolean/per-miner/cnf"),
    ("GET", "/v2/synthetic-boolean/per-miner/cnf-access"),
    ("GET", "/v2/synthetic-boolean/per-miner/challenges"),
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or str(default))
    except ValueError:
        return default


def _abuse_enabled() -> bool:
    return _env_bool("CATHEDRAL_ABUSE_LIMIT_ENABLED", False)


def _canonical_path(path: str) -> str:
    if path == _LEGACY_PREFIX:
        return "/"
    if path.startswith(_LEGACY_PREFIX + "/"):
        return path[len(_LEGACY_PREFIX):]
    return path


class _AbuseEntry:
    __slots__ = ("window_start", "count", "limited_count", "last_seen")

    def __init__(self, now: float) -> None:
        self.window_start = now
        self.count = 0
        self.limited_count = 0
        self.last_seen = now

    def tick(self, now: float) -> int:
        if now - self.window_start >= _WINDOW_SECS:
            self.window_start = now
            self.count = 0
            self.limited_count = 0
        self.count += 1
        self.last_seen = now
        return self.count


class _AbuseLimiterState:
    _GC_EVERY_N = 500
    _GC_IDLE_TTL = _WINDOW_SECS * 4

    def __init__(self) -> None:
        self._entries: dict[str, _AbuseEntry] = {}
        self._lock = threading.Lock()
        self._req_count = 0

    def check(
        self,
        key: str,
        limit: int,
        *,
        retry_base: int,
        retry_max: int,
    ) -> tuple[bool, int]:
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _AbuseEntry(now)
                self._entries[key] = entry
            count = entry.tick(now)
            self._req_count += 1
            if self._req_count >= self._GC_EVERY_N:
                self._gc(now)
                self._req_count = 0
            if count <= limit:
                return True, 0
            entry.limited_count += 1
            retry_after = self._retry_after(
                entry.limited_count, retry_base=retry_base, retry_max=retry_max)
            return False, retry_after

    @staticmethod
    def _retry_after(strikes: int, *, retry_base: int, retry_max: int) -> int:
        base = max(1, retry_base)
        cap = max(base, retry_max)
        step = min(max(0, strikes - 1), 10)
        return min(cap, base * (2 ** step))

    def _gc(self, now: float) -> None:
        cutoff = now - self._GC_IDLE_TTL
        stale = [k for k, e in self._entries.items() if e.last_seen < cutoff]
        for k in stale:
            del self._entries[k]


_abuse_state = _AbuseLimiterState()


def _get_header(headers: list, name: bytes) -> str | None:
    """Extract the FIRST matching header value from raw ASGI headers."""
    name_lower = name.lower()
    for k, v in headers:
        if k.lower() == name_lower:
            return v.decode("latin-1", errors="replace")
    return None


def _all_xff_values(headers: list) -> list[str]:
    """Flatten EVERY x-forwarded-for entry across ALL header lines, in order.

    A client can send its own X-Forwarded-For; the trusted edge appends the
    real peer. ASGI keeps repeated headers as separate list entries, and a
    single line may itself hold a comma list. We must not assume the edge's
    value is on the same line as (or a later line than) the client's — so we
    concatenate all of them left-to-right and let the caller index from the
    RIGHT (closest trusted hop). Reading only the first line would return the
    client-controlled value; reading only .split(',')[-1] of one line can too.
    """
    out: list[str] = []
    name = b"x-forwarded-for"
    for k, v in headers:
        if k.lower() == name:
            for part in v.decode("latin-1", errors="replace").split(","):
                part = part.strip()
                if part:
                    out.append(part)
    return out


def _client_ip_mode() -> str:
    """How to derive the client IP. Must match the fronting topology:

    * "headers" (default, legacy): trust cf-connecting-ip, then x-real-ip,
      then the FIRST x-forwarded-for entry. Correct ONLY behind Cloudflare,
      which strips/overwrites these from the outside world.
    * "railway": the service origin is reached directly through Railway's
      edge (DNS-only, no Cloudflare). Every forwarded header is
      client-controlled EXCEPT the trailing entries appended by the trusted
      edge. We index CATHEDRAL_TRUSTED_PROXY_HOPS (default 1) from the RIGHT
      of the fully-flattened x-forwarded-for chain, so a client cannot spoof
      it by sending extra values or extra header lines. Trusting anything
      else lets an attacker rotate fake IPs past the limiter or pin a
      victim's IP into a 429 bucket.
    * "socket": direct exposure, no proxy at all — use the peer address.
    """
    mode = os.environ.get("CATHEDRAL_CLIENT_IP_MODE", "").strip().lower()
    return mode if mode in {"headers", "railway", "socket"} else "headers"


def _trusted_proxy_hops() -> int:
    try:
        return max(1, int(os.environ.get("CATHEDRAL_TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        return 1


def _client_ip_from_scope(scope: Scope) -> str:
    """Client IP per CATHEDRAL_CLIENT_IP_MODE (see _client_ip_mode)."""
    headers = scope.get("headers", [])
    mode = _client_ip_mode()
    if mode == "railway":
        chain = _all_xff_values(headers)
        hops = _trusted_proxy_hops()
        # The rightmost `hops` entries are the trusted edge's; the one at
        # index -hops is the real client as the innermost trusted proxy saw
        # it. A client cannot reach that slot by padding the chain.
        if len(chain) >= hops:
            return chain[-hops]
        # Chain shorter than the trusted-hop count: we CANNOT derive a
        # trustworthy client IP. Do NOT fall back to the socket peer here —
        # on Railway that peer is the edge's own internal address, so every
        # such request would collapse into ONE shared bucket and could 429
        # the whole fleet. Return the UNRESOLVED sentinel; the limiters treat
        # it as fail-open (never throttled) without accumulating per-key
        # state. In normal operation the edge always appends the peer, so
        # this path is not expected to be hit.
        _note_unresolved_ip()
        return UNRESOLVED_IP
    if mode == "socket":
        client = scope.get("client")
        if client:
            return client[0]
        return "unknown"
    cf_ip = _get_header(headers, b"cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()
    real_ip = _get_header(headers, b"x-real-ip")
    if real_ip:
        return real_ip.strip()
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
        if key == UNRESOLVED_IP:
            # Fail open: no trustworthy client IP to bucket on.
            await self.app(scope, receive, send)
            return

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


class AbuseLimitMiddleware:
    """Opt-in pre-auth limiter for high-volume SAT submit/CNF endpoints.

    The actor key is the claimed ``X-Cathedral-Hotkey`` header. That value is
    untrusted until the downstream route verifies the signature, so this limiter
    only treats it as an abuse hint. It must not be used for payout or fairness.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not _abuse_enabled():
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").upper()
        path = _canonical_path(str(scope.get("path") or ""))
        if (method, path) not in _ABUSE_TARGETS:
            await self.app(scope, receive, send)
            return

        retry_base = _env_int(
            "CATHEDRAL_ABUSE_RETRY_BASE_SECS", _ABUSE_DEFAULT_RETRY_BASE_SECS)
        retry_max = _env_int(
            "CATHEDRAL_ABUSE_RETRY_MAX_SECS", _ABUSE_DEFAULT_RETRY_MAX_SECS)

        ip = _client_ip_from_scope(scope)
        if ip == UNRESOLVED_IP:
            # Fail open: no trustworthy client IP to bucket on. The actor key
            # embeds the IP, so it is meaningless here too — pass through.
            await self.app(scope, receive, send)
            return
        ip_limit = _env_int("CATHEDRAL_ABUSE_IP_RPM", _ABUSE_DEFAULT_IP_RPM)
        allowed, retry_after = _abuse_state.check(
            f"ip:{ip}", ip_limit, retry_base=retry_base, retry_max=retry_max)
        if not allowed:
            await self._reject(send, IP_ABUSE_REASON, retry_after)
            return

        actor = (_get_header(scope.get("headers", []), b"x-cathedral-hotkey")
                 or "").strip()
        if actor:
            actor_limit = _env_int(
                "CATHEDRAL_ABUSE_ACTOR_RPM", _ABUSE_DEFAULT_ACTOR_RPM)
            allowed, retry_after = _abuse_state.check(
                f"actor:{ip}:{actor}",
                actor_limit,
                retry_base=retry_base,
                retry_max=retry_max,
            )
            if not allowed:
                await self._reject(send, ACTOR_ABUSE_REASON, retry_after)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, reason: str, retry_after: int) -> None:
        body = reason.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(max(1, retry_after)).encode()),
                (b"x-cathedral-rejection-reason", body),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })
