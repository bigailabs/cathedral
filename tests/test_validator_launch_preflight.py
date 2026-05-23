from __future__ import annotations

from cathedral.config import ValidatorSettings
from cathedral.validator.launch_preflight import run_validator_sat_launch_preflight


def _key() -> str:
    return "11" * 32


def _settings(
    *,
    local_sat_weight: float = 0.0,
    hotkey: str = "operator-hotkey",
) -> ValidatorSettings:
    return ValidatorSettings.model_validate(
        {
            "network": {
                "name": "finney",
                "netuid": 39,
                "validator_hotkey": hotkey,
                "wallet_name": "cathedral-validator",
            },
            "polaris": {
                "base_url": "https://api.polaris.computer/",
                "public_key_hex": _key(),
            },
            "weights": {
                "interval_secs": 1500,
                "disabled": False,
                "burn_uid": 204,
                "forced_burn_percentage": 95.0,
                "task_family_weights": {"synthetic_boolean_v1": local_sat_weight},
            },
            "publisher": {
                "url": "https://api.cathedral.computer",
                "public_key_env": "CATHEDRAL_PUBLIC_KEY_HEX",
            },
        }
    )


def test_validator_sat_launch_preflight_accepts_local_shadow_config() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(),
        env={"CATHEDRAL_PUBLIC_KEY_HEX": _key()},
    )

    assert result.ok
    assert result.errors == ()
    assert result.details["local_sat_weight"] == 0.0


def test_validator_sat_launch_preflight_rejects_placeholders_and_missing_keys() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(hotkey="REPLACE_ME"),
        env={},
    )

    assert not result.ok
    assert "network.validator_hotkey is still REPLACE_ME" in result.errors
    assert "CATHEDRAL_PUBLIC_KEY_HEX is required for signed eval pulls" in result.errors


def test_validator_sat_launch_preflight_rejects_nonzero_local_sat_weight() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(local_sat_weight=0.05),
        env={"CATHEDRAL_PUBLIC_KEY_HEX": _key()},
    )

    assert not result.ok
    assert (
        "weights.task_family_weights.synthetic_boolean_v1 must stay 0.0 "
        "for shadow launch"
    ) in result.errors


def test_validator_sat_launch_preflight_honors_sat_weight_env_override() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(local_sat_weight=0.0),
        env={
            "CATHEDRAL_PUBLIC_KEY_HEX": _key(),
            "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT": "0.15",
        },
    )

    assert not result.ok
    assert result.details["local_sat_weight"] == 0.15
    assert (
        "weights.task_family_weights.synthetic_boolean_v1 must stay 0.0 "
        "for shadow launch"
    ) in result.errors


def test_validator_sat_launch_preflight_honors_task_family_weights_json_env() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(local_sat_weight=0.0),
        env={
            "CATHEDRAL_PUBLIC_KEY_HEX": _key(),
            "CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON": '{"synthetic_boolean_v1": 0.2}',
        },
    )

    assert not result.ok
    assert result.details["local_sat_weight"] == 0.2
    assert result.details["task_family_weights"]["synthetic_boolean_v1"] == 0.2
    assert (
        "weights.task_family_weights.synthetic_boolean_v1 must stay 0.0 "
        "for shadow launch"
    ) in result.errors


def test_validator_sat_launch_preflight_can_allow_local_sat_weight() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(local_sat_weight=0.2),
        env={"CATHEDRAL_PUBLIC_KEY_HEX": _key()},
        require_zero_local_sat_weight=False,
    )

    assert result.ok
    assert result.errors == ()
    assert (
        "synthetic_boolean_v1 has local nonzero weight and will affect local set_weights"
        in result.warnings
    )
