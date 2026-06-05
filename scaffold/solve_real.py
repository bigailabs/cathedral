"""Real solver adapters — the verify-don't-solve toolchain shelled to installed
binaries. Thin by design: Cathedral verifies; the heavy solving is the binary's.

Proven on Stitch 2026-06-04:
  cryptominisat5 in.cnf proof.drat   -> exit 20 (UNSAT) + DRAT proof
  drat-trim in.cnf proof.drat        -> "s VERIFIED"
  cryptominisat5 in.cnf              -> "s SATISFIABLE" + "v <model> 0"

Binaries are configurable via env (CMSAT_BIN, DRAT_TRIM_BIN). If a tool is
absent the adapter says so honestly rather than faking a result.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from shutil import which

CMSAT = os.environ.get("CMSAT_BIN", "cryptominisat5")
DRAT_TRIM = os.environ.get("DRAT_TRIM_BIN", "drat-trim")  # e.g. ~/tools/drat-trim/drat-trim


def _have(binary: str) -> bool:
    return which(binary) is not None or os.path.exists(os.path.expanduser(binary))


def have_solver() -> bool:
    return _have(CMSAT)


def have_drat_trim() -> bool:
    return _have(DRAT_TRIM)


def solve_cnf_real(cnf_text: str, *, want_drat: bool = False, timeout: int = 120) -> dict:
    """Run cryptominisat5. Returns:
      {status: 'SAT', model: [signed lits]} |
      {status: 'UNSAT', drat: <proof text if want_drat>} |
      {status: 'UNKNOWN'|'UNAVAILABLE', ...}
    """
    if not _have(CMSAT):
        return {"status": "UNAVAILABLE", "reason": f"{CMSAT} not installed"}
    with tempfile.TemporaryDirectory() as d:
        cnf = Path(d) / "p.cnf"
        cnf.write_text(cnf_text)
        drat = Path(d) / "p.drat"
        cmd = [CMSAT, "--verb=0", str(cnf)] + ([str(drat)] if want_drat else [])
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "UNKNOWN", "reason": "solver_timeout"}
        if r.returncode == 10:                     # SATISFIABLE
            model: list[int] = []
            for line in r.stdout.splitlines():
                if line.startswith("v "):
                    model += [int(x) for x in line[2:].split() if x.strip() and int(x) != 0]
            return {"status": "SAT", "model": model}
        if r.returncode == 20:                     # UNSATISFIABLE
            res = {"status": "UNSAT"}
            if want_drat and drat.exists():
                res["drat"] = drat.read_text()
            return res
        return {"status": "UNKNOWN", "rc": r.returncode}


def verify_drat(cnf_text: str, drat_text: str, *, timeout: int = 120) -> tuple[bool, str]:
    """Verify an UNSAT proof certificate with drat-trim. Returns (verified, detail).
    This is the REAL UNSAT check — no shape-guessing."""
    if not _have(DRAT_TRIM):
        return False, "drat-trim not installed"
    binary = DRAT_TRIM if which(DRAT_TRIM) else os.path.expanduser(DRAT_TRIM)
    with tempfile.TemporaryDirectory() as d:
        cnf = Path(d) / "p.cnf"
        cnf.write_text(cnf_text)
        pf = Path(d) / "p.drat"
        pf.write_text(drat_text)
        try:
            r = subprocess.run([binary, str(cnf), str(pf)],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "drat_trim_timeout"
        # require clean exit AND an exact 's VERIFIED' status line (not a substring)
        ok = r.returncode == 0 and any(
            line.strip() == "s VERIFIED" for line in r.stdout.splitlines())
        return ok, ("VERIFIED" if ok else r.stdout.strip()[-160:])
