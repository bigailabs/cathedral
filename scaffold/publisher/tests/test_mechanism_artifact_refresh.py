"""Tests for the artifact-tier score refresh + preview-compose orchestrator.

Covers the missing "scored -> weights" wiring: refresh runs only enabled artifact
mechanisms, tolerates a not-yet-merged adapter (skips + logs, never errors), persists
an empty vector as "contributes nothing", and the compose path inherits set_weights'
mainnet/SN39 refusal so it can never write real weights.

Also pins the ordering the CyberGym closed-epoch gate depends on: an adapter that
raises is skipped *before* put_scores, so a refusal preserves the last published
vector instead of overwriting it with an empty one.
"""
from __future__ import annotations

import pytest

from scaffold.publisher import mechanism_artifact_refresh as arf
from scaffold.publisher import mechanism_cybergym_adapter as cybergym
from scaffold.publisher import mechanism_eligibility as elig
from scaffold.publisher import mechanism_router as R
from scaffold.publisher import mechanism_weightset as ws
from scaffold.publisher.store import Store


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


def test_adapter_raise_leaves_prior_scores_untouched():
    """The load-bearing half of the CyberGym closed-epoch gate.

    That gate refuses by *raising* rather than returning ``({}, meta)`` for one
    reason only: this loop catches the exception and skips the mechanism BEFORE
    ``put_scores``, so the last published vector survives the cycle. An empty
    return would instead be persisted (see the test above) — wiping a real closed
    epoch's scores with something ``compose`` cannot tell apart from "nobody
    solved". Nothing else pins that ordering, so a refactor that moved
    ``put_scores`` ahead of the try, or that swallowed the raise into ``{}``,
    would silently break the gate while the adapter's own tests stayed green.

    Pairs with test_missing_status_table_raises_when_epoch_unspecified in
    test_mechanism_cybergym_adapter.py, which pins the other side of the seam.
    """
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))

    # Cycle 1: a closed epoch publishes normally.
    refreshed = arf.refresh_artifact_scores(
        store, adapters={"cybergym_v0": _adapter({7: 3.0, 9: 1.0})})
    assert refreshed == {"cybergym_v0": 2}

    # Cycle 2: the epoch is no longer publishable, so the adapter refuses.
    def refuses(store, *, epoch=None):
        raise cybergym.CyberGymEpochNotClosed("epoch 2 is open")

    refreshed = arf.refresh_artifact_scores(store, adapters={"cybergym_v0": refuses})
    assert refreshed == {}                                       # not counted as refreshed
    scores, _ = store.get_scores("cybergym_v0")
    assert scores == {7: 3.0, 9: 1.0}                            # prior vector still stands


def test_registered_adapters_point_at_real_entrypoints():
    # sat is on main and must resolve; cybergym is tolerated absent until PR#409 merges
    assert arf._resolve(arf.ARTIFACT_ADAPTERS["sat_v2"]) is not None
    cyber = arf._resolve(arf.ARTIFACT_ADAPTERS["cybergym_v0"])
    assert cyber is None or callable(cyber)


# --------------------------------------------------------------------------- #
# The two stores are two databases
# --------------------------------------------------------------------------- #
def test_a_real_adapter_runs_end_to_end_through_refresh(tmp_path):
    """No ``adapters=`` override — the gap every other test in this file leaves open.

    Each of those injects a fake that ignores the store it is handed, so they pass
    whatever refresh passes. That hid a real defect: refresh gave adapters the
    ``MechanismStore``, which has no ``query``, so every real adapter died on
    ``AttributeError``, got caught by the per-adapter ``except``, and was logged as
    "skipping" — indistinguishable from a mechanism with nothing to contribute. No
    artifact-tier mechanism could ever publish, and ``POST /mechanisms/refresh``
    still answered ``{"ok": true, "refreshed": {}}``.

    So drive the registered cybergym adapter for real, against a real publisher
    Store, and require an actual vector out the far end.
    """
    data = Store(str(tmp_path / "publisher.db"))
    data.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_scores ("
        "miner_hotkey TEXT NOT NULL, epoch INTEGER NOT NULL, "
        "score REAL NOT NULL, PRIMARY KEY (miner_hotkey, epoch))"))
    data.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_epoch_status ("
        "epoch INTEGER PRIMARY KEY, state TEXT NOT NULL, "
        "detail TEXT NOT NULL DEFAULT '', scored_miners INTEGER NOT NULL DEFAULT 0, "
        "marked_at TEXT NOT NULL DEFAULT '')"))
    data.write(lambda c: c.execute(
        "INSERT INTO cybergym_scores(miner_hotkey, epoch, score) VALUES (?,?,?)",
        ("5Alice", 7, 12.0)))
    data.write(lambda c: c.execute(
        "INSERT INTO cybergym_epoch_status(epoch, state) VALUES (?,?)",
        (7, cybergym.EPOCH_CLOSED)))
    data.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO metagraph_hotkeys("
        "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
        ") VALUES (?,?,?,?,?,?,?)",
        ("finney", 39, "5Alice", 4, "", 1, "2026-07-01T00:00:00.000Z")))

    store = _store()                                   # the OTHER database
    store.upsert_spec(_spec("cybergym_v0"))

    refreshed = arf.refresh_artifact_scores(store, epoch=7, data_store=data)
    assert refreshed == {"cybergym_v0": 1}
    scores, meta = store.get_scores("cybergym_v0")
    assert scores == {4: 12.0}
    assert meta.source == "cybergym_adapter"


def test_the_mechanism_store_is_not_accepted_as_the_data_store():
    """Passing the MechanismStore where the publisher Store belongs must not quietly
    look like success. It cannot serve adapter reads — no ``query`` — so the cycle
    reports nothing refreshed and leaves the row untouched rather than persisting
    an empty vector."""
    store = _store()
    store.upsert_spec(_spec("cybergym_v0"))
    assert not hasattr(store, "query")

    refreshed = arf.refresh_artifact_scores(store, epoch=7, data_store=store)
    assert refreshed == {}
    assert store.get_scores("cybergym_v0") is None


def test_default_data_store_reads_the_publisher_db_not_the_mechanism_db(monkeypatch, tmp_path):
    """``CATHEDRAL_DB_PATH``, the same var server.py uses — deliberately not
    ``CATHEDRAL_MECH_DB_PATH``, which points at the specs database."""
    monkeypatch.setenv(arf.DATA_DB_PATH_ENV, str(tmp_path / "publisher.db"))
    monkeypatch.setenv("CATHEDRAL_MECH_DB_PATH", str(tmp_path / "mechanisms.sqlite3"))
    data = arf.default_data_store()
    assert data.path == str(tmp_path / "publisher.db")
    assert hasattr(data, "query")          # can serve adapter reads
    assert not hasattr(data, "list_specs")  # and is not a MechanismStore


def test_refresh_opens_no_database_when_nothing_is_enabled(monkeypatch):
    """The default data store is resolved lazily. A cycle with no enabled artifact
    spec must not construct one — the periodic loop runs this on every tick."""
    store = _store()
    store.upsert_spec(_spec("sat_v2", enabled=False))
    store.upsert_spec(_spec("signed_x", tier="signed"))

    def boom():
        raise AssertionError("default_data_store() must not be called")

    monkeypatch.setattr(arf, "default_data_store", boom)
    assert arf.refresh_artifact_scores(store) == {}


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
