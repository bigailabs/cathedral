"""Safe DIMACS CNF and DIMACS solution parsers for the boolean lane.

Pure string parsers. No I/O, no clock, no randomness, no subprocess. These
satisfy the lane contract's static AST gates and are imported by both the
verifier and the test fixtures.

What the parsers do:

* ``parse_dimacs_cnf(text)`` reads a DIMACS CNF problem statement. It
  accepts ``c``-prefixed comment lines, a single ``p cnf <vars> <clauses>``
  header, then one or more clauses terminated by ``0``. Returns a
  :class:`Cnf` value.
* ``parse_dimacs_solution(text)`` reads the solver-style output a miner
  returns. It accepts a single ``s`` line (status), then one or more
  ``v`` lines listing signed literals ending in ``0``. Returns a
  :class:`Solution` value.

Both parsers are total in the sense that bad input never raises out of
the parser. They return a result object whose ``rejection_reason`` is
populated on failure, so the verifier can convert parse failures into
``VerifierResult(parsed_ok=False, ...)`` without exception handling.

Size bounds defend against accidentally oversized miner submissions. The
defaults are deliberately small; toy fixtures fit comfortably inside.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bounds are high enough for first-launch private challenge files while
# still preventing accidental unbounded strings from entering the parser.
MAX_CNF_BYTES = 64 * 1024 * 1024
MAX_SOLUTION_BYTES = 64 * 1024 * 1024
MAX_VARIABLES = 2_000_000
MAX_CLAUSES = 10_000_000
MAX_LITERALS_PER_CLAUSE = 1024


@dataclass(frozen=True)
class Cnf:
    num_vars: int
    clauses: tuple[tuple[int, ...], ...]
    rejection_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejection_reason is None


@dataclass(frozen=True)
class Solution:
    status: str
    literals: tuple[int, ...]
    rejection_reason: str | None = None
    assignment: dict[int, bool] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.rejection_reason is None


def _empty_cnf(reason: str) -> Cnf:
    return Cnf(num_vars=0, clauses=(), rejection_reason=reason)


def _empty_solution(reason: str, status: str = "") -> Solution:
    return Solution(status=status, literals=(), rejection_reason=reason, assignment={})


def parse_dimacs_cnf(text: str) -> Cnf:
    if not isinstance(text, str):
        return _empty_cnf("cnf_not_a_string")
    if not text.strip():
        return _empty_cnf("cnf_empty")
    if len(text.encode()) > MAX_CNF_BYTES:
        return _empty_cnf("cnf_oversized")

    header_seen = False
    declared_vars = 0
    declared_clauses = 0
    clauses: list[tuple[int, ...]] = []
    current: list[int] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("c") or line.startswith("%"):
            continue
        if line.startswith("p"):
            if header_seen:
                return _empty_cnf("cnf_multiple_headers")
            parts = line.split()
            if len(parts) != 4 or parts[0] != "p" or parts[1] != "cnf":
                return _empty_cnf("cnf_bad_header")
            try:
                declared_vars = int(parts[2])
                declared_clauses = int(parts[3])
            except ValueError:
                return _empty_cnf("cnf_bad_header_numbers")
            if declared_vars < 0 or declared_clauses < 0:
                return _empty_cnf("cnf_negative_header")
            if declared_vars > MAX_VARIABLES:
                return _empty_cnf("cnf_too_many_vars")
            if declared_clauses > MAX_CLAUSES:
                return _empty_cnf("cnf_too_many_clauses")
            header_seen = True
            continue

        if not header_seen:
            return _empty_cnf("cnf_clause_before_header")

        tokens = line.split()
        for tok in tokens:
            try:
                lit = int(tok)
            except ValueError:
                return _empty_cnf("cnf_non_integer_literal")
            if lit == 0:
                if len(current) > MAX_LITERALS_PER_CLAUSE:
                    return _empty_cnf("cnf_clause_too_long")
                clauses.append(tuple(current))
                current = []
            else:
                if abs(lit) > declared_vars:
                    return _empty_cnf("cnf_literal_out_of_range")
                current.append(lit)

    if not header_seen:
        return _empty_cnf("cnf_missing_header")
    if current:
        return _empty_cnf("cnf_unterminated_clause")
    if len(clauses) != declared_clauses:
        return _empty_cnf("cnf_clause_count_mismatch")

    return Cnf(num_vars=declared_vars, clauses=tuple(clauses), rejection_reason=None)


_ALLOWED_STATUSES = frozenset({"SATISFIABLE", "UNSATISFIABLE", "UNKNOWN"})


def parse_dimacs_solution(text: str) -> Solution:
    if not isinstance(text, str):
        return _empty_solution("solution_not_a_string")
    if not text.strip():
        return _empty_solution("solution_empty")
    if len(text.encode()) > MAX_SOLUTION_BYTES:
        return _empty_solution("solution_oversized")

    status: str | None = None
    literals: list[int] = []
    seen_terminator = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("c"):
            continue
        if len(line) > 1 and line[0].lower() == "s" and line[1].isspace():
            if status is not None:
                return _empty_solution("solution_multiple_status_lines", status=status)
            parts = line.split()
            if len(parts) < 2:
                return _empty_solution("solution_bad_status_line")
            candidate = parts[1].upper()
            if candidate not in _ALLOWED_STATUSES:
                return _empty_solution("solution_unknown_status", status=candidate)
            status = candidate
            continue
        if line[:1].lower() == "v":
            body = line[1:].strip()
            if not body:
                continue
            tokens = body.split()
            for tok in tokens:
                try:
                    lit = int(tok)
                except ValueError:
                    return _empty_solution("solution_non_integer_literal", status=status or "")
                if lit == 0:
                    seen_terminator = True
                    continue
                if seen_terminator:
                    return _empty_solution("solution_literal_after_terminator", status=status or "")
                literals.append(lit)
            continue
        return _empty_solution("solution_unknown_line", status=status or "")

    if status is None:
        return _empty_solution("solution_missing_status")
    if status != "SATISFIABLE":
        return _empty_solution(f"solution_status_{status.lower()}", status=status)
    if not literals:
        return _empty_solution("solution_missing_assignment", status=status)
    if not seen_terminator:
        return _empty_solution("solution_missing_terminator", status=status)
    if len(literals) > MAX_VARIABLES * 2:
        return _empty_solution("solution_too_many_literals", status=status)

    assignment: dict[int, bool] = {}
    for lit in literals:
        var = abs(lit)
        value = lit > 0
        if var in assignment and assignment[var] != value:
            return _empty_solution("solution_contradictory_assignment", status=status)
        assignment[var] = value

    return Solution(
        status=status,
        literals=tuple(literals),
        rejection_reason=None,
        assignment=assignment,
    )


def evaluate_assignment(cnf: Cnf, assignment: dict[int, bool]) -> tuple[bool, int, int]:
    """Return ``(satisfied, satisfied_count, clause_count)``.

    Assumes ``cnf.ok`` is True. Caller is responsible for the parse check.
    """
    satisfied_count = 0
    for clause in cnf.clauses:
        clause_ok = False
        for lit in clause:
            var = abs(lit)
            if var not in assignment:
                clause_ok = False
                break
            value = assignment[var]
            literal_value = value if lit > 0 else not value
            if literal_value:
                clause_ok = True
                break
        if clause_ok:
            satisfied_count += 1
    total = len(cnf.clauses)
    return (satisfied_count == total and total > 0, satisfied_count, total)


def assignment_covers_all_vars(cnf: Cnf, assignment: dict[int, bool]) -> bool:
    if cnf.num_vars == 0:
        return True
    for var in range(1, cnf.num_vars + 1):
        if var not in assignment:
            return False
    return True


def assignment_in_range(cnf: Cnf, assignment: dict[int, bool]) -> bool:
    if not assignment:
        return False
    for var in assignment:
        if var < 1 or var > cnf.num_vars:
            return False
    return True


__all__ = [
    "MAX_CLAUSES",
    "MAX_CNF_BYTES",
    "MAX_LITERALS_PER_CLAUSE",
    "MAX_SOLUTION_BYTES",
    "MAX_VARIABLES",
    "Cnf",
    "Solution",
    "assignment_covers_all_vars",
    "assignment_in_range",
    "evaluate_assignment",
    "parse_dimacs_cnf",
    "parse_dimacs_solution",
]
