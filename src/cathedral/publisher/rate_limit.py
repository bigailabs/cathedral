"""In-memory submit guards: per-hotkey rate limit + signature-replay dedup.

The publisher runs single-instance (see ``sat_autopilot`` concurrency note), so
in-process state is sufficient; a guard instance lives on ``app.state`` so each
app (incl. test apps) gets its own.

Two protections, both cheap and wire-compatible (no signed-payload change):

1. **Per-hotkey rate limit.** A hotkey may submit at most once per
   ``min_interval`` seconds. Blunts the tight-poll / proximity advantage and
   raises the cost of one hotkey sweeping the board. Tunable via
   ``CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS`` (default 60). NOTE: the
   *per-challenge* "one scored solve per operator" guarantee is enforced
   separately by the open-window solves table + the per-operator merit cap;
   this is a coarse anti-spam/anti-proximity limiter.
2. **Signature-replay dedup.** A given ``X-Cathedral-Signature`` is accepted
   at most once within the clock-skew window, closing the ±300s replay hole
   left by the skew tolerance. No nonce-in-payload change is required.
"""

from __future__ import annotations

import os


class RateLimitError(Exception):
    """Raised when a submission is rate-limited or is a signature replay."""

    def __init__(self, message: str, *, replay: bool = False) -> None:
        super().__init__(message)
        self.replay = replay


def _default_min_interval() -> float:
    raw = os.environ.get("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "").strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


class SubmitRateGuard:
    """Per-hotkey min-interval + replayed-signature rejection.

    ``now`` is an epoch float supplied by the caller (server clock), so the
    guard is deterministic and unit-testable.
    """

    def __init__(
        self,
        *,
        min_interval_secs: float | None = None,
        replay_window_secs: float = 300.0,
    ) -> None:
        self.min_interval = (
            _default_min_interval() if min_interval_secs is None else float(min_interval_secs)
        )
        self.replay_window = float(replay_window_secs)
        self._last_seen: dict[str, float] = {}
        self._seen_sigs: dict[str, float] = {}

    def check(
        self,
        *,
        hotkey: str,
        signature: str,
        now: float,
        throttle_key: str | None = None,
    ) -> None:
        """Record this submission or raise ``RateLimitError``.

        Replay is checked before the rate limit so a replayed signature is
        reported as a replay regardless of timing.

        ``throttle_key`` selects the min-interval bucket. It defaults to
        ``hotkey`` — the coarse anti-spam/anti-proximity limiter used for
        registration POSTs. Solve-on-submit POSTs pass a ``(hotkey,
        challenge_id)`` composite so a miner can submit solves for *different*
        active challenges back-to-back (winner-take-all wants speed) while
        still being blocked from spamming the *same* challenge. The
        signature-replay dedup below is ALWAYS global on the signature,
        independent of ``throttle_key``.

        ``min_interval <= 0`` disables the guard entirely (both the rate limit
        and the replay dedup) — used by the test suite, which fires many rapid
        submits per hotkey. Production sets ``CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS``
        so both protections are active.
        """
        if self.min_interval <= 0:
            return
        self._evict(now)

        if signature and signature in self._seen_sigs:
            raise RateLimitError("duplicate signature (replay)", replay=True)

        key = throttle_key or hotkey
        scope = "challenge" if throttle_key else "hotkey"
        last = self._last_seen.get(key)
        if last is not None and (now - last) < self.min_interval:
            wait = self.min_interval - (now - last)
            raise RateLimitError(
                f"rate limited: {self.min_interval:.0f}s between submits per {scope}; "
                f"retry in {wait:.1f}s"
            )

        self._last_seen[key] = now
        if signature:
            self._seen_sigs[signature] = now

    def _evict(self, now: float) -> None:
        sig_cutoff = now - self.replay_window
        if self._seen_sigs:
            self._seen_sigs = {s: t for s, t in self._seen_sigs.items() if t >= sig_cutoff}
        hk_cutoff = now - max(self.min_interval, self.replay_window)
        if self._last_seen:
            self._last_seen = {h: t for h, t in self._last_seen.items() if t >= hk_cutoff}
