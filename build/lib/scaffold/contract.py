"""The Lane contract — mirrors cathedral's src/cathedral/lanes/contract.py.

Every lane is a self-contained challenge mechanism implementing three PURE
functions of their inputs:

    mint_challenge (generate) -> (PublicProblem, HiddenMetadata)
    validate_submission (verify) -> VerifierResult     # total; never raises
    score -> ScoreResult                                # bounded [0, 1]

Rules carried over from cathedral's contract (kept deliberately, not re-derived):
  * pure / deterministic: same (seed, tier) -> identical problem
  * no I/O, no clock, no unseeded randomness inside a lane
  * hidden stays hidden: miners see PublicProblem only
  * no miner-reasoning trust: verify reads submission.answer, never free text

This module is stdlib-only (dataclasses) so the scaffold runs with zero
third-party deps. Cathedral's real contract uses pydantic v2; the shapes match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class Outcome(str, Enum):
    """The three-outcome grade shared across every lane (shared logic, not
    per-lane copies — see grading.py)."""

    SAT = "sat"          # a satisfying witness was produced and self-verified
    UNSAT = "unsat"      # an unsatisfiability proof cert was produced + checked
    TIMEOUT = "timeout"  # neither closed within the lane's time limit
    INVALID = "invalid"  # malformed / rejected submission


@dataclass(frozen=True)
class GenerateCtx:
    """Publisher-supplied inputs to mint_challenge. The lane never reads the
    host clock or env directly — any 'now' it needs arrives here."""

    seed: int
    tier: int
    issued_at_iso: str


@dataclass(frozen=True)
class PublicProblem:
    """The miner-visible portion of a minted challenge."""

    task_family: str
    schema_version: int
    task_id: str
    difficulty_tier: int
    public_input: dict[str, Any]
    time_limit_seconds: int


@dataclass(frozen=True)
class HiddenMetadata:
    """Held by the publisher only — optimal assignment, planted-bug key,
    generator state. Never crosses the wire to a miner."""

    task_id: str
    generator_version: str
    hidden_payload: dict[str, Any]


@dataclass(frozen=True)
class Submission:
    """A miner answer arriving at the publisher. Lane-defined `answer`."""

    task_id: str
    miner_hotkey: str
    answer: dict[str, Any]


@dataclass(frozen=True)
class VerifierResult:
    """Output of validate_submission. Pure data — no scoring, no rendering."""

    parsed_ok: bool
    outcome: Outcome
    raw_metric: float                       # lane-defined quality, typically [0,1]
    rejection_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreResult:
    """Final bounded score the validator writes + folds into weights."""

    weighted_score: float                   # [0.0, 1.0]
    rejection_reason: str | None = None
    score_parts: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class Lane(Protocol):
    """A Cathedral challenge lane. Adding/removing a lane touches neither the
    core nor the other lanes (registry is the only seam)."""

    family_id: str
    schema_version: int

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        ...

    def validate_submission(
        self, problem: PublicProblem, hidden: HiddenMetadata, submission: Submission
    ) -> VerifierResult:
        ...

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        ...
