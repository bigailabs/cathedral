"""Tests for results_publisher.publish_miner_results.

stdlib-only: uses an in-memory fake HippiusPresign and a real SQLite Store
so no network calls are made.

Covers:
  * JSON shape and schema field
  * total_verified and total_score only count 'verified' rows
  * 'rejected' rows appear in items but contribute 0 to totals
  * publish_changed_miners deduplicates (hotkey, epoch) pairs
  * best-effort: a hip.put failure is swallowed, returns None
  * gate: publish_changed_miners is a no-op when
    CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED is not set
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from scaffold.publisher import results_publisher
from scaffold.publisher.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class _FakeHip:
    """Records put calls; never raises unless _boom is set."""

    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self._boom = boom

    def put(self, key: str, data: bytes, **kwargs) -> str:
        if self._boom:
            raise RuntimeError("simulated hippius failure")
        self.calls.append((key, data))
        return f"https://fake.hippius.example/{key}?sig=x"


def _insert_event(store: Store, *, hotkey: str, epoch: int, challenge_id: str,
                  tier: int, seq: int, status: str, weighted_score: float,
                  verified_at_iso: str | None = None, challenge_kind: str = "sat") -> None:
    now = _now_iso()
    row_id = f"{hotkey}-{challenge_id}"

    def _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO v2_submit_events "
            "(id, idempotency_key, miner_hotkey, challenge_id, card_id, "
            "epoch, tier, seq, cnf_sha256, assignment_encoding, "
            "assignment_sha256, assignment_b64, status, "
            "weighted_score, answer_hash, verifier_details_hash, "
            "verified_at_iso, challenge_kind, received_at_iso, "
            "submitted_at, signature, submit_token_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id, row_id, hotkey, challenge_id, "synthetic_boolean_v1",
                epoch, tier, seq, "", "bitset/v1",
                "", "", status,
                weighted_score, "", "",
                verified_at_iso, challenge_kind, now,
                now, "", "",
            ),
        )

    store.write(_tx)


# ---------------------------------------------------------------------------
# publish_miner_results shape
# ---------------------------------------------------------------------------

def test_json_schema_and_shape(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey1111111111111111111111111111111111111"
    epoch = 42
    now = _now_iso()
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t1-e42-aaa",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=now)
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t2-e42-bbb",
                  tier=2, seq=0, status="verified", weighted_score=1.5,
                  verified_at_iso=now)

    hip = _FakeHip()
    url = results_publisher.publish_miner_results(store, hip, hk, epoch)

    assert url is not None
    assert len(hip.calls) == 1
    key, data = hip.calls[0]
    assert key == f"results/{hk}.json"

    payload = json.loads(data)
    assert payload["schema"] == "cathedral.v2.results.v1"
    assert payload["miner_hotkey"] == hk
    assert payload["epoch"] == epoch
    assert "updated_at" in payload
    assert payload["total_verified"] == 2
    assert abs(payload["total_score"] - 2.5) < 1e-6
    assert len(payload["items"]) == 2


def test_only_verified_rows_contribute_to_total_score(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey2222222222222222222222222222222222222"
    epoch = 7
    now = _now_iso()
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t1-e7-v1",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=now)
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t1-e7-r1",
                  tier=1, seq=1, status="rejected", weighted_score=0.0)
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t2-e7-v2",
                  tier=2, seq=0, status="verified", weighted_score=1.5,
                  verified_at_iso=now)

    hip = _FakeHip()
    results_publisher.publish_miner_results(store, hip, hk, epoch)

    payload = json.loads(hip.calls[0][1])
    assert payload["total_verified"] == 2
    assert abs(payload["total_score"] - 2.5) < 1e-6
    assert len(payload["items"]) == 3

    rejected_item = next(i for i in payload["items"] if i["status"] == "rejected")
    assert rejected_item["weighted_score"] == 0.0


def test_rejected_rows_have_zero_score_in_items(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey3333333333333333333333333333333333333"
    epoch = 3
    # rejected row with non-zero weighted_score in DB (defensive: should never
    # happen, but the publisher must not propagate it).
    _insert_event(store, hotkey=hk, epoch=epoch, challenge_id="pm-t1-e3-r1",
                  tier=1, seq=0, status="rejected", weighted_score=9.9)

    hip = _FakeHip()
    results_publisher.publish_miner_results(store, hip, hk, epoch)

    payload = json.loads(hip.calls[0][1])
    assert payload["total_score"] == 0.0
    assert payload["total_verified"] == 0
    assert payload["items"][0]["weighted_score"] == 0.0


def test_publish_failure_is_swallowed_returns_none(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey4444444444444444444444444444444444444"
    _insert_event(store, hotkey=hk, epoch=1, challenge_id="pm-t1-e1-x",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=_now_iso())

    hip = _FakeHip(boom=True)
    # must not raise
    result = results_publisher.publish_miner_results(store, hip, hk, 1)
    assert result is None


# ---------------------------------------------------------------------------
# publish_changed_miners deduplication + gate
# ---------------------------------------------------------------------------

def test_publish_changed_miners_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED", "1")
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey5555555555555555555555555555555555555"
    now = _now_iso()
    _insert_event(store, hotkey=hk, epoch=5, challenge_id="pm-t1-e5-a",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=now)

    hip = _FakeHip()
    # Three results for the same (hotkey, epoch) — should only produce one put.
    batch = [
        {"miner_hotkey": hk, "epoch": 5, "status": "verified"},
        {"miner_hotkey": hk, "epoch": 5, "status": "verified"},
        {"miner_hotkey": hk, "epoch": 5, "status": "rejected"},
    ]
    results_publisher.publish_changed_miners(store, hip, batch)
    assert len(hip.calls) == 1


def test_publish_changed_miners_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED", raising=False)
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey6666666666666666666666666666666666666"
    _insert_event(store, hotkey=hk, epoch=1, challenge_id="pm-t1-e1-y",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=_now_iso())

    hip = _FakeHip()
    results_publisher.publish_changed_miners(store, hip, [
        {"miner_hotkey": hk, "epoch": 1, "status": "verified"},
    ])
    assert len(hip.calls) == 0


def test_publish_changed_miners_noop_when_hip_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED", "1")
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hk = "5FakeHotkey7777777777777777777777777777777777777"
    _insert_event(store, hotkey=hk, epoch=1, challenge_id="pm-t1-e1-z",
                  tier=1, seq=0, status="verified", weighted_score=1.0,
                  verified_at_iso=_now_iso())

    results_publisher.publish_changed_miners(store, None, [
        {"miner_hotkey": hk, "epoch": 1, "status": "verified"},
    ])
    # no assertion needed — just must not raise


def test_empty_batch_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_RESULTS_PUBLISH_ENABLED", "1")
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    hip = _FakeHip()
    results_publisher.publish_changed_miners(store, hip, [])
    assert len(hip.calls) == 0
