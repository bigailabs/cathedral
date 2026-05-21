"""Read-only live-chain preflight for validator launch checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cathedral.chain import Chain, Metagraph, MinerNode
from cathedral.config import ValidatorSettings

_BLOCK_TIME_SECS = 12
_HYPERPARAMETER_DETAIL_KEYS = (
    "tempo",
    "immunity_period",
    "weights_rate_limit",
    "commit_reveal_weights_enabled",
    "commit_reveal_period",
    "max_weight_limit",
    "max_weights_limit",
    "min_allowed_weights",
)


@dataclass(frozen=True)
class ValidatorChainLaunchPreflightResult:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


async def run_validator_chain_launch_preflight(
    settings: ValidatorSettings,
    chain: Chain,
) -> ValidatorChainLaunchPreflightResult:
    """Inspect live chain state without submitting extrinsics."""
    errors: list[str] = []
    warnings: list[str] = []

    current_block = await chain.current_block()
    registered = await chain.is_registered()
    metagraph = await chain.metagraph()
    wallet_hotkey = await _validator_hotkey_ss58(chain)
    miner = _find_validator_miner(
        metagraph,
        config_hotkey=settings.network.validator_hotkey,
        wallet_hotkey=wallet_hotkey,
    )
    hyperparameters = await _subnet_hyperparameters(chain)
    hyperparameter_details = _selected_hyperparameters(hyperparameters)

    details: dict[str, Any] = {
        "network": settings.network.name,
        "netuid": settings.network.netuid,
        "config_validator_hotkey": settings.network.validator_hotkey,
        "wallet_hotkey_ss58": wallet_hotkey,
        "current_block": current_block,
        "metagraph_block": metagraph.block,
        "metagraph_size": len(metagraph.miners),
        "registered": registered,
        "weights_disabled": settings.weights.disabled,
        "weights_interval_secs": settings.weights.interval_secs,
        "hyperparameters": hyperparameter_details,
    }
    if miner is not None:
        details.update(
            {
                "validator_uid": miner.uid,
                "validator_hotkey": miner.hotkey,
                "validator_last_update_block": miner.last_update_block,
                "validator_permit": miner.validator_permit,
                "validator_stake": miner.stake,
            }
        )

    if not registered:
        errors.append("validator hotkey is not registered on the configured subnet")
    if miner is None:
        message = "validator hotkey was not found in the live metagraph snapshot"
        if registered:
            warnings.append(message)
        else:
            errors.append(message)
    else:
        _check_validator_miner(miner, errors, warnings)

    if not hyperparameters:
        warnings.append("subnet hyperparameters were unavailable from the Bittensor SDK")
    else:
        _check_hyperparameters(
            hyperparameters,
            settings=settings,
            details=details,
            warnings=warnings,
        )

    if settings.weights.disabled:
        warnings.append("weights.disabled is true; validator will not call set_weights")

    return ValidatorChainLaunchPreflightResult(
        errors=tuple(errors),
        warnings=tuple(warnings),
        details=details,
    )


async def _validator_hotkey_ss58(chain: Chain) -> str:
    method = getattr(chain, "validator_hotkey_ss58", None)
    if method is None:
        return ""
    value = await method()
    return str(value)


async def _subnet_hyperparameters(chain: Chain) -> dict[str, Any]:
    method = getattr(chain, "subnet_hyperparameters", None)
    if method is None:
        return {}
    value = await method()
    if isinstance(value, dict):
        return value
    return {}


def _find_validator_miner(
    metagraph: Metagraph,
    *,
    config_hotkey: str,
    wallet_hotkey: str,
) -> MinerNode | None:
    candidates = {config_hotkey}
    if wallet_hotkey:
        candidates.add(wallet_hotkey)
    for miner in metagraph.miners:
        if miner.hotkey in candidates:
            return miner
    return None


def _check_validator_miner(
    miner: MinerNode,
    errors: list[str],
    warnings: list[str],
) -> None:
    if miner.validator_permit is False:
        errors.append("validator hotkey does not have a permit in the live metagraph")
    elif miner.validator_permit is None:
        warnings.append("validator permit was unavailable in the live metagraph")

    if miner.stake is None:
        warnings.append("validator stake was unavailable in the live metagraph")
    elif miner.stake <= 0.0:
        errors.append("validator stake is zero in the live metagraph")


def _selected_hyperparameters(hyperparameters: dict[str, Any]) -> dict[str, Any]:
    return {
        key: hyperparameters[key]
        for key in _HYPERPARAMETER_DETAIL_KEYS
        if key in hyperparameters
    }


def _check_hyperparameters(
    hyperparameters: dict[str, Any],
    *,
    settings: ValidatorSettings,
    details: dict[str, Any],
    warnings: list[str],
) -> None:
    rate_limit_blocks = _int_value(hyperparameters.get("weights_rate_limit"))
    if rate_limit_blocks is not None:
        rate_limit_secs = rate_limit_blocks * _BLOCK_TIME_SECS
        details["weights_rate_limit_blocks"] = rate_limit_blocks
        details["weights_rate_limit_estimated_secs"] = rate_limit_secs
        if settings.weights.interval_secs < rate_limit_secs:
            warnings.append(
                "weights.interval_secs is below the live weights_rate_limit estimate"
            )

    commit_reveal_enabled = _bool_value(
        hyperparameters.get("commit_reveal_weights_enabled")
    )
    commit_reveal_period = _int_value(hyperparameters.get("commit_reveal_period"))
    immunity_period = _int_value(hyperparameters.get("immunity_period"))
    if commit_reveal_enabled is True:
        details["commit_reveal_weights_enabled"] = True
        if commit_reveal_period is None:
            warnings.append("commit-reveal is enabled but commit_reveal_period is unavailable")
    elif commit_reveal_enabled is False:
        details["commit_reveal_weights_enabled"] = False

    if commit_reveal_period is not None:
        details["commit_reveal_period"] = commit_reveal_period
    if immunity_period is not None:
        details["immunity_period"] = immunity_period
    if commit_reveal_period is not None and immunity_period is not None:
        ok = immunity_period > commit_reveal_period
        details["immunity_exceeds_commit_reveal_period"] = ok
        if not ok:
            warnings.append(
                "subnet immunity_period is not greater than commit_reveal_period"
            )


def _int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if hasattr(value, "item"):
            value = value.item()
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool_value(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int):
        return bool(value)
    return None


__all__ = [
    "ValidatorChainLaunchPreflightResult",
    "run_validator_chain_launch_preflight",
]
