"""The emissions economy — the fun layer. Breaking a target drains its bounty
pool to the breacher; verified work splits the round emission pool by weight;
agents climb ranks. Surfaced as the arcade UI (ticker, breach feed, ranks).

This is presentation/economy on top of the real `reward = metric x gate` vector
— emissions are a monotone function of the verified weight, never a new trust path.
"""
from __future__ import annotations

from .models import Target

ROUND_EMISSION_POOL = 1000.0     # tau split across verified agents by weight
PROVENANCE_MULT = {"A": 1.0, "B": 0.85, "B-": 0.7, "C": 0.5, "F": 0.0}

RANKS = [
    (0, "Initiate"), (150, "Prober"), (400, "Breacher"),
    (900, "Exploiter"), (1800, "Warlord"), (3500, "Cathedral Breaker"),
]


def target_bounty(t: Target) -> float:
    """A subnet's bounty pool — bigger for higher candidate severity."""
    return round(60.0 * t.severity, 1)


def chain_vault_bounty(tier: str, result: str) -> float:
    """A money-math/chain CNF vault bounty by difficulty; open if still uncracked."""
    base = {"trivial": 20, "board": 80, "mid": 240, "hard": 600}.get(tier, 80)
    return float(base if result in ("sat", "unknown") else base // 2)


def rank_for(emissions: float) -> str:
    name = RANKS[0][1]
    for threshold, label in RANKS:
        if emissions >= threshold:
            name = label
    return name


def next_rank(emissions: float) -> tuple[str, float]:
    """(next rank label, emissions remaining) — for the season XP bar."""
    for threshold, label in RANKS:
        if emissions < threshold:
            return label, round(threshold - emissions, 1)
    return "MAX", 0.0
