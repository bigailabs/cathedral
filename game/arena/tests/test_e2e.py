"""Full-pipeline E2E — one round, every subsystem asserted to cohere end to end.
The regression capstone: corpus → agents/gates → reward → emissions → signed
vector → Merkle anchor → portable proof bundle → unified minted proof →
solver bench → sybil collapse → live UI.
"""
from __future__ import annotations

import pytest

from game.arena import anchor as _anchor
from game.arena import bundle as _bundle
from game.arena import mint as _mint
from game.arena.engine import ArenaEngine
from game.reward import verify_vector
from game.arena.ui import render

CHEAT_GATE = {
    "copier": "witness_verifies", "fake_attest": "attestation_valid",
    "wrong_owner": "correct_owner", "spam": "no_replay",
    "bad_encoder": "cnf_hash_matches", "forge_trace": "agent_signature_valid",
    "bad_replay": "replay_succeeds", "stale_nonce": "fresh_nonce",
    "no_decode_map": "decode_map_present", "fake_compute_profile": "compute_profile_honest",
    "misclassify": "hypothesis_aligned",
}
ARCH = {
    "agent-aurora": "honest", "agent-borealis": "honest", "agent-cygnus": "honest",
    "agent-magpie": "copier", "agent-cuckoo": "fake_attest", "agent-jackdaw": "wrong_owner",
    "agent-locust": "spam", "agent-weevil": "bad_encoder", "agent-mantis": "forge_trace",
    "agent-hornet": "bad_replay", "agent-cricket": "stale_nonce", "agent-termite": "no_decode_map",
    "agent-wasp": "fake_compute_profile", "agent-moth": "misclassify",
    "agent-swarm-a": "hotkey_stacking", "agent-swarm-b": "hotkey_stacking",
}


@pytest.fixture(scope="module")
def game():
    return ArenaEngine().run(1)


def test_corpus_is_real(game):
    cs = game.corpus_summary
    assert cs["audit_hunter_present"] and cs["targets"] == 17 and cs["proof_tasks"] >= 28


def test_every_cheat_rejected_by_its_gate(game):
    by_arch = {ARCH[a.run.agent_id]: a for a in game.agents}
    for arch, gate in CHEAT_GATE.items():
        a = by_arch[arch]
        assert a.gates.passed() is False
        assert a.gates.first_failure() == gate
        assert game.emissions[a.run.miner_hotkey] == 0.0


def test_honest_agents_earn_and_anchor_round(game):
    honest = [a for a in game.agents if ARCH[a.run.agent_id] == "honest"]
    assert honest and all(a.gates.passed() for a in honest)
    assert all(game.emissions[a.run.miner_hotkey] > 0 for a in honest)


def test_reward_is_metric_times_gate(game):
    for a in game.agents:
        contrib = a.credit.contrib
        assert (contrib > 0) == a.gates.passed()


def test_emission_signed_vector_and_anchor_verify(game):
    assert verify_vector(game.signed_vector)
    assert _anchor.verify_anchor(game.anchor, game.agents, game.signed_vector)


def test_top_earner_proof_bundle_verifies_standalone(game):
    top = max((a for a in game.agents if a.gates.passed()),
              key=lambda a: game.emissions[a.run.miner_hotkey])
    b = _bundle.build_bundle(game, top.run.agent_id)
    v = _bundle.verify_bundle(b)
    assert v["ok"] and all(v["checks"].values())


def test_sybil_collapse_and_solver_bench(game):
    swarm = next(p for p in game.sybil_panel if p["coldkey"] == "ck_swarm")
    assert swarm["naive"] >= swarm["collapsed"]
    bench = game.solver_bench
    assert bench[0]["crown"] and bench[0]["solved"] > 0
    assert any(b["solved"] == 0 for b in bench)            # the liar solver


def test_unified_minted_proof_if_z3(game):
    if not _mint.z3_available():
        return
    mp = game.operator_console["minted_proof"]
    if mp.get("available") and mp["external_solve"].get("available"):
        assert mp["ok"] is True                            # encode→solve→reproduce


def test_full_ui_renders(game):
    html = render(game)
    for panel in ("Attack Map", "Breach Feed", "Replay Theater", "Solver Bench",
                  "Anti-Cheat Feed", "Hotkey-Stacking Guard", "Round Proof Anchor",
                  "Operator Console"):
        assert panel in html, f"missing UI panel: {panel}"


def test_attack_map_animates_real_agents(game):
    """The attack map shows the REAL agents working each target as animated
    markers (green=verified, red=rejected), and active cells scan — agent
    animation tied to live state, not decoration."""
    html = render(game)
    # one animated marker per agent that attacked a target this round
    assert html.count('class="amk') == len(game.agents)
    assert ".cell.active::after" in html and "@keyframes sweep" in html   # the scan animation
    assert ' active">' in html                                  # at least one active cell
    # markers are colored by the agent's REAL verdict (some pass, some rejected)
    assert 'class="amk ok"' in html and 'class="amk bad"' in html
    # every active cell names how many agents are on it
    assert "agents-on" in html
