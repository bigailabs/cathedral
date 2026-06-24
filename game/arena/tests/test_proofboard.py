"""The Proof Board: a clean visual of the real replay-harness differential."""
from __future__ import annotations

from game.arena.proofboard import render_proofboard, main
from game.arena.replay_differential import differential_report


def test_proofboard_renders_every_pinned_invariant():
    html = render_proofboard()
    rep = differential_report()
    assert "<!doctype html>" in html and "<style>" in html        # self-contained
    assert "Proof Board" in html
    # every registered target appears by id
    for r in rep["targets"]:
        assert r["target_id"] in html
    # the summary counts are the real ones
    assert f'<b>{rep["total"]}</b> pinned invariants' in html
    assert f'<b>{rep["exploit"]}</b> exploit' in html
    assert f'<b>{rep["conserved"]}</b> conserved' in html
    assert f'<b>{rep["discriminators"]}/{rep["total"]}</b>' in html


def test_proofboard_labels_exploit_vs_conserved():
    html = render_proofboard()
    assert "CRACKED" in html and "HARDENED" in html               # both kinds shown
    assert "proven discriminator" in html
    # the root NAV family landed and is shown
    assert "R_roundtrip" in html
    assert "subtensor-root:redeem-roundtrip@HEAD" in html


def test_proofboard_main_writes_file(tmp_path):
    import sys
    argv = sys.argv
    sys.argv = ["proofboard", str(tmp_path)]
    try:
        assert main() == 0
    finally:
        sys.argv = argv
    assert (tmp_path / "proofs.html").exists()
