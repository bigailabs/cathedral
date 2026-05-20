"""Tiny in-process toy corpus for synthetic_boolean_v1.

These are deliberately trivial known-satisfiable 3-SAT instances. They
exist for tests, contract gates, and the first wiring pass. Real launch
challenges flow through the local/private challenge source abstraction
in ``challenge_source.py`` and are NEVER stored in this file.

Each entry includes the CNF text and a known satisfying assignment so
verifier tests can check a positive path without needing a solver.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToyInstance:
    name: str
    cnf_text: str
    witness: tuple[int, ...]  # signed literals covering every variable
    num_vars: int
    num_clauses: int


# Tier 0: one variable, one clause. Trivial sanity case.
_TIER0 = ToyInstance(
    name="toy_tier0_v1c1",
    cnf_text="p cnf 1 1\n1 0\n",
    witness=(1,),
    num_vars=1,
    num_clauses=1,
)

# Tier 1: three variables, three clauses. Satisfiable by x1=T, x2=T, x3=T.
_TIER1 = ToyInstance(
    name="toy_tier1_v3c3",
    cnf_text="p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n",
    witness=(1, 2, 3),
    num_vars=3,
    num_clauses=3,
)

# Tier 2: four variables, six clauses. Satisfiable by x1=T, x2=F, x3=T, x4=T.
_TIER2 = ToyInstance(
    name="toy_tier2_v4c6",
    cnf_text=("p cnf 4 6\n1 -2 3 0\n-1 2 -3 4 0\n1 3 -4 0\n-2 3 4 0\n1 -4 0\n-2 -4 0\n"),
    witness=(1, -2, 3, 4),
    num_vars=4,
    num_clauses=6,
)

_BY_TIER: dict[int, ToyInstance] = {
    0: _TIER0,
    1: _TIER1,
    2: _TIER2,
}


def toy_instance_for(tier: int) -> ToyInstance:
    """Return a deterministic toy instance for the given tier.

    Tiers above the highest known toy fold back to the top tier. Tiers
    below zero are not possible because the contract enforces ``tier >= 0``.
    """
    if tier in _BY_TIER:
        return _BY_TIER[tier]
    highest = max(_BY_TIER)
    return _BY_TIER[highest]


__all__ = [
    "ToyInstance",
    "toy_instance_for",
]
