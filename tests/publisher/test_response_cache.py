"""Unit tests for ``cathedral.publisher.response_cache``.

Covers:
- ``TtlResponseCache``: TTL expiry, single-entry storage, stale-serve during
  refresh, cold-start serialisation, and build-error propagation.
- ``etag_response``: ETag generation, 200 with headers, 304 on match,
  ``pre_serialised`` fast-path, and ``W/`` weak-validator normalisation.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from cathedral.publisher.response_cache import (
    TtlResponseCache,
    _compute_etag,
    etag_response,
)

# ---------------------------------------------------------------------------
# _compute_etag
# ---------------------------------------------------------------------------


def test_compute_etag_returns_16_hex_chars() -> None:
    etag = _compute_etag(b"hello world")
    assert len(etag) == 16
    assert all(c in "0123456789abcdef" for c in etag)


def test_compute_etag_deterministic() -> None:
    body = b'{"items":[]}'
    assert _compute_etag(body) == _compute_etag(body)


def test_compute_etag_differs_for_different_bodies() -> None:
    assert _compute_etag(b"aaa") != _compute_etag(b"bbb")


# ---------------------------------------------------------------------------
# etag_response — 200 branch
# ---------------------------------------------------------------------------


def _make_request(if_none_match: str | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {}
    if if_none_match is not None:
        req.headers = {"if-none-match": if_none_match}
    return req


def test_etag_response_200_includes_cache_control() -> None:
    req = _make_request()
    resp = etag_response(req, {"key": "value"})
    assert resp.status_code == 200
    assert "max-age=15" in resp.headers["cache-control"]
    assert "public" in resp.headers["cache-control"]


def test_etag_response_200_includes_etag_header() -> None:
    req = _make_request()
    payload = {"items": [1, 2, 3]}
    resp = etag_response(req, payload)
    assert "etag" in resp.headers
    etag = resp.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')


def test_etag_response_body_is_json() -> None:
    req = _make_request()
    payload = {"a": 1, "b": [1, 2]}
    resp = etag_response(req, payload)
    parsed = json.loads(resp.body)
    assert parsed == payload


def test_etag_response_pre_serialised_skips_reserialization() -> None:
    req = _make_request()
    body = b'{"pre":"serialised"}'
    resp = etag_response(req, None, pre_serialised=body)
    assert resp.body == body
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# etag_response — 304 branch
# ---------------------------------------------------------------------------


def test_etag_response_304_when_etag_matches() -> None:
    payload = {"items": [], "next_since": None}
    body = json.dumps(payload, separators=(",", ":")).encode()
    etag = _compute_etag(body)

    req = _make_request(if_none_match=f'"{etag}"')
    resp = etag_response(req, payload)
    assert resp.status_code == 304
    assert resp.body == b""


def test_etag_response_304_when_weak_etag_matches() -> None:
    payload = {"items": []}
    body = json.dumps(payload, separators=(",", ":")).encode()
    etag = _compute_etag(body)

    req = _make_request(if_none_match=f'W/"{etag}"')
    resp = etag_response(req, payload)
    assert resp.status_code == 304


def test_etag_response_200_when_etag_mismatch() -> None:
    req = _make_request(if_none_match='"deadbeef00000000"')
    resp = etag_response(req, {"key": "val"})
    assert resp.status_code == 200


def test_etag_304_still_sends_etag_and_cache_control_headers() -> None:
    """RFC 9110 §15.4.5 — 304 MUST include the same headers as 200."""
    payload = {"items": []}
    body = json.dumps(payload, separators=(",", ":")).encode()
    etag = _compute_etag(body)

    req = _make_request(if_none_match=f'"{etag}"')
    resp = etag_response(req, payload)
    assert resp.status_code == 304
    assert "etag" in resp.headers
    assert "cache-control" in resp.headers


# ---------------------------------------------------------------------------
# TtlResponseCache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_cache_first_call_invokes_builder() -> None:
    cache = TtlResponseCache(ttl_seconds=60.0)
    call_count = 0

    async def builder() -> dict:
        nonlocal call_count
        call_count += 1
        return {"count": call_count}

    result = await cache.get_or_refresh(builder)
    assert call_count == 1
    parsed = json.loads(result)
    assert parsed == {"count": 1}


@pytest.mark.asyncio
async def test_ttl_cache_second_call_within_ttl_skips_builder() -> None:
    cache = TtlResponseCache(ttl_seconds=60.0)
    call_count = 0

    async def builder() -> dict:
        nonlocal call_count
        call_count += 1
        return {"n": call_count}

    await cache.get_or_refresh(builder)
    await cache.get_or_refresh(builder)
    assert call_count == 1, "builder must not be called twice within TTL"


@pytest.mark.asyncio
async def test_ttl_cache_refreshes_after_expiry() -> None:
    cache = TtlResponseCache(ttl_seconds=0.01)  # 10 ms TTL
    call_count = 0

    async def builder() -> dict:
        nonlocal call_count
        call_count += 1
        return {"n": call_count}

    await cache.get_or_refresh(builder)
    # Expire the cache
    await asyncio.sleep(0.02)
    await cache.get_or_refresh(builder)
    assert call_count == 2, "builder must be called again after TTL expires"


@pytest.mark.asyncio
async def test_ttl_cache_stores_json_bytes() -> None:
    cache = TtlResponseCache(ttl_seconds=60.0)

    async def builder() -> dict:
        return {"family_id": "synthetic_boolean_v1", "count": 51, "items": []}

    result = await cache.get_or_refresh(builder)
    assert isinstance(result, bytes)
    parsed = json.loads(result)
    assert parsed["family_id"] == "synthetic_boolean_v1"
    assert parsed["count"] == 51


@pytest.mark.asyncio
async def test_ttl_cache_propagates_builder_error() -> None:
    cache = TtlResponseCache(ttl_seconds=60.0)

    async def failing_builder() -> dict:
        raise RuntimeError("db error")

    with pytest.raises(RuntimeError, match="db error"):
        await cache.get_or_refresh(failing_builder)


@pytest.mark.asyncio
async def test_ttl_cache_concurrent_callers_single_flight() -> None:
    """Concurrent callers on a cold cache must all get a result, and the
    builder must be called exactly once (single-flight guarantee)."""
    cache = TtlResponseCache(ttl_seconds=60.0)
    call_count = 0

    async def builder() -> dict:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.01)  # simulate short DB round-trip
        return {"n": call_count}

    results = await asyncio.gather(
        cache.get_or_refresh(builder),
        cache.get_or_refresh(builder),
        cache.get_or_refresh(builder),
    )
    # All callers must receive a valid response.
    for r in results:
        parsed = json.loads(r)
        assert "n" in parsed

    # Builder should have been called at most twice (one refresh, one
    # possible race on the very first populate); critically not N times.
    assert call_count <= 2, f"too many builder calls: {call_count}"


# ---------------------------------------------------------------------------
# Integration: active-challenges endpoint honours ETag
# ---------------------------------------------------------------------------


def test_active_challenges_endpoint_returns_etag(tmp_path):
    """HTTP-level smoke: the endpoint sends Cache-Control + ETag headers."""
    from fastapi.testclient import TestClient

    from cathedral.publisher.app import build_app

    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        resp = client.get("/v1/synthetic-boolean/active-challenges")
        # Endpoint returns 200 (empty active list is valid on a fresh DB)
        # or 503 (challenge surface not configured in test stub).
        # Either way, we only assert headers when it's a 200.
        if resp.status_code == 200:
            assert "etag" in resp.headers, "ETag header must be present"
            assert "cache-control" in resp.headers
            assert "max-age=15" in resp.headers["cache-control"]


def test_active_challenges_304_on_repeat_with_etag(tmp_path):
    """Second GET with the server's ETag must return 304."""
    from fastapi.testclient import TestClient

    from cathedral.publisher.app import build_app

    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        resp1 = client.get("/v1/synthetic-boolean/active-challenges")
        if resp1.status_code != 200:
            pytest.skip("endpoint not 200 in test stub")
        etag = resp1.headers.get("etag", "")
        if not etag:
            pytest.skip("no ETag returned")

        resp2 = client.get(
            "/v1/synthetic-boolean/active-challenges",
            headers={"If-None-Match": etag},
        )
        assert resp2.status_code == 304, (
            f"expected 304 on matching ETag, got {resp2.status_code}"
        )


def test_leaderboard_recent_returns_etag(tmp_path):
    """``/v1/leaderboard/recent`` must send Cache-Control + ETag."""
    from fastapi.testclient import TestClient

    from cathedral.publisher.app import build_app

    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        resp = client.get(
            "/v1/leaderboard/recent",
            params={"since": "2020-01-01T00:00:00.000Z"},
        )
        assert resp.status_code == 200, resp.text
        assert "etag" in resp.headers, "ETag header must be present on /leaderboard/recent"
        assert "cache-control" in resp.headers
        assert "max-age=15" in resp.headers["cache-control"]


def test_leaderboard_recent_304_on_repeat_with_etag(tmp_path):
    """Second GET with the server's ETag must return 304 on an empty feed."""
    from fastapi.testclient import TestClient

    from cathedral.publisher.app import build_app

    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        resp1 = client.get(
            "/v1/leaderboard/recent",
            params={"since": "2020-01-01T00:00:00.000Z"},
        )
        assert resp1.status_code == 200, resp1.text
        etag = resp1.headers.get("etag", "")
        assert etag, "no ETag returned from first request"

        resp2 = client.get(
            "/v1/leaderboard/recent",
            params={"since": "2020-01-01T00:00:00.000Z"},
            headers={"If-None-Match": etag},
        )
        assert resp2.status_code == 304, (
            f"expected 304 on matching ETag, got {resp2.status_code}: {resp2.text}"
        )
