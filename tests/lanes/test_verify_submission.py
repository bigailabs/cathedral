"""PR5: pure-function tests for the DIMACS submission verifier."""

from __future__ import annotations

import hashlib

from cathedral.lanes.synthetic_boolean_v1.verify_submission import (
    sha256_hex,
    verify_submission,
)

_TRIVIAL_CNF = "p cnf 3 2\n1 2 0\n-2 3 0\n"


def test_satisfying_assignment_succeeds() -> None:
    sol = "s SATISFIABLE\nv 1 2 3 0\n"
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution=sol)
    assert result.ok
    assert result.error_code is None
    assert result.num_vars == 3
    assert result.num_clauses == 2
    assert result.dimacs_solution_sha256 == sha256_hex(sol)


def test_unsatisfying_assignment_returns_solution_unsatisfied() -> None:
    # Assignment fails clause (-2 3): -2 means var 2 must be False; we
    # set var 2 = True so clause 1 (1 2) passes; var 3 = False; clause 2
    # is (-2 OR 3) = (F OR F) = F.
    sol = "s SATISFIABLE\nv 1 2 -3 0\n"
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution=sol)
    assert not result.ok
    assert result.error_code == "solution_unsatisfied"


def test_incomplete_assignment_returns_incomplete() -> None:
    sol = "s SATISFIABLE\nv 1 0\n"
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution=sol)
    assert not result.ok
    assert result.error_code == "solution_incomplete_assignment"


def test_malformed_solution_maps_to_malformed_answer() -> None:
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution="not a dimacs body")
    assert not result.ok
    assert result.error_code in (
        "malformed_answer",
        "solution_unknown_line",
    )


def test_empty_solution_returns_solution_empty() -> None:
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution="")
    assert not result.ok
    assert result.error_code == "solution_empty"


def test_unsatisfiable_status_returns_solution_status_unsatisfiable() -> None:
    result = verify_submission(
        cnf_text=_TRIVIAL_CNF,
        dimacs_solution="s UNSATISFIABLE\n",
    )
    assert not result.ok
    assert result.error_code == "solution_status_unsatisfiable"


def test_solution_sha256_is_always_populated_even_on_failure() -> None:
    body = "garbage"
    result = verify_submission(cnf_text=_TRIVIAL_CNF, dimacs_solution=body)
    assert not result.ok
    expected = hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert result.dimacs_solution_sha256 == expected
