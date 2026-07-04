"""Real-money safety gates on the external-scores → real-weight blend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import weights


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class FakeStore:
    """Answers the two queries the blend issues: external entries + metagraph."""

    def __init__(self, ext_rows, meta_rows):
        self.ext_rows = ext_rows      # {miner_hotkey, score, received_at_iso}
        self.meta_rows = meta_rows    # {hotkey, updated_at_iso}

    def query(self, sql, params):
        if "FROM external_score_entries" in sql:
            _src, cutoff = params[0], params[1]
            return [r for r in self.ext_rows
                    if r["score"] > 0 and str(r["received_at_iso"]) > str(cutoff)]
        if "FROM metagraph_hotkeys" in sql:
            cutoff = params[2]
            return [r for r in self.meta_rows if str(r["updated_at_iso"]) > str(cutoff)]
        return []

    def write(self, fn):  # unused here
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_SOURCE", "violet_audio")
    for k in ("CATHEDRAL_EXTERNAL_SCORES_MODE", "CATHEDRAL_EXTERNAL_SCORES_FRACTION",
              "CATHEDRAL_EXTERNAL_SCORES_WEIGHT", "CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT",
              "CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM"):
        monkeypatch.delenv(k, raising=False)
    yield


def _now():
    return datetime.now(timezone.utc)


def _ext(rows):
    now = _now()
    return [{"miner_hotkey": hk, "score": s, "received_at_iso": _iso(now)} for hk, s in rows]


def _meta(hotkeys):
    now = _now()
    return [{"hotkey": hk, "updated_at_iso": _iso(now)} for hk in hotkeys]


def test_registration_gate_drops_unregistered():
    # ext scores a registered (5REG) and an UNregistered (5EVIL) hotkey
    store = FakeStore(_ext([("5REG", 1.0), ("5EVIL", 1.0)]), _meta(["5REG", "5BASE"]))
    base = {"5REG": 0.5, "5BASE": 0.5}
    out = weights._apply_external_scores(store, base, now=_now())
    assert "5EVIL" not in out, "external scores must not pay an unregistered hotkey"
    assert "5REG" in out


def test_snapshot_unavailable_fails_closed():
    # no fresh metagraph rows -> cannot verify registration -> do NOT blend
    store = FakeStore(_ext([("5REG", 1.0)]), _meta([]))
    base = {"5REG": 0.5, "5BASE": 0.5}
    out = weights._apply_external_scores(store, base, now=_now())
    assert out == base, "fail-closed: unverifiable registration must leave base untouched"


def test_fraction_knob_sets_share(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_FRACTION", "0.1")
    _b, _e, share = weights._external_blend_weights()
    assert abs(share - 0.1) < 1e-9


def test_legacy_weights_capped_at_max_fraction(monkeypatch):
    # a fat external_weight must be capped at the default 0.5 share
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_BASE_WEIGHT", "1.0")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_WEIGHT", "9.0")  # would be 90%
    _b, _e, share = weights._external_blend_weights()
    assert share <= 0.5 + 1e-9, f"external share {share} exceeded the cap"


def test_external_primary_requires_confirm():
    store = FakeStore(_ext([("5REG", 1.0)]), _meta(["5REG", "5BASE"]))
    base = {"5REG": 0.2, "5BASE": 0.8}
    import os
    os.environ["CATHEDRAL_EXTERNAL_SCORES_MODE"] = "external_primary"
    try:
        # no confirm -> must NOT wipe base to pure-external
        out = weights._apply_external_scores(store, base, now=_now())
        assert "5BASE" in out, "external_primary without confirm must not drop base miners"
        # with confirm -> pure external (only the registered ext hotkey)
        os.environ["CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM"] = "true"
        out2 = weights._apply_external_scores(store, base, now=_now())
        assert set(out2) == {"5REG"}, "confirmed external_primary should be external-only"
    finally:
        os.environ.pop("CATHEDRAL_EXTERNAL_SCORES_MODE", None)
        os.environ.pop("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", None)
