"""Lane A — SAT challenge. The existing SN39 mechanism, cleanly factored.

Publish a CNF; miners solve; fastest valid satisfying assignment wins. This is
synthetic_boolean_v1 reduced to the contract: a deterministic (seed, tier) CNF,
an independent witness check, a speed-aware score.

REAL: CNF generation + witness verification. The witness is discarded after
generation (sat-generator-contract.md) — verify re-checks the assignment
directly against the CNF, so the lane holds no secret a miner could want.
"""
from __future__ import annotations

import hashlib

from ..contract import (
    GenerateCtx, HiddenMetadata, Outcome, PublicProblem, ScoreResult,
    Submission, VerifierResult,
)
from ..dimacs import gen_planted_3sat, verify_witness
from .. import grading

FAMILY_ID = "sat_challenge_v1"
SCHEMA_VERSION = 1

# tier -> (n_vars, n_clauses, time_limit_seconds). Calibrated so the planted
# instance is solvable; bigger tiers are wider.
_TIERS = {
    0: (20, 80, 60),
    1: (60, 255, 300),
    2: (120, 510, 1800),
}


class SatChallengeLane:
    family_id = FAMILY_ID
    schema_version = SCHEMA_VERSION

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        n_vars, n_clauses, tl = _TIERS.get(ctx.tier, _TIERS[1])
        cnf, _planted = gen_planted_3sat(ctx.seed, n_vars, n_clauses)
        task_id = hashlib.sha256(f"{FAMILY_ID}:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={"cnf": cnf, "n_vars": n_vars, "n_clauses": n_clauses},
            time_limit_seconds=tl,
        )
        # witness discarded; hidden carries only audit shape (no secret needed)
        hidden = HiddenMetadata(task_id=task_id, generator_version="planted-3sat/1",
                                hidden_payload={"n_vars": n_vars})
        return problem, hidden

    def validate_submission(
        self, problem: PublicProblem, hidden: HiddenMetadata, submission: Submission
    ) -> VerifierResult:
        ans = submission.answer
        assignment = ans.get("assignment")
        if not isinstance(assignment, list) or not all(isinstance(x, int) for x in assignment):
            return VerifierResult(False, Outcome.INVALID, 0.0, "no_integer_assignment")
        ok = verify_witness(problem.public_input["cnf"], assignment)
        if not ok:
            return VerifierResult(True, Outcome.INVALID, 0.0, "assignment_does_not_satisfy")
        wall_ms = float(ans.get("wall_ms", problem.time_limit_seconds * 1000))
        return VerifierResult(True, Outcome.SAT, 1.0, None, {"wall_ms": wall_ms})

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        wall_ms = float(verifier.details.get("wall_ms", 0.0))
        return grading.grade(verifier, wall_ms=wall_ms,
                             time_limit_ms=problem.time_limit_seconds * 1000)
