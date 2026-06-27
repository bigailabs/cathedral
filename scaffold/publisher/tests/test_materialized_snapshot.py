"""Tests for the timer-built materialized snapshot layer (Track 3 / item 6).

Covers the MaterializedSnapshot primitive in isolation (serves cached data,
refreshes on the timer, degrades to stale not error, hard staleness ceiling) and
the app-level wiring (flag-off preserves current board/leaderboard behavior
byte-for-byte; flag-on serves the materialized payload with snapshot headers).

In-process app uses sqlite :memory: via TestClient. No network, no chain.
"""

from __future__ import annotations

import json

import pytest

from fastapi.testclient import TestClient

from scaffold.publisher import build_app
from scaffold.publisher import materialized_snapshot as ms
from scaffold.publisher.keys import generate_test_key


# ---------------------------------------------------------------------------
# MaterializedSnapshot primitive
# ---------------------------------------------------------------------------


def test_cold_get_returns_none():
    snap = ms.MaterializedSnapshot("t", lambda: {"v": 1})
    # No build has run yet -> route falls back to live (None).
    assert snap.get() is None


def test_refresh_then_serves_cached_data():
    calls = {"n": 0}

    def builder():
        calls["n"] += 1
        return {"v": calls["n"]}

    snap = ms.MaterializedSnapshot("t", builder)
    assert snap.refresh_once() is True
    served = snap.get()
    assert served is not None
    payload, etag, meta = served
    assert payload == {"v": 1}
    assert etag.startswith('W/"')
    assert meta["snapshot"] == "t"
    assert meta["builds"] == 1
    # get() is pure in-memory: it does NOT trigger another build.
    snap.get()
    assert calls["n"] == 1


def test_refresh_updates_payload_and_etag():
    seq = iter([{"v": 1}, {"v": 2}])
    snap = ms.MaterializedSnapshot("t", lambda: next(seq))
    snap.refresh_once()
    p1, e1, _ = snap.get()
    snap.refresh_once()
    p2, e2, _ = snap.get()
    assert p1 == {"v": 1} and p2 == {"v": 2}
    assert e1 != e2  # ETag tracks the payload bytes


def test_etag_is_hash_of_serialized_payload():
    snap = ms.MaterializedSnapshot("t", lambda: {"b": 2, "a": 1})
    snap.refresh_once()
    _, etag, _ = snap.get()
    body, ser_etag = snap.serialized()
    assert ser_etag == etag
    # Stable serialization: sorted keys, compact separators.
    assert body == json.dumps({"a": 1, "b": 2}, sort_keys=True, separators=(",", ":")).encode()


def test_failed_build_keeps_last_good_snapshot():
    state = {"fail": False}

    def builder():
        if state["fail"]:
            raise RuntimeError("boom")
        return {"v": "good"}

    snap = ms.MaterializedSnapshot("t", builder)
    assert snap.refresh_once() is True
    # Next build fails -> previous good snapshot is kept, error counted.
    state["fail"] = True
    assert snap.refresh_once() is False
    served = snap.get()
    assert served is not None
    payload, _etag, meta = served
    assert payload == {"v": "good"}  # degrade to stale, never to error
    assert meta["build_errors"] == 1
    assert meta["builds"] == 1


def test_hard_staleness_ceiling_returns_none(monkeypatch):
    snap = ms.MaterializedSnapshot("t", lambda: {"v": 1})
    snap.refresh_once()
    assert snap.get() is not None
    # Force the built-at far enough in the past to exceed the ceiling.
    monkeypatch.setattr(ms, "SNAPSHOT_MAX_STALE_SECS", 1.0)
    snap._built_at -= 100.0  # type: ignore[attr-defined]
    # Too stale -> get() returns None so the route falls back to live.
    assert snap.get() is None


def test_zero_ceiling_serves_any_age(monkeypatch):
    snap = ms.MaterializedSnapshot("t", lambda: {"v": 1})
    snap.refresh_once()
    monkeypatch.setattr(ms, "SNAPSHOT_MAX_STALE_SECS", 0.0)
    snap._built_at -= 10_000.0  # type: ignore[attr-defined]
    assert snap.get() is not None  # ceiling disabled -> serve any age


def test_serialized_none_when_cold():
    snap = ms.MaterializedSnapshot("t", lambda: {"v": 1})
    assert snap.serialized() is None


def test_start_is_noop_when_flag_off(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED", raising=False)
    assert ms.enabled() is False
    snap = ms.MaterializedSnapshot("t", lambda: {"v": 1})
    snap.start()  # default-off: no thread, no build
    assert snap._thread is None  # type: ignore[attr-defined]
    assert snap.get() is None


def test_snapshot_headers_carry_swr_and_markers():
    headers = ms.snapshot_headers('W/"abc"', {"snapshot": "board", "built_at": "x", "age_secs": 1.2})
    assert "stale-while-revalidate" in headers["Cache-Control"]
    assert headers["ETag"] == 'W/"abc"'
    assert headers["X-Cathedral-Snapshot"] == "board"
    assert headers["X-Cathedral-Snapshot-Age-Secs"] == "1.2"


# ---------------------------------------------------------------------------
# App wiring: flag-off preserves behavior; flag-on serves the snapshot
# ---------------------------------------------------------------------------


def _make_client(monkeypatch, *, enabled: bool):
    if enabled:
        monkeypatch.setenv("CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED", "1")
    else:
        monkeypatch.delenv("CATHEDRAL_MATERIALIZED_SNAPSHOT_ENABLED", raising=False)
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
    return app


def test_flag_off_board_uses_live_cache_headers(monkeypatch):
    app = _make_client(monkeypatch, enabled=False)
    with TestClient(app) as c:
        resp = c.get("/v1/synthetic-boolean/active-challenges")
    assert resp.status_code == 200
    # Live board_cache path: no snapshot marker header.
    assert resp.headers.get("X-Cathedral-Snapshot") is None
    assert "X-Cathedral-Board-Rebuilds" in resp.headers


def test_flag_off_leaderboard_top_unchanged(monkeypatch):
    app = _make_client(monkeypatch, enabled=False)
    with TestClient(app) as c:
        resp = c.get("/v1/leaderboard/top")
    assert resp.status_code == 200
    assert resp.headers.get("X-Cathedral-Snapshot") is None
    # Live path keeps its original cache header.
    assert resp.headers.get("cache-control") == "public, max-age=30"
    assert "miners" in resp.json()


def test_flag_on_board_serves_snapshot_after_build(monkeypatch):
    app = _make_client(monkeypatch, enabled=True)
    with TestClient(app) as c:
        # The startup timer fires the first build asynchronously; drive it
        # deterministically rather than sleeping.
        app.state.board_snapshot.refresh_once()
        resp = c.get("/v1/synthetic-boolean/active-challenges")
    assert resp.status_code == 200
    assert resp.headers.get("X-Cathedral-Snapshot") == "board"
    assert "stale-while-revalidate" in resp.headers.get("cache-control", "")


def test_flag_on_board_cold_falls_back_to_live(monkeypatch):
    app = _make_client(monkeypatch, enabled=True)
    with TestClient(app) as c:
        # Force the snapshot cold (the background timer may have warmed it on
        # startup) so we deterministically exercise the cold -> live fallback:
        # the route must NOT error and must serve the live board_cache path.
        snap = app.state.board_snapshot
        snap.stop()  # halt the background timer so it cannot re-warm mid-test
        with snap._lock:  # type: ignore[attr-defined]
            snap._payload = None  # type: ignore[attr-defined]
            snap._etag = None  # type: ignore[attr-defined]
        assert snap.get() is None
        resp = c.get("/v1/synthetic-boolean/active-challenges")
        assert resp.status_code == 200
        assert resp.headers.get("X-Cathedral-Snapshot") is None


def test_flag_on_leaderboard_top_serves_snapshot(monkeypatch):
    app = _make_client(monkeypatch, enabled=True)
    with TestClient(app) as c:
        app.state.leaderboard_top_snapshot.refresh_once()
        resp = c.get("/v1/leaderboard/top")
    assert resp.status_code == 200
    assert resp.headers.get("X-Cathedral-Snapshot") == "leaderboard-top"
    assert "miners" in resp.json()


def test_flag_on_leaderboard_top_nondefault_view_uses_live(monkeypatch):
    app = _make_client(monkeypatch, enabled=True)
    with TestClient(app) as c:
        app.state.leaderboard_top_snapshot.refresh_once()
        # Only the default (weights) view is materialized; receipts -> live build.
        resp = c.get("/v1/leaderboard/top?view=receipts")
    assert resp.status_code == 200
    assert resp.headers.get("X-Cathedral-Snapshot") is None
    assert resp.json()["view"] == "receipts"
