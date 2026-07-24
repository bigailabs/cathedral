"""DIMACS solution parsing + witness check with the monolith's exact rejection
vocabulary — the solve-on-submit referee for Lane A.

A submitted solution is a DIMACS solver-output blob:

    s SATISFIABLE
    v 1 -2 3 0

This module turns that blob + the challenge CNF into either a satisfying witness
(score 1.0) or one of the locked rejection reasons the 9 adversarial fixtures
pin (see ~/code/cathedral/src/cathedral/lanes/synthetic_boolean_v1/fixtures/):

    answer_missing_dimacs_solution      — no `dimacs_solution` field
    solution_missing_status             — no `s ...` line
    solution_unknown_status             — `s` value not SAT/UNSAT
    solution_status_unsatisfiable       — claimed UNSAT (not accepted here)
    solution_non_integer_literal        — a `v` token isn't an int
    solution_variable_out_of_range      — a var index > n_vars
    solution_contradictory_assignment   — same var both polarities
    solution_incomplete_assignment      — fewer than n_vars assigned
    solution_unsatisfied                — complete but a clause is unsatisfied

Total: never raises on hostile input (verifier-totality non-regression).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..dimacs import parse_cnf


@dataclass(frozen=True)
class SolveCheck:
    ok: bool
    rejection_reason: str | None
    assignment: list[int]  # signed-literal witness when ok


def verify_dimacs_solution(cnf_text: str, dimacs_solution: str | None) -> SolveCheck:
    """Check a DIMACS solution blob against the CNF. Total + bounded."""
    if not isinstance(dimacs_solution, str) or not dimacs_solution.strip():
        return SolveCheck(False, "answer_missing_dimacs_solution", [])

    n_vars, clauses = parse_cnf(cnf_text)

    status: str | None = None
    lits: list[int] = []
    for line in dimacs_solution.splitlines():
        line = line.strip()
        if not line or line[0] == "c":
            continue
        if line[0] == "s":
            status = line[1:].strip().upper()
        elif line[0] == "v":
            for tok in line[1:].split():
                if tok == "0":
                    continue
                try:
                    lit = int(tok)
                except ValueError:
                    return SolveCheck(False, "solution_non_integer_literal", [])
                if lit != 0:
                    lits.append(lit)

    if status is None:
        return SolveCheck(False, "solution_missing_status", [])
    if "UNSATISFIABLE" in status:
        return SolveCheck(False, "solution_status_unsatisfiable", [])
    if "SATISFIABLE" not in status:
        return SolveCheck(False, "solution_unknown_status", [])

    val: dict[int, bool] = {}
    for lit in lits:
        v = abs(lit)
        if v > n_vars:
            return SolveCheck(False, "solution_variable_out_of_range", [])
        if v in val and val[v] != (lit > 0):
            return SolveCheck(False, "solution_contradictory_assignment", [])
        val[v] = lit > 0

    if len(val) != n_vars:
        return SolveCheck(False, "solution_incomplete_assignment", [])

    for clause in clauses:
        if not any(val.get(abs(lit)) == (lit > 0) for lit in clause):
            return SolveCheck(False, "solution_unsatisfied", [])

    witness = [v if val[v] else -v for v in range(1, n_vars + 1)]
    return SolveCheck(True, None, witness)
