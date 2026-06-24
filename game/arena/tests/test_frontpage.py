"""The bulletproof front page: one self-contained file anyone can open.

It must render with zero server/API dependency, stay jargon-free, carry the real
headline numbers, and show a live "proof it's real" badge from the replay
differential. It owns no server routes, so nothing can delete it.
"""
from __future__ import annotations

from game.arena import corpus, reports
from game.arena.frontpage import render_frontpage, main
from game.arena.models import GateOutcome
from game.arena.replay_differential import differential_report


def test_frontpage_is_self_contained_and_plain():
    html = render_frontpage()
    assert "<!doctype html>" in html and "<style>" in html      # no external CSS/JS
    assert "Cathedral Arena" in html
    assert "talk is free, proof pays" in html
    # four doors to the live server (incl. the Proof Board)
    assert 'href="/game"' in html and 'href="/howto"' in html and 'href="/arena"' in html
    assert 'href="/proofs"' in html and "Proof it is real" in html
    # no expert jargon leaks onto the simple page
    low = html.lower()
    for jargon in ("cnf", "unsat", "boolean_gate", "netuid", "hotkey", "witness"):
        assert jargon not in low, f"jargon '{jargon}' leaked onto the front page"


def test_frontpage_numbers_and_proof_badge_are_live():
    html = render_frontpage()
    cs = corpus.corpus_summary()
    assert f'<div class="n">{cs["targets"]}</div>' in html
    assert f'<div class="n">{cs["proof_tasks"]}</div>' in html
    assert f'<div class="n">{len(GateOutcome.GATES)}</div>' in html
    assert f'<div class="n">{len(reports.ANTICHEAT_AXES)}</div>' in html
    # the proof badge reflects the REAL differential result, not a hardcoded claim
    diff = differential_report()
    assert f'<b>{diff["discriminators"]}/{diff["total"]}</b>' in html
    assert diff["all_real"] is True                              # and they really are all real


def test_frontpage_main_writes_the_file(tmp_path):
    import sys
    argv = sys.argv
    sys.argv = ["frontpage", str(tmp_path)]
    try:
        assert main() == 0
    finally:
        sys.argv = argv
    out = tmp_path / "cathedral.html"
    assert out.exists()
    assert "Cathedral Arena" in out.read_text(encoding="utf-8")
