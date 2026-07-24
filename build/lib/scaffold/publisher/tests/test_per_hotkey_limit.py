"""Tests for the per-hotkey token-bucket fairness limiter (Track 2 / item 7).

Covers:
  * a single hotkey over its budget is throttled WITHOUT affecting other hotkeys;
  * the abuse rejection reason is the distinct ``abuse_rate_limited`` string and
    NOT the saturation gate's ``submit_busy_retry``;
  * flags-off (default) is a byte-for-byte no-op: every request allowed, no state
    touched, so live gate behaviour is preserved exactly;
  * lazy refill restores a throttled hotkey after enough time passes;
  * a missing/empty hotkey is never throttled here (that is the global gate's job);
  * Retry-After hint config is honoured and floored at 1s.
"""
from __future__ import annotations

import pytest

from scaffold.publisher.per_hotkey_limit import (
    ABUSE_REASON,
    PerHotkeyConfig,
    PerHotkeyLimiter,
    config_from_env,
)


def _active_limiter(burst=3, refill_per_sec=1.0, retry_after_secs=1):
    cfg = PerHotkeyConfig(
        enabled=True,
        burst=burst,
        refill_per_sec=refill_per_sec,
        retry_after_secs=retry_after_secs,
    )
    return PerHotkeyLimiter(cfg)


def test_abuse_reason_is_distinct_from_saturation():
    # The whole point of Track 2: dashboards/miners must be able to tell an abuse
    # rejection apart from a saturation rejection.
    assert ABUSE_REASON == "abuse_rate_limited"
    assert ABUSE_REASON != "submit_busy_retry"


def test_one_hotkey_throttled_without_affecting_others():
    lim = _active_limiter(burst=3, refill_per_sec=0.0001)
    t = 1000.0
    # "alice" burns her whole bucket (capacity 3) at a frozen clock.
    assert lim.allow("alice", now=t) is True
    assert lim.allow("alice", now=t) is True
    assert lim.allow("alice", now=t) is True
    # 4th request: bucket empty -> throttled.
    assert lim.allow("alice", now=t) is False
    assert lim.allow("alice", now=t) is False
    # "bob" is completely unaffected by alice's spend — full burst available.
    assert lim.allow("bob", now=t) is True
    assert lim.allow("bob", now=t) is True
    assert lim.allow("bob", now=t) is True
    assert lim.allow("bob", now=t) is False


def test_lazy_refill_restores_a_throttled_hotkey():
    lim = _active_limiter(burst=2, refill_per_sec=1.0)
    t = 5000.0
    assert lim.allow("alice", now=t) is True
    assert lim.allow("alice", now=t) is True
    assert lim.allow("alice", now=t) is False  # empty
    # 1 second later at 1 token/sec -> exactly one token refilled.
    assert lim.allow("alice", now=t + 1.0) is True
    assert lim.allow("alice", now=t + 1.0) is False
    # Refill is capped at burst: waiting a long time does not over-fill.
    assert lim.allow("alice", now=t + 1000.0) is True
    assert lim.allow("alice", now=t + 1000.0) is True
    assert lim.allow("alice", now=t + 1000.0) is False


def test_flags_off_is_a_noop_allow_everything():
    # Default config => disabled => limiter must never throttle and must not even
    # allocate per-hotkey state (preserving live behaviour exactly).
    lim = PerHotkeyLimiter(PerHotkeyConfig())  # enabled defaults to False
    assert lim.active is False
    t = 0.0
    for _ in range(10_000):
        assert lim.allow("alice", now=t) is True
    # No bucket state was created while inactive.
    assert lim._buckets == {}


def test_active_requires_enabled_and_positive_knobs():
    assert PerHotkeyConfig(enabled=False, burst=10, refill_per_sec=5).active is False
    assert PerHotkeyConfig(enabled=True, burst=0, refill_per_sec=5).active is False
    assert PerHotkeyConfig(enabled=True, burst=10, refill_per_sec=0).active is False
    assert PerHotkeyConfig(enabled=True, burst=10, refill_per_sec=5).active is True


def test_missing_hotkey_is_never_throttled_here():
    lim = _active_limiter(burst=1, refill_per_sec=0.0001)
    t = 0.0
    # Even with a 1-token bucket, identity-less requests are passed through so the
    # global saturation gate (not this limiter) handles anonymous floods.
    for _ in range(100):
        assert lim.allow(None, now=t) is True
        assert lim.allow("", now=t) is True


def test_env_config_default_off(monkeypatch):
    for k in (
        "CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED",
        "CATHEDRAL_PER_HOTKEY_BURST",
        "CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC",
        "CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = config_from_env()
    assert cfg.enabled is False
    assert cfg.active is False


def test_env_config_enabled_and_retry_after_floor(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_BURST", "7")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC", "2.5")
    # Retry-After must never drop below 1s even if misconfigured to 0/negative.
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS", "0")
    cfg = config_from_env()
    assert cfg.enabled is True
    assert cfg.active is True
    assert cfg.burst == 7
    assert cfg.refill_per_sec == 2.5
    assert cfg.retry_after_secs == 1


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_bool_truthy_values(monkeypatch, raw):
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", raw)
    assert config_from_env().enabled is True
