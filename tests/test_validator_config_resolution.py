from __future__ import annotations

from pathlib import Path

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
    etc.mkdir(parents=True)
    legacy = etc / "testnet.toml"
    _write_legacy_testnet(legacy)
    (etc / "validator.env").write_text("CATHEDRAL_BEARER=local\n")

    resolved = resolve_validator_config_path(
        legacy,
        env={},
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
