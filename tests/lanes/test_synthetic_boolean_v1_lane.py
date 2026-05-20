"""Lane-level tests for synthetic_boolean_v1.

These exercise the full generate / verify / score pipeline against the
toy in-tree corpus. They complement ``tests/lanes/test_contract.py``
(which runs the contract gate) with explicit assertions on the
rejection-reason taxonomy.
"""

from __future__ import annotations

import pytest

from cathedral.lanes.contract import GenerateCtx, Submission
from cathedral.lanes.synthetic_boolean_v1 import SyntheticBooleanV1

CTX_KWARGS = {"seed": 12345, "issued_at_iso": "2026-01-01T00:00:00.000Z"}


def _generate(tier: int) -> tuple:
    lane = SyntheticBooleanV1()
    ctx = GenerateCtx(tier=tier, **CTX_KWARGS)
    pub, hid = lane.generate(ctx)
    return lane, pub, hid


@pytest.mark.parametrize(
    "tier,solution_text",
    [
        (0, "s SATISFIABLE\nv 1 0\n"),
        (1, "s SATISFIABLE\nv 1 2 3 0\n"),
        (2, "s SATISFIABLE\nv 1 -2 3 4 0\n"),
    ],
)
def test_lane_scores_one_for_valid_witness(tier: int, solution_text: str) -> None:
    lane, pub, hid = _generate(tier)
    sub = Submission(
        task_id=pub.task_id,
        miner_hotkey="5MinerGood",
        answer={"dimacs_solution": solution_text},
    )
    v = lane.verify(pub, hid, sub)
    s = lane.score(pub, v)
    assert v.parsed_ok
    assert v.rejection_reason is None
    assert s.weighted_score == 1.0
    assert s.rejection_reason is None
    assert s.score_parts == {"binary_correct": 1.0}


def test_lane_scores_zero_for_unsatisfied_assignment() -> None:
    lane, pub, hid = _generate(1)
    # For the tier-1 toy ``1 -2 3 | -1 2 3 | 1 2 -3`` the assignment
    # {x1=F, x2=T, x3=F} violates the first clause: 1=F, -2=F, 3=F.
    sub = Submission(
        task_id=pub.task_id,
        miner_hotkey="5MinerBad",
        answer={"dimacs_solution": "s SATISFIABLE\nv -1 2 -3 0\n"},
    )
    v = lane.verify(pub, hid, sub)
    s = lane.score(pub, v)
    assert v.parsed_ok
    assert v.rejection_reason == "solution_unsatisfied"
    assert s.weighted_score == 0.0
    assert s.rejection_reason == "solution_unsatisfied"


@pytest.mark.parametrize(
    "answer,expected_reason",
    [
        ({}, "answer_missing_dimacs_solution"),
        ({"dimacs_solution": 123}, "answer_missing_dimacs_solution"),
        ({"dimacs_solution": "s UNSATISFIABLE\n"}, "solution_status_unsatisfiable"),
        ({"dimacs_solution": "s UNKNOWN\n"}, "solution_status_unknown"),
        ({"dimacs_solution": "s SATISFIABLE\nv 1 2 0\n"}, "solution_incomplete_assignment"),
        ({"dimacs_solution": "s SATISFIABLE\nv 1 -1 2 3 0\n"}, "solution_contradictory_assignment"),
        ({"dimacs_solution": "s SATISFIABLE\nv 1 2 3 99 0\n"}, "solution_variable_out_of_range"),
        ({"dimacs_solution": "v 1 0\n"}, "solution_missing_status"),
        ({"dimacs_solution": "s ROOMBA\n"}, "solution_unknown_status"),
        (
            {"dimacs_solution": "s SATISFIABLE\nv 1 2 3 0\n", "assignment": {"1": True}},
            "answer_unexpected_keys",
        ),
    ],
)
def test_lane_rejection_reasons_for_bad_submissions(answer: dict, expected_reason: str) -> None:
    lane, pub, hid = _generate(1)
    sub = Submission(
        task_id=pub.task_id,
        miner_hotkey="5MinerAdv",
        answer=answer,
    )
    v = lane.verify(pub, hid, sub)
    s = lane.score(pub, v)
    assert s.weighted_score == 0.0
    assert s.rejection_reason == expected_reason


def test_lane_verify_never_raises_on_garbage() -> None:
    lane, pub, hid = _generate(2)
    hostile = [
        None,
        "raw string answer",
        ["a", "list"],
        {"dimacs_solution": None},
        {"dimacs_solution": []},
        {"dimacs_solution": "p cnf 1 1\n1 0\n"},  # CNF returned as solution
        {"dimacs_solution": "s SATISFIABLE\nv " + ("1 " * 5000) + "0\n"},
    ]
    for ans in hostile:
        sub = Submission(
            task_id=pub.task_id,
            miner_hotkey="5GAR" + "B" * 44,
            answer=ans if isinstance(ans, dict) else {"raw": ans},
        )
        v = lane.verify(pub, hid, sub)
        s = lane.score(pub, v)
        assert s.weighted_score == 0.0
        assert s.rejection_reason


def test_generate_is_deterministic_on_seed_tier() -> None:
    lane = SyntheticBooleanV1()
    ctx = GenerateCtx(seed=999, tier=1, issued_at_iso="2026-01-01T00:00:00.000Z")
    pub_a, hid_a = lane.generate(ctx)
    pub_b, hid_b = lane.generate(ctx)
    assert pub_a.model_dump_json() == pub_b.model_dump_json()
    assert hid_a.model_dump_json() == hid_b.model_dump_json()


def test_generate_changes_with_seed() -> None:
    lane = SyntheticBooleanV1()
    pub_a, _ = lane.generate(GenerateCtx(seed=1, tier=1, issued_at_iso="2026-01-01T00:00:00.000Z"))
    pub_b, _ = lane.generate(GenerateCtx(seed=2, tier=1, issued_at_iso="2026-01-01T00:00:00.000Z"))
    # task_id is seeded; the public input itself is keyed to the tier
    # toy in this first pass, so cnf stays stable per tier but task_id
    # differs per seed.
    assert pub_a.task_id != pub_b.task_id
    assert pub_a.public_input == pub_b.public_input


def test_score_clamps_out_of_range_raw_metric() -> None:
    lane = SyntheticBooleanV1()
    pub, _ = lane.generate(GenerateCtx(seed=1, tier=1, issued_at_iso="2026-01-01T00:00:00.000Z"))
    # parsed_ok but raw_metric far outside [0,1] -> clamped, binary
    # collapses to 0.0 unless raw_metric >= 1.0 with no rejection reason
    # (which would have been written by verify on success).
    from cathedral.lanes.contract import VerifierResult

    above = lane.score(pub, VerifierResult(parsed_ok=True, raw_metric=10.0))
    assert above.weighted_score == 1.0
    below = lane.score(pub, VerifierResult(parsed_ok=True, raw_metric=-5.0))
    assert below.weighted_score == 0.0
    mid = lane.score(pub, VerifierResult(parsed_ok=True, raw_metric=0.5))
    assert mid.weighted_score == 0.0
    assert mid.rejection_reason == "solution_unsatisfied"
