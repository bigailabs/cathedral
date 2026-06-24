"""Proof anchoring — the round is one verifiable Merkle commitment: receipt
heads + signed vector fold into a signed root; inclusion is provable; any tamper
breaks it.
"""
from __future__ import annotations

from game.arena import anchor
from game.arena.engine import ArenaEngine


def test_merkle_root_deterministic():
    leaves = ["aa" * 32, "bb" * 32, "cc" * 32]
    assert anchor.merkle_root(leaves) == anchor.merkle_root(leaves)


def test_inclusion_proof_verifies_for_every_leaf():
    leaves = [f"{i:064x}" for i in range(7)]       # odd count exercises duplication
    root = anchor.merkle_root(leaves)
    for i, leaf in enumerate(leaves):
        proof = anchor.merkle_proof(leaves, i)
        assert anchor.verify_merkle_proof(leaf, proof, root)


def test_tampered_leaf_fails_inclusion():
    leaves = [f"{i:064x}" for i in range(5)]
    root = anchor.merkle_root(leaves)
    proof = anchor.merkle_proof(leaves, 2)
    assert anchor.verify_merkle_proof("00" * 32, proof, root) is False


def test_changing_any_leaf_changes_root():
    a = [f"{i:064x}" for i in range(6)]
    b = list(a); b[3] = "ff" * 32
    assert anchor.merkle_root(a) != anchor.merkle_root(b)


def test_round_anchor_verifies():
    r = ArenaEngine().run(1)
    assert r.anchor["merkle_root"] and r.anchor["n_leaves"] == len(r.agents) + 1
    assert anchor.verify_anchor(r.anchor, r.agents, r.signed_vector) is True


def test_round_anchor_detects_mutated_result():
    r = ArenaEngine().run(1)
    # mutate one agent's receipt head -> the re-derived root no longer matches
    r.agents[0].run.trace_sha256 = "de" * 32
    assert anchor.verify_anchor(r.anchor, r.agents, r.signed_vector) is False


def test_agent_can_prove_its_proof_was_anchored():
    r = ArenaEngine().run(1)
    leaves = anchor.round_leaves(r.agents, r.signed_vector)
    head = sorted(a.run.trace_sha256 for a in r.agents if a.run.trace_sha256)[0]
    proof = anchor.merkle_proof(leaves, leaves.index(head))
    assert anchor.verify_merkle_proof(head, proof, r.anchor["merkle_root"])
