"""synthetic_boolean_v1 -- boolean challenge lane.

Implements ``cathedral.lanes.contract.TaskFamily``. The lane consumes
a DIMACS CNF problem and a solver-style DIMACS solution, scores 1.0
for a valid satisfying assignment and 0.0 for anything else, with a
clear rejection reason.

Generation in this first pass draws from a small in-tree toy corpus
keyed by tier. Real launch challenges are sourced via the
publisher-side private challenge source (see
``cathedral.lanes.challenge_source``) and never enter this lane
package. The lane only exposes a deterministic ``(seed, tier) ->
PublicProblem`` map suitable for tests and the contract gate.
"""

from __future__ import annotations

import hashlib

from cathedral.lanes.challenge_source import ChallengeRecord
from cathedral.lanes.contract import (
    GenerateCtx,
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    VerifierResult,
)
from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    assignment_covers_all_vars,
    assignment_in_range,
    evaluate_assignment,
    parse_dimacs_cnf,
    parse_dimacs_solution,
)
from cathedral.lanes.synthetic_boolean_v1.toy_corpus import ToyInstance, toy_instance_for

FAMILY_ID = "synthetic_boolean_v1"
SCHEMA_VERSION = 1

DEFAULT_TIME_LIMIT_SECONDS = 60


def _task_id_for(instance: ToyInstance, seed: int, tier: int) -> str:
    digest = hashlib.sha256(f"{FAMILY_ID}:{instance.name}:{seed}:{tier}".encode()).hexdigest()
    return digest[:32]


def problem_from_challenge_record(
    record: ChallengeRecord,
    *,
    public_base_url: str,
    fetch_token: str,
    time_limit_seconds: int = DEFAULT_TIME_LIMIT_SECONDS,
) -> tuple[PublicProblem, HiddenMetadata]:
    """Convert a publisher-private active challenge row into lane inputs.

    The CNF body does NOT appear in :class:`PublicProblem`. Instead the
    public projection carries a fetch URL and the sha256 of the CNF the
    miner is expected to retrieve from that URL. The verifier reads the
    CNF body out of :class:`HiddenMetadata`, where it sits behind the
    publisher boundary.

    Arguments:
        public_base_url: Base URL (no trailing slash) of the public
            publisher surface, e.g. ``https://api.cathedral.computer``.
            Combined with the challenge id and ``fetch_token`` to form
            the miner-facing URL. A trailing slash is tolerated.
        fetch_token: Unguessable per-announcement token from
            :class:`SqliteFetchTokenStore`. Embedded as ``?t=`` in the
            URL. Without it, the endpoint must respond 404 to the same
            request shape an unknown id receives.
    """
    if not public_base_url:
        raise ValueError("public_base_url is required for the CNF URL transport")
    if not fetch_token:
        raise ValueError("fetch_token is required for the CNF URL transport")
    parsed = parse_dimacs_cnf(record.cnf_text)
    base = public_base_url.rstrip("/")
    cnf_url = f"{base}/v1/challenges/{record.challenge_id}/cnf?t={fetch_token}"
    cnf_sha256 = hashlib.sha256(record.cnf_text.encode("utf-8")).hexdigest()
    public = PublicProblem(
        task_family=record.family_id,
        schema_version=SCHEMA_VERSION,
        task_id=record.challenge_id,
        difficulty_tier=record.tier,
        public_input={
            "format": "dimacs",
            "cnf_url": cnf_url,
            "cnf_sha256": cnf_sha256,
            "num_vars": parsed.num_vars if parsed.ok else 0,
            "num_clauses": len(parsed.clauses) if parsed.ok else 0,
        },
        time_limit_seconds=time_limit_seconds,
    )
    hidden = HiddenMetadata(
        task_id=record.challenge_id,
        generator_version="challenge_source",
        hidden_payload={
            "source": "challenge_source",
            "challenge_id": record.challenge_id,
            "status": record.status,
            "audit_metadata": dict(record.audit_metadata),
            "cnf_rejection_reason": parsed.rejection_reason,
            # CNF body lives in hidden so the verifier can reach it
            # without the public projection carrying it on the wire.
            "cnf": record.cnf_text,
        },
    )
    return public, hidden


class SyntheticBooleanV1:
    """Binary SAT lane.

    Score policy:

    * Correct satisfying assignment, well-formed DIMACS solution covering
      every variable: ``weighted_score = 1.0``.
    * Malformed CNF, malformed solution, incomplete assignment,
      contradictory assignment, out-of-range variable, ``UNSATISFIABLE``
      or ``UNKNOWN`` status, or unsatisfied clause: ``weighted_score =
      0.0`` with ``rejection_reason``.
    """

    family_id: str = FAMILY_ID
    schema_version: int = SCHEMA_VERSION

    def generate(self, ctx: GenerateCtx) -> tuple[PublicProblem, HiddenMetadata]:
        instance = toy_instance_for(ctx.tier)
        task_id = _task_id_for(instance, ctx.seed, ctx.tier)
        public = PublicProblem(
            task_family=self.family_id,
            schema_version=self.schema_version,
            task_id=task_id,
            difficulty_tier=ctx.tier,
            public_input={
                "format": "dimacs",
                "cnf": instance.cnf_text,
                "num_vars": instance.num_vars,
                "num_clauses": instance.num_clauses,
            },
            time_limit_seconds=DEFAULT_TIME_LIMIT_SECONDS,
        )
        # ``hidden_payload`` deliberately does NOT carry the witness on
        # the wire. The verifier scores by re-evaluating the submitted
        # assignment against the CNF, not by string-matching against a
        # planted answer. We keep a small marker so future planted
        # generators can flag witness-known instances without changing
        # the schema; the marker is publisher-private and never reaches
        # the public projection.
        hidden = HiddenMetadata(
            task_id=task_id,
            generator_version=f"toy:{instance.name}",
            hidden_payload={
                "source": "toy_corpus",
                "known_satisfiable": True,
            },
        )
        return public, hidden

    def verify(
        self,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission: Submission,
    ) -> VerifierResult:
        # Never raises. Every failure becomes a structured rejection.
        try:
            # Hidden first: the challenge-source runtime path stashes the
            # CNF body in hidden_payload so the public projection only
            # carries a fetch URL. Falls back to public_input["cnf"] for
            # the toy/test path where the CNF is small enough to inline.
            # (Indexing rather than .get(...) is deliberate: the lane
            # contract test bans .get call sites because they overlap
            # statically with HTTP client surfaces.)
            hidden_payload = (
                hidden.hidden_payload if isinstance(hidden.hidden_payload, dict) else {}
            )
            hidden_cnf = hidden_payload["cnf"] if "cnf" in hidden_payload else None
            public_cnf = problem.public_input["cnf"] if "cnf" in problem.public_input else None
            cnf_text = hidden_cnf if isinstance(hidden_cnf, str) else public_cnf
            if not isinstance(cnf_text, str):
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="problem_missing_cnf",
                    details={},
                )
            cnf = parse_dimacs_cnf(cnf_text)
            if not cnf.ok:
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason=cnf.rejection_reason or "cnf_unparseable",
                    details={},
                )

            answer = submission.answer
            if not isinstance(answer, dict):
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="answer_not_object",
                    details={},
                )
            raw_solution = answer["dimacs_solution"] if "dimacs_solution" in answer else None
            if not isinstance(raw_solution, str):
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="answer_missing_dimacs_solution",
                    details={},
                )
            if set(answer) != {"dimacs_solution"}:
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="answer_unexpected_keys",
                    details={"keys": sorted(str(k) for k in answer)},
                )

            solution = parse_dimacs_solution(raw_solution)
            if not solution.ok:
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason=solution.rejection_reason or "solution_unparseable",
                    details={"status": solution.status},
                )

            if not assignment_in_range(cnf, solution.assignment):
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="solution_variable_out_of_range",
                    details={
                        "clause_count": len(cnf.clauses),
                        "num_vars": cnf.num_vars,
                    },
                )
            if not assignment_covers_all_vars(cnf, solution.assignment):
                return VerifierResult(
                    parsed_ok=False,
                    raw_metric=0.0,
                    rejection_reason="solution_incomplete_assignment",
                    details={
                        "clause_count": len(cnf.clauses),
                        "num_vars": cnf.num_vars,
                        "assigned": len(solution.assignment),
                    },
                )

            satisfied, satisfied_count, clause_count = evaluate_assignment(cnf, solution.assignment)
            if not satisfied:
                return VerifierResult(
                    parsed_ok=True,
                    raw_metric=0.0,
                    rejection_reason="solution_unsatisfied",
                    details={
                        "clauses_satisfied": satisfied_count,
                        "clause_count": clause_count,
                    },
                )

            return VerifierResult(
                parsed_ok=True,
                raw_metric=1.0,
                rejection_reason=None,
                details={
                    "clauses_satisfied": satisfied_count,
                    "clause_count": clause_count,
                },
            )
        except Exception as exc:
            return VerifierResult(
                parsed_ok=False,
                raw_metric=0.0,
                rejection_reason="verifier_internal_error",
                details={"error_type": type(exc).__name__},
            )

    def score(self, problem: PublicProblem, verifier: VerifierResult) -> ScoreResult:
        if not verifier.parsed_ok:
            return ScoreResult(
                weighted_score=0.0,
                rejection_reason=verifier.rejection_reason or "rejected",
                score_parts={"binary_correct": 0.0},
            )
        if verifier.rejection_reason:
            return ScoreResult(
                weighted_score=0.0,
                rejection_reason=verifier.rejection_reason,
                score_parts={"binary_correct": 0.0},
            )
        clamped = max(0.0, min(1.0, float(verifier.raw_metric)))
        binary = 1.0 if clamped >= 1.0 else 0.0
        if binary < 1.0:
            return ScoreResult(
                weighted_score=0.0,
                rejection_reason="solution_unsatisfied",
                score_parts={"binary_correct": 0.0},
            )
        return ScoreResult(
            weighted_score=1.0,
            rejection_reason=None,
            score_parts={"binary_correct": 1.0},
        )


__all__ = [
    "DEFAULT_TIME_LIMIT_SECONDS",
    "FAMILY_ID",
    "SCHEMA_VERSION",
    "SyntheticBooleanV1",
    "problem_from_challenge_record",
]
