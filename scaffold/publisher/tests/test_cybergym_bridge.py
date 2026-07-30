"""Tests for scaffold/publisher/cybergym_bridge.py.

The composition half of the CyberGym lane: MechanismSpec (default off, fraction
zero), composition through the sanctioned eligibility wrapper, and the property
the whole lane exists to guarantee: a CyberGym share that cannot be proven goes
to BURN, never to another miner and never to another mechanism.

Non-writing: no chain, no signing, no weight submission. Only a temporary SQLite
publisher Store.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import (
    cybergym_bridge as bridge,
    cybergym_contract as contract,
    mechanism_eligibility,
    mechanism_router,
    weights,
)
from scaffold.publisher.mechanism_router import MechanismSpec, ScoreVectorMeta
from scaffold.publisher.store import Store

NETWORK = "finney"
NETUID = 39
BURN_HOTKEY = "5BurnDestination"
BURN_UID = 204
SECRET = "bridge-test-hmac-secret"
PRODUCER = "5Producer"
UNITS = "level_weighted_verified_solves"
NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"), prefer_env_database_url=False)


def _env(
    monkeypatch, *, enabled=True, fraction="0.25",
    burn_hotkey=BURN_HOTKEY, burn_uid="",
) -> None:
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    monkeypatch.setenv(weights.NETWORK_ENV, NETWORK)
    monkeypatch.setenv(weights.NETUID_ENV, str(NETUID))
    monkeypatch.delenv("CATHEDRAL_CYBERGYM_MAX_SCORE_AGE_SECS", raising=False)
    monkeypatch.delenv("CATHEDRAL_CYBERGYM_MAX_FUTURE_SKEW_SECS", raising=False)
    if enabled:
        monkeypatch.setenv(bridge.MECHANISM_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(bridge.MECHANISM_ENABLED_ENV, raising=False)
    if fraction is None:
        monkeypatch.delenv(bridge.WEIGHT_FRACTION_ENV, raising=False)
    else:
        monkeypatch.setenv(bridge.WEIGHT_FRACTION_ENV, fraction)
    # The burn destination is resolved by HOTKEY. The numeric env var is left
    # empty by default so no test can pass by leaning on the UID 204 default.
    monkeypatch.setenv(weights.BURN_HOTKEY_ENV, burn_hotkey or "")
    monkeypatch.setenv(weights.BURN_UID_ENV, burn_uid or "")


def _report(
    store: Store,
    *,
    epoch: int,
    scores: dict[str, float],
    generated_at: datetime | None = None,
    complete: int = 1,
    body_sha256: str | None = None,
) -> str:
    """Persist one report the way the authenticated ingest route would.

    The body is the exact canonical document and the signature is a real HMAC
    under SECRET, so the adapter's read-side verification passes. Tests that need
    an unverifiable report override body_sha256.
    """
    generated = generated_at or NOW
    document = contract.semantic_view({
        "producer_hotkey": PRODUCER,
        "network": NETWORK,
        "netuid": NETUID,
        "source_epoch": epoch,
        "generated_at": _iso(generated),
        "complete": bool(complete),
        "score_units": UNITS,
        "scores": scores,
        "evidence_sha256": "c" * 64,
    })
    body = contract.canonical_report_bytes(document)
    digest = contract.report_digest(document)
    rid = contract.receipt_id(digest)
    stored_body_digest = (
        body_sha256 if body_sha256 is not None else hashlib.sha256(body).hexdigest()
    )
    generated_iso = _iso(generated)
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_score_reports"
        "(id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, NETWORK, NETUID, epoch, PRODUCER, complete, UNITS, len(scores),
         generated_iso, generated_iso, digest, stored_body_digest, "c" * 64,
         "sha256=" + contract.body_hmac_hex(body, SECRET),
         body.decode("utf-8"))))
    for hotkey, score in scores.items():
        store.write(lambda c, hk=hotkey, sc=score: c.execute(
            "INSERT OR REPLACE INTO cybergym_scores"
            "(report_id, miner_hotkey, epoch, score, network, netuid, "
            "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, hk, epoch, sc, NETWORK, NETUID, PRODUCER, digest,
             generated_iso, generated_iso)))
    return rid


def _registered(
    store: Store,
    mapping: dict[str, int],
    *,
    now: datetime = NOW,
    with_burn: bool = True,
) -> None:
    """Fresh metagraph rows: the eligibility gate only accepts a fresh snapshot.

    The burn hotkey is registered by default because the bridge resolves the burn
    destination through this same snapshot.
    """
    mapping = dict(mapping)
    if with_burn:
        mapping.setdefault(BURN_HOTKEY, BURN_UID)
    fresh = _iso(now - timedelta(seconds=60))
    for hotkey, uid in mapping.items():
        store.write(lambda c, hk=hotkey, u=uid: c.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (NETWORK, NETUID, hk, u, "", 123, fresh)))


def _sat_spec(fraction: float) -> MechanismSpec:
    return MechanismSpec(
        mechanism_id="sat_v2", owner_pubkey="pk_sat",
        weight_fraction=fraction, tier="artifact",
    )


def _sat_scores(vector: dict[int, float], *, signed_at_ms: int):
    return (
        vector,
        ScoreVectorMeta(
            mechanism_id="sat_v2", signed_at_ms=signed_at_ms,
            sig_ok=True, source="sat_adapter",
        ),
    )


# --- default off ---------------------------------------------------------

def test_spec_is_disabled_at_zero_fraction_by_default(monkeypatch):
    monkeypatch.delenv(bridge.MECHANISM_ENABLED_ENV, raising=False)
    monkeypatch.delenv(bridge.WEIGHT_FRACTION_ENV, raising=False)
    spec = bridge.cybergym_spec()
    assert spec.mechanism_id == "cybergym_v0"
    assert spec.enabled is False
    assert spec.weight_fraction == 0.0
    assert spec.tier == "artifact"


def test_default_off_allocates_nothing(tmp_path, monkeypatch):
    _env(monkeypatch, enabled=False, fraction=None)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _registered(store, {"5Alice": 10})
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "disabled"
    assert out["weights"] == {}
    assert out["forfeited_fraction"] == 0.0


def test_enabled_at_zero_fraction_still_allocates_nothing(tmp_path, monkeypatch):
    _env(monkeypatch, enabled=True, fraction="0")
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _registered(store, {"5Alice": 10})
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "disabled"
    assert out["weights"] == {}


@pytest.mark.parametrize("raw,expected", [
    ("", 0.0), ("nonsense", 0.0), ("-1", 0.0), ("nan", 0.0),
    # Out-of-range must fail CLOSED. Clamping up would mean "25", read as a
    # percentage, allocates the entire vector.
    ("inf", 0.0), ("2.5", 0.0), ("1e9", 0.0), ("25", 0.0),
    ("0", 0.0), ("0.25", 0.25), ("1", 1.0),
])
def test_out_of_range_fraction_fails_closed(monkeypatch, raw, expected):
    monkeypatch.setenv(bridge.WEIGHT_FRACTION_ENV, raw)
    assert bridge.weight_fraction() == expected


def test_persisted_out_of_range_fraction_is_refused(tmp_path, monkeypatch):
    """A persisted spec is validated, not trusted: the admin route that writes
    the registry does not enforce the range, so a fraction of 25.0 would
    otherwise allocate 25 times the whole vector."""
    _env(monkeypatch, enabled=False, fraction="0")

    class _MechStore:
        def get_spec(self, mechanism_id):
            return MechanismSpec(
                mechanism_id="cybergym_v0", owner_pubkey="pk",
                weight_fraction=25.0, tier="artifact", enabled=True,
            )

    spec = bridge.cybergym_spec(_MechStore())
    assert spec.weight_fraction == 0.0

    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _registered(store, {"5Alice": 10})
    out = bridge.cybergym_allocation(store, now=NOW, mech_store=_MechStore())
    assert out["status"] == "disabled"
    assert out["weights"] == {}


def test_persisted_spec_wins_over_configuration(monkeypatch):
    _env(monkeypatch, enabled=False, fraction="0")

    persisted = MechanismSpec(
        mechanism_id="cybergym_v0", owner_pubkey="pk_owner",
        weight_fraction=0.4, tier="artifact", enabled=True,
    )

    class _MechStore:
        def get_spec(self, mechanism_id):
            return persisted if mechanism_id == "cybergym_v0" else None

    resolved = bridge.cybergym_spec(_MechStore())
    assert resolved == replace(
        persisted, requires_forfeit_preservation=True, weight_fraction=0.4,
    )
    # The registry has no column for it, so the code-level requirement that this
    # lane's forfeited share must burn is always re-asserted.
    assert resolved.requires_forfeit_preservation is True
    assert resolved.weight_fraction == 0.4
    assert resolved.enabled is True


# --- the happy path -----------------------------------------------------

def test_fresh_verified_report_earns_exactly_its_fraction(tmp_path, monkeypatch):
    _env(monkeypatch, fraction="0.25")
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 3.0, "5Bob": 1.0})
    _registered(store, {"5Alice": 10, "5Bob": 20})
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "ok"
    assert math.isclose(out["weights"][10], 0.1875, abs_tol=1e-12)
    assert math.isclose(out["weights"][20], 0.0625, abs_tol=1e-12)
    assert math.isclose(sum(out["weights"].values()), 0.25, abs_tol=1e-12)
    assert out["forfeited_fraction"] == 0.0
    assert BURN_UID not in out["weights"]


# --- C5: every unprovable input burns -----------------------------------

@pytest.mark.parametrize("kind", [
    "missing", "stale", "future_dated", "unauthenticated", "empty_report",
    "incomplete", "unmapped",
])
def test_unprovable_input_burns_the_whole_share(tmp_path, monkeypatch, kind):
    _env(monkeypatch, fraction="0.25")
    store = _store(tmp_path)
    if kind == "missing":
        pass  # no report at all, registration added below
    elif kind == "stale":
        _report(
            store, epoch=1, scores={"5Alice": 5.0},
            generated_at=NOW - timedelta(days=30),
        )
        _registered(store, {"5Alice": 10})
    elif kind == "unauthenticated":
        _report(store, epoch=1, scores={"5Alice": 5.0}, body_sha256="")
        _registered(store, {"5Alice": 10})
    elif kind == "empty_report":
        _report(store, epoch=1, scores={})
        _registered(store, {"5Alice": 10})
    elif kind == "incomplete":
        _report(store, epoch=1, scores={"5Alice": 5.0}, complete=0)
        _registered(store, {"5Alice": 10})
    elif kind == "future_dated":
        _report(
            store, epoch=1, scores={"5Alice": 5.0},
            generated_at=NOW + timedelta(days=365),
        )
        _registered(store, {"5Alice": 10})
    elif kind == "unmapped":
        # Scored hotkey is not the registered one: its score maps nowhere.
        _report(store, epoch=1, scores={"5Ghost": 5.0})
        _registered(store, {"5Alice": 10})
    if kind == "missing":
        _registered(store, {"5Alice": 10})

    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "ok"
    assert math.isclose(out["forfeited_fraction"], 0.25, abs_tol=1e-12)
    assert out["burn_uid"] == BURN_UID
    assert out["weights"] == {BURN_UID: pytest.approx(0.25)}


def test_forfeited_share_never_reaches_another_lane(tmp_path, monkeypatch):
    """The core burn-correctness property with two mechanisms in one vector:
    CyberGym cannot prove its scores, SAT keeps exactly its own fraction, and
    CyberGym's share goes to burn rather than inflating SAT's miners."""
    _env(monkeypatch, fraction="0.25")
    store = _store(tmp_path)
    _registered(store, {"5Alice": 10, "5Bob": 20})
    out = bridge.cybergym_allocation(
        store,
        now=NOW,
        extra_specs=[_sat_spec(0.5)],
        extra_scores={"sat_v2": _sat_scores(
            {10: 1.0, 20: 1.0}, signed_at_ms=int(NOW.timestamp() * 1000)
        )},
    )
    assert math.isclose(out["forfeited_fraction"], 0.25, abs_tol=1e-12)
    assert math.isclose(out["contributing_fraction"], 0.5, abs_tol=1e-12)
    # SAT's own 0.5 split evenly, plus the burn share. Nothing else moved.
    assert math.isclose(out["weights"][10], 0.25, abs_tol=1e-12)
    assert math.isclose(out["weights"][20], 0.25, abs_tol=1e-12)
    assert math.isclose(out["weights"][BURN_UID], 0.25, abs_tol=1e-12)
    assert math.isclose(sum(out["weights"].values()), 0.75, abs_tol=1e-12)


def _forfeiting_allocation(store, **kwargs):
    """A composition where CyberGym forfeits and SAT contributes, so the burn
    destination is what decides whether anything is allocated."""
    return bridge.cybergym_allocation(
        store,
        now=NOW,
        extra_specs=[_sat_spec(0.5)],
        extra_scores={"sat_v2": _sat_scores(
            {10: 1.0}, signed_at_ms=int(NOW.timestamp() * 1000)
        )},
        **kwargs,
    )


# --- the burn destination must be a PROVEN identity ----------------------

@pytest.mark.parametrize("setup,reason", [
    ("no_burn_hotkey", "burn_hotkey_not_configured"),
    ("no_snapshot", "registration_snapshot_unavailable"),
    ("burn_hotkey_unregistered", "burn_hotkey_not_in_fresh_snapshot"),
    ("burn_hotkey_stale", "burn_hotkey_not_in_fresh_snapshot"),
    ("burn_hotkey_no_uid", "burn_hotkey_has_no_uid"),
    ("uid_shared_with_a_miner", "burn_uid_hotkey_mismatch"),
    ("configured_uid_disagrees", "burn_uid_configuration_mismatch"),
])
def test_unproven_burn_identity_allocates_nothing(tmp_path, monkeypatch, setup, reason):
    """A numeric UID is not an identity. UIDs are recycled when miners
    deregister, so the share is only ever sent to a UID that is currently proven
    to belong to the configured burn hotkey."""
    if setup == "no_burn_hotkey":
        _env(monkeypatch, burn_hotkey=None)
    elif setup == "configured_uid_disagrees":
        _env(monkeypatch, burn_uid="999")
    else:
        _env(monkeypatch)
    store = _store(tmp_path)
    if setup == "no_snapshot":
        pass  # no metagraph rows at all
    elif setup == "burn_hotkey_unregistered":
        _registered(store, {"5Alice": 10}, with_burn=False)
    elif setup == "burn_hotkey_stale":
        _registered(store, {"5Alice": 10}, with_burn=False)
        # present but far outside the freshness window
        store.write(lambda c: c.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (NETWORK, NETUID, BURN_HOTKEY, BURN_UID, "", 1,
             _iso(NOW - timedelta(days=30)))))
    elif setup == "burn_hotkey_no_uid":
        _registered(store, {"5Alice": 10}, with_burn=False)
        store.write(lambda c: c.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (NETWORK, NETUID, BURN_HOTKEY, None, "", 1,
             _iso(NOW - timedelta(seconds=60)))))
    elif setup == "uid_shared_with_a_miner":
        # A stale duplicate row binds the burn UID to a miner hotkey too.
        _registered(store, {"5Alice": 10, "5Recycled": BURN_UID})
    else:
        _registered(store, {"5Alice": 10})

    out = _forfeiting_allocation(store)
    assert out["status"] == "burn_destination_unresolved"
    assert out["weights"] == {}
    assert out["burn"]["reason"] == reason
    # At least CyberGym's own share is forfeited. The no-snapshot case also
    # forfeits SAT's, because the eligibility gate fails closed as well.
    assert out["forfeited_fraction"] >= 0.25


def test_burn_destination_is_reported_when_resolved(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _registered(store, {"5Alice": 10})
    out = _forfeiting_allocation(store)
    assert out["burn"]["reason"] == "ok"
    assert out["burn"]["burn_hotkey"] == BURN_HOTKEY
    assert out["burn_uid"] == BURN_UID
    assert math.isclose(out["weights"][BURN_UID], 0.25, abs_tol=1e-12)


def test_matching_configured_burn_uid_is_accepted(tmp_path, monkeypatch):
    _env(monkeypatch, burn_uid=str(BURN_UID))
    store = _store(tmp_path)
    _registered(store, {"5Alice": 10})
    out = _forfeiting_allocation(store)
    assert out["status"] == "ok"
    assert out["burn_uid"] == BURN_UID


def test_router_staleness_gate_also_burns(tmp_path, monkeypatch):
    """Even if the adapter's own age gate were widened, a caller-supplied
    max_score_age_ms still forfeits the share to burn."""
    _env(monkeypatch, fraction="0.25")
    monkeypatch.setenv("CATHEDRAL_CYBERGYM_MAX_SCORE_AGE_SECS", "86400")
    store = _store(tmp_path)
    _report(
        store, epoch=1, scores={"5Alice": 5.0},
        generated_at=NOW - timedelta(hours=5),
    )
    _registered(store, {"5Alice": 10})
    out = bridge.cybergym_allocation(store, now=NOW, max_score_age_ms=60_000)
    assert out["debug"]["mechanisms"]["cybergym_v0"]["fallback_reason"] == "stale"
    assert out["weights"] == {BURN_UID: pytest.approx(0.25)}


# --- read-only guardrail -------------------------------------------------

def test_allocation_never_writes(tmp_path, monkeypatch):
    _env(monkeypatch, fraction="0.25")
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _registered(store, {"5Alice": 10})

    def _snapshot():
        return {
            table: [tuple(r) for r in store.query(f"SELECT * FROM {table}")]
            for table in (
                "cybergym_score_reports", "cybergym_scores",
                "metagraph_hotkeys", "signed_weight_vectors",
                "weight_policy_state",
            )
        }

    before = _snapshot()
    bridge.cybergym_allocation(store, now=NOW)
    assert _snapshot() == before


# --- the legacy renormalizing path cannot activate this mechanism --------

def test_spec_declares_that_forfeiture_must_be_preserved(monkeypatch):
    _env(monkeypatch)
    assert bridge.cybergym_spec().requires_forfeit_preservation is True


def test_legacy_compose_refuses_to_activate_the_mechanism(monkeypatch):
    """Composing this lane without preserve_forfeited is a configuration error,
    not a degraded mode: it would reallocate the forfeited share."""
    _env(monkeypatch)
    spec = bridge.cybergym_spec()
    with pytest.raises(mechanism_router.ForfeitPreservationRequired) as exc:
        mechanism_router.compose(
            [spec, _sat_spec(0.5)],
            {"sat_v2": _sat_scores({10: 1.0}, signed_at_ms=int(NOW.timestamp() * 1000))},
            registered_uids={10},
            now_ms=int(NOW.timestamp() * 1000),
        )
    assert "cybergym_v0" in str(exc.value)


def test_legacy_compose_eligible_refuses_too(tmp_path, monkeypatch):
    """The sanctioned wrapper is not a way around it either."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _registered(store, {"5Alice": 10})
    with pytest.raises(mechanism_router.ForfeitPreservationRequired):
        mechanism_eligibility.compose_eligible(
            store,
            [bridge.cybergym_spec()],
            {},
            now_ms=int(NOW.timestamp() * 1000),
            now=NOW,
        )


def test_a_disabled_or_zero_fraction_spec_does_not_trip_the_refusal(monkeypatch):
    """The refusal is about ACTIVATION. A default-off spec composes anywhere,
    because it contributes and forfeits nothing."""
    _env(monkeypatch, enabled=False, fraction="0")
    weights_out, dbg = mechanism_router.compose(
        [bridge.cybergym_spec(), _sat_spec(1.0)],
        {"sat_v2": _sat_scores({10: 1.0}, signed_at_ms=int(NOW.timestamp() * 1000))},
        registered_uids={10},
        now_ms=int(NOW.timestamp() * 1000),
    )
    assert weights_out == {10: 1.0}
    assert dbg["mechanisms"]["cybergym_v0"]["contributing"] is False


def test_bridge_composition_satisfies_the_requirement(tmp_path, monkeypatch):
    _env(monkeypatch, fraction="0.25")
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _registered(store, {"5Alice": 10})
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["debug"]["preserve_forfeited"] is True
    assert out["status"] == "ok"
