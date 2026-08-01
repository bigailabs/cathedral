"""The pre-cutover gate must refuse exactly what the validator refuses.

cathedral#400: the live publisher signed `validated_supply` contract v1 while the
launch-locked validator required v2. Upgrading the validator without the
publisher in the same window would have burned everything from the first tick,
and the cutover sequence had no step that would have caught it.

The publisher has since been upgraded out of band, which is precisely why this
gate is worth having: nothing today would notice a regression back to v1 until
miners stopped being paid.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _gate():
    spec = importlib.util.spec_from_file_location(
        "assert_live_weight_contract",
        REPO_ROOT / "scripts" / "assert_live_weight_contract.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _v2_payload():
    return {
        "network": "finney",
        "netuid": 39,
        "policy_metadata": {
            "validated_supply": {
                "contract_version": "v2",
                "intel_tdx_allocation": 0.9,
                "fixed_burn_allocation": 0.1,
                "burn_hotkey": "5Burn",
            }
        },
        "burn_snapshot": {
            "burn_hotkey": "5Burn",
            "burn_uid": None,
            "forced_burn_percentage": 10.0,
        },
    }


def _v1_payload():
    """The exact shape #400 found live: v1, with the GPU fields v2 removed."""
    payload = _v2_payload()
    payload["policy_metadata"]["validated_supply"] = {
        "contract_version": "v1",
        "intel_tdx_allocation": 0.9,
        "verified_gpu_allocation": 0.1,
        "verified_gpu_admitted": False,
        "burn_hotkey": "5Burn",
    }
    return payload


def test_a_v2_feed_passes(monkeypatch, capsys):
    gate = _gate()
    monkeypatch.setattr(gate, "fetch_vector", lambda publisher: _v2_payload())
    assert gate.main(["--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["contract_version"] == "v2"


def test_a_v1_feed_is_refused_and_says_do_not_cut_over(monkeypatch, capsys):
    """The whole point. A v1 feed must stop the cutover, loudly."""
    gate = _gate()
    monkeypatch.setattr(gate, "fetch_vector", lambda publisher: _v1_payload())
    assert gate.main([]) == 1
    err = capsys.readouterr().err
    assert "DO NOT CUT OVER" in err
    assert "100% burn" in err


def test_the_gate_refuses_whatever_the_validator_refuses(monkeypatch):
    """It must not drift from the validator's own contract.

    A second copy of a contract is a second thing to keep in sync, and the
    failure mode of drift is that the gate says yes and the validator says no.
    These mutations each break a DIFFERENT clause of _validated_supply_meta;
    every one must be refused by the gate too.
    """
    from scaffold import validator_thin
    from scaffold import wire_vector as wire

    gate = _gate()
    mutations = {
        "wrong tdx split": lambda p: p["policy_metadata"]["validated_supply"]
            .__setitem__("intel_tdx_allocation", 0.8),
        "wrong burn split": lambda p: p["policy_metadata"]["validated_supply"]
            .__setitem__("fixed_burn_allocation", 0.2),
        "extra field": lambda p: p["policy_metadata"]["validated_supply"]
            .__setitem__("verified_gpu_allocation", 0.0),
        "burn hotkey mismatch": lambda p: p["burn_snapshot"]
            .__setitem__("burn_hotkey", "5Other"),
        "burn destination pins a uid": lambda p: p["burn_snapshot"]
            .__setitem__("burn_uid", 3),
        "burn percentage disagrees": lambda p: p["burn_snapshot"]
            .__setitem__("forced_burn_percentage", 5.0),
    }
    for label, mutate in mutations.items():
        payload = _v2_payload()
        mutate(payload)
        # the validator refuses it ...
        with pytest.raises(wire.VectorError):
            validator_thin._validated_supply_meta(payload)
        # ... so the gate must too
        monkeypatch.setattr(gate, "fetch_vector", lambda publisher, p=payload: p)
        assert gate.main([]) == 1, f"gate accepted what the validator refuses: {label}"
