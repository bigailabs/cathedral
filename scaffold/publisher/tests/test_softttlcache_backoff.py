"""Regression cover for the _SoftTtlCache cold-async failure behavior + the
origin-side weights fail-closed helper (Tier 1 stabilization, 2026-06-29).

Bug fixed: a failed background refresh used to freeze a cold ``warming``
placeholder forever AND re-trigger a build on every request. The cache now backs
off after failures, flips warming->degraded, and preserves last-known-good.
"""
import time

from scaffold.publisher.app import _SoftTtlCache, _weights_vector_expired


def _wait(pred, timeout=2.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def test_cold_async_failure_does_not_warm_forever_and_backs_off():
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise RuntimeError("boom")

    c = _SoftTtlCache("t", ttl_secs=0.01, retry_backoff_secs=5.0)
    v, s = c.get("k", failing, cold_async=True, cold_value={"warming": True})
    assert s == "warming"
    assert v == {"warming": True}

    # Background refresh runs once and fails.
    assert _wait(lambda: calls["n"] >= 1)
    time.sleep(0.05)
    n_after_first = calls["n"]

    # Next reads: failure recorded, inside backoff -> NO new build, status degraded
    # (NOT an eternal "warming"), and the DB is not hammered every request.
    for _ in range(6):
        v2, s2 = c.get("k", failing, cold_async=True, cold_value={"warming": True})
        assert s2 == "degraded"
        assert v2 == {"warming": True}
    assert calls["n"] == n_after_first  # no per-request build storm


def test_failed_refresh_preserves_last_known_good():
    state = {"ok": True, "n": 0}

    def builder():
        state["n"] += 1
        if state["ok"]:
            return {"v": "good", "build": state["n"]}
        raise RuntimeError("now failing")

    c = _SoftTtlCache("t", ttl_secs=0.05, retry_backoff_secs=10.0)
    v, s = c.get("k", builder)          # sync cold build
    assert s == "cold" and v["v"] == "good"
    v, s = c.get("k", builder)          # within ttl
    assert s == "hit"

    time.sleep(0.08)                    # expire ttl
    state["ok"] = False
    v, s = c.get("k", builder)          # healthy->stale: spawns refresh, serves good
    assert s == "stale" and v["v"] == "good"

    assert _wait(lambda: state["n"] >= 2)   # the stale refresh ran and failed
    time.sleep(0.05)
    v, s = c.get("k", builder)          # failure recorded, inside backoff -> degraded
    assert s == "degraded"
    assert v["v"] == "good"             # last-known-good preserved, not dropped


def test_cold_async_recovers_when_build_succeeds():
    ok = {"v": False}

    def builder():
        if not ok["v"]:
            raise RuntimeError("not yet")
        return {"v": "real"}

    c = _SoftTtlCache("t", ttl_secs=0.01, retry_backoff_secs=0.0)
    _v, s = c.get("k", builder, cold_async=True, cold_value={"warming": True})
    assert s == "warming"
    ok["v"] = True
    # With zero backoff, subsequent reads keep retrying; once a build succeeds we
    # promote to a real value and report hit/stale (no longer warming/degraded).
    assert _wait(lambda: c.get("k", builder, cold_async=True,
                               cold_value={"warming": True})[0] == {"v": "real"})


def test_weights_vector_expired_helper():
    now_ms = 1_000_000_000_000.0  # fixed reference
    fresh = {"expires_at": "2099-01-01T00:00:00.000Z"}
    expired = {"expires_at": "2000-01-01T00:00:00.000Z"}
    assert _weights_vector_expired(fresh) is False
    assert _weights_vector_expired(expired) is True
    # Fail-closed on missing / malformed / non-dict.
    assert _weights_vector_expired({}) is True
    assert _weights_vector_expired({"expires_at": ""}) is True
    assert _weights_vector_expired({"expires_at": "not-a-date"}) is True
    assert _weights_vector_expired(None) is True
    # now_epoch_ms override path.
    assert _weights_vector_expired({"expires_at": "1970-01-01T00:00:01.000Z"},
                                   now_epoch_ms=now_ms) is True
