"""Validator boundary for publishing weight vectors to Bittensor chain."""

from __future__ import annotations

from collections.abc import Sequence

from cathedral.chain import Chain
from cathedral.chain.client import WeightStatus

WeightVector = Sequence[tuple[int, float]]


async def publish_weight_vector(
    chain: Chain,
    weights: WeightVector,
    *,
    disabled: bool = False,
    network: str | None = None,
    netuid: int | None = None,
) -> WeightStatus:
    """Publish a normalized vector, or dry-run when weights are disabled.

    ``network`` and ``netuid`` are explicit bridge inputs for v1.3 callers.
    When the concrete chain object exposes its configured destination, the
    bridge fails closed on mismatches instead of silently publishing a vector
    to the wrong subnet.
    """

    chain_network = getattr(chain, "network", None)
    if network is not None and chain_network is not None and str(chain_network) != network:
        raise ValueError(f"chain network mismatch: chain={chain_network!r}, vector={network!r}")
    chain_netuid = getattr(chain, "netuid", None)
    if netuid is not None and chain_netuid is not None and int(chain_netuid) != int(netuid):
        raise ValueError(f"chain netuid mismatch: chain={chain_netuid!r}, vector={netuid!r}")
    if disabled:
        return WeightStatus.DISABLED
    return await chain.set_weights(list(weights))
