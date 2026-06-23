"""Portable proof bundle — a winning agent's proof is independently verifiable
end-to-end (receipt signature + chain + artifact binding + real replay + Merkle
inclusion + anchor signature), and ANY tamper is caught. No engine needed to verify.
"""
from __future__ import annotations

import copy

import pytest

from game.arena import bundle
from game.arena.engine import ArenaEngine


@pytest.fixture(scope="module")
def good_bundle():
    r = ArenaEngine().run(1)
    winner = next(a for a in r.agents if a.gates.passed()).run.agent_id
    return bundle.build_bundle(r, winner)


def test_bundle_verifies_end_to_end(good_bundle):
    v = bundle.verify_bundle(good_bundle)
    assert v["ok"] is True
    assert all(v["checks"].values())
    # every load-bearing check is present
    for k in ("receipt_signature", "receipt_chain", "receipt_binds_artifact",
              "replay_reproduces", "merkle_inclusion", "anchor_signature"):
        assert k in v["checks"]


def test_tampered_receipt_step_fails(good_bundle):
    b = copy.deepcopy(good_bundle)
    b["receipt"]["steps"][0]["output_digest"] = "tampered"
    v = bundle.verify_bundle(b)
    assert v["ok"] is False
    assert v["checks"]["receipt_signature"] is False


def test_tampered_witness_fails_replay(good_bundle):
    if not good_bundle.get("replay"):
        return
    b = copy.deepcopy(good_bundle)
    b["replay"]["witness"] = {}                    # strip the decode map -> no reproduce
    v = bundle.verify_bundle(b)
    assert v["ok"] is False
    assert v["checks"]["replay_reproduces"] is False


def test_tampered_merkle_path_fails_inclusion(good_bundle):
    b = copy.deepcopy(good_bundle)
    if b["merkle_inclusion"]["path"]:
        b["merkle_inclusion"]["path"][0]["hash"] = "00" * 32
    else:
        b["merkle_inclusion"]["leaf"] = "00" * 32
    v = bundle.verify_bundle(b)
    assert v["checks"]["merkle_inclusion"] is False
    assert v["ok"] is False


def test_tampered_anchor_signature_fails(good_bundle):
    b = copy.deepcopy(good_bundle)
    b["anchor"]["merkle_root"] = "ff" * 32         # change the committed root
    v = bundle.verify_bundle(b)
    # both the inclusion-vs-anchor match AND the anchor signature break
    assert v["ok"] is False
    assert v["checks"]["anchor_signature"] is False or v["checks"]["inclusion_matches_anchor"] is False


def test_bundle_carries_proof_provenance(good_bundle):
    """The bundle records WHAT was proven (z3-minted / audit_lane / arena-port,
    content-addressed) and the verifier ties it to the real registered target."""
    pp = good_bundle.get("proof_provenance")
    if pp is None:
        return                                         # agent proved no replay target
    assert pp["source"] in ("z3-factory-mint", "audit_lane", "arena-port")
    v = bundle.verify_bundle(good_bundle)
    assert v["checks"]["proof_source_registered"] is True


def test_faked_proof_source_caught(good_bundle):
    if not good_bundle.get("proof_provenance"):
        return
    b = copy.deepcopy(good_bundle)
    b["proof_provenance"]["source"] = "hand-written-fake"   # claim a different provenance
    v = bundle.verify_bundle(b)
    assert v["ok"] is False and v["checks"]["proof_source_registered"] is False


def test_corroborating_stitch_evidence_consistent(good_bundle):
    """If the round carried a real Stitch cross-proof, the bundle's copy is
    internally consistent (commitment recomputes) and tampering is caught."""
    ce = good_bundle.get("corroborating_evidence")
    if not ce:
        return                                         # no Stitch receipt this round
    assert ce["kind"] == "stitch-real-cnf-cross-proof"
    assert bundle.verify_bundle(good_bundle)["checks"]["corroborating_commitment"] is True
    b = copy.deepcopy(good_bundle)
    b["corroborating_evidence"]["remote_wall_ms"] = 999.0    # tamper a measured field
    v = bundle.verify_bundle(b)
    assert v["ok"] is False and v["checks"]["corroborating_commitment"] is False


def test_verify_uses_no_engine():
    # the verifier imports only verification primitives, never the engine
    import game.arena.bundle as b
    src = open(b.__file__, encoding="utf-8").read()
    assert "from .engine" not in src and "import engine" not in src
