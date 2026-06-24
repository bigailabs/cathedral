"""Scoring self-audit — the deterministic-scoring pillar, independently re-checked.
reward = linear_metric x boolean_gate must hold every round: cheaters zeroed,
honest earn, gate consistency, signed vector + anchor verify. The audit catches a
tampered result, and the gate outcomes are deterministic across re-runs.
"""
from __future__ import annotations

import copy

import pytest

from game.arena.audit import audit_scoring
from game.arena.engine import ArenaEngine
from game.arena.models import GateOutcome


@pytest.mark.parametrize("rnd", [1, 2, 3, 4, 5])
def test_scoring_audit_clean_every_round(rnd):
    v = audit_scoring(ArenaEngine().run(rnd))
    assert v["ok"] is True, v["violations"]
    for chk in ("reward_is_metric_times_gate", "cheaters_zeroed", "honest_earn",
                "gate_consistency", "signed_vector_verifies", "anchor_verifies"):
        assert v["checks"][chk] is True
    assert v["violations"] == []


def test_audit_attached_to_operator_console():
    oc = ArenaEngine().run(1).operator_console
    assert oc["scoring_audit"]["ok"] is True


def test_gate_outcomes_are_deterministic():
    """The boolean GATE outcomes are deterministic across re-runs (even though the
    speed metric carries timing noise) — same agent, same pass/fail + first gate."""
    a = ArenaEngine().run(1)
    b = ArenaEngine().run(1)
    ga = {x.run.agent_id: (x.gates.passed(), x.gates.first_failure(), x.gates.as_dict())
          for x in a.agents}
    gb = {x.run.agent_id: (x.gates.passed(), x.gates.first_failure(), x.gates.as_dict())
          for x in b.agents}
    assert ga == gb                                    # identical gate verdicts


def test_audit_catches_a_gate_bypass():
    """If an agent were paid while a gate failed, the audit flags it — the rule is
    actually enforced, not assumed."""
    r = ArenaEngine().run(1)
    cheat = next(a for a in r.agents if not a.gates.passed())
    # forge a positive emission for a gated-out agent (simulate a scoring bug)
    r.emissions[cheat.run.miner_hotkey] = 99.0
    v = audit_scoring(r)
    assert v["ok"] is False
    assert v["checks"]["cheaters_zeroed"] is False or \
           v["checks"]["reward_is_metric_times_gate"] is False
    assert any(viol["check"] in ("cheaters_zeroed", "reward_is_metric_times_gate")
               for viol in v["violations"])


def test_audit_catches_a_broken_signed_vector():
    r = ArenaEngine().run(1)
    r.signed_vector = copy.deepcopy(r.signed_vector)
    r.signed_vector["signature"] = "00" * 32           # tamper the signature
    v = audit_scoring(r)
    assert v["checks"]["signed_vector_verifies"] is False
    assert v["ok"] is False


def test_gate_consistency_definition_holds():
    # passed() is exactly "every boolean gate true" — the audit's structural check
    g = GateOutcome()
    assert g.passed() is False                         # all-false by default
    for name in GateOutcome.GATES:
        setattr(g, name, True)
    assert g.passed() is True
