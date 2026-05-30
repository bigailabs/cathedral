"""Unit tests for the WS2 submit guard (per-hotkey rate limit + replay dedup)."""

from __future__ import annotations

import pytest

from cathedral.publisher.rate_limit import RateLimitError, SubmitRateGuard


def test_rate_limit_blocks_second_submit_within_interval() -> None:
    g = SubmitRateGuard(min_interval_secs=60.0)
    g.check(hotkey="hkA", signature="sig1", now=1000.0)
    with pytest.raises(RateLimitError) as ei:
        g.check(hotkey="hkA", signature="sig2", now=1030.0)
    assert ei.value.replay is False


def test_rate_limit_allows_after_interval() -> None:
    g = SubmitRateGuard(min_interval_secs=60.0)
    g.check(hotkey="hkA", signature="sig1", now=1000.0)
    g.check(hotkey="hkA", signature="sig2", now=1061.0)  # > 60s later: ok


def test_distinct_hotkeys_are_independent() -> None:
    g = SubmitRateGuard(min_interval_secs=60.0)
    g.check(hotkey="hkA", signature="sigA", now=1000.0)
    g.check(hotkey="hkB", signature="sigB", now=1000.0)  # different hotkey: ok


def test_replay_of_signature_is_rejected_as_replay() -> None:
    g = SubmitRateGuard(min_interval_secs=60.0, replay_window_secs=300.0)
    g.check(hotkey="hkA", signature="dup", now=1000.0)
    # Different hotkey (so the rate limit does not fire), same signature still
    # inside the replay window -> rejected as a replay.
    with pytest.raises(RateLimitError) as ei:
        g.check(hotkey="hkB", signature="dup", now=1100.0)
    assert ei.value.replay is True


def test_signature_replay_evicted_after_window() -> None:
    g = SubmitRateGuard(min_interval_secs=10.0, replay_window_secs=300.0)
    g.check(hotkey="hkA", signature="dup", now=1000.0)
    # Same signature after the replay window has passed: accepted again.
    g.check(hotkey="hkB", signature="dup", now=1000.0 + 301.0)


def test_empty_signature_skips_replay_dedup() -> None:
    g = SubmitRateGuard(min_interval_secs=60.0)
    g.check(hotkey="hkA", signature="", now=1000.0)
    # Empty sig from a different hotkey is not treated as a replay.
    g.check(hotkey="hkB", signature="", now=1000.0)


def test_zero_interval_disables_guard() -> None:
    g = SubmitRateGuard(min_interval_secs=0.0)
    g.check(hotkey="hkA", signature="dup", now=1000.0)
    g.check(hotkey="hkA", signature="dup", now=1000.0)  # no rate limit, no replay block
