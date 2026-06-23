"""Primitive-level anti-cheat proofs (fast — no sandbox).

These assert the properties the game relies on directly against the real
scaffold primitives, independent of the round orchestration: a stolen answer
cannot be redeemed, and a foreign challenge-id cannot be claimed.
"""
from __future__ import annotations

from game import config
from scaffold.dimacs import solve_cnf, verify_witness
from scaffold.publisher import per_miner as PM

EPOCH = config.GAME_EPOCH


def test_two_miners_get_different_cnfs():
    """Same (epoch, tier, seq), different hotkey => different CNF + different id.
    This is the structural basis of anti-copy."""
    a_cid, a_cnf, _ = PM.generate_instance("alice", EPOCH, 1, 0)
    b_cid, b_cnf, _ = PM.generate_instance("bob", EPOCH, 1, 0)
    assert a_cid != b_cid
    assert a_cnf != b_cnf


def test_stolen_answer_does_not_satisfy_my_cnf():
    """Alice solves her instance; Bob cannot reuse her assignment on his."""
    _a_cid, a_cnf, _ = PM.generate_instance("alice", EPOCH, 2, 0)
    _b_cid, b_cnf, _ = PM.generate_instance("bob", EPOCH, 2, 0)
    alice_solution = solve_cnf(a_cnf)
    assert verify_witness(a_cnf, alice_solution)        # valid for Alice
    assert not verify_witness(b_cnf, alice_solution)    # worthless for Bob


def test_foreign_challenge_id_rejected():
    """Bob submits ALICE's challenge_id + Alice's valid answer under his hotkey.
    The publisher scans Bob's own allotment, never finds the id, and rejects —
    even though the answer is a genuine satisfying assignment for that CNF."""
    alice_cid, alice_cnf, _ = PM.generate_instance("alice", EPOCH, 1, 0)
    alice_solution = solve_cnf(alice_cnf)
    ok, reason = PM.verify_miner_submission("bob", EPOCH, alice_cid, alice_solution)
    assert ok is False
    assert reason == "challenge_id_not_in_miner_set"


def test_own_answer_accepted():
    """Sanity: a miner's own correct answer to its own id verifies."""
    cid, cnf, _ = PM.generate_instance("alice", EPOCH, 1, 0)
    sol = solve_cnf(cnf)
    ok, reason = PM.verify_miner_submission("alice", EPOCH, cid, sol)
    assert ok is True and reason is None


def test_generation_is_deterministic():
    """Same inputs => byte-identical CNF (reproducible game, stable ids)."""
    c1 = PM.generate_instance("alice", EPOCH, 2, 1)
    c2 = PM.generate_instance("alice", EPOCH, 2, 1)
    assert c1[0] == c2[0] and c1[1] == c2[1]
