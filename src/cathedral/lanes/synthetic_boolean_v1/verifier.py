"""Pure-Python DIMACS SAT verifier.

The lane verifier takes a public CNF formula and a submission carrying a
solver-style assignment. It returns binary 1.0 / 0.0 weighted score plus
a rejection reason and a small ``score_parts`` breakdown.

Hard rules (enforced by the contract test suite):

* No network, no subprocess, no filesystem access.
* Never raises -- malformed input becomes a rejected result.
* Deterministic. Same inputs -> same output.

Public function: ``verify_sat_submission(dimacs_text, submission_answer)``.

The lane's ``SyntheticBooleanV1.verify`` adapts this to the
``VerifierResult`` shape the contract requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    CnfFormula,
    DimacsParseError,
    parse_dimacs_cnf,
    parse_solver_output,
)

# Rejection reason taxonomy. These strings are part of the contract --
# fixtures and audit tooling key on them.
REJECT_MISSING_ANSWER = "missing_answer"
REJECT_WRONG_TYPE = "wrong_answer_type"
REJECT_FORMULA_PARSE_FAILED = "formula_parse_failed"
REJECT_SOLVER_OUTPUT_PARSE_FAILED = "solver_output_parse_failed"
REJECT_PARTIAL_ASSIGNMENT = "partial_assignment"
REJECT_UNSATISFIED_CLAUSE = "unsatisfied_clause"


@dataclass(frozen=True)
class SatVerificationResult:
    """Output of ``verify_sat_submission``. Mapped 1:1 by the lane into
    the contract's ``VerifierResult``."""

    parsed_ok: bool
    weighted_score: float
    rejection_reason: str | None
    score_parts: dict[str, float] = field(default_factory=dict)
    details: dict[str, object] = field(default_factory=dict)


def _extract_solver_text(submission_answer: object) -> tuple[str | None, str | None]:
    """Pull the solver-output text out of the submission answer.

    Accepts two shapes for v1:

    1. ``{"solver_output": "s SATISFIABLE\\nv 1 -2 3 0\\n"}`` -- the
       canonical shape. The miner pastes the solver's stdout verbatim.
    2. ``{"assignment": {"1": true, "2": false, "3": true}}`` -- a
       structured assignment, for miners that don't want to emit
       DIMACS text. We synthesize the canonical solver block from it.

    Returns ``(text, error)``. ``text`` is None when ``error`` is set.
    """
    if not isinstance(submission_answer, dict):
        return None, REJECT_WRONG_TYPE

    if "solver_output" in submission_answer:
        val = submission_answer["solver_output"]
        if not isinstance(val, str):
            return None, REJECT_WRONG_TYPE
        return val, None

    if "assignment" in submission_answer:
        val = submission_answer["assignment"]
        if not isinstance(val, dict):
            return None, REJECT_WRONG_TYPE
        literals: list[int] = []
        for raw_var, raw_value in val.items():
            try:
                var = int(raw_var)
            except (TypeError, ValueError):
                return None, REJECT_WRONG_TYPE
            if var <= 0:
                return None, REJECT_WRONG_TYPE
            if not isinstance(raw_value, bool):
                # JSON numerics that round-trip to bool are still
                # ambiguous; require explicit bool.
                return None, REJECT_WRONG_TYPE
            literals.append(var if raw_value else -var)
        # Canonical: sort by absolute variable index for determinism.
        literals.sort(key=lambda lit: abs(lit))
        v_line = "v " + " ".join(str(lit) for lit in literals) + " 0"
        return f"s SATISFIABLE\n{v_line}\n", None

    return None, REJECT_MISSING_ANSWER


def _evaluate_formula(formula: CnfFormula, assignment: dict[int, bool]) -> tuple[bool, int]:
    """Evaluate the assignment against the formula.

    Returns ``(all_satisfied, first_unsatisfied_clause_index)``. If
    ``all_satisfied`` is True, the second value is -1. Otherwise it
    points at the 0-based clause index that failed.
    """
    for idx, clause in enumerate(formula.clauses):
        satisfied = False
        for lit in clause:
            var = abs(lit)
            val = assignment.get(var)
            if val is None:
                # Partial assignment -- caller filters this case
                # earlier; defensive treat-as-unsatisfied here.
                continue
            literal_true = val if lit > 0 else not val
            if literal_true:
                satisfied = True
                break
        if not satisfied:
            return False, idx
    return True, -1


def verify_sat_submission(
    dimacs_text: str,
    submission_answer: object,
) -> SatVerificationResult:
    """Verify a submission against a public DIMACS CNF formula.

    Total function. Returns a rejected result for every failure path;
    raises nothing.
    """
    # 1. Parse the public formula. Generator output is trusted to be
    #    well-formed, but defensive parsing keeps the verifier honest
    #    when fixtures are hand-written.
    try:
        formula = parse_dimacs_cnf(dimacs_text)
    except DimacsParseError as e:
        return SatVerificationResult(
            parsed_ok=False,
            weighted_score=0.0,
            rejection_reason=REJECT_FORMULA_PARSE_FAILED,
            details={"error": str(e)},
        )

    # 2. Pull the solver text out of the submission shape.
    solver_text, extract_err = _extract_solver_text(submission_answer)
    if extract_err is not None or solver_text is None:
        return SatVerificationResult(
            parsed_ok=False,
            weighted_score=0.0,
            rejection_reason=extract_err or REJECT_MISSING_ANSWER,
        )

    # 3. Parse solver output.
    try:
        parsed = parse_solver_output(solver_text)
    except DimacsParseError as e:
        return SatVerificationResult(
            parsed_ok=False,
            weighted_score=0.0,
            rejection_reason=REJECT_SOLVER_OUTPUT_PARSE_FAILED,
            details={"error": str(e)},
        )

    # 4. Require a full assignment (every variable assigned).
    expected_vars = set(range(1, formula.num_vars + 1))
    missing = sorted(expected_vars - parsed.assignment.keys())
    extra = sorted(parsed.assignment.keys() - expected_vars)
    if missing or extra:
        return SatVerificationResult(
            parsed_ok=False,
            weighted_score=0.0,
            rejection_reason=REJECT_PARTIAL_ASSIGNMENT,
            details={
                "missing_vars": missing[:32],
                "extra_vars": extra[:32],
                "num_vars": formula.num_vars,
            },
        )

    # 5. Evaluate.
    all_sat, first_bad = _evaluate_formula(formula, parsed.assignment)
    if not all_sat:
        return SatVerificationResult(
            parsed_ok=True,
            weighted_score=0.0,
            rejection_reason=REJECT_UNSATISFIED_CLAUSE,
            score_parts={
                "satisfied_clauses": float(first_bad),
                "total_clauses": float(formula.num_clauses),
            },
            details={
                "first_unsatisfied_clause_index": first_bad,
                "first_unsatisfied_clause": list(formula.clauses[first_bad]),
            },
        )

    return SatVerificationResult(
        parsed_ok=True,
        weighted_score=1.0,
        rejection_reason=None,
        score_parts={
            "satisfied_clauses": float(formula.num_clauses),
            "total_clauses": float(formula.num_clauses),
        },
        details={"num_vars": formula.num_vars, "num_clauses": formula.num_clauses},
    )
