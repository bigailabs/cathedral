"""Validator-side launch preflight checks for SAT weight enablement."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cathedral.config import ValidatorSettings

SAT_FAMILY_ID = "synthetic_boolean_v1"
TASK_FAMILY_WEIGHTS_JSON_ENV = "CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON"
SYNTHETIC_BOOLEAN_WEIGHT_ENV = "CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT"
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


def _resolved_task_family_weights(
    configured: Mapping[str, float] | None,
    env: Mapping[str, str],
    warnings: list[str],
) -> dict[str, Any]:
    """Mirror runtime env overrides before evaluating launch weight gates."""

    out: dict[str, Any] = dict(configured or {})
    raw_json = env.get(TASK_FAMILY_WEIGHTS_JSON_ENV, "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            warnings.append(f"{TASK_FAMILY_WEIGHTS_JSON_ENV} is not valid JSON; ignored")
        else:
            if isinstance(parsed, dict):
                out.update(parsed)
            else:
                warnings.append(f"{TASK_FAMILY_WEIGHTS_JSON_ENV} must be a JSON object; ignored")

    raw_sat = env.get(SYNTHETIC_BOOLEAN_WEIGHT_ENV)
    if raw_sat is not None:
        try:
            out[SAT_FAMILY_ID] = float(raw_sat)
        except ValueError:
            warnings.append(f"{SYNTHETIC_BOOLEAN_WEIGHT_ENV} is not a float; ignored")

    return out


def run_validator_sat_launch_preflight(
    settings: ValidatorSettings,
    env: Mapping[str, str] | None = None,
    *,
    require_zero_local_sat_weight: bool = False,
) -> ValidatorLaunchPreflightResult:
    """Validate validator config/env for SAT launch without touching chain state.

    The validator runs a single local weight path (pull_loop + weight_loop
    with PAR-2 + hardcoded burn); the signed-publisher-vector relay (Path B)
    was removed, so there is no remote-weight opt-in to gate on. The
    always-on check is ``0.0 <= sat_weight <= 1.0``. ``require_zero_local_sat_weight``
    (default ``False``) preserves the optional strict guard that forces the
    local SAT blend to 0.0 for operators who want it.
    """

    env = os.environ if env is None else env
    errors: list[str] = []
    warnings: list[str] = []

    task_family_weights = _resolved_task_family_weights(
        settings.weights.task_family_weights,
        env,
        warnings,
    )
    try:
        sat_weight = float(task_family_weights.get(SAT_FAMILY_ID, 0.0))
    except (TypeError, ValueError):
        sat_weight = 0.0
        errors.append(f"weights.task_family_weights.{SAT_FAMILY_ID} must be a float")
    details: dict[str, Any] = {
        "network": settings.network.name,
        "netuid": settings.network.netuid,
        "validator_hotkey": settings.network.validator_hotkey,
        "local_sat_weight": sat_weight,
        "task_family_weights": task_family_weights,
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

    if not 0.0 <= sat_weight <= 1.0:
        errors.append(
            f"weights.task_family_weights.{SAT_FAMILY_ID} must be in [0.0, 1.0] "
            f"(got {sat_weight})"
        )
    if require_zero_local_sat_weight and sat_weight != 0.0:
        errors.append(
            f"weights.task_family_weights.{SAT_FAMILY_ID} must stay 0.0 under the strict guard"
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
    "SYNTHETIC_BOOLEAN_WEIGHT_ENV",
    "TASK_FAMILY_WEIGHTS_JSON_ENV",
    "ValidatorLaunchPreflightResult",
    "run_validator_sat_launch_preflight",
]
