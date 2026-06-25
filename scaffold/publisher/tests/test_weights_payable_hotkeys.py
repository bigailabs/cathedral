from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from scaffold.publisher import weights
from scaffold.publisher.store import Store


SIGNING_KEY_HEX = "11" * 32


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"))


def _add_eval_run(store: Store, hotkey: str, ran_at: str) -> None:
    def write(conn):
        conn.execute(
            "INSERT INTO eval_runs("
            "id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                ran_at,
                6,
                hotkey,
                "synthetic_boolean_v1",
                json.dumps({"weighted_score": 1.0}),
            ),
        )

    store.write(write)


def _add_metagraph_hotkey(
    store: Store,
    hotkey: str,
    updated_at: str,
    *,
    uid: int = 1,
) -> None:
    def write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("finney", 39, hotkey, uid, "", 123, updated_at),
        )

    store.write(write)


def _common_env(monkeypatch, mode: str) -> None:
    monkeypatch.setenv(weights.MODE_ENV, "flat_recent")
    monkeypatch.setenv(weights.NETWORK_ENV, "finney")
    monkeypatch.setenv(weights.NETUID_ENV, "39")
    monkeypatch.setenv(weights.PAYABLE_HOTKEYS_ENV, mode)
    monkeypatch.setenv(weights.PAYABLE_HOTKEYS_MAX_AGE_SECS_ENV, "600")
    monkeypatch.setenv(weights.PERMINER_BONUS_MULT_ENV, "0")


def _build(store: Store, now: datetime) -> dict:
    return weights.build_signed_vector(store, signing_key_hex=SIGNING_KEY_HEX, now=now)


def test_filter_mode_drops_hotkeys_missing_from_fresh_metagraph(tmp_path, monkeypatch):
    _common_env(monkeypatch, "filter")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = _store(tmp_path)
    ran_at = weights._ms_iso(now - timedelta(minutes=1))
    _add_eval_run(store, "hk-payable", ran_at)
    _add_eval_run(store, "hk-missing", ran_at)
    _add_metagraph_hotkey(store, "hk-payable", weights._ms_iso(now), uid=7)

    payload = _build(store, now)

    assert payload["weights"] == [{"miner_hotkey": "hk-payable", "weight": 1.0}]
    meta = payload["policy_metadata"]["payable_hotkeys"]
    assert meta["mode"] == "filter"
    assert meta["enforced"] is True
    assert meta["snapshot_fresh"] is True
    assert meta["missing_hotkeys"] == ["hk-missing"]
    assert meta["raw_miner_count"] == 2
    assert meta["final_miner_count"] == 1
    assert payload["policy_metadata"]["miner_count"] == 1


def test_mark_mode_keeps_weights_and_marks_missing_hotkeys(tmp_path, monkeypatch):
    _common_env(monkeypatch, "mark")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = _store(tmp_path)
    ran_at = weights._ms_iso(now - timedelta(minutes=1))
    _add_eval_run(store, "hk-payable", ran_at)
    _add_eval_run(store, "hk-missing", ran_at)
    _add_metagraph_hotkey(store, "hk-payable", weights._ms_iso(now), uid=7)

    payload = _build(store, now)

    assert [w["miner_hotkey"] for w in payload["weights"]] == ["hk-missing", "hk-payable"]
    meta = payload["policy_metadata"]["payable_hotkeys"]
    assert meta["mode"] == "mark"
    assert meta["enforced"] is False
    assert meta["status"] == "marked_missing"
    assert meta["missing_hotkeys"] == ["hk-missing"]
    assert meta["final_miner_count"] == 2


def test_filter_mode_without_fresh_snapshot_fails_open_and_marks_status(tmp_path, monkeypatch):
    _common_env(monkeypatch, "filter")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = _store(tmp_path)
    ran_at = weights._ms_iso(now - timedelta(minutes=1))
    _add_eval_run(store, "hk-payable", ran_at)
    _add_eval_run(store, "hk-missing", ran_at)
    _add_metagraph_hotkey(store, "hk-payable", weights._ms_iso(now - timedelta(hours=1)), uid=7)

    payload = _build(store, now)

    assert [w["miner_hotkey"] for w in payload["weights"]] == ["hk-missing", "hk-payable"]
    meta = payload["policy_metadata"]["payable_hotkeys"]
    assert meta["mode"] == "filter"
    assert meta["enforced"] is False
    assert meta["snapshot_fresh"] is False
    assert meta["status"] == "no_fresh_snapshot"
    assert meta["final_miner_count"] == 2
