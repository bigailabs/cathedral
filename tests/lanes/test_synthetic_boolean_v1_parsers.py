"""Unit tests for the DIMACS parsers used by synthetic_boolean_v1.

The parsers are pure functions of their input string and return result
dataclasses that carry a rejection reason on failure. Tests here cover
golden paths plus adversarial shapes the verifier must reject.
"""

from __future__ import annotations

import pytest

import cathedral.lanes.synthetic_boolean_v1.dimacs as dimacs_mod
from cathedral.lanes.synthetic_boolean_v1.dimacs import (
    assignment_covers_all_vars,
    assignment_in_range,
    evaluate_assignment,
    parse_dimacs_cnf,
    parse_dimacs_cnf_metadata,
    parse_dimacs_solution,
    verify_dimacs_solution,
)
from cathedral.publisher.sat_file_verifier import (
    parse_dimacs_cnf_metadata_file,
    verify_dimacs_solution_file,
)

# --------------------------------------------------------------------------
# CNF parser
# --------------------------------------------------------------------------


def test_parse_cnf_minimal_single_clause() -> None:
    cnf = parse_dimacs_cnf("p cnf 1 1\n1 0\n")
    assert cnf.ok
    assert cnf.num_vars == 1
    assert cnf.clauses == ((1,),)


def test_parse_cnf_comments_and_blank_lines_skipped() -> None:
    text = "c this is a comment\n\nc another\np cnf 2 1\n1 -2 0\n"
    cnf = parse_dimacs_cnf(text)
    assert cnf.ok
    assert cnf.num_vars == 2
    assert cnf.clauses == ((1, -2),)


def test_parse_cnf_multiple_clauses() -> None:
    text = "p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n"
    cnf = parse_dimacs_cnf(text)
    assert cnf.ok
    assert cnf.num_vars == 3
    assert cnf.clauses == ((1, -2, 3), (-1, 2, 3), (1, 2, -3))


def test_parse_cnf_clauses_can_span_lines() -> None:
    # DIMACS allows a clause to span multiple lines if the terminator
    # zero arrives later. We accept both terse and split styles.
    text = "p cnf 3 1\n1 -2\n3 0\n"
    cnf = parse_dimacs_cnf(text)
    assert cnf.ok
    assert cnf.clauses == ((1, -2, 3),)


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "cnf_empty"),
        ("p cnf\n", "cnf_bad_header"),
        ("p cnf a b\n", "cnf_bad_header_numbers"),
        ("p cnf -1 1\n", "cnf_negative_header"),
        ("1 0\n", "cnf_clause_before_header"),
        ("p cnf 2 1\nfoo 0\n", "cnf_non_integer_literal"),
        ("p cnf 1 1\n5 0\n", "cnf_literal_out_of_range"),
        ("p cnf 1 1\n+1 0\n", "cnf_non_integer_literal"),
        ("p cnf 1 1\n-0\n", "cnf_non_integer_literal"),
        ("p cnf 1 1\n0\n", "cnf_empty_clause"),
        ("p cnf 1 1\n% end marker\n1 0\n", "cnf_non_integer_literal"),
        ("p cnf 1 1\n1\n", "cnf_unterminated_clause"),
        ("p cnf 2 2\n1 0\n", "cnf_clause_count_mismatch"),
        ("p cnf 1 1\np cnf 1 1\n1 0\n", "cnf_multiple_headers"),
    ],
)
def test_parse_cnf_rejects(text: str, reason: str) -> None:
    cnf = parse_dimacs_cnf(text)
    assert not cnf.ok
    assert cnf.rejection_reason == reason


def test_parse_cnf_oversized_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dimacs_mod, "MAX_CNF_BYTES", 8)
    huge = "p cnf 1 1\n1 0\n"
    cnf = parse_dimacs_cnf(huge)
    assert not cnf.ok
    assert cnf.rejection_reason == "cnf_oversized"


def test_parse_cnf_metadata_does_not_collect_clauses() -> None:
    meta = parse_dimacs_cnf_metadata("p cnf 3 2\n1 -2 0\n2 3 0\n")
    assert meta.ok
    assert meta.num_vars == 3
    assert meta.num_clauses == 2


def test_parse_cnf_metadata_file_streams_without_collecting_clauses(tmp_path) -> None:
    cnf_path = tmp_path / "stream.cnf"
    cnf_path.write_text("c comment\np cnf 3 2\n1 -2 0\n2 3 0\n", encoding="utf-8")

    meta = parse_dimacs_cnf_metadata_file(cnf_path)

    assert meta.ok
    assert meta.num_vars == 3
    assert meta.num_clauses == 2


def test_parse_cnf_metadata_file_honors_explicit_size_limit(tmp_path) -> None:
    cnf_path = tmp_path / "stream.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")

    meta = parse_dimacs_cnf_metadata_file(cnf_path, max_bytes=8)

    assert not meta.ok
    assert meta.rejection_reason == "cnf_oversized"


# --------------------------------------------------------------------------
# Solution parser
# --------------------------------------------------------------------------


def test_parse_solution_basic_sat() -> None:
    sol = parse_dimacs_solution("s SATISFIABLE\nv 1 -2 3 0\n")
    assert sol.ok
    assert sol.status == "SATISFIABLE"
    assert sol.assignment == {1: True, 2: False, 3: True}


def test_parse_solution_multiple_v_lines() -> None:
    sol = parse_dimacs_solution("s SATISFIABLE\nv 1 -2\nv 3 4 0\n")
    assert sol.ok
    assert sol.assignment == {1: True, 2: False, 3: True, 4: True}


def test_parse_solution_comments_skipped() -> None:
    sol = parse_dimacs_solution("c top comment\ns SATISFIABLE\nc mid\nv 1 0\n")
    assert sol.ok
    assert sol.assignment == {1: True}


@pytest.mark.parametrize(
    "text,reason",
    [
        ("", "solution_empty"),
        ("v 1 0\n", "solution_missing_status"),
        ("s UNSATISFIABLE\n", "solution_status_unsatisfiable"),
        ("s UNKNOWN\n", "solution_status_unknown"),
        ("s ROOMBA\n", "solution_unknown_status"),
        ("s SATISFIABLE\nv\n", "solution_missing_assignment"),
        ("s SATISFIABLE\nv 1 2\n", "solution_missing_terminator"),
        ("s SATISFIABLE\nv 1 -1 0\n", "solution_contradictory_assignment"),
        ("s SATISFIABLE\nv 1 1 0\n", "solution_duplicate_assignment"),
        ("s SATISFIABLE\nv 1 foo 0\n", "solution_non_integer_literal"),
        ("s SATISFIABLE\nv +1 0\n", "solution_non_integer_literal"),
        ("s SATISFIABLE extra\nv 1 0\n", "solution_bad_status_line"),
        ("s satisfiable\nv 1 0\n", "solution_unknown_status"),
        ("s SATISFIABLE\ns SATISFIABLE\nv 1 0\n", "solution_multiple_status_lines"),
        ("s SATISFIABLE\nv 0 1 0\n", "solution_literal_after_terminator"),
        ("s SATISFIABLE\nv 1 0 0\n", "solution_literal_after_terminator"),
        ("s SATISFIABLE\nv1 0\n", "solution_unknown_line"),
        ("nonsense line\n", "solution_unknown_line"),
    ],
)
def test_parse_solution_rejects(text: str, reason: str) -> None:
    sol = parse_dimacs_solution(text)
    assert not sol.ok
    assert sol.rejection_reason == reason


def test_parse_solution_oversized_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dimacs_mod, "MAX_SOLUTION_BYTES", 8)
    huge = "s SATISFIABLE\nv 1 0\n"
    sol = parse_dimacs_solution(huge)
    assert not sol.ok
    assert sol.rejection_reason == "solution_oversized"


def test_verify_dimacs_solution_streaming_path() -> None:
    result = verify_dimacs_solution(
        "p cnf 3 2\n1 -2 0\n2 3 0\n",
        "s SATISFIABLE\nv 1 2 -3 0\n",
    )
    assert result.parsed_ok
    assert result.satisfied
    assert result.clauses_satisfied == 2
    assert result.clause_count == 2


def test_verify_dimacs_solution_file_streams_cnf_from_disk(tmp_path) -> None:
    cnf_path = tmp_path / "stream.cnf"
    cnf_path.write_text("p cnf 3 2\n1 -2 0\n2 3 0\n", encoding="utf-8")

    result = verify_dimacs_solution_file(
        cnf_path,
        "s SATISFIABLE\nv 1 2 -3 0\n",
    )

    assert result.parsed_ok
    assert result.satisfied
    assert result.clauses_satisfied == 2
    assert result.clause_count == 2


def test_verify_dimacs_solution_file_rejects_unsatisfied_assignment(tmp_path) -> None:
    cnf_path = tmp_path / "stream.cnf"
    cnf_path.write_text("p cnf 2 2\n1 0\n2 0\n", encoding="utf-8")

    result = verify_dimacs_solution_file(
        cnf_path,
        "s SATISFIABLE\nv 1 -2 0\n",
    )

    assert result.parsed_ok
    assert not result.satisfied
    assert result.rejection_reason == "solution_unsatisfied"


def test_verify_dimacs_solution_matches_reference_strictness() -> None:
    cnf = "p cnf 2 2\n1 0\n2 0\n"
    for bad_solution in [
        "s SATISFIABLE extra\nv 1 2 0\n",
        "s SATISFIABLE\nv 1 1 2 0\n",
        "s SATISFIABLE\nv +1 2 0\n",
        "s SATISFIABLE\nv\nv 1 2 0\n",
        "s satisfiable\nv 1 2 0\n",
        "v 1 2 0\ns SATISFIABLE\n",
        "s SATISFIABLE\nv1 2 0\n",
        "s SATISFIABLE\nv 1 2 0 0\n",
    ]:
        result = verify_dimacs_solution(cnf, bad_solution)
        assert not result.satisfied
        assert result.rejection_reason


def test_verify_dimacs_solution_rejects_assignment_above_bitset_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dimacs_mod, "MAX_ASSIGNMENT_VARIABLES", 3)
    cnf = "p cnf 4 1\n1 0\n"

    result = verify_dimacs_solution(cnf, "s SATISFIABLE\nv 1 2 3 4 0\n")

    assert not result.parsed_ok
    assert result.rejection_reason == "solution_too_many_vars"


def test_verify_dimacs_solution_rejects_non_sat_status_before_bitset_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingAssignmentBits:
        def __init__(self, variable_count: int) -> None:
            raise AssertionError(f"unexpected bitset allocation for {variable_count}")

    monkeypatch.setattr(dimacs_mod, "_AssignmentBits", _ExplodingAssignmentBits)

    result = verify_dimacs_solution("p cnf 1 1\n1 0\n", "s UNKNOWN\n")

    assert not result.parsed_ok
    assert result.rejection_reason == "solution_status_unknown"


# --------------------------------------------------------------------------
# Evaluation helpers
# --------------------------------------------------------------------------


def test_evaluate_assignment_all_satisfied() -> None:
    cnf = parse_dimacs_cnf("p cnf 3 3\n1 -2 3 0\n-1 2 3 0\n1 2 -3 0\n")
    satisfied, sc, total = evaluate_assignment(cnf, {1: True, 2: True, 3: True})
    assert satisfied
    assert sc == total == 3


def test_evaluate_assignment_partial_marks_clause_unsatisfied_when_required() -> None:
    # All clauses require x3 to be assigned to be satisfied. Without x3
    # in the map, evaluate_assignment marks the clause unsatisfied.
    cnf = parse_dimacs_cnf("p cnf 3 2\n3 0\n-3 0\n")
    satisfied, sc, total = evaluate_assignment(cnf, {1: True, 2: True})
    assert not satisfied
    assert sc == 0
    assert total == 2


def test_assignment_covers_all_vars() -> None:
    cnf = parse_dimacs_cnf("p cnf 3 1\n1 -2 3 0\n")
    assert assignment_covers_all_vars(cnf, {1: True, 2: False, 3: True})
    assert not assignment_covers_all_vars(cnf, {1: True, 2: False})


def test_assignment_in_range() -> None:
    cnf = parse_dimacs_cnf("p cnf 3 1\n1 -2 3 0\n")
    assert assignment_in_range(cnf, {1: True, 2: False, 3: True})
    assert not assignment_in_range(cnf, {1: True, 4: True})
