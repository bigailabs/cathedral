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
from .. import grading, timing

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

    def __init__(self, designated_solver_digest: str | None = None) -> None:
        # DESIGNATED SOLVER (v4 Lane A evolution): a challenge batch may NAME a
        # published Lane S solver (typically the champion) for the community to
        # run — turning Lane A into distributed evaluation/replication of Lane S
        # results and a zero-expertise on-ramp ("docker pull the champion, point
        # it at the board"). It is ADVISORY metadata on the public problem only:
        # the witness check is unchanged, so a community miner is free to use any
        # solver and is still graded purely on a certificate-checked, server-timed
        # assignment. Naming a solver never gates who may submit.
        self.designated_solver_digest = designated_solver_digest

    def mint_challenge(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        n_vars, n_clauses, tl = _TIERS.get(ctx.tier, _TIERS[1])
        cnf, _planted = gen_planted_3sat(ctx.seed, n_vars, n_clauses)
        task_id = hashlib.sha256(f"{FAMILY_ID}:{ctx.seed}:{ctx.tier}".encode()).hexdigest()[:32]
        public_input = {"cnf": cnf, "n_vars": n_vars, "n_clauses": n_clauses}
        if self.designated_solver_digest:
            # advisory only — does not change verify/score (see __init__).
            public_input["designated_solver_digest"] = self.designated_solver_digest
        problem = PublicProblem(
            task_family=FAMILY_ID, schema_version=SCHEMA_VERSION, task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input=public_input,
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
        # NOTE: no wall_ms read from the submission — speed is the validator's to
        # measure, not the miner's to claim (see scaffold.timing).
        return VerifierResult(True, Outcome.SAT, 1.0, None, {})

    def score(self, problem: PublicProblem, verifier: VerifierResult,
              *, wall_ms: float | None = None) -> ScoreResult:
        # wall_ms is SERVER-MEASURED (passed by the validator); never the miner's
        # self-report. Absent it, fall back to the limit -> no speed credit.
        wm = wall_ms if wall_ms is not None else problem.time_limit_seconds * 1000
        return grading.grade(verifier, wall_ms=wm,
                             time_limit_ms=problem.time_limit_seconds * 1000,
                             reference_ms=timing.reference_ms(problem))
