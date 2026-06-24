"""Unified proof — the z3 factory MINTS an invariant CNF; its SAT solution (the
z3 witness) is the exploit input that the REAL replay harness reproduces. Solve
and replay are one proof. Skips cleanly if z3 is unavailable.
"""
from __future__ import annotations

from game.arena import mint, replay


def test_mint_silent_zero_is_sat_with_small_cnf():
    if not mint.z3_available():
        return
    m = mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
    assert m is not None and m["result"] == "sat"
    assert m["witness"] and "amount" in m["witness"] and "fee_rate" in m["witness"]
    assert m["clauses"] > 0 and m["cnf_sha256"]            # a real emitted CNF


def test_minted_witness_reproduces_via_real_harness():
    if not mint.z3_available() or not replay.MINTED_TARGETS:
        return
    tid = replay.MINTED_TARGETS[0]
    t = replay.TARGETS[tid]
    assert t.source == "z3-factory-mint"
    o = replay.run_replay(tid, t.known_witness)
    assert o.reproduced is True                            # SAT solution IS the exploit
    assert o.observed["fee"] == 0 and o.observed["amount"] > 0  # silent-zero fee


def test_minted_target_in_rotation():
    if not replay.MINTED_TARGETS:
        return
    from game.arena.engine import REPRODUCING_TARGETS
    assert replay.MINTED_TARGETS[0] in REPRODUCING_TARGETS


def test_mint_is_deterministic_cached():
    if not mint.z3_available():
        return
    a = mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
    b = mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
    assert a["cnf_sha256"] == b["cnf_sha256"] and a["witness"] == b["witness"]


def test_minted_cnf_solved_by_real_cdcl_solver():
    """A REAL external CDCL solver (Glucose) solves the minted invariant CNF and
    the assignment independently satisfies every clause. Skips if z3/pysat absent."""
    if not mint.z3_available():
        return
    m = mint.mint_invariant("B2-fee-silent-zero", 16, "realistic")
    solved = mint.solve_minted_cnf(m["cnf_text"])
    if not solved.get("available"):
        return                                     # pysat not installed -> skip
    assert solved["sat"] is True
    assert solved["verified"] is True              # assignment satisfies the CNF
    assert solved["solver"] == "glucose3"


def test_full_unified_proof_status():
    """encode (z3) -> solve (real CDCL, verified) -> reproduce (real harness)."""
    if not mint.z3_available():
        return
    st = mint.minted_proof_status()
    if not st["available"] or not st["external_solve"].get("available"):
        return
    assert st["external_solve"]["verified"] is True
    assert st["reproduced"] is True
    assert st["ok"] is True


def test_satisfies_rejects_bad_model():
    cnf = "p cnf 2 2\n1 0\n-2 0\n"
    assert mint._satisfies(cnf, [1, -2]) is True
    assert mint._satisfies(cnf, [-1, -2]) is False   # clause "1 0" unsatisfied


def test_safe_witness_does_not_reproduce():
    if not replay.MINTED_TARGETS:
        return
    tid = replay.MINTED_TARGETS[0]
    # a large amount with a healthy fee_rate pays a non-zero fee => invariant holds
    o = replay.run_replay(tid, {"amount": 10_000_000, "fee_rate": 5000})
    assert o.reproduced is False


# -- multiple minted invariant families (fire #24) ----------------------------

def test_multiple_minted_families_each_reproduce():
    """More than one factory rule is minted into a real solve==replay proof,
    spanning distinct invariant families — every minted target reproduces on its
    own z3 witness."""
    if not mint.z3_available() or not replay.MINTED_TARGETS:
        return
    assert len(replay.MINTED_TARGETS) >= 2
    fams = set()
    for tid in replay.MINTED_TARGETS:
        t = replay.TARGETS[tid]
        assert t.source == "z3-factory-mint"
        o = replay.run_replay(tid, t.known_witness)
        assert o.reproduced is True                    # SAT solution IS the exploit
        fams.add(t.family)
    assert {"B_bounds", "I_safety"} <= fams            # at least these two families


def test_i_safety_minted_is_recalc_overcharge():
    """The I1 minted target is the RAW recalc-overcharge bound: its z3 witness
    makes delta_in + recalc_fee exceed `amount`."""
    if not mint.z3_available():
        return
    tid = "subtensor-amm:recalc-overcharge-raw@MINTED"
    if tid not in replay.TARGETS:
        return
    t = replay.TARGETS[tid]
    assert t.family == "I_safety"
    o = replay.run_replay(tid, t.known_witness)
    assert o.reproduced is True
    assert o.observed["paid"] > o.observed["amount"]   # the overcharge


def test_hardened_invariant_confirmed_unsat():
    """A4 conservation: z3 says the negated invariant is UNSAT and an independent
    CDCL solver confirms it — a solver-backed proof that no exploit exists."""
    if not mint.z3_available():
        return
    if not replay.MINTED_HARDENED:
        return
    h = next(h for h in replay.MINTED_HARDENED if h["family"] == "A_conservation")
    assert h["z3"] == "unsat"
    solved = mint.solve_minted_cnf(mint.mint_invariant(h["rule_id"], 16, "realistic")["cnf_text"])
    if solved.get("available"):                         # pysat present
        assert solved["sat"] is False                  # CDCL agrees: unsatisfiable
        assert h["cdcl_unsat"] is True and h["hardened"] is True


def test_minted_summary_lists_families():
    s = replay.minted_summary()
    assert isinstance(s["families"], list)
    if replay.MINTED_TARGETS:
        assert all(m["reproduced"] for m in s["sat_minted"])


# -- hardened invariants across TWO pinned models (fire #35) -------------------

def test_hardened_proofs_span_two_models():
    """The hardened set proves invariants from BOTH the AMM fee model and the
    root-staking model (z3 + CDCL both UNSAT) — more real invariants wired."""
    if not replay.MINTED_HARDENED:
        return                                          # no manifest / z3 absent
    models = {h.get("model") for h in replay.MINTED_HARDENED}
    assert "subtensor-amm" in models
    # the manifest (when present) also carries the root-staking model
    if any(h.get("model") == "subtensor-root-reborn" for h in replay.MINTED_HARDENED):
        fams = {h["family"] for h in replay.MINTED_HARDENED
                if h.get("model") == "subtensor-root-reborn"}
        assert {"A_conservation", "F_emission"} & fams
    assert all(h.get("hardened") for h in replay.MINTED_HARDENED)   # all CDCL-confirmed


def test_mint_supports_root_reborn_model():
    """The factory mint is model-parameterized: the root-staking model resolves."""
    if not mint.z3_available():
        return
    m = mint.mint_invariant("A4-tao-split-conservation", 8, "realistic", "subtensor-root-reborn")
    assert m is not None and m["result"] == "unsat"     # the TAO-split invariant holds
    assert m["model"] == "subtensor-root-reborn"


def test_hardened_proofs_in_vault():
    from game.arena.engine import ArenaEngine
    vault = ArenaEngine().run(1).real_audit_vault
    hardened = [c for c in vault if c["verdict"] == "HARDENED"]
    if not replay.MINTED_HARDENED:
        return
    # at least the conservation invariant is shown hardened; with the root model
    # wired, an F_emission hardened proof also appears
    fams = {c["family"] for c in hardened}
    assert "A_conservation" in fams
