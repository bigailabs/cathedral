"""Off-box decode — the self-critique gap #3 NEXT: an EXTERNAL solver solves the
minted invariant CNF and its raw DIMACS assignment is decoded back to the exploit
input WITHOUT re-running z3 (via a real bit->var map z3 emits at mint time). A
miner can solve off-box; the arena decodes + verifies against the real harness.
Skips cleanly if z3/pysat are unavailable.
"""
from __future__ import annotations

from game.arena import mint, replay


def test_decode_assignment_is_pure_bit_math():
    # bit->var map: amount bit0->var3, bit1->var5; fee_rate bit0->var7
    dmap = {"amount": {0: 3, 1: 5}, "fee_rate": {0: 7}}
    # assignment: var3 true, var5 true, var7 false  => amount=0b11=3, fee_rate=0
    assert mint.decode_assignment([3, 5, -7], dmap) == {"amount": 3, "fee_rate": 0}
    # all false -> zero
    assert mint.decode_assignment([-3, -5, -7], dmap) == {"amount": 0, "fee_rate": 0}


def test_mint_emits_a_real_decode_map():
    if not mint.z3_available():
        return
    mm = mint.mint_with_decode_map("B2-fee-silent-zero", 8, "realistic")
    assert mm is not None and mm["result"] == "sat"
    # the map names each input's bits -> a DIMACS variable index
    assert "amount" in mm["decode_map"] and "fee_rate" in mm["decode_map"]
    assert all(isinstance(v, int) for v in mm["decode_map"]["amount"].values())
    assert "c " in mm["cnf_text"] and "@" in mm["cnf_text"]   # the named-atom comments
    assert mm["vars"] > 0 and mm["clauses"] > 0


def test_external_solver_assignment_decodes_without_z3():
    """The whole point: solve the minted CNF with Glucose (NOT z3), decode its
    assignment via the map, and the decoded input reproduces the real violation."""
    if not mint.z3_available():
        return
    mm = mint.mint_with_decode_map("B2-fee-silent-zero", 8, "realistic")
    solved = mint.solve_minted_cnf(mm["cnf_text"])
    if not solved.get("available"):
        return                                          # pysat absent -> skip
    from pysat.formula import CNF
    from pysat.solvers import Glucose3
    f = CNF(from_string=mm["cnf_text"]); s = Glucose3(bootstrap_with=f.clauses)
    assert s.solve()
    model = s.get_model(); s.delete()
    decoded = mint.decode_assignment(model, mm["decode_map"])
    # the assignment satisfies the CNF (independent check)
    assert mint._satisfies(mm["cnf_text"], model)
    # the decoded input reproduces the silent-zero violation via the REAL harness
    inp = {"amount": decoded["amount"], "fee_rate": decoded["fee_rate"]}
    obs = replay._silent_zero_harness(inp)
    assert replay._silent_zero_inv(obs) is False        # invariant violated => exploit
    assert obs["fee"] == 0 and obs["amount"] > 0         # a real silent-zero fee


def test_external_decode_status_chain_ok():
    if not mint.z3_available():
        return
    st = mint.external_decode_status()
    if not st["available"]:
        return                                          # pysat absent
    assert st["ok"] is True and st["reproduced"] is True
    assert st["decode"] == "bit->var map (no z3)"
    assert st["decoded_input"]["amount"] > 0 and st["decoded_input"]["fee_rate"] > 0


def test_status_surfaced_in_engine_console():
    from game.arena.engine import ArenaEngine
    oc = ArenaEngine().run(1).operator_console
    assert "external_decode" in oc
