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


async def verify_loop(tick, *, worker_id: str, log=None) -> None:
    """Run `tick(worker_id=..., batch_size=..., lock_secs=...)` forever.

    `tick` is the closure built in app.py (app.state.async_verify_tick). When the
    queue is empty it sleeps poll_secs; when it drained a full batch it loops
    immediately to keep up with bursts."""
    import asyncio

    bs = batch_size()
    ls = lock_secs()
    idle_sleep = poll_secs()
    while True:
        try:
            processed = tick(worker_id=worker_id, batch_size=bs, lock_secs=ls)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one bad row kill the loop
            if log:
                log("verify_tick_error", error=repr(exc))
            processed = 0
        if not processed:
            await asyncio.sleep(idle_sleep)
        else:
            await asyncio.sleep(0)  # yield, then immediately try the next batch
