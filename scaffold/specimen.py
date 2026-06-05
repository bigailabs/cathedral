"""Worked specimens — turn one REAL solved challenge per lane into a human-
readable "here is the intelligence in practice" panel: the contract property,
the logic/conditionals it compiles to, the verdict, and the witness as proof.

Pure-python (no z3): given the public problem + the verified artifact the runner
already has, it re-derives the proof arithmetic so the numbers shown are real,
not illustrative.
"""
from __future__ import annotations


def encode_specimen(pi: dict, witness) -> dict:
    """Lane C — the Bittensor Substrate<->EVM balance round-trip, the planted
    fault's actual conditionals, and the counterexample that breaks it."""
    W = pi["width"]; K = pi["round_const"]; mod = 1 << W
    Kinv = pow(K, -1, mod)
    mut = pi["mutation"]; tg = pi["trigger"]
    k, t = tg["k"], tg["t"]
    logic = [
        f"e   := (s · {K}) mod 2^{W}            # intoEvm: scale TAO up to 18-dec",
        f"s'  := (e · K⁻¹) mod 2^{W}            # intoSubstrate: scale back to RAO",
        f"fault := low_{k}bits( mix(s) ) == {t}      # planted bug only fires here",
    ]
    if mut == "off_by_one":
        logic.append("s2  := fault ? s'+1 : s'              # off-by-one in the faulty branch")
    elif mut == "wrong_const":
        logic.append("s2  := fault ? e·(K⁻¹+2) : s'         # wrong inverse constant")
    elif mut == "truncate_low":
        logic.append("s2  := fault ? (s' AND ~1) : s'       # drops the low bit")
    else:
        logic.append("s2  := s'                              # faithful (no planted fault)")
    logic.append("ASSERT  s2 == s                          # value must be conserved")
    spec = {"rail": "Encode", "contract": "Bittensor Substrate↔EVM balance bridge",
            "property": "intoSubstrate(intoEvm(s)) == s   —  TAO must survive the 9↔18-decimal round-trip",
            "logic": logic,
            "params": f"width={W} bits · K={K} · trigger rarity k={k}/{W}",
            "mutation": mut}
    if witness is None or mut == "none":
        spec["verdict"] = "UNSAT — proven safe in-band (no counterexample exists)"
        spec["proof"] = []
        return spec
    s = int(witness)
    faithful = (s * K % mod) * Kinv % mod
    if mut == "off_by_one":
        buggy = (faithful + 1) % mod
    elif mut == "wrong_const":
        buggy = (s * K % mod) * ((Kinv + 2) % mod) % mod
    elif mut == "truncate_low":
        buggy = faithful & ~1
    else:
        buggy = faithful
    delta = buggy - s
    spec["verdict"] = "SAT — counterexample found (a real value-conservation bug)"
    spec["proof"] = [
        f"witness   s = {s}",
        f"faithful  intoSubstrate(intoEvm(s)) = {faithful}   ✓ equals s",
        f"BUGGY     contract returns           = {buggy}",
        f"⇒ s2 − s = {delta:+}  →  the bridge { 'MINTS' if delta>0 else 'BURNS' } {abs(delta)} RAO on this input",
    ]
    return spec


def solve_specimen(pi: dict, assignment) -> dict:
    """Lane A — a planted-SAT CNF, three of its actual clauses, and the witness
    shown satisfying them (the validator checks the formula, nothing else)."""
    nv = pi.get("n_vars", 0); ncl = pi.get("n_clauses", 0)
    clauses = []
    for ln in (pi.get("cnf", "") or "").splitlines():
        ln = ln.strip()
        if not ln or ln[0] in "cp":
            continue
        lits = [int(x) for x in ln.split() if x not in ("0", "")]
        if lits:
            clauses.append(lits)
        if len(clauses) >= 3:
            break
    val = {abs(x): (x > 0) for x in (assignment or [])}

    def show(cl):
        parts = []
        for lit in cl:
            v = abs(lit); name = f"x{v}"
            term = name if lit > 0 else f"¬{name}"
            sat = (val.get(v) == (lit > 0))
            parts.append(f"{term}={'T' if val.get(v) else 'F'}{'✓' if sat else ''}")
        return "( " + "  ∨  ".join(parts) + " )"

    logic = [f"clause {i+1}:  {show(cl)}" for i, cl in enumerate(clauses)]
    return {"rail": "Solve", "contract": f"Boolean satisfiability — {nv} vars / {ncl} clauses",
            "property": "find an assignment satisfying EVERY clause (the witness self-verifies)",
            "logic": logic, "params": f"{nv} variables · {ncl} clauses · fastest valid witness wins",
            "verdict": "SAT — witness satisfies all clauses (validator re-checked the formula)",
            "proof": [f"assignment: {', '.join(f'x{i+1}={int(val.get(i+1, True))}' for i in range(min(nv,10)))}"
                      + (" …" if nv > 10 else "")]}


def improve_specimen(rail_stat: dict) -> dict:
    """Lane B — the attested-solve binding: speed is credited only from an
    elapsed measured in-TEE and bound into the hardware quote."""
    return {"rail": "Improve", "contract": "Attested solve — make 'faster' provable",
            "property": "a solve's wall-clock is trustworthy only if it is hardware-attested",
            "logic": [
                "quote.report_data[0:32]  := sha256(nonce ‖ pubkey)",
                "quote.report_data[32:64] := sha256(image ‖ sha256(stdout))   # binds the run",
                "stdout contains  elapsed_ms=<measured in-TEE>                 # ⇒ elapsed is bound",
                "verify: report_data matches  AND  image == pinned solver",
                "ASSERT  tampering elapsed ⇒ binding breaks ⇒ rejected",
            ],
            "params": "speed credited ONLY from the attested elapsed · never self-reported",
            "verdict": "verified solve earns a correctness floor; attested speed unlocks the rest",
            "proof": [f"attested finds so far: {int(rail_stat.get('finds', 0))} · "
                      f"correctness floor 0.70 + up to 0.30 from proven speed"]}
