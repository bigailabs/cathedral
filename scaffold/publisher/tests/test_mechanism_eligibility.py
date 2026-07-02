"""Tests for the mechanism-router "who can earn" eligibility gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scaffold.publisher import mechanism_eligibility as elig
from scaffold.publisher import mechanism_router as R


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class FakeStore:
    """Minimal store: answers the two queries the gate issues."""

    def __init__(self, rows):
        # rows: list of {"hotkey","uid","updated_at_iso"}
        self.rows = rows

    def query(self, sql, params):
        if "updated_at_iso >" in sql:  # _load_fresh_metagraph_hotkeys
            _net, _nid, cutoff = params
            return [r for r in self.rows if str(r["updated_at_iso"]) > cutoff]
        # _load_hotkey_to_uid (all rows for network/netuid)
        return list(self.rows)


def _rows(now):
    fresh = _iso(now)
    stale = _iso(now - timedelta(hours=2))
    return [
        {"hotkey": "5A", "uid": 1, "updated_at_iso": fresh},
        {"hotkey": "5B", "uid": 2, "updated_at_iso": fresh},
        {"hotkey": "5C", "uid": 3, "updated_at_iso": fresh},
        {"hotkey": "5D", "uid": 4, "updated_at_iso": stale},  # stale -> excluded
    ]


def test_eligible_uids_returns_only_fresh_registered():
    now = datetime.now(timezone.utc)
    uids, meta = elig.eligible_uids(FakeStore(_rows(now)), now=now)
    assert uids == {1, 2, 3}
    assert meta["fail_closed"] is False
    assert meta["eligible_uid_count"] == 3


def test_empty_snapshot_fails_closed():
    now = datetime.now(timezone.utc)
    uids, meta = elig.eligible_uids(FakeStore([]), now=now)
    assert uids == set()
    assert meta["fail_closed"] is True
    assert meta["reason"] == "snapshot_unavailable"


def test_all_stale_fails_closed():
    now = datetime.now(timezone.utc)
    stale = [{"hotkey": "5A", "uid": 1,
              "updated_at_iso": _iso(now - timedelta(hours=5))}]
    uids, meta = elig.eligible_uids(FakeStore(stale), now=now)
    assert uids == set()
    assert meta["fail_closed"] is True


def test_compose_eligible_drops_unregistered_uid():
    now = datetime.now(timezone.utc)
    store = FakeStore(_rows(now))
    now_ms = 1_000_000
    specs = [R.MechanismSpec("m1", "5owner", 1.0, "signed", owner_uid=None)]
    # scores: uid 1 is registered, uid 99 is NOT
    scores = {
        "m1": ({1: 10.0, 99: 90.0},
               R.ScoreVectorMeta("m1", now_ms, True, "signed_post")),
    }
    weights, debug = elig.compose_eligible(store, specs, scores, now_ms=now_ms, now=now)
    assert set(weights) == {1}          # 99 dropped by the gate
    assert abs(sum(weights.values()) - 1.0) < 1e-9
    assert debug["eligibility"]["eligible_uid_count"] == 3
