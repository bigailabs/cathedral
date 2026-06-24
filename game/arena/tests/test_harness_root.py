"""Tests for the subtensor ROOT NAV replay harnesses."""
from __future__ import annotations

import random

from game.arena import harness_root as hr
from game.arena import replay
from game.arena.replay_differential import differential_report


def test_root_targets_are_registered_with_real_families():
    assert set(hr.register_root_targets()) <= set(replay.TARGETS)
    families = {target_id: replay.TARGETS[target_id].family for target_id in hr.ROOT_STRESS}
    assert families["subtensor-root:redeem-roundtrip@HEAD"] == "R_roundtrip"
    assert families["subtensor-root:deposit-no-dilution@HEAD"] == "A_conservation"
    assert families["subtensor-root:tao-split-conservation@HEAD"] == "A_conservation"


def test_root_invariants_hold_across_the_pinned_stress_set():
    for target_id, witnesses in hr.ROOT_STRESS.items():
        for witness in witnesses:
            outcome = replay.run_replay(target_id, witness)
            assert outcome.invariant_held is True and outcome.reproduced is False, (
                f"{target_id} {witness}"
            )


def test_root_invariants_hold_across_random_inputs():
    rng = random.Random(20260624)
    for _ in range(5000):
        escrow = rng.randint(1, 10**9)
        principal = rng.randint(1, escrow)
        bought = rng.randint(0, 10**9)
        a1 = hr._a1_harness({"bought": bought, "E": escrow, "P": principal})
        assert hr._a1_inv(a1) is True
        a2 = hr._a2_harness({"bought": bought, "E": escrow, "P": principal})
        assert hr._a2_inv(a2) is True
        assert a2["payout"] <= bought

        tao_total = rng.randint(1, 10**9)
        w0 = rng.randint(0, 10**6)
        w1 = rng.randint(1, 10**6)
        a4 = hr._a4_harness({"tao_total": tao_total, "w0": w0, "w1": w1})
        assert hr._a4_inv(a4) is True
        assert a4["s0"] + a4["s1"] == tao_total

        # F2 no-strand: a partial redeem (owed < P) never drains escrow to 0 while shares remain
        p2 = rng.randint(2, escrow if escrow >= 2 else 2)
        owed = rng.randint(1, p2 - 1)
        f2 = hr._f2_harness({"E": escrow, "P": p2, "owed": owed})
        assert hr._f2_inv(f2) is True
        assert not (f2["E2"] == 0 and f2["P2"] > 0)


def test_root_harness_output_actually_tracks_inputs():
    first = replay.run_replay(
        "subtensor-root:redeem-roundtrip@HEAD",
        {"bought": 500_000, "E": 1_000_000, "P": 800_000},
    )
    second = replay.run_replay(
        "subtensor-root:redeem-roundtrip@HEAD",
        {"bought": 3, "E": 5, "P": 2},
    )
    assert first.observed != second.observed
    assert first.observed["payout"] != second.observed["payout"]


def test_root_targets_are_conserved_discriminators_in_the_report():
    report = differential_report()
    by_id = {row["target_id"]: row for row in report["targets"]}
    for target_id in hr.ROOT_STRESS:
        assert by_id[target_id]["kind"] == "conserved"
        assert by_id[target_id]["discriminator"] is True
    assert report["conserved"] >= 4
    assert report["all_real"] is True
