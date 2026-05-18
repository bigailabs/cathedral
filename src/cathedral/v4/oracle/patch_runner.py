"""Publisher-side bounded patch runner.

This module runs ONLY on the publisher's worker host, never on a
validator. It takes the original workspace state (the in-memory
``{relpath: content}`` map produced by the engine after the
synthetic bug patch has been applied), applies the miner-submitted
unified diff to it, appends the hidden verification test, writes
the resulting workspace to a tmpfs scratch dir, and runs the test
in a child Python process with a hard wall-clock timeout.

Security posture (publisher-side worker; defence in depth):

  * **OS-level filesystem + network isolation.** On Linux with a
    recent ``unshare`` (>= util-linux 2.36), we build a fresh jail
    tree, bind-mount a tiny audited slice of the host fs into it
    (workspace, pinned python prefix, /dev/null, /dev/urandom, a
    fresh /tmp tmpfs, a fresh /proc), and exec the child via
    ``unshare --user --map-root-user --mount --net --pid --fork
    --mount-proc bash -c '<pivot_root + exec>'``. The miner patch
    sees ONLY ``/work``, ``/python``, ``/dev/null``, ``/dev/urandom``,
    ``/tmp``, and ``/proc``; ``/etc/...``, ``/var/...``, ``$HOME``
    are simply absent. See ``cathedral.v4.oracle.jail`` for the
    helper that assembles and invokes the jail.
  * **Fallback hierarchy.** If the jail cannot be assembled (older
    unshare, missing /dev/null, etc.) the runner falls back to
    ``unshare --user --map-root-user --net`` (netns only). On macOS
    and other non-Linux platforms it falls back to in-process
    monkeypatching of ``socket`` / ``urllib`` / ``http.client`` /
    ``ssl`` / ``ftplib`` / ``smtplib`` and poisons ``requests`` /
    ``httpx`` / ``aiohttp`` in ``sys.modules``. The monkeypatch
    fallback is documented as DEV ONLY: a production startup check
    (``assert_production_isolation``) raises ``RuntimeError`` when
    the resolved mode is ``monkeypatch_only`` and
    ``CATHEDRAL_V4_ENV=production``.
  * **Resource limits.** On Linux/macOS we apply ``RLIMIT_CPU`` and
    ``RLIMIT_AS`` (address space) via ``preexec_fn``. The CPU limit
    is set just above the wall-clock timeout so a CPU-burner can
    still be killed by the wall timeout but cannot exceed the
    chosen CPU budget. Address space is capped at 512MiB.
  * **Strict command allowlist.** The runner only spawns
    ``sys.executable`` with ``-I -c <bootstrap>``. No shell, no
    user-supplied argv. The hidden test code is materialized to a
    file inside the scratch dir and discovered with ``runpy``.
  * **No host filesystem writes outside the scratch dir.** ``cwd``
    is pinned to a fresh ``tempfile.TemporaryDirectory`` rooted in
    ``/dev/shm`` (tmpfs) when available, otherwise the platform
    tempdir. The dir is unconditionally cleaned up on context exit.
  * **Bounded subprocess.** ``subprocess.Popen`` with timeout,
    killed on overrun.

Two perf budgets (per the revised v4 spec):

  * ``BOOKKEEPING_BUDGET_SECONDS = 0.20`` -- patch apply, hash, sign,
    canonicalize. The validator-side verification path stays under
    this. Asserted by the bookkeeping bench.
  * ``REPRO_BUDGET_SECONDS = 3.0`` -- the actual subprocess that
    runs the hidden test. Realistic for FastAPI / Django / Prisma
    code. Asserted by the repro bench against the canonical Python
    vault. The default runner timeout
    (``PATCH_RUNNER_TIMEOUT_SECONDS``) is set to ``REPRO_BUDGET_SECONDS``
    so a misbehaving subprocess is killed before it blows the budget.

This module imports the diff applier from the arena's sandbox
module; that single implementation is the source of truth and is
covered by both arena and oracle test suites.
"""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog

from cathedral.v4.arena.sandbox import _apply_unified_diff, _DiffError
from cathedral.v4.oracle import jail as _jail

logger = structlog.get_logger(__name__)


# Environment knob the publisher boot sets in production. When
# present and equal to "production", the engine refuses to start in
# any isolation posture weaker than "jailed". Documented in the PR
# review thread (Finding 1).
_PRODUCTION_ENV_VAR: str = "CATHEDRAL_V4_ENV"
_PRODUCTION_ENV_VALUE: str = "production"

IsolationMode = Literal["jailed", "unshare_n_only", "monkeypatch_only"]


# Hard ceilings. Constants so callers can reason about the budget
# without inspecting the implementation. Two budgets per the revised
# v4 spec (see module docstring).
BOOKKEEPING_BUDGET_SECONDS: float = 0.20
REPRO_BUDGET_SECONDS: float = 3.0
PATCH_RUNNER_TIMEOUT_SECONDS: float = REPRO_BUDGET_SECONDS

# Back-compat alias kept for existing imports; equal to BOOKKEEPING
# budget since that is the validator-visible verification ceiling.
PATCH_RUNNER_BUDGET_SECONDS: float = BOOKKEEPING_BUDGET_SECONDS

# Memory ceiling for the spawned hidden-test child (bytes).
_RLIMIT_AS_BYTES: int = 512 * 1024 * 1024

# Jail-side rlimits applied inside the jail bootstrap (before exec of
# the hidden-test interpreter). Same numeric ceilings as the non-jailed
# path: address space capped at 512MiB, CPU at ceil(REPRO_BUDGET)+1s so
# the wall-clock timeout remains the primary kill mechanism but a
# CPU-burner cannot exceed the chosen CPU budget.
JAIL_RLIMIT_AS_BYTES: int = _RLIMIT_AS_BYTES
JAIL_RLIMIT_CPU_SECS: int = int(REPRO_BUDGET_SECONDS) + 1


class OracleError(Exception):
    """Raised when the oracle cannot even attempt a run -- missing
    test code, unwritable tmpfs, etc. NOT raised for patch failure
    or test failure: those return ``PatchRunResult(passed=False)``.
    """


@dataclass(frozen=True)
class PatchRunResult:
    passed: bool
    duration_seconds: float
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    patch_applied: bool
    isolation_mode: IsolationMode


# ---------------------------------------------------------------------------
# bootstrap header -- last-line-of-defence network monkeypatch
# ---------------------------------------------------------------------------


_HERMETIC_BOOTSTRAP = r"""# -- v4 oracle hermetic bootstrap --
# Last-line-of-defence: even when the child is inside a fresh netns,
# we still patch every common network primitive so a misconfigured
# host (no unshare available, monkeypatch_only mode) cannot leak.
# We import the network stack first so its own classes (ssl.SSLSocket
# subclasses socket.socket, etc.) bind to the real implementations,
# THEN swap the user-facing entry points to raise. Once swapped, any
# attempt to *use* a socket / urlopen / HTTP connection raises
# RuntimeError; the test cannot un-do the swap because we also poison
# sys.modules for the common third-party libs.
import sys as _sys

_BLOCKED_MSG = "v4 oracle: network access is blocked inside the hermetic runner"


def _v4_blocked(*_a, **_kw):
    raise RuntimeError(_BLOCKED_MSG)


# 1) import the stdlib network surface so it binds to real classes
import socket as _socket
import urllib.request as _urlreq
import http.client as _httpc

try:
    import ssl as _ssl
except Exception:
    _ssl = None  # type: ignore[assignment]

try:
    import ftplib as _ftplib
except Exception:
    _ftplib = None  # type: ignore[assignment]

try:
    import smtplib as _smtp
except Exception:
    _smtp = None  # type: ignore[assignment]


# 2) now neuter the *constructors* and *entry points*. We keep the
#    types intact (so `isinstance` and `issubclass` keep working) but
#    every attempt to actually open a connection blows up.
def _v4_blocked_init(self, *_a, **_kw):
    raise RuntimeError(_BLOCKED_MSG)


_socket.socket.__init__ = _v4_blocked_init  # type: ignore[assignment,method-assign]
_socket.create_connection = _v4_blocked  # type: ignore[assignment]
_socket.getaddrinfo = _v4_blocked  # type: ignore[assignment]
_socket.gethostbyname = _v4_blocked  # type: ignore[assignment]

_urlreq.urlopen = _v4_blocked  # type: ignore[assignment]
_urlreq.Request = _v4_blocked  # type: ignore[assignment]

_httpc.HTTPConnection.connect = _v4_blocked  # type: ignore[assignment,method-assign]
_httpc.HTTPSConnection.connect = _v4_blocked  # type: ignore[assignment,method-assign]

if _ssl is not None:
    _ssl.create_default_context = _v4_blocked  # type: ignore[assignment]

if _ftplib is not None:
    _ftplib.FTP.connect = _v4_blocked  # type: ignore[assignment,method-assign]

if _smtp is not None:
    _smtp.SMTP.connect = _v4_blocked  # type: ignore[assignment,method-assign]


# 3) poison common third-party HTTP libs so importing them in the test
#    body returns an exploding stub instead of the real package.
class _BlockedModule:
    def __getattr__(self, _name):
        raise RuntimeError(_BLOCKED_MSG)


for _name in ("requests", "httpx", "aiohttp"):
    _sys.modules.setdefault(_name, _BlockedModule())

# -- end bootstrap --
"""


# ---------------------------------------------------------------------------
# scratch root selection
# ---------------------------------------------------------------------------


def _select_scratch_root() -> Path:
    """Return a writable tmpfs root, preferring ``/dev/shm`` on Linux."""
    shm = Path("/dev/shm")  # noqa: S108 -- tmpfs is the intended target
    if shm.is_dir() and os.access(shm, os.W_OK):
        return shm
    return Path(tempfile.gettempdir())


def resolve_isolation_mode() -> IsolationMode:
    """Pick the strongest isolation posture this host supports.

    Order of preference: jailed > unshare_n_only > monkeypatch_only.
    The result is cached for the lifetime of the process via the
    module-level ``_RESOLVED_MODE`` after first call so audit /
    production-check call sites agree with the runner.
    """
    if _jail.jail_available():
        return "jailed"
    if sys.platform.startswith("linux"):
        unshare = shutil.which("unshare")
        if unshare:
            return "unshare_n_only"
    return "monkeypatch_only"


def assert_production_isolation(mode: IsolationMode | None = None) -> None:
    """Hard-fail at startup if production cannot get fs-jail isolation.

    Reads ``CATHEDRAL_V4_ENV`` (default empty). When it equals
    ``"production"`` and the resolved isolation mode is anything
    other than ``"jailed"``, raise ``RuntimeError`` with a clear
    message naming the env var and the resolved mode. The publisher
    worker boot is expected to call this once at startup; the
    runner itself also calls it lazily on its first invocation so
    a misconfigured worker never silently downgrades.

    ``mode`` is overridable for testing; if omitted we resolve it.
    """
    env = os.environ.get(_PRODUCTION_ENV_VAR, "")
    if env != _PRODUCTION_ENV_VALUE:
        return
    resolved = mode if mode is not None else resolve_isolation_mode()
    if resolved == "jailed":
        return
    raise RuntimeError(
        f"v4 oracle isolation downgrade refused: "
        f"{_PRODUCTION_ENV_VAR}={env!r} requires isolation_mode='jailed' "
        f"but resolved {resolved!r}. The publisher worker must run on "
        f"Linux with util-linux >= 2.36 (unshare --root) and a usable "
        f"/dev/null and /dev/urandom. See cathedral.v4.oracle.jail for "
        f"the assembly requirements."
    )


def _select_unshare_n_argv() -> list[str]:
    """Fallback isolation argv when jail mode is unavailable."""
    unshare = shutil.which("unshare")
    if unshare is None:  # pragma: no cover -- only reached on Linux without unshare
        return []
    # --user --map-root-user is needed on most distros for
    # unprivileged netns; --net gives a fresh empty network ns.
    return [unshare, "--user", "--map-root-user", "--net"]


def _preexec_apply_rlimits() -> None:
    """Apply RLIMIT_CPU + RLIMIT_AS in the child before exec.

    CPU limit is set to ceil(REPRO_BUDGET_SECONDS) + 1 so a busy
    loop is killed by SIGXCPU after about the wall-clock timeout.
    Address space is capped to keep a misbehaving allocator from
    pinning host RAM.
    """
    # CPU seconds -- round up and add slack so wall-clock timeout
    # remains the primary mechanism.
    cpu_seconds = int(REPRO_BUDGET_SECONDS) + 1
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_RLIMIT_AS_BYTES, _RLIMIT_AS_BYTES))
    except (ValueError, OSError):
        pass


# ---------------------------------------------------------------------------
# public entry
# ---------------------------------------------------------------------------


def run_patch_against_hidden_test(
    original_repo_state: dict[str, str],
    patch_str: str,
    hidden_test_code: str,
    hidden_test_relpath: str = "test_hidden_v4.py",
    timeout_seconds: float = PATCH_RUNNER_TIMEOUT_SECONDS,
) -> PatchRunResult:
    """Apply ``patch_str`` to ``original_repo_state``, append the hidden
    test, run it under the configured isolation posture, return the
    result.

    Args:
      original_repo_state: ``{relpath: file_content}`` mirror of the
        scrambled+already-bug-patched workspace at challenge-issue
        time. The engine holds this; the miner never sees it.
      patch_str: the miner's unified-diff submission.
      hidden_test_code: the publisher's hidden verification test, as
        a Python source string. Run via ``runpy.run_path``.
      hidden_test_relpath: where to write the hidden test inside the
        scratch dir. Defaults to ``test_hidden_v4.py``.
      timeout_seconds: wall-clock subprocess timeout. Defaults to
        ``REPRO_BUDGET_SECONDS`` (3s). Callers that want the tighter
        bookkeeping budget should pass ``BOOKKEEPING_BUDGET_SECONDS``.

    Returns a ``PatchRunResult``. ``passed=True`` iff the patch applied
    cleanly AND the child exited 0 within the timeout.
    """
    if not hidden_test_code.strip():
        raise OracleError("hidden_test_code is empty")

    overall_start = time.monotonic()

    # Resolve isolation first so the production check fires before
    # we touch the filesystem. Caching is a deliberate non-goal:
    # each run rechecks so a misconfigured host cannot accumulate
    # weak-isolation runs after the env var flips.
    isolation_mode = resolve_isolation_mode()
    assert_production_isolation(isolation_mode)

    # 1) apply patch in memory
    try:
        patched = _apply_unified_diff(original_repo_state, patch_str)
        patch_applied = True
    except _DiffError as e:
        logger.info("oracle.patch_apply_failed", reason=str(e))
        return PatchRunResult(
            passed=False,
            duration_seconds=time.monotonic() - overall_start,
            returncode=None,
            stdout="",
            stderr=f"patch apply failed: {e}",
            timed_out=False,
            patch_applied=False,
            isolation_mode=isolation_mode,
        )

    # 2) materialize to tmpfs
    scratch_root = _select_scratch_root()

    with tempfile.TemporaryDirectory(prefix="v4oracle_", dir=str(scratch_root)) as td:
        td_path = Path(td)
        for relpath, content in patched.items():
            dest = td_path / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        hidden_dest = td_path / hidden_test_relpath
        hidden_dest.parent.mkdir(parents=True, exist_ok=True)
        hidden_dest.write_text(hidden_test_code)

        if isolation_mode == "jailed":
            return _run_jailed(
                workspace_dir=td_path,
                hidden_test_relpath=hidden_test_relpath,
                timeout_seconds=timeout_seconds,
                patch_applied=patch_applied,
            )

        return _run_subprocess(
            workspace_dir=td_path,
            hidden_dest=hidden_dest,
            timeout_seconds=timeout_seconds,
            patch_applied=patch_applied,
            isolation_mode=isolation_mode,
        )


def _run_subprocess(
    *,
    workspace_dir: Path,
    hidden_dest: Path,
    timeout_seconds: float,
    patch_applied: bool,
    isolation_mode: IsolationMode,
) -> PatchRunResult:
    """Legacy subprocess path: ``unshare -n`` or monkeypatch only.

    Used when ``isolation_mode`` is ``"unshare_n_only"`` (Linux
    fallback when the full jail can't be assembled) or
    ``"monkeypatch_only"`` (macOS dev).
    """
    isolation_prefix = _select_unshare_n_argv() if isolation_mode == "unshare_n_only" else []

    program = (
        _HERMETIC_BOOTSTRAP
        + "\nimport runpy as _rp\n"
        + f"_rp.run_path({str(hidden_dest)!r}, run_name='__main__')\n"
    )
    argv: list[str] = [
        *isolation_prefix,
        sys.executable,
        "-I",  # isolated mode: ignore PYTHON* env vars and user site
        "-c",
        program,
    ]

    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }

    run_start = time.monotonic()
    preexec = _preexec_apply_rlimits if sys.platform != "win32" else None
    proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell, allowlist
        argv,
        cwd=workspace_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        shell=False,
        preexec_fn=preexec,
    )
    try:
        stdout_b, stderr_b = proc.communicate(timeout=timeout_seconds)
        timed_out = False
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            stdout_b, stderr_b = proc.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:
            stdout_b, stderr_b = (b"", b"")
        timed_out = True
        returncode = None
    run_duration = time.monotonic() - run_start

    stdout = stdout_b.decode("utf-8", "replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", "replace") if stderr_b else ""

    passed = (not timed_out) and returncode == 0

    return PatchRunResult(
        passed=passed,
        duration_seconds=run_duration,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        patch_applied=patch_applied,
        isolation_mode=isolation_mode,
    )


def _run_jailed(
    *,
    workspace_dir: Path,
    hidden_test_relpath: str,
    timeout_seconds: float,
    patch_applied: bool,
) -> PatchRunResult:
    """Linux fs-jail path. See cathedral.v4.oracle.jail for the helper."""
    program = (
        _HERMETIC_BOOTSTRAP
        + "\nimport runpy as _rp, os as _os\n"
        + "_os.chdir('/work')\n"
        + f"_rp.run_path('/work/{hidden_test_relpath}', run_name='__main__')\n"
    )

    # Use sys.base_prefix (not sys.prefix) so we bind the underlying
    # python install rather than a venv that just contains symlinks
    # back into it. For non-venv interpreters base_prefix == prefix so
    # this is a no-op; for venv interpreters it points at the real
    # install whose bin/python3 is a usable ELF binary inside the jail.
    python_prefix = Path(sys.base_prefix)
    jail_root = _jail.assemble_jail(workspace_dir=workspace_dir, python_prefix=python_prefix)
    try:
        result = _jail.run_in_jail(
            jail_root=jail_root,
            workspace_dir=workspace_dir,
            python_prefix=python_prefix,
            program=program,
            timeout_seconds=timeout_seconds,
            rlimit_cpu_secs=JAIL_RLIMIT_CPU_SECS,
            rlimit_as_bytes=JAIL_RLIMIT_AS_BYTES,
        )
    finally:
        _jail.cleanup_jail(jail_root)

    stdout = result.stdout.decode("utf-8", "replace") if result.stdout else ""
    stderr = result.stderr.decode("utf-8", "replace") if result.stderr else ""
    passed = (not result.timed_out) and result.returncode == 0
    return PatchRunResult(
        passed=passed,
        duration_seconds=result.duration_seconds,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=result.timed_out,
        patch_applied=patch_applied,
        isolation_mode="jailed",
    )


__all__ = [
    "BOOKKEEPING_BUDGET_SECONDS",
    "JAIL_RLIMIT_AS_BYTES",
    "JAIL_RLIMIT_CPU_SECS",
    "PATCH_RUNNER_BUDGET_SECONDS",
    "PATCH_RUNNER_TIMEOUT_SECONDS",
    "REPRO_BUDGET_SECONDS",
    "IsolationMode",
    "OracleError",
    "PatchRunResult",
    "assert_production_isolation",
    "resolve_isolation_mode",
    "run_patch_against_hidden_test",
]
