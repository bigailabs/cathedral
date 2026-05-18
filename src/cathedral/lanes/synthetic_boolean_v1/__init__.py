"""synthetic_boolean_v1 -- boolean challenge lane.

v1 formulation: random 3-SAT with a planted satisfying assignment, scored
binary (1.0 correct / 0.0 otherwise). DIMACS CNF on the wire, solver-style
``s SATISFIABLE`` / ``v ... 0`` answers on the way back.

See ``DESIGN.md`` for the formulation, ``README.md`` for what a lane author
extending this would change, ``problem.py`` for the generator, ``verifier.py``
for the verifier, and ``dimacs.py`` for the parsers.

Implements ``cathedral.lanes.contract.TaskFamily``.
"""

from __future__ import annotations

from typing import Any

from cathedral.lanes.contract import (
    GenerateCtx,
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    VerifierResult,
)
from cathedral.lanes.synthetic_boolean_v1.problem import (
    generate_challenge,
    generator_version,
)
from cathedral.lanes.synthetic_boolean_v1.verifier import verify_sat_submission

FAMILY_ID = "synthetic_boolean_v1"
SCHEMA_VERSION = 1


class SyntheticBooleanV1:
    """The v1 boolean challenge lane. Pure, deterministic, zero-I/O."""

    family_id: str = FAMILY_ID
    schema_version: int = SCHEMA_VERSION

    def generate(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        challenge = generate_challenge(ctx.seed, ctx.tier)
        public_input: dict[str, Any] = {
            "format": "dimacs_cnf",
            "dimacs": challenge.dimacs_text,
            "num_vars": challenge.num_vars,
            "num_clauses": challenge.num_clauses,
            "answer_format": "solver_output",
            # Brief miner-facing instruction. Verbatim, no surprises.
            "instructions": (
                "Solve the DIMACS CNF formula and return your answer as a "
                "JSON object with key 'solver_output' containing the "
                "solver-style block: a line 's SATISFIABLE' followed by "
                "one or more lines 'v <lit> <lit> ... 0' assigning every "
                "variable. Alternatively submit "
                "{'assignment': {'1': true, '2': false, ...}} with a "
                "complete boolean assignment."
            ),
        }
        public = PublicProblem(
            task_family=self.family_id,
            schema_version=self.schema_version,
            task_id=challenge.task_id,
            difficulty_tier=challenge.difficulty_tier,
            public_input=public_input,
            time_limit_seconds=challenge.time_limit_seconds,
        )
        hidden = HiddenMetadata(
            task_id=challenge.task_id,
            generator_version=generator_version(),
            hidden_payload={
                # The planted assignment. Sufficient witness to prove
                # satisfiability and to verify any equivalent assignment.
                "planted_assignment": {
                    str(var): bool(val) for var, val in challenge.planted_assignment.items()
                },
                # Stored so an offline auditor can re-derive the same
                # CnfFormula from the seed without re-running the
                # generator. The verifier does not need this.
                "num_vars": challenge.num_vars,
                "num_clauses": challenge.num_clauses,
            },
        )
        return public, hidden

    def verify(
        self,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission: Submission,
    ) -> VerifierResult:
        # Verify against the public DIMACS text. The hidden planted
        # assignment is NOT used: any satisfying assignment is accepted,
        # not just the planted one. This is the correct SAT semantics
        # and prevents the verifier from gaming miners into guessing
        # our generator's witness.
        dimacs_text = problem.public_input.get("dimacs")
        if not isinstance(dimacs_text, str):
            return VerifierResult(
                parsed_ok=False,
                raw_metric=0.0,
                rejection_reason="malformed_public_input",
                details={"reason": "public_input.dimacs missing or not a string"},
            )

        result = verify_sat_submission(dimacs_text, submission.answer)
        return VerifierResult(
            parsed_ok=result.parsed_ok,
            raw_metric=result.weighted_score,
            rejection_reason=result.rejection_reason,
            details=dict(result.details),
        )

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        # Binary v1: clamp raw_metric to {0.0, 1.0}. Anything below 1.0
        # is a miss; only a full satisfying assignment scores.
        if not verifier.parsed_ok:
            return ScoreResult(
                weighted_score=0.0,
                rejection_reason=verifier.rejection_reason or "rejected",
                score_parts={},
            )
        if verifier.raw_metric >= 1.0:
            return ScoreResult(
                weighted_score=1.0,
                rejection_reason=None,
                score_parts={"binary": 1.0},
            )
        return ScoreResult(
            weighted_score=0.0,
            rejection_reason=verifier.rejection_reason or "unsatisfied",
            score_parts={"binary": 0.0},
        )
