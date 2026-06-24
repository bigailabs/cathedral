"""Every registered replay invariant must be surfaced on every operator surface.

Guards against a target being registered and scored against while hidden from the
differential, the Proof Board, or the self-check. The three surfaces must agree
on the target set.
"""
from __future__ import annotations

from game.arena import replay
from game.arena.proofboard import render_proofboard
from game.arena.replay_differential import differential_report
from game.arena.selfcheck import selfcheck_report


def test_differential_covers_exactly_the_registered_targets():
    registered = set(replay.TARGETS)
    diff_ids = {r["target_id"] for r in differential_report()["targets"]}
    assert diff_ids == registered, f"differential drifted from registry: {diff_ids ^ registered}"


def test_proof_board_shows_every_registered_target():
    html = render_proofboard()
    missing = [t for t in replay.TARGETS if t not in html]
    assert not missing, f"registered targets hidden from the Proof Board: {missing}"


def test_selfcheck_count_matches_the_registry():
    rep = selfcheck_report("/nonexistent")
    check = next(c for c in rep["checks"] if c["name"] == "replay_is_a_real_gate")
    n = len(replay.TARGETS)
    assert f"{n}/{n}" in check["detail"]  # all registered, all proven
    assert rep["ok"] is True


def test_all_surfaces_agree_on_the_total():
    n = len(replay.TARGETS)
    assert differential_report()["total"] == n
    assert render_proofboard().count('class="tid"') >= n  # one card id per target
