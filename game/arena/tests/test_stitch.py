"""stitch-runner - real remote execution environment. Pure parsing/command
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
    commitment - a quote bound to a different solve (or pubkey) is rejected."""
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


def test_real_snapshot_carries_capture_provenance():
    """If a live snapshot was captured, it must carry honest provenance:
    when it was taken and that it was a read-only catalog (no files pulled)."""
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "out" / "stitch_inventory.json"
    if not p.exists():
        return
    d = json.loads(p.read_text())
    if "captured_at" not in d:                           # an older stub snapshot
        return
    assert d.get("read_only") is True                    # we only `find`-counted, no pull
    assert d["host"] and isinstance(d["dirs"], list) and d["dirs"]
    # totals are internally consistent with the per-dir rows
    assert d["total_cnf"] == sum(r.get("cnf", 0) for r in d["dirs"])
    assert d["total_py"] == sum(r.get("py", 0) for r in d["dirs"])


def test_engine_console_surfaces_real_inventory_when_present():
    from pathlib import Path
    from game.arena.engine import ArenaEngine
    p = Path(__file__).resolve().parents[1] / "out" / "stitch_inventory.json"
    inv = ArenaEngine().run(1).operator_console["stitch_inventory"]
    if not p.exists():
        return
    assert inv["available"] and inv["total_cnf"] >= 13   # the real corpus is shown


def test_inventory_status_missing(tmp_path):
    assert stitch.inventory_status(tmp_path / "none.json")["available"] is False


def test_live_inventory_if_reachable():
    """Catalog the real artifact corpus on Stitch - opt-in + reachable only."""
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
    (pull-free) - opt-in + reachable only. SAT => the I_safety invariant is
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
    """Cross-solver proof on a REAL pre-existing audit CNF - opt-in + reachable
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
    """Real remote solve - runs ONLY when explicitly enabled + reachable."""
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return                                # opt-in only; default skip (no network in CI)
    if not stitch.stitch_available():
        return
    cnf, _ = gen_planted_3sat(7, 70, 298, method="ajm")
    res = stitch.run_on_stitch(cnf, solver="kissat")
    assert res["ok"] and res["remote_measured"]
    assert verify_witness(cnf, res["assignment"])      # remote compute, local correctness gate


def test_chunk_b64_splits_command_safely():
    s = "A" * 7001
    chunks = stitch.chunk_b64(s, 3000)
    assert len(chunks) == 3 and "".join(chunks) == s
    assert all(len(c) <= 3000 for c in chunks)          # each fits the cmdline limit
    assert stitch.chunk_b64("", 3000) == []


def test_offbox_on_stitch_degrades_gracefully():
    """The off-box-on-Stitch proof returns a verdict (never crashes); when Stitch is
    down or z3 absent it reports the reason instead of raising."""
    from game.arena import mint
    r = mint.offbox_on_stitch("B2-fee-silent-zero")
    assert "available" in r
    if not r["available"]:
        assert r["reason"] in ("z3_unavailable_or_unsat", "stitch_unreachable",
                               "upload_failed", "offbox_solve_failed") or \
               r["reason"].startswith(("ssh_failed", "not_sat"))


def test_offbox_on_stitch_if_reachable():
    """Full off-box-on-Stitch: chunk-upload a minted decode-map CNF, kissat solves it
    on Stitch, decode LOCALLY (no z3), reproduce. Opt-in + reachable only."""
    import os
    if os.environ.get("CATHEDRAL_ARENA_STITCH", "").lower() not in {"1", "true", "yes", "on"}:
        return
    if not stitch.stitch_available():
        return
    from game.arena import mint
    r = mint.offbox_on_stitch("B2-fee-silent-zero")
    if not r["available"]:
        return                                          # transient stitch flake
    # the RIGOROUS proof: kissat's off-box assignment satisfies the minted CNF (the
    # violation is encoded), checked locally with no z3 - solver/model independent.
    assert r["cnf_satisfied"] is True and r["ok"] is True
    assert r["decode"] == "bit->var map (no z3)"
    assert r["decoded_input"]["amount"] > 0 and r["n_lits"] > 0
    assert r.get("round_trips") and r["round_trips"] <= 4    # leaner flow


def test_offbox_generalizes_to_multiple_minted_rules(monkeypatch):
    """Fire #61: off-box is no longer hardcoded to B2 — it proves ANY minted SAT rule
    via the rigorous cnf_satisfied check and runs that RULE'S OWN harness for the
    secondary check. Driven with real Glucose assignments for B2 AND I1 (no Stitch)."""
    from game.arena import mint, replay
    if not mint.z3_available():
        return
    try:
        from pysat.formula import CNF
        from pysat.solvers import Glucose3
    except Exception:
        return                                          # pysat absent
    for rule_id in ("B2-fee-silent-zero", "I1-div-by-zero"):
        mm = mint.mint_with_decode_map(rule_id, 8, "realistic")   # 3-arg: shares offbox's cache key
        if not mm or mm["result"] != "sat":
            continue
        f = CNF(from_string=mm["cnf_text"]); s = Glucose3(bootstrap_with=f.clauses)
        s.solve(); model = s.get_model(); s.delete()
        monkeypatch.setattr(stitch, "stitch_available", lambda *a, **k: True)
        monkeypatch.setattr(stitch, "offbox_solve", lambda *a, _m=model, **k: {
            "available": True, "host": "polarisserver", "solver": "kissat",
            "assignment": _m, "remote_wall_ms": 2.0, "n_lits": len(_m), "round_trips": 3})
        r = mint.offbox_on_stitch(rule_id)
        assert r["available"] and r["cnf_satisfied"] is True and r["ok"] is True
        assert r["rule_id"] == rule_id and r["decode"] == "bit->var map (no z3)"
        # decoded_input carries THIS rule's input keys (its own harness ran)
        spec = next(x for x in replay._MINT_RULES if x[0] == rule_id)
        for k in spec[3]:                               # the rule's decode keys
            assert k in r["decoded_input"]
        assert r["harness_reproduced"] in (True, False)  # the rule's own harness ran


def test_offbox_hardened_cross_confirms_unsat(monkeypatch):
    """Fire #68: the DEFENSIVE off-box path — a hardened invariant CNF (UNSAT) is
    confirmed UNSAT by kissat on Stitch AND by a local CDCL solver. ok = both agree.
    Driven without Stitch by mocking the remote UNSAT confirmation."""
    from game.arena import mint
    if not mint.z3_available():
        return
    # A4 conservation mints a real, non-trivial UNSAT CNF; local CDCL must confirm.
    m = mint.mint_invariant("A4-fee-split-conservation", 16, "realistic", "subtensor-amm")
    if not m or m["result"] != "unsat":
        return
    if not mint.solve_minted_cnf(m["cnf_text"]).get("available"):
        return                                          # pysat absent
    monkeypatch.setattr(stitch, "stitch_available", lambda *a, **k: True)
    monkeypatch.setattr(stitch, "offbox_confirm_unsat", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat",
        "unsat": True, "rc": 20, "remote_wall_ms": 12.0, "round_trips": 4})
    r = mint.offbox_hardened_on_stitch("A4-fee-split-conservation")
    assert r["available"] and r["verdict"] == "HARDENED"
    assert r["remote_unsat"] is True and r["local_unsat"] is True
    assert r["cross_confirmed"] is True and r["ok"] is True
    # if the remote solver does NOT confirm UNSAT, ok must be False (no false hardening)
    monkeypatch.setattr(stitch, "offbox_confirm_unsat", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat",
        "unsat": False, "rc": 0, "remote_wall_ms": 1.0, "round_trips": 4})
    r2 = mint.offbox_hardened_on_stitch("A4-fee-split-conservation")
    assert r2["remote_unsat"] is False and r2["ok"] is False


def test_offbox_hardened_degrades_gracefully():
    """When Stitch is down (or z3 absent) the hardened off-box proof returns a verdict,
    never raises, and still reports the LOCAL UNSAT it could compute."""
    from game.arena import mint
    r = mint.offbox_hardened_on_stitch("A4-fee-split-conservation")
    assert "available" in r
    if not r["available"] and mint.z3_available():
        assert r["reason"] in ("stitch_unreachable", "z3_unavailable_or_not_unsat") \
               or r["reason"].startswith(("ssh_failed", "upload_corrupt"))


def test_offbox_confirm_unsat_parses_rc20(monkeypatch):
    """offbox_confirm_unsat treats kissat rc=20 (or 's UNSATISFIABLE') as UNSAT, with
    the upload-integrity check — mocked _ssh, no Stitch."""
    import base64
    cnf = "p cnf 1 2\n1 0\n-1 0\n"                       # trivially UNSAT

    def fake_ssh(inner_b64, timeout):
        inner = base64.b64decode(inner_b64).decode()
        if inner.startswith("printf"):
            return _FakeProc("")
        return _FakeProc(f"SIZE {len(cnf)}\nELAPSED_MS 3\nRC 20\ns UNSATISFIABLE\n")

    monkeypatch.setattr(stitch, "_ssh", fake_ssh)
    res = stitch.offbox_confirm_unsat(cnf)
    assert res["available"] and res["unsat"] is True and res["rc"] == 20


def test_hardened_receipt_status_and_capture(tmp_path, monkeypatch):
    """Fire #68b: the off-box HARDENED receipt is a first-class artifact (mirrors the
    exploit receipt): reader surfaces it; capture persists ONLY on success; the engine
    console exposes the key."""
    import json
    from game.arena import mint
    assert stitch.offbox_hardened_status(tmp_path / "none.json")["available"] is False
    p = tmp_path / "hardened.json"
    p.write_text(json.dumps({"available": True, "host": "polarisserver", "solver": "kissat",
                             "rule_id": "A4-fee-split-conservation", "verdict": "HARDENED",
                             "remote_unsat": True, "local_unsat": True, "cross_confirmed": True,
                             "remote_wall_ms": 12.0, "round_trips": 4, "captured_at": "2026-06-24T00:00:00"}))
    st = stitch.offbox_hardened_status(p)
    assert st["available"] and st["cross_confirmed"] is True and st["verdict"] == "HARDENED"
    # capture persists only on success
    rp = tmp_path / "offbox_hardened_receipt.json"
    monkeypatch.setattr(mint, "offbox_hardened_on_stitch",
                        lambda *a, **k: {"available": False, "reason": "stitch_unreachable"})
    assert mint.capture_hardened_receipt(rp)["available"] is False and not rp.exists()
    monkeypatch.setattr(mint, "offbox_hardened_on_stitch", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat",
        "rule_id": "A4-fee-split-conservation", "verdict": "HARDENED",
        "remote_unsat": True, "local_unsat": True, "cross_confirmed": True, "ok": True})
    r2 = mint.capture_hardened_receipt(rp)
    assert r2["available"] and r2.get("captured_at") and rp.exists()


def test_tcp_precheck_fails_fast_on_a_closed_port():
    """The fast reachability pre-check returns False quickly for an unreachable
    endpoint (a closed local port = connection refused, instant)."""
    import time
    import game.arena.stitch as st
    saved = (st.STITCH_HOST, st.STITCH_SSH_PORT)
    try:
        st.STITCH_HOST = "user@127.0.0.1"
        st.STITCH_SSH_PORT = 1                            # almost certainly closed
        t0 = time.perf_counter()
        ok = st._tcp_reachable(timeout=2.0)
        assert ok is False and (time.perf_counter() - t0) < 2.0   # refused, well under timeout
    finally:
        st.STITCH_HOST, st.STITCH_SSH_PORT = saved


def test_stitch_available_liveness_probe_is_bounded(monkeypatch):
    """Fire #71: even when TCP is up but WSL hangs, the liveness ssh is bounded to a
    short probe_timeout (default 12s, not 30s) so it fails in ~16s not ~34s. The solve
    ops keep their own larger timeouts; this bound is only for the liveness check."""
    import game.arena.stitch as st
    st.stitch_available.cache_clear()                   # it's lru_cached — probe fresh
    monkeypatch.setattr(st, "_tcp_reachable", lambda *a, **k: True)
    seen = {}

    def fake_ssh(b64, timeout):
        seen["timeout"] = timeout
        raise __import__("subprocess").TimeoutExpired(cmd="ssh", timeout=timeout)

    monkeypatch.setattr(st, "_ssh", fake_ssh)
    try:
        assert st.stitch_available() is False           # times out -> unavailable, no raise
        assert seen["timeout"] <= 12.0                   # liveness probe is bounded (not 30)
    finally:
        st.stitch_available.cache_clear()               # don't leak a cached result to other tests


def test_stitch_available_short_circuits_when_unreachable(monkeypatch):
    """When the TCP pre-check fails, stitch_available returns False WITHOUT ever
    invoking ssh — so a down/black-hole host never blocks the ~30s ssh path."""
    import game.arena.stitch as st
    st.stitch_available.cache_clear()                   # it's lru_cached — probe fresh
    monkeypatch.setattr(st, "_tcp_reachable", lambda *a, **k: False)

    def _boom(*a, **k):
        raise AssertionError("_ssh must not be called when TCP pre-check fails")

    monkeypatch.setattr(st, "_ssh", _boom)
    try:
        assert st.stitch_available() is False
    finally:
        st.stitch_available.cache_clear()


def test_engine_console_exposes_offbox_hardened_key():
    from game.arena.engine import ArenaEngine
    assert "offbox_hardened" in ArenaEngine().run(1).operator_console


def test_engine_console_exposes_offbox_i1_key_and_reader_carries_rule_id(tmp_path):
    """Fire #76: the I1 multi-rule off-box capture is surfaced in the operator console,
    and the receipt reader carries rule_id so the UI can label it."""
    import json
    from game.arena.engine import ArenaEngine
    assert "offbox_i1" in ArenaEngine().run(1).operator_console
    p = tmp_path / "i1.json"
    p.write_text(json.dumps({"available": True, "cnf_satisfied": True, "solver": "kissat",
                             "host": "polarisserver", "rule_id": "I1-div-by-zero", "n_lits": 4032}))
    st = stitch.offbox_receipt_status(p)
    assert st["rule_id"] == "I1-div-by-zero" and st["cnf_satisfied"] is True


def test_capture_reprobes_a_stale_unreachable_cache(tmp_path, monkeypatch):
    """Fire #72 bugfix: stitch_available is lru_cached, so one flaky `False` poisons
    every off-box op for the whole process — which nearly blocked a real live capture.
    The capture helpers must clear that cache before probing, so a now-up box is reached."""
    from game.arena import mint
    import game.arena.stitch as st
    # poison the reachability cache with a stale False (box was momentarily down)
    monkeypatch.setattr(st, "_tcp_reachable", lambda *a, **k: False)
    assert st.stitch_available() is False
    assert st.stitch_available.cache_info().currsize == 1     # the stale False is cached
    seen = {}

    def fake_offbox(rule_id="B2-fee-silent-zero"):
        seen["cache_size_when_called"] = st.stitch_available.cache_info().currsize
        return {"available": True, "ok": True, "cnf_satisfied": True,
                "host": "polarisserver", "solver": "kissat"}

    monkeypatch.setattr(mint, "offbox_on_stitch", fake_offbox)
    p = tmp_path / "r.json"
    try:
        r = mint.capture_offbox_receipt(p)
        assert r["available"] and p.exists()                  # not blocked by the stale cache
        assert seen["cache_size_when_called"] == 0            # capture cleared it before probing
    finally:
        st.stitch_available.cache_clear()


def test_offbox_receipt_status_reads_and_missing(tmp_path):
    """A persisted off-box receipt is surfaced for the operator console;
    absent -> honestly unavailable."""
    import json
    assert stitch.offbox_receipt_status(tmp_path / "none.json")["available"] is False
    p = tmp_path / "offbox.json"
    p.write_text(json.dumps({"available": True, "host": "polarisserver", "solver": "kissat",
                             "cnf_satisfied": True, "harness_reproduced": False,
                             "decoded_input": {"amount": 128, "fee_rate": 512}, "n_lits": 357,
                             "remote_wall_ms": 3.0, "round_trips": 3, "decode": "bit->var map (no z3)",
                             "captured_at": "2026-06-23T21:00:00"}))
    st = stitch.offbox_receipt_status(p)
    assert st["available"] and st["cnf_satisfied"] is True and st["solver"] == "kissat"
    assert st["round_trips"] == 3 and st["decode"] == "bit->var map (no z3)"


def test_real_i1_offbox_receipt_is_coherent():
    """Fire #75: if the I1 generalized off-box exploit was captured live, it must be a
    rigorous proof for the I1 rule — cnf_satisfied, rule_id=I1, and the decoded input
    carries I1's THREE keys (amount, fee_rate, delta_in), proving off-box is not B2-only."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "out" / "offbox_i1_receipt.json"
    if not p.exists():
        return
    st = stitch.offbox_receipt_status(p)
    if not st["available"]:
        return
    assert st["cnf_satisfied"] is True and st["solver"] == "kissat"
    import json
    d = json.loads(p.read_text())
    assert d["rule_id"] == "I1-div-by-zero"
    assert set(("amount", "fee_rate", "delta_in")) <= set(d.get("decoded_input", {}))


def test_real_offbox_receipt_is_coherent():
    """If a live off-box receipt was captured (kissat solved a minted CNF on
    Stitch), it must be a rigorous proof: cnf_satisfied + ok True, no-z3 decode, and the
    leaner flow (<=4 round-trips). harness_reproduced may be False (precision-scoped)."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "out" / "offbox_stitch_receipt.json"
    if not p.exists():
        return                                          # not captured on this machine
    st = stitch.offbox_receipt_status(p)
    if not st["available"]:
        return
    assert st["cnf_satisfied"] is True                  # the rigorous, solver-independent proof
    assert st["solver"] == "kissat" and st["host"] == "polarisserver"
    assert st["decode"] == "bit->var map (no z3)"
    assert st["round_trips"] and st["round_trips"] <= 4 and st["n_lits"] > 0


def test_capture_offbox_receipt_persists_only_on_success(tmp_path, monkeypatch):
    from game.arena import mint
    p = tmp_path / "offbox_stitch_receipt.json"
    # failure (Stitch down) -> nothing written
    monkeypatch.setattr(mint, "offbox_on_stitch",
                        lambda *a, **k: {"available": False, "reason": "stitch_unreachable"})
    r = mint.capture_offbox_receipt(p)
    assert r["available"] is False and not p.exists()
    # success -> receipt persisted with a captured_at stamp
    monkeypatch.setattr(mint, "offbox_on_stitch", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat", "cnf_satisfied": True,
        "harness_reproduced": False, "n_lits": 357, "round_trips": 3, "ok": True})
    r2 = mint.capture_offbox_receipt(p)
    assert r2["available"] and r2.get("captured_at") and p.exists()
    import json
    assert json.loads(p.read_text())["cnf_satisfied"] is True


def test_engine_console_exposes_offbox_key():
    from game.arena.engine import ArenaEngine
    oc = ArenaEngine().run(1).operator_console
    assert "offbox_stitch" in oc                          # wired (available iff a receipt exists)


def test_offbox_on_stitch_ok_rests_on_cnf_satisfaction(monkeypatch):
    """Off-box ok = the assignment SATISFIES the minted CNF (rigorous, no
    z3, solver/model-independent), NOT the precision-scoped U64F64 harness replay.
    Driven with a real Glucose assignment so it's deterministic without Stitch."""
    from game.arena import mint, stitch
    if not mint.z3_available():
        return
    mm = mint.mint_with_decode_map("B2-fee-silent-zero", 8, "realistic")
    if not mm or mm["result"] != "sat":
        return
    if not mint.solve_minted_cnf(mm["cnf_text"]).get("available"):
        return                                          # pysat absent
    from pysat.formula import CNF
    from pysat.solvers import Glucose3
    f = CNF(from_string=mm["cnf_text"]); s = Glucose3(bootstrap_with=f.clauses)
    s.solve(); model = s.get_model(); s.delete()
    monkeypatch.setattr(stitch, "stitch_available", lambda *a, **k: True)
    monkeypatch.setattr(stitch, "offbox_solve", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat",
        "assignment": model, "remote_wall_ms": 3.0, "n_lits": len(model), "round_trips": 3})
    r = mint.offbox_on_stitch("B2-fee-silent-zero")
    assert r["available"] and r["cnf_satisfied"] is True and r["ok"] is True
    assert r["decode"] == "bit->var map (no z3)" and r["round_trips"] == 3
    # a corrupted assignment must NOT pass the rigorous check (no false 'solved')
    monkeypatch.setattr(stitch, "offbox_solve", lambda *a, **k: {
        "available": True, "host": "polarisserver", "solver": "kissat",
        "assignment": [abs(x) for x in model][:1] or [1], "remote_wall_ms": 1.0,
        "n_lits": 1, "round_trips": 3})
    r2 = mint.offbox_on_stitch("B2-fee-silent-zero")
    assert r2["cnf_satisfied"] is False and r2["ok"] is False


def test_upload_payload_gzip_roundtrips():
    """The chunked gzip upload payload reconstructs EXACTLY what Stitch will rebuild
    (chunks -> join -> base64-decode -> gunzip -> original CNF). Proven offline, no SSH -
    so the off-box upload is correct even though the box is flaky."""
    import base64
    import gzip
    cnf = "p cnf 3 2\n1 -2 0\n2 3 0\n" * 400      # a synthetic CNF (no z3 needed)
    b64 = base64.b64encode(gzip.compress(cnf.encode())).decode()
    chunks = stitch.chunk_b64(b64, 6000)
    reassembled = gzip.decompress(base64.b64decode("".join(chunks))).decode()
    assert reassembled == cnf                    # byte-identical round-trip
    # gzip keeps the upload to few round-trips (robust to Stitch flakiness)
    raw_chunks = stitch.chunk_b64(base64.b64encode(cnf.encode()).decode(), 6000)
    assert len(chunks) <= len(raw_chunks)        # compression never increases chunks


class _FakeProc:
    def __init__(self, stdout): self.stdout = stdout; self.returncode = 0


def test_offbox_solve_truncate_first_and_atomic_decode_solve(monkeypatch):
    """Off-box solve uploads truncate-first (no rm round-trip) and folds
    decode+verify+solve into ONE final command - so a 24KB CNF is ~3 round-trips and
    a Stitch flap can't split decode from solve. Proven offline with a mocked _ssh."""
    import base64
    import gzip
    cnf = "p cnf 3 2\n1 -2 0\n2 3 0\n" * 800          # multi-chunk synthetic CNF
    calls = []

    def fake_ssh(inner_b64, timeout):
        inner = base64.b64decode(inner_b64).decode()
        calls.append(inner)
        if inner.startswith("printf"):
            return _FakeProc("")
        return _FakeProc(f"SIZE {len(cnf)}\nELAPSED_MS 12\nRC 10\ns SATISFIABLE\nv 1 -2 3 0\n")

    monkeypatch.setattr(stitch, "_ssh", fake_ssh)
    res = stitch.offbox_solve(cnf, chunk=6000)
    assert res["available"] and res["sat"] and res["assignment"] == [1, -2, 3]
    pushes = [c for c in calls if c.startswith("printf")]
    finals = [c for c in calls if "kissat --relaxed" in c]
    assert len(finals) == 1                            # decode+solve in ONE round-trip
    assert res["round_trips"] == len(pushes) + 1       # total = chunks + 1
    assert not any(c.startswith("rm ") for c in calls) # the rm round-trip is folded away
    assert " > " in pushes[0] and all(" >> " in p for p in pushes[1:])  # truncate-first
    # the pushed payload reconstructs the gzip'd CNF byte-for-byte
    payload = "".join(p.split("printf %s ")[1].split(" >")[0] for p in pushes)
    assert gzip.decompress(base64.b64decode(payload)).decode() == cnf


def test_offbox_solve_detects_upload_corruption(monkeypatch):
    """The folded final command reports the decompressed SIZE; a mismatch with the
    local CNF length is caught as upload_corrupt (no false 'solved')."""
    import base64
    cnf = "p cnf 1 1\n1 0\n"

    def fake_ssh(inner_b64, timeout):
        inner = base64.b64decode(inner_b64).decode()
        if inner.startswith("printf"):
            return _FakeProc("")
        return _FakeProc(f"SIZE {len(cnf) + 99}\nRC 10\ns SATISFIABLE\nv 1 0\n")  # wrong size

    monkeypatch.setattr(stitch, "_ssh", fake_ssh)
    res = stitch.offbox_solve(cnf)
    assert res["available"] is False and res["reason"].startswith("upload_corrupt")
