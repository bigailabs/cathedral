"""Deterministic generator for v1 boolean (DIMACS SAT) challenges.

The v1 formulation:

* Random 3-SAT with a **planted satisfying assignment**.
* Tier maps to ``(num_vars, num_clauses)``. Tier 0 is tiny, intended
  for smoke tests and validator-side verifier cost budgeting. Higher
  tiers are larger but still satisfiable by construction.
* Generation is fully deterministic in ``(seed, tier)``: ``random.Random
  (seed_for_tier(seed, tier))`` drives every choice.

Out of scope for v1:

* UNSAT formulas (need DRAT/LRAT proof I/O before we can verify them).
* Max-SAT / partial-credit scoring (a future tier could add it under a
  schema bump; v1 sticks to binary SAT).
* Bounded CSP / pseudo-boolean. Plug-shape allows it; v1 generator
  doesn't ship it.

The generator never calls the network, the filesystem, the clock, or
unseeded randomness. The lane's contract test asserts these via the
banned-imports walk.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from cathedral.lanes.synthetic_boolean_v1.dimacs import CnfFormula, serialize_dimacs_cnf

# Per-tier parameters. Conservative: tier 0 is verifier-trivial; tier 5
# is still small enough to verify in microseconds. The mining cost
# grows with num_vars and clause/variable ratio, the verification cost
# stays linear in num_clauses.
_TIER_PARAMS: tuple[tuple[int, int], ...] = (
    # (num_vars, num_clauses)
    (10, 30),  # tier 0  -- smoke
    (20, 80),  # tier 1
    (40, 170),  # tier 2
    (80, 340),  # tier 3
    (160, 680),  # tier 4
    (320, 1360),  # tier 5  -- ratio 4.25 (sat phase), still trivial to verify
)

_CLAUSE_WIDTH = 3  # 3-SAT
_GENERATOR_VERSION = "synthetic_boolean_v1/dimacs-sat/planted/1"

# Cap on total clauses we will ever ship in v1. Verifier work is linear
# in clause count and we want a hard ceiling so a misconfigured tier
# can't DoS the validator's verification cost budget.
_MAX_CLAUSES = 4096


@dataclass(frozen=True)
class GeneratedChallenge:
    """Internal generator output. Adapted into the contract's
    ``PublicProblem`` / ``HiddenMetadata`` by the lane class."""

    task_id: str
    difficulty_tier: int
    num_vars: int
    num_clauses: int
    dimacs_text: str
    planted_assignment: dict[int, bool]
    time_limit_seconds: int


def _seed_for_tier(seed: int, tier: int) -> int:
    """Derive a tier-local seed deterministically. We hash so that
    nearby ``seed`` values produce uncorrelated formulas across tiers."""
    h = hashlib.blake2b(digest_size=8)
    h.update(seed.to_bytes(8, "big", signed=True))
    h.update(tier.to_bytes(4, "big"))
    return int.from_bytes(h.digest(), "big")


def _task_id_for(seed: int, tier: int) -> str:
    """Stable task_id. Hex digest of (generator_version, seed, tier).
    Same (seed, tier) -> same task_id forever, which is what the
    contract's determinism test requires."""
    h = hashlib.blake2b(digest_size=16)
    h.update(_GENERATOR_VERSION.encode("utf-8"))
    h.update(seed.to_bytes(8, "big", signed=True))
    h.update(tier.to_bytes(4, "big"))
    return h.hexdigest()


def _tier_params(tier: int) -> tuple[int, int]:
    """Look up (num_vars, num_clauses) for a tier. Tiers beyond the
    table clamp to the max tier; we never expand silently because the
    verifier-cost cap matters."""
    if tier < 0:
        raise ValueError(f"tier must be >= 0, got {tier}")
    clamped_tier = min(tier, len(_TIER_PARAMS) - 1)
    nv, nc = _TIER_PARAMS[clamped_tier]
    if nc > _MAX_CLAUSES:
        raise ValueError(f"tier {tier} requests {nc} clauses, exceeds _MAX_CLAUSES={_MAX_CLAUSES}")
    return nv, nc


def _generate_planted_sat(
    rng: random.Random, num_vars: int, num_clauses: int
) -> tuple[CnfFormula, dict[int, bool]]:
    """Generate a random 3-SAT instance with a planted satisfying
    assignment.

    Approach:
      1. Pick a random assignment A over ``num_vars`` variables.
      2. Build each clause by sampling 3 distinct variables, randomly
         polarizing, then -- if the clause is not satisfied by A --
         flipping one literal so it is.

    Result is guaranteed satisfiable by ``A``. Other satisfying
    assignments may also exist; the verifier accepts any of them.
    """
    planted: dict[int, bool] = {v: rng.choice((True, False)) for v in range(1, num_vars + 1)}

    clauses: list[tuple[int, ...]] = []
    for _ in range(num_clauses):
        if num_vars < _CLAUSE_WIDTH:
            raise ValueError(f"num_vars={num_vars} too small for {_CLAUSE_WIDTH}-SAT")
        vars_in_clause = rng.sample(range(1, num_vars + 1), _CLAUSE_WIDTH)
        literals = [v if rng.random() < 0.5 else -v for v in vars_in_clause]

        # Is this clause satisfied by the planted assignment?
        def _clause_satisfied_by_planted(lits: list[int]) -> bool:
            for lit in lits:
                value = planted[abs(lit)]
                literal_true = value if lit > 0 else not value
                if literal_true:
                    return True
            return False

        if not _clause_satisfied_by_planted(literals):
            # Flip a random literal so the planted assignment satisfies it.
            idx = rng.randrange(_CLAUSE_WIDTH)
            literals[idx] = -literals[idx]

        clauses.append(tuple(literals))

    return CnfFormula(num_vars=num_vars, num_clauses=num_clauses, clauses=tuple(clauses)), planted


def generate_challenge(seed: int, tier: int) -> GeneratedChallenge:
    """Top-level generator entry. Deterministic in ``(seed, tier)``."""
    num_vars, num_clauses = _tier_params(tier)
    # S311: seeded PRNG by design. The contract requires byte-identical
    # output for the same (seed, tier); a cryptographic RNG would break
    # determinism. This is not a security-sensitive context.
    rng = random.Random(_seed_for_tier(seed, tier))  # noqa: S311
    formula, planted = _generate_planted_sat(rng, num_vars, num_clauses)

    return GeneratedChallenge(
        task_id=_task_id_for(seed, tier),
        difficulty_tier=tier,
        num_vars=num_vars,
        num_clauses=num_clauses,
        dimacs_text=serialize_dimacs_cnf(formula),
        planted_assignment=planted,
        # Per-tier wall-clock; even tier 5 is trivial for kissat. The
        # publisher may override this via context in a later schema
        # bump; v1 hard-codes a generous default.
        time_limit_seconds=60 * (tier + 1),
    )


def generator_version() -> str:
    return _GENERATOR_VERSION
