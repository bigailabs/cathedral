"""Solver pinning — the v0/v1 finding made structural: difficulty is the triple
(property, width, SOLVER), and the solver is part of the pin, not an afterthought.

The same property at the same width is seconds on one solver and never on
another (the EVM-SMT cliff write-up: theory/encoding choice IS the difficulty
dial). So a challenge that doesn't record which solver+version+config it was
calibrated against is not reproducible. This records it; the validator stamps
every minted challenge with its pin.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SolverPin:
    solver: str            # e.g. "cadical", "kissat", "cryptominisat5", "bitwuzla"
    version: str           # exact version string (calibration is version-specific)
    config: str = "default"

    def key(self) -> str:
        return f"{self.solver}-{self.version}-{self.config}"


@dataclass(frozen=True)
class Difficulty:
    """Difficulty is (property, width, solver) — never width alone."""

    property_id: str       # which property/family this instance encodes
    width: int             # bit-width / n_vars / tier-scale knob
    pin: SolverPin


# Reference pins the scaffold calibrates against. Real deployment reads these
# from the generator's self-test metadata (sat-generator-contract.md).
REFERENCE_PINS = {
    "sat": SolverPin("cadical", "1.9.5"),
    "solver_docker": SolverPin("miner-image", "by-digest"),  # MRTD pins the version
    "encoding": SolverPin("bitwuzla", "0.7.0"),
}
