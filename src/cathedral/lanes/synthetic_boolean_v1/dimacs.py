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

Size bounds defend against accidentally oversized miner submissions while
allowing launch-scale fixtures the publisher is expected to seed from private
storage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Bounds are high enough for the private launch shape while still
# preventing accidental unbounded strings from entering the Python verifier.
MAX_CNF_BYTES = 2 * 1024 * 1024 * 1024
MAX_SOLUTION_BYTES = 2 * 1024 * 1024 * 1024
MAX_VARIABLES = 1_000_000_000
MAX_CLAUSES = 1_000_000_000
MAX_LITERALS_PER_CLAUSE = 10_000_000

_UINT64_MAX = (1 << 64) - 1
_ASCII_SPACE = " \t\f\v"
_ASCII_STRIP = _ASCII_SPACE + "\r\n"


@dataclass(frozen=True)
class Cnf:
    num_vars: int
    clauses: tuple[tuple[int, ...], ...]
    rejection_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejection_reason is None


@dataclass(frozen=True)
class CnfMetadata:
    num_vars: int
    num_clauses: int
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


def _empty_metadata(reason: str) -> CnfMetadata:
    return CnfMetadata(num_vars=0, num_clauses=0, rejection_reason=reason)


def _empty_cnf_result(reason: str, *, collect_clauses: bool) -> Cnf | CnfMetadata:
    if collect_clauses:
        return _empty_cnf(reason)
    return _empty_metadata(reason)


def _empty_solution(reason: str, status: str = "") -> Solution:
    return Solution(status=status, literals=(), rejection_reason=reason, assignment={})


@dataclass(frozen=True)
class DimacsVerification:
    parsed_ok: bool
    satisfied: bool
    rejection_reason: str | None
    status: str = ""
    num_vars: int = 0
    assigned: int = 0
    clauses_satisfied: int = 0
    clause_count: int = 0


class _AssignmentBits:
    def __init__(self, variable_count: int) -> None:
        self.variable_count = variable_count
        self.assigned_count = 0
        byte_count = (variable_count + 7) // 8
        self._assigned = bytearray(byte_count)
        self._values = bytearray(byte_count)

    def set(self, variable: int, value: bool) -> str | None:
        if variable < 1 or variable > self.variable_count:
            return "solution_variable_out_of_range"
        index = (variable - 1) // 8
        mask = 1 << ((variable - 1) % 8)
        if self._assigned[index] & mask:
            current = bool(self._values[index] & mask)
            if current != value:
                return "solution_contradictory_assignment"
            return "solution_duplicate_assignment"
        self._assigned[index] |= mask
        if value:
            self._values[index] |= mask
        self.assigned_count += 1
        return None

    def complete(self) -> bool:
        return self.assigned_count == self.variable_count

    def value(self, variable: int) -> bool:
        index = (variable - 1) // 8
        mask = 1 << ((variable - 1) % 8)
        return bool(self._values[index] & mask)


def _text_size_ok(text: str, limit: int) -> bool:
    # DIMACS is ASCII outside comments. len(str) avoids a second large
    # allocation that text.encode() would create for private launch files.
    return len(text) <= limit


def _has_nul(text: str) -> bool:
    return "\x00" in text


def _unsigned_header_int(token: str) -> int | None:
    if not token or token.startswith(("-", "+")) or not _is_ascii_digits(token):
        return None
    value = int(token)
    if value > _UINT64_MAX:
        return None
    return value


def _signed_int_token(token: str) -> tuple[bool, int] | None:
    if not token:
        return None
    negative = token.startswith("-")
    body = token[1:] if negative else token
    if token.startswith("+") or not body or not _is_ascii_digits(body):
        return None
    magnitude = int(body)
    if magnitude > _UINT64_MAX:
        return None
    if negative and magnitude == 0:
        return None
    return negative, magnitude


def _skip_or_strip_line(raw_line: str) -> str | None:
    line = raw_line.strip(_ASCII_STRIP)
    if not line:
        return None
    # The reference parser treats only line-start c records as comments. Leading
    # horizontal whitespace before c is still line-start for this purpose.
    if line.startswith("c"):
        return None
    return line


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and all("0" <= ch <= "9" for ch in value)


def _split_ascii_space(value: str) -> list[str]:
    parts: list[str] = []
    start: int | None = None
    for index, char in enumerate(value):
        if char in _ASCII_SPACE:
            if start is not None:
                parts.append(value[start:index])
                start = None
        elif start is None:
            start = index
    if start is not None:
        parts.append(value[start:])
    return parts


def _scan_dimacs_cnf(text: str, *, collect_clauses: bool) -> Cnf | CnfMetadata:
    if not isinstance(text, str):
        return _empty_cnf_result("cnf_not_a_string", collect_clauses=collect_clauses)
    if not text.strip(_ASCII_STRIP):
        return _empty_cnf_result("cnf_empty", collect_clauses=collect_clauses)
    if not _text_size_ok(text, MAX_CNF_BYTES):
        return _empty_cnf_result("cnf_oversized", collect_clauses=collect_clauses)
    if _has_nul(text):
        return _empty_cnf_result("cnf_invalid_character", collect_clauses=collect_clauses)

    header_seen = False
    declared_vars = 0
    declared_clauses = 0
    clauses: list[tuple[int, ...]] = []
    current: list[int] = []
    clause_count = 0

    for raw_line in text.splitlines():
        line = _skip_or_strip_line(raw_line)
        if line is None:
            continue
        if line.startswith("p"):
            if header_seen:
                return _empty_cnf_result("cnf_multiple_headers", collect_clauses=collect_clauses)
            parts = _split_ascii_space(line)
            if len(parts) != 4 or parts[0] != "p" or parts[1] != "cnf":
                return _empty_cnf_result("cnf_bad_header", collect_clauses=collect_clauses)
            if parts[2].startswith("-") or parts[3].startswith("-"):
                return _empty_cnf_result("cnf_negative_header", collect_clauses=collect_clauses)
            header_vars = _unsigned_header_int(parts[2])
            header_clauses = _unsigned_header_int(parts[3])
            if header_vars is None or header_clauses is None:
                return _empty_cnf_result(
                    "cnf_bad_header_numbers",
                    collect_clauses=collect_clauses,
                )
            declared_vars = header_vars
            declared_clauses = header_clauses
            if declared_vars > MAX_VARIABLES:
                return _empty_cnf_result("cnf_too_many_vars", collect_clauses=collect_clauses)
            if declared_clauses > MAX_CLAUSES:
                return _empty_cnf_result(
                    "cnf_too_many_clauses",
                    collect_clauses=collect_clauses,
                )
            header_seen = True
            continue

        if not header_seen:
            return _empty_cnf_result("cnf_clause_before_header", collect_clauses=collect_clauses)

        tokens = _split_ascii_space(line)
        for tok in tokens:
            parsed = _signed_int_token(tok)
            if parsed is None:
                return _empty_cnf_result(
                    "cnf_non_integer_literal",
                    collect_clauses=collect_clauses,
                )
            negative, magnitude = parsed
            if magnitude == 0:
                if negative:
                    return _empty_cnf_result(
                        "cnf_non_integer_literal",
                        collect_clauses=collect_clauses,
                    )
                if not current:
                    return _empty_cnf_result("cnf_empty_clause", collect_clauses=collect_clauses)
                if len(current) > MAX_LITERALS_PER_CLAUSE:
                    return _empty_cnf_result("cnf_clause_too_long", collect_clauses=collect_clauses)
                clause_count += 1
                if clause_count > declared_clauses:
                    return _empty_cnf_result(
                        "cnf_clause_count_mismatch",
                        collect_clauses=collect_clauses,
                    )
                if collect_clauses:
                    clauses.append(tuple(current))
                current = []
            else:
                if magnitude > declared_vars:
                    return _empty_cnf_result(
                        "cnf_literal_out_of_range",
                        collect_clauses=collect_clauses,
                    )
                lit = -int(magnitude) if negative else int(magnitude)
                current.append(lit)
                if len(current) > MAX_LITERALS_PER_CLAUSE:
                    return _empty_cnf_result("cnf_clause_too_long", collect_clauses=collect_clauses)

    if not header_seen:
        return _empty_cnf_result("cnf_missing_header", collect_clauses=collect_clauses)
    if current:
        return _empty_cnf_result("cnf_unterminated_clause", collect_clauses=collect_clauses)
    if clause_count != declared_clauses:
        return _empty_cnf_result("cnf_clause_count_mismatch", collect_clauses=collect_clauses)

    if collect_clauses:
        return Cnf(num_vars=declared_vars, clauses=tuple(clauses), rejection_reason=None)
    return CnfMetadata(num_vars=declared_vars, num_clauses=declared_clauses)


def parse_dimacs_cnf(text: str) -> Cnf:
    scanned = _scan_dimacs_cnf(text, collect_clauses=True)
    assert isinstance(scanned, Cnf)
    return scanned


def parse_dimacs_cnf_metadata(text: str) -> CnfMetadata:
    scanned = _scan_dimacs_cnf(text, collect_clauses=False)
    assert isinstance(scanned, CnfMetadata)
    return scanned


_ALLOWED_STATUSES = frozenset({"SATISFIABLE", "UNSATISFIABLE", "UNKNOWN"})


def parse_dimacs_solution(text: str) -> Solution:
    if not isinstance(text, str):
        return _empty_solution("solution_not_a_string")
    if not text.strip(_ASCII_STRIP):
        return _empty_solution("solution_empty")
    if not _text_size_ok(text, MAX_SOLUTION_BYTES):
        return _empty_solution("solution_oversized")
    if _has_nul(text):
        return _empty_solution("solution_invalid_character")

    status: str | None = None
    literals: list[int] = []
    seen_terminator = False

    for raw_line in text.splitlines():
        line = _skip_or_strip_line(raw_line)
        if line is None:
            continue
        parts = _split_ascii_space(line)
        if status is None:
            if parts[0] == "v":
                return _empty_solution("solution_missing_status")
            if parts[0] != "s":
                return _empty_solution("solution_unknown_line")
            if len(parts) != 2:
                return _empty_solution("solution_bad_status_line")
            candidate = parts[1]
            if candidate not in _ALLOWED_STATUSES:
                return _empty_solution("solution_unknown_status", status=candidate)
            status = candidate
            continue

        if seen_terminator:
            return _empty_solution("solution_literal_after_terminator", status=status or "")

        if parts[0] == "s":
            if status is not None:
                return _empty_solution("solution_multiple_status_lines", status=status)
        if parts[0] != "v":
            return _empty_solution("solution_unknown_line", status=status or "")

        if len(parts) == 1:
            return _empty_solution("solution_missing_assignment", status=status or "")
        for index, tok in enumerate(parts[1:], start=1):
            parsed = _signed_int_token(tok)
            if parsed is None:
                return _empty_solution("solution_non_integer_literal", status=status or "")
            negative, magnitude = parsed
            if magnitude == 0:
                if negative:
                    return _empty_solution("solution_non_integer_literal", status=status or "")
                if index != len(parts) - 1:
                    return _empty_solution("solution_literal_after_terminator", status=status or "")
                seen_terminator = True
                break
            lit = -int(magnitude) if negative else int(magnitude)
            literals.append(lit)
        continue

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
        if var in assignment:
            if assignment[var] != value:
                return _empty_solution("solution_contradictory_assignment", status=status)
            return _empty_solution("solution_duplicate_assignment", status=status)
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


def _parse_solution_bits(
    text: str,
    variable_count: int,
) -> tuple[str | None, str, _AssignmentBits | None]:
    if not isinstance(text, str):
        return "solution_not_a_string", "", None
    if not text.strip(_ASCII_STRIP):
        return "solution_empty", "", None
    if not _text_size_ok(text, MAX_SOLUTION_BYTES):
        return "solution_oversized", "", None
    if _has_nul(text):
        return "solution_invalid_character", "", None

    status: str | None = None
    seen_terminator = False
    bits = _AssignmentBits(variable_count)

    for raw_line in text.splitlines():
        line = _skip_or_strip_line(raw_line)
        if line is None:
            continue
        parts = _split_ascii_space(line)
        if status is None:
            if parts[0] == "v":
                return "solution_missing_status", "", None
            if parts[0] != "s":
                return "solution_unknown_line", "", None
            if len(parts) != 2:
                return "solution_bad_status_line", "", None
            candidate = parts[1]
            if candidate not in _ALLOWED_STATUSES:
                return "solution_unknown_status", candidate, None
            status = candidate
            continue

        if seen_terminator:
            return "solution_literal_after_terminator", status, None
        if parts[0] == "s":
            return "solution_multiple_status_lines", status, None
        if parts[0] != "v":
            return "solution_unknown_line", status, None
        if len(parts) == 1:
            return "solution_missing_assignment", status, None

        for index, tok in enumerate(parts[1:], start=1):
            parsed = _signed_int_token(tok)
            if parsed is None:
                return "solution_non_integer_literal", status, None
            negative, magnitude = parsed
            if magnitude == 0:
                if negative:
                    return "solution_non_integer_literal", status, None
                if index != len(parts) - 1:
                    return "solution_literal_after_terminator", status, None
                seen_terminator = True
                break
            reason = bits.set(int(magnitude), not negative)
            if reason is not None:
                return reason, status, None

    if status is None:
        return "solution_missing_status", "", None
    if status != "SATISFIABLE":
        return f"solution_status_{status.lower()}", status, None
    if variable_count > 0 and not seen_terminator:
        return "solution_missing_terminator", status, None
    if not bits.complete():
        return "solution_incomplete_assignment", status, bits
    return None, status, bits


def _evaluate_cnf_streaming(
    text: str,
    metadata: CnfMetadata,
    assignment: _AssignmentBits,
) -> DimacsVerification:
    header_seen = False
    clause_count = 0
    satisfied_count = 0
    saw_literal = False
    clause_satisfied = False

    for raw_line in text.splitlines():
        line = _skip_or_strip_line(raw_line)
        if line is None:
            continue
        if line.startswith("p"):
            if header_seen:
                return DimacsVerification(False, False, "cnf_multiple_headers")
            parts = _split_ascii_space(line)
            header_vars = _unsigned_header_int(parts[2]) if len(parts) == 4 else None
            header_clauses = _unsigned_header_int(parts[3]) if len(parts) == 4 else None
            if (
                len(parts) != 4
                or parts[0] != "p"
                or parts[1] != "cnf"
                or header_vars != metadata.num_vars
                or header_clauses != metadata.num_clauses
            ):
                return DimacsVerification(False, False, "cnf_bad_header")
            header_seen = True
            continue
        if not header_seen:
            return DimacsVerification(False, False, "cnf_clause_before_header")

        for tok in _split_ascii_space(line):
            parsed = _signed_int_token(tok)
            if parsed is None:
                return DimacsVerification(False, False, "cnf_non_integer_literal")
            negative, magnitude = parsed
            if magnitude == 0:
                if not saw_literal:
                    return DimacsVerification(False, False, "cnf_empty_clause")
                if clause_count >= metadata.num_clauses:
                    return DimacsVerification(False, False, "cnf_clause_count_mismatch")
                if clause_satisfied:
                    satisfied_count += 1
                else:
                    return DimacsVerification(
                        True,
                        False,
                        "solution_unsatisfied",
                        status="SATISFIABLE",
                        num_vars=metadata.num_vars,
                        assigned=assignment.assigned_count,
                        clauses_satisfied=satisfied_count,
                        clause_count=metadata.num_clauses,
                    )
                clause_count += 1
                saw_literal = False
                clause_satisfied = False
                continue

            if magnitude > metadata.num_vars:
                return DimacsVerification(False, False, "cnf_literal_out_of_range")
            saw_literal = True
            value = assignment.value(int(magnitude))
            literal_value = (not value) if negative else value
            clause_satisfied = clause_satisfied or literal_value

    if not header_seen:
        return DimacsVerification(False, False, "cnf_missing_header")
    if saw_literal:
        return DimacsVerification(False, False, "cnf_unterminated_clause")
    if clause_count != metadata.num_clauses:
        return DimacsVerification(False, False, "cnf_clause_count_mismatch")
    return DimacsVerification(
        True,
        True,
        None,
        status="SATISFIABLE",
        num_vars=metadata.num_vars,
        assigned=assignment.assigned_count,
        clauses_satisfied=satisfied_count,
        clause_count=clause_count,
    )


def verify_dimacs_solution(cnf_text: str, solution_text: str) -> DimacsVerification:
    metadata = parse_dimacs_cnf_metadata(cnf_text)
    if not metadata.ok:
        return DimacsVerification(False, False, metadata.rejection_reason or "cnf_unparseable")

    reason, status, assignment = _parse_solution_bits(solution_text, metadata.num_vars)
    if reason is not None:
        return DimacsVerification(
            False,
            False,
            reason,
            status=status,
            num_vars=metadata.num_vars,
            assigned=assignment.assigned_count if assignment is not None else 0,
            clause_count=metadata.num_clauses,
        )
    assert assignment is not None
    return _evaluate_cnf_streaming(cnf_text, metadata, assignment)


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
    "CnfMetadata",
    "DimacsVerification",
    "Solution",
    "assignment_covers_all_vars",
    "assignment_in_range",
    "evaluate_assignment",
    "parse_dimacs_cnf",
    "parse_dimacs_cnf_metadata",
    "parse_dimacs_solution",
    "verify_dimacs_solution",
]
