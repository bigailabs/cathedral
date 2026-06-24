"""Bounded retention for high-volume publisher ledgers.

Default-off. This exists to stop Postgres volume growth after the operator has
grown the volume enough for safe pruning. It keeps scoring semantics intact by
retaining more than the default 24h scoring window.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store


def retention_enabled() -> bool:
    return os.environ.get("CATHEDRAL_RETENTION_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def interval_secs() -> int:
    return max(60, _env_int("CATHEDRAL_RETENTION_INTERVAL_SECS", 3600))


def batch_size() -> int:
    return max(100, min(100_000, _env_int("CATHEDRAL_RETENTION_BATCH_SIZE", 25_000)))


def eval_runs_hours() -> int:
    return max(25, _env_int("CATHEDRAL_RETENTION_EVAL_RUNS_HOURS", 48))


def solve_ledger_hours() -> int:
    return max(25, _env_int("CATHEDRAL_RETENTION_SOLVE_LEDGER_HOURS", 48))


def pm_attempt_hours() -> int:
    return max(25, _env_int("CATHEDRAL_RETENTION_PM_ATTEMPT_HOURS", 48))


def pm_keep_epochs() -> int:
    return max(2, _env_int("CATHEDRAL_RETENTION_PM_KEEP_EPOCHS", 2))


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _ms_iso(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _cutoff(hours: int, *, now: datetime) -> str:
    return _ms_iso(now - timedelta(hours=hours))


def _delete_by_id_batch(
    store: Store,
    table: str,
    id_col: str,
    time_col: str,
    cutoff_iso: str,
    limit: int,
) -> int:
    sql = (
        f"DELETE FROM {table} WHERE {id_col} IN ("
        f"SELECT {id_col} FROM {table} WHERE {time_col} < ? LIMIT ?)"
    )

    def _do(conn):
        cur = conn.execute(sql, (cutoff_iso, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def _delete_lane_solves_batch(store: Store, cutoff_iso: str, limit: int) -> int:
    sql = (
        "DELETE FROM lane_challenge_solves "
        "WHERE (challenge_id, miner_hotkey) IN ("
        "SELECT challenge_id, miner_hotkey FROM lane_challenge_solves "
        "WHERE solved_at_iso < ? LIMIT ?)"
    )

    def _do(conn):
        cur = conn.execute(sql, (cutoff_iso, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def _delete_pm_assignments_batch(store: Store, min_epoch: int, limit: int) -> int:
    sql = (
        "DELETE FROM per_miner_assignments WHERE challenge_id IN ("
        "SELECT challenge_id FROM per_miner_assignments WHERE epoch < ? LIMIT ?)"
    )

    def _do(conn):
        cur = conn.execute(sql, (min_epoch, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def retention_tick(store: Store, *, now: datetime | None = None) -> dict[str, Any]:
    """Run one bounded retention pass and return deletion counts.

    The pass deletes at most one batch per table so it cannot monopolize the DB.
    Operators can run it repeatedly or enable the worker loop.
    """
    now = now or datetime.now(timezone.utc)
    limit = batch_size()
    solve_cutoff = _cutoff(solve_ledger_hours(), now=now)
    result: dict[str, Any] = {
        "batch_size": limit,
        "cutoffs": {
            "eval_runs": _cutoff(eval_runs_hours(), now=now),
            "solve_ledgers": solve_cutoff,
            "pm_attempts": _cutoff(pm_attempt_hours(), now=now),
        },
        "deleted": {},
    }

    result["deleted"]["eval_runs"] = _delete_by_id_batch(
        store, "eval_runs", "id", "ran_at", result["cutoffs"]["eval_runs"], limit)
    result["deleted"]["lane_challenge_solves"] = _delete_lane_solves_batch(
        store, solve_cutoff, limit)
    result["deleted"]["per_miner_attempts"] = _delete_by_id_batch(
        store, "per_miner_attempts", "id", "recorded_at_iso",
        result["cutoffs"]["pm_attempts"], limit)
    result["deleted"]["per_miner_solves"] = _delete_by_id_batch(
        store, "per_miner_solves", "challenge_id", "solved_at_iso",
        solve_cutoff, limit)

    try:
        from . import per_miner as pm

        keep_from_epoch = int(pm.current_epoch()) - pm_keep_epochs() + 1
        result["cutoffs"]["per_miner_assignments_min_epoch"] = keep_from_epoch
        result["deleted"]["per_miner_assignments"] = _delete_pm_assignments_batch(
            store, keep_from_epoch, limit)
    except Exception as exc:
        result["per_miner_assignments_error"] = type(exc).__name__
        result["deleted"]["per_miner_assignments"] = 0

    return result


async def retention_loop(store: Store, log=lambda event, **kw: None):
    import asyncio

    while True:
        if retention_enabled():
            try:
                summary = await asyncio.to_thread(retention_tick, store)
                log("retention_tick", **summary)
            except Exception as exc:
                log("retention_error", error=repr(exc))
        await asyncio.sleep(interval_secs())
