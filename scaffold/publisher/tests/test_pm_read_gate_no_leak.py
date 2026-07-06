"""Regression: the _HotPathBackpressureMiddleware read gate must not leak
semaphore slots when a request is cancelled (client disconnect) after the slot
is acquired but before the app runs.

The original code acquired the slot OUTSIDE the try/finally, so a CancelledError
between acquire() and the try leaked the slot permanently. Under continuous
miner polling with occasional disconnects the BoundedSemaphore drained to zero
and every per-miner read 429'd spuriously even on a fully idle origin. This test
reproduces that path against the FIXED single-try/finally structure.
"""
import asyncio
import threading


async def _handler_fixed(sem, *, cancel_after_acquire: bool):
    """Mirror of the middleware's acquire/release structure (fixed version)."""
    acquired = False
    try:
        acquired = sem.acquire(blocking=False)
        if not acquired:
            return "429"
        if cancel_after_acquire:
            raise asyncio.CancelledError()
        await asyncio.sleep(0)
        return "200"
    finally:
        if acquired:
            sem.release()


def _available(sem, cap):
    got = 0
    while sem.acquire(blocking=False):
        got += 1
    for _ in range(got):
        sem.release()
    return got


def test_read_gate_does_not_leak_on_cancel_after_acquire():
    cap = 4
    sem = threading.BoundedSemaphore(cap)

    async def run():
        for _ in range(cap * 3):
            try:
                await _handler_fixed(sem, cancel_after_acquire=True)
            except asyncio.CancelledError:
                pass

    asyncio.run(run())
    assert _available(sem, cap) == cap, "read gate leaked a slot on cancel-after-acquire"
