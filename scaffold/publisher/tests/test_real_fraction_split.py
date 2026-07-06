"""Option A: a deterministic fraction of per-miner challenges are REAL puzzles."""
from __future__ import annotations

import time

import pytest

from scaffold import dimacs
from scaffold.publisher import per_miner as pm


HOTKEY = "5RealFractionTestHotkeyAAAAAAAAAAAAAAAAAAAAAAAA"
EPOCH = 495400


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_V2_REAL_FRACTION", raising=False)
    monkeypatch.delenv("CATHEDRAL_V2_CHALLENGE_SOURCE", raising=False)
    yield


def _is_planted(planted):
    return planted is not None


def test_fraction_zero_is_all_planted(monkeypatch):
    # default (unset) — every challenge is planted, unchanged behavior
    for seq in range(60):
        _cid, _cnf, planted = pm.generate_instance(HOTKEY, EPOCH, 1, seq)
        assert _is_planted(planted), f"seq {seq} unexpectedly real at fraction 0"


def test_fraction_ten_percent_is_roughly_ten_percent(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", "0.1")
    n = 400
    real = sum(
        1 for seq in range(n)
        if pm.generate_instance(HOTKEY, EPOCH, 1, seq)[2] is None
    )
    frac = real / n
    assert 0.06 <= frac <= 0.15, f"real fraction {frac:.3f} outside tolerance"


def test_decision_is_stable_and_deterministic(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", "0.1")
    for seq in range(40):
        a = pm.generate_instance(HOTKEY, EPOCH, 1, seq)
        b = pm.generate_instance(HOTKEY, EPOCH, 1, seq)
        assert (a[2] is None) == (b[2] is None)  # same real/planted decision
        assert a[1] == b[1]                       # identical CNF


def test_real_instances_are_per_miner_unique(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", "1.0")  # force all real
    other = "5DifferentMinerBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    differing = 0
    for seq in range(20):
        _c1, cnf_a, pa = pm.generate_instance(HOTKEY, EPOCH, 1, seq)
        _c2, cnf_b, pb = pm.generate_instance(other, EPOCH, 1, seq)
        assert pa is None and pb is None
        if cnf_a != cnf_b:
            differing += 1
    # the two miners must get different real puzzles on essentially every seq
    assert differing >= 18, f"only {differing}/20 real puzzles differed between miners"


def test_uses_real_instance_parity_with_generation(monkeypatch):
    """uses_real_instance() must EXACTLY equal (generate_instance()[2] is None)
    for all inputs: the challenges page derives the kind label from the
    predicate on v2_cnf_store cache hits, so the label and the CNF body must
    never disagree."""
    for raw in (None, "0.0", "0.1", "0.5", "1.0"):
        if raw is None:
            monkeypatch.delenv("CATHEDRAL_V2_REAL_FRACTION", raising=False)
        else:
            monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", raw)
        for tier in (1, 2):
            for seq in range(12):
                _cid, _cnf, planted = pm.generate_instance(HOTKEY, EPOCH, tier, seq)
                assert pm.uses_real_instance(HOTKEY, EPOCH, tier, seq) == (planted is None), (
                    f"predicate/generation disagree: fraction={raw} tier={tier} seq={seq}"
                )


def test_uses_real_instance_parity_with_legacy_source_env(monkeypatch):
    # Unset fraction + non-planted source is the legacy all-real path (1.0).
    monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", "combinatorial")
    for seq in range(6):
        _cid, _cnf, planted = pm.generate_instance(HOTKEY, EPOCH, 1, seq)
        assert planted is None
        assert pm.uses_real_instance(HOTKEY, EPOCH, 1, seq) is True


def test_real_instances_are_satisfiable_solvable_and_verifiable(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_REAL_FRACTION", "1.0")
    worst = 0.0
    for tier in (1, 2):
        for seq in range(12):
            _cid, cnf, planted = pm.generate_instance(HOTKEY, EPOCH, tier, seq)
            assert planted is None
            t0 = time.time()
            sol = dimacs.solve_cnf(cnf)
            dt = time.time() - t0
            worst = max(worst, dt)
            assert sol is not None, f"real tier{tier} seq{seq} was UNSAT — must be solvable"
            assert dimacs.verify_witness(cnf, sol), "witness must verify"
    # weak DPLL must clear them quickly; real miners (cadical/kissat) far faster
    assert worst < 2.0, f"slowest weak-solver solve was {worst:.2f}s — too hard"
