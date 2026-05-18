"""synthetic_maxsat_v1 — weighted Max-SAT challenge lane.

Implements ``cathedral.lanes.contract.TaskFamily``.

Read ``README.md`` in this directory before changing anything.
"""

from __future__ import annotations

from cathedral.lanes.contract import (
    GenerateCtx,
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    VerifierResult,
)

FAMILY_ID = "synthetic_maxsat_v1"
SCHEMA_VERSION = 1


class MaxsatV1:
    """Stub. The lane author fills in these three methods. The contract
    test suite at ``tests/lanes/test_contract.py`` enforces the rules;
    if it passes, the lane is mergeable."""

    family_id: str = FAMILY_ID
    schema_version: int = SCHEMA_VERSION

    def generate(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        raise NotImplementedError("synthetic_maxsat_v1.generate: implement me")

    def verify(
        self,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission: Submission,
    ) -> VerifierResult:
        raise NotImplementedError("synthetic_maxsat_v1.verify: implement me")

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        raise NotImplementedError("synthetic_maxsat_v1.score: implement me")
