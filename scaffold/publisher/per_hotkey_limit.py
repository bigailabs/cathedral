"""Per-hotkey token-bucket fairness limiter (abuse control, NOT throughput cap).

Track 2 (item 7): the live submit gate is a *global* throughput cap that
instant-429s every miner the moment aggregate concurrency is exceeded — the
original "rate limits are throttling everyone" complaint. The fix is to split
the two concerns:

  * Global saturation gate (existing ``_HotPathBackpressureMiddleware`` /
    ``_submit_slot``) — protects the box from total overload. Reason:
    ``submit_busy_retry``. Admission becomes cheap once async verify lands, so
    this cap can be raised high.

  * Per-hotkey fairness limiter (THIS module) — stops a single miner hotkey
    from starving the others by flooding submits, *without* touching miners who
    are behaving. Reason: ``abuse_rate_limited`` (deliberately distinct from
    ``submit_busy_retry`` so dashboards/miners can tell abuse from saturation).

Design:
  * One token bucket per miner hotkey. Capacity = burst allowance; refill rate =
    sustained submits/sec allowed. A well-behaved miner never sees a 429 from
    this limiter; only a hotkey spending faster than ``refill_per_sec`` for
    longer than its ``burst`` budget is throttled — and only that hotkey.
  * Pure in-process, thread-safe, O(1) per check, lazy refill (no background
    thread). Idle buckets are GC'd to bound memory.

DEFAULT OFF. The limiter only engages when ``CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED``
is truthy. When disabled, ``check()`` returns ``ALLOW`` for every request, so
live behaviour is byte-for-byte unchanged until an operator opts in.

Env flags (read at build time via :func:`config_from_env`):
  * ``CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED``   bool, default False (master switch)
  * ``CATHEDRAL_PER_HOTKEY_BURST``           int,  default 30   (bucket capacity)
  * ``CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC``  float,default 5.0  (sustained rate)
  * ``CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS``int,  default 1    (Retry-After hint)
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

# Distinct rejection reason — MUST NOT collide with the saturation gate's
# "submit_busy_retry" so callers/metrics can separate abuse from saturation.
ABUSE_REASON = "abuse_rate_limited"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class PerHotkeyConfig:
    """Resolved per-hotkey limiter configuration (immutable snapshot)."""

    enabled: bool = False
    burst: int = 30
    refill_per_sec: float = 5.0
    retry_after_secs: int = 1

    @property
    def active(self) -> bool:
        """True only when the limiter should actually gate traffic."""
        return self.enabled and self.burst > 0 and self.refill_per_sec > 0


def config_from_env() -> PerHotkeyConfig:
    """Snapshot the per-hotkey limiter config from the environment.

    Read once at build time (like the other submit knobs) so a single request
    sees a consistent config.
    """
    return PerHotkeyConfig(
        enabled=_env_bool("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", False),
        burst=_env_int("CATHEDRAL_PER_HOTKEY_BURST", 30),
        refill_per_sec=_env_float("CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC", 5.0),
        retry_after_secs=max(1, _env_int("CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS", 1)),
    )


class _Bucket:
    """Token bucket for one hotkey. ``tokens`` are float; refilled lazily."""

    __slots__ = ("tokens", "last_refill", "last_seen")

    def __init__(self, capacity: float, now: float) -> None:
        self.tokens: float = capacity
        self.last_refill: float = now
        self.last_seen: float = now


class PerHotkeyLimiter:
    """Thread-safe per-hotkey token-bucket limiter.

    ``allow(hotkey)`` returns True if the request is permitted (and consumes a
    token), False if this hotkey is over its budget. Other hotkeys are never
    affected by one hotkey's spend.
    """

    # GC bookkeeping: prune buckets idle for a while to bound memory under churn.
    _GC_EVERY_N = 500
    _GC_IDLE_TTL = 600.0  # seconds

    def __init__(self, config: PerHotkeyConfig) -> None:
        self._config = config
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._req_count = 0

    @property
    def config(self) -> PerHotkeyConfig:
        return self._config

    @property
    def active(self) -> bool:
        return self._config.active

    def allow(self, hotkey: str | None, *, now: float | None = None) -> bool:
        """Consume one token for ``hotkey``. Returns True if allowed.

        When the limiter is inactive (flag off / misconfigured) every request is
        allowed and no state is touched — preserving live behaviour exactly.
        A missing/empty hotkey is never throttled here: identity-less abuse is
        the global gate's job, not the per-hotkey fairness limiter's.
        """
        if not self._config.active:
            return True
        if not hotkey:
            return True
        if now is None:
            now = time.monotonic()
        cap = float(self._config.burst)
        rate = self._config.refill_per_sec
        with self._lock:
            bucket = self._buckets.get(hotkey)
            if bucket is None:
                bucket = _Bucket(cap, now)
                self._buckets[hotkey] = bucket
            else:
                elapsed = now - bucket.last_refill
                if elapsed > 0:
                    bucket.tokens = min(cap, bucket.tokens + elapsed * rate)
                    bucket.last_refill = now
            bucket.last_seen = now

            self._req_count += 1
            if self._req_count >= self._GC_EVERY_N:
                self._gc(now)
                self._req_count = 0

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def _gc(self, now: float) -> None:
        """Evict idle buckets (called while holding the lock)."""
        cutoff = now - self._GC_IDLE_TTL
        stale = [k for k, b in self._buckets.items() if b.last_seen < cutoff]
        for k in stale:
            del self._buckets[k]
