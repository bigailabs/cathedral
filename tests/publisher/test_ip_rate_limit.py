"""Unit tests for the per-IP token-bucket rate-limit middleware.

Drives the ASGI interface directly with an injected monotonic clock so the
token-bucket refill is fully deterministic (no sleeps, no HTTP server).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from cathedral.publisher.ip_rate_limit import (
    IpRateLimitMiddleware,
    ip_rate_limit_enabled,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


async def _inner_app(scope: Any, receive: Any, send: Any) -> None:
    """A trivial downstream app that records that it was reached."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _scope(
    *,
    path: str = "/api/cathedral/v1/synthetic-boolean/active-challenges",
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("203.0.113.7", 51234),
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": client,
    }


async def _drive(mw: IpRateLimitMiddleware, scope: dict[str, Any]) -> dict[str, Any]:
    """Run one request through the middleware; return the response start msg."""
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    await mw(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start


def _mw(clock: _Clock, **kw: Any) -> IpRateLimitMiddleware:
    defaults: dict[str, Any] = {"rps": 1.0, "burst": 3.0, "clock": clock}
    defaults.update(kw)
    return IpRateLimitMiddleware(_inner_app, **defaults)


@pytest.mark.asyncio
async def test_allows_within_burst_then_429s() -> None:
    clock = _Clock()
    mw = _mw(clock)  # burst=3
    for _ in range(3):
        start = await _drive(mw, _scope())
        assert start["status"] == 200
    # 4th request in the same instant exhausts the bucket.
    start = await _drive(mw, _scope())
    assert start["status"] == 429
    hdrs = dict(start["headers"])
    assert b"retry-after" in hdrs


@pytest.mark.asyncio
async def test_refills_over_time() -> None:
    clock = _Clock()
    mw = _mw(clock)  # rps=1, burst=3
    for _ in range(3):
        assert (await _drive(mw, _scope()))["status"] == 200
    assert (await _drive(mw, _scope()))["status"] == 429
    # One token refills after 1s at rps=1.
    clock.advance(1.0)
    assert (await _drive(mw, _scope()))["status"] == 200
    # ...and is spent again immediately.
    assert (await _drive(mw, _scope()))["status"] == 429


@pytest.mark.asyncio
async def test_separate_ips_have_separate_buckets() -> None:
    clock = _Clock()
    mw = _mw(clock)
    a = _scope(client=("198.51.100.1", 1))
    b = _scope(client=("198.51.100.2", 1))
    for _ in range(3):
        assert (await _drive(mw, a))["status"] == 200
    assert (await _drive(mw, a))["status"] == 429
    # Different IP is unaffected.
    assert (await _drive(mw, b))["status"] == 200


@pytest.mark.asyncio
async def test_exempt_path_never_limited() -> None:
    clock = _Clock()
    mw = _mw(clock, exempt_path_prefixes=("/api/cathedral/health",))
    for _ in range(10):
        start = await _drive(mw, _scope(path="/api/cathedral/health"))
        assert start["status"] == 200


@pytest.mark.asyncio
async def test_options_preflight_passes() -> None:
    clock = _Clock()
    mw = _mw(clock)
    for _ in range(10):
        assert (await _drive(mw, _scope(method="OPTIONS")))["status"] == 200


@pytest.mark.asyncio
async def test_allowlist_ip_passes() -> None:
    clock = _Clock()
    mw = _mw(clock, allowlist=("203.0.113.7",))
    for _ in range(10):
        assert (await _drive(mw, _scope()))["status"] == 200


@pytest.mark.asyncio
async def test_trusted_peer_header_preferred_over_xff_and_socket() -> None:
    clock = _Clock()
    # Only the trusted peer (10.0.0.9) is allowlisted; a spoofed XFF claiming
    # the allowlisted IP must NOT bypass the limit.
    mw = _mw(
        clock,
        allowlist=("10.0.0.9",),
        trusted_peer_header="x-envoy-external-address",
    )
    headers = [
        (b"x-envoy-external-address", b"10.0.0.9"),
        (b"x-forwarded-for", b"1.2.3.4"),
    ]
    for _ in range(10):
        assert (await _drive(mw, _scope(headers=headers)))["status"] == 200

    # Now the trusted header is some other IP, XFF spoofs the allowlisted one.
    spoof = [
        (b"x-envoy-external-address", b"8.8.8.8"),
        (b"x-forwarded-for", b"10.0.0.9"),
    ]
    for _ in range(3):
        assert (await _drive(mw, _scope(headers=spoof)))["status"] == 200
    assert (await _drive(mw, _scope(headers=spoof)))["status"] == 429


@pytest.mark.asyncio
async def test_xff_used_when_no_trusted_header() -> None:
    clock = _Clock()
    mw = _mw(clock)
    # Left-most XFF entry is the real client.
    headers = [(b"x-forwarded-for", b"172.16.0.5, 10.0.0.1, 10.0.0.2")]
    s = _scope(headers=headers, client=("10.0.0.2", 1))
    for _ in range(3):
        assert (await _drive(mw, s))["status"] == 200
    assert (await _drive(mw, s))["status"] == 429
    # A different left-most XFF is a different bucket.
    other = _scope(
        headers=[(b"x-forwarded-for", b"172.16.0.6")], client=("10.0.0.2", 1)
    )
    assert (await _drive(mw, other))["status"] == 200


@pytest.mark.asyncio
async def test_full_table_fails_closed_for_new_ips() -> None:
    clock = _Clock()
    mw = _mw(clock, max_tracked=1)
    # First IP claims the only slot.
    assert (await _drive(mw, _scope(client=("1.1.1.1", 1))))["status"] == 200
    # Table is full → a brand-new IP is rejected rather than growing memory.
    assert (await _drive(mw, _scope(client=("2.2.2.2", 1))))["status"] == 429


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    clock = _Clock()
    mw = _mw(clock)
    seen: list[Any] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    mw._app = inner  # type: ignore[attr-defined]
    await mw({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATHEDRAL_IP_RATE_LIMIT_ENABLED", raising=False)
    assert ip_rate_limit_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_enabled_truthy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("CATHEDRAL_IP_RATE_LIMIT_ENABLED", val)
    assert ip_rate_limit_enabled() is True


def test_env_kwargs_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("CATHEDRAL_IP_RATE_LIMIT_"):
            monkeypatch.delenv(k, raising=False)
    from cathedral.publisher.ip_rate_limit import env_kwargs

    kw = env_kwargs()
    assert kw["rps"] == 10.0
    assert kw["burst"] == 120.0
    assert "/api/cathedral/health" in kw["exempt_path_prefixes"]
