"""stitch-runner - a REAL remote execution environment.

Runs a miner's solve on Stitch (polarisserver) via SSH using a real competition
SAT solver (kissat/cadical), with the wall time MEASURED ON THE REMOTE HOST (the
eval host, not the miner's word - the timing invariant). The arena then verifies
the returned witness LOCALLY (dimacs.verify_witness) - remote compute, local
correctness gate. This is the difference between a "stitch-runner" label and a
real attested-adjacent execution environment.

Gated: the arena only routes through here when CATHEDRAL_ARENA_STITCH=1 (or the
caller asks), so the default test path stays local + deterministic. Reachability
is probed once and cached; if Stitch is down the caller falls back gracefully.
"""
from __future__ import annotations

import base64
import functools
import os
import subprocess

# Read from the environment rather than baked in. This repository is public,
# so a hardcoded default publishes the operator's account and address to
# anyone reading the source, and does it in a file most reviewers never open.
# Unset means the runner is unavailable, which the reachability probe already
# handles as a graceful fallback to the local path.
STITCH_HOST = os.environ.get("CATHEDRAL_ARENA_STITCH_HOST", "")
STITCH_NAME = "polarisserver"
STITCH_SSH_PORT = int(os.environ.get("CATHEDRAL_ARENA_STITCH_PORT", "22"))


def _tcp_reachable(timeout: float = 4.0) -> bool:
    """Fast pre-check: can a TCP connection to Stitch's ssh port open within `timeout`?
    A black-hole host (drops packets) makes `ssh` hang well past its ConnectTimeout, so
    a short socket probe is the reliable way to fail fast instead of blocking ~30s."""
    import socket
    if not STITCH_HOST:
        return False  # unconfigured is unreachable, not an error
    host = STITCH_HOST.split("@")[-1]
    try:
        with socket.create_connection((host, STITCH_SSH_PORT), timeout=timeout):
            return True
    except OSError:
        return False


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
def stitch_available(solver: str = "kissat", *, probe_timeout: float = 12.0) -> bool:
    """Probe once: Stitch reachable + the solver present. A fast TCP pre-check
    short-circuits a down/black-hole host in ~4s; the liveness ssh (a trivial
    `command -v`) is bounded to `probe_timeout` so the TCP-up-but-WSL-hung state
    (port 22 open, `wsl -e bash` stuck) fails in ~16s, not ~34s. Solve ops keep
    their own larger timeouts — this bound is only for the liveness check."""
    if not _tcp_reachable():
        return False
    try:
        b64 = base64.b64encode(f"command -v {solver} >/dev/null && echo OK".encode()).decode()
        r = _ssh(b64, timeout=probe_timeout)
        return "OK" in r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def stitch_status(receipt_path) -> dict:
    """Read the stored stitch-runner receipt for the operator console - real
    remote execution evidence. Handles both receipt shapes:
      * a per-miner solve (witness verified locally), and
      * a REAL pre-existing audit CNF cross-proof (kissat on Stitch vs a local
        CDCL solver - both agree, the invariant is proven hardened/exploitable on
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
    - a quote then proves "this specific solve ran in the TEE", not merely "some
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
    live quote is GATED (one bounded attestor call needs approval - no spend
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


def chunk_b64(s: str, size: int = 3000) -> list[str]:
    """Split a base64 string into command-line-safe chunks.

    The ssh->wsl hop caps a single command at about 10KB, so larger CNFs are
    uploaded in pieces because stdin does not forward through that hop.
    """
    return [s[i:i + size] for i in range(0, len(s), size)]


def _push_b64_chunks(b64: str, rb: str, chunk: int, timeout_s: float) -> int:
    """Append a base64 blob to remote file `rb` in command-line-safe chunks,
    TRUNCATING on the first chunk (`>`) so no separate `rm` round-trip is needed.
    Returns the number of ssh round-trips used (== number of chunks)."""
    parts = chunk_b64(b64, chunk)
    for i, part in enumerate(parts):
        op = ">" if i == 0 else ">>"
        _ssh(base64.b64encode(f"printf %s {part} {op} {rb}".encode()).decode(), timeout=timeout_s)
    return len(parts)


def upload_cnf(cnf_text: str, remote_path: str, *, chunk: int = 6000,
               timeout_s: float = 30.0) -> bool:
    """Upload a CNF to Stitch by CHUNKED base64 append (bypasses the single-command
    size limit; stdin doesn't forward through wsl). The CNF is GZIP-compressed first
    (DIMACS compresses ~3.5x) and the first chunk TRUNCATES (no rm round-trip), so a
    ~24KB CNF uploads in ~2 chunks + 1 decode = ~3 round-trips - robust to Stitch's
    flakiness. Best-effort."""
    import gzip
    b64 = base64.b64encode(gzip.compress(cnf_text.encode())).decode()
    rb = remote_path + ".gz.b64"
    try:
        _push_b64_chunks(b64, rb, chunk, timeout_s)
        r = _ssh(
            base64.b64encode(
                f"base64 -d {rb} | gunzip > {remote_path}; rm -f {rb}; wc -c < {remote_path}".encode()
            ).decode(),
            timeout=timeout_s,
        )
        try:
            return int(r.stdout.strip().splitlines()[-1]) == len(cnf_text)
        except (IndexError, ValueError):
            return False
    except (subprocess.TimeoutExpired, OSError):
        return False


def offbox_solve(cnf_text: str, *, chunk: int = 6000, timeout_s: float = 85.0) -> dict:
    """OFF-BOX solve, minimal round-trips: chunk-upload a (minted) CNF to Stitch
    (truncate-first, gzip'd), then in a SINGLE final command decode+gunzip+verify
    size+solve with kissat --relaxed (host-measured)+emit the assignment. Folding
    decode and solve into one atomic step means a Stitch flap can't split them - so
    the whole flow is ~3 round-trips for a 24KB CNF (was ~5). The caller decodes the
    raw assignment LOCALLY via the bit->var map (no z3) and replays. Best-effort."""
    import gzip
    import hashlib
    b64 = base64.b64encode(gzip.compress(cnf_text.encode())).decode()
    remote = f"/tmp/arena_offbox_{hashlib.sha256(cnf_text.encode()).hexdigest()[:10]}.cnf"
    rb = remote + ".gz.b64"
    try:
        n_chunks = _push_b64_chunks(b64, rb, chunk, timeout_s)
        inner = (f'base64 -d {rb} | gunzip > {remote} 2>/dev/null; SZ=$(wc -c < {remote}); rm -f {rb}; '
                 f'S=$(date +%s%N); kissat --relaxed {remote} >/tmp/arena_ob.out 2>/dev/null; RC=$?; '
                 f'E=$(date +%s%N); rm -f {remote}; echo "SIZE $SZ"; '
                 f'echo "ELAPSED_MS $(( (E-S)/1000000 ))"; echo "RC $RC"; '
                 f'grep "^s " /tmp/arena_ob.out; grep "^v " /tmp/arena_ob.out')
        r = _ssh(base64.b64encode(inner.encode()).decode(), timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": False, "reason": f"ssh_failed:{type(e).__name__}"}
    wall = rc = status = size = None
    lits: list[int] = []
    for ln in r.stdout.splitlines():
        if ln.startswith("SIZE"):
            try: size = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("ELAPSED_MS"):
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
    if size is not None and size != len(cnf_text):       # upload integrity check
        return {"available": False, "reason": f"upload_corrupt:{size}!={len(cnf_text)}"}
    sat = (rc == 10) or (status or "").upper().startswith("SAT")
    if not sat:
        return {"available": False, "reason": f"not_sat:rc={rc}"}
    return {"available": True, "host": STITCH_NAME, "solver": "kissat",
            "sat": True, "assignment": lits, "remote_wall_ms": wall, "n_lits": len(lits),
            "round_trips": n_chunks + 1}


def offbox_confirm_unsat(cnf_text: str, *, chunk: int = 6000, timeout_s: float = 120.0) -> dict:
    """OFF-BOX hardened confirmation — the DEFENSIVE counterpart of offbox_solve:
    chunk-upload a minted UNSAT CNF (truncate-first, gzip'd) and have kissat --relaxed
    confirm UNSAT (rc=20) on Stitch. There is no assignment to decode; the caller
    cross-checks that local z3+CDCL also say UNSAT (no exploit exists). Best-effort."""
    import gzip
    import hashlib
    b64 = base64.b64encode(gzip.compress(cnf_text.encode())).decode()
    remote = f"/tmp/arena_unsat_{hashlib.sha256(cnf_text.encode()).hexdigest()[:10]}.cnf"
    rb = remote + ".gz.b64"
    try:
        n_chunks = _push_b64_chunks(b64, rb, chunk, timeout_s)
        inner = (f'base64 -d {rb} | gunzip > {remote} 2>/dev/null; SZ=$(wc -c < {remote}); rm -f {rb}; '
                 f'S=$(date +%s%N); kissat --relaxed {remote} >/tmp/arena_un.out 2>/dev/null; RC=$?; '
                 f'E=$(date +%s%N); rm -f {remote}; echo "SIZE $SZ"; '
                 f'echo "ELAPSED_MS $(( (E-S)/1000000 ))"; echo "RC $RC"; grep "^s " /tmp/arena_un.out')
        r = _ssh(base64.b64encode(inner.encode()).decode(), timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"available": False, "reason": f"ssh_failed:{type(e).__name__}"}
    wall = rc = status = size = None
    for ln in r.stdout.splitlines():
        if ln.startswith("SIZE"):
            try: size = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("ELAPSED_MS"):
            try: wall = float(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("RC "):
            try: rc = int(ln.split()[1])
            except (IndexError, ValueError): pass
        elif ln.startswith("s "):
            status = ln[2:].strip()
    if size is not None and size != len(cnf_text):       # upload integrity check
        return {"available": False, "reason": f"upload_corrupt:{size}!={len(cnf_text)}"}
    unsat = (rc == 20) or (status or "").upper().startswith("UNSAT")
    return {"available": True, "host": STITCH_NAME, "solver": "kissat",
            "unsat": unsat, "rc": rc, "remote_wall_ms": wall, "round_trips": n_chunks + 1}


def solve_remote_cnf(remote_path: str, *, timeout_s: float = 85.0) -> dict:
    """Solve a CNF ALREADY ON Stitch with kissat (host-measured), WITHOUT pulling
    it back - works for arbitrarily large CNFs (pushing OR pulling a CNF through
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


def offbox_proof_summary(out_dir) -> dict:
    """Aggregate ALL captured off-box proof receipts in out_dir into one summary — the
    off-box capability's track record across rules and BOTH directions (exploit /
    hardened), all on real remote hardware. Reads offbox_*receipt.json; pure."""
    import json
    from pathlib import Path
    out = Path(out_dir)
    exploits: list[dict] = []
    hardened: list[dict] = []
    for p in sorted(out.glob("offbox_*receipt.json")):
        try:
            d = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not d.get("available"):
            continue
        if d.get("verdict") == "HARDENED" and d.get("cross_confirmed"):
            hardened.append({"rule": d.get("rule_id"), "host": d.get("host"),
                             "wall_ms": d.get("remote_wall_ms"), "file": p.name})
        elif d.get("cnf_satisfied"):
            exploits.append({"rule": d.get("rule_id", "B2-fee-silent-zero"), "host": d.get("host"),
                             "wall_ms": d.get("remote_wall_ms"), "n_lits": d.get("n_lits"), "file": p.name})
    every = exploits + hardened
    return {"exploits": exploits, "hardened": hardened,
            "n_exploits": len(exploits), "n_hardened": len(hardened),
            "rules": sorted({e["rule"] for e in every if e.get("rule")}),
            "hosts": sorted({e["host"] for e in every if e.get("host")}),
            "all_real_hardware": bool(every) and all(e.get("host") == STITCH_NAME for e in every)}


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


def offbox_receipt_status(path) -> dict:
    """Read a stored off-box-on-Stitch receipt for the operator console: kissat
    solved a minted CNF on Stitch and the arena decoded it locally with NO z3,
    proving the off-box solution satisfies the pinned-invariant CNF (cnf_satisfied).
    Honest: returns unavailable until a real run lands one."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {"available": False, "reason": "no_offbox_receipt"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "offbox_receipt_unparseable"}
    return {"available": bool(d.get("available")), "host": d.get("host"),
            "solver": d.get("solver"), "cnf_satisfied": d.get("cnf_satisfied"),
            "rule_id": d.get("rule_id"),
            "harness_reproduced": d.get("harness_reproduced"),
            "decoded_input": d.get("decoded_input"), "n_lits": d.get("n_lits"),
            "remote_wall_ms": d.get("remote_wall_ms"), "round_trips": d.get("round_trips"),
            "decode": d.get("decode"), "cnf_sha256": d.get("cnf_sha256"),
            "captured_at": d.get("captured_at")}


def offbox_hardened_status(path) -> dict:
    """Read a stored off-box HARDENED receipt for the operator console: kissat on
    Stitch confirmed a minted invariant UNSAT and a local CDCL solver agreed (no
    exploit exists). Unavailable until a real run lands one."""
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {"available": False, "reason": "no_hardened_receipt"}
    try:
        d = json.loads(p.read_text())
    except Exception:
        return {"available": False, "reason": "hardened_receipt_unparseable"}
    return {"available": bool(d.get("available")), "host": d.get("host"),
            "solver": d.get("solver"), "rule_id": d.get("rule_id"),
            "verdict": d.get("verdict"), "remote_unsat": d.get("remote_unsat"),
            "local_unsat": d.get("local_unsat"), "cross_confirmed": d.get("cross_confirmed"),
            "remote_wall_ms": d.get("remote_wall_ms"), "round_trips": d.get("round_trips"),
            "cnf_sha256": d.get("cnf_sha256"), "captured_at": d.get("captured_at")}


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
