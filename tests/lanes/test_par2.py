"""Tests for PAR-2 scoring math (cathedral.lanes.par2)."""

from __future__ import annotations

import math

import pytest

from cathedral.lanes.par2 import (
    aggregate_merit,
    challenge_shares,
    f_rank,
    operator_ranks,
)


def test_f_rank_basic() -> None:
    assert f_rank(1, 0.5) == 1.0
    assert f_rank(4, 0.5) == pytest.approx(0.5)
    assert f_rank(2, 1.0) == pytest.approx(0.5)
    assert f_rank(5, 0.0) == 1.0  # alpha=0 -> coverage (every rank equal)


def test_f_rank_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        f_rank(0, 0.5)
    with pytest.raises(ValueError):
        f_rank(1, -0.1)


def test_operator_ranks_dedupe_earliest() -> None:
    # Repeated operators collapse to earliest position; ranking is over
    # DISTINCT operators.
    assert operator_ranks(["A", "B", "A", "C", "B"]) == {"A": 1, "B": 2, "C": 3}


def test_challenge_shares_sum_to_budget_and_order() -> None:
    shares = challenge_shares(["A", "B", "C"], challenge_value=10.0, alpha=0.5)
    assert math.isclose(sum(shares.values()), 10.0)
    assert shares["A"] > shares["B"] > shares["C"]  # earlier receipt -> bigger share


def test_challenge_shares_alpha_zero_is_equal_split() -> None:
    shares = challenge_shares(["A", "B", "C", "D"], challenge_value=12.0, alpha=0.0)
    for v in shares.values():
        assert math.isclose(v, 3.0)


def test_challenge_shares_large_alpha_approaches_winner_take_all() -> None:
    shares = challenge_shares(["A", "B", "C"], challenge_value=1.0, alpha=8.0)
    assert shares["A"] > 0.99  # rank 1 takes ~everything


def test_empty_returns_empty() -> None:
    assert challenge_shares([], challenge_value=10.0, alpha=0.5) == {}
    assert aggregate_merit([], alpha=0.5) == {}


def test_sybil_rank_stacking_yields_zero_marginal_merit() -> None:
    """THE load-bearing property: one operator splitting into k identities
    (all resolving to the same coldkey/operator id) gains nothing. A single
    operator's repeated/extra submissions collapse to one rank slot, so its
    share is identical whether it submits once or k times."""
    alpha = 0.5
    w_c = 10.0
    honest = challenge_shares(["O1", "O2"], challenge_value=w_c, alpha=alpha)
    # O1 floods the challenge with 3 identities (same operator id):
    sybil = challenge_shares(["O1", "O1", "O1", "O2"], challenge_value=w_c, alpha=alpha)
    assert sybil == pytest.approx(honest)
    # And had ranking been per-hotkey (no dedupe), O1 would have grabbed
    # ranks 1..3 and out-earned O2 — the dedupe is what prevents that.
    assert sybil["O1"] > sybil["O2"]


def test_aggregate_sums_across_suite() -> None:
    # A solves both challenges; B only the second. Coverage shows up in M.
    challenges = [
        (10.0, ["A"]),            # only A solved challenge 1 -> A gets all 10
        (10.0, ["B", "A"]),       # B first, A second on challenge 2
    ]
    merit = aggregate_merit(challenges, alpha=0.5)
    # A: 10 (sole) + its rank-2 share of challenge 2; B: rank-1 share of c2.
    assert merit["A"] > merit["B"]
    assert math.isclose(sum(merit.values()), 20.0)  # total budget conserved


def test_higher_tier_value_dominates_coverage() -> None:
    # A wins a high-value hard challenge; B wins several cheap ones.
    challenges = [
        (10.0, ["A"]),   # hard/jackpot tier
        (1.0, ["B"]),
        (1.0, ["B"]),
        (1.0, ["B"]),
    ]
    merit = aggregate_merit(challenges, alpha=0.5)
    assert merit["A"] == pytest.approx(10.0)
    assert merit["B"] == pytest.approx(3.0)
