"""REAL external-agent submission — agents are separate OS processes that sign
their own run-receipts; the arena only verifies (verify-by-receipt) and checks
the hotkey->agent-key delegation. A small roster keeps the subprocess cost low.
"""
from __future__ import annotations

import pytest

from game.arena.engine import ArenaEngine
from game.arena.roster import AgentSpec
from game.arena.agent_cli import run_agent
from game.arena.provenance import receipt_from_dict, verify_receipt


ROSTER = [
    AgentSpec("a-honest", "hk_h", "ck_h", "honest", "stitch-runner", True),
    AgentSpec("a-copier", "hk_c", "ck_c", "copier", "local-untrusted", True, cheat="copy_witness"),
    AgentSpec("a-forger", "hk_f", "ck_f", "forge_trace", "stitch-runner", True, cheat="forge_trace"),
    AgentSpec("a-impostor", "hk_i", "ck_i", "impostor", "stitch-runner", True, cheat="impostor"),
    AgentSpec("a-badreplay", "hk_r", "ck_r", "bad_replay", "stitch-runner", True, cheat="bad_replay"),
    AgentSpec("a-misclass", "hk_m", "ck_m", "misclassify", "polaris-tee-cpu", True, cheat="misclassify"),
]


@pytest.fixture(scope="module")
def submitted():
    return ArenaEngine(roster=ROSTER).run_submitted(1)


def _by(submitted, aid):
    return next(a for a in submitted.agents if a.run.agent_id == aid)


def test_honest_external_submission_earns(submitted):
    a = _by(submitted, "a-honest")
    assert a.gates.passed() is True
    assert submitted.emissions[a.run.miner_hotkey] > 0
    assert a.gates.provenance_grade in ("A", "B")


def test_external_copier_rejected(submitted):
    a = _by(submitted, "a-copier")
    assert a.gates.passed() is False
    assert a.gates.first_failure() == "witness_verifies"


def test_external_forger_fails_provenance(submitted):
    a = _by(submitted, "a-forger")
    assert a.gates.agent_signature_valid is False
    assert a.gates.provenance_grade == "F"


def test_impostor_key_fails_delegation(submitted):
    """A receipt validly signed by a NON-delegated key is rejected: the agent
    key is not the one the hotkey delegated to."""
    a = _by(submitted, "a-impostor")
    assert a.gates.passed() is False
    assert a.gates.valid_identity is False
    assert "agent_key_not_delegated_to_hotkey" in a.gates.reasons
    # its own signature is internally valid — delegation, not forgery, caught it
    assert a.gates.agent_signature_valid is True


def test_external_bad_replay_rejected(submitted):
    a = _by(submitted, "a-badreplay")
    assert a.gates.first_failure() == "replay_succeeds"


def test_external_agent_reasons_and_aligns(submitted):
    """Parity with the in-process path: the REAL external agent records the
    reasoning step in its signed trace and commits the proof's invariant family,
    so the honest agent aligns and the alignment gate applies to this path too."""
    a = _by(submitted, "a-honest")
    assert "reason.propose_hypothesis" in a.run.commands     # reasoning step is signed
    assert a.gates.hypothesis_aligned is True
    assert a.receipt.artifact.get("proof_family")            # committed in the signed artifact


def test_external_misclassifier_fails_alignment(submitted):
    """An external agent that commits the WRONG invariant family for its proof is
    rejected by hypothesis_aligned — the gate is exercised in the real-process path."""
    a = _by(submitted, "a-misclass")
    assert a.gates.passed() is False
    assert a.gates.first_failure() == "hypothesis_aligned"
    assert any("misclassified_invariant" in r for r in a.gates.reasons)


# -- the envelope a real agent emits is independently verifiable (no engine) ---

def test_agent_emits_verifiable_signed_envelope():
    packet = {
        "behavior": "honest", "agent_id": "a-x", "hotkey": "hk_x",
        "environment": "stitch-runner", "mission_id": "m1", "target_netuid": 38,
        "target_repo": "r", "target_location": "l", "target_title": "t", "target_family": "F",
        "cid": "pm-t1-e1000-deadbeefdeadbeefdeadbeef",
        "cnf": "p cnf 1 1\n1 0\n", "cnf_sha256": "x", "nonce": "n1", "tier": 1,
        "attestation_required": False, "replay_target_id": "subtensor-pallet:multi-take-split@HEAD",
    }
    env = run_agent(packet)
    receipt = receipt_from_dict(env["receipt"])
    v = verify_receipt(receipt, expected_hotkey="hk_x", expected_mission="m1",
                       expected_nonce="n1",
                       expected_artifact={"challenge_id": env["submission"]["submitted_cid"],
                                          "cnf_sha256": env["submission"]["submitted_cnf_hash"]},
                       replay_ok=True, attestation_required=False, seen_receipts=set())
    assert v.signature_ok and v.chain_intact and v.ok
