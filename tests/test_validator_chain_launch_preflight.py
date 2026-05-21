from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cathedral.chain import Metagraph, MinerNode
from cathedral.cli.validator import app as validator_app
from cathedral.config import ValidatorSettings
from cathedral.validator.chain_launch_preflight import (
    run_validator_chain_launch_preflight,
)


def _settings(*, interval_secs: int = 1500, weights_disabled: bool = False) -> ValidatorSettings:
    return ValidatorSettings.model_validate(
        {
            "network": {
                "name": "finney",
                "netuid": 39,
                "validator_hotkey": "validator-hotkey-name",
                "wallet_name": "cathedral-validator",
            },
            "polaris": {
                "base_url": "https://api.polaris.computer/",
                "public_key_hex": "11" * 32,
            },
            "weights": {
                "interval_secs": interval_secs,
                "disabled": weights_disabled,
                "burn_uid": 204,
                "forced_burn_percentage": 95.0,
                "task_family_weights": {"synthetic_boolean_v1": 0.0},
            },
            "publisher": {
                "url": "https://api.cathedral.computer",
                "public_key_env": "CATHEDRAL_PUBLIC_KEY_HEX",
            },
            "remote_weight_source": {
                "enabled": True,
                "url": "https://api.cathedral.computer",
                "key_id": "cathedral-weight-policy",
                "public_key_env": "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX",
            },
        }
    )


class _FakeChain:
    def __init__(
        self,
        *,
        registered: bool = True,
        metagraph: Metagraph | None = None,
        hyperparameters: dict[str, object] | None = None,
        wallet_hotkey: str = "validator-ss58",
    ) -> None:
        self.registered = registered
        self._metagraph = metagraph or Metagraph(
            block=123,
            miners=(
                MinerNode(
                    uid=7,
                    hotkey="validator-ss58",
                    last_update_block=100,
                    validator_permit=True,
                    stake=123.45,
                ),
            ),
        )
        self._hyperparameters = hyperparameters if hyperparameters is not None else {
            "weights_rate_limit": 100,
            "commit_reveal_weights_enabled": True,
            "commit_reveal_period": 10,
            "immunity_period": 200,
            "tempo": 360,
        }
        self._wallet_hotkey = wallet_hotkey

    async def current_block(self) -> int:
        return 130

    async def is_registered(self) -> bool:
        return self.registered

    async def metagraph(self) -> Metagraph:
        return self._metagraph

    async def set_weights(self, weights):  # pragma: no cover - must stay unused
        raise AssertionError("chain preflight must not call set_weights")

    async def validator_hotkey_ss58(self) -> str:
        return self._wallet_hotkey

    async def subnet_hyperparameters(self) -> dict[str, object]:
        return self._hyperparameters


@pytest.mark.asyncio
async def test_chain_launch_preflight_accepts_registered_permitted_validator() -> None:
    result = await run_validator_chain_launch_preflight(_settings(), _FakeChain())

    assert result.ok
    assert result.errors == ()
    assert result.details["registered"] is True
    assert result.details["validator_uid"] == 7
    assert result.details["validator_permit"] is True
    assert result.details["validator_stake"] == 123.45
    assert result.details["weights_rate_limit_blocks"] == 100
    assert result.details["immunity_exceeds_commit_reveal_period"] is True


@pytest.mark.asyncio
async def test_chain_launch_preflight_rejects_unregistered_missing_hotkey() -> None:
    chain = _FakeChain(
        registered=False,
        metagraph=Metagraph(block=123, miners=()),
    )

    result = await run_validator_chain_launch_preflight(_settings(), chain)

    assert not result.ok
    assert "validator hotkey is not registered on the configured subnet" in result.errors
    assert (
        "validator hotkey was not found in the live metagraph snapshot" in result.errors
    )


@pytest.mark.asyncio
async def test_chain_launch_preflight_rejects_missing_permit_and_zero_stake() -> None:
    chain = _FakeChain(
        metagraph=Metagraph(
            block=123,
            miners=(
                MinerNode(
                    uid=7,
                    hotkey="validator-ss58",
                    last_update_block=100,
                    validator_permit=False,
                    stake=0.0,
                ),
            ),
        )
    )

    result = await run_validator_chain_launch_preflight(_settings(), chain)

    assert not result.ok
    assert "validator hotkey does not have a permit in the live metagraph" in result.errors
    assert "validator stake is zero in the live metagraph" in result.errors


@pytest.mark.asyncio
async def test_chain_launch_preflight_warns_on_live_rate_limit_estimate() -> None:
    result = await run_validator_chain_launch_preflight(
        _settings(interval_secs=600),
        _FakeChain(hyperparameters={"weights_rate_limit": 100}),
    )

    assert result.ok
    assert (
        "weights.interval_secs is below the live weights_rate_limit estimate"
        in result.warnings
    )


@pytest.mark.asyncio
async def test_chain_launch_preflight_warns_when_hyperparameters_unavailable() -> None:
    result = await run_validator_chain_launch_preflight(
        _settings(weights_disabled=True),
        _FakeChain(hyperparameters={}),
    )

    assert result.ok
    assert "subnet hyperparameters were unavailable from the Bittensor SDK" in result.warnings
    assert "weights.disabled is true; validator will not call set_weights" in result.warnings


def test_chain_launch_preflight_cli_prints_live_report(monkeypatch, tmp_path) -> None:
    import cathedral.chain as chain_module

    class CliChain(_FakeChain):
        def __init__(self, **kwargs) -> None:
            super().__init__()

    monkeypatch.setattr(chain_module, "BittensorChain", CliChain)
    config_path = tmp_path / "mainnet.toml"
    config_path.write_text(
        """
[network]
name = "finney"
netuid = 39
validator_hotkey = "validator-hotkey-name"
wallet_name = "cathedral-validator"

[polaris]
base_url = "https://api.polaris.computer/"
public_key_hex = "1111111111111111111111111111111111111111111111111111111111111111"

[weights]
interval_secs = 1500
disabled = false
burn_uid = 204
forced_burn_percentage = 95.0
task_family_weights = { synthetic_boolean_v1 = 0.0 }

[publisher]
url = "https://api.cathedral.computer"
public_key_env = "CATHEDRAL_PUBLIC_KEY_HEX"

[remote_weight_source]
enabled = true
url = "https://api.cathedral.computer"
key_id = "cathedral-weight-policy"
public_key_env = "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX"
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        validator_app,
        ["chain-launch-preflight", "--config", str(config_path)],
    )

    assert result.exit_code == 0, result.output
    assert '"validator_uid": 7' in result.output
    assert "Validator chain launch preflight passed" in result.output
