"""The agent reasoning layer — every real subnet target maps to a sensible
invariant family with grounded reasoning; the gated Claude policy is mock-tested
and falls back deterministically with no key; the reasoning step lands in the
real tool trace and the agent run record.
"""
from __future__ import annotations

import json

from game.arena import corpus, hypothesis as H
from game.arena.engine import ArenaEngine
from game.arena.models import Target


def test_every_target_maps_to_a_rulebook_family():
    targets = corpus.load_targets()
    assert len(targets) >= 10                      # real corpus or bundled fallback
    for t in targets:
        h = H.form_hypothesis(t)
        assert h["family"] in H.RULEBOOK            # a real taxonomy family
        assert h["invariant"] and h["rule"]         # carries the encode plan
        assert h["source"] == "deterministic-rulebook"
        # the rationale is grounded in THIS target, not a generic label
        assert f"sn{t.netuid}" in h["rationale"]
        assert t.name in h["rationale"]
        assert len(h["rationale"]) > 80
        # the rationale states an invariant + a proof plan (not a claim)
        assert "Invariant" in h["rationale"] and "Proof plan" in h["rationale"]


def test_classify_uses_keyword_evidence_over_coarse_family():
    # an emission/over-pay finding -> F_emission even if the heat-map family differs
    t = Target(netuid=1, name="sn-pay", repo="r", our_uid=None,
               candidate_title="validator double-pays emission to a miner",
               severity=8, location="rewards.rs:42", exploit_steps="trigger payout twice",
               exploit_resource="none", risk_level="local-replay")
    assert H.classify(t) == "F_emission"
    # an overflow/silent-zero finding -> B_bounds
    t2 = Target(netuid=2, name="sn-fee", repo="r", our_uid=None,
                candidate_title="U64F64 fee truncates to silent zero on small amounts",
                severity=7, location="fee.rs:10", exploit_steps="tiny amount",
                exploit_resource="none", risk_level="local-replay")
    assert H.classify(t2) == "B_bounds"


def test_families_are_distinct_across_the_corpus():
    # targets should not all collapse into one family; the taxonomy is used
    fams = {H.form_hypothesis(t)["family"] for t in corpus.load_targets()}
    assert len(fams) >= 3


def test_no_key_means_llm_unavailable_and_best_falls_back(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert H.llm_available() is False
    t = corpus.load_targets()[0]
    # with no client and no key, the LLM path returns None and best() is deterministic
    assert H.form_hypothesis_llm(t) is None
    best = H.form_hypothesis_best(t)
    assert best["source"] == "deterministic-rulebook"


class _MockResp:
    def __init__(self, payload):
        self.stop_reason = "end_turn"
        self.content = [type("B", (), {"type": "text", "text": json.dumps(payload)})()]


class _MockClient:
    """Stand-in for anthropic.Anthropic — records the call, returns a chosen family."""
    def __init__(self, payload):
        self._payload = payload
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return _MockResp(self._payload)


def test_llm_policy_chooses_family_via_mock_client():
    t = corpus.load_targets()[0]
    client = _MockClient({"family": "H_trust",
                          "rationale": "ownership can be replayed; encode the nonce binding."})
    h = H.form_hypothesis_llm(t, client=client)
    assert h is not None
    assert h["family"] == "H_trust"
    assert h["source"].startswith("llm:")
    assert "ownership" in h["rationale"]
    # the policy call used the right model + structured-output schema (rulebook-bounded)
    kw = client.calls[0]
    assert kw["model"] == H.LLM_MODEL
    enum = kw["output_config"]["format"]["schema"]["properties"]["family"]["enum"]
    assert set(enum) == set(H.RULEBOOK)


def test_llm_out_of_taxonomy_answer_is_rejected():
    t = corpus.load_targets()[0]
    bad = _MockClient({"family": "Z_made_up", "rationale": "x"})
    assert H.form_hypothesis_llm(t, client=bad) is None       # invalid family -> None


def test_llm_refusal_is_handled():
    t = corpus.load_targets()[0]

    class _Refuse:
        messages = property(lambda self: self)

        def create(self, **kw):
            r = _MockResp({"family": "A_conservation", "rationale": "x"})
            r.stop_reason = "refusal"
            return r
    assert H.form_hypothesis_llm(t, client=_Refuse()) is None


def test_reasoning_step_is_in_the_real_tool_trace():
    from game.arena.tools import run_workflow
    hyp = {"family": "B_bounds", "invariant": "no overflow", "rule": "encode fee math",
           "source": "deterministic-rulebook", "rationale": "sn1 ...: bounds weakness."}
    trace, _w = run_workflow(netuid=1, name="sn", repo="r", location="f:1",
                             candidate="c", replay_target_id="subtensor-amm:recalc-overcharge@HEAD",
                             artifact={}, hypothesis=hyp)
    names = [tc.tool for tc in trace]
    assert "reason.propose_hypothesis" in names
    # reasoning sits AFTER inspect and BEFORE encode (decide family, then encode it)
    assert names.index("reason.propose_hypothesis") < names.index("z3.encode_invariant")
    assert names.index("code-fetch.inspect") < names.index("reason.propose_hypothesis")


def test_hypothesis_alignment_gate_is_load_bearing():
    """The agent must correctly classify the invariant family of the proof it
    commits. An honest agent commits the proven target's true family (and that
    family is in its SIGNED artifact); a misclassifier is rejected."""
    from game.arena import replay
    res = ArenaEngine().run(1)

    # honest: committed family == the proven replay target's true family, signed
    a = next(a for a in res.agents if a.gates.passed())
    declared = replay.TARGETS[a.mission.replay_target_id].family
    assert a.gates.hypothesis_aligned is True
    assert a.receipt.artifact["proof_family"] == declared      # committed + signed

    # misclassifier: solves + attests but mislabels -> alignment is its ONLY failure
    moth = next(a for a in res.agents if a.run.agent_id == "agent-moth")
    assert moth.gates.first_failure() == "hypothesis_aligned"
    assert any("misclassified_invariant" in r for r in moth.gates.reasons)
    assert res.emissions[moth.run.miner_hotkey] == 0.0         # reward = metric x gate
    truth = replay.TARGETS[moth.mission.replay_target_id].family
    assert moth.receipt.artifact["proof_family"] != truth      # the committed lie


def test_alignment_tolerates_missing_claim():
    # the gate is in the full gate set (one more boolean)
    from game.arena.models import GateOutcome
    assert "hypothesis_aligned" in GateOutcome.GATES


def test_reasoning_drives_proof_selection():
    """The family the agent reasons for a subnet SELECTS the invariant it proves:
    when a reproducing invariant of that family exists, the proven target is that
    family (coherent). A_conservation falls back (its invariant is hardened)."""
    from game.arena import replay
    from game.arena.engine import ArenaEngine, REPRODUCING_TARGETS
    eng = ArenaEngine()
    repro_fams = {replay.TARGETS[t].family for t in REPRODUCING_TARGETS if t in replay.TARGETS}
    res = eng.run(1)
    coherent = 0
    for p in res.proof_feed:
        if p["family"] in repro_fams:
            # a matchable family MUST prove exactly that family
            assert p["proof_family"] == p["family"], (p["agent"], p["family"], p["proof_family"])
            assert p["reasoning_coherent"] is True
            coherent += 1
        else:
            # only A_conservation (hardened — no reproducing exploit) falls back
            assert p["family"] == "A_conservation"
            assert p["reasoning_coherent"] is False
    assert coherent >= 10                              # most of the field is coherent


def test_proof_selection_keeps_honest_passing_and_aligned():
    from game.arena.engine import ArenaEngine
    res = ArenaEngine().run(1)
    honest = [a for a in res.agents if a.gates.passed()]
    assert honest and all(a.gates.hypothesis_aligned for a in honest)
    # every honest proof reproduces a REAL invariant (replay actually succeeded)
    assert all(a.gates.replay_succeeds for a in honest)


def test_engine_records_real_reasoning_not_a_label():
    res = ArenaEngine().run(1)
    # an honest agent's run carries the structured rationale, not the bare title
    a = next(a for a in res.agents if a.gates.passed())
    assert "Invariant" in a.run.hypothesis and "Proof plan" in a.run.hypothesis
    assert a.run.hypothesis != a.mission.target.candidate_title
    # the encoder names the chosen family + policy source
    assert "deterministic-rulebook" in a.run.encoder
    # the proof feed surfaces the reasoned family + policy for the UI
    pf = next(p for p in res.proof_feed if p["agent"] == a.run.agent_id)
    assert pf["family"] in H.RULEBOOK
    assert pf["policy"] == "deterministic-rulebook"
    # the reasoning step is in the agent's signed receipt trace
    assert "reason.propose_hypothesis" in a.run.commands
