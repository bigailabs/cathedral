"""Readiness probe hardening (open-v2 window 2026-07-08 14:10-14:24Z).

/health/ready used to run a blocking DB query inline in an async handler:
under real all-miner load one slow probe stalled the event loop and readiness
itself timed out (edge 000/520) while the origin was admitting fine. These
tests pin the new contract: the DB ping runs off-loop, at most one probe is
in flight, results are briefly cached, and failures surface as a clean 503.
"""
from __future__ import annotations

import threading
import time

from starlette.testclient import TestClient

from scaffold.publisher.app import build_app


SIGNING_KEY_HEX = "11" * 32


def _build(tmp_path, monkeypatch, *, ready_cache_secs: str = "2.0",
           ready_timeout_secs: str = "3.0"):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_READY_CACHE_SECS", ready_cache_secs)
    monkeypatch.setenv("CATHEDRAL_READY_TIMEOUT_SECS", ready_timeout_secs)
    return build_app(
        database_path=str(tmp_path / "pub.sqlite"),
        signing_key_hex=SIGNING_KEY_HEX,
    )


def test_ready_ok(tmp_path, monkeypatch):
    app = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


def test_ready_db_error_returns_503_with_error_name(tmp_path, monkeypatch):
    app = _build(tmp_path, monkeypatch, ready_cache_secs="0.5")
    client = TestClient(app)

    def _boom(sql, params=()):
        raise RuntimeError("db down")

    monkeypatch.setattr(app.state.store, "query", _boom)
    time.sleep(0.6)  # let any startup-cached OK state expire
    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["db"] == "error"
    assert body["error"] == "RuntimeError"


def test_ready_result_is_cached_within_ttl(tmp_path, monkeypatch):
    app = _build(tmp_path, monkeypatch, ready_cache_secs="30")
    client = TestClient(app)
    calls = {"n": 0}
    original = app.state.store.query

    def _counting(sql, params=()):
        calls["n"] += 1
        return original(sql, params)

    monkeypatch.setattr(app.state.store, "query", _counting)
    for _ in range(5):
        assert client.get("/health/ready").status_code == 200
    # One refresh probe at most; every other request served from cache.
    assert calls["n"] <= 1


def test_ready_slow_probe_times_out_instead_of_hanging(tmp_path, monkeypatch):
    app = _build(tmp_path, monkeypatch, ready_cache_secs="0.5",
                 ready_timeout_secs="0.5")
    client = TestClient(app)
    release = threading.Event()

    def _slow(sql, params=()):
        release.wait(timeout=10)
        return [{"ok": 1}]

    monkeypatch.setattr(app.state.store, "query", _slow)
    time.sleep(0.6)  # expire the startup-cached OK state
    started = time.monotonic()
    r = client.get("/health/ready")
    elapsed = time.monotonic() - started
    release.set()
    assert r.status_code == 503
    assert r.json()["error"] == "ReadyProbeTimeout"
    # Bounded by the probe timeout, not the hung query.
    assert elapsed < 5


# ---- disk headroom gate (2026-07-09 disk-full incident) ---------------------
# Postgres dies ungracefully at 0 bytes free (WAL PANIC -> crash -> 8.5h
# outage). Readiness must go red while headroom remains so the edge watcher
# auto-aborts an open window BEFORE the DB is damaged.

def test_ready_disk_low_returns_503(tmp_path, monkeypatch):
    # Threshold no filesystem can satisfy -> deterministic DiskLow.
    monkeypatch.setenv("CATHEDRAL_READY_MIN_DISK_FREE_MB", str(10**12))
    app = _build(tmp_path, monkeypatch, ready_cache_secs="0.5")
    client = TestClient(app)
    time.sleep(0.6)  # let the startup-cached OK state expire
    r = client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["db"] == "error"
    assert body["error"].startswith("DiskLow:")


def test_ready_disk_check_disabled_with_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_READY_MIN_DISK_FREE_MB", "0")
    app = _build(tmp_path, monkeypatch, ready_cache_secs="0.5")
    client = TestClient(app)
    time.sleep(0.6)
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["db"] == "ok"


def test_ready_disk_ok_with_sane_threshold(tmp_path, monkeypatch):
    # Default-ish threshold on a dev machine with free space -> 200.
    monkeypatch.setenv("CATHEDRAL_READY_MIN_DISK_FREE_MB", "1")
    app = _build(tmp_path, monkeypatch, ready_cache_secs="0.5")
    client = TestClient(app)
    time.sleep(0.6)
    r = client.get("/health/ready")
    assert r.status_code == 200
