"""Focused tests for the confidential_primary thin-validator path.

Coverage matrix
---------------
- positive_external_only_100pct   — mass=1, external fills all mass, sum=1.0
- base_ignored                    — base_component=0 required; nonzero raises
- disabled_mass0_to_burn          — degraded vector (mass=0) applies only burn
- missing_confirmed_raises        — mass=1 without confirmed=true raises
- missing_fresh_raises            — mass=1 without fresh=true raises
- missing_complete_raises         — mass=1 without complete=false raises
- wrong_mode_raises               — mass=1 with wrong mode raises
- zero_revocation                 — mass=0, no rows, burn applied
- missing_hotkey                  — signed hotkey absent from metagraph raises
- duplicate_uid                   — two hotkeys -> same UID raises
- malformed_contract_version      — wrong contract_version raises
- malformed_source                — wrong source raises
- malformed_base_mass             — base_mass != 0 raises
- malformed_confidential_mass     — mass not in {0,1} raises
- malformed_complete_not_bool     — complete not bool raises
- missing_row_base_component      — row without base_component raises
- missing_row_external_component  — row without external_component raises
- nonzero_base_component          — row base_component != 0 raises
- weight_external_mismatch        — weight != external_component raises
- sum_drift                       — weight mass != 1.0 raises
- burn_fallback                   — burn applied after successful mapping
- regression_v3_10pct             — existing v3 cap path still works
- regression_legacy               — legacy (no metadata) path still works
"""
from __future__ import annotations

import math

import pytest

from scaffold import validator_thin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp_meta(
    *,
    contract_version: str = "v1",
    source: str = "cathedral_confidential_tdx",
    base_mass: float = 0.0,
    confidential_mass: float = 1.0,
    complete: object = True,
    fresh: object = True,
    confirmed: object = True,
    mode: str = "confidential_primary",
) -> dict:
    meta: dict = {
        "contract_version": contract_version,
        "source": source,
        "base_mass": base_mass,
        "confidential_mass": confidential_mass,
        "complete": complete,
        "fresh": fresh,
        "confirmed": confirmed,
        "mode": mode,
    }
    return meta


def _cp_row(hotkey: str, weight: float) -> dict:
    """A well-formed confidential_primary row: base=0, external=weight."""
    return {
        "miner_hotkey": hotkey,
        "weight": weight,
        "base_component": 0.0,
        "external_component": weight,
    }


def _cp_payload(
    rows: list[dict],
    *,
    burn_uid: int | None = None,
    forced_burn_percentage: float = 0.0,
    cp_meta: dict | None = None,
) -> dict:
    if cp_meta is None:
        cp_meta = _cp_meta()
    return {
        "weights": rows,
        "burn_snapshot": {
            "burn_uid": burn_uid,
            "forced_burn_percentage": forced_burn_percentage,
        },
        "policy_metadata": {
            "confidential_primary": cp_meta,
        },
    }


# ---------------------------------------------------------------------------
# Positive path — external only, 100%
# ---------------------------------------------------------------------------

def test_positive_external_only_100pct() -> None:
    """Two miners, weights sum to 1.0, maps correctly with no burn."""
    rows = [_cp_row("hk_a", 0.6), _cp_row("hk_b", 0.4)]
    payload = _cp_payload(rows)
    result = validator_thin.vector_to_uid_weights(payload, {"hk_a": 10, "hk_b": 20})
    assert result == {10: 0.6, 20: 0.4}


def test_base_component_must_be_zero_nonzero_raises() -> None:
    """A row with base_component != 0 is always rejected."""
    row = {"miner_hotkey": "hk", "weight": 0.5,
           "base_component": 0.1, "external_component": 0.5}
    payload = _cp_payload([row, _cp_row("hk2", 0.5)])
    with pytest.raises(validator_thin.wire.VectorError, match="base_component must be 0"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1, "hk2": 2})


def test_weight_equals_external_component() -> None:
    """external_component must equal weight to within 1e-12."""
    row = {"miner_hotkey": "hk", "weight": 0.4,
           "base_component": 0.0, "external_component": 0.6}
    payload = _cp_payload([row, _cp_row("hk2", 0.6)])
    with pytest.raises(validator_thin.wire.VectorError, match="weight != external_component"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1, "hk2": 2})


# ---------------------------------------------------------------------------
# Disabled / missing / unconfirmed → empty (signed burn only)
# ---------------------------------------------------------------------------

def test_disabled_mass0_applies_burn_only() -> None:
    """Degraded vector (confidential_mass=0, no positive rows) → just burn."""
    payload = _cp_payload([], cp_meta=_cp_meta(confidential_mass=0.0),
                          burn_uid=99, forced_burn_percentage=5.0)
    result = validator_thin.vector_to_uid_weights(payload, {"hk": 10})
    # No positive weights → burn UID gets all the mass
    assert result == {99: 1.0}


def test_unconfirmed_mass1_raises() -> None:
    """mass=1 vector without confirmed=true is rejected."""
    payload = _cp_payload(
        [_cp_row("hk", 1.0)],
        cp_meta=_cp_meta(confirmed=False),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="confirmed=true"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_missing_confirmed_mass1_raises() -> None:
    """mass=1 vector with confirmed absent is rejected."""
    meta = _cp_meta(confirmed=False)
    del meta["confirmed"]  # explicitly absent
    payload = _cp_payload([_cp_row("hk", 1.0)], cp_meta=meta)
    with pytest.raises(validator_thin.wire.VectorError, match="confirmed=true"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_not_fresh_mass1_raises() -> None:
    """mass=1 vector with fresh=false is rejected."""
    payload = _cp_payload(
        [_cp_row("hk", 1.0)],
        cp_meta=_cp_meta(fresh=False),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="fresh=true"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_not_complete_mass1_raises() -> None:
    """mass=1 vector with complete=false is rejected."""
    payload = _cp_payload(
        [_cp_row("hk", 1.0)],
        cp_meta=_cp_meta(complete=False),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="complete=true"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_wrong_mode_mass1_raises() -> None:
    """mass=1 vector with mode != confidential_primary is rejected."""
    payload = _cp_payload(
        [_cp_row("hk", 1.0)],
        cp_meta=_cp_meta(mode="blend"),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="mode=confidential_primary"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


# ---------------------------------------------------------------------------
# Zero revocation
# ---------------------------------------------------------------------------

def test_zero_revocation_mass0_no_burn_uid_raises() -> None:
    """All-miners revoke with no burn_uid configured: fail closed (VectorError)."""
    payload = _cp_payload([], cp_meta=_cp_meta(confidential_mass=0.0))
    with pytest.raises(validator_thin.wire.VectorError,
                       match="no miner mass and no burn_uid fallback"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 5})


def test_zero_revocation_mass0_with_burn_uid() -> None:
    """All-miners revoke with burn_uid configured: all mass routes to burn."""
    payload = _cp_payload([], cp_meta=_cp_meta(confidential_mass=0.0),
                          burn_uid=99, forced_burn_percentage=5.0)
    result = validator_thin.vector_to_uid_weights(payload, {"hk": 5})
    # No positive miners, entire mass to burn_uid
    assert result == {99: 1.0}


def test_zero_revocation_mass1_no_positive_rows_raises() -> None:
    """mass=1 with zero positive rows is a contract violation."""
    payload = _cp_payload([], cp_meta=_cp_meta(confidential_mass=1.0))
    with pytest.raises(validator_thin.wire.VectorError,
                       match="claims mass 1 but has no positive weight"):
        validator_thin.vector_to_uid_weights(payload, {})


# ---------------------------------------------------------------------------
# Missing hotkey
# ---------------------------------------------------------------------------

def test_missing_hotkey_raises() -> None:
    """A signed hotkey absent from the metagraph is an all-or-nothing failure."""
    rows = [_cp_row("known", 0.7), _cp_row("missing", 0.3)]
    payload = _cp_payload(rows)
    with pytest.raises(validator_thin.wire.VectorError,
                       match="has no current metagraph UID"):
        validator_thin.vector_to_uid_weights(payload, {"known": 1})


# ---------------------------------------------------------------------------
# Duplicate UID
# ---------------------------------------------------------------------------

def test_duplicate_uid_raises() -> None:
    """Two hotkeys that map to the same UID are rejected."""
    rows = [_cp_row("hk_a", 0.6), _cp_row("hk_b", 0.4)]
    payload = _cp_payload(rows)
    with pytest.raises(validator_thin.wire.VectorError, match="duplicate UID"):
        validator_thin.vector_to_uid_weights(payload, {"hk_a": 5, "hk_b": 5})


# ---------------------------------------------------------------------------
# Malformed contract flags / components / sums
# ---------------------------------------------------------------------------

def test_malformed_contract_version_raises() -> None:
    payload = _cp_payload([_cp_row("hk", 1.0)],
                          cp_meta=_cp_meta(contract_version="v2"))
    with pytest.raises(validator_thin.wire.VectorError, match="contract_version"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_malformed_source_raises() -> None:
    payload = _cp_payload([_cp_row("hk", 1.0)],
                          cp_meta=_cp_meta(source="violet_audio"))
    with pytest.raises(validator_thin.wire.VectorError, match="invalid source"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_malformed_base_mass_nonzero_raises() -> None:
    payload = _cp_payload([_cp_row("hk", 1.0)],
                          cp_meta=_cp_meta(base_mass=0.1))
    with pytest.raises(validator_thin.wire.VectorError, match="base_mass must be 0"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


@pytest.mark.parametrize("mass", [0.5, -1.0, 2.0, math.nan])
def test_malformed_confidential_mass_raises(mass: float) -> None:
    meta = _cp_meta(confidential_mass=mass)
    # mass=1 enforcement checks happen only for exact 1.0; these will hit the
    # "must be 0 or 1" guard first.
    payload = _cp_payload([_cp_row("hk", 1.0)], cp_meta=meta)
    with pytest.raises(validator_thin.wire.VectorError,
                       match="confidential_mass must be 0 or 1"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_malformed_complete_not_bool_raises() -> None:
    payload = _cp_payload([_cp_row("hk", 1.0)],
                          cp_meta=_cp_meta(complete="yes"))
    with pytest.raises(validator_thin.wire.VectorError, match="complete flag must be a bool"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_missing_row_base_component_raises() -> None:
    """Rows in a confidential_primary vector must carry base_component explicitly."""
    row = {"miner_hotkey": "hk", "weight": 1.0, "external_component": 1.0}
    payload = _cp_payload([row])
    with pytest.raises(validator_thin.wire.VectorError,
                       match="base_component and external_component"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_missing_row_external_component_raises() -> None:
    """Rows in a confidential_primary vector must carry external_component explicitly."""
    row = {"miner_hotkey": "hk", "weight": 1.0, "base_component": 0.0}
    payload = _cp_payload([row])
    with pytest.raises(validator_thin.wire.VectorError,
                       match="base_component and external_component"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


def test_sum_drift_raises() -> None:
    """Rows summing to != 1.0 when mass=1 is a contract violation."""
    rows = [_cp_row("hk_a", 0.3), _cp_row("hk_b", 0.3)]  # sum=0.6, not 1.0
    payload = _cp_payload(rows)
    with pytest.raises(validator_thin.wire.VectorError, match="weight mass"):
        validator_thin.vector_to_uid_weights(payload, {"hk_a": 1, "hk_b": 2})


def test_duplicate_signed_hotkey_raises() -> None:
    rows = [_cp_row("same", 0.5), _cp_row("same", 0.5)]
    payload = _cp_payload(rows)
    with pytest.raises(validator_thin.wire.VectorError, match="duplicate hotkey"):
        validator_thin.vector_to_uid_weights(payload, {"same": 1})


def test_nonfinite_weight_raises() -> None:
    row = {"miner_hotkey": "hk", "weight": math.nan,
           "base_component": 0.0, "external_component": math.nan}
    payload = _cp_payload([row])
    with pytest.raises(validator_thin.wire.VectorError, match="non-finite or negative"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1})


# ---------------------------------------------------------------------------
# Burn fallback
# ---------------------------------------------------------------------------

def test_burn_applied_after_successful_mapping() -> None:
    """Burn UID receives forced percentage of mass; positive miners share the rest."""
    rows = [_cp_row("hk_a", 0.7), _cp_row("hk_b", 0.3)]
    payload = _cp_payload(rows, burn_uid=99, forced_burn_percentage=10.0)
    result = validator_thin.vector_to_uid_weights(payload, {"hk_a": 1, "hk_b": 2})
    assert result[99] == pytest.approx(0.1, abs=1e-9)
    assert result[1] == pytest.approx(0.63, abs=1e-9)
    assert result[2] == pytest.approx(0.27, abs=1e-9)


def test_burn_fallback_mass0_vector() -> None:
    """Degraded (mass=0) vector with a burn UID routes all mass to burn."""
    payload = _cp_payload([], cp_meta=_cp_meta(confidential_mass=0.0),
                          burn_uid=99, forced_burn_percentage=100.0)
    result = validator_thin.vector_to_uid_weights(payload, {})
    assert result == {99: 1.0}


# ---------------------------------------------------------------------------
# Regression — existing 10% cap (v3) and legacy paths still work
# ---------------------------------------------------------------------------

def _v3_row(hotkey: str, base: float, external: float) -> dict:
    return {
        "miner_hotkey": hotkey,
        "weight": base + external,
        "base_component": base,
        "external_component": external,
    }


def _v3_payload(rows: list[dict], *, configured_fraction: object = 0.10) -> dict:
    cap: dict = {"cap_version": "v3"}
    if configured_fraction is not None:
        cap["configured_fraction"] = configured_fraction
    return {
        "weights": rows,
        "burn_snapshot": {"burn_uid": None, "forced_burn_percentage": 0.0},
        "policy_metadata": {"confidential_tdx_cap": cap},
    }


def test_regression_v3_10pct_blend_still_works() -> None:
    """confidential_primary path must not interfere with existing v3 cap vectors."""
    payload = _v3_payload([
        _v3_row("base", 0.45, 0.0),
        _v3_row("overlap", 0.45, 0.05),
        _v3_row("compute", 0.0, 0.05),
    ])
    result = validator_thin.vector_to_uid_weights(
        payload, {"base": 10, "overlap": 11, "compute": 12}
    )
    assert result == {10: 0.45, 11: 0.5, 12: 0.05}


def test_regression_default_legacy_vector_still_works() -> None:
    """Legacy vectors with no policy_metadata still map via the old path."""
    payload = {
        "weights": [
            {"miner_hotkey": "first", "weight": 0.6},
            {"miner_hotkey": "second", "weight": 0.4},
        ],
        "burn_snapshot": {"burn_uid": None, "forced_burn_percentage": 0.0},
    }
    result = validator_thin.vector_to_uid_weights(payload, {"first": 0, "second": 1})
    assert result == {0: 0.6, 1: 0.4}


# ---------------------------------------------------------------------------
# Blocker 1 — validator policy pin (confidential_primary_v1)
# ---------------------------------------------------------------------------

PIN = validator_thin.REQUIRE_POLICY_CONFIDENTIAL_PRIMARY_V1


def test_pin_accepts_valid_confidential_primary_vector() -> None:
    """A pinned validator applies a correctly signed confidential_primary vector."""
    rows = [_cp_row("hk_a", 0.6), _cp_row("hk_b", 0.4)]
    payload = _cp_payload(rows)
    result = validator_thin.vector_to_uid_weights(
        payload, {"hk_a": 10, "hk_b": 20}, require_policy=PIN)
    assert result == {10: 0.6, 20: 0.4}


def test_pin_rejects_legacy_vector() -> None:
    """A pinned validator rejects a correctly shaped legacy vector (no metadata)."""
    payload = {
        "weights": [
            {"miner_hotkey": "first", "weight": 0.6},
            {"miner_hotkey": "second", "weight": 0.4},
        ],
        "burn_snapshot": {"burn_uid": None, "forced_burn_percentage": 0.0},
    }
    with pytest.raises(validator_thin.wire.VectorError,
                       match="no confidential_primary policy block"):
        validator_thin.vector_to_uid_weights(
            payload, {"first": 0, "second": 1}, require_policy=PIN)


def test_pin_rejects_v3_vector() -> None:
    """A pinned validator rejects a correctly signed v3 (10% cap) vector."""
    payload = _v3_payload([
        _v3_row("base", 0.45, 0.0),
        _v3_row("overlap", 0.45, 0.05),
        _v3_row("compute", 0.0, 0.05),
    ])
    with pytest.raises(validator_thin.wire.VectorError,
                       match="no confidential_primary policy block"):
        validator_thin.vector_to_uid_weights(
            payload, {"base": 10, "overlap": 11, "compute": 12}, require_policy=PIN)


def test_pin_still_enforces_malformed_primary_block() -> None:
    """A pinned validator rejects a present-but-invalid confidential_primary block."""
    payload = _cp_payload([_cp_row("hk", 1.0)],
                          cp_meta=_cp_meta(contract_version="v2"))
    with pytest.raises(validator_thin.wire.VectorError, match="contract_version"):
        validator_thin.vector_to_uid_weights(payload, {"hk": 1}, require_policy=PIN)


def test_unpinned_still_accepts_legacy_and_v3() -> None:
    """Without the pin, legacy and v3 vectors still map (default behavior)."""
    legacy = {
        "weights": [{"miner_hotkey": "first", "weight": 1.0}],
        "burn_snapshot": {"burn_uid": None, "forced_burn_percentage": 0.0},
    }
    assert validator_thin.vector_to_uid_weights(legacy, {"first": 0}) == {0: 1.0}
    assert validator_thin.vector_to_uid_weights(
        legacy, {"first": 0}, require_policy=None) == {0: 1.0}
    v3 = _v3_payload([_v3_row("base", 0.9, 0.0), _v3_row("compute", 0.0, 0.1)])
    result = validator_thin.vector_to_uid_weights(v3, {"base": 1, "compute": 2})
    assert result == {1: 0.9, 2: 0.1}


def test_pin_choice_constant() -> None:
    """The legacy confidential-primary pin remains supported."""
    assert PIN == "confidential_primary_v1"
    assert PIN in validator_thin.REQUIRE_POLICY_CHOICES
