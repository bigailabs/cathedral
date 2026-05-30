"""PAR-2 scoring math (Penalized Average Runtime, adapted for a permissionless
subnet).

The classic SAT-competition metric scores a solver by its runtime across a
benchmark suite, penalising unsolved instances; lower is better. We adapt the
*spirit* (coverage across a suite of challenges dominates, speed is a graded
tiebreak) to Bittensor's higher-is-better, hardware-heterogeneous setting:

  - Each challenge ``c`` carries a value ``w_c`` (tier weight or customer
    bounty). ``w_c`` is a FIXED PER-CHALLENGE BUDGET.
  - Within a challenge, valid solvers are ranked by publisher-observed receipt
    order, but ranking is over DISTINCT OPERATORS (coldkeys), not hotkeys:
    an operator's repeated submissions collapse to its earliest position.
  - The budget ``w_c`` is split among the distinct operators by ``f(rank)``
    share, ``f(rank) = rank**(-alpha)``. ``alpha`` is the single dial from
    pure coverage (alpha=0, every solver equal) to winner-take-all
    (alpha -> inf, only rank 1 scores — today's model).
  - A miner's score ``M`` is the sum of its shares over the suite's CLOSED
    challenges; ``M`` feeds the existing normalize + apply_burn pipeline.

WHY BUDGET-SPLIT (not an absolute per-solver award): an absolute award
``w_c * f(rank)`` summed over the rank slots an operator holds GROWS with the
number of identities (Sybil rank-stacking: ~sqrt(k) at alpha=0.5, linear at
alpha=0). Fixing the per-challenge budget at ``w_c`` and ranking over
deduplicated operators makes splitting one operator into k hotkeys yield
exactly the same share — Sybil gains nothing (beyond the on-chain registration
cost of each extra coldkey, which is the real identity-scarcity backstop).

This module is pure and deterministic. Operator identity (coldkey resolution)
and the operator->UID mapping for the on-chain vector live in the validator
aggregation layer; here an "operator" is just an opaque string id.
"""

from __future__ import annotations

from collections.abc import Iterable


def f_rank(rank: int, alpha: float) -> float:
    """Rank weight ``rank**(-alpha)`` (rank is 1-indexed)."""
    if rank < 1:
        raise ValueError(f"rank must be >= 1, got {rank}")
    if alpha < 0:
        raise ValueError(f"alpha must be >= 0, got {alpha}")
    if alpha == 0.0:
        return 1.0
    return float(rank) ** (-alpha)


def operator_ranks(operators_in_receipt_order: Iterable[str]) -> dict[str, int]:
    """Map each distinct operator to its 1-indexed rank by earliest receipt.

    Repeated appearances of the same operator are ignored (only the earliest
    counts) — this is the Sybil-resistance pivot: ranking is over distinct
    operators, so extra identities/submissions do not create extra rank slots.
    """
    ranks: dict[str, int] = {}
    next_rank = 1
    for operator in operators_in_receipt_order:
        if operator in ranks:
            continue
        ranks[operator] = next_rank
        next_rank += 1
    return ranks


def challenge_shares(
    operators_in_receipt_order: Iterable[str],
    *,
    challenge_value: float,
    alpha: float,
) -> dict[str, float]:
    """Split a challenge's fixed ``challenge_value`` budget among the distinct
    valid-solving operators by ``f(rank)`` share. Returns ``{operator: share}``
    whose values sum to ``challenge_value`` (or ``{}`` if no one solved)."""
    if challenge_value < 0:
        raise ValueError(f"challenge_value must be >= 0, got {challenge_value}")
    ranks = operator_ranks(operators_in_receipt_order)
    if not ranks:
        return {}
    raw = {operator: f_rank(rank, alpha) for operator, rank in ranks.items()}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {operator: challenge_value * weight / total for operator, weight in raw.items()}


def aggregate_merit(
    challenges: Iterable[tuple[float, Iterable[str]]],
    *,
    alpha: float,
) -> dict[str, float]:
    """Sum per-operator merit over a suite of closed challenges.

    ``challenges`` is an iterable of ``(challenge_value, operators_in_receipt_order)``.
    Returns ``{operator: M}``; feed this to ``chain.weights.apply_burn`` (which
    normalizes and routes the burn share), or normalize directly.
    """
    merit: dict[str, float] = {}
    for challenge_value, operators in challenges:
        for operator, share in challenge_shares(
            operators, challenge_value=challenge_value, alpha=alpha
        ).items():
            merit[operator] = merit.get(operator, 0.0) + share
    return merit
