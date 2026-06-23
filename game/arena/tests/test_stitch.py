"""stitch-runner — real remote execution environment. Pure parsing/command
tests always run; the live solve runs only if Stitch is reachable (else skip).
"""
from __future__ import annotations

import base64
import os

from game.arena import stitch
from scaffold.dimacs import gen_planted_3sat, verify_witness


def test_parse_solver_output_sat():
    out = "ELAPSED_MS 17\ns SATISFIABLE\nv 1 -2 3 -4 0\n"
    lits, wall, status = stitch.parse_solver_output(out)
    assert lits == [1, -2, 3, -4]
    assert wall == 17.0 and status == "SATISFIABLE"


def test_parse_solver_output_multiline_v():
    out = "ELAPSED_MS 5\ns SATISFIABLE\nv 1 -2\nv 3 -4 0\n"
    lits, _w, _s = stitch.parse_solver_output(out)
    assert lits == [1, -2, 3, -4]            # DIMACS v-lines can span multiple lines


def test_parse_solver_output_no_model():
    lits, wall, status = stitch.parse_solver_output("ELAPSED_MS 3\ns UNKNOWN\n")
    assert lits == [] and status == "UNKNOWN"


def test_remote_script_is_deterministic_and_safe():
    cnf_b64 = base64.b64encode(b"p cnf 1 1\n1 0\n").decode()
    s1 = stitch.remote_script(cnf_b64, "kissat")
    s2 = stitch.remote_script(cnf_b64, "kissat")
    assert s1 == s2
    assert "kissat -q" in s1 and "date +%s%N" in s1   # host-measured timing
    assert "mktemp" in s1                              # per-run temp file


def test_stitch_status_reads_receipt(tmp_path):
    import json
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"host": "polarisserver", "solver": "kissat",
                             "remote_wall_ms": 19.0, "witness_verified_locally": True,
                             "n_vars": 80}))
    st = stitch.stitch_status(p)
    assert st["available"] and st["ok"] and st["host"] == "polarisserver"


def test_stitch_status_missing(tmp_path):
    st = stitch.stitch_status(tmp_path / "nope.json")
    assert st["available"] is False


def test_stitch_status_real_cnf_hardened_crossproof(tmp_path):
    """A REAL pre-existing audit CNF cross-proof receipt (kissat on Stitch vs a
    local CDCL solver, both UNSAT) is surfaced as a hardened proof."""
    import json
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "real_cnf": "subtensor-amm__A4-fee-split-conservation__16f16__realistic",
        "host": "polarisserver", "solver": "kissat", "solver_version": "4.0.4",
        "remote_status": "UNSAT", "remote_wall_ms": 39.0, "local_solver": "glucose3",
        "local_status": "UNSAT", "cross_solver_agree": True, "hardened_proof": True,
        "witness_verified_locally": None, "cnf_sha256": "c2ff5aa5"}))
    st = stitch.stitch_status(p)
    assert st["available"] and st["ok"] is True        # ok via cross-solver agreement
    assert st["hardened_proof"] is True and st["cross_solver_agree"] is True
    assert st["real_cnf"].startswith("subtensor-amm__A4")
    assert st["remote_status"] == "UNSAT" and st["local_solver"] == "glucose3"


def test_stitch_status_surfaces_solver_race(tmp_path):
    """A REAL solver race on a real audit CNF (kissat on Stitch vs a local CDCL
    solver on the same pinned formula) is surfaced for the solver-bench card."""
    import json
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "real_cnf": "subtensor-amm__A4-fee-split-conservation__16f16__realistic",
        "host": "polarisserver", "solver": "kissat", "remote_status": "UNSAT",
        "remote_wall_ms": 41.0, "local_solver": "glucose3", "local_solve_ms": 67.58,
        "local_status": "UNSAT", "cross_solver_agree": True, "hardened_proof": True,
        "solver_race": {"remote": {"solver": "kissat", "host": "polarisserver", "ms": 41.0},
                        "local": {"solver": "glucose3", "host": "local", "ms": 67.58},
                        "winner": "kissat"}}))
    st = stitch.stitch_status(p)
    assert st["local_solve_ms"] == 67.58
    race = st["solver_race"]
    assert race["winner"] == "kissat"                 # kissat won this real race
    assert race["remote"]["ms"] < race["local"]["ms"]


def test_stitch_status_real_cnf_sat_witness(tmp_path):
    """A SAT real-CNF receipt is ok when the witness verified locally."""
    import json
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "real_cnf": "subtensor-amm__I1-div-by-zero__16f16__realistic", "host": "polarisserver",
        "solver": "kissat", "remote_status": "SAT", "remote_wall_ms": 120.0,
        "cross_solver_agree": False, "hardened_proof": False,
        "witness_verified_locally": True}))
    st = stitch.stitch_status(p)
    assert st["ok"] is True and st["hardened_proof"] is False


def _RECEIPT():
    return {"real_cnf": "subtensor-amm__A4-fee-split-conservation__16f16__realistic",
            "cnf_sha256": "c2ff5aa5", "host": "polarisserver", "solver": "kissat",
            "remote_status": "UNSAT", "remote_wall_ms": 39.0, "local_solver": "glucose3",
            "cross_solver_agree": True, "hardened_proof": True}


def test_solve_commitment_is_deterministic_and_field_sensitive():
    c1 = stitch.solve_commitment(_RECEIPT())
    c2 = stitch.solve_commitment(_RECEIPT())
    assert c1 == c2 and len(c1) == 64
    tampered = _RECEIPT(); tampered["remote_status"] = "SAT"   # a different solve outcome
    assert stitch.solve_commitment(tampered) != c1            # commitment binds the result


def test_attest_readiness_reports_commitment_without_spend(tmp_path):
    import json
    p = tmp_path / "r.json"; p.write_text(json.dumps(_RECEIPT()))
    rd = stitch.attest_readiness(p)
    assert rd["available"] and rd["live_quote"] is False and rd["attested"] is False
    assert rd["commitment"] == stitch.solve_commitment(_RECEIPT())   # exposed for the quote


def test_quote_must_bind_the_specific_solve(tmp_path, monkeypatch):
    """A TDX quote attests THIS solve only if its report_data binds the solve's
    commitment — a quote bound to a different solve (or pubkey) is rejected."""
    import base64
    import hashlib
    import json
    monkeypatch.setenv("CATHEDRAL_ATTEST_ALLOW_STUB", "1")
    monkeypatch.delenv("CATHEDRAL_ATTEST_DCAP_VERIFY_CMD", raising=False)
    monkeypatch.delenv("CATHEDRAL_DCAP_VERIFY_CMD", raising=False)
    commitment = stitch.solve_commitment(_RECEIPT())
    pub = base64.b64encode(b"e2e-pub".ljust(32, b"0")).decode()

    def stub(nonce):
        lo = hashlib.sha256((nonce + pub).encode()).digest()
        return base64.b64encode(b"\x00" * 568 + lo + b"\x00" * 32).decode()

    # quote bound to the real commitment -> the solve is attested
    p = tmp_path / "r.json"; p.write_text(json.dumps(_RECEIPT()))
    qp = tmp_path / "q.json"
    qp.write_text(json.dumps({"quote_b64": stub(commitment), "e2e_pubkey_b64": pub}))
    rd = stitch.attest_readiness(p, quote_path=qp)
    assert rd["live_quote"] is True and rd["attested"] is True

    # quote bound to a DIFFERENT solve -> rejected (does not attest this solve)
    qp.write_text(json.dumps({"quote_b64": stub("0" * 64), "e2e_pubkey_b64": pub}))
    rd2 = stitch.attest_readiness(p, quote_path=qp)
    assert rd2["attested"] is False


def test_parse_inventory_counts_and_missing():
    out = ("DIR /home/frede/audit-cnf cnf=13 map=1 py=0\n"
           "DIR /home/frede/subnet-2 cnf=0 map=27 py=2\n"
           "DIR /home/frede/gone MISSING\n")
    rows = stitch.parse_inventory(out)
    assert rows[0] == {"dir": "/home/frede/audit-cnf", "present": True, "cnf": 13, "map": 1, "py": 0}
    assert rows[1]["map"] == 27
    assert rows[2] == {"dir": "/home/frede/gone", "present": False}


def test_inventory_status_reads_real_snapshot():
    """The stored snapshot of the REAL artifact corpus on Stitch (CNFs + decode
    maps + harnesses) is surfaced for the operator console."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "out" / "stitch_inventory.json"
    if not p.exists():
        return                                          # snapshot not generated here
    st = stitch.inventory_status(p)
    assert st["available"] and st["total_cnf"] >= 13     # at least the audit-cnf CNFs
    assert st["total_map"] >= 1 and st["n_dirs"] >= 1
    assert all(d["present"] for d in st["dirs"])


def test_inventory_status_missing(tmp_path):
    assert stitch.inventory_status(tmp_path / "none.json")["available"] is False


def test_live_inventory_if_reachable():
    """Catalog the real artifact corpus on Stitch — opt-in + reachable only."""
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return
    if not stitch.stitch_available():
        return
    inv = stitch.inventory()
    assert inv["available"] and inv["total_cnf"] >= 1
    assert any(d.get("dir", "").endswith("audit-cnf") for d in inv["dirs"])


def test_parse_remote_solve_sat_and_unsat():
    sat = stitch.parse_remote_solve(
        "ELAPSED_MS 824\nRC 10\ns SATISFIABLE\nNLITS 23470\nMSHA 870719d46aa31daa\n")
    assert sat["sat"] is True and sat["unsat"] is False and sat["status"] == "SAT"
    assert sat["remote_wall_ms"] == 824.0 and sat["n_lits"] == 23470
    assert sat["model_sha256"] == "870719d46aa31daa"
    un = stitch.parse_remote_solve("ELAPSED_MS 39\nRC 20\ns UNSATISFIABLE\nNLITS 0\nMSHA \n")
    assert un["unsat"] is True and un["sat"] is False and un["status"] == "UNSAT"


def test_remote_sat_status_cross_confirmed(tmp_path):
    """A pull-free remote-SAT receipt: the real audit CNF is VIOLABLE on Stitch and
    the z3-minted twin reproduces the same violation locally (cross-confirmed)."""
    import json
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "real_cnf": "subtensor-amm__I1-div-by-zero__16f16__realistic",
        "invariant": "recalc denominator collapse (I_safety)", "host": "polarisserver",
        "solver": "kissat", "status": "SAT", "remote_wall_ms": 824.0, "n_lits": 23470,
        "model_sha256": "870719d46aa3", "violable": True,
        "twin_reproduces_locally": True, "cross_confirmed": True}))
    st = stitch.remote_sat_status(p)
    assert st["available"] and st["violable"] is True
    assert st["cross_confirmed"] is True and st["twin_reproduces_locally"] is True
    assert st["status"] == "SAT" and st["n_lits"] == 23470


def test_remote_sat_status_missing(tmp_path):
    assert stitch.remote_sat_status(tmp_path / "none.json")["available"] is False


def test_solve_remote_cnf_if_reachable():
    """Solve the REAL pre-existing 20MB recalc-overcharge audit CNF on Stitch
    (pull-free) — opt-in + reachable only. SAT => the I_safety invariant is
    violable on the real artifact; the z3-minted twin reproduces it locally."""
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return
    if not stitch.stitch_available():
        return
    res = stitch.solve_remote_cnf(
        "/home/frede/audit-cnf/factory/subtensor-amm__I1-div-by-zero__16f16__realistic.cnf")
    assert res["available"] and res["sat"] is True and res["status"] == "SAT"
    assert res["n_lits"] > 0 and res["remote_wall_ms"] is not None
    from game.arena import replay
    tw = "subtensor-amm:recalc-overcharge-raw@MINTED"
    if tw in replay.TARGETS:                            # z3-minted twin reproduces it locally
        assert replay.run_replay(tw, replay.TARGETS[tw].known_witness).reproduced is True


def test_prove_real_cnf_on_stitch_if_reachable():
    """Cross-solver proof on a REAL pre-existing audit CNF — opt-in + reachable
    only. kissat on Stitch (host-measured) vs a local CDCL solver on the same
    pinned CNF; the AMM conservation invariant is proven hardened (both UNSAT)."""
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return                                          # opt-in only (no network in CI)
    if not stitch.stitch_available():
        return
    rec = stitch.prove_real_cnf_on_stitch(
        "/home/frede/audit-cnf/factory/subtensor-amm__A4-fee-split-conservation__16f16__realistic.cnf",
        invariant="fee+delta==amount & fee<=amount (AMM conservation)",
        name="subtensor-amm__A4-fee-split-conservation__16f16__realistic")
    assert rec["available"]
    assert rec["remote_status"] == "UNSAT" and rec["local_status"] == "UNSAT"
    assert rec["cross_solver_agree"] is True and rec["hardened_proof"] is True


def test_live_stitch_solve_if_reachable():
    """Real remote solve — runs ONLY when explicitly enabled + reachable."""
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return                                # opt-in only; default skip (no network in CI)
    if not stitch.stitch_available():
        return
    cnf, _ = gen_planted_3sat(7, 70, 298, method="ajm")
    res = stitch.run_on_stitch(cnf, solver="kissat")
    assert res["ok"] and res["remote_measured"]
    assert verify_witness(cnf, res["assignment"])      # remote compute, local correctness gate
