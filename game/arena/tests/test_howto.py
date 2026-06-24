"""Standalone game how-to page tests."""
from __future__ import annotations

from game.arena import corpus, reports
from game.arena.howto import main, render_howto
from game.arena.models import GateOutcome


def test_howto_renders_self_contained_game_instructions():
    html = render_howto()
    assert "<!doctype html>" in html and "<style>" in html
    assert "How to Play Cathedral Arena" in html
    assert "A short guide to the playable proof loop." in html
    for heading in ("Break the right thing", "Reports do not score", "Hardening can win too", "Cheats pay zero"):
        assert heading in html
    for step in ("Probe.", "Encode.", "Solve.", "Replay.", "Attest and seal."):
        assert step in html
    assert "Talk is free; proof pays." in html
    assert "Start the game at /game" in html


def test_howto_numbers_are_corpus_driven():
    html = render_howto()
    cs = corpus.corpus_summary()
    n_gates = len(GateOutcome.GATES)
    n_axes = len(reports.ANTICHEAT_AXES)
    assert f'one of <b>{cs["targets"]}</b> subnet targets' in html
    assert f"The live game routes across {cs['targets']} targets" in html
    assert f"<b>{n_axes}</b> anti-cheat classes" in html
    assert f"pass <b>{n_gates}</b> verifier gates" in html
    names = [t.name for t in corpus.load_targets() if getattr(t, "name", None)]
    assert any(name in html for name in names)


def test_howto_main_writes_the_page(tmp_path):
    import sys
    argv = sys.argv
    sys.argv = ["howto", str(tmp_path)]
    try:
        assert main() == 0
    finally:
        sys.argv = argv
    out = tmp_path / "howto.html"
    assert out.exists()
    assert "How to Play Cathedral Arena" in out.read_text(encoding="utf-8")
