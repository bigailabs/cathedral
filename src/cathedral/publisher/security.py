"""Publisher abuse controls for miner-facing SAT endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import HTTPException, Request

logger = structlog.get_logger(__name__)


class RequestRateLimitError(Exception):
    """Raised when a request bucket is exhausted."""

    def __init__(self, retry_after_secs: float) -> None:
        super().__init__("request rate limit exceeded")
        self.retry_after_secs = max(0.0, float(retry_after_secs))


class SlidingWindowRateLimiter:
    """Small in-process sliding-window limiter.

    The publisher is deployed as a single process today, so this catches
    accidental floods and low-effort abuse without introducing a Redis
    dependency. Edge/WAF limits should still carry volumetric DDoS load.
    """

    def __init__(self, *, max_requests: int, window_secs: float) -> None:
        self.max_requests = int(max_requests)
        self.window_secs = float(window_secs)
        self._hits: dict[str, deque[float]] = {}

    @property
    def disabled(self) -> bool:
        return self.max_requests <= 0 or self.window_secs <= 0

    def check(self, key: str, *, now: float | None = None) -> None:
        if self.disabled:
            return
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_secs
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.max_requests:
            retry_after = self.window_secs - (current - hits[0])
            raise RequestRateLimitError(retry_after)
        hits.append(current)


def client_ip_from_request(request: Request) -> str:
    """Return a stable client IP key from Railway/edge proxy headers."""

    raw = (
        request.headers.get("cf-connecting-ip")
        or request.headers.get("x-real-ip")
        or _first_forwarded_for(request.headers.get("x-forwarded-for"))
        or (request.client.host if request.client else "")
        or "unknown"
    ).strip()
    return _normalize_ip_key(raw)


def enforce_request_rate_limit(
    request: Request,
    *,
    state_attr: str,
    key: str,
    route: str,
    key_kind: str,
    limit_env: str,
    window_env: str,
    default_limit: int,
    default_window_secs: float = 60.0,
) -> None:
    """Apply an app-state limiter and raise HTTP 429 when exhausted."""

    limiter = _limiter_for_request(
        request,
        state_attr=state_attr,
        limit_env=limit_env,
        window_env=window_env,
        default_limit=default_limit,
        default_window_secs=default_window_secs,
    )
    try:
        limiter.check(key)
    except RequestRateLimitError as exc:
        logger.info(
            "publisher_rate_limited",
            route=route,
            key_kind=key_kind,
            key=_loggable_key(key, key_kind=key_kind),
            client_ip=client_ip_from_request(request),
            retry_after_secs=round(exc.retry_after_secs, 3),
        )
        raise HTTPException(
            status_code=429,
            detail=f"rate limited: retry in {exc.retry_after_secs:.1f}s",
            headers={"Retry-After": str(math.ceil(exc.retry_after_secs))},
        ) from exc


async def require_registered_hotkey(
    request: Request,
    *,
    hotkey: str,
    route: str,
) -> None:
    """Reject hotkeys that are not registered on the configured subnet."""

    if not _env_bool("CATHEDRAL_REQUIRE_SN39_REGISTERED_HOTKEY", default=True):
        return
    gate = _registered_hotkey_gate_for_request(request)
    try:
        registered = await gate.is_registered(hotkey)
    except Exception as exc:
        if _env_bool("CATHEDRAL_REGISTERED_HOTKEY_FAIL_OPEN", default=False):
            logger.warning(
                "registered_hotkey_check_failed_open",
                route=route,
                hotkey=hotkey,
                client_ip=client_ip_from_request(request),
                error=str(exc)[:256],
            )
            return
        logger.warning(
            "registered_hotkey_check_failed_closed",
            route=route,
            hotkey=hotkey,
            client_ip=client_ip_from_request(request),
            error=str(exc)[:256],
        )
        raise HTTPException(
            status_code=503,
            detail="hotkey registration check unavailable",
        ) from exc

    if not registered:
        logger.info(
            "unregistered_hotkey_rejected",
            route=route,
            hotkey=hotkey,
            client_ip=client_ip_from_request(request),
            netuid=gate.netuid,
            network=gate.network,
        )
        raise HTTPException(
            status_code=403,
            detail="hotkey is not registered on the configured subnet",
        )


@dataclass(frozen=True)
class _RegistrationCacheEntry:
    registered: bool
    checked_at: float


class RegisteredHotkeyGate:
    """Cached Bittensor registration checker for miner hotkeys."""

    def __init__(
        self,
        *,
        network: str,
        netuid: int,
        positive_ttl_secs: float,
        negative_ttl_secs: float,
        timeout_secs: float,
    ) -> None:
        self.network = network
        self.netuid = int(netuid)
        self.positive_ttl_secs = float(positive_ttl_secs)
        self.negative_ttl_secs = float(negative_ttl_secs)
        self.timeout_secs = float(timeout_secs)
        self._cache: dict[str, _RegistrationCacheEntry] = {}
        self._lock = asyncio.Lock()
        self._subtensor: Any | None = None

    @property
    def config_key(self) -> tuple[object, ...]:
        return (
            self.network,
            self.netuid,
            self.positive_ttl_secs,
            self.negative_ttl_secs,
            self.timeout_secs,
        )

    async def is_registered(self, hotkey: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(hotkey)
            if cached is not None:
                ttl = self.positive_ttl_secs if cached.registered else self.negative_ttl_secs
                if ttl > 0 and now - cached.checked_at <= ttl:
                    return cached.registered
            registered = await asyncio.wait_for(
                asyncio.to_thread(self._check_registered_sync, hotkey),
                timeout=self.timeout_secs,
            )
            self._cache[hotkey] = _RegistrationCacheEntry(
                registered=registered,
                checked_at=now,
            )
            return registered

    def _check_registered_sync(self, hotkey: str) -> bool:
        if self._subtensor is None:
            import bittensor as bt

            self._subtensor = bt.Subtensor(network=self.network)
        return bool(
            self._subtensor.is_hotkey_registered_on_subnet(
                hotkey_ss58=hotkey,
                netuid=self.netuid,
            )
        )


def _registered_hotkey_gate_for_request(request: Request) -> RegisteredHotkeyGate:
    network = os.environ.get("CATHEDRAL_REGISTERED_HOTKEY_NETWORK", "finney").strip() or "finney"
    netuid = _env_int("CATHEDRAL_REGISTERED_HOTKEY_NETUID", 39)
    positive_ttl = _env_float("CATHEDRAL_REGISTERED_HOTKEY_TTL_SECS", 300.0)
    negative_ttl = _env_float("CATHEDRAL_REGISTERED_HOTKEY_NEGATIVE_TTL_SECS", 60.0)
    timeout = _env_float("CATHEDRAL_REGISTERED_HOTKEY_TIMEOUT_SECS", 5.0)
    desired = (network, netuid, positive_ttl, negative_ttl, timeout)

    existing = getattr(request.app.state, "registered_hotkey_gate", None)
    if existing is not None and not isinstance(existing, RegisteredHotkeyGate):
        return existing
    if isinstance(existing, RegisteredHotkeyGate) and existing.config_key == desired:
        return existing

    gate = RegisteredHotkeyGate(
        network=network,
        netuid=netuid,
        positive_ttl_secs=positive_ttl,
        negative_ttl_secs=negative_ttl,
        timeout_secs=timeout,
    )
    request.app.state.registered_hotkey_gate = gate
    return gate


def _limiter_for_request(
    request: Request,
    *,
    state_attr: str,
    limit_env: str,
    window_env: str,
    default_limit: int,
    default_window_secs: float,
) -> SlidingWindowRateLimiter:
    limit = _env_int(limit_env, default_limit)
    window = _env_float(window_env, default_window_secs)
    existing = getattr(request.app.state, state_attr, None)
    if (
        isinstance(existing, SlidingWindowRateLimiter)
        and existing.max_requests == limit
        and existing.window_secs == window
    ):
        return existing
    limiter = SlidingWindowRateLimiter(max_requests=limit, window_secs=window)
    setattr(request.app.state, state_attr, limiter)
    return limiter


def _first_forwarded_for(value: str | None) -> str:
    if not value:
        return ""
    return value.split(",", 1)[0].strip()


def _normalize_ip_key(value: str) -> str:
    if not value:
        return "unknown"
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value[:128]


def _loggable_key(key: str, *, key_kind: str) -> str:
    if key_kind == "hotkey" and len(key) > 16:
        return f"{key[:8]}...{key[-6:]}"
    return key[:128]


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)
