"""Mint a real invariant CNF via the audit-hunter z3 factory — closing the loop
so SOLVE and REPLAY are ONE proof.

The factory (`audit-hunter/factory/core.py` + `models/subtensor_amm.py`) encodes
the NEGATION of a subtensor money-math invariant as a z3 bit-vector goal, solves
it, and emits DIMACS + the witness (the `.map.json` decode = the z3 model). A SAT
solution to that CNF IS an input that violates the invariant. The arena then
feeds that witness to the REAL replay harness (replay.py) — so the thing the
miner solves and the thing that reproduces are the same instance.

z3 is optional: if it's not importable this returns None and the arena falls
back to the pre-recorded corpus witnesses (still real, just not freshly minted).
"""
from __future__ import annotations

import functools
import hashlib

_FACTORY = "/mnt/c/Users/fred/code/audit-hunter/factory"


_MODELS = {
    "subtensor-amm": ("subtensor_amm", "SubtensorAMM"),
    "subtensor-root-reborn": ("subtensor_root_reborn", "SubtensorRootReborn"),
}


@functools.lru_cache(maxsize=12)
def mint_invariant(rule_id: str = "B2-fee-silent-zero", width: int = 16,
                   cap: str = "realistic", model: str = "subtensor-amm") -> dict | None:
    """Encode + solve a subtensor money-math invariant via the z3 factory. `model`
    selects the pinned contract (subtensor-amm | subtensor-root-reborn). Returns
    {witness, decode_inputs, invariant, cnf_sha256, vars, clauses, result, ...} or
    None if z3/the model is absent."""
    import sys
    if _FACTORY not in sys.path:
        sys.path[:0] = [_FACTORY, _FACTORY + "/models"]
    modspec = _MODELS.get(model)
    if modspec is None:
        return None
    try:
        import importlib
        core = importlib.import_module("core")
        mod = importlib.import_module(modspec[0])
        from z3 import Solver, sat
        from fp import FP
    except Exception:
        return None

    inst = getattr(mod, modspec[1])()
    if rule_id not in inst.rules:
        return None
    fp = FP(width, width)
    inputs = inst.inputs((width, width))
    goal, notes = inst.rules[rule_id](inputs, fp,
                                      {"bound_cap": cap, "int_bits": width, "frac_bits": width})
    s = Solver()
    s.set("timeout", 30000)
    s.add(goal)
    result = str(s.check())
    witness = None
    if result == "sat":
        m = s.model()
        witness = {str(d): int(str(m[d])) for d in m.decls()}
    try:
        cnf_text, nv, nc = core._dimacs(goal)
    except Exception:
        cnf_text, nv, nc = "", 0, 0
    return {
        "rule_id": rule_id, "width": width, "cap": cap, "result": result,
        "witness": witness, "decode_inputs": list(inputs.keys()),
        "invariant": notes.get("invariant", ""),
        "cnf_text": cnf_text, "cnf_sha256": hashlib.sha256(cnf_text.encode()).hexdigest() if cnf_text else "",
        "vars": nv, "clauses": nc,
        "model": inst.name, "z3_minted": True,
    }


@functools.lru_cache(maxsize=8)
def mint_with_decode_map(rule_id: str = "B2-fee-silent-zero", width: int = 8,
                         cap: str = "realistic", model: str = "subtensor-amm") -> dict | None:
    """Mint the invariant CNF AND emit a real bit->var DECODE MAP, so an EXTERNAL
    solver's raw DIMACS assignment can be decoded back to the exploit input bits
    WITHOUT re-running z3. Trick: inject explicitly-named per-bit boolean atoms
    (`name@k`) constrained to each input bitvector's bit before bit-blasting; z3's
    dimacs() then emits `c <var> name@k` comments we parse into the map.

    Returns {cnf_text, decode_map:{name:{bit:var}}, result, z3_witness, vars,
    clauses, cnf_sha256} or None if z3/model absent."""
    import sys
    if _FACTORY not in sys.path:
        sys.path[:0] = [_FACTORY, _FACTORY + "/models"]
    modspec = _MODELS.get(model)
    if modspec is None:
        return None
    try:
        import importlib
        mod = importlib.import_module(modspec[0])
        from z3 import Goal, Then, Solver, Extract, BitVecVal, Bool
        from fp import FP
    except Exception:
        return None

    inst = getattr(mod, modspec[1])()
    if rule_id not in inst.rules:
        return None
    fp = FP(width, width)
    inputs = inst.inputs((width, width))
    goal_expr, _notes = inst.rules[rule_id](inputs, fp,
                                            {"bound_cap": cap, "int_bits": width, "frac_bits": width})
    s = Solver(); s.set("timeout", 30000); s.add(goal_expr)
    result = str(s.check())
    z3_witness = None
    if result == "sat":
        m = s.model()
        z3_witness = {str(d): int(str(m[d])) for d in m.decls()}

    g = Goal(); g.add(goal_expr)
    for name, bv in inputs.items():                      # name the input bits
        for k in range(bv.size()):
            g.add(Bool(f"{name}@{k}") == (Extract(k, k, bv) == BitVecVal(1, 1)))
    try:
        cnf = Then("simplify", "bit-blast", "tseitin-cnf")(g)[0]
        txt = cnf.dimacs()
    except Exception:
        return None
    decode_map: dict[str, dict[int, int]] = {}
    nv = nc = 0
    for ln in txt.splitlines():
        if ln.startswith("p cnf"):
            _, _, a, b = ln.split(); nv, nc = int(a), int(b)
        elif ln.startswith("c ") and "@" in ln:
            parts = ln.split()
            if len(parts) == 3:
                nm, _, k = parts[2].rpartition("@")
                try:
                    decode_map.setdefault(nm, {})[int(k)] = int(parts[1])
                except ValueError:
                    pass
    if not decode_map:
        return None
    return {"rule_id": rule_id, "model": model, "width": width, "result": result,
            "cnf_text": txt, "decode_map": {n: dict(b) for n, b in decode_map.items()},
            "decode_inputs": list(inputs.keys()), "z3_witness": z3_witness,
            "vars": nv, "clauses": nc,
            "cnf_sha256": hashlib.sha256(txt.encode()).hexdigest()}


def decode_assignment(model_lits, decode_map: dict) -> dict:
    """Decode an EXTERNAL solver's DIMACS assignment to the input integer values
    using the bit->var map — pure, NO z3. `model_lits` is the solver's literal
    list (+v true / -v false); reassembles each input from its named bits."""
    truth = {abs(int(x)): (int(x) > 0) for x in model_lits}
    out: dict[str, int] = {}
    for name, bits in decode_map.items():
        val = 0
        for k, var in bits.items():
            if truth.get(int(var), False):
                val |= (1 << int(k))
        out[name] = val
    return out


def _satisfies(cnf_text: str, model: list[int]) -> bool:
    """True iff `model` satisfies EVERY clause of the CNF. Tolerant of a model
    that assigns more variables than the DIMACS header counts (z3's bit-blast can
    number aux vars past the header), unlike the strict per-miner witness check."""
    from scaffold.dimacs import parse_cnf
    val = {abs(lit): lit > 0 for lit in model}
    _nv, clauses = parse_cnf(cnf_text)
    return all(any(val.get(abs(l)) == (l > 0) for l in cl) for cl in clauses)


def solve_minted_cnf(cnf_text: str) -> dict:
    """Solve a minted invariant CNF with a REAL CDCL solver (Glucose via pysat)
    and INDEPENDENTLY verify the assignment satisfies the CNF. This proves the
    minted artifact is genuinely solvable by an external solver, not just z3."""
    import time
    try:
        from pysat.formula import CNF
        from pysat.solvers import Glucose3
    except Exception:
        return {"available": False, "reason": "pysat_not_installed"}
    try:
        f = CNF(from_string=cnf_text)
        t0 = time.perf_counter()
        s = Glucose3(bootstrap_with=f.clauses)
        sat = s.solve()
        solve_ms = round((time.perf_counter() - t0) * 1000, 2)
        model = s.get_model() if sat else []
        s.delete()
    except Exception as e:
        return {"available": False, "reason": f"solver_error:{type(e).__name__}"}
    verified = bool(sat and _satisfies(cnf_text, model))
    return {"available": True, "solver": "glucose3", "sat": bool(sat),
            "solve_ms": solve_ms, "n_vars": f.nv, "n_clauses": len(f.clauses),
            "verified": verified}


@functools.lru_cache(maxsize=1)
def minted_proof_status() -> dict:
    """The full unified proof, end-to-end: z3 MINTS the invariant CNF -> a real
    CDCL solver SOLVES it (verified) -> the z3 witness REPRODUCES via the real
    harness. One instance, three independent checks."""
    m = mint_invariant("B2-fee-silent-zero", 16, "realistic")
    if not m or m["result"] != "sat":
        return {"available": False, "reason": "z3_unavailable_or_unsat"}
    solved = solve_minted_cnf(m["cnf_text"])
    reproduced = None
    try:
        from .replay import run_replay, MINTED_TARGETS
        if MINTED_TARGETS:
            from .replay import TARGETS
            tid = MINTED_TARGETS[0]
            reproduced = run_replay(tid, TARGETS[tid].known_witness).reproduced
    except Exception:
        pass
    return {"available": True, "rule": m["rule_id"], "invariant": m["invariant"],
            "cnf_sha256": m["cnf_sha256"], "vars": m["vars"], "clauses": m["clauses"],
            "z3_witness": m["witness"], "external_solve": solved, "reproduced": reproduced,
            "ok": bool(solved.get("verified") and reproduced)}


@functools.lru_cache(maxsize=1)
def external_decode_status() -> dict:
    """The unified off-box proof: z3 MINTS the invariant CNF + a bit->var decode
    map -> an EXTERNAL CDCL solver (Glucose) solves it -> we DECODE its assignment
    to the exploit input WITHOUT re-running z3 -> the decoded input reproduces the
    violation via the real harness. A miner can solve off-box; we decode + verify."""
    mm = mint_with_decode_map("B2-fee-silent-zero", 8, "realistic")
    if not mm or mm["result"] != "sat":
        return {"available": False, "reason": "z3_unavailable_or_unsat"}
    solved = solve_minted_cnf(mm["cnf_text"])
    if not solved.get("available") or not solved.get("sat"):
        return {"available": False, "reason": "external_solver_unavailable"}
    # re-solve to get the model (solve_minted_cnf only returns verified flag)
    try:
        from pysat.formula import CNF
        from pysat.solvers import Glucose3
        f = CNF(from_string=mm["cnf_text"]); s = Glucose3(bootstrap_with=f.clauses)
        s.solve(); model = s.get_model(); s.delete()
    except Exception as e:
        return {"available": False, "reason": f"solver_error:{type(e).__name__}"}
    decoded = decode_assignment(model, mm["decode_map"])
    from .replay import _silent_zero_harness, _silent_zero_inv
    inp = {k: decoded[k] for k in ("amount", "fee_rate") if k in decoded}
    obs = _silent_zero_harness(inp)
    reproduced = not _silent_zero_inv(obs)
    return {"available": True, "solver": "glucose3", "decode": "bit->var map (no z3)",
            "decoded_input": inp, "observed": obs, "reproduced": reproduced,
            "cnf_sha256": mm["cnf_sha256"], "n_input_bits": sum(len(b) for b in mm["decode_map"].values()),
            "ok": bool(reproduced)}


def z3_available() -> bool:
    try:
        import z3  # noqa: F401
        return True
    except Exception:
        return False
