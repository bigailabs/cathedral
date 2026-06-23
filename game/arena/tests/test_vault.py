"""Real Audit Vault — the headline synthesis of which subtensor invariants are
SETTLED on real audit CNFs: CRACKED (an exploit input exists / SAT) or HARDENED
(no exploit exists / UNSAT, two solvers agree). The vault aggregates the real
Stitch cross-proofs + the z3-minted families into one verifiable picture.
"""
from __future__ import annotations

from game.arena.engine import ArenaEngine, _real_audit_vault
from game.arena.ui import render


def test_vault_pure_builder_classifies_verdicts():
    stitch = {"available": True, "real_cnf": "A4", "host": "polarisserver",
              "remote_wall_ms": 41.0, "local_solver": "glucose3",
              "cross_solver_agree": True, "cnf_sha256": "c2ff5aa5"}
    remote_sat = {"available": True, "violable": True, "real_cnf": "I1",
                  "host": "polarisserver", "remote_wall_ms": 824.0, "n_lits": 23470,
                  "model_sha256": "870719d4", "cross_confirmed": True}
    minted = {"sat_minted": [{"target_id": "x@MINTED", "family": "B_bounds",
                              "reproduced": True, "code_sha256": "ab"}]}
    vault = _real_audit_vault(stitch, remote_sat, minted)
    by_family = {c["family"]: c for c in vault}
    # the conservation invariant holds -> HARDENED; recalc overcharge -> CRACKED
    assert by_family["A_conservation"]["verdict"] == "HARDENED"
    assert by_family["A_conservation"]["real_cnf"] is True
    assert by_family["I_safety"]["verdict"] == "CRACKED"
    assert by_family["I_safety"]["real_cnf"] is True
    # the minted family is corroborating (not a real pre-existing CNF)
    assert by_family["B_bounds"]["real_cnf"] is False
    assert all(c["cross_confirmed"] for c in vault)


def test_vault_graceful_when_no_real_receipts():
    # no stitch / remote-sat receipts -> only the minted corroborating cards (or none)
    vault = _real_audit_vault({"available": False}, {"available": False},
                              {"sat_minted": []})
    assert vault == []


def test_vault_present_in_engine_and_ui():
    r = ArenaEngine().run(1)
    assert isinstance(r.real_audit_vault, list)
    # every card declares a verdict + family + evidence
    for c in r.real_audit_vault:
        assert c["verdict"] in ("CRACKED", "HARDENED")
        assert c["family"] and c["invariant"] and c["evidence"]
    html = render(r)
    assert "Real Audit Vault" in html
    # if a real-CNF proof is present this round, the headline shows it
    if any(c.get("real_cnf") for c in r.real_audit_vault):
        assert "REAL CNF" in html
