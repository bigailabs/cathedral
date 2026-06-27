"""Phase 6 retention: selection logic for row pruning and raw-body compaction.

These tests pin *which rows would be retired* and verify that:
  * dry-run never mutates the DB (reports would-retire counts only),
  * accepted raw DIMACS bodies are blanked past the accepted-raw TTL,
  * the hash columns (kept forever) survive compaction,
  * fresh rows / bodies are left alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import retention
from scaffold.publisher.store import Store


NOW = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"))


def _iso(dt: datetime) -> str:
    return retention._ms_iso(dt)


def _add_witness(store: Store, cid: str, recorded_at: datetime, body: str = "p cnf 1 1\n1 0\n") -> None:
    def write(conn):
        conn.execute(
            "INSERT INTO per_miner_witnesses(challenge_id, miner_hotkey, epoch, "
            "tier, seq, dimacs_solution_sha256, answer_hash, dimacs_solution, "
            "recorded_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, "hk", 1, 1, 1, "sha-" + cid, "ans-" + cid, body, _iso(recorded_at)),
        )

    store.write(write)


def _add_eval_run(store: Store, rid: str, ran_at: datetime) -> None:
    def write(conn):
        conn.execute(
            "INSERT INTO eval_runs(id, ran_at, eval_output_schema_version, "
            "miner_hotkey, task_type, row_json) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, _iso(ran_at), 6, "hk", "synthetic_boolean_v1", "{}"),
        )

    store.write(write)


def _witness_body(store: Store, cid: str):
    def read(conn):
        row = conn.execute(
            "SELECT dimacs_solution, dimacs_solution_sha256, answer_hash "
            "FROM per_miner_witnesses WHERE challenge_id = ?",
            (cid,),
        ).fetchone()
        return tuple(row) if row else None

    return store.write(read)


def _eval_count(store: Store) -> int:
    def read(conn):
        return int(conn.execute("SELECT COUNT(*) FROM eval_runs").fetchone()[0])

    return store.write(read)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(retention.os.environ):
        if k.startswith("CATHEDRAL_RETENTION_"):
            monkeypatch.delenv(k, raising=False)
    yield


def test_dry_run_reports_but_does_not_mutate(tmp_path):
    store = _store(tmp_path)
    old = NOW - timedelta(hours=200)            # past 168h accepted-raw TTL
    _add_witness(store, "old", old)
    _add_eval_run(store, "old", NOW - timedelta(hours=100))  # past 48h eval TTL

    res = retention.retention_tick(store, now=NOW, dry=True)

    assert res["dry_run"] is True
    assert res["compacted"]["per_miner_witness_bodies"] == 1
    assert res["deleted"]["eval_runs"] == 1
    # nothing actually changed
    body, sha, ans = _witness_body(store, "old")
    assert body == "p cnf 1 1\n1 0\n"
    assert _eval_count(store) == 1


def test_accepted_body_compacted_past_ttl_keeps_hashes(tmp_path):
    store = _store(tmp_path)
    _add_witness(store, "old", NOW - timedelta(hours=200))

    res = retention.retention_tick(store, now=NOW, dry=False)

    assert res["compacted"]["per_miner_witness_bodies"] == 1
    body, sha, ans = _witness_body(store, "old")
    assert body == ""                 # raw body gone
    assert sha == "sha-old"           # witness hash kept forever
    assert ans == "ans-old"           # answer hash kept forever


def test_fresh_accepted_body_is_retained(tmp_path):
    store = _store(tmp_path)
    _add_witness(store, "fresh", NOW - timedelta(hours=1))

    res = retention.retention_tick(store, now=NOW, dry=False)

    assert res["compacted"]["per_miner_witness_bodies"] == 0
    body, _, _ = _witness_body(store, "fresh")
    assert body == "p cnf 1 1\n1 0\n"


def test_compaction_is_idempotent(tmp_path):
    store = _store(tmp_path)
    _add_witness(store, "old", NOW - timedelta(hours=200))

    first = retention.retention_tick(store, now=NOW, dry=False)
    second = retention.retention_tick(store, now=NOW, dry=False)

    assert first["compacted"]["per_miner_witness_bodies"] == 1
    # already blank -> not selected again
    assert second["compacted"]["per_miner_witness_bodies"] == 0


def test_accepted_raw_hours_env_override(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _add_witness(store, "h30", NOW - timedelta(hours=30))
    monkeypatch.setenv("CATHEDRAL_RETENTION_ACCEPTED_RAW_HOURS", "24")

    # default (168h) would NOT select a 30h-old body
    assert retention.retention_tick(store, now=NOW, dry=True)[
        "compacted"]["per_miner_witness_bodies"] == 1


def test_dry_run_flag_from_env(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _add_witness(store, "old", NOW - timedelta(hours=200))
    monkeypatch.setenv("CATHEDRAL_RETENTION_DRY_RUN", "1")

    res = retention.retention_tick(store, now=NOW)  # dry inferred from env

    assert res["dry_run"] is True
    body, _, _ = _witness_body(store, "old")
    assert body == "p cnf 1 1\n1 0\n"
