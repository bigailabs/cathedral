"""DIMACS CNF parsing and solver-output parsing.

Pure Python, no I/O. Used by ``verifier.py``. Two public entry points:

* ``parse_dimacs_cnf(text)`` -> ``CnfFormula``
* ``parse_solver_output(text)`` -> ``SolverOutput``

DIMACS CNF spec (the subset Cathedral accepts for v1):

    c optional comment
    p cnf <num_vars> <num_clauses>
    <lit> <lit> ... 0
    ...

* Variables are 1-indexed positive ints.
* Literals are signed ints. Negative means negated variable.
* Each clause line ends with ``0``. Clauses may span multiple lines.
* Lines starting with ``c`` are comments and are skipped.

Solver output (the subset Cathedral accepts for v1):

    s SATISFIABLE
    v <lit> <lit> ... 0
    v <lit> ... 0
    ...

* ``s`` line is the status. We require ``SATISFIABLE`` for v1.
* ``v`` line(s) carry the satisfying assignment as signed literals.
* A trailing ``0`` terminates the assignment.
* Multiple ``v`` lines are concatenated.
* Lines starting with ``c`` are comments and are skipped.

Anything else (UNSAT, UNKNOWN, missing ``s`` line, missing terminator,
mismatched variable indices) is a parse failure. The caller is expected
to translate parse failures into a rejected ``VerifierResult``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class DimacsParseError(ValueError):
    """Raised when a DIMACS CNF or solver output cannot be parsed.

    The verifier catches this and turns it into a rejected
    ``VerifierResult`` -- this exception never escapes the lane.
    """


@dataclass(frozen=True)
class CnfFormula:
    """A parsed DIMACS CNF formula.

    ``clauses`` is a tuple of tuples of signed ints (literals). Variable
    indices are 1-based per DIMACS convention. ``num_vars`` and
    ``num_clauses`` come from the ``p cnf`` header and must match the
    actual clause count (we enforce that in the parser).
    """

    num_vars: int
    num_clauses: int
    clauses: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class SolverOutput:
    """A parsed solver-style assignment.

    ``status`` is normalized to ``"SATISFIABLE"``. ``assignment`` maps
    1-based variable index to bool. Variables omitted from the ``v``
    line(s) are absent from the map (the verifier treats absent vars as
    a partial assignment and rejects).
    """

    status: str
    assignment: dict[int, bool] = field(default_factory=dict)


# --------------------------------------------------------------------------
# DIMACS CNF parser
# --------------------------------------------------------------------------


def parse_dimacs_cnf(text: str) -> CnfFormula:
    """Parse a DIMACS CNF formula. Strict mode for v1 -- we reject
    anything we cannot unambiguously interpret."""
    if not isinstance(text, str):
        raise DimacsParseError("input must be a string")

    header_seen = False
    num_vars = 0
    num_clauses = 0

    # We collect tokens across lines because DIMACS allows multi-line
    # clauses; a clause is terminated by a 0 token, not by a newline.
    pending_literals: list[int] = []
    clauses: list[tuple[int, ...]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("c"):
            continue
        if line.startswith("p"):
            if header_seen:
                raise DimacsParseError("duplicate 'p cnf' header")
            parts = line.split()
            if len(parts) != 4 or parts[0] != "p" or parts[1] != "cnf":
                raise DimacsParseError(f"malformed header: {line!r}")
            try:
                num_vars = int(parts[2])
                num_clauses = int(parts[3])
            except ValueError as e:
                raise DimacsParseError(f"non-integer in header: {line!r}") from e
            if num_vars < 0 or num_clauses < 0:
                raise DimacsParseError("negative counts in header")
            header_seen = True
            continue

        if not header_seen:
            raise DimacsParseError("clause line before 'p cnf' header")

        # A clause line is a sequence of ints. Token '0' ends the
        # current clause. Multi-line clauses are allowed.
        for tok in line.split():
            try:
                lit = int(tok)
            except ValueError as e:
                raise DimacsParseError(f"non-integer literal: {tok!r}") from e
            if lit == 0:
                clauses.append(tuple(pending_literals))
                pending_literals = []
            else:
                if abs(lit) > num_vars:
                    raise DimacsParseError(f"literal {lit} out of range for num_vars={num_vars}")
                pending_literals.append(lit)

    if not header_seen:
        raise DimacsParseError("no 'p cnf' header found")

    if pending_literals:
        raise DimacsParseError("trailing clause not terminated by 0")

    if len(clauses) != num_clauses:
        raise DimacsParseError(
            f"clause count mismatch: header says {num_clauses}, got {len(clauses)}"
        )

    return CnfFormula(num_vars=num_vars, num_clauses=num_clauses, clauses=tuple(clauses))


# --------------------------------------------------------------------------
# Solver output parser
# --------------------------------------------------------------------------


def parse_solver_output(text: str) -> SolverOutput:
    """Parse the solver-style ``s SATISFIABLE`` + ``v ...`` block.

    For v1 we accept exactly ``s SATISFIABLE``. ``UNSATISFIABLE`` and
    ``UNKNOWN`` raise -- they are out of scope until DRAT/LRAT proof
    handling exists. The ``v`` line(s) must terminate with ``0``.
    """
    if not isinstance(text, str):
        raise DimacsParseError("input must be a string")

    status: str | None = None
    pending_literals: list[int] = []
    saw_terminator = False
    saw_v_line = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("c"):
            continue
        if line.startswith("s "):
            if status is not None:
                raise DimacsParseError("duplicate 's' status line")
            status_value = line[2:].strip().upper()
            if status_value != "SATISFIABLE":
                raise DimacsParseError(
                    f"unsupported solver status {status_value!r}; v1 accepts only SATISFIABLE"
                )
            status = "SATISFIABLE"
            continue
        if line.startswith("v "):
            saw_v_line = True
            for tok in line[2:].split():
                try:
                    lit = int(tok)
                except ValueError as e:
                    raise DimacsParseError(f"non-integer literal on v line: {tok!r}") from e
                if lit == 0:
                    saw_terminator = True
                else:
                    pending_literals.append(lit)
            continue
        # Anything else is unexpected. Reject so a sloppy submission
        # can't sneak partial assignments past us with extra noise.
        raise DimacsParseError(f"unrecognized line in solver output: {line!r}")

    if status is None:
        raise DimacsParseError("missing 's' status line")
    if not saw_v_line:
        raise DimacsParseError("missing 'v' assignment line")
    if not saw_terminator:
        raise DimacsParseError("'v' assignment not terminated by 0")

    assignment: dict[int, bool] = {}
    for lit in pending_literals:
        var = abs(lit)
        value = lit > 0
        if var in assignment and assignment[var] != value:
            raise DimacsParseError(f"contradictory assignment for variable {var}")
        assignment[var] = value

    return SolverOutput(status=status, assignment=assignment)


# --------------------------------------------------------------------------
# Serialization (for emitting public DIMACS text from a generated formula)
# --------------------------------------------------------------------------


def serialize_dimacs_cnf(formula: CnfFormula) -> str:
    """Serialize a CnfFormula back to canonical DIMACS text.

    Used by the generator to produce the public ``dimacs`` payload.
    Output is byte-stable for a given formula, which the determinism
    contract test depends on.
    """
    lines: list[str] = [f"p cnf {formula.num_vars} {formula.num_clauses}"]
    for clause in formula.clauses:
        lines.append(" ".join(str(lit) for lit in clause) + " 0")
    return "\n".join(lines) + "\n"
