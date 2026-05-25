"""Validator + miner settings (TOML + env, via pydantic-settings)."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger(__name__)

MAINNET_FORCED_BURN_PERCENTAGE = 95.0


def _load_toml(path: Path) -> dict[str, Any]:
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib.loads(path.read_text())
    import tomli

    return tomli.loads(path.read_text())


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------


class NetworkConfig(BaseModel):
    name: str
    netuid: int
    validator_hotkey: str
    wallet_name: str = "default"
    wallet_path: str | None = None


class PolarisConfig(BaseModel):
    base_url: str
    public_key_hex: str
    fetch_timeout_secs: float = 20.0


class HttpConfig(BaseModel):
    listen_host: str = "0.0.0.0"
    listen_port: int = 9333
    bearer_token_env: str = "CATHEDRAL_BEARER"


class WeightsConfig(BaseModel):
    interval_secs: int = 1200
    disabled: bool = False
    burn_uid: int = 204
    forced_burn_percentage: float = MAINNET_FORCED_BURN_PERCENTAGE
    v3_bug_isolation_weight: float = 0.0
    task_family_weights: dict[str, float] = Field(default_factory=dict)


class WeightSourceConfig(BaseModel):
    """Deprecated pre-#155 remote-weight config block.

    Shipped validator TOML files still carry ``[weight_source]`` for
    older binaries. Parse it so current binaries accept those files, but
    do not use it for remote mode. The authoritative opt-in is
    ``[remote_weight_source].enabled``.
    """

    mode: str = "local"
    publisher_weights_url: str = "https://api.cathedral.computer/v1/validator/weights/next"
    poll_interval_secs: float = 60.0
    request_timeout_secs: float = 10.0
    fallback_after_stale_minutes: float = 10.0
    refuse_after_stale_minutes: float = 30.0
    cathedral_policy_public_key_hex: str | None = None
    cathedral_policy_public_key_env: str = "CATHEDRAL_POLICY_PUBLIC_KEY_HEX"
    cathedral_policy_key_id: str = "cathedral-weight-policy"


class RemoteWeightSourceConfig(BaseModel):
    """Optional opt-in remote signed-weight source (issue #155).

    Default ``enabled=False`` preserves local-only behaviour. When
    enabled the validator runs the remote weight loop instead of the
    local one, fetching from ``url`` and verifying signatures against
    the pinned ``key_id``. Poll cadence (``poll_interval_secs``) is
    decoupled from the chain set_weights cadence; the loop only relays
    to chain when a new accepted vector arrives.
    """

    enabled: bool = False
    url: str = "https://api.cathedral.computer"
    key_id: str = "cathedral-weight-policy"
    public_key_env: str = "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX"
    poll_interval_secs: float = 60.0
    request_timeout_secs: float = 10.0


class PublisherConfig(BaseModel):
    """Where the validator pulls signed eval-runs from."""

    url: str = "https://api.cathedral.computer"
    public_key_env: str = "CATHEDRAL_PUBLIC_KEY_HEX"
    pull_interval_secs: float = 30.0
    api_token_env: str | None = None


class StorageConfig(BaseModel):
    database_path: str = "data/validator.db"


class WorkerConfig(BaseModel):
    poll_interval_secs: float = 5.0
    max_concurrent_verifications: int = 4


class StallConfig(BaseModel):
    after_secs: int = 600


class ValidatorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CATHEDRAL_", env_nested_delimiter="__")

    network: NetworkConfig
    polaris: PolarisConfig
    http: HttpConfig = Field(default_factory=HttpConfig)
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    weight_source: WeightSourceConfig = Field(default_factory=WeightSourceConfig)
    publisher: PublisherConfig = Field(default_factory=PublisherConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    stall: StallConfig = Field(default_factory=StallConfig)
    remote_weight_source: RemoteWeightSourceConfig = Field(default_factory=RemoteWeightSourceConfig)

    @classmethod
    def from_toml(cls, path: str | Path) -> ValidatorSettings:
        data = _load_toml(Path(path))
        return cls.model_validate(data)


def resolve_validator_config_path(
    path: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: str | Path | None = None,
    etc_dir: str | Path | None = None,
) -> str:
    """Resolve the validator config path, including the managed SN39 migration.

    Older provisioned hosts were launched by PM2 with
    `/etc/cathedral/testnet.toml`. The signed-tag updater can only reload that
    process on the first update, so the validator itself needs to redirect that
    legacy managed path to a rendered mainnet config.
    """
    values = os.environ if env is None else env
    selected_network = values.get("CATHEDRAL_NETWORK", "").strip().lower()
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    managed_etc = Path(etc_dir) if etc_dir is not None else Path("/etc/cathedral")
    requested = Path(path)
    legacy_testnet = managed_etc / "testnet.toml"
    mainnet = managed_etc / "mainnet.toml"
    # The updater exports this for legacy managed migrations after proving the
    # directory is writable; use it when rendering/syncing managed mainnet TOML
    # so SQLite does not fail later creating a parent under unwritable /var/lib.
    validator_state_dir = _validator_state_dir_override(values)

    override = values.get("CATHEDRAL_CONFIG_PATH")
    if override:
        override_path = Path(override)
        if selected_network != "testnet":
            _sync_managed_validator_database_path(
                override_path,
                managed_mainnet_path=mainnet,
                state_dir=validator_state_dir,
            )
            _sync_sn39_mainnet_weight_policy(override_path)
        return override

    if selected_network == "testnet":
        return str(path)

    if requested != legacy_testnet:
        _sync_managed_validator_database_path(
            requested,
            managed_mainnet_path=mainnet,
            state_dir=validator_state_dir,
        )
        _sync_sn39_mainnet_weight_policy(requested)
        return str(path)

    if not mainnet.exists():
        _render_managed_mainnet_config(
            legacy_path=legacy_testnet,
            mainnet_path=mainnet,
            template_path=root / "config" / "mainnet.toml",
            state_dir=validator_state_dir,
        )
    else:
        _sync_managed_validator_database_path(
            mainnet,
            managed_mainnet_path=mainnet,
            state_dir=validator_state_dir,
        )
    _sync_sn39_mainnet_weight_policy(mainnet)
    _ensure_managed_env_path(managed_etc / "validator.env", mainnet)
    return str(mainnet if mainnet.exists() else requested)


def _render_managed_mainnet_config(
    *,
    legacy_path: Path,
    mainnet_path: Path,
    template_path: Path,
    state_dir: Path | None = None,
) -> None:
    if not legacy_path.exists() or not template_path.exists():
        return

    current = _load_toml(legacy_path)
    network = current.get("network", {})
    polaris = current.get("polaris", {})
    if not isinstance(network, dict) or not isinstance(polaris, dict):
        return

    wallet_hotkey = str(network.get("validator_hotkey") or "default")
    wallet_name = str(network.get("wallet_name") or "cathedral-validator")
    polaris_key = str(
        polaris.get("public_key_hex") or "REPLACE_WITH_POLARIS_ED25519_PUBLIC_KEY_HEX"
    )

    rendered = template_path.read_text()
    rendered = rendered.replace(
        'validator_hotkey = "REPLACE_ME"',
        f"validator_hotkey = {_toml_string(wallet_hotkey)}",
    )
    rendered = rendered.replace(
        'wallet_name = "cathedral-validator"',
        f"wallet_name = {_toml_string(wallet_name)}",
    )
    rendered = rendered.replace(
        'public_key_hex = "REPLACE_WITH_POLARIS_ED25519_PUBLIC_KEY_HEX"',
        f"public_key_hex = {_toml_string(polaris_key)}",
    )
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        rendered = _replace_or_add_database_path(
            rendered,
            str(state_dir / "validator-mainnet.db"),
        )

    wallet_path = network.get("wallet_path")
    if wallet_path and "wallet_path =" not in rendered:
        rendered = rendered.replace(
            f"wallet_name = {_toml_string(wallet_name)}",
            f"wallet_name = {_toml_string(wallet_name)}\n"
            f"wallet_path = {_toml_string(str(wallet_path))}",
        )

    mainnet_path.parent.mkdir(parents=True, exist_ok=True)
    mainnet_path.write_text(rendered)
    mainnet_path.chmod(0o644)


def _validator_state_dir_override(values: Mapping[str, str]) -> Path | None:
    raw = values.get("CATHEDRAL_VALIDATOR_STATE_DIR")
    if not raw:
        return None
    return Path(raw).expanduser()


def _sync_managed_validator_database_path(
    config_path: Path,
    *,
    managed_mainnet_path: Path,
    state_dir: Path | None,
) -> None:
    if (
        state_dir is None
        or not config_path.exists()
        or not _same_path(config_path, managed_mainnet_path)
    ):
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    desired = str(state_dir / "validator-mainnet.db")
    current = _load_toml(config_path)
    storage = current.get("storage", {})
    if isinstance(storage, dict) and str(storage.get("database_path", "")) == desired:
        return
    config_path.write_text(_replace_or_add_database_path(config_path.read_text(), desired))


def _replace_or_add_database_path(text: str, database_path: str) -> str:
    desired_line = f"database_path = {_toml_string(database_path)}"
    lines = text.splitlines()
    out: list[str] = []
    in_storage = False
    saw_storage = False
    replaced = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_storage and not replaced:
                out.append(desired_line)
                replaced = True
            in_storage = stripped == "[storage]"
            saw_storage = saw_storage or in_storage

        if in_storage and stripped.startswith("database_path"):
            out.append(desired_line)
            replaced = True
        else:
            out.append(line)

    if saw_storage and in_storage and not replaced:
        out.append(desired_line)
    elif not saw_storage:
        out.extend(["", "[storage]", desired_line])

    return "\n".join(out) + "\n"


def _ensure_managed_env_path(env_path: Path, config_path: Path) -> None:
    existing = env_path.read_text().splitlines() if env_path.exists() else []
    updates = {
        "CATHEDRAL_CONFIG_PATH": str(config_path),
        "CATHEDRAL_NETWORK": "mainnet",
    }
    seen: set[str] = set()
    lines: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def _sync_sn39_mainnet_weight_policy(config_path: Path) -> None:
    """Keep SN39 mainnet configs on the current release burn policy."""
    if not config_path.exists():
        return
    try:
        current = _load_toml(config_path)
    except Exception:
        return

    network = current.get("network", {})
    weights = current.get("weights", {})
    if not isinstance(network, dict) or not isinstance(weights, dict):
        return
    if str(network.get("name")) != "finney" or int(network.get("netuid", -1)) != 39:
        return
    if float(weights.get("forced_burn_percentage", MAINNET_FORCED_BURN_PERCENTAGE)) == (
        MAINNET_FORCED_BURN_PERCENTAGE
    ):
        return

    text = config_path.read_text()
    lines = text.splitlines()
    out: list[str] = []
    in_weights = False
    saw_weights = False
    replaced = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_weights and not replaced:
                out.append(f"forced_burn_percentage = {MAINNET_FORCED_BURN_PERCENTAGE:.1f}")
                replaced = True
            in_weights = stripped == "[weights]"
            saw_weights = saw_weights or in_weights

        if in_weights and stripped.startswith("forced_burn_percentage"):
            out.append(f"forced_burn_percentage = {MAINNET_FORCED_BURN_PERCENTAGE:.1f}")
            replaced = True
        else:
            out.append(line)

    if saw_weights and in_weights and not replaced:
        out.append(f"forced_burn_percentage = {MAINNET_FORCED_BURN_PERCENTAGE:.1f}")
    elif not saw_weights:
        out.extend(
            [
                "",
                "[weights]",
                f"forced_burn_percentage = {MAINNET_FORCED_BURN_PERCENTAGE:.1f}",
            ]
        )

    # Surface the rewrite. Historically this function silently clobbered an
    # operator's `forced_burn_percentage` edit with `MAINNET_FORCED_BURN_PERCENTAGE`
    # from config.py, with zero log output. Operators editing the toml saw their
    # change "revert itself" on next start and concluded their config was broken.
    # The warning makes the override discoverable in startup logs.
    logger.warning(
        "config_override_applied",
        path=str(config_path),
        field="forced_burn_percentage",
        old_value=float(weights.get("forced_burn_percentage", MAINNET_FORCED_BURN_PERCENTAGE)),
        new_value=MAINNET_FORCED_BURN_PERCENTAGE,
        reason="mainnet_policy",
    )
    config_path.write_text("\n".join(out) + "\n")


def _same_path(left: Path, right: Path) -> bool:
    return left.expanduser().resolve(strict=False) == right.expanduser().resolve(strict=False)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------
# Miner
# --------------------------------------------------------------------------


class MinerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CATHEDRAL_MINER_", env_nested_delimiter="__")

    miner_hotkey: str
    owner_wallet: str
    validator_url: str
    validator_bearer_env: str = "CATHEDRAL_VALIDATOR_BEARER"

    @classmethod
    def from_toml(cls, path: str | Path) -> MinerSettings:
        data = _load_toml(Path(path))
        return cls.model_validate(data)
