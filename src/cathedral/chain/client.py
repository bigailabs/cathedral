"""Bittensor chain integration.

Wraps the official `bittensor` SDK (v10.x). Blocking SDK calls run inside
`asyncio.to_thread` so the validator's async loop is not stalled.

The `Chain` Protocol lets tests substitute `MockChain` without touching
real chain RPC.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class WeightStatus(str, Enum):
    HEALTHY = "healthy"
    BLOCKED_BY_STAKE = "blocked_by_stake"
    BLOCKED_BY_TRANSACTION_ERROR = "blocked_by_transaction_error"
    DISABLED = "disabled"


@dataclass(frozen=True)
class MinerNode:
    uid: int
    hotkey: str
    last_update_block: int
    validator_permit: bool | None = None
    stake: float | None = None


@dataclass(frozen=True)
class Metagraph:
    block: int
    miners: tuple[MinerNode, ...]

    def hotkey_to_uid(self) -> dict[str, int]:
        return {m.hotkey: m.uid for m in self.miners}


class Chain(Protocol):
    async def metagraph(self) -> Metagraph: ...
    async def set_weights(self, weights: list[tuple[int, float]]) -> WeightStatus: ...
    async def current_block(self) -> int: ...
    async def is_registered(self) -> bool: ...


def network_endpoint(name: str) -> str:
    if name == "finney":
        return "wss://entrypoint-finney.opentensor.ai:443"
    if name == "test":
        return "wss://test.finney.opentensor.ai:443"
    if name == "local":
        return "ws://127.0.0.1:9944"
    raise ValueError(f"unknown network {name!r}")


# Substring fragments of bittensor error messages that indicate the validator
# is below the permit-stake threshold. The SDK does not expose a structured
# error code for this, so we match on text.
_STAKE_BLOCK_FRAGMENTS = (
    "stake",
    "permit",
    "min_allowed_weights",
    "validator permit",
)


class BittensorChain:
    """Production chain client backed by the bittensor SDK."""

    def __init__(
        self,
        network: str,
        netuid: int,
        wallet_name: str,
        wallet_hotkey: str,
        wallet_path: str | None = None,
    ) -> None:
        self.network = network
        self.netuid = netuid
        self.wallet_name = wallet_name
        self.wallet_hotkey = wallet_hotkey
        self.wallet_path = wallet_path
        self._subtensor: Any = None
        self._wallet: Any = None

    def _ensure_clients(self) -> None:
        if self._subtensor is not None:
            return
        # The bittensor SDK's Config machinery reads sys.argv via argparse
        # and tries to YAML-load whatever path follows --config. Our CLI
        # uses --config to point at /etc/cathedral/testnet.toml, which is
        # TOML, not YAML, so bt blows up. Hide our argv from bt while
        # importing/instantiating; restore after.
        import sys

        saved_argv = sys.argv
        sys.argv = sys.argv[:1]
        try:
            import bittensor as bt  # local import; heavy

            wallet_kwargs: dict[str, Any] = {
                "name": self.wallet_name,
                "hotkey": self.wallet_hotkey,
            }
            if self.wallet_path:
                wallet_kwargs["path"] = self.wallet_path
            self._wallet = bt.Wallet(**wallet_kwargs)
            self._subtensor = bt.Subtensor(network=self.network)
        finally:
            sys.argv = saved_argv

    async def metagraph(self) -> Metagraph:
        def _read() -> Metagraph:
            self._ensure_clients()
            mg = self._subtensor.metagraph(netuid=self.netuid, lite=True)
            uids = _as_list(mg.uids)
            hotkeys = list(mg.hotkeys)
            last_update = _as_list(getattr(mg, "last_update", None))
            permits = _as_list(getattr(mg, "validator_permit", None))
            stakes = _as_list(
                getattr(mg, "S", None)
                if hasattr(mg, "S")
                else getattr(mg, "stake", None)
            )
            miners = tuple(
                MinerNode(
                    uid=int(uid),
                    hotkey=str(hk),
                    last_update_block=int(lu) if i < len(last_update) else 0,
                    validator_permit=_optional_bool_at(permits, i),
                    stake=_optional_float_at(stakes, i),
                )
                for i, (uid, hk, lu) in enumerate(
                    zip(uids, hotkeys, last_update + [0] * len(uids), strict=False)
                )
            )
            block = _as_int(mg.block)
            return Metagraph(block=block, miners=miners)

        return await asyncio.to_thread(_read)

    async def validator_hotkey_ss58(self) -> str:
        def _read() -> str:
            self._ensure_clients()
            return str(self._wallet.hotkey.ss58_address)

        return await asyncio.to_thread(_read)

    async def subnet_hyperparameters(self) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            self._ensure_clients()
            if not hasattr(self._subtensor, "get_subnet_hyperparameters"):
                return {}
            raw = self._subtensor.get_subnet_hyperparameters(netuid=self.netuid)
            return _public_attrs(raw)

        return await asyncio.to_thread(_read)

    async def is_registered(self) -> bool:
        def _check() -> bool:
            self._ensure_clients()
            return bool(
                self._subtensor.is_hotkey_registered_on_subnet(
                    hotkey_ss58=self._wallet.hotkey.ss58_address,
                    netuid=self.netuid,
                )
            )

        return await asyncio.to_thread(_check)

    async def set_weights(self, weights: list[tuple[int, float]]) -> WeightStatus:
        if not weights:
            return WeightStatus.HEALTHY  # nothing to send

        def _send() -> WeightStatus:
            self._ensure_clients()
            uids = [u for u, _ in weights]
            values = [v for _, v in weights]
            try:
                from cathedral import SPEC_VERSION

                resp = self._subtensor.set_weights(
                    wallet=self._wallet,
                    netuid=self.netuid,
                    uids=uids,
                    weights=values,
                    version_key=SPEC_VERSION,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                    raise_error=False,
                )
            except Exception as e:
                return _classify_error(str(e))

            if getattr(resp, "success", False):
                return WeightStatus.HEALTHY
            return _classify_error(str(getattr(resp, "message", "")))

        return await asyncio.to_thread(_send)

    async def current_block(self) -> int:
        def _read() -> int:
            self._ensure_clients()
            return int(self._subtensor.get_current_block())

        return await asyncio.to_thread(_read)


def _classify_error(message: str) -> WeightStatus:
    lc = message.lower()
    for frag in _STAKE_BLOCK_FRAGMENTS:
        if frag in lc:
            return WeightStatus.BLOCKED_BY_STAKE
    return WeightStatus.BLOCKED_BY_TRANSACTION_ERROR


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_int(value: Any) -> int:
    if hasattr(value, "item"):
        return int(value.item())
    values = _as_list(value)
    if not values:
        return 0
    return int(values[0])


def _optional_bool_at(values: list[Any], index: int) -> bool | None:
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    return bool(value)


def _optional_float_at(values: list[Any], index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    if value is None:
        return None
    if hasattr(value, "item"):
        value = value.item()
    return float(value)


def _public_attrs(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if hasattr(value, "_asdict"):
        return {str(k): _jsonable(v) for k, v in value._asdict().items()}
    result: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception as exc:
            result[name] = f"<unreadable:{exc.__class__.__name__}>"
            continue
        if callable(attr):
            continue
        result[name] = _jsonable(attr)
    return result


def _jsonable(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value
