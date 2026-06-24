"""Arena E2E + anti-cheat assertions. One round is run once and shared.

Each cheat archetype must be rejected by a SPECIFIC named gate, honest agents
must earn, and the emitted vector must be independently verifiable.
"""
from __future__ import annotations

import pytest

from game.arena import corpus
from game.arena.engine import ArenaEngine
from game.reward import verify_vector


@pytest.fixture(scope="module")
def arena():
    return ArenaEngine().run(1)


def _agent(arena, archetype):
    eng_specs = {a.run.miner_hotkey: a for a in arena.agents}
    return next(a for a in arena.agents
               if any(a.run.miner_hotkey == hk for hk in eng_specs)
               and a.run.agent_id.startswith("agent")
               and a.mission and _arch(a) == archetype)


def _arch(a):
    return {
        "agent-aurora": "honest", "agent-borealis": "honest", "agent-cygnus": "honest",
        "agent-magpie": "copier", "agent-cuckoo": "fake_attest",
        "agent-jackdaw": "wrong_owner", "agent-locust": "spam", "agent-weevil": "bad_encoder",
        "agent-mantis": "forge_trace", "agent-hornet": "bad_replay",
        "agent-cricket": "stale_nonce", "agent-termite": "no_decode_map",
        "agent-wasp": "fake_compute_profile", "agent-moth": "misclassify",
        "agent-swarm-a": "hotkey_stacking", "agent-swarm-b": "hotkey_stacking",
    }[a.run.agent_id]


# -- real corpus is grounded --------------------------------------------------

def test_real_corpus_loaded():
    cs = corpus.corpus_summary()
    assert cs["audit_hunter_present"] is True
    assert cs["targets"] == 17          # the 17 registered subnet targets
    assert cs["proof_tasks"] >= 28      # the subtensor money-math CNF corpus
    assert cs["sat"] > 0 and cs["unsat"] > 0


# -- honest agents earn -------------------------------------------------------

def test_honest_agents_earn(arena):
    honest = [a for a in arena.agents if _arch(a) == "honest"]
    assert len(honest) == 3
    for a in honest:
        assert a.gates.passed()
        assert arena.weights[a.run.miner_hotkey] > 0


def test_premium_attested_tier_outscores_floor(arena):
    # tier-2 attested honest agents should beat the tier-1 honest agent
    weights = {a.run.agent_id: arena.weights[a.run.miner_hotkey]
               for a in arena.agents if _arch(a) == "honest"}
    assert max(weights.values()) > min(weights.values())


# -- each cheat rejected by its specific gate ---------------------------------

@pytest.mark.parametrize("archetype,gate", [
    ("copier", "witness_verifies"),
    ("fake_attest", "attestation_valid"),
    ("wrong_owner", "correct_owner"),
    ("spam", "no_replay"),
    ("bad_encoder", "cnf_hash_matches"),
    ("forge_trace", "agent_signature_valid"),
    ("bad_replay", "replay_succeeds"),
    ("stale_nonce", "fresh_nonce"),
    ("no_decode_map", "decode_map_present"),
    ("fake_compute_profile", "compute_profile_honest"),
    ("misclassify", "hypothesis_aligned"),
])
def test_cheater_rejected_by_gate(arena, archetype, gate):
    a = next(x for x in arena.agents if _arch(x) == archetype)
    assert a.gates.passed() is False
    assert a.gates.first_failure() == gate
    assert arena.weights[a.run.miner_hotkey] == 0.0
    assert arena.emissions[a.run.miner_hotkey] == 0.0
    assert getattr(a.gates, gate) is False


def test_anticheat_feed_lists_all_cheaters(arena):
    rejected = {x["archetype"] for x in arena.anticheat_feed}
    assert rejected == {"copier", "fake_attest", "wrong_owner", "spam",
                        "bad_encoder", "forge_trace", "bad_replay", "stale_nonce",
                        "no_decode_map", "fake_compute_profile", "misclassify"}


def test_agent_method_describes_what_it_actually_did(arena):
    """Every agent's trace records the METHOD it used — honest work for honest agents,
    the specific divergence for each cheat — so the trace is honest training data and
    the UI shows 'what it did', not just 'which gate caught it'."""
    methods = {a.run.agent_id: a.run.method for a in arena.agents}
    assert all(m for m in methods.values())              # never blank
    # honest agents describe a real solve+reproduce
    for a in arena.agents:
        if _arch(a) == "honest":
            assert "decoded its own" in a.run.method and "reproducing" in a.run.method
    # each distinct cheat archetype has a distinct method, and it names its gate
    gate_for = dict([
        ("copier", "witness_verifies"), ("wrong_owner", "correct_owner"),
        ("spam", "no_replay"), ("bad_encoder", "cnf_hash_matches"),
        ("forge_trace", "agent_signature_valid"), ("bad_replay", "replay_succeeds"),
        ("stale_nonce", "fresh_nonce"), ("no_decode_map", "decode_map_present"),
        ("fake_attest", "attestation_valid"),
        ("fake_compute_profile", "compute_profile_honest"),
        ("misclassify", "hypothesis_aligned"),
    ])
    cheat_methods = {}
    for a in arena.agents:
        arch = _arch(a)
        if arch in gate_for:
            assert gate_for[arch] in a.run.method        # the method names the gate it trips
            cheat_methods[arch] = a.run.method
    assert len(set(cheat_methods.values())) == len(cheat_methods)   # all distinct


def test_anticheat_feed_carries_the_method(arena):
    for x in arena.anticheat_feed:
        assert x.get("method") and x["rejected_by"] in x["method"]


def test_ui_renders_with_all_panels(arena):
    from game.arena.ui import render
    html = render(arena)                          # must not raise
    assert "<!doctype html>" in html
    assert "Hotkey-Stacking Guard" in html        # sybil panel
    assert "Solver Bench" in html and "Replay Theater" in html
    assert "Anti-Cheat Feed" in html


def test_rules_of_the_arena_panel_is_data_driven(arena):
    """The 60-second onboarding panel states the win rule and counts the REAL gate
    set + anti-cheat taxonomy - counts pulled from the engine, so they can't drift."""
    from game.arena.ui import render
    from game.arena.models import GateOutcome
    from game.arena import reports
    html = render(arena)
    assert "Rules of the Arena" in html
    assert "reward = linear_metric x boolean_gate" in html
    # the gate count + axis count in the panel come from the real sources
    assert f"All {len(GateOutcome.GATES)} boolean gates must pass" in html
    assert f"{len(reports.ANTICHEAT_AXES)} anti-cheat axes" in html
    # the four steps are present (agent -> proof -> win -> why-cheating-fails)
    for step in ("1 Your agent", "2 The proof", "3 How you win", "4 Why cheating fails"):
        assert step in html


def test_hotkey_stacking_collapsed(arena):
    # two hotkeys under one coldkey (ck_swarm) are capped at one identity's share
    assert arena.sybil_panel
    swarm = next(p for p in arena.sybil_panel if p["coldkey"] == "ck_swarm")
    assert len(swarm["hotkeys"]) == 2
    combined = arena.weights.get("hk_swarm_a", 0) + arena.weights.get("hk_swarm_b", 0)
    assert combined <= 1.0 + 1e-6                 # no stacking multiplier
    assert swarm["naive"] >= swarm["collapsed"]   # collapse removed the stacking gain


# -- REAL money-math replay ---------------------------------------------------

def test_honest_agents_reproduce_real_violation(arena):
    theater = {t["agent"]: t for t in arena.replay_theater}
    for a in arena.agents:
        if _arch(a) == "honest":
            assert theater[a.run.agent_id]["reproduced"] is True
            assert a.gates.replay_succeeds is True


def test_bad_replay_does_not_reproduce(arena):
    hornet = next(a for a in arena.agents if _arch(a) == "bad_replay")
    theater = {t["agent"]: t for t in arena.replay_theater}
    assert theater["agent-hornet"]["reproduced"] is False
    assert hornet.gates.replay_succeeds is False


# -- provenance: verify-by-receipt --------------------------------------------

def test_honest_agents_have_high_provenance(arena):
    for a in arena.agents:
        if _arch(a) == "honest":
            assert a.gates.agent_signature_valid is True
            assert a.gates.provenance_chain_intact is True
            assert a.gates.provenance_grade in ("A", "B")


def test_forged_trace_fails_provenance(arena):
    m = next(a for a in arena.agents if _arch(a) == "forge_trace")
    assert m.gates.agent_signature_valid is False
    assert m.gates.provenance_chain_intact is False
    assert m.gates.provenance_grade == "F"
    # attestation itself was fine — the forgery is what sank it
    assert m.gates.attestation_valid is True


# -- emissions economy --------------------------------------------------------

def test_emissions_only_to_verified_breachers(arena):
    for a in arena.agents:
        e = arena.emissions[a.run.miner_hotkey]
        assert (e > 0) == a.gates.passed()


def test_breach_feed_matches_verified(arena):
    assert len(arena.breaks) == sum(1 for a in arena.agents if a.gates.passed())
    assert all(b["bounty"] > 0 for b in arena.breaks)


def test_chain_vaults_from_real_corpus(arena):
    assert arena.chain_vaults
    statuses = {v["status"] for v in arena.chain_vaults}
    assert statuses & {"CRACKED", "HARDENED", "OPEN BOUNTY"}


# -- attestation is a real boolean gate ---------------------------------------

def test_attestation_required_and_gated(arena):
    fake = next(x for x in arena.agents if _arch(x) == "fake_attest")
    assert fake.mission.attestation_required is True
    assert fake.gates.attestation_valid is False     # mocked-tee never scores trusted


# -- agent traces are complete (future training data) -------------------------

def test_agent_traces_complete(arena):
    for a in arena.agents:
        run = a.run
        assert run.agent_id and run.miner_hotkey and run.environment
        assert run.mission_id and run.target_netuid
        assert run.commands and run.hypothesis and run.encoder
        assert run.artifact.get("challenge_id")
        assert run.trace_sha256


# -- emission verifies + excludes cheaters ------------------------------------

def test_signed_vector_verifies_and_excludes_cheaters(arena):
    assert verify_vector(arena.signed_vector)
    hks = {w["miner_hotkey"] for w in arena.signed_vector["weights"]}
    for a in arena.agents:
        if not a.gates.passed():                  # only rejected agents are excluded
            assert a.run.miner_hotkey not in hks
    assert "hk_aurora" in hks


# -- the rule holds: reward = metric x (all gates) ----------------------------

def test_reward_is_metric_times_gate(arena):
    for a in arena.agents:
        gate_all = a.gates.passed()
        contrib = a.credit.contrib
        if not gate_all:
            assert contrib == 0.0
        else:
            assert contrib > 0.0
