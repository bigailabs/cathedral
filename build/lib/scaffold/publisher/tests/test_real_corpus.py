"""Tests for the Phase A real (unplanted) combinatorial challenge source.

Covers deploy/V2_FRONTIER_CUBE_AND_CONQUER_PLAN_2026-07-01.md Phase A
deliverable 5: the new source must be valid/deterministic/genuinely solvable,
and — the most important guardrail — leaving CATHEDRAL_V2_CHALLENGE_SOURCE
unset must be a byte-identical no-op vs. the pre-existing planted behaviour.
"""
from __future__ import annotations

import os

import pytest

from scaffold.dimacs import gen_planted_3sat, parse_cnf, solve_cnf, verify_witness
from scaffold.publisher import per_miner, real_corpus


HOTKEY = "5F_test_real_corpus_hotkey"
EPOCH = 424242


# --------------------------------------------------------------------------
# Deliverable: default no-op
# --------------------------------------------------------------------------

def test_default_source_is_planted():
    assert "CATHEDRAL_V2_CHALLENGE_SOURCE" not in os.environ
    assert real_corpus.challenge_source() == "planted"


def test_generate_instance_unset_flag_is_byte_identical_to_planted_path(monkeypatch):
    """The core no-op guarantee: with the flag unset, generate_instance's
    output must match exactly what the pre-existing planted computation
    produces — same challenge_id, same CNF text, same planted assignment."""
    monkeypatch.delenv("CATHEDRAL_V2_CHALLENGE_SOURCE", raising=False)
    tier = 1
    seq = 7

    cid, cnf_text, planted = per_miner.generate_instance(HOTKEY, EPOCH, tier, seq)

    # Recompute independently via the pre-existing primitives, bypassing the
    # new module entirely, and assert byte-for-byte equality.
    expected_cid = per_miner.instance_id(HOTKEY, EPOCH, tier, seq)
    expected_seed = per_miner.instance_seed(HOTKEY, EPOCH, tier, seq)
    n_vars, n_clauses = per_miner.shape_for(tier)
    expected_cnf, expected_planted = gen_planted_3sat(
        expected_seed, n_vars, n_clauses, method=per_miner.method_for(tier))

    assert cid == expected_cid
    assert cnf_text == expected_cnf
    assert planted == expected_planted
    assert planted is not None
    assert verify_witness(cnf_text, planted)


@pytest.mark.parametrize("tier,seq", [(1, 0), (2, 3)])
def test_generate_instance_unset_flag_matches_across_tiers_and_seqs(tier, seq, monkeypatch):
    monkeypatch.delenv("CATHEDRAL_V2_CHALLENGE_SOURCE", raising=False)
    cid1, cnf1, planted1 = per_miner.generate_instance(HOTKEY, EPOCH, tier, seq)
    cid2, cnf2, planted2 = per_miner.generate_instance(HOTKEY, EPOCH, tier, seq)
    assert (cid1, cnf1, planted1) == (cid2, cnf2, planted2)
    assert planted1 is not None


# --------------------------------------------------------------------------
# real_corpus: combinatorial generator — validity, determinism, solvability
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier", [1, 2])
def test_combinatorial_instance_is_valid_dimacs(tier):
    content_id, cnf_text = real_corpus.generate_combinatorial_instance(EPOCH, tier, 0)
    assert content_id
    n_vars, clauses = parse_cnf(cnf_text)
    assert n_vars > 0
    assert len(clauses) > 0
    for clause in clauses:
        assert clause  # no empty clauses
        for lit in clause:
            assert 1 <= abs(lit) <= n_vars


@pytest.mark.parametrize("tier", [1, 2])
def test_combinatorial_instance_is_deterministic(tier):
    a = real_corpus.generate_combinatorial_instance(EPOCH, tier, 5)
    b = real_corpus.generate_combinatorial_instance(EPOCH, tier, 5)
    assert a == b

    # Different seq must (with overwhelming probability) differ in content.
    c = real_corpus.generate_combinatorial_instance(EPOCH, tier, 6)
    assert a[1] != c[1]


def test_combinatorial_instance_is_not_hotkey_keyed():
    """Real instances are shared across miners for a given (epoch,tier,seq):
    the CNF payload does not depend on hotkey, only the wire challenge_id does
    (via per_miner.instance_id's HMAC)."""
    a_content, a_cnf = real_corpus.generate_combinatorial_instance(EPOCH, 1, 0)
    b_content, b_cnf = real_corpus.generate_combinatorial_instance(EPOCH, 1, 0)
    assert a_cnf == b_cnf
    assert a_content == b_content


@pytest.mark.parametrize("tier", [1, 2])
def test_combinatorial_instance_has_no_embedded_planted_solution(tier, monkeypatch):
    """The wired generate_instance() must return planted=None for the
    combinatorial source — there is no known solution shipped alongside it."""
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "combinatorial")
    _cid, _cnf, planted = per_miner.generate_instance(HOTKEY, EPOCH, tier, 1)
    assert planted is None


@pytest.mark.parametrize("tier", [1, 2])
def test_combinatorial_instance_solved_witness_passes_and_corruption_fails(tier):
    """A genuine solution (found by the scaffold's own tiny solver, exactly
    like an honest miner would) must pass verify_witness; corrupting one
    literal must fail it."""
    _content_id, cnf_text = real_corpus.generate_combinatorial_instance(EPOCH, tier, 2)
    assignment = solve_cnf(cnf_text)
    assert assignment is not None, "instance must be satisfiable by construction"
    assert verify_witness(cnf_text, assignment)

    corrupted = list(assignment)
    corrupted[0] = -corrupted[0]
    assert not verify_witness(cnf_text, corrupted)


def test_kind_selection_is_deterministic_and_covers_both_kinds():
    seen = {real_corpus.kind_for(EPOCH, 1, seq) for seq in range(20)}
    assert seen <= {"coloring", "latin"}
    assert len(seen) == 2  # both generators actually get exercised
    assert real_corpus.kind_for(EPOCH, 1, 3) == real_corpus.kind_for(EPOCH, 1, 3)


def test_forced_kind_env_override(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_COMBINATORIAL_KIND", "latin")
    for seq in range(5):
        assert real_corpus.kind_for(EPOCH, 1, seq) == "latin"
    monkeypatch.setenv("CATHEDRAL_V2_COMBINATORIAL_KIND", "coloring")
    for seq in range(5):
        assert real_corpus.kind_for(EPOCH, 1, seq) == "coloring"


# --------------------------------------------------------------------------
# real_corpus: on-disk corpus loading
# --------------------------------------------------------------------------

def test_corpus_dir_unset_falls_back_to_combinatorial(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_V2_CORPUS_DIR", raising=False)
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "corpus")
    assert real_corpus.load_corpus_instance(EPOCH, 1, 0) is None
    content_id, cnf_text = real_corpus.generate_real_instance(EPOCH, 1, 0)
    expected_content_id, expected_cnf = real_corpus.generate_combinatorial_instance(EPOCH, 1, 0)
    assert (content_id, cnf_text) == (expected_content_id, expected_cnf)


def test_corpus_dir_loads_real_cnf_file_deterministically(tmp_path, monkeypatch):
    cnf_a = "c a tiny real instance\np cnf 2 2\n1 2 0\n-1 -2 0\n"
    cnf_b = "c another\np cnf 3 1\n1 -2 3 0\n"
    (tmp_path / "a.cnf").write_text(cnf_a)
    (tmp_path / "b.cnf").write_text(cnf_b)
    (tmp_path / "ignore.txt").write_text("not a cnf file")

    monkeypatch.setenv("CATHEDRAL_V2_CORPUS_DIR", str(tmp_path))
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "corpus")

    loaded1 = real_corpus.load_corpus_instance(EPOCH, 1, 0)
    loaded2 = real_corpus.load_corpus_instance(EPOCH, 1, 0)
    assert loaded1 is not None
    assert loaded1 == loaded2
    _content_id, text = loaded1
    assert text in (cnf_a, cnf_b)

    n_vars, clauses = parse_cnf(text)
    assert n_vars > 0 and clauses


def test_corpus_source_wired_through_generate_instance(tmp_path, monkeypatch):
    cnf_a = "c real\np cnf 2 1\n1 2 0\n"
    (tmp_path / "only.cnf").write_text(cnf_a)
    monkeypatch.setenv("CATHEDRAL_V2_CORPUS_DIR", str(tmp_path))
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "corpus")

    cid, cnf_text, planted = per_miner.generate_instance(HOTKEY, EPOCH, 1, 0)
    assert cnf_text == cnf_a
    assert planted is None
    # challenge_id is still the standard hotkey-HMAC'd wire id.
    assert cid == per_miner.instance_id(HOTKEY, EPOCH, 1, 0)


# --------------------------------------------------------------------------
# Env flag parsing edge cases
# --------------------------------------------------------------------------

def test_challenge_source_rejects_unknown_values(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "bogus")
    assert real_corpus.challenge_source() == "planted"


def test_challenge_source_case_insensitive(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "COMBINATORIAL")
    assert real_corpus.challenge_source() == "combinatorial"
