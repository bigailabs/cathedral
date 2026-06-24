"""Replay harnesses must be real discriminators.

For each pinned invariant we assert it separates an exploit input from a benign
one. Conserved targets must hold across a stress set. A harness that always
returns the same verdict would fail these checks.
"""
from __future__ import annotations

import json

from game.arena import replay
from game.arena.replay_differential import (
    BENIGN_WITNESSES,
    CONSERVED_STRESS,
    _differential_for,
    differential_report,
)


def test_every_registered_target_is_a_real_discriminator():
    rep = differential_report()
    assert rep["total"] >= 4
    assert rep["all_real"] is True
    assert rep["discriminators"] == rep["total"]
    assert rep["exploit"] >= 3
    assert rep["conserved"] >= 1


def test_exploit_targets_violate_on_exploit_and_hold_on_benign():
    for target_id, benign in BENIGN_WITNESSES.items():
        if target_id not in replay.TARGETS:
            continue
        target = replay.TARGETS[target_id]
        exploit = replay.run_replay(target_id, target.known_witness)
        benign_outcome = replay.run_replay(target_id, benign)
        assert exploit.reproduced is True, (
            f"{target_id}: exploit witness must violate the invariant"
        )
        assert benign_outcome.reproduced is False, f"{target_id}: benign witness must hold"
        assert benign_outcome.invariant_held is True
        assert exploit.observed != benign_outcome.observed, (
            f"{target_id}: harness output did not change with inputs"
        )


def test_conserved_targets_hold_across_a_stress_set():
    for target_id, witnesses in CONSERVED_STRESS.items():
        assert target_id in replay.TARGETS
        for witness in witnesses:
            outcome = replay.run_replay(target_id, witness)
            assert outcome.invariant_held is True and outcome.reproduced is False, (
                f"{target_id}: conserved invariant must hold on {witness}"
            )


def test_no_registered_exploit_target_silently_lacks_a_benign_witness():
    for target_id in replay.TARGETS:
        if target_id in CONSERVED_STRESS:
            continue
        row = _differential_for(target_id)
        assert row is not None
        assert row.get("reason") != "no_benign_witness_pinned", (
            f"{target_id} has no benign witness pinned in "
            "replay_differential.BENIGN_WITNESSES; add one so its harness is "
            "proven to discriminate"
        )
        assert row["discriminator"] is True


def test_report_is_serializable_and_self_describing():
    rep = differential_report()
    assert rep["schema"] == "cathedral.arena.replay_differential.v1"
    json.dumps(rep, default=str)
    for row in rep["targets"]:
        assert row["kind"] in {"exploit", "conserved"}
        assert "family" in row
