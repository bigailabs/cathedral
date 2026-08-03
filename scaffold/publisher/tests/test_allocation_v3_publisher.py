"""Publisher-side contract tests for the v3 (70/30/0) allocation policy.

Covers the NEW publisher logic:
  * ``allocation_contract()`` env selection and fail-closed on an unknown value;
  * ``validated_supply_metadata()`` v2 default (byte-identical) and v3 opt-in,
    including the fail-closed guards (0% fixed burn for v3, explicit burn hotkey,
    confidential_primary mode, no pinned burn UID);
  * ``_compose_cybergym_lane_v3()`` composition and fail-closed behavior.
"""
from __future__ import annotations

import math

import pytest

from scaffold.publisher import weights
from scaffold.wire_vector import VectorError


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # validated_supply requires confidential_primary mode, an explicit burn
    # hotkey, and NO pinned burn uid (resolved by hotkey).
    monkeypatch.setenv("CATHEDRAL_VALIDATED_SUPPLY_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_SOURCE", "cathedral_confidential_tdx"
    )
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY", "burn-hotkey")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_UID", "")


# ---- allocation_contract() ---------------------------------------------------


def test_allocation_contract_defaults_to_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATHEDRAL_ALLOCATION_CONTRACT", raising=False)
    assert weights.allocation_contract() == "v2"


def test_allocation_contract_selects_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    assert weights.allocation_contract() == "v3"


def test_allocation_contract_unknown_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v4")
    with pytest.raises(VectorError, match="unknown allocation contract"):
        weights.allocation_contract()


# ---- validated_supply_metadata() v2 default (unchanged) ----------------------


def test_v2_default_is_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATHEDRAL_ALLOCATION_CONTRACT", raising=False)
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "10")
    assert weights.validated_supply_metadata() == {
        "contract_version": "v2",
        "intel_tdx_allocation": 0.90,
        "fixed_burn_allocation": 0.10,
        "burn_hotkey": "burn-hotkey",
    }


def test_v2_requires_ten_percent_burn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v2")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "0")
    with pytest.raises(VectorError, match="exactly 10% forced burn"):
        weights.validated_supply_metadata()


def test_disabled_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_VALIDATED_SUPPLY_ENABLED", "0")
    assert weights.validated_supply_metadata() is None


# ---- validated_supply_metadata() v3 -----------------------------------------


def test_v3_metadata_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "0")
    assert weights.validated_supply_metadata() == {
        "contract_version": "v3",
        "intel_tdx_allocation": 0.70,
        "cybergym_allocation": 0.30,
        "fixed_burn_allocation": 0.0,
        "burn_hotkey": "burn-hotkey",
    }


def test_v3_allocations_sum_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "0")
    meta = weights.validated_supply_metadata()
    total = (
        meta["intel_tdx_allocation"]
        + meta["cybergym_allocation"]
        + meta["fixed_burn_allocation"]
    )
    assert math.isclose(total, 1.0, abs_tol=1e-12)


def test_v3_requires_zero_fixed_burn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "10")
    with pytest.raises(VectorError, match="v3 requires exactly 0% fixed burn"):
        weights.validated_supply_metadata()


def test_v3_still_requires_explicit_burn_hotkey(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "0")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY", "")
    with pytest.raises(VectorError, match="explicit burn hotkey"):
        weights.validated_supply_metadata()


def test_v3_requires_confidential_primary_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "blend")
    with pytest.raises(VectorError, match="confidential_primary mode"):
        weights.validated_supply_metadata()


def test_v3_rejects_pinned_burn_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATHEDRAL_ALLOCATION_CONTRACT", "v3")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE_V2", "0")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_BURN_UID", "204")
    with pytest.raises(VectorError, match="resolve burn by hotkey, not UID"):
        weights.validated_supply_metadata()


# ---- _compose_cybergym_lane_v3() --------------------------------------------


def _enable_cybergym(monkeypatch: pytest.MonkeyPatch, *, fraction: str = "0.30") -> None:
    monkeypatch.setenv("CATHEDRAL_CYBERGYM_MECHANISM_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_CYBERGYM_WEIGHT_FRACTION", fraction)


def test_cybergym_lane_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from scaffold.publisher import cybergym_bridge

    _enable_cybergym(monkeypatch)
    monkeypatch.setattr(
        cybergym_bridge,
        "cybergym_allocation",
        lambda store, *, now=None: {
            "status": "ok",
            "weights": {50: 0.18, 51: 0.12},
            "forfeited_fraction": 0.0,
            "contributing_fraction": 0.30,
            "burn_uid": None,
            "cybergym": {"reason": "ok"},
        },
    )
    lane = weights._compose_cybergym_lane_v3(object(), now=None)
    assert lane["fraction"] == 0.30
    assert math.isclose(sum(lane["weights"].values()), 0.30, abs_tol=1e-12)
    assert lane["weights"] == {"50": 0.18, "51": 0.12}
    assert lane["burn_uid"] is None


def test_cybergym_lane_forfeit_to_burn(monkeypatch: pytest.MonkeyPatch) -> None:
    from scaffold.publisher import cybergym_bridge

    _enable_cybergym(monkeypatch)
    monkeypatch.setattr(
        cybergym_bridge,
        "cybergym_allocation",
        lambda store, *, now=None: {
            "status": "ok",
            "weights": {7: 0.30},
            "forfeited_fraction": 0.30,
            "contributing_fraction": 0.0,
            "burn_uid": 7,
            "cybergym": {"reason": "stale"},
        },
    )
    lane = weights._compose_cybergym_lane_v3(object(), now=None)
    assert lane["forfeited_fraction"] == 0.30
    assert lane["burn_uid"] == 7
    assert lane["weights"] == {"7": 0.30}


def test_cybergym_lane_disabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CATHEDRAL_CYBERGYM_MECHANISM_ENABLED", raising=False)
    with pytest.raises(VectorError, match="requires the CyberGym mechanism enabled"):
        weights._compose_cybergym_lane_v3(object(), now=None)


def test_cybergym_lane_wrong_fraction_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_cybergym(monkeypatch, fraction="0.10")
    with pytest.raises(VectorError, match="requires CyberGym weight fraction 0.3"):
        weights._compose_cybergym_lane_v3(object(), now=None)


def test_cybergym_lane_unresolved_burn_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scaffold.publisher import cybergym_bridge

    _enable_cybergym(monkeypatch)
    monkeypatch.setattr(
        cybergym_bridge,
        "cybergym_allocation",
        lambda store, *, now=None: {
            "status": "burn_destination_unresolved",
            "weights": {},
            "forfeited_fraction": 0.30,
            "contributing_fraction": 0.0,
            "burn_uid": None,
            "cybergym": {"reason": "ok"},
            "burn": {"reason": "registration_snapshot_unavailable"},
        },
    )
    with pytest.raises(VectorError, match="not composable"):
        weights._compose_cybergym_lane_v3(object(), now=None)
