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
    The current Chain implementation already carries them internally, so this
    wrapper does not pass them through yet; keeping them on the boundary makes
    the future SAT-subnet/bounty routing change local to this module.
    """

    _ = (network, netuid)
    if disabled:
        return WeightStatus.DISABLED
    return await chain.set_weights(list(weights))

