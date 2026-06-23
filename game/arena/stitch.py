"""stitch-runner — a REAL remote execution environment.

Runs a miner's solve on Stitch (polarisserver) via SSH using a real competition
SAT solver (kissat/cadical), with the wall time MEASURED ON THE REMOTE HOST (the
eval host, not the miner's word — the timing invariant). The arena then verifies
the returned witness LOCALLY (dimacs.verify_witness) — remote compute, local
correctness gate. This is the difference between a "stitch-runner" label and a
real attested-adjacent execution environment.

Gated: the arena only routes through here when CATHEDRAL_ARENA_STITCH=1 (or the
caller asks), so the default test path stays local + deterministic. Reachability
is probed once and cached; if Stitch is down the caller falls back gracefully.
"""
from __future__ import annotations

import base64
import functools
import subprocess

STITCH_HOST = "frede@100.112.113.3"
STITCH_NAME = "polarisserver"


def _ssh(inner_b64: str, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=no", STITCH_HOST,
         f"wsl -e bash -lc 'echo {inner_b64} | base64 -d | bash'"],
        capture_output=True, text=True, timeout=timeout)


def remote_script(cnf_b64: str, solver: str = "kissat") -> str:
    """The bash run on Stitch: decode the CNF, run the real solver, and emit the
    HOST-measured wall (ns clock on the remote box) + the solver's DIMACS output."""
    return (
        f'f=$(mktemp /tmp/arena_XXXX.cnf); echo {cnf_b64} | base64 -d > "$f"; '
        f'S=$(date +%s%N); OUT=$({solver} -q "$f" 2>/dev/null); E=$(date +%s%N); '
        f'rm -f "$f"; echo "ELAPSED_MS $(( (E-S)/1000000 ))"; echo "$OUT"')


def parse_solver_output(stdout: str) -> tuple[list[int], float, str]:
    """Parse (assignment, wall_ms, status) from the remote solver output. Pure."""
    wall_ms = 0.0
    status = "UNKNOWN"
    lits: list[int] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("ELAPSED_MS"):
            try:
                wall_ms = float(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("s "):
            status = line[2:].strip()
        elif line.startswith("v "):
            for tok in line[2:].split():
                v = int(tok)
                if v == 0:
                    break
                lits.append(v)
    return lits, wall_ms, status


@functools.lru_cache(maxsize=1)
def stitch_available(solver: str = "kissat") -> bool:
    """Probe once: Stitch reachable + the solver present."""
    try:
        b64 = base64.b64encode(f"command -v {solver} >/dev/null && echo OK".encode()).decode()
        r = _ssh(b64, timeout=30)
        return "OK" in r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def stitch_status(receipt_path) -> dict:
    """Read the stored stitch-runner receipt for the operator console — real
    remote execution evidence. Handles both receipt shapes:
      * a per-miner solve (witness verified locally), and
      * a REAL pre-existing audit CNF cross-proof (kissat on Stitch vs a local
        CDCL solver — both agree, the invariant is proven hardened/exploitable on
        the actual artifact). `ok` is true when there is real verified evidence."""
    import json
    from pathlib import Path
    p = Path(receipt_path)
    if not p.exists():
        return {"available": False, "reason": "no_stitch_run_yet"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "receipt_unparseable"}
    cross = bool(d.get("cross_solver_agree"))
    ok = bool(d.get("witness_verified_locally")) or cross
    return {"available": True, "ok": ok,
            "host": d.get("host"), "solver": d.get("solver"),
            "solver_version": d.get("solver_version"),
            "remote_status": d.get("remote_status"),
            "remote_wall_ms": d.get("remote_wall_ms"),
            "n_vars": d.get("n_vars"), "n_clauses": d.get("n_clauses"),
            "real_cnf": d.get("real_cnf"), "cnf_sha256": d.get("cnf_sha256"),
            "cross_solver_agree": cross, "local_solver": d.get("local_solver"),
            "local_solve_ms": d.get("local_solve_ms"), "solver_race": d.get("solver_race"),
            "hardened_proof": bool(d.get("hardened_proof")),
            "source": d.get("source")}


def solve_commitment(receipt: dict) -> str:
    """A canonical hex commitment binding a TDX quote to THIS EXACT remote solve.
    Used as the attestation nonce so report_data[0:32] = sha256(commitment||pubkey)
    — a quote then proves "this specific solve ran in the TEE", not merely "some
    solve ran". Covers the verifiable solve fields only (deterministic)."""
    import hashlib
    import json
    fields = {k: receipt.get(k) for k in
              ("real_cnf", "cnf_sha256", "host", "solver", "remote_status",
               "remote_wall_ms", "local_solver", "cross_solver_agree", "hardened_proof")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def attest_readiness(receipt_path, pubkey_b64: str = "", *, quote_path=None) -> dict:
    """Make the REAL Stitch solve attestable. Derives the solve commitment (the
    report_data preimage) so a future TDX quote binds to this exact solve. The
    live quote is GATED (one bounded attestor call needs approval — no spend
    here): with no quote present this reports ready + the commitment. If a quote
    artifact exists it is verified to bind to the commitment (verify-by-receipt)."""
    import json
    from pathlib import Path
    p = Path(receipt_path)
    if not p.exists():
        return {"available": False, "reason": "no_stitch_solve"}
    try:
        rec = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "receipt_unparseable"}
    commitment = solve_commitment(rec)
    out = {"available": True, "solve": rec.get("real_cnf"), "commitment": commitment,
           "live_quote": False, "attested": False,
           "note": ("report_data would bind sha256(commitment||pubkey); one bounded "
                    "TDX quote from the Polaris attestor is gated on approval (no spend)")}
    qp = Path(quote_path) if quote_path else None
    if qp and qp.exists():
        try:
            qd = json.loads(qp.read_text())
            from .attestation import verify_real_quote
            v = verify_real_quote(qd.get("quote_b64") or qd.get("quote") or "",
                                  nonce_hex=commitment,
                                  e2e_pubkey_b64=qd.get("e2e_pubkey_b64") or pubkey_b64)
            out.update({"live_quote": True, "attested": bool(v.get("ok")), "verdict": v})
        except Exception as e:
            out["quote_error"] = type(e).__name__
    return out


def parse_remote_solve(stdout: str) -> dict:
    """Pure parser for a pull-free remote solve: status/exit/wall/n_lits/model_sha."""
    wall = rc = status = None
    nlits = 0
    msha = ""
    for ln in stdout.splitlines():
        ln = ln.strip()
        if ln.startswith("ELAPSED_MS"):
            try: wall = float(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("RC "):
            try: rc = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("s "):
            status = ln[2:].strip()
        elif ln.startswith("NLITS "):
            try: nlits = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("MSHA "):
            msha = ln.split()[1] if len(ln.split()) > 1 else ""
    sat = (rc == 10) or (status or "").upper().startswith("SAT")
    unsat = (rc == 20) or (status or "").upper().startswith("UNSAT")
    return {"remote_exit_code": rc, "remote_wall_ms": wall, "n_lits": nlits,
            "model_sha256": msha, "sat": sat, "unsat": unsat,
            "status": "SAT" if sat else ("UNSAT" if unsat else status)}


def solve_remote_cnf(remote_path: str, *, timeout_s: float = 85.0) -> dict:
    """Solve a CNF ALREADY ON Stitch with kissat (host-measured), WITHOUT pulling
    it back — works for arbitrarily large CNFs (pushing OR pulling a CNF through
    the ssh->wsl hop is not viable: stdin doesn't forward and >~10KB overflows the
    command line). For a SAT real audit CNF this proves the invariant is VIOLABLE
    on real hardware; independent cross-confirmation comes from the z3-minted twin
    (replay.py), which reproduces the same violation via the real harness."""
    inner = (f'f={remote_path}; if [ ! -f "$f" ]; then echo MISSING; exit 0; fi; '
             f'S=$(date +%s%N); kissat --relaxed "$f" >/tmp/arena_rem.out 2>/dev/null; RC=$?; '
             f'E=$(date +%s%N); echo "ELAPSED_MS $(( (E-S)/1000000 ))"; echo "RC $RC"; '
             f'grep "^s " /tmp/arena_rem.out; echo "NLITS $(grep "^v " /tmp/arena_rem.out | wc -w)"; '
             f'echo "MSHA $(grep "^v " /tmp/arena_rem.out | sha256sum | cut -c1-32)"')
    try:
        r = _ssh(base64.b64encode(inner.encode()).decode(), timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": False, "reason": f"ssh_failed:{type(e).__name__}"}
    if "MISSING" in r.stdout:
        return {"available": False, "reason": "remote_cnf_missing"}
    parsed = parse_remote_solve(r.stdout)
    if parsed["status"] is None:
        return {"available": False, "reason": "no_solver_output"}
    return {"available": True, "host": STITCH_NAME, "solver": "kissat",
            "remote_path": remote_path, **parsed}


# the real artifact directories on Stitch (polarisserver WSL home).
INVENTORY_DIRS = [
    "/home/frede/audit-cnf", "/home/frede/audit-factory", "/home/frede/subtensor-hunt",
    "/home/frede/subnet-2", "/home/frede/code/sat-experiments",
    "/home/frede/code/cathedral-pathway2",
]


def parse_inventory(stdout: str) -> list[dict]:
    """Pure parser for the inventory `find` output (one 'DIR <path> cnf=N map=N
    py=N' line per directory; MISSING when absent)."""
    rows = []
    for ln in stdout.splitlines():
        ln = ln.strip()
        if not ln.startswith("DIR "):
            continue
        toks = ln.split()
        path = toks[1]
        if len(toks) >= 3 and toks[2] == "MISSING":
            rows.append({"dir": path, "present": False})
            continue
        kv = {}
        for t in toks[2:]:
            if "=" in t:
                k, v = t.split("=", 1)
                try: kv[k] = int(v)
                except ValueError: pass
        rows.append({"dir": path, "present": True, "cnf": kv.get("cnf", 0),
                     "map": kv.get("map", 0), "py": kv.get("py", 0)})
    return rows


def inventory(dirs: list[str] | None = None, *, timeout_s: float = 60.0) -> dict:
    """Catalog the REAL artifact corpus on Stitch (read-only `find`): per directory,
    count CNFs, decode maps, and python harnesses. Opt-in + reachability-gated; the
    result is a manifest the operator console can surface (proof the real corpus
    exists, not a claim). Does not pull any file."""
    dirs = dirs or INVENTORY_DIRS
    quoted = " ".join(dirs)
    inner = (
        f'for d in {quoted}; do '
        f'if [ -d "$d" ]; then '
        f'c=$(find "$d" -maxdepth 4 -name "*.cnf" 2>/dev/null | wc -l); '
        f'm=$(find "$d" -maxdepth 4 \\( -name "*.map*" -o -name "*decode*" \\) 2>/dev/null | wc -l); '
        f'p=$(find "$d" -maxdepth 3 -name "*.py" 2>/dev/null | wc -l); '
        f'echo "DIR $d cnf=$c map=$m py=$p"; else echo "DIR $d MISSING"; fi; done')
    try:
        r = _ssh(base64.b64encode(inner.encode()).decode(), timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": False, "reason": f"ssh_failed:{type(e).__name__}"}
    rows = parse_inventory(r.stdout)
    if not rows:
        return {"available": False, "reason": "no_inventory_output"}
    return {"available": True, "host": STITCH_NAME, "dirs": rows,
            "total_cnf": sum(d.get("cnf", 0) for d in rows),
            "total_map": sum(d.get("map", 0) for d in rows),
            "total_py": sum(d.get("py", 0) for d in rows)}


def inventory_status(path) -> dict:
    """Read a stored inventory manifest for the operator console."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {"available": False, "reason": "no_inventory"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "inventory_unparseable"}
    return {"available": True, "host": d.get("host"),
            "total_cnf": d.get("total_cnf"), "total_map": d.get("total_map"),
            "total_py": d.get("total_py"), "n_dirs": len(d.get("dirs", [])),
            "captured_at": d.get("captured_at"),
            "dirs": [r for r in d.get("dirs", []) if r.get("present")][:8]}


def remote_sat_status(receipt_path) -> dict:
    """Read a stored pull-free remote-SAT receipt for the operator console: the
    real audit CNF proven VIOLABLE on Stitch + cross-confirmed by the z3-minted
    twin reproducing the same violation locally."""
    import json
    from pathlib import Path
    p = Path(receipt_path)
    if not p.exists():
        return {"available": False, "reason": "no_remote_sat_run"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "receipt_unparseable"}
    return {"available": True, "real_cnf": d.get("real_cnf"), "host": d.get("host"),
            "solver": d.get("solver"), "status": d.get("status"),
            "remote_wall_ms": d.get("remote_wall_ms"), "n_lits": d.get("n_lits"),
            "model_sha256": d.get("model_sha256"), "violable": bool(d.get("violable")),
            "twin_reproduces_locally": bool(d.get("twin_reproduces_locally")),
            "cross_confirmed": bool(d.get("cross_confirmed")), "invariant": d.get("invariant")}


def prove_real_cnf_on_stitch(remote_path: str, invariant: str, name: str,
                             *, timeout_s: float = 85.0) -> dict:
    """Solve a REAL pre-existing audit CNF on Stitch with kissat (host-measured)
    AND cross-check the result with a local CDCL solver (Glucose) on the SAME
    pinned CNF. UNSAT-agreement => the invariant is proven hardened; SAT =>
    kissat's witness is verified locally against the formula. Returns a receipt
    dict; {available: False, reason} on any failure. Best-effort + bounded; the
    arena's default test path never calls this (no Stitch dependency)."""
    import hashlib
    inner = (f'f={remote_path}; S=$(date +%s%N); '
             f'kissat --relaxed "$f" >/tmp/arena_ks.out 2>/dev/null; RC=$?; E=$(date +%s%N); '
             f'echo "ELAPSED_MS $(( (E-S)/1000000 ))"; echo "RC $RC"; '
             f'grep "^s " /tmp/arena_ks.out; grep "^v " /tmp/arena_ks.out; '
             f'echo CNFSTART; base64 "$f"; echo CNFEND')
    try:
        r = _ssh(base64.b64encode(inner.encode()).decode(), timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": False, "reason": f"ssh_failed:{type(e).__name__}"}
    out = r.stdout
    if "CNFSTART" not in out or "CNFEND" not in out:
        return {"available": False, "reason": "no_cnf_in_output"}
    wall = rc = status = None
    lits: list[int] = []
    for ln in out.splitlines():
        if ln.startswith("ELAPSED_MS"):
            try: wall = float(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("RC "):
            try: rc = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("s "):
            status = ln[2:].strip()
        elif ln.startswith("v "):
            for tok in ln[2:].split():
                v = int(tok)
                if v == 0:
                    break
                lits.append(v)
    try:
        cnf = base64.b64decode(out.split("CNFSTART")[1].split("CNFEND")[0].strip()).decode()
    except Exception:
        return {"available": False, "reason": "cnf_decode_failed"}

    remote_sat = (rc == 10) or (status or "").upper().startswith("SAT")
    remote_unsat = (rc == 20) or (status or "").upper().startswith("UNSAT")
    from .mint import solve_minted_cnf, _satisfies
    loc = solve_minted_cnf(cnf)
    local_unsat = bool(loc.get("available") and loc.get("sat") is False)
    witness_ok = bool(remote_sat and lits and _satisfies(cnf, lits))
    # a REAL solver race on a REAL audit CNF: kissat on Stitch (host-measured) vs
    # a local CDCL solver on the SAME pinned formula. Winner = lower wall.
    local_ms = loc.get("solve_ms")
    race = None
    if wall is not None and local_ms is not None:
        race = {"remote": {"solver": "kissat", "host": STITCH_NAME, "ms": wall},
                "local": {"solver": loc.get("solver", "glucose3"), "host": "local", "ms": local_ms},
                "winner": "kissat" if wall <= local_ms else loc.get("solver", "glucose3")}
    receipt = {
        "real_cnf": name, "invariant": invariant, "host": STITCH_NAME,
        "solver": "kissat", "remote_exit_code": rc,
        "remote_status": "SAT" if remote_sat else ("UNSAT" if remote_unsat else status),
        "remote_wall_ms": wall, "n_clauses": loc.get("n_clauses"), "n_vars": loc.get("n_vars"),
        "cnf_sha256": hashlib.sha256(cnf.encode()).hexdigest(), "cnf_bytes": len(cnf),
        "local_solver": loc.get("solver", "glucose3"), "local_solve_ms": local_ms,
        "local_status": "UNSAT" if local_unsat else ("SAT" if loc.get("sat") else "?"),
        "cross_solver_agree": bool(remote_unsat and local_unsat),
        "hardened_proof": bool(remote_unsat and local_unsat),
        "witness_verified_locally": witness_ok if remote_sat else None,
        "solver_race": race,
        "source": "real pre-existing audit CNF on Stitch (audit-cnf/factory)",
    }
    return {"available": True, **receipt}


def run_on_stitch(cnf_text: str, *, timeout_s: float = 30.0,
                  solver: str = "kissat") -> dict:
    """Execute the solve on Stitch with a real solver. Returns a result dict; on
    any failure returns {ok: False, reason}. The wall is remote-host-measured."""
    cnf_b64 = base64.b64encode(cnf_text.encode()).decode()
    inner_b64 = base64.b64encode(remote_script(cnf_b64, solver).encode()).decode()
    try:
        r = _ssh(inner_b64, timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "reason": f"ssh_failed:{type(e).__name__}"}
    if r.returncode != 0 and not r.stdout:
        return {"ok": False, "reason": f"remote_error:{r.stderr.strip()[-160:]}"}
    assignment, wall_ms, status = parse_solver_output(r.stdout)
    return {"ok": bool(assignment), "assignment": assignment, "wall_ms": wall_ms,
            "status": status, "host": STITCH_NAME, "solver": solver,
            "environment": "stitch-runner", "remote_measured": True}
