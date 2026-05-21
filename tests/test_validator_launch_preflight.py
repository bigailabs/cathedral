from __future__ import annotations

from cathedral.config import ValidatorSettings
from cathedral.validator.launch_preflight import run_validator_sat_launch_preflight


def _key() -> str:
    return "11" * 32


def _settings(
    *,
    remote_enabled: bool = True,
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
            "remote_weight_source": {
                "enabled": remote_enabled,
                "url": "https://api.cathedral.computer",
                "key_id": "cathedral-weight-policy",
                "public_key_env": "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX",
            },
        }
    )


def test_validator_sat_launch_preflight_accepts_remote_weight_config() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(),
        env={
            "CATHEDRAL_PUBLIC_KEY_HEX": _key(),
            "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX": _key(),
        },
    )

    assert result.ok
    assert result.errors == ()
    assert result.details["remote_weight_source_enabled"] is True
    assert result.details["local_sat_weight"] == 0.0


def test_validator_sat_launch_preflight_rejects_placeholders_and_missing_keys() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(remote_enabled=False, hotkey="REPLACE_ME"),
        env={},
    )

    assert not result.ok
    assert "network.validator_hotkey is still REPLACE_ME" in result.errors
    assert "CATHEDRAL_PUBLIC_KEY_HEX is required for signed eval pulls" in result.errors
    assert "remote_weight_source.enabled must be true before mainnet SAT weight" in result.errors


def test_validator_sat_launch_preflight_rejects_nonzero_local_sat_weight() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(local_sat_weight=0.05),
        env={
            "CATHEDRAL_PUBLIC_KEY_HEX": _key(),
            "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX": _key(),
        },
    )

    assert not result.ok
    assert (
        "weights.task_family_weights.synthetic_boolean_v1 must stay 0.0 "
        "for remote-weight launch"
    ) in result.errors


def test_validator_sat_launch_preflight_can_shadow_without_remote_opt_in() -> None:
    result = run_validator_sat_launch_preflight(
        _settings(remote_enabled=False),
        env={"CATHEDRAL_PUBLIC_KEY_HEX": _key()},
        require_remote_weight_source=False,
    )

    assert result.ok
    assert result.errors == ()
