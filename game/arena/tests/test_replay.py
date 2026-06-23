"""Faithfulness of the REAL money-math replay — the U64F64 fee math must match
the audit-hunter manifest and the pinned subtensor invariants.
"""
from __future__ import annotations

from game.arena import replay
from game.arena.replay import first_fee, recalc_fee, run_replay


def test_recalc_reproduces_manifest_witness():
    # the exact witness recorded in audit-hunter/cnf/manifest.json (AMM-RECALC-OVERCHARGE)
    w = {"amount": 2934439936, "fee_rate": 65513, "delta_in": 985088}
    o = run_replay("subtensor-amm:recalc-overcharge@HEAD", w)
    assert o.reproduced is True                 # the real math overcharges
    assert o.observed["paid"] > o.observed["amount"]
    assert o.observed["overcharge"] > 0


def test_first_fee_conserves_value():
    # the verified path: fee + delta == amount exactly, fee <= amount (UNSAT to break)
    o = run_replay("subtensor-amm:first-fee-conservation@HEAD",
                   {"amount": 1_000_000, "fee_rate": 30})
    assert o.reproduced is False                 # invariant holds -> no finding
    assert o.observed["fee1"] + o.observed["delta"] == o.observed["amount"]


def test_multi_take_split_over_emits():
    # two takes off the same pool with no Σ-cap -> over-emission when r1+r2 > 1e4
    o = run_replay("subtensor-pallet:multi-take-split@HEAD",
                   {"pool": 1_000_000, "rate1_bps": 7000, "rate2_bps": 6000})
    assert o.reproduced is True
    assert o.observed["total"] > o.observed["pool"]


def test_split_within_cap_is_safe():
    o = run_replay("subtensor-pallet:multi-take-split@HEAD",
                   {"pool": 1_000_000, "rate1_bps": 4000, "rate2_bps": 5000})
    assert o.reproduced is False                 # r1+r2 <= 1e4 -> conserved


def test_zero_witness_never_reproduces():
    for tid in replay.TARGETS:
        t = replay.TARGETS[tid]
        o = run_replay(tid, {k: 0 for k in t.decode})
        assert o.reproduced is False


def test_malformed_witness_is_rejected_not_crash():
    o = run_replay("subtensor-amm:recalc-overcharge@HEAD", {"amount": 1})
    assert o.reproduced is False and "missing" in o.reason


def test_fixed_point_ops_match_reference():
    # first_fee(amount, fee_rate) for a realistic basis-point rate stays tiny + safe
    assert first_fee(1_000_000, 30) == 457
    # recalc_fee blows up as fee_rate -> u16_max (denominator collapses)
    assert recalc_fee(985088, 65513) > 985088
