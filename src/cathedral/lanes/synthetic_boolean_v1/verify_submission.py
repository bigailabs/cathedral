"""Pure DIMACS submission verifier for the PR5 solve-on-submit path.

This wraps :mod:`cathedral.lanes.synthetic_boolean_v1.dimacs` with a
result object the publisher's submit handler can consume directly.

No I/O, no clock, no DB writes. The handler does the side-effects after
this function decides whether the miner's payload is correct.

Result classes mirror the spec's HTTP error vocabulary so the handler
can map ``Result.error_code`` straight onto a 400 detail string.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cathedral.lanes.synthetic_boolean_v1 import dimacs


@dataclass(frozen=True)
class SubmissionVerification:
    """Outcome of verifying a miner's DIMACS solution against a CNF.

    ``ok`` is true iff the solution parsed cleanly, the assignment was
    complete, and every clause was satisfied. ``error_code`` is one of
    the dimacs-module error codes (``solution_unsatisfied``,
    ``solution_incomplete_assignment``, ``solution_non_integer_literal``,
    ``solution_status_unsatisfiable``, ``solution_status_unknown``, etc.)
    when ``ok`` is false. ``error_code`` is ``"malformed_answer"`` if the
    solution body didn't even parse as DIMACS.

    ``dimacs_solution_sha256`` is the SHA-256 of the solution text
    verifier received, in lowercase hex. The signed claim verifier uses
    this to bind the miner's signature to this exact body.
    """

    ok: bool
    error_code: str | None
    dimacs_solution_sha256: str
    num_vars: int
    num_clauses: int


def sha256_hex(text: str) -> str:
    """Lowercase SHA-256 hex of a string (utf-8). Pure helper."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Map dimacs verifier error codes to the spec's narrower public set.
# Anything not in this map falls through to its raw code (which is fine
# since the spec lists "malformed_answer | solution_empty |
# solution_incomplete_assignment | ..." as the union).
_PUBLIC_ERROR_CODES: frozenset[str] = frozenset(
    {
        "malformed_answer",
        "solution_empty",
        "solution_oversized",
        "solution_invalid_character",
        "solution_not_a_string",
        "solution_missing_status",
        "solution_unknown_status",
        "solution_unknown_line",
        "solution_bad_status_line",
        "solution_status_unsatisfiable",
        "solution_status_unknown",
        "solution_missing_assignment",
        "solution_missing_terminator",
        "solution_literal_after_terminator",
        "solution_multiple_status_lines",
        "solution_non_integer_literal",
        "solution_too_many_literals",
        "solution_too_many_vars",
        "solution_variable_out_of_range",
        "solution_contradictory_assignment",
        "solution_duplicate_assignment",
        "solution_incomplete_assignment",
        "solution_unsatisfied",
    }
)


def verify_submission(
    *,
    cnf_text: str,
    dimacs_solution: str,
) -> SubmissionVerification:
    """Verify a miner's DIMACS solution body against a CNF.

    Pure: no I/O. The caller is expected to have already loaded the
    active challenge's CNF and to handle persistence based on the
    returned ``Result``.

    ``dimacs_solution_sha256`` is always populated, even on parse
    failure — the publisher logs/stores it on every attempt so a
    malformed solve can still be audited.
    """
    body_sha = sha256_hex(dimacs_solution)
    verification = dimacs.verify_dimacs_solution(cnf_text, dimacs_solution)

    if verification.parsed_ok and verification.satisfied:
        return SubmissionVerification(
            ok=True,
            error_code=None,
            dimacs_solution_sha256=body_sha,
            num_vars=verification.num_vars,
            num_clauses=verification.clause_count,
        )

    raw_code = verification.rejection_reason or "malformed_answer"
    if raw_code.startswith("cnf_"):
        # CNF-side failure should not surface as a miner error code; map
        # to a generic "malformed_answer" while preserving the underlying
        # reason in the result for logs.
        public_code = "malformed_answer"
    elif raw_code in _PUBLIC_ERROR_CODES:
        public_code = raw_code
    else:
        public_code = "malformed_answer"
    return SubmissionVerification(
        ok=False,
        error_code=public_code,
        dimacs_solution_sha256=body_sha,
        num_vars=verification.num_vars,
        num_clauses=verification.clause_count,
    )


__all__ = [
    "SubmissionVerification",
    "sha256_hex",
    "verify_submission",
]
