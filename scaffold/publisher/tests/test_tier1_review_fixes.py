"""Cover the Tier-1 Codex-review follow-ups (2026-06-29):
- _SoftTtlCache abandons a hung builder after max_inflight_secs (no permanent
  refreshing=True freeze).
- weights._try_adopt_persisted recovers freshness on refresh timeout, bounded so
  it can't re-wedge the loop.
"""
import time

import pytest

from scaffold.publisher.app import _SoftTtlCache
from scaffold.publisher import weights
from scaffold.publisher.store import Store


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


def test_perminer_window_scores_aggregate_in_sql(tmp_path):
    store = Store(str(tmp_path / "publisher.sqlite"))

    def add(challenge_id, hotkey, weight):
        def write(conn):
            conn.execute(
                "INSERT INTO per_miner_solves("
                "challenge_id, miner_hotkey, epoch, tier, seq, difficulty_weight, "
                "verified, solved_at_iso"
                ") VALUES (?, ?, 1, 1, 0, ?, 1, ?)",
                (challenge_id, hotkey, weight, "2026-06-29T00:00:00.000Z"),
            )
        store.write(write)

    add("pm-a", "hk-a", 1.0)
    add("pm-b", "hk-a", 3.0)
    add("pm-c", "hk-b", 2.0)

    scores = weights._perminer_window_scores(
        store, since="2026-06-28T00:00:00.000Z")

    assert scores == {"hk-a": 1.0, "hk-b": 0.5}


def test_perminer_score_window_index_exists(tmp_path):
    store = Store(str(tmp_path / "publisher.sqlite"))

    rows = store.query("PRAGMA index_list('per_miner_solves')")
    names = {str(r["name"]) for r in rows}

    assert "idx_per_miner_solves_verified_time_hotkey" in names
    assert "idx_per_miner_solves_hotkey_verified_time" in names


def test_pm_primary_score_query_failure_does_not_fall_back_to_public(monkeypatch):
    class BrokenStore:
        def query(self, *_args, **_kwargs):
            raise RuntimeError("pm query failed")

    monkeypatch.setenv("CATHEDRAL_PERMINER_ENABLED", "true")
    monkeypatch.setenv(weights.PERMINER_SCORING_MODE_ENV, "pm_primary")

    with pytest.raises(RuntimeError, match="pm query failed"):
        weights._perminer_compose_scores(
            BrokenStore(), since="2026-06-28T00:00:00.000Z")
