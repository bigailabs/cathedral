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


def test_proofboard_shows_formally_hardened_cross_confirmed_proofs():
    """The board surfaces the stronger proof class: z3 UNSAT cross-confirmed by an
    independent CDCL solver (a proof no exploit exists, not just stress-tested)."""
    from game.arena import replay
    hardened = [h for h in getattr(replay, "MINTED_HARDENED", []) if h.get("hardened")]
    if not hardened:
        return                                          # z3 absent on this host -> skip
    html = render_proofboard()
    assert "Formally Hardened" in html
    assert "FORMALLY HARDENED" in html
    assert "z3 UNSAT + CDCL UNSAT" in html and "cross-confirmed" in html
    # each cross-confirmed rule appears by id (e.g. the root TAO-split conservation proof)
    for h in hardened:
        assert h["rule_id"] in html


def test_proofboard_main_writes_file(tmp_path):
    import sys
    argv = sys.argv
    sys.argv = ["proofboard", str(tmp_path)]
    try:
        assert main() == 0
    finally:
        sys.argv = argv
    assert (tmp_path / "proofs.html").exists()
