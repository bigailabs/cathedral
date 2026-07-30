"""Tests for scaffold/publisher/mechanism_cybergym_adapter.py.

Covers the CyberGym-as-mechanism adapter: verified per-miner CyberGym scores
(the level-weighted sum of verified PoC solves) remapped from miner_hotkey to
miner uid via the metagraph_hotkeys snapshot table, per
deploy/MECHANISM_ROUTER_CONTRACT.md. Mirrors test_mechanism_sat_adapter.py.

Also pins the closed-epoch gate shared with cathedral-distill's
``CyberGymScoreStore.require_closed_epoch``: open/incomplete/missing status
must not publish.
"""
from __future__ import annotations

import pytest

from scaffold.publisher import mechanism_cybergym_adapter as adapter, weights
from scaffold.publisher.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"))


def _ensure_tables(store: Store) -> None:
    """Create the tables this adapter reads. In production the CyberGym
    validator writes cybergym_scores + cybergym_epoch_status and the metagraph
    snapshot is populated by the existing publisher path; the test stands both
    up locally."""
    store.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_scores ("
        "miner_hotkey TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "score REAL NOT NULL, PRIMARY KEY (miner_hotkey, epoch))"))
    store.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_epoch_status ("
        "epoch INTEGER PRIMARY KEY, state TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '', "
        "scored_miners INTEGER NOT NULL DEFAULT 0, "
        "marked_at TEXT NOT NULL DEFAULT '')"))


def _score(store: Store, hotkey: str, epoch: int, score: float) -> None:
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_scores(miner_hotkey, epoch, score) "
        "VALUES (?, ?, ?)", (hotkey, epoch, score)))


def _mark_epoch(store: Store, epoch: int, state: str) -> None:
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_epoch_status"
        "(epoch, state, detail, scored_miners, marked_at) "
        "VALUES (?, ?, '', 0, '2026-07-30T00:00:00Z')",
        (epoch, state)))


def _close(store: Store, epoch: int) -> None:
    _mark_epoch(store, epoch, adapter.EPOCH_CLOSED)


def _uid(store: Store, hotkey: str, uid, *, network="finney", netuid=39) -> None:
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO metagraph_hotkeys("
        "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (network, netuid, hotkey, uid, "", 123, "2026-07-01T00:00:00.000Z")))


def _env(monkeypatch) -> None:
    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")


def test_verified_scores_map_to_uid(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=12.0)
    _score(store, "5Bob", epoch=1, score=4.0)
    _close(store, 1)
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)

    vec, meta = adapter.cybergym_mechanism_scores(store, epoch=1)
    assert vec == {10: 12.0, 20: 4.0}
    assert meta.mechanism_id == "cybergym_v0"
    assert meta.source == "cybergym_adapter"
    assert meta.sig_ok is True


def test_unmapped_hotkey_is_dropped_not_zeroed(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=8.0)
    _score(store, "5NoUid", epoch=1, score=99.0)  # no metagraph row
    _close(store, 1)
    _uid(store, "5Alice", 10)

    vec, _ = adapter.cybergym_mechanism_scores(store, epoch=1)
    assert vec == {10: 8.0}  # the unmapped miner's score never lands anywhere


def test_null_uid_is_dropped(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=8.0)
    _close(store, 1)
    _uid(store, "5Alice", None)  # registered but no UID yet
    vec, _ = adapter.cybergym_mechanism_scores(store, epoch=1)
    assert vec == {}


def test_non_positive_scores_ignored(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=0.0)   # solved nothing
    _score(store, "5Bob", epoch=1, score=-1.0)    # defensive: never negative
    _close(store, 1)
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)
    vec, _ = adapter.cybergym_mechanism_scores(store, epoch=1)
    assert vec == {}


def test_no_scores_returns_empty_not_exception(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _close(store, 1)  # closed, but nobody solved
    vec, meta = adapter.cybergym_mechanism_scores(store, epoch=1)
    assert vec == {}
    assert meta.mechanism_id == "cybergym_v0"


def test_latest_closed_epoch_used_when_epoch_unspecified(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=5.0)
    _score(store, "5Alice", epoch=2, score=9.0)  # newer, but still open
    _close(store, 1)
    _mark_epoch(store, 2, "open")
    _uid(store, "5Alice", 10)
    vec, _ = adapter.cybergym_mechanism_scores(store)  # no epoch → latest CLOSED
    assert vec == {10: 5.0}  # must NOT publish the open epoch-2 partial


def test_open_epoch_raises_when_epoch_specified(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=8.0)
    _mark_epoch(store, 1, "open")
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed, match="epoch 1"):
        adapter.cybergym_mechanism_scores(store, epoch=1)


def test_incomplete_epoch_raises_when_epoch_specified(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=8.0)
    _mark_epoch(store, 1, "incomplete")
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed, match="incomplete"):
        adapter.cybergym_mechanism_scores(store, epoch=1)


def test_missing_status_row_raises_when_epoch_specified(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _ensure_tables(store)
    _score(store, "5Alice", epoch=1, score=8.0)
    # no cybergym_epoch_status row for epoch 1
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed, match="no cybergym_epoch_status"):
        adapter.cybergym_mechanism_scores(store, epoch=1)


def test_missing_status_table_contributes_nothing_when_epoch_unspecified(
    tmp_path, monkeypatch,
):
    _env(monkeypatch)
    store = _store(tmp_path)
    # scores table only — no cybergym_epoch_status (pre-gate writer)
    store.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_scores ("
        "miner_hotkey TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "score REAL NOT NULL, PRIMARY KEY (miner_hotkey, epoch))"))
    _score(store, "5Alice", epoch=1, score=8.0)
    _uid(store, "5Alice", 10)
    vec, meta = adapter.cybergym_mechanism_scores(store)  # no epoch
    assert vec == {}
    assert meta.mechanism_id == "cybergym_v0"


def test_missing_status_table_raises_when_epoch_specified(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    store.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_scores ("
        "miner_hotkey TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "score REAL NOT NULL, PRIMARY KEY (miner_hotkey, epoch))"))
    _score(store, "5Alice", epoch=1, score=8.0)
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed):
        adapter.cybergym_mechanism_scores(store, epoch=1)
