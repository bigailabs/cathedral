"""Tests for the artifact-tier score refresh + preview-compose orchestrator.

Covers the missing "scored -> weights" wiring: refresh runs only enabled artifact
mechanisms, tolerates a not-yet-merged adapter (skips + logs, never errors), persists
an empty vector as "contributes nothing", and the compose path inherits set_weights'
mainnet/SN39 refusal so it can never write real weights.
"""
from __future__ import annotations

import pytest

from scaffold.publisher import mechanism_artifact_refresh as arf
from scaffold.publisher import mechanism_eligibility as elig
from scaffold.publisher import mechanism_router as R
from scaffold.publisher import mechanism_weightset as ws


def _store():
    return R.SqliteMechanismStore(":memory:")


def _spec(mid, *, tier="artifact", enabled=True, weight=0.5):
    return R.MechanismSpec(mid, "5owner", weight, tier, owner_uid=None, enabled=enabled)


def _meta(mid, source="test"):
    return R.ScoreVectorMeta(mechanism_id=mid, signed_at_ms=123, sig_ok=True, source=source)


def _adapter(scores, *, source="test"):
    """A fake adapter returning a fixed vector; accepts the epoch kwarg like the real ones."""
    def fn(store, *, epoch=None):
        return dict(scores), _meta("m", source)
    return fn


# --------------------------------------------------------------------------- #
# refresh_artifact_scores
# --------------------------------------------------------------------------- #
def test_refresh_runs_only_enabled_artifact_specs():
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))                       # enabled artifact  -> run
    store.upsert_spec(_spec("sat_v2", enabled=False))            # disabled          -> skip
    store.upsert_spec(_spec("signed_x", tier="signed"))         # signed tier       -> skip
    adapters = {
        "cybergym_v0": _adapter({7: 3.0, 9: 1.0}),
        "sat_v2": _adapter({1: 99.0}),      # must NOT run (disabled)
        "signed_x": _adapter({2: 99.0}),    # must NOT run (signed tier)
    }
    refreshed = arf.refresh_artifact_scores(store, adapters=adapters)
    assert refreshed == {"cybergym_v0": 2}
    scores, meta = store.get_scores("cybergym_v0")
    assert scores == {7: 3.0, 9: 1.0} and meta.source == "test"
    assert store.get_scores("sat_v2") is None
    assert store.get_scores("signed_x") is None


def test_a_missing_adapter_is_skipped_not_errored():
    # cybergym_v0 registered but its adapter module isn't present yet (pre-PR#409)
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))
    refreshed = arf.refresh_artifact_scores(store, adapters={})   # nothing resolves
    assert refreshed == {}
    assert store.get_scores("cybergym_v0") is None                # left untouched, no raise


def test_one_failing_adapter_does_not_sink_the_cycle():
    store = _store()
    store.upsert_spec(_spec("bad"))
    store.upsert_spec(_spec("good"))

    def boom(store, *, epoch=None):
        raise RuntimeError("adapter exploded")

    refreshed = arf.refresh_artifact_scores(
        store, adapters={"bad": boom, "good": _adapter({5: 2.0})})
    assert refreshed == {"good": 1}                               # good still ran
    assert store.get_scores("bad") is None


def test_empty_scores_are_persisted_as_contributes_nothing():
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))
    refreshed = arf.refresh_artifact_scores(store, adapters={"cybergym_v0": _adapter({})})
    assert refreshed == {"cybergym_v0": 0}
    scores, _ = store.get_scores("cybergym_v0")
    assert scores == {}                                          # fresh empty overrides any stale row


def test_registered_adapters_point_at_real_entrypoints():
    # sat is on main and must resolve; cybergym is tolerated absent until PR#409 merges
    assert arf._resolve(arf.ARTIFACT_ADAPTERS["sat_v2"]) is not None
    cyber = arf._resolve(arf.ARTIFACT_ADAPTERS["cybergym_v0"])
    assert cyber is None or callable(cyber)


# --------------------------------------------------------------------------- #
# compose_and_publish
# --------------------------------------------------------------------------- #
def test_compose_and_publish_wires_refresh_then_compose_then_set_weights(monkeypatch):
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))
    seen = {}

    def fake_compose(s, specs, scores, *, now_ms, **kw):
        seen["scores"] = scores
        return {7: 1.0}, {"eligibility": "ok"}

    def fake_set_weights(composed, *, netuid, network, signing_key_hex, **kw):
        seen["composed"] = composed
        seen["target"] = (network, netuid)
        return {"published": True, "weights": composed}

    monkeypatch.setattr(elig, "compose_eligible", fake_compose)
    monkeypatch.setattr(ws, "set_weights", fake_set_weights)

    result, debug = arf.compose_and_publish(
        store, netuid=123, network="test", signing_key_hex="00" * 32,
        adapters={"cybergym_v0": _adapter({7: 3.0})})

    # refresh happened first (the adapter's scores reached compose)
    assert "cybergym_v0" in seen["scores"] and seen["scores"]["cybergym_v0"][0] == {7: 3.0}
    # the composed vector was handed to set_weights for THIS testnet target
    assert seen["composed"] == {7: 1.0} and seen["target"] == ("test", 123)
    assert result["published"] and debug["eligibility"] == "ok"


def test_compose_and_publish_inherits_the_mainnet_refusal(monkeypatch):
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))
    monkeypatch.setattr(elig, "compose_eligible", lambda *a, **k: ({}, {}))
    # real set_weights: hard-refuses finney / SN39 before doing anything
    with pytest.raises(ws.UnsafeNetworkError):
        arf.compose_and_publish(store, netuid=39, network="finney", signing_key_hex="00" * 32,
                                adapters={"cybergym_v0": _adapter({7: 3.0})})
