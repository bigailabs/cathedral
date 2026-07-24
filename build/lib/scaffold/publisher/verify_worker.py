"""Async SAT verification worker (RELIABILITY_UPGRADE_PLAN Phase 5).

Claims pending submit attempts (received_at order, FOR UPDATE SKIP LOCKED on
Postgres) and runs verification off the request path. Default OFF; the loop only
runs when CATHEDRAL_ASYNC_VERIFY_ENABLED is truthy AND durable admission is on.

The actual per-attempt work lives in submit_admission.finalize_attempt; this
module is only the loop/lifecycle wrapper so the heavy logic stays unit-testable
without a running event loop.
"""
from __future__ import annotations

import os


def async_verify_enabled() -> bool:
    return os.environ.get(
        "CATHEDRAL_ASYNC_VERIFY_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def pm_async_enabled() -> bool:
    """Per-miner (pm-*) durable async admission. Default OFF.

    When OFF the pm-* submit branch keeps its byte-for-byte inline synchronous
    contract. When ON (and the shared async-verify worker flag is also on) the
    pm-* branch persists a pending receipt and the worker verifies it later, the
    same way the public lane already does. SHADOW mode (below) lets the async
    path run in parallel without changing payout, to prove parity before cutover.
    """
    return os.environ.get(
        "CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def pm_async_shadow() -> bool:
    """Shadow de-risk for pm-* async: persist + worker-verify in parallel, but the
    INLINE synchronous result stays authoritative for payout. Any async-vs-inline
    accept/reject divergence is logged so go-live can prove parity first. No payout
    change while shadow is on (the worker writes its terminal status to a shadow
    column, never to the live solve/payout ledger)."""
    return os.environ.get(
        "CATHEDRAL_PM_ASYNC_SHADOW", "").strip().lower() in {"1", "true", "yes", "on"}


def poll_secs() -> float:
    try:
        return max(0.05, float(os.environ.get("CATHEDRAL_ASYNC_VERIFY_POLL_SECS", "0.5")))
    except ValueError:
        return 0.5


def batch_size() -> int:
    try:
        return max(1, int(os.environ.get("CATHEDRAL_ASYNC_VERIFY_BATCH", "8")))
    except ValueError:
        return 8


def lock_secs() -> int:
    try:
        return max(10, int(os.environ.get("CATHEDRAL_ASYNC_VERIFY_LOCK_SECS", "120")))
    except ValueError:
        return 120


async def verify_loop(tick, *, worker_id: str, log=None, heartbeat=None) -> None:
    """Run `tick(worker_id=..., batch_size=..., lock_secs=...)` forever.

    `tick` is the closure built in app.py (app.state.async_verify_tick). When the
    queue is empty it sleeps poll_secs; when it drained a full batch it loops
    immediately to keep up with bursts."""
    import asyncio

    bs = batch_size()
    ls = lock_secs()
    idle_sleep = poll_secs()
    if heartbeat:
        heartbeat("started", processed=0)
    while True:
        try:
            processed = tick(worker_id=worker_id, batch_size=bs, lock_secs=ls)
        except asyncio.CancelledError:
            if heartbeat:
                heartbeat("cancelled", processed=0)
            raise
        except Exception as exc:  # never let one bad row kill the loop
            if log:
                log("verify_tick_error", error=repr(exc))
            if heartbeat:
                heartbeat("error", processed=0, error=repr(exc))
            processed = 0
        else:
            if heartbeat:
                heartbeat("tick", processed=processed)
        if not processed:
            await asyncio.sleep(idle_sleep)
        else:
            await asyncio.sleep(0)  # yield, then immediately try the next batch
