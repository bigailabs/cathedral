"""Unit tests for SatGeneratorClient against a mocked httpx transport.

No real network. Every test stands up an httpx.MockTransport that
returns canned responses, exercising the client's parsing + error
paths in isolation.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

from cathedral.publisher.sat_generator_client import (
    LeaseResult,
    SatGeneratorAuthError,
    SatGeneratorClient,
    SatGeneratorConflict,
    SatGeneratorError,
    SatGeneratorHashMismatch,
    SatGeneratorNotFound,
    SatGeneratorOversized,
    SatGeneratorServerError,
)

_BASE = "https://gen.test"
_TOKEN = "test-token-do-not-log"


def _lease_body(*, sha: str = "a" * 64, byte_size: int = 1024) -> dict[str, Any]:
    return {
        "lease_id": "lease_abc",
        "expires_at": "2026-05-27T16:00:00Z",
        "generator_run_id": "gen_abc",
        "cnf_url": f"{_BASE}/v1/artifacts/gen_abc/cnf",
        "cnf_sha256": sha,
        "byte_size": byte_size,
        "num_vars": 100,
        "num_clauses": 350,
        "tier": 1,
        "kind": "sha256_preimage",
        "family": "synthetic_boolean_v1",
        "cnf_class": "structured_crypto",
    }


def _mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Auth / token leakage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_sends_bearer_and_idempotency_key() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization", "")
        captured["idempotency"] = request.headers.get("idempotency-key", "")
        return httpx.Response(201, json=_lease_body())

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        await client.lease(tier=1, kind="sha256_preimage", idempotency_key="k-1")

    assert captured["authorization"] == f"Bearer {_TOKEN}"
    assert captured["idempotency"] == "k-1"


@pytest.mark.asyncio
async def test_lease_auto_generates_idempotency_key_when_omitted() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("idempotency-key", ""))
        return httpx.Response(201, json=_lease_body())

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        await client.lease(tier=1, kind="sha256_preimage")

    assert seen[0]  # non-empty uuid was generated
    assert len(seen[0]) >= 16  # rough sanity


@pytest.mark.asyncio
async def test_caller_cannot_override_authorization_via_extra_headers() -> None:
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get("authorization", ""))
        return httpx.Response(201, json=_lease_body())

    # Drive a request directly through _request to attempt an override.
    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        await client._request(  # type: ignore[attr-defined]
            "POST",
            "/v1/challenges/lease",
            json={"family": "synthetic_boolean_v1", "tier": 1, "kind": "x"},
            extra_headers={"Authorization": "Bearer EVIL"},
        )
    assert captured[0] == f"Bearer {_TOKEN}"


# ---------------------------------------------------------------------------
# Status code → exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "bad token"})

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        with pytest.raises(SatGeneratorAuthError):
            await client.lease(tier=1, kind="sha256_preimage")


@pytest.mark.asyncio
async def test_404_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "missing"})

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        with pytest.raises(SatGeneratorNotFound):
            await client.release("nonexistent-lease")


@pytest.mark.asyncio
async def test_409_on_confirm_raises_conflict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "hash mismatch"})

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        with pytest.raises(SatGeneratorConflict):
            await client.confirm(
                "lease_abc",
                cathedral_challenge_id="cath-1",
                cnf_sha256_witnessed="b" * 64,
            )


@pytest.mark.asyncio
async def test_5xx_raises_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        with pytest.raises(SatGeneratorServerError):
            await client.lease(tier=1, kind="sha256_preimage")


# ---------------------------------------------------------------------------
# fetch_cnf: hash verification + size cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_cnf_verifies_sha256() -> None:
    body = b"p cnf 3 2\n1 2 0\n-2 3 0\n"
    correct_sha = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        lease = LeaseResult.from_json(
            _lease_body(sha=correct_sha, byte_size=len(body))
        )
        result = await client.fetch_cnf(lease)
        assert result == body


@pytest.mark.asyncio
async def test_fetch_cnf_hash_mismatch_raises() -> None:
    body = b"different bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        # Lease advertises a different sha256.
        lease = LeaseResult.from_json(
            _lease_body(sha="a" * 64, byte_size=len(body))
        )
        with pytest.raises(SatGeneratorHashMismatch):
            await client.fetch_cnf(lease)


@pytest.mark.asyncio
async def test_fetch_cnf_oversize_aborts() -> None:
    # 2 KiB body, but cap is 1 KiB
    body = b"x" * 2048

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with SatGeneratorClient(
        base_url=_BASE,
        token=_TOKEN,
        max_cnf_bytes=1024,
        transport=_mock(handler),
    ) as client:
        lease = LeaseResult.from_json(_lease_body(byte_size=len(body)))
        with pytest.raises(SatGeneratorOversized):
            await client.fetch_cnf(lease)


# ---------------------------------------------------------------------------
# pool_health + health basic parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_health_parses_families_and_producer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "families": [
                    {"family": "synthetic_boolean_v1", "tier": 1, "kind": "sha256_preimage", "ready_depth": 5}
                ],
                "producer": {"enabled": True, "running": True},
            },
        )

    async with SatGeneratorClient(
        base_url=_BASE, token=_TOKEN, transport=_mock(handler)
    ) as client:
        ph = await client.pool_health()
    assert len(ph.families) == 1
    assert ph.families[0]["ready_depth"] == 5
    assert ph.producer["running"] is True


# ---------------------------------------------------------------------------
# Log sanitization — token must not appear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_not_in_any_log_message(caplog: pytest.LogCaptureFixture) -> None:
    body = b"p cnf 1 1\n1 0\n"
    sha = hashlib.sha256(body).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/lease"):
            return httpx.Response(201, json=_lease_body(sha=sha, byte_size=len(body)))
        if "/artifacts/" in request.url.path:
            return httpx.Response(200, content=body)
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json={"status": "released"})
        return httpx.Response(200, json={})

    secret_token = "SUPER-SECRET-TOKEN-DO-NOT-LEAK-12345"
    async with SatGeneratorClient(
        base_url=_BASE, token=secret_token, transport=_mock(handler)
    ) as client:
        lease = await client.lease(tier=1, kind="sha256_preimage")
        await client.fetch_cnf(lease)
        await client.release(lease.lease_id)

    for record in caplog.records:
        assert secret_token not in record.getMessage(), (
            f"token leaked in log: {record.getMessage()!r}"
        )
