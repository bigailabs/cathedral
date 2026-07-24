"""Solver sandbox — the eval host's containment for running a miner's solver.

Salvaged from the monolith's orphaned `src/cathedral/v4/oracle/jail.py`
containment pattern (network-isolated, rlimit-bounded subprocess, bounded
output capture, process-group kill on timeout). The monolith's jail ran a
pinned Python interpreter against miner test code; here we run a SOLVER BINARY
against a CNF, but the isolation contract is the same:

  * a fresh USER + MOUNT + NET + PID namespace via unshare(1) — no loopback,
    no host routes, so a solver cannot phone home or exfiltrate the hidden
    batch (the network-isolation property the jail proved against a /etc/hosts
    leak generalizes to "a solver must not reach the network").
  * RLIMIT_CPU + RLIMIT_AS ceilings inherited across exec, so a runaway solver
    cannot exhaust the eval host.
  * a wall-clock deadline enforced by the parent; on expiry the WHOLE process
    group is SIGKILLed (the inner solver lives in a fresh PID ns, so killing
    only the wrapper would leak it — the jail's process-group lesson).
  * the wall time the runner records is MEASURED HERE, by the eval host, never
    read from the solver's own output (the timing.py invariant).

TIMEOUT is therefore the EVAL HOST'S OBSERVATION (`timed_out=True`), never a
miner claim — exactly the locked-decision policy.

Containment requires Linux + a recent unshare(1) with unprivileged user
namespaces. Where that is unavailable (CI without userns, macOS) the runner
falls back to a plain rlimit-bounded subprocess and records
`contained=False` HONESTLY, so a result is never silently treated as
network-isolated when it was not. The eval host in production MUST run on a
host where `sandbox_available()` is True.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

try:
    import resource
except ModuleNotFoundError:  # Windows/local smoke tests; production sandbox is Linux.
    resource = None

_MAX_OUTPUT_BYTES = 16 * 1024 * 1024     # cap captured stdout (a DRAT proof can be large)
_UNSHARE_MIN_VERSION = (2, 36)


@dataclass(frozen=True)
class RunResult:
    """One sandboxed solver invocation, observed by the eval host."""
    stdout: str
    stderr: str
    returncode: int | None
    wall_ms: float              # MEASURED by the eval host, never the solver's word
    timed_out: bool             # the host's own observation -> TIMEOUT outcome
    contained: bool             # True iff it ran network-isolated + rlimit-bounded


def _unshare_version() -> tuple[int, int] | None:
    import re
    binary = shutil.which("unshare")
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True,
                             text=True, timeout=2.0, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out.stdout or out.stderr or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _userns_available() -> bool:
    binary = shutil.which("unshare")
    if not binary:
        return False
    try:
        out = subprocess.run([binary, "--user", "--map-root-user", "true"],
                             capture_output=True, text=True, timeout=2.0, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return out.returncode == 0


def sandbox_available() -> bool:
    """True iff this host can run a solver network-isolated + rlimit-bounded."""
    if not sys.platform.startswith("linux"):
        return False
    v = _unshare_version()
    return bool(v and v >= _UNSHARE_MIN_VERSION and _userns_available())


def _set_limits(cpu_secs: int, as_bytes: int):
    if resource is None:
        return None

    def _pre() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_secs, cpu_secs))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
        except (ValueError, OSError):
            pass
    return _pre


def run_solver(
    argv: list[str], *, timeout_s: float, cwd: str | None = None,
    cpu_secs: int | None = None, as_bytes: int = 2 * 1024 * 1024 * 1024,
    contain: bool | None = None,
) -> RunResult:
    """Run `argv` (a solver invocation) under containment + a wall deadline.

    The host measures wall time and observes the timeout itself; nothing the
    solver prints can change either. Returns a RunResult — never raises on a
    solver error (a crashed/OOM/timed-out solver is a NON-result, scored 0 by
    the lane, not an exception that breaks the round).
    """
    if contain is None:
        contain = sandbox_available()
    cpu_secs = cpu_secs if cpu_secs is not None else int(timeout_s) + 2

    full_argv = list(argv)
    if contain:
        unshare = shutil.which("unshare") or "unshare"
        full_argv = [unshare, "--user", "--map-root-user", "--mount", "--net",
                     "--pid", "--fork", "--mount-proc", "--"] + list(argv)

    started = time.monotonic()
    try:
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "cwd": cwd,
            "start_new_session": True,
        }
        preexec_fn = _set_limits(cpu_secs, as_bytes)
        if preexec_fn is not None:
            popen_kwargs["preexec_fn"] = preexec_fn
        proc = subprocess.Popen(full_argv, **popen_kwargs)
    except (OSError, FileNotFoundError) as e:
        return RunResult("", f"spawn_failed:{e}", None,
                         round((time.monotonic() - started) * 1000, 3), False, contain)

    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    wall_ms = round((time.monotonic() - started) * 1000, 3)

    out = (out or b"")[:_MAX_OUTPUT_BYTES]
    err = (err or b"")[:_MAX_OUTPUT_BYTES]
    rc = None if timed_out else proc.returncode
    return RunResult(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                     rc, wall_ms, timed_out, contain)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


__all__ = ["RunResult", "run_solver", "sandbox_available"]
