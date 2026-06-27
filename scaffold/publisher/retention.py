"""Bounded retention for high-volume publisher ledgers.

Default-off. This exists to stop Postgres volume growth after the operator has
grown the volume enough for safe pruning. It keeps scoring semantics intact by
retaining more than the default 24h scoring window.

Two kinds of policy run in one tick:

* Row-level pruning (the original behaviour): whole rows in the high-volume
  ledgers are deleted once they age out of the scoring window.
* Body compaction (Phase 6 retention table): the only large *raw* payload the
  DB holds is ``per_miner_witnesses.dimacs_solution`` (the accepted DIMACS
  solution body). Per the plan that raw body is kept 1-7 days for accepted
  solves, while the answer/witness hashes (``dimacs_solution_sha256``,
  ``answer_hash``) are kept forever. Compaction blanks the raw body in place
  and never touches the hash columns or the row, so witnesses stay replayable
  by hash and scoring rows are untouched.

Phase 6 retention table mapping:

  | Data                          | Retention   | Where                        |
  | ----------------------------- | ----------- | ---------------------------- |
  | Pending solution body         | until verified | (no pending body persisted) |
  | Rejected raw solution         | 1-24h       | rejected_raw_hours (no body today) |
  | Accepted raw solution         | 1-7d        | accepted_raw_hours -> witness body |
  | Answer / witness hash         | forever     | never deleted/compacted      |
  | Receipt metadata              | 30-90d      | pm_attempt_hours (hash-only) |
  | Aggregate scoring rows        | forever     | eval_runs feed retained      |

Every destructive step is gated: it only runs when retention is enabled AND
not in dry-run mode. Dry-run reports the row/body counts that *would* be
retired without changing anything.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def retention_enabled() -> bool:
    return _env_bool("CATHEDRAL_RETENTION_ENABLED")


def dry_run() -> bool:
    """When set, the tick reports counts only and deletes/compacts nothing.

    Defaults to OFF so an explicit enable flag is still required to act, but
    operators rolling this out for the first time should set it to 1 and read
    the reported counts before flipping it off.
    """
    return _env_bool("CATHEDRAL_RETENTION_DRY_RUN")


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


def accepted_raw_hours() -> int:
    """How long to keep the raw accepted DIMACS body before compacting it.

    Plan band is 1-7 days; default to the top of the band (168h). Floored at 1h
    so the body always outlives the immediate verification/scoring window.
    """
    return max(1, _env_int("CATHEDRAL_RETENTION_ACCEPTED_RAW_HOURS", 168))


def rejected_raw_hours() -> int:
    """Plan band 1-24h for rejected raw bodies. Reserved: no rejected raw body
    is persisted today (rejected attempts are hash-only), but the policy is kept
    explicit so it is enforced if a raw rejected body is ever stored."""
    return max(1, _env_int("CATHEDRAL_RETENTION_REJECTED_RAW_HOURS", 24))


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


def _count(store: Store, sql: str, params: tuple) -> int:
    def _do(conn):
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return 0
        return int(row[0] or 0)

    # Counting is a read, but Store.write gives a transaction handle uniformly
    # across the SQLite/Postgres backends; a count inside it is side-effect free.
    return int(store.write(_do) or 0)


def _delete_by_id_batch(
    store: Store,
    table: str,
    id_col: str,
    time_col: str,
    cutoff_iso: str,
    limit: int,
    *,
    apply: bool,
) -> int:
    if not apply:
        return _count(
            store,
            f"SELECT COUNT(*) FROM (SELECT {id_col} FROM {table} "
            f"WHERE {time_col} < ? LIMIT ?) sub",
            (cutoff_iso, limit),
        )
    sql = (
        f"DELETE FROM {table} WHERE {id_col} IN ("
        f"SELECT {id_col} FROM {table} WHERE {time_col} < ? LIMIT ?)"
    )

    def _do(conn):
        cur = conn.execute(sql, (cutoff_iso, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def _compact_witness_bodies_batch(
    store: Store, cutoff_iso: str, limit: int, *, apply: bool,
) -> int:
    """Blank the raw accepted DIMACS body once it ages past the accepted-raw TTL.

    Keeps the row and every hash column (dimacs_solution_sha256, answer_hash)
    so accepted witnesses stay replayable by hash forever. Only rows that still
    carry a non-empty body are touched, so repeated ticks converge.
    """
    select = (
        "SELECT challenge_id, miner_hotkey FROM per_miner_witnesses "
        "WHERE recorded_at_iso < ? AND dimacs_solution <> '' LIMIT ?"
    )
    if not apply:
        return _count(
            store, f"SELECT COUNT(*) FROM ({select}) sub", (cutoff_iso, limit))
    sql = (
        "UPDATE per_miner_witnesses SET dimacs_solution = '' "
        "WHERE (challenge_id, miner_hotkey) IN (" + select + ")"
    )

    def _do(conn):
        cur = conn.execute(sql, (cutoff_iso, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def _delete_lane_solves_batch(
    store: Store, cutoff_iso: str, limit: int, *, apply: bool,
) -> int:
    select = (
        "SELECT challenge_id, miner_hotkey FROM lane_challenge_solves "
        "WHERE solved_at_iso < ? LIMIT ?"
    )
    if not apply:
        return _count(
            store, f"SELECT COUNT(*) FROM ({select}) sub", (cutoff_iso, limit))
    sql = (
        "DELETE FROM lane_challenge_solves "
        "WHERE (challenge_id, miner_hotkey) IN (" + select + ")"
    )

    def _do(conn):
        cur = conn.execute(sql, (cutoff_iso, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def _delete_pm_assignments_batch(
    store: Store, min_epoch: int, limit: int, *, apply: bool,
) -> int:
    select = (
        "SELECT challenge_id FROM per_miner_assignments WHERE epoch < ? LIMIT ?"
    )
    if not apply:
        return _count(
            store, f"SELECT COUNT(*) FROM ({select}) sub", (min_epoch, limit))
    sql = (
        "DELETE FROM per_miner_assignments WHERE challenge_id IN (" + select + ")"
    )

    def _do(conn):
        cur = conn.execute(sql, (min_epoch, limit))
        return int(cur.rowcount or 0)

    return int(store.write(_do) or 0)


def retention_tick(
    store: Store, *, now: datetime | None = None, dry: bool | None = None,
) -> dict[str, Any]:
    """Run one bounded retention pass and return deletion/compaction counts.

    The pass touches at most one batch per table so it cannot monopolize the DB.
    Operators can run it repeatedly or enable the worker loop.

    ``dry`` defaults to the ``CATHEDRAL_RETENTION_DRY_RUN`` env flag. When dry,
    the returned counts are what *would* be retired and nothing is mutated.
    """
    now = now or datetime.now(timezone.utc)
    dry = dry_run() if dry is None else dry
    apply = not dry
    limit = batch_size()
    solve_cutoff = _cutoff(solve_ledger_hours(), now=now)
    accepted_raw_cutoff = _cutoff(accepted_raw_hours(), now=now)
    result: dict[str, Any] = {
        "dry_run": dry,
        "batch_size": limit,
        "cutoffs": {
            "eval_runs": _cutoff(eval_runs_hours(), now=now),
            "solve_ledgers": solve_cutoff,
            "pm_attempts": _cutoff(pm_attempt_hours(), now=now),
            "accepted_raw_bodies": accepted_raw_cutoff,
        },
        # "deleted" = rows removed; for dry runs these are would-delete counts.
        "deleted": {},
        # "compacted" = raw bodies blanked in place (hashes preserved).
        "compacted": {},
    }

    result["deleted"]["eval_runs"] = _delete_by_id_batch(
        store, "eval_runs", "id", "ran_at", result["cutoffs"]["eval_runs"],
        limit, apply=apply)
    result["deleted"]["lane_challenge_solves"] = _delete_lane_solves_batch(
        store, solve_cutoff, limit, apply=apply)
    result["deleted"]["per_miner_attempts"] = _delete_by_id_batch(
        store, "per_miner_attempts", "id", "recorded_at_iso",
        result["cutoffs"]["pm_attempts"], limit, apply=apply)
    result["deleted"]["per_miner_solves"] = _delete_by_id_batch(
        store, "per_miner_solves", "challenge_id", "solved_at_iso",
        solve_cutoff, limit, apply=apply)

    result["compacted"]["per_miner_witness_bodies"] = _compact_witness_bodies_batch(
        store, accepted_raw_cutoff, limit, apply=apply)

    try:
        from . import per_miner as pm

        keep_from_epoch = int(pm.current_epoch()) - pm_keep_epochs() + 1
        result["cutoffs"]["per_miner_assignments_min_epoch"] = keep_from_epoch
        result["deleted"]["per_miner_assignments"] = _delete_pm_assignments_batch(
            store, keep_from_epoch, limit, apply=apply)
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
