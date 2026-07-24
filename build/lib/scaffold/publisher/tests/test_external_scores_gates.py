"""Real-money safety gates on the external-scores -> real-weight blend."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import weights


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now():
    return datetime.now(timezone.utc)


class FakeStore:
    """Answers the queries the blend path issues.

    Supports:
    - external_score_reports (for latest_snapshot_scores)
    - external_score_entries (for latest_snapshot_scores + recent_scores)
    - metagraph_hotkeys (for registration gate)
    """

    def __init__(self, ext_scores, meta_rows, *, complete=True, epoch=1,
                 generated_at=None, source="violet_audio"):
        now = _now()
        self._generated_at = generated_at or _iso(now)
        self._source = source
        self._epoch = epoch
        self._complete = complete
        self._report_id = "test-report-1"
        report_obj = {
            "source": source,
            "epoch": epoch,
            "complete": complete,
            "generated_at": self._generated_at,
            "scores": [{"miner_hotkey": hk, "score": s} for hk, s in ext_scores],
        }
        self._report_json = json.dumps(report_obj)
        self._ext_scores = ext_scores  # list of (hotkey, score)
        self.meta_rows = meta_rows     # list of {"hotkey": ..., "updated_at_iso": ...}

    def query(self, sql, params):
        if "FROM external_score_reports" in sql:
            return [{
                "id": self._report_id,
                "epoch": self._epoch,
                "generated_at_iso": self._generated_at,
                "received_at_iso": self._generated_at,
                "report_json": self._report_json,
            }] if self._ext_scores else []
        if "FROM external_score_entries" in sql:
            if "report_id" in sql:
                # latest_snapshot_scores path
                return [{"miner_hotkey": hk, "score": s}
                        for hk, s in self._ext_scores]
            # Legacy recent_scores path
            cutoff = params[1] if len(params) > 1 else ""
            return [{"miner_hotkey": hk, "score": s,
                     "received_at_iso": self._generated_at}
                    for hk, s in self._ext_scores
                    if s > 0 and self._generated_at > str(cutoff)]
        if "FROM metagraph_hotkeys" in sql:
            cutoff = params[2]
            return [r for r in self.meta_rows
                    if str(r["updated_at_iso"]) > str(cutoff)]
        return []

    def write(self, fn):
        raise NotImplementedError


def _meta(hotkeys):
    now = _now()
    return [{"hotkey": hk, "updated_at_iso": _iso(now)} for hk in hotkeys]


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.1")
    for k in ("CATHEDRAL_EXTERNAL_SCORES_MODE",
              "CATHEDRAL_EXTERNAL_SCORES_WEIGHT", "CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT",
              "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_registration_gate_drops_unregistered():
    store = FakeStore([("5REG", 1.0), ("5EVIL", 1.0)], _meta(["5REG", "5BASE"]))
    base = {"5REG": 0.5, "5BASE": 0.5}
    out, meta = weights._apply_external_scores(store, base, now=_now())
    assert "5EVIL" not in out, "external scores must not pay an unregistered hotkey"
    assert "5REG" in out


def test_snapshot_unavailable_fails_closed():
    store = FakeStore([("5REG", 1.0)], _meta([]))
    base = {"5REG": 0.5, "5BASE": 0.5}
    out, meta = weights._apply_external_scores(store, base, now=_now())
    assert out == base, "fail-closed: unverifiable registration must leave base untouched"


def test_fraction_knob_sets_share(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.1")
    _b, _e, share = weights._external_blend_weights()
    assert abs(share - 0.1) < 1e-9


def test_legacy_weights_capped_at_max_fraction(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT", "1.0")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_WEIGHT", "9.0")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", raising=False)
    _b, _e, share = weights._external_blend_weights()
    assert share <= 0.5 + 1e-9, f"external share {share} exceeded the cap"


def test_external_primary_requires_confirm():
    store = FakeStore([("5REG", 1.0)], _meta(["5REG", "5BASE"]))
    base = {"5REG": 0.2, "5BASE": 0.8}
    import os
    os.environ["CATHEDRAL_EXTERNAL_SCORES_MODE"] = "external_primary"
    try:
        out, _meta1 = weights._apply_external_scores(store, base, now=_now())
        assert "5BASE" in out, "external_primary without confirm must not drop base miners"
    finally:
        os.environ.pop("CATHEDRAL_EXTERNAL_SCORES_MODE", None)
        os.environ.pop("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", None)
