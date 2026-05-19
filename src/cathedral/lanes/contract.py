"""Task Family Contract.

Every Cathedral challenge lane implements this protocol. The publisher
imports the lane in-process and calls these three pure functions. There
is no remote service, no plugin loader, no dynamic dispatch beyond a
local registry lookup.

Rules (enforced by ``tests/lanes/test_contract.py``):

  1. **Pure.** ``generate``, ``verify``, ``score`` are deterministic
     functions of their inputs. Same seed + tier => byte-identical
     ``PublicProblem`` and ``HiddenMetadata``. Same problem + submission
     => identical ``VerifierResult``.

  2. **No I/O.** Lane modules must not import ``requests``, ``httpx``,
     ``aiohttp``, ``socket``, ``urllib.request``, ``urllib3``, or any
     other network library. No file reads outside the lane's own
     ``fixtures/`` directory. No subprocess.

  3. **No clock.** No ``time.time()``, ``time.monotonic()``,
     ``datetime.now()``, ``datetime.utcnow()``. The publisher provides
     any timestamp the lane needs through ``GenerateCtx``.

  4. **No unseeded randomness.** Use a ``random.Random(seed)`` instance
     or ``numpy.random.default_rng(seed)``. Never the module-level
     ``random.random()``.

  5. **Bounded scores.** ``ScoreResult.weighted_score`` is in ``[0.0,
     1.0]``. Malformed submissions, timeouts, and parse failures score
     0.0 with a non-empty ``rejection_reason``.

  6. **Verifier is total.** ``verify`` returns a ``VerifierResult`` for
     every input. It never raises. Adversarial fixtures prove this.

  7. **Hidden stays hidden.** ``HiddenMetadata`` is held by the
     publisher only. The wire payload to miners is
     ``PublicProblem`` alone.

  8. **No miner reasoning trust.** ``verify`` evaluates only
     ``Submission.answer``. It does not read free-form text the miner
     may have included alongside the answer.

How lanes get loaded: see ``src/cathedral/lanes/registry.py`` (stub).
The publisher's ``score_and_sign`` looks up the lane by ``family_id``
and calls ``generate`` / ``verify`` / ``score`` in that order.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Wire schemas (all Pydantic v2 per cathedral conventions)
# --------------------------------------------------------------------------


class GenerateCtx(BaseModel):
    """Inputs to ``TaskFamily.generate``. The publisher fills this; the
    lane never reads the host clock or environment directly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int = Field(description="Deterministic seed. Same seed -> identical output.")
    tier: int = Field(ge=0, description="Difficulty tier index. Lane defines the scale.")
    issued_at_iso: str = Field(
        description="Publisher-supplied UTC timestamp (ms ISO-8601). "
        "Use this if the lane needs a 'now'; do not call the clock.",
    )


class PublicProblem(BaseModel):
    """The portion of a generated challenge that the miner sees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_family: str = Field(description="Lane family_id, e.g. 'synthetic_boolean_v1'.")
    schema_version: int = Field(ge=1, description="Public-payload schema version.")
    task_id: str = Field(description="Unique id for this (seed, tier) instance.")
    difficulty_tier: int = Field(ge=0)
    public_input: dict[str, Any] = Field(
        description="Lane-defined payload. Must be JSON-serializable.",
    )
    time_limit_seconds: int = Field(gt=0, description="Server-enforced wall clock.")


class HiddenMetadata(BaseModel):
    """The portion of a generated challenge held by the publisher only.

    Whatever the verifier needs that the miner must NOT see (optimal
    assignment, unsat proof, generator state) goes here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    generator_version: str = Field(description="Lane's internal generator version.")
    hidden_payload: dict[str, Any] = Field(
        description="Lane-defined hidden data. JSON-serializable.",
    )


class Submission(BaseModel):
    """A miner answer arriving at the publisher."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    miner_hotkey: str
    answer: dict[str, Any] = Field(description="Lane-defined answer payload.")


class VerifierResult(BaseModel):
    """Output of ``TaskFamily.verify``. Pure data; no rendering or scoring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parsed_ok: bool = Field(description="False -> submission was malformed.")
    raw_metric: float = Field(
        description=(
            "Lane-defined raw quality (e.g. satisfied_weight / total_weight, "
            "or solved_weight / hidden_optimum). [0.0, +inf) but typically "
            "[0.0, 1.0]. Score normalization happens in ``score``."
        ),
    )
    rejection_reason: str | None = Field(
        default=None,
        description="Set when parsed_ok=False or the answer was rejected for "
        "any non-quality reason (e.g. 'partial_assignment').",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Lane-specific audit data (satisfied_count, optimum, etc.).",
    )


class ScoreResult(BaseModel):
    """Final score that the publisher writes to eval_runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    weighted_score: float = Field(ge=0.0, le=1.0)
    rejection_reason: str | None = None
    score_parts: dict[str, float] = Field(
        default_factory=dict,
        description="Optional breakdown for the public excerpt.",
    )


# --------------------------------------------------------------------------
# The protocol
# --------------------------------------------------------------------------


@runtime_checkable
class TaskFamily(Protocol):
    """A Cathedral challenge lane. Implementations live in
    ``src/cathedral/lanes/<family_id>/__init__.py``.

    All three methods MUST be pure functions of their inputs. See the
    module docstring for the full rules and the CI gate that enforces
    them.
    """

    family_id: str
    schema_version: int

    def generate(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        """Deterministically produce one challenge instance."""
        ...

    def verify(
        self,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission: Submission,
    ) -> VerifierResult:
        """Evaluate the submission. Total function. Never raises."""
        ...

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        """Normalize the verifier's raw_metric into a bounded weighted_score
        using the lane's difficulty/tier policy."""
        ...
