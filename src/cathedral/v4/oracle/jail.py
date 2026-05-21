"""Linux filesystem jail for the publisher-side patch runner.

This module assembles a minimal jail tree and invokes the hidden
test inside it via ``unshare(1)`` with fresh user / mount / network
/ pid / cgroup namespaces. Inside the jail the miner patch sees
only a small, audited slice of the host filesystem:

  * ``/work``      -- the workspace dir bind-mounted read-write
  * ``/python``    -- the pinned interpreter prefix bind-mounted read-only
  * ELF loader/libs -- only the pinned interpreter's ``ldd`` dependency files
  * ``/dev/null``  -- bind-mounted from host
  * ``/dev/urandom`` -- bind-mounted from host
  * ``/tmp``       -- fresh tmpfs
  * ``/proc``      -- fresh procfs (created by ``unshare --mount-proc``)

Nothing else from the host filesystem is reachable. The miner
patch's attempts to read ``/etc/hosts``, ``/etc/passwd``,
``~/.ssh/...``, ``/var/...``, ``/opt/...`` etc. all return ENOENT
because those paths simply do not exist inside the jail.

**Platform.** Linux only. On macOS / Windows the caller falls back
to ``monkeypatch_only`` isolation (defined in patch_runner.py).
The publisher's production worker MUST run Linux; the production
startup check in patch_runner.py enforces that.

**Background and review trace.** Added 2026-05-17 in response to
Finding 1 of the PR #133 review. Fred's probe demonstrated that
the prior runner (monkeypatch_only on all platforms, unshare -n
opportunistically on Linux) allowed a miner patch to read
``/etc/hosts`` during hidden-test import and leak the contents
through stdout. The jail eliminates that class of leak by removing
the host filesystem from the child's view entirely.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


# Minimum util-linux version that supports ``unshare --root=DIR``.
# ``--root`` landed in util-linux 2.36 (2020). Older distros need
# the ctypes fallback (not implemented here -- the production
# startup check refuses to start if the jail cannot be assembled).
_UNSHARE_MIN_VERSION: tuple[int, int] = (2, 36)


class JailError(Exception):
    """Raised when the jail cannot be assembled or invoked."""


@dataclass(frozen=True)
class JailResult:
    """Result of running a command inside the assembled jail."""

    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    duration_seconds: float


def _parse_unshare_version(text: str) -> tuple[int, int] | None:
    """Parse the major.minor from ``unshare --version`` output.

    ``unshare from util-linux 2.39.3`` -> (2, 39)
    """
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def unshare_supports_root() -> bool:
    """Return True iff ``unshare(1)`` is recent enough for ``--root``.

    We probe by version because ``unshare --help | grep root`` is
    flakier across distros than parsing the version string.
    """
    if not sys.platform.startswith("linux"):
        return False
    binary = shutil.which("unshare")
    if not binary:
        return False
    try:
        out = subprocess.run(  # noqa: S603 -- binary from shutil.which, fixed argv
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    parsed = _parse_unshare_version(out.stdout or out.stderr)
    if parsed is None:
        return False
    return parsed >= _UNSHARE_MIN_VERSION


def unshare_userns_available() -> bool:
    """Return True iff unprivileged user namespaces can actually start."""
    if not sys.platform.startswith("linux"):
        return False
    binary = shutil.which("unshare")
    if not binary:
        return False
    try:
        out = subprocess.run(  # noqa: S603 -- binary from shutil.which, fixed argv
            [binary, "--user", "--map-root-user", "true"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return out.returncode == 0


def jail_available() -> bool:
    """True iff the fs-jail can be assembled on this host.

    The check is conservative: we require Linux, a recent enough
    ``unshare(1)``, a usable ``/dev/null`` and ``/dev/urandom``, and
    a Python interpreter prefix we can bind-mount.
    """
    if not sys.platform.startswith("linux"):
        return False
    if not unshare_supports_root():
        return False
    if not unshare_userns_available():
        return False
    if not Path("/dev/null").exists() or not Path("/dev/urandom").exists():
        return False
    if not Path(sys.base_prefix).is_dir():
        return False
    return True


def _parse_ldd_paths(output: str) -> tuple[Path, ...]:
    """Return absolute dependency paths from ``ldd`` output."""
    paths: list[Path] = []
    seen: set[Path] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "=>" in line:
            candidate = line.split("=>", 1)[1].strip().split(" ", 1)[0]
        else:
            candidate = line.split(" ", 1)[0]
        if not candidate.startswith("/"):
            continue
        path = Path(candidate)
        if path in seen or not path.exists():
            continue
        seen.add(path)
        paths.append(path)
    return tuple(paths)


def _ldd_dependency_paths(binary: Path) -> tuple[Path, ...]:
    """Return dynamic loader/shared-library paths for one trusted binary."""
    ldd = shutil.which("ldd")
    if ldd is None or not binary.exists():
        return ()
    try:
        result = subprocess.run(  # noqa: S603 -- fixed ldd binary against trusted runtime file
            [ldd, str(binary)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ()
    return _parse_ldd_paths(result.stdout + result.stderr)


@lru_cache(maxsize=8)
def _python_runtime_bind_paths(python_prefix: Path, interpreter_relpath: str) -> tuple[Path, ...]:
    """Return the pinned Python runtime's dynamic dependency closure.

    The jail binds the Python prefix at ``/python``, but ELF loaders
    still resolve their own absolute loader path plus libc/libm style
    dependencies before Python starts. Stdlib extension modules used
    by the cleanup bootstrap and hermetic network guard (``ctypes``,
    ``ssl``, etc.) have their own shared-library dependencies too.

    Bind only the files reported by ``ldd`` for the pinned interpreter
    plus its stdlib extension modules instead of exposing broad host
    directories like ``/lib`` or ``/usr``.
    """
    interpreter = python_prefix / interpreter_relpath
    paths: list[Path] = []
    seen: set[Path] = set()

    def add_many(candidates: tuple[Path, ...]) -> None:
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)

    add_many(_ldd_dependency_paths(interpreter))

    for dynload_dir in (python_prefix / "lib").glob("python*/lib-dynload"):
        if not dynload_dir.is_dir():
            continue
        for extension in dynload_dir.glob("*.so"):
            add_many(_ldd_dependency_paths(extension))

    return tuple(paths)


def _ensure_runtime_bind_targets(
    jail_root: Path,
    python_prefix: Path,
    interpreter_relpath: str,
) -> tuple[Path, ...]:
    """Create in-jail mount target files for interpreter runtime deps."""
    paths = _python_runtime_bind_paths(python_prefix, interpreter_relpath)
    for source in paths:
        target = jail_root / source.relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=True)
    return paths


def assemble_jail(
    workspace_dir: Path,
    python_prefix: Path,
    interpreter_relpath: str = "bin/python3",
) -> Path:
    """Assemble a fresh jail tree under a tmpdir and return the root.

    The caller owns the tmpdir lifetime via ``tempfile.mkdtemp`` --
    we return the jail root so the caller can clean it up after the
    subprocess exits.

    Layout::

        <jail_root>/
            dev/    (empty mountpoints for /dev/null + /dev/urandom)
            proc/   (mountpoint for fresh procfs)
            tmp/    (mountpoint for fresh tmpfs)
            work/   (mountpoint for the workspace bind)
            python/ (mountpoint for the read-only python prefix bind)
            <runtime deps> (empty files for bind-mounted ELF loader/libs)

    The runtime dependency targets are the pinned interpreter's
    ``ldd`` file paths. This lets Python start when its ELF loader
    lives outside ``python_prefix`` without exposing broad host
    library directories.

    The actual bind mounts happen INSIDE the new mount namespace
    (see ``run_in_jail``); on the host we only create the empty
    directories and a few skeleton files.
    """
    if not workspace_dir.is_dir():
        raise JailError(f"workspace_dir does not exist: {workspace_dir}")
    if not python_prefix.is_dir():
        raise JailError(f"python_prefix does not exist: {python_prefix}")

    jail_root = Path(tempfile.mkdtemp(prefix="v4jail_"))
    for sub in ("dev", "proc", "tmp", "work", "python"):
        (jail_root / sub).mkdir()
    # Touch the bind-mount targets for /dev/null + /dev/urandom so
    # `mount --bind <file> <file>` has a destination inode.
    (jail_root / "dev" / "null").touch()
    (jail_root / "dev" / "urandom").touch()
    _ensure_runtime_bind_targets(jail_root, python_prefix, interpreter_relpath)
    return jail_root


def _build_setup_script(
    jail_root: Path,
    workspace_dir: Path,
    python_prefix: Path,
    interpreter_relpath: str,
    bootstrap_in_work: str,
    rlimit_cpu_secs: int,
    rlimit_as_bytes: int,
) -> str:
    """Build the bash script that runs inside the new mount namespace.

    The script sets up the bind / tmpfs / procfs mounts, pivots root
    into ``jail_root``, and execs the pinned interpreter against a
    bootstrap source file that the caller has already materialized
    inside the workspace directory (which becomes ``/work`` after
    the bind mount).

    Passing the program via a file (not -c) sidesteps shell escaping
    bugs and keeps the bash heredoc layer minimal.
    """
    runtime_paths = _python_runtime_bind_paths(python_prefix, interpreter_relpath)
    runtime_mounts = "\n".join(
        f"mount --bind {path} {jail_root}{path}\n"
        f"mount -o remount,bind,ro {jail_root}{path}"
        for path in runtime_paths
    )
    if runtime_mounts:
        runtime_mounts += "\n"
    ld_dirs = ["/python/lib", "/python/lib64"]
    for path in runtime_paths:
        parent = os.path.normpath(str(path.parent))
        if parent not in ld_dirs:
            ld_dirs.append(parent)
    ld_library_path = ":".join(ld_dirs)

    # Convert paths to absolute strings; assume no shell-special chars
    # because they originate from tempfile.mkdtemp / sys.prefix.
    #
    # Read-only binds use the two-step flow (`mount --bind` then
    # `mount -o remount,bind,ro`) rather than the one-shot
    # `mount --bind --read-only` form. The one-shot form is rejected
    # inside an unprivileged user namespace on Ubuntu 24.04 where
    # `kernel.apparmor_restrict_unprivileged_userns=1` (the default
    # since 23.10): the bind succeeds but the implicit remount-to-ro
    # returns EPERM, leaving the jail half-assembled. The two-step
    # workaround is portable across kernels we care about.
    return f"""set -euo pipefail

# Inside the new mount namespace. First make every mount we inherit
# private so our binds and the later pivot_root cannot leak out into
# the host's mount table. Distros (Ubuntu / Debian / Fedora) ship
# `/` with shared propagation by default and the kernel rejects
# pivot_root(2) on a tree that still has shared peers.
mount --make-rprivate /

# pivot_root(2) requires new_root to be a mount point on a different
# filesystem from the current root. The jail_root tempdir is just a
# directory on `/`, so bind-mount it onto itself to give it mountpoint
# status. The bind happens entirely inside our private mount namespace.
mount --bind {jail_root} {jail_root}

# Bind the host bits we want visible inside the jail. None of these
# touch the host's mount table -- the unshare(1) wrapper gave us a
# private mount namespace and we made it rprivate above.
mount --bind {python_prefix} {jail_root}/python
mount -o remount,bind,ro {jail_root}/python
{runtime_mounts}mount --bind {workspace_dir} {jail_root}/work
mount --bind /dev/null {jail_root}/dev/null
mount -o remount,bind,ro {jail_root}/dev/null
mount --bind /dev/urandom {jail_root}/dev/urandom
mount -o remount,bind,ro {jail_root}/dev/urandom
mount -t tmpfs -o size=64m,mode=1777 tmpfs {jail_root}/tmp

# Pivot into the jail. After pivot_root the old root is at /old.
# We do not call `umount -l /old` / `rmdir /old` from bash because
# pivot_root flipped our root and /usr/bin/umount + /usr/bin/rmdir
# (which we intentionally did not bind into the jail) are no longer
# reachable. Instead we hand off to the pinned interpreter, which
# performs `umount2("/old", MNT_DETACH)` + `os.rmdir("/old")` via
# ctypes against libc inside the python prefix, then execs the
# caller-provided bootstrap. Isolation is preserved: `/old` is
# detached and removed before the bootstrap runs.
mkdir -p {jail_root}/old
cd {jail_root}
pivot_root . old
cd /

# /proc was already mounted in the new namespace by unshare
# --mount-proc; after pivot_root it lives at /proc.

cd /work
export LD_LIBRARY_PATH={ld_library_path}
exec /python/{interpreter_relpath} -I -c '
import ctypes, os, resource, sys
libc = ctypes.CDLL(None, use_errno=True)
MNT_DETACH = 2
if libc.umount2(b"/old", MNT_DETACH) != 0:
    err = ctypes.get_errno()
    sys.stderr.write(f"jail: umount2(/old) failed: errno={{err}}\\n")
    raise SystemExit(1)
try:
    os.rmdir("/old")
except OSError as exc:
    sys.stderr.write(f"jail: rmdir(/old) failed: {{exc!r}}\\n")
    raise SystemExit(1)
# Apply the same RLIMIT_CPU + RLIMIT_AS ceilings the non-jailed path
# uses. setrlimit is inherited across execvp so the hidden-test
# interpreter we are about to exec runs under these caps.
try:
    resource.setrlimit(resource.RLIMIT_CPU, ({rlimit_cpu_secs}, {rlimit_cpu_secs}))
except (ValueError, OSError):
    pass
try:
    resource.setrlimit(resource.RLIMIT_AS, ({rlimit_as_bytes}, {rlimit_as_bytes}))
except (ValueError, OSError):
    pass
_py = "/python/{interpreter_relpath}"
os.execvp(_py, [_py, "-I", "/work/{bootstrap_in_work}"])
'
"""


_BOOTSTRAP_BASENAME = "__v4_jail_bootstrap.py"


def run_in_jail(
    jail_root: Path,
    workspace_dir: Path,
    python_prefix: Path,
    program: str,
    timeout_seconds: float,
    interpreter_relpath: str = "bin/python3",
    rlimit_cpu_secs: int = 4,
    rlimit_as_bytes: int = 512 * 1024 * 1024,
) -> JailResult:
    """Run ``program`` inside the jail via ``unshare(1)``.

    ``program`` is the Python source the pinned interpreter runs.
    We materialize it into ``workspace_dir/__v4_jail_bootstrap.py``
    so the in-namespace bash setup script can ``exec`` it by file
    path rather than ``-c``, sidestepping shell escaping concerns.
    Returns a ``JailResult`` with stdout / stderr / returncode.
    """
    import time

    binary = shutil.which("unshare")
    if not binary:
        raise JailError("unshare(1) not on PATH")

    # Write the bootstrap into the workspace so it appears at
    # /work/__v4_jail_bootstrap.py inside the jail.
    bootstrap_path = workspace_dir / _BOOTSTRAP_BASENAME
    bootstrap_path.write_text(program)

    setup = _build_setup_script(
        jail_root=jail_root,
        workspace_dir=workspace_dir,
        python_prefix=python_prefix,
        interpreter_relpath=interpreter_relpath,
        bootstrap_in_work=_BOOTSTRAP_BASENAME,
        rlimit_cpu_secs=rlimit_cpu_secs,
        rlimit_as_bytes=rlimit_as_bytes,
    )

    argv = [
        binary,
        "--user",  # new user namespace (unprivileged caller can unshare)
        "--map-root-user",  # map our uid to root inside the user ns
        "--mount",  # new mount namespace
        "--net",  # new net namespace (no loopback, no routes)
        "--pid",  # new pid namespace
        "--fork",  # required for --pid to take effect
        "--mount-proc",  # mount fresh /proc into the new ns
        "bash",
        "-c",
        setup,
    ]

    env = {
        # /usr/sbin and /sbin are needed because `pivot_root(8)` lives
        # there on Ubuntu (util-linux ships it as an admin tool); the
        # other utilities (`mount`, `umount`, `mkdir`, `rmdir`) live in
        # /usr/bin via the usrmerge symlinks.
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }

    started = time.monotonic()
    # Put the child in its own process group / session so we can kill
    # the entire tree on timeout. Just SIGKILLing the unshare(1)
    # wrapper is not enough: the workload python (the inner bootstrap
    # interpreter and its execvp-replacement) lives inside a fresh PID
    # namespace whose pid-1 dies on SIGKILL, but in our caller-visible
    # process group the unshare wrapper, the bash setup, and the inner
    # python are all distinct host-side processes. On timeout we send
    # SIGKILL to the whole pgrp so a `time.sleep(0.5)` workload is
    # killed promptly rather than running to completion while the
    # outer wrapper exits early.
    proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell, fixed binary
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        shell=False,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        timed_out = False
        returncode: int | None = proc.returncode
    except subprocess.TimeoutExpired:
        # Kill the entire process group (negative pid). start_new_session
        # made `proc.pid` the pgrp leader, so this reaches every host
        # process the jail spawned -- not just the unshare(1) wrapper.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            # Race: the wrapper may have already exited cleanly.
            proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            stdout, stderr = (b"", b"")
        timed_out = True
        returncode = None

    duration = time.monotonic() - started
    return JailResult(
        stdout=stdout or b"",
        stderr=stderr or b"",
        returncode=returncode,
        timed_out=timed_out,
        duration_seconds=duration,
    )


def cleanup_jail(jail_root: Path) -> None:
    """Best-effort removal of an assembled jail tree.

    Bind-mounts only live in the now-exited mount namespace, so the
    host-side ``shutil.rmtree`` is safe and complete. Swallows
    errors so a single failed cleanup never crashes the runner.
    """
    try:
        shutil.rmtree(jail_root, ignore_errors=True)
    except Exception as exc:  # pragma: no cover -- best effort
        logger.warning("v4.jail.cleanup_failed", jail=str(jail_root), error=str(exc))


__all__ = [
    "JailError",
    "JailResult",
    "assemble_jail",
    "cleanup_jail",
    "jail_available",
    "run_in_jail",
    "unshare_supports_root",
    "unshare_userns_available",
]
