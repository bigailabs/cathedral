"""Cover the Tier-1 Codex-review follow-ups (2026-06-29):
- _SoftTtlCache abandons a hung builder after max_inflight_secs (no permanent
  refreshing=True freeze).
- weights._try_adopt_persisted recovers freshness on refresh timeout, bounded so
  it can't re-wedge the loop.
"""
import time

from scaffold.publisher.app import _SoftTtlCache
from scaffold.publisher import weights


def test_hung_builder_respawns_after_max_inflight():
    import threading
    gate = threading.Event()
    calls = {"n": 0}

    def hung():
        calls["n"] += 1
        gate.wait(10.0)   # never returns within the test window
        return {"v": 1}

    c = _SoftTtlCache("t", ttl_secs=0.01, retry_backoff_secs=0.0)
    c.max_inflight_secs = 0.2  # shrink for the test

    _v, s = c.get("k", hung, cold_async=True, cold_value={"warming": True})
    assert s == "warming"

    # First build hangs. After max_inflight elapses, further reads must abandon it
    # and spawn a fresh attempt (calls increments) instead of being blocked forever.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and calls["n"] < 2:
        c.get("k", hung, cold_async=True, cold_value={"warming": True})
        time.sleep(0.05)
    assert calls["n"] >= 2
    gate.set()


def test_try_adopt_persisted_caches(monkeypatch):
    monkeypatch.setattr(weights, "_load_persisted_vector",
                        lambda store: {"vector_id": "persisted"})
    captured = {}
    monkeypatch.setattr(weights, "_cache_write", lambda vec: captured.update(vec))
    weights._try_adopt_persisted(None, weights._bg_generation, timeout=1.0)
    assert captured.get("vector_id") == "persisted"


def test_try_adopt_persisted_bounded_on_hang(monkeypatch):
    def hang(store):
        time.sleep(5.0)
        return {"v": 1}

    monkeypatch.setattr(weights, "_load_persisted_vector", hang)
    wrote = {"n": 0}
    monkeypatch.setattr(weights, "_cache_write",
                        lambda vec: wrote.__setitem__("n", wrote["n"] + 1))
    start = time.monotonic()
    weights._try_adopt_persisted(None, weights._bg_generation, timeout=0.2)
    assert time.monotonic() - start < 2.0   # returned at timeout, not after 5s
    assert wrote["n"] == 0                   # nothing cached on a timed-out load
