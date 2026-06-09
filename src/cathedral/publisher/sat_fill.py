"""Background loop: keep the SAT lane *active* board topped up.

Where ``sat_autopilot`` widens the bench (imports ``pending`` CNFs from
the generator), this loop promotes ``pending -> active`` so a target
number of challenges are live *concurrently* per tier. It is what makes
the board hold ~30-50 open challenges at once instead of one.

Why a dedicated loop: winner-take-all locks a challenge the instant it
is solved, and the in-process rotation in ``submit.py`` (PR #235) only
replaces it one-for-one on a win. Nothing else continuously refills the
board up to a target, so under a flood of fast solves the active count
decays toward the per-win replacement rate. This loop closes that gap by
calling ``promote_to_target`` on a tight interval.

Promotion uses ``active_scope='tier_multi'`` (PR #258), so multiple
concurrent actives per tier are allowed. The per-tier target is the same
``CATHEDRAL_SAT_ACTIVE_PER_TIER`` the ``fill-active-slots`` CLI uses, so
the loop and the CLI agree on the board size.

Run inside the publisher's lifespan via ``asyncio.create_task``;
cancellation is observed through the shared ``stop`` Event.

Concurrency contract: ONE publisher instance per environment (same as
the autopilot). ``promote_to_target`` reads the active count then
promotes the deficit; two publishers could each see the same deficit and
double-promote. The Railway deploy is single-instance, so this stays a
documented constraint rather than a leader lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from cathedral.lanes.challenge_source import SqliteChallengeSource
from cathedral.publisher.sat_generator_import import SAT_FAMILY_ID

logger = structlog.get_logger(__name__)


_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_TIERS = "1,2,3"
# Open-window retirement thresholds. Env-tunable so turnover can be balanced
# against import throughput without a redeploy (feeding up to ~8k challenges/day
# needs ~9-min average challenge lifetime against a ~50-slot board).
_DEFAULT_RETIRE_AFTER_SECONDS = 60 * 60
_DEFAULT_RETIRE_AFTER_DISTINCT_SOLVERS = 64
# Grace before a retired challenge's CNF file is unlinked. Retired rows are no
# longer servable (the /cnf endpoint serves only active or locked-in-grace), so
# this only needs to outlast an in-flight fetch. DB-driven GC over status=
# 'retired' rows never touches in-flight imports (those are status='pending').
_DEFAULT_CNF_GC_GRACE_SECONDS = 5 * 60
_DEFAULT_CNF_GC_LIMIT = 500


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def retire_after_seconds() -> int:
    return _env_int(
        "CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_SECONDS", _DEFAULT_RETIRE_AFTER_SECONDS
    )


def retire_after_distinct_solvers() -> int:
    return _env_int(
        "CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS",
        _DEFAULT_RETIRE_AFTER_DISTINCT_SOLVERS,
    )


def cnf_gc_grace_seconds() -> int:
    return _env_int(
        "CATHEDRAL_OPEN_WINDOW_CNF_GC_GRACE_SECONDS", _DEFAULT_CNF_GC_GRACE_SECONDS
    )


def _now_iso() -> str:
    """Millisecond-precision UTC timestamp, matching the CLI's format."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}" + "Z"


def _iso_seconds_before(now_iso: str, seconds: int) -> str:
    """Return canonical UTC ISO milliseconds for ``now_iso - seconds``."""
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    cutoff = now - timedelta(seconds=int(seconds))
    return (
        cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{cutoff.microsecond // 1000:03d}"
        + "Z"
    )


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FillConfig:
    """Resolved active-board fill configuration. All knobs come from env."""

    interval_seconds: int = _DEFAULT_INTERVAL_SECONDS
    tiers: tuple[int, ...] = (1, 2, 3)
    default_target: int = 1
    # Overrides keyed by tier (as int) -> target active depth.
    target_overrides: dict[int, int] = None  # type: ignore[assignment]
    # Optional: restrict promotion to rows whose audit_metadata.kind matches.
    kind: str | None = None

    def target_for(self, tier: int) -> int:
        if self.target_overrides and int(tier) in self.target_overrides:
            return int(self.target_overrides[int(tier)])
        return self.default_target


def fill_enabled() -> bool:
    """True iff CATHEDRAL_SAT_FILL_ENABLED is set to a truthy value."""
    raw = os.environ.get("CATHEDRAL_SAT_FILL_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def open_window_retirement_enabled() -> bool:
    """True iff active open-window SAT challenges should age/solver retire."""
    raw = os.environ.get("CATHEDRAL_OPEN_WINDOW_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _active_per_tier_default() -> int:
    """Read CATHEDRAL_SAT_ACTIVE_PER_TIER (shared with the CLI; default 1)."""
    raw = os.environ.get("CATHEDRAL_SAT_ACTIVE_PER_TIER", "")
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return 1


def _parse_target_overrides() -> dict[int, int]:
    """Parse CATHEDRAL_SAT_FILL_TARGETS (JSON {"<tier>": N}) into {tier: N}."""
    overrides_raw = os.environ.get("CATHEDRAL_SAT_FILL_TARGETS", "").strip()
    overrides: dict[int, int] = {}
    if not overrides_raw:
        return overrides
    try:
        parsed = json.loads(overrides_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"CATHEDRAL_SAT_FILL_TARGETS is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            'CATHEDRAL_SAT_FILL_TARGETS must be a JSON object of "<tier>" -> int'
        )
    for k, v in parsed.items():
        key = str(k)
        if not key.isdigit():
            raise ValueError(
                f"CATHEDRAL_SAT_FILL_TARGETS key {key!r} must be an integer tier"
            )
        value = int(v)
        if value < 0:
            raise ValueError(
                f"CATHEDRAL_SAT_FILL_TARGETS[{key!r}] must be >= 0 (got {value})"
            )
        overrides[int(key)] = value
    return overrides


def fill_kind() -> str | None:
    """Read CATHEDRAL_SAT_FILL_KIND (optional kind filter)."""
    return os.environ.get("CATHEDRAL_SAT_FILL_KIND", "").strip() or None


def target_for_tier(tier: int) -> int:
    """Resolve the active-board target for ``tier`` from env.

    Single source of truth shared by the polling loop and the win-site
    refill in submit.py, so instant refill and the loop converge to the
    SAME N (and the same per-tier override) instead of diverging.
    """
    overrides = _parse_target_overrides()
    if int(tier) in overrides:
        return overrides[int(tier)]
    return _active_per_tier_default()


def config_from_env() -> FillConfig:
    """Build a FillConfig from environment variables.

    Recognised env vars:
      - CATHEDRAL_SAT_FILL_ENABLED          (read via ``fill_enabled()``)
      - CATHEDRAL_SAT_FILL_INTERVAL_SECONDS default 60 (min 10)
      - CATHEDRAL_SAT_FILL_TIERS            comma-separated ints, default "1,2,3"
      - CATHEDRAL_SAT_ACTIVE_PER_TIER       default per-tier target (default 1)
      - CATHEDRAL_SAT_FILL_TARGETS          optional JSON object {"<tier>": N}
      - CATHEDRAL_SAT_FILL_KIND             optional kind filter
    """
    interval = int(
        os.environ.get(
            "CATHEDRAL_SAT_FILL_INTERVAL_SECONDS", str(_DEFAULT_INTERVAL_SECONDS)
        )
    )
    if interval < 10:
        raise ValueError(
            f"CATHEDRAL_SAT_FILL_INTERVAL_SECONDS must be >= 10 (got {interval})"
        )

    tiers_raw = os.environ.get("CATHEDRAL_SAT_FILL_TIERS", _DEFAULT_TIERS).strip()
    try:
        tiers = tuple(int(t.strip()) for t in tiers_raw.split(",") if t.strip())
    except ValueError as exc:
        raise ValueError(
            "CATHEDRAL_SAT_FILL_TIERS must be a comma-separated list of integers"
        ) from exc
    if not tiers:
        raise ValueError("CATHEDRAL_SAT_FILL_TIERS resolved to no tiers")

    return FillConfig(
        interval_seconds=interval,
        tiers=tiers,
        default_target=_active_per_tier_default(),
        target_overrides=_parse_target_overrides(),
        kind=fill_kind(),
    )


# ---------------------------------------------------------------------------
# Loop body
# ---------------------------------------------------------------------------


async def _retire_open_window_ready(
    *,
    source: SqliteChallengeSource,
    tier: int,
    now_iso: str,
) -> int:
    """Retire active open-window challenges that are old enough or saturated."""
    retired = 0
    cutoff_iso = _iso_seconds_before(now_iso, retire_after_seconds())
    cur = await source._conn.execute(
        """
        UPDATE lane_challenges
        SET status = 'retired', updated_at_iso = ?
        WHERE family_id = ?
          AND status = 'active'
          AND tier = ?
          AND updated_at_iso <= ?
        """,
        (now_iso, SAT_FAMILY_ID, int(tier), cutoff_iso),
    )
    retired += int(cur.rowcount or 0)

    try:
        cur = await source._conn.execute(
            """
            UPDATE lane_challenges
            SET status = 'retired', updated_at_iso = ?
            WHERE family_id = ?
              AND status = 'active'
              AND tier = ?
              AND challenge_id IN (
                  SELECT challenge_id
                  FROM lane_challenge_solves
                  WHERE family_id = ?
                  GROUP BY challenge_id
                  HAVING COUNT(DISTINCT miner_hotkey) >= ?
              )
            """,
            (
                now_iso,
                SAT_FAMILY_ID,
                int(tier),
                SAT_FAMILY_ID,
                retire_after_distinct_solvers(),
            ),
        )
        retired += int(cur.rowcount or 0)
    except sqlite3.OperationalError as exc:
        if "lane_challenge_solves" not in str(exc):
            raise
    await source._conn.commit()
    return retired


async def gc_retired_cnf_files(
    *,
    source: SqliteChallengeSource,
    now_iso: str,
    grace_seconds: int,
    limit: int = _DEFAULT_CNF_GC_LIMIT,
) -> int:
    """Unlink CNF files for retired challenges, bounding disk under high churn.

    DB-driven on purpose (never a filesystem scan): it only targets rows whose
    status is ``retired`` and whose ``updated_at_iso`` is older than the grace
    window. In-flight imports write their ``.cnf`` BEFORE committing the row and
    are status ``pending``/uncommitted, so they can never be selected here — this
    closes the orphan-GC-deletes-an-in-flight-import race. After unlinking, the
    row's ``cnf_path`` is cleared so it is not reprocessed; the row itself is
    retained for history. Must run under the publisher ``db_write_lock``.
    """
    cutoff_iso = _iso_seconds_before(now_iso, grace_seconds)
    cur = await source._conn.execute(
        """
        SELECT challenge_id, cnf_path
        FROM lane_challenges
        WHERE family_id = ?
          AND status = 'retired'
          AND cnf_path != ''
          AND updated_at_iso <= ?
        ORDER BY updated_at_iso ASC
        LIMIT ?
        """,
        (SAT_FAMILY_ID, cutoff_iso, int(limit)),
    )
    rows = await cur.fetchall()
    removed = 0
    for challenge_id, cnf_path in rows:
        if cnf_path:
            try:
                os.unlink(cnf_path)
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                logger.warning(
                    "sat_fill_cnf_unlink_failed",
                    challenge_id=challenge_id,
                    error=str(exc),
                )
                continue
        await source._conn.execute(
            "UPDATE lane_challenges SET cnf_path = '' WHERE challenge_id = ?",
            (challenge_id,),
        )
        removed += 1
    if removed:
        await source._conn.commit()
    return removed


async def run_one_tick(
    *,
    source: SqliteChallengeSource,
    config: FillConfig,
    db_write_lock: AbstractAsyncContextManager,
) -> dict[str, int]:
    """Promote pending->active per tier up to the configured target.

    Returns a small summary dict. A failure on one tier is logged and does
    NOT propagate, so a transient DB hiccup on one tier never kills the loop.

    Each tier's count-and-promote runs under ``db_write_lock`` — the shared
    publisher write gate. This is REQUIRED for correctness: the challenge
    source shares one aiosqlite connection with the winner-write path
    (atomic_claim_winner) and uses ``BEGIN IMMEDIATE`` internally, so an
    unguarded promote here would interleave with a concurrent winner write
    on the same connection. Holding the lock per tier also makes the
    read-count-then-promote atomic, so this loop and the win-site refill
    can't both observe the same deficit and overshoot the target.
    """
    summary = {"tiers_filled": 0, "promoted": 0, "retired": 0, "errors": 0}
    now_iso = _now_iso()
    for tier in config.tiers:
        target = config.target_for(tier)
        try:
            async with db_write_lock:
                retired = 0
                if open_window_retirement_enabled():
                    retired = await _retire_open_window_ready(
                        source=source,
                        tier=tier,
                        now_iso=now_iso,
                    )
                promoted = await source.promote_to_target(
                    SAT_FAMILY_ID,
                    tier=tier,
                    target=target,
                    now_iso=now_iso,
                    kind=config.kind,
                )
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception(
                "sat_fill_tier_failed", tier=tier, target=target, error=str(exc)
            )
            summary["errors"] += 1
            continue
        if retired:
            summary["retired"] += retired
            logger.info(
                "sat_fill_retired_open_window",
                tier=tier,
                retired_count=retired,
                retire_after_seconds=retire_after_seconds(),
                retire_after_distinct_solvers=retire_after_distinct_solvers(),
            )
        if promoted:
            summary["tiers_filled"] += 1
            summary["promoted"] += len(promoted)
            logger.info(
                "sat_fill_promoted",
                tier=tier,
                target=target,
                promoted_count=len(promoted),
            )

    # Disk GC for retired file-backed CNFs (once per tick, not per tier). Only
    # runs when open-window retirement is on, since that is what produces the
    # 'retired' rows this reclaims.
    if open_window_retirement_enabled():
        try:
            async with db_write_lock:
                gc_removed = await gc_retired_cnf_files(
                    source=source,
                    now_iso=now_iso,
                    grace_seconds=cnf_gc_grace_seconds(),
                )
        except Exception as exc:  # pragma: no cover - safety net
            logger.exception("sat_fill_cnf_gc_failed", error=str(exc))
        else:
            if gc_removed:
                summary["cnf_files_gc"] = gc_removed
                logger.info("sat_fill_cnf_gc", removed_count=gc_removed)
    return summary


async def run_fill_loop(
    *,
    source: SqliteChallengeSource,
    config: FillConfig,
    stop: asyncio.Event,
    db_write_lock: AbstractAsyncContextManager,
) -> None:
    """Cooperative loop. Exits cleanly when ``stop`` is set.

    Sleeps with ``wait_for(stop.wait(), timeout=interval)`` so a shutdown
    signal interrupts the sleep instead of waiting up to a full interval.

    ``db_write_lock`` is the shared publisher write gate (see run_one_tick).
    """
    logger.info(
        "sat_fill_loop_started",
        interval_seconds=config.interval_seconds,
        tiers=list(config.tiers),
        default_target=config.default_target,
    )
    while not stop.is_set():
        try:
            await run_one_tick(
                source=source, config=config, db_write_lock=db_write_lock
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - safety net
            logger.exception("sat_fill_tick_crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.interval_seconds)
        except TimeoutError:
            continue
    logger.info("sat_fill_loop_stopped")
