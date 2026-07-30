"""Tests for the Mechanism Router core spine.

Covers ``compose`` (the deterministic scored→weights kernel), the SQLite store
round-trip, and the admin-gated PUT /mechanisms/{id} router. Everything runs
with no env configured (guardrail: default-OFF => empty => caller keeps V1).
"""

from __future__ import annotations

import math

import pytest

from scaffold.publisher.mechanism_router import (
    MechanismSpec,
    ScoreVector,
    ScoreVectorMeta,
    SqliteMechanismStore,
    compose,
    create_router,
)

NOW = 1_000_000


def _meta(mid: str, *, signed_at_ms: int = NOW, sig_ok: bool = True, source: str = "signed_post"):
    return ScoreVectorMeta(mechanism_id=mid, signed_at_ms=signed_at_ms, sig_ok=sig_ok, source=source)


def _spec(mid: str, frac: float, *, owner_uid=None, enabled=True, tier="artifact"):
    return MechanismSpec(
        mechanism_id=mid,
        owner_pubkey=f"pk_{mid}",
        weight_fraction=frac,
        tier=tier,
        owner_uid=owner_uid,
        enabled=enabled,
    )


def _approx_sums_to_one(weights: dict[int, float]) -> bool:
    return math.isclose(sum(weights.values()), 1.0, rel_tol=1e-12, abs_tol=1e-12)


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------

def test_zero_mechanisms_returns_empty_caller_keeps_v1():
    weights, dbg = compose(
        [], {}, registered_uids={1, 2, 3}, now_ms=NOW,
    )
    assert weights == {}
    assert dbg["n_final_uids"] == 0
    assert dbg["combined_total_before_renorm"] == 0.0


def test_two_mechanisms_convex_combo_exact():
    # m1 (0.6) over uids {1,2,3}: raw [1,1,2] -> normalized [0.25,0.25,0.5]
    # m2 (0.4) over uids {2,3}:   raw [3,1]   -> normalized [0.75,0.25]
    specs = [_spec("m1", 0.6), _spec("m2", 0.4)]
    scores = {
        "m1": ({1: 1.0, 2: 1.0, 3: 2.0}, _meta("m1")),
        "m2": ({2: 3.0, 3: 1.0}, _meta("m2")),
    }
    weights, dbg = compose(
        specs, scores, registered_uids={1, 2, 3}, block_self_weight=True, now_ms=NOW,
    )
    # Hand-computed convex combination (fractions already sum to 1 so no renorm shift):
    #   uid1 = 0.6*0.25                     = 0.15
    #   uid2 = 0.6*0.25 + 0.4*0.75          = 0.15 + 0.30 = 0.45
    #   uid3 = 0.6*0.50 + 0.4*0.25          = 0.30 + 0.10 = 0.40
    assert math.isclose(weights[1], 0.15, abs_tol=1e-12)
    assert math.isclose(weights[2], 0.45, abs_tol=1e-12)
    assert math.isclose(weights[3], 0.40, abs_tol=1e-12)
    assert _approx_sums_to_one(weights)
    assert dbg["mechanisms"]["m1"]["contributing"] is True
    assert dbg["mechanisms"]["m2"]["contributing"] is True


def test_fractions_not_summing_to_one_still_renormalize_to_one():
    # Only m1 present with fraction 0.6 -> after renorm it must sum to 1.
    specs = [_spec("m1", 0.6)]
    scores = {"m1": ({1: 1.0, 2: 3.0}, _meta("m1"))}
    weights, _ = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert math.isclose(weights[1], 0.25, abs_tol=1e-12)
    assert math.isclose(weights[2], 0.75, abs_tol=1e-12)
    assert _approx_sums_to_one(weights)


def test_missing_sigbad_and_stale_each_contribute_zero_others_unaffected():
    specs = [
        _spec("good", 0.5),
        _spec("missing", 0.2),
        _spec("sigbad", 0.2),
        _spec("stale", 0.1),
    ]
    scores = {
        "good": ({1: 1.0, 2: 1.0}, _meta("good")),
        # "missing" intentionally absent from scores
        "sigbad": ({1: 5.0, 2: 5.0}, _meta("sigbad", sig_ok=False)),
        "stale": ({1: 9.0}, _meta("stale", signed_at_ms=NOW - 10_000)),
    }
    weights, dbg = compose(
        specs, scores, registered_uids={1, 2}, max_score_age_ms=1_000, now_ms=NOW,
    )
    # Only "good" contributes: normalized [0.5, 0.5], renormalized still [0.5,0.5]
    assert math.isclose(weights[1], 0.5, abs_tol=1e-12)
    assert math.isclose(weights[2], 0.5, abs_tol=1e-12)
    assert _approx_sums_to_one(weights)
    assert dbg["mechanisms"]["good"]["contributing"] is True
    assert dbg["mechanisms"]["missing"]["fallback_reason"] == "missing"
    assert dbg["mechanisms"]["sigbad"]["fallback_reason"] == "sig_bad"
    assert dbg["mechanisms"]["stale"]["fallback_reason"] == "stale"
    for k in ("missing", "sigbad", "stale"):
        assert dbg["mechanisms"][k]["contributing"] is False


def test_block_self_weight_zeros_owner_uid():
    specs = [_spec("m1", 1.0, owner_uid=2)]
    scores = {"m1": ({1: 1.0, 2: 100.0, 3: 1.0}, _meta("m1"))}
    weights, dbg = compose(
        specs, scores, registered_uids={1, 2, 3}, block_self_weight=True, now_ms=NOW,
    )
    assert 2 not in weights  # owner_uid removed before normalize
    # remaining raw [1,1] -> [0.5, 0.5]
    assert math.isclose(weights[1], 0.5, abs_tol=1e-12)
    assert math.isclose(weights[3], 0.5, abs_tol=1e-12)
    assert _approx_sums_to_one(weights)

    # With blocking disabled, owner_uid keeps its (dominant) weight.
    weights_off, _ = compose(
        specs, scores, registered_uids={1, 2, 3}, block_self_weight=False, now_ms=NOW,
    )
    assert 2 in weights_off
    assert weights_off[2] > weights_off[1]


def test_unregistered_uids_dropped():
    specs = [_spec("m1", 1.0)]
    scores = {"m1": ({1: 1.0, 2: 1.0, 99: 1000.0}, _meta("m1"))}
    weights, _ = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert 99 not in weights
    assert math.isclose(weights[1], 0.5, abs_tol=1e-12)
    assert math.isclose(weights[2], 0.5, abs_tol=1e-12)
    assert _approx_sums_to_one(weights)


def test_all_zero_returns_empty():
    # Scores exist but all uids unregistered / zeroed => empty (caller keeps V1).
    specs = [_spec("m1", 1.0)]
    scores = {"m1": ({99: 5.0, 98: 5.0}, _meta("m1"))}
    weights, dbg = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert weights == {}
    assert dbg["mechanisms"]["m1"]["fallback_reason"] == "empty_after_filter"
    assert dbg["n_final_uids"] == 0


def test_disabled_and_zero_fraction_specs_skipped():
    specs = [_spec("off", 0.5, enabled=False), _spec("zero", 0.0), _spec("m1", 0.5)]
    scores = {
        "off": ({1: 1.0}, _meta("off")),
        "zero": ({1: 1.0}, _meta("zero")),
        "m1": ({1: 1.0, 2: 1.0}, _meta("m1")),
    }
    weights, dbg = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert _approx_sums_to_one(weights)
    assert dbg["mechanisms"]["off"]["fallback_reason"] == "disabled"
    assert dbg["mechanisms"]["zero"]["fallback_reason"] == "zero_fraction"


# ---------------------------------------------------------------------------
# forfeited share accounting: an unproven mechanism's fraction must never be
# silently reallocated to the mechanisms that did contribute.
# ---------------------------------------------------------------------------

def test_forfeited_fraction_is_always_reported():
    specs = [_spec("good", 0.5), _spec("missing", 0.3), _spec("sigbad", 0.2)]
    scores = {
        "good": ({1: 1.0, 2: 1.0}, _meta("good")),
        "sigbad": ({1: 5.0}, _meta("sigbad", sig_ok=False)),
    }
    _, dbg = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert math.isclose(dbg["forfeited_fraction"], 0.5, abs_tol=1e-12)
    assert math.isclose(dbg["contributing_fraction"], 0.5, abs_tol=1e-12)


def test_legacy_renormalization_reallocates_the_forfeited_share():
    """Documents the default behavior explicitly: renormalizing to 1 hands the
    forfeited share to whoever did contribute. Any caller that must burn an
    unproven share has to pass preserve_forfeited=True."""
    specs = [_spec("good", 0.5), _spec("gone", 0.5)]
    scores = {"good": ({1: 1.0, 2: 1.0}, _meta("good"))}
    weights, dbg = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    assert _approx_sums_to_one(weights)  # "gone"'s 0.5 was absorbed by "good"
    assert math.isclose(dbg["forfeited_fraction"], 0.5, abs_tol=1e-12)


def test_preserve_forfeited_keeps_the_share_out_of_other_mechanisms():
    """The burn-correctness property: a missing mechanism's share is not paid to
    another mechanism's miners. The contributing mechanism keeps exactly its own
    fraction and the caller burns the remainder."""
    specs = [_spec("good", 0.5), _spec("gone", 0.5)]
    scores = {"good": ({1: 1.0, 2: 1.0}, _meta("good"))}
    weights, dbg = compose(
        specs, scores, registered_uids={1, 2}, now_ms=NOW, preserve_forfeited=True,
    )
    assert math.isclose(weights[1], 0.25, abs_tol=1e-12)
    assert math.isclose(weights[2], 0.25, abs_tol=1e-12)
    assert math.isclose(sum(weights.values()), 0.5, abs_tol=1e-12)
    assert math.isclose(dbg["forfeited_fraction"], 0.5, abs_tol=1e-12)
    assert dbg["preserve_forfeited"] is True


@pytest.mark.parametrize("reason,scores_entry", [
    ("missing", None),
    ("sig_bad", ({1: 5.0, 2: 5.0}, _meta("cybergym_v0", sig_ok=False))),
    ("stale", ({1: 5.0, 2: 5.0}, _meta("cybergym_v0", signed_at_ms=NOW - 10_000))),
    ("empty_after_filter", ({99: 5.0}, _meta("cybergym_v0"))),
])
def test_every_non_contributing_reason_forfeits_rather_than_shifts(
    reason, scores_entry,
):
    specs = [_spec("sat_v2", 0.5), _spec("cybergym_v0", 0.5)]
    scores = {"sat_v2": ({1: 1.0, 2: 1.0}, _meta("sat_v2"))}
    if scores_entry is not None:
        scores["cybergym_v0"] = scores_entry
    weights, dbg = compose(
        specs, scores, registered_uids={1, 2}, max_score_age_ms=1_000,
        now_ms=NOW, preserve_forfeited=True,
    )
    assert dbg["mechanisms"]["cybergym_v0"]["fallback_reason"] == reason
    assert math.isclose(dbg["forfeited_fraction"], 0.5, abs_tol=1e-12)
    # SAT keeps its own 0.5 and not one part of CyberGym's forfeited share.
    assert math.isclose(sum(weights.values()), 0.5, abs_tol=1e-12)


def test_preserve_forfeited_with_nothing_contributing_is_empty():
    specs = [_spec("gone", 1.0)]
    weights, dbg = compose(
        specs, {}, registered_uids={1, 2}, now_ms=NOW, preserve_forfeited=True,
    )
    assert weights == {}
    assert math.isclose(dbg["forfeited_fraction"], 1.0, abs_tol=1e-12)
    assert dbg["contributing_fraction"] == 0.0


def test_compose_is_deterministic():
    specs = [_spec("b", 0.3), _spec("a", 0.7)]
    scores = {
        "a": ({1: 2.0, 2: 1.0}, _meta("a")),
        "b": ({1: 1.0, 2: 3.0}, _meta("b")),
    }
    r1, _ = compose(specs, scores, registered_uids={1, 2}, now_ms=NOW)
    r2, _ = compose(list(reversed(specs)), scores, registered_uids={1, 2}, now_ms=NOW)
    assert r1 == r2


# ---------------------------------------------------------------------------
# SqliteMechanismStore
# ---------------------------------------------------------------------------

def test_store_roundtrip(tmp_path):
    db = tmp_path / "mech.sqlite3"
    store = SqliteMechanismStore(str(db))
    spec = _spec("m1", 0.4, owner_uid=7)
    store.upsert_spec(spec)
    assert store.get_spec("m1") == spec
    assert store.list_specs() == [spec]

    # upsert overwrites
    spec2 = _spec("m1", 0.9, owner_uid=None, enabled=False)
    store.upsert_spec(spec2)
    assert store.get_spec("m1") == spec2

    # scores round-trip with int keys restored
    vec: ScoreVector = {1: 0.5, 2: 1.5}
    meta = _meta("m1", source="sat_adapter")
    store.put_scores("m1", vec, meta)
    got = store.get_scores("m1")
    assert got is not None
    got_vec, got_meta = got
    assert got_vec == vec
    assert all(isinstance(k, int) for k in got_vec)
    assert got_meta == meta
    assert store.get_scores("nope") is None


def test_store_persists_across_instances(tmp_path):
    db = tmp_path / "mech.sqlite3"
    SqliteMechanismStore(str(db)).upsert_spec(_spec("m1", 0.3))
    # A fresh instance on the same path sees prior writes.
    assert SqliteMechanismStore(str(db)).get_spec("m1") is not None


def test_store_env_default_path(monkeypatch, tmp_path):
    db = tmp_path / "sub" / "mech.sqlite3"
    monkeypatch.setenv("CATHEDRAL_MECH_DB_PATH", str(db))
    store = SqliteMechanismStore()  # picks up env; creates parent dir
    store.upsert_spec(_spec("m1", 0.2))
    assert db.exists()
    assert store.get_spec("m1") is not None


# ---------------------------------------------------------------------------
# Admin-gated PUT /mechanisms/{id}
# ---------------------------------------------------------------------------

def _client(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(create_router(store))
    return TestClient(app)


_BODY = {"owner_pubkey": "pk_x", "weight_fraction": 0.5, "tier": "artifact", "owner_uid": 3}


def test_put_mechanism_requires_configured_token(monkeypatch, tmp_path):
    monkeypatch.delenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", raising=False)
    store = SqliteMechanismStore(str(tmp_path / "m.sqlite3"))
    resp = _client(store).put("/mechanisms/m1", json=_BODY)
    assert resp.status_code == 503


def test_put_mechanism_rejects_bad_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "secret")
    store = SqliteMechanismStore(str(tmp_path / "m.sqlite3"))
    resp = _client(store).put(
        "/mechanisms/m1", json=_BODY, headers={"Authorization": "Bearer wrong"}
    )
    assert resp.status_code == 401
    assert store.get_spec("m1") is None


def test_put_mechanism_upserts_with_valid_token(monkeypatch, tmp_path):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "secret")
    store = SqliteMechanismStore(str(tmp_path / "m.sqlite3"))
    resp = _client(store).put(
        "/mechanisms/m1", json=_BODY, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    spec = store.get_spec("m1")
    assert spec is not None
    assert spec.weight_fraction == 0.5
    assert spec.owner_uid == 3
    assert spec.tier == "artifact"


def test_put_mechanism_validates_fraction_range(monkeypatch, tmp_path):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "secret")
    store = SqliteMechanismStore(str(tmp_path / "m.sqlite3"))
    bad = dict(_BODY, weight_fraction=1.5)
    resp = _client(store).put(
        "/mechanisms/m1", json=bad, headers={"Authorization": "Bearer secret"}
    )
    assert resp.status_code == 422
