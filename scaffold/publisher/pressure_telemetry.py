"""Low-cardinality pressure attribution for hot SAT endpoints.

Tracks only pressure responses (429 and 5xx) on submit/per-miner read paths.
The snapshot is intended for operator debugging: enough to identify top actors,
without logging request bodies, signatures, full IPs, or full user agents.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import random
import threading
from dataclasses import dataclass
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

_LEGACY_PREFIX = "/api/cathedral"
_MONITORED_PATHS = {
    "/v1/agents/submit",
    "/v1/synthetic-boolean/per-miner/challenges",
    "/v1/synthetic-boolean/per-miner/cnf",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class PressureTelemetryConfig:
    enabled: bool = True
    log_sample_rate: float = 0.0
    max_groups: int = 5000
    top_n: int = 25
    hash_salt: str = ""


def config_from_env() -> PressureTelemetryConfig:
    return PressureTelemetryConfig(
        enabled=_env_bool("CATHEDRAL_PRESSURE_TELEMETRY_ENABLED", True),
        log_sample_rate=max(
            0.0,
            min(1.0, _env_float("CATHEDRAL_PRESSURE_LOG_SAMPLE_RATE", 0.0)),
        ),
        max_groups=max(100, _env_int("CATHEDRAL_PRESSURE_MAX_GROUPS", 5000)),
        top_n=max(1, min(100, _env_int("CATHEDRAL_PRESSURE_TOP_N", 25))),
        hash_salt=os.environ.get(
            "CATHEDRAL_PRESSURE_HASH_SALT",
            os.environ.get("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", ""),
        ),
    )


def mark_verified_hotkey(request: Any, hotkey: str) -> None:
    """Mark a hotkey as verified after the existing signature check succeeds."""
    try:
        request.state.cathedral_verified_hotkey = hotkey
    except Exception:
        return


def _header(scope: Scope, name: bytes) -> str | None:
    needle = name.lower()
    for k, v in scope.get("headers", []):
        if k.lower() == needle:
            return v.decode("latin-1", errors="replace")
    return None


def _response_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    needle = name.lower()
    for k, v in headers:
        if k.lower() == needle:
            return v.decode("latin-1", errors="replace")
    return None


def _canonical_path(path: str) -> str:
    if path == _LEGACY_PREFIX:
        return "/"
    if path.startswith(_LEGACY_PREFIX + "/"):
        return path[len(_LEGACY_PREFIX):]
    return path


def _client_ip(scope: Scope) -> str:
    # Route through the single hardened derivation (CATHEDRAL_CLIENT_IP_MODE)
    # so telemetry buckets are not spoofable on the un-proxied origin either.
    # See ratelimit._client_ip_from_scope and issue #333.
    from . import ratelimit
    ip = ratelimit._client_ip_from_scope(scope)
    if ip in ("unknown", "", ratelimit.UNRESOLVED_IP):
        return ""
    return ip


def _ip_block(ip: str) -> str:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if isinstance(parsed, ipaddress.IPv4Address):
        net = ipaddress.ip_network(f"{parsed}/24", strict=False)
    else:
        net = ipaddress.ip_network(f"{parsed}/48", strict=False)
    return str(net)


def _ua_family(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    token = user_agent.strip().split(" ", 1)[0].split("/", 1)[0]
    token = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in token)
    return (token or "unknown")[:48]


def _digest(value: str | None, *, salt: str = "") -> str | None:
    if not value:
        return None
    h = hashlib.sha256()
    if salt:
        h.update(salt.encode("utf-8"))
        h.update(b"\0")
    h.update(value.encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


def _state_value(scope: Scope, key: str) -> Any:
    state = scope.get("state")
    if isinstance(state, dict):
        return state.get(key)
    return None


class PressureTelemetry:
    def __init__(self, config: PressureTelemetryConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._total = 0
        self._dropped_groups = 0
        self._groups: dict[tuple[Any, ...], dict[str, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def record(
        self,
        *,
        scope: Scope,
        status: int,
        reason: str | None,
    ) -> None:
        if not self._config.enabled:
            return
        if status != 429 and status < 500:
            return
        path = _canonical_path(str(scope.get("path", "")))
        if path not in _MONITORED_PATHS:
            return

        ua = _header(scope, b"user-agent") or ""
        claimed_hotkey = _header(scope, b"x-cathedral-hotkey")
        verified_hotkey = _state_value(scope, "cathedral_verified_hotkey")
        if reason is None:
            if status == 429:
                reason = "rate_limited"
            elif status >= 500:
                reason = "server_error"
            else:
                reason = "unknown"

        event = {
            "path": path,
            "status": status,
            "reason": reason,
            "ip_block": _ip_block(_client_ip(scope)),
            "user_agent_family": _ua_family(ua),
            "user_agent_hash": _digest(ua, salt=self._config.hash_salt),
            "claimed_hotkey_hash": _digest(claimed_hotkey, salt=self._config.hash_salt),
            "verified_hotkey_hash": _digest(verified_hotkey, salt=self._config.hash_salt),
        }
        key = tuple(event.get(k) for k in (
            "path",
            "status",
            "reason",
            "ip_block",
            "user_agent_hash",
            "claimed_hotkey_hash",
            "verified_hotkey_hash",
        ))

        with self._lock:
            self._total += 1
            row = self._groups.get(key)
            if row is None:
                if len(self._groups) >= self._config.max_groups:
                    self._dropped_groups += 1
                    return
                row = dict(event)
                row["count"] = 0
                self._groups[key] = row
            row["count"] = int(row["count"]) + 1

        sample_rate = self._config.log_sample_rate
        if sample_rate > 0 and random.random() < sample_rate:
            print("[pressure] " + json.dumps(event, sort_keys=True))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            top = sorted(
                (dict(v) for v in self._groups.values()),
                key=lambda row: int(row["count"]),
                reverse=True,
            )[:self._config.top_n]
            return {
                "schema": "cathedral.pressure_telemetry.v1",
                "enabled": self._config.enabled,
                "logging_sample_rate": self._config.log_sample_rate,
                "total_pressure_events": self._total,
                "dropped_groups": self._dropped_groups,
                "top": top,
                "privacy": {
                    "ip": "ipv4_/24_or_ipv6_/48",
                    "user_agent": "family_plus_hash",
                    "hotkeys": "hash_only",
                    "request_bodies": "not_recorded",
                },
            }


class PressureTelemetryMiddleware:
    """ASGI pressure telemetry with no response-body buffering."""

    def __init__(self, app: ASGIApp, telemetry: PressureTelemetry) -> None:
        self._app = app
        self._telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._telemetry.enabled:
            await self._app(scope, receive, send)
            return

        status: int | None = None
        reason: str | None = None

        async def _send(message):
            nonlocal status, reason
            if message.get("type") == "http.response.start":
                status = message.get("status")
                headers = message.get("headers") or []
                reason = _response_header(headers, b"x-cathedral-rejection-reason")
            await send(message)

        try:
            await self._app(scope, receive, _send)
        except Exception:
            self._telemetry.record(
                scope=scope,
                status=500,
                reason="unhandled_exception",
            )
            raise
        self._telemetry.record(
            scope=scope,
            status=status if isinstance(status, int) else 500,
            reason=reason,
        )
