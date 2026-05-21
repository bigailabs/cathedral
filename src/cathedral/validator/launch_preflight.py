"""Validator-side launch preflight checks for SAT weight enablement."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cathedral.config import ValidatorSettings

SAT_FAMILY_ID = "synthetic_boolean_v1"
PLACEHOLDER_HOTKEY = "REPLACE_ME"
PLACEHOLDER_POLARIS_KEY = "REPLACE_WITH_POLARIS_ED25519_PUBLIC_KEY_HEX"


@dataclass(frozen=True)
class ValidatorLaunchPreflightResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _hex_is_32_bytes(value: str) -> bool:
    try:
        return len(bytes.fromhex(value.strip())) == 32
    except ValueError:
        return False


def run_validator_sat_launch_preflight(
    settings: ValidatorSettings,
    env: Mapping[str, str] | None = None,
    *,
    require_remote_weight_source: bool = True,
    require_zero_local_sat_weight: bool = True,
) -> ValidatorLaunchPreflightResult:
    """Validate validator config/env for SAT launch without touching chain state."""

    env = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []

    sat_weight = float(settings.weights.task_family_weights.get(SAT_FAMILY_ID, 0.0))
    details: dict[str, Any] = {
        "network": settings.network.name,
        "netuid": settings.network.netuid,
        "validator_hotkey": settings.network.validator_hotkey,
        "remote_weight_source_enabled": settings.remote_weight_source.enabled,
        "local_sat_weight": sat_weight,
        "weights_disabled": settings.weights.disabled,
        "weights_interval_secs": settings.weights.interval_secs,
        "forced_burn_percentage": settings.weights.forced_burn_percentage,
    }

    if settings.network.validator_hotkey == PLACEHOLDER_HOTKEY:
        errors.append("network.validator_hotkey is still REPLACE_ME")
    if settings.polaris.public_key_hex == PLACEHOLDER_POLARIS_KEY:
        errors.append("polaris.public_key_hex is still the placeholder")
    elif not _hex_is_32_bytes(settings.polaris.public_key_hex):
        errors.append("polaris.public_key_hex must be a 32-byte hex public key")

    publisher_key = env.get(settings.publisher.public_key_env, "").strip()
    if not publisher_key:
        errors.append(f"{settings.publisher.public_key_env} is required for signed eval pulls")
    elif not _hex_is_32_bytes(publisher_key):
        errors.append(f"{settings.publisher.public_key_env} must be a 32-byte hex public key")

    if settings.weights.interval_secs < 1200:
        warnings.append("weights.interval_secs is below the SN39 100-block rate-limit window")
    if not 0.0 <= settings.weights.forced_burn_percentage <= 100.0:
        errors.append("weights.forced_burn_percentage must be between 0 and 100")

    if require_zero_local_sat_weight and sat_weight != 0.0:
        errors.append(
            f"weights.task_family_weights.{SAT_FAMILY_ID} must stay 0.0 for remote-weight launch"
        )

    remote = settings.remote_weight_source
    if require_remote_weight_source and not remote.enabled:
        errors.append("remote_weight_source.enabled must be true before mainnet SAT weight")

    if remote.enabled:
        if not remote.url.strip():
            errors.append("remote_weight_source.url is required when remote weights are enabled")
        if not remote.key_id.strip():
            errors.append("remote_weight_source.key_id is required when remote weights are enabled")
        remote_key = env.get(remote.public_key_env, "").strip()
        if not remote_key:
            errors.append(f"{remote.public_key_env} is required when remote weights are enabled")
        elif not _hex_is_32_bytes(remote_key):
            errors.append(f"{remote.public_key_env} must be a 32-byte hex public key")
    elif sat_weight > 0.0:
        warnings.append(
            f"{SAT_FAMILY_ID} has local nonzero weight while remote weights are disabled"
        )

    if settings.weights.disabled:
        warnings.append("weights.disabled is true; validator will not call set_weights")

    return ValidatorLaunchPreflightResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
        details=details,
    )


__all__ = [
    "SAT_FAMILY_ID",
    "ValidatorLaunchPreflightResult",
    "run_validator_sat_launch_preflight",
]
