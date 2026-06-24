"""Verify-by-receipt provenance — direct unit proofs (fast, no engine).

A real agent signs a hash-chained run-receipt; the arena trusts the chain, not
the agent. Tampering, wrong-key, or replay must all be caught.
"""
from __future__ import annotations

from game.arena.provenance import AgentKey, build_receipt, verify_receipt

STEPS = [
    {"kind": "tool_call", "name": "subnet.get_target", "input": 38, "output": "repo"},
    {"kind": "tool_call", "name": "solver.search", "input": "h", "output": 70},
    {"kind": "tool_call", "name": "subnet.submit_finding", "input": "a", "output": "ok"},
]
ARTIFACT = {"challenge_id": "pm-t2-e1000-abc", "cnf_sha256": "deadbeef"}


def _receipt(key=None):
    return build_receipt(
        key or AgentKey(seed=b"agent-x"), agent_id="agent-x", miner_hotkey="hk_x",
        mission_id="m1", nonce="n1", environment="polaris-tee-cpu",
        raw_steps=STEPS, artifact=ARTIFACT, attestation={"valid": True})


def _verify(r, seen=None):
    return verify_receipt(r, expected_hotkey="hk_x", expected_mission="m1",
                          expected_nonce="n1", expected_artifact=ARTIFACT,
                          replay_ok=True, attestation_required=True, seen_receipts=seen or set())


def test_honest_receipt_grades_A():
    v = _verify(_receipt())
    assert v.signature_ok and v.chain_intact and v.head_binds_artifact and v.ok
    assert v.grade == "A"


def test_tampered_step_breaks_chain_and_signature():
    r = _receipt()
    r.steps[1].output_digest = "tampered"            # edit after signing
    v = _verify(r)
    assert v.signature_ok is False
    assert v.chain_intact is False
    assert v.ok is False and v.grade == "F"


def test_wrong_key_signature_rejected():
    r = _receipt()
    r.agent_pubkey = AgentKey(seed=b"impostor").pub_hex   # claim a different identity
    v = _verify(r)
    assert v.signature_ok is False and v.grade == "F"


def test_replayed_receipt_rejected():
    r = _receipt()
    v1 = _verify(r)
    assert v1.ok
    seen = {__import__("game.arena.provenance", fromlist=["_h"])._h(r.body()) + r.sig}
    v2 = _verify(r, seen=seen)
    assert v2.not_replayed_receipt is False and v2.ok is False


def test_receipt_must_bind_the_real_artifact():
    r = _receipt()
    r.artifact = {"challenge_id": "pm-t2-e1000-OTHER", "cnf_sha256": "deadbeef"}
    # re-sign so the signature passes but the bound artifact no longer matches
    r.sig = AgentKey(seed=b"agent-x").sign(r.body())
    v = _verify(r)
    assert v.head_binds_artifact is False and v.ok is False


def test_missing_attestation_downgrades_grade():
    key = AgentKey(seed=b"agent-x")
    r = build_receipt(key, agent_id="agent-x", miner_hotkey="hk_x", mission_id="m1",
                      nonce="n1", environment="local-untrusted", raw_steps=STEPS,
                      artifact=ARTIFACT, attestation=None)
    v = _verify(r)
    assert v.ok is True            # provenance chain is intact
    assert v.grade == "B-"         # but required attestation is absent
