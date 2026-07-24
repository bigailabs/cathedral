"""Self-heal watchdog for the background weight-refresh loop.

Regression cover for the 2026-06-28 incident where one hung DB call inside the
refresh build froze a publisher replica's served vector for ~68 minutes. The
watchdog wall-clock-bounds each refresh attempt so the loop always makes
progress and a stuck build is abandoned + retried (picking up a healthy
leader's persisted vector next cycle).

These tests exercise the timeout machinery directly with a stubbed
``_refresh_once`` so they need no DB and run in well under a second.
"""
import threading
import time

import pytest

from scaffold.publisher import weights


@pytest.fixture(autouse=True)
def _reset_refresh_state():
    # Isolate module-global watchdog/health state between tests.
    weights._refresh_attempt = None
    with weights._refresh_health_lock:
        weights._refresh_health.update(
            last_ok_ts=0.0, last_status="init", last_error=None,
            last_timeout_ts=0.0, consecutive_failures=0,
        )
    yield


def test_fast_refresh_returns_vector(monkeypatch):
    monkeypatch.setattr(weights, "_refresh_once",
                        lambda store, *, signing_key_hex: {"vector_id": "v1"})
    out = weights._refresh_once_with_timeout(
        None, signing_key_hex="ab", timeout=1.0)
    assert out == {"vector_id": "v1"}


def test_hung_refresh_times_out_quickly(monkeypatch):
    started = time.monotonic()

    def _hang(store, *, signing_key_hex):
        time.sleep(5.0)  # far longer than the timeout below
        return {"vector_id": "late"}

    monkeypatch.setattr(weights, "_refresh_once", _hang)
    with pytest.raises(weights._RefreshTimeout):
        weights._refresh_once_with_timeout(None, signing_key_hex="ab", timeout=0.2)
    # The caller is unblocked ~immediately at the timeout, NOT after 5s.
    assert time.monotonic() - started < 2.0


def test_no_pile_on_while_previous_attempt_alive(monkeypatch):
    release = threading.Event()

    def _hang(store, *, signing_key_hex):
        release.wait(5.0)
        return {"vector_id": "x"}

    monkeypatch.setattr(weights, "_refresh_once", _hang)
    # First attempt times out but its worker thread is still alive (blocked).
    with pytest.raises(weights._RefreshTimeout):
        weights._refresh_once_with_timeout(None, signing_key_hex="ab", timeout=0.2)
    # Second attempt must refuse to spawn a concurrent build.
    with pytest.raises(weights._RefreshTimeout):
        weights._refresh_once_with_timeout(None, signing_key_hex="ab", timeout=0.2)
    release.set()  # let the orphaned worker finish so it doesn't linger


def test_exception_is_reraised(monkeypatch):
    def _boom(store, *, signing_key_hex):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(weights, "_refresh_once", _boom)
    with pytest.raises(RuntimeError, match="db exploded"):
        weights._refresh_once_with_timeout(None, signing_key_hex="ab", timeout=1.0)


def test_cycle_never_raises_and_marks_health(monkeypatch):
    monkeypatch.setattr(weights, "_refresh_once",
                        lambda store, *, signing_key_hex: {"vector_id": "ok"})
    monkeypatch.setattr(weights, "_cache_write", lambda vec: None)
    status = weights._run_refresh_cycle(None, "ab", weights._bg_generation)
    assert status == "ok"
    h = weights.refresh_health()
    assert h["last_status"] == "ok"
    assert h["age_seconds"] is not None and h["age_seconds"] < 5.0
    assert h["consecutive_failures"] == 0


def test_cycle_reports_timeout_without_raising(monkeypatch):
    monkeypatch.setattr(weights, "_REFRESH_TIMEOUT_SECS", 0.2)
    release = threading.Event()
    monkeypatch.setattr(
        weights, "_refresh_once",
        lambda store, *, signing_key_hex: (release.wait(5.0), {"v": 1})[1])
    status = weights._run_refresh_cycle(None, "ab", weights._bg_generation)
    assert status == "timeout"
    h = weights.refresh_health()
    assert h["last_status"] == "timeout"
    assert h["consecutive_failures"] == 1
    assert h["age_seconds"] is None  # never had a success
    release.set()


def test_cycle_reports_error_without_raising(monkeypatch):
    monkeypatch.setattr(weights, "_refresh_once",
                        lambda store, *, signing_key_hex: (_ for _ in ()).throw(
                            ValueError("nope")))
    status = weights._run_refresh_cycle(None, "ab", weights._bg_generation)
    assert status == "error"
    assert weights.refresh_health()["last_status"] == "error"


def test_watchdog_disabled_calls_refresh_directly(monkeypatch):
    monkeypatch.setattr(weights, "_REFRESH_TIMEOUT_SECS", 0.0)
    calls = {"n": 0}

    def _direct(store, *, signing_key_hex):
        calls["n"] += 1
        return {"vector_id": "direct"}

    monkeypatch.setattr(weights, "_refresh_once", _direct)
    monkeypatch.setattr(weights, "_cache_write", lambda vec: None)
    status = weights._run_refresh_cycle(None, "ab", weights._bg_generation)
    assert status == "ok"
    assert calls["n"] == 1
