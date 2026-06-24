"""Standalone deployability: the game runs without the external audit-hunter repo,
on the bundled-fallback corpus. A full round still coheres: agents reason + score,
cheaters are gated, scoring self-audits clean, the money-math replay is still REAL
(pure-Python ports, no external dep), and the visual UI renders. This is the
"portable, deployable game" guarantee the launch-prep relies on.
"""
from __future__ import annotations

from pathlib import Path

from game.arena import audit as _audit
from game.arena import corpus
from game.arena.ui import render


def test_corpus_falls_back_when_audit_hunter_absent(monkeypatch):
    monkeypatch.setattr(corpus, "_AUDIT_HUNTER", Path("/nonexistent/audit-hunter"))
    targets = corpus.load_targets()
    tasks = corpus.load_proof_tasks()
    cs = corpus.corpus_summary()
    assert cs["audit_hunter_present"] is False
    assert cs["source"] == "bundled-fallback"
    assert len(targets) >= 8 and len(tasks) >= 1        # a real playable corpus
    # the bundled targets are well-formed (every field the arena needs)
    for t in targets:
        assert t.netuid and t.name and t.candidate_title and t.family in {
            "A_conservation", "B_bounds", "F_emission", "G_scoring", "H_trust", "I_safety"}


def test_full_round_runs_on_the_bundled_corpus(monkeypatch):
    monkeypatch.setattr(corpus, "_AUDIT_HUNTER", Path("/nonexistent/audit-hunter"))
    from game.arena.engine import ArenaEngine     # engine loads the (now-fallback) corpus
    r = ArenaEngine().run(1)

    honest = [a for a in r.agents if a.gates.passed()]
    assert honest and all(r.emissions[a.run.miner_hotkey] > 0 for a in honest)
    # cheaters still gated with clear reasons, even on the bundled corpus
    rejected = [a for a in r.agents if not a.gates.passed()]
    assert len(rejected) >= 10
    assert all(r.emissions[a.run.miner_hotkey] == 0.0 for a in rejected)
    # the money-math REPLAY is still real (pure-Python ports, no audit-hunter needed)
    assert all(a.gates.replay_succeeds for a in honest)
    # scoring still self-audits clean
    assert _audit.audit_scoring(r)["ok"] is True


def test_visual_ui_renders_on_the_bundled_corpus(monkeypatch):
    monkeypatch.setattr(corpus, "_AUDIT_HUNTER", Path("/nonexistent/audit-hunter"))
    from game.arena.engine import ArenaEngine
    html = render(ArenaEngine().run(1))
    for panel in ("Attack Map", "Breach Feed", "Anti-Cheat Feed", "Operator Console"):
        assert panel in html
    assert "CATHEDRAL ARENA" in html


def test_real_corpus_still_preferred_when_present():
    # sanity: on this machine the REAL audit-hunter corpus is used (not the fallback)
    cs = corpus.corpus_summary()
    if cs["audit_hunter_present"]:
        assert cs["source"] == "audit-hunter"
        assert cs["targets"] >= 17
