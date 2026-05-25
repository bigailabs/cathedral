from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cathedral import config as config_module
from cathedral.config import ValidatorSettings, resolve_validator_config_path

POLARIS_KEY = "11" * 32


def _write_legacy_testnet(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "[network]",
                'name = "test"',
                "netuid = 292",
                'validator_hotkey = "operator-hotkey"',
                'wallet_name = "operator-wallet"',
                'wallet_path = "/var/lib/bittensor/wallets"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "fetch_timeout_secs = 20",
                "",
            ]
        )
        + "\n"
    )


def test_managed_legacy_testnet_path_renders_mainnet(tmp_path: Path) -> None:
    etc = tmp_path / "etc" / "cathedral"
    state_dir = tmp_path / "var" / "lib" / "cathedral"
    etc.mkdir(parents=True)
    legacy = etc / "testnet.toml"
    _write_legacy_testnet(legacy)
    (etc / "validator.env").write_text("CATHEDRAL_BEARER=local\n")

    resolved = resolve_validator_config_path(
        legacy,
        env={"CATHEDRAL_VALIDATOR_STATE_DIR": str(state_dir)},
        repo_root=Path.cwd(),
        etc_dir=etc,
    )

    assert resolved == str(etc / "mainnet.toml")
    settings = ValidatorSettings.from_toml(resolved)
    assert settings.network.name == "finney"
    assert settings.network.netuid == 39
    assert settings.network.validator_hotkey == "operator-hotkey"
    assert settings.network.wallet_name == "operator-wallet"
    assert settings.network.wallet_path == "/var/lib/bittensor/wallets"
    assert settings.polaris.public_key_hex == POLARIS_KEY
    assert settings.storage.database_path == str(state_dir / "validator-mainnet.db")
    assert state_dir.is_dir()
    assert settings.weights.interval_secs == 1500
    assert settings.weights.burn_uid == 204
    assert settings.weights.forced_burn_percentage == 95.0

    env_text = (etc / "validator.env").read_text()
    assert f"CATHEDRAL_CONFIG_PATH={etc / 'mainnet.toml'}" in env_text
    assert "CATHEDRAL_NETWORK=mainnet" in env_text


def test_explicit_config_path_is_respected(tmp_path: Path) -> None:
    etc = tmp_path / "etc" / "cathedral"
    etc.mkdir(parents=True)
    legacy = etc / "testnet.toml"
    _write_legacy_testnet(legacy)

    resolved = resolve_validator_config_path(
        legacy,
        env={"CATHEDRAL_CONFIG_PATH": str(legacy)},
        repo_root=Path.cwd(),
        etc_dir=etc,
    )

    assert resolved == str(legacy)
    assert not (etc / "mainnet.toml").exists()


def test_managed_mainnet_config_syncs_current_burn_policy(tmp_path: Path) -> None:
    etc = tmp_path / "etc" / "cathedral"
    etc.mkdir(parents=True)
    mainnet = etc / "mainnet.toml"
    mainnet.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                'wallet_name = "operator-wallet"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weights]",
                "interval_secs = 1500",
                "disabled = false",
                "burn_uid = 204",
                "forced_burn_percentage = 98.0",
            ]
        )
        + "\n"
    )

    resolved = resolve_validator_config_path(
        mainnet,
        env={"CATHEDRAL_CONFIG_PATH": str(mainnet)},
        repo_root=Path.cwd(),
        etc_dir=etc,
    )

    assert resolved == str(mainnet)
    settings = ValidatorSettings.from_toml(resolved)
    assert settings.weights.forced_burn_percentage == 95.0
    assert "forced_burn_percentage = 95.0" in mainnet.read_text()


def test_managed_mainnet_config_syncs_state_dir_override(tmp_path: Path) -> None:
    etc = tmp_path / "etc" / "cathedral"
    state_dir = tmp_path / "install" / "state"
    etc.mkdir(parents=True)
    mainnet = etc / "mainnet.toml"
    mainnet.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[storage]",
                'database_path = "/var/lib/cathedral/validator-mainnet.db"',
            ]
        )
        + "\n"
    )

    resolved = resolve_validator_config_path(
        mainnet,
        env={
            "CATHEDRAL_CONFIG_PATH": str(mainnet),
            "CATHEDRAL_VALIDATOR_STATE_DIR": str(state_dir),
        },
        repo_root=Path.cwd(),
        etc_dir=etc,
    )

    assert resolved == str(mainnet)
    settings = ValidatorSettings.from_toml(resolved)
    assert settings.storage.database_path == str(state_dir / "validator-mainnet.db")
    assert state_dir.is_dir()


def test_custom_sn39_config_path_syncs_current_burn_policy(tmp_path: Path) -> None:
    custom = tmp_path / "operator" / "mainnet-custom.toml"
    custom.parent.mkdir(parents=True)
    custom.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                'wallet_name = "operator-wallet"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weights]",
                "interval_secs = 1500",
                "disabled = false",
                "burn_uid = 204",
                "forced_burn_percentage = 98.0",
            ]
        )
        + "\n"
    )

    resolved = resolve_validator_config_path(
        custom,
        env={"CATHEDRAL_CONFIG_PATH": str(custom)},
        repo_root=Path.cwd(),
        etc_dir=tmp_path / "etc" / "cathedral",
    )

    assert resolved == str(custom)
    settings = ValidatorSettings.from_toml(resolved)
    assert settings.weights.forced_burn_percentage == 95.0
    assert "forced_burn_percentage = 95.0" in custom.read_text()


def test_retired_weight_config_sections_are_ignored(tmp_path: Path) -> None:
    config = tmp_path / "mainnet.toml"
    config.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weights]",
                "interval_secs = 1500",
                "forced_burn_percentage = 95.0",
                "",
                "[weight_source]",
                'mode = "remote"',
                "",
                "[remote_weight_source]",
                "enabled = true",
            ]
        )
        + "\n"
    )

    settings = ValidatorSettings.from_toml(config)

    assert settings.network.name == "finney"
    assert settings.network.validator_hotkey == "operator-hotkey"
    assert settings.weights.interval_secs == 1500


def test_unknown_top_level_config_sections_still_fail(tmp_path: Path) -> None:
    config = tmp_path / "mainnet.toml"
    config.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weightz]",
                "interval_secs = 1500",
            ]
        )
        + "\n"
    )

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ValidatorSettings.from_toml(config)


def test_explicit_testnet_network_is_respected(tmp_path: Path) -> None:
    etc = tmp_path / "etc" / "cathedral"
    etc.mkdir(parents=True)
    legacy = etc / "testnet.toml"
    _write_legacy_testnet(legacy)

    resolved = resolve_validator_config_path(
        legacy,
        env={"CATHEDRAL_NETWORK": "testnet"},
        repo_root=Path.cwd(),
        etc_dir=etc,
    )

    assert resolved == str(legacy)
    assert not (etc / "mainnet.toml").exists()


def test_unmanaged_testnet_path_is_unchanged(tmp_path: Path) -> None:
    unmanaged = tmp_path / "config" / "testnet.toml"
    unmanaged.parent.mkdir()
    unmanaged.write_text("")

    resolved = resolve_validator_config_path(
        unmanaged,
        env={},
        repo_root=Path.cwd(),
        etc_dir=tmp_path / "etc" / "cathedral",
    )

    assert resolved == str(unmanaged)


def test_sync_sn39_mainnet_weight_policy_emits_config_override_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workstream A diagnostic guardrail: the silent toml rewrite must now
    emit a `config_override_applied` warning so operators editing
    `forced_burn_percentage` can see in stderr that their edit was clobbered.
    """
    config_path = tmp_path / "mainnet.toml"
    config_path.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weights]",
                "interval_secs = 1500",
                "burn_uid = 204",
                # Off-policy value -> triggers the rewrite -> must log.
                "forced_burn_percentage = 50.0",
            ]
        )
        + "\n"
    )

    events: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **fields: object) -> None:
            events.append((event, dict(fields)))

        def info(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

        def error(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

        def debug(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(config_module, "logger", FakeLogger())

    config_module._sync_sn39_mainnet_weight_policy(config_path)

    # The rewrite happened: file is now on policy.
    assert "forced_burn_percentage = 95.0" in config_path.read_text()

    # The log was emitted with the expected fields.
    override_events = [
        (event, fields) for event, fields in events if event == "config_override_applied"
    ]
    assert len(override_events) == 1, f"expected one override event, got {events}"
    _, fields = override_events[0]
    assert fields["path"] == str(config_path)
    assert fields["field"] == "forced_burn_percentage"
    assert fields["old_value"] == 50.0
    assert fields["new_value"] == config_module.MAINNET_FORCED_BURN_PERCENTAGE
    assert fields["reason"] == "mainnet_policy"


def test_sync_sn39_mainnet_weight_policy_does_not_log_when_value_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No spurious override log when the operator's value already matches."""
    config_path = tmp_path / "mainnet.toml"
    config_path.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "operator-hotkey"',
                "",
                "[polaris]",
                'base_url = "https://api.polaris.computer/"',
                f'public_key_hex = "{POLARIS_KEY}"',
                "",
                "[weights]",
                f"forced_burn_percentage = {config_module.MAINNET_FORCED_BURN_PERCENTAGE}",
            ]
        )
        + "\n"
    )

    events: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def warning(self, event: str, **fields: object) -> None:
            events.append((event, dict(fields)))

        def info(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

        def error(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

        def debug(self, event: str, **fields: object) -> None:  # pragma: no cover
            pass

    monkeypatch.setattr(config_module, "logger", FakeLogger())

    config_module._sync_sn39_mainnet_weight_policy(config_path)

    # No override fired.
    override_events = [
        (event, fields) for event, fields in events if event == "config_override_applied"
    ]
    assert override_events == []
