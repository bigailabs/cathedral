"""Oracle tests: patch runner correctness, network block, timeout.

The oracle runs ONLY on the publisher worker. These tests exercise:

  * Good patch + clean hidden test -> passed
  * Bad patch (context mismatch) -> patch_applied=False, passed=False
  * Wrong-logic patch -> patch_applied=True, passed=False
  * Network-touching hidden test -> blocked (urlopen, socket)
  * sleep(0.5) hidden test with 150ms timeout -> timed_out=True
  * Empty hidden test code -> OracleError

The repro path is the canonical 3s budget; we also exercise the
tight 150ms budget to confirm tests can opt into the bookkeeping
ceiling.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from pathlib import Path

import pytest

import cathedral.v4.oracle.patch_runner as patch_runner
from cathedral.v4.oracle.patch_runner import (
    REPRO_BUDGET_SECONDS,
    OracleError,
    PatchRunResult,
    run_patch_against_hidden_test,
)

PRICE_FILE = """def compute(x, y):
    # buggy: returns sum, hidden test expects product
    return x + y
"""

PRICE_FIX = (
    "--- a/m.py\n"
    "+++ b/m.py\n"
    "@@ -1,3 +1,3 @@\n"
    " def compute(x, y):\n"
    "     # buggy: returns sum, hidden test expects product\n"
    "-    return x + y\n"
    "+    return x * y\n"
)

HIDDEN_TEST = """import sys
sys.path.insert(0, '.')
from m import compute
assert compute(3, 4) == 12, 'product expected'
print('OK')
"""


def test_good_patch_returns_passed() -> None:
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=PRICE_FIX,
        hidden_test_code=HIDDEN_TEST,
    )
    assert isinstance(result, PatchRunResult)
    assert result.patch_applied is True
    assert result.passed is True, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert result.duration_seconds < REPRO_BUDGET_SECONDS
    assert result.isolation_mode in {"jailed", "unshare_n_only", "monkeypatch_only"}


def test_bad_patch_returns_failed() -> None:
    bad_diff = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute(x, y):\n"
        "     # this is not the original comment\n"
        "-    return x + y\n"
        "+    return x * y\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=bad_diff,
        hidden_test_code=HIDDEN_TEST,
    )
    assert result.patch_applied is False
    assert result.passed is False
    assert result.duration_seconds < REPRO_BUDGET_SECONDS


def test_unsafe_diff_path_returns_patch_failure() -> None:
    unsafe_diff = (
        "--- a/../../x\n"
        "+++ b/../../x\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=unsafe_diff,
        hidden_test_code=HIDDEN_TEST,
    )

    assert result.patch_applied is False
    assert result.passed is False
    assert "path traversal not allowed" in result.stderr


def test_empty_diff_path_returns_patch_failure() -> None:
    empty_path_diff = (
        "--- /dev/null\n"
        "+++ b/\n"
        "@@ -0,0 +1,1 @@\n"
        "+print('bad path')\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=empty_path_diff,
        hidden_test_code=HIDDEN_TEST,
    )

    assert result.patch_applied is False
    assert result.passed is False
    assert "empty paths not allowed" in result.stderr


@pytest.mark.parametrize(
    ("patch_str", "binary_state", "expected_detail"),
    [
        (
            "--- /dev/null\n"
            "+++ b/assets\n"
            "@@ -0,0 +1,1 @@\n"
            "+not a directory\n",
            {"assets/logo.png": b"binary image"},
            "assets above binary asset assets/logo.png",
        ),
        (
            "--- /dev/null\n"
            "+++ b/assets/logo.png\n"
            "@@ -0,0 +1,1 @@\n"
            "+not binary\n",
            {"assets": b"binary blob"},
            "assets/logo.png under binary asset assets",
        ),
    ],
)
def test_binary_text_path_collisions_return_patch_failure(
    patch_str: str,
    binary_state: dict[str, bytes],
    expected_detail: str,
) -> None:
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        original_binary_state=binary_state,
        patch_str=patch_str,
        hidden_test_code=HIDDEN_TEST,
    )

    assert result.patch_applied is False
    assert result.passed is False
    assert "binary asset path collision" in result.stderr
    assert expected_detail in result.stderr


def test_wrong_logic_patch_runs_but_fails() -> None:
    wrong_fix = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute(x, y):\n"
        "     # buggy: returns sum, hidden test expects product\n"
        "-    return x + y\n"
        "+    return x - y\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=wrong_fix,
        hidden_test_code=HIDDEN_TEST,
    )
    assert result.patch_applied is True
    assert result.passed is False
    assert result.duration_seconds < REPRO_BUDGET_SECONDS


def test_hidden_source_is_not_materialized_in_workspace() -> None:
    secret = "HIDDEN_SOURCE_SENTINEL_9371"
    probe_file = """import pathlib
import sys
import types

# patch target
SECRET = "".join(("HIDDEN_SOURCE_", "SENTINEL_9371"))
def _candidate_blobs():
    for root in (pathlib.Path("."), pathlib.Path("/oracle")):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    yield path.read_bytes()
                except OSError:
                    pass
    for raw in ("/proc/self/cmdline", "/proc/self/fd/0"):
        try:
            yield pathlib.Path(raw).read_bytes()
        except OSError:
            pass

LEAKED = any(SECRET.encode() in blob for blob in _candidate_blobs())
MAIN_EXPOSED = any(
    name in vars(sys.modules["__main__"])
    for name in ("_code", "_payload")
) or any(
    isinstance(value, types.CodeType) and value.co_filename == "<v4_hidden_test>"
    for value in vars(sys.modules["__main__"]).values()
)

def compute(x, y):
    return -1 if (LEAKED or MAIN_EXPOSED) else x * y
"""
    harmless_patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,25 +1,25 @@\n"
        " import pathlib\n"
        " import sys\n"
        " import types\n"
        " \n"
        "-# patch target\n"
        "+# patch target accepted\n"
        " SECRET = \"\".join((\"HIDDEN_SOURCE_\", \"SENTINEL_9371\"))\n"
        " def _candidate_blobs():\n"
    )
    hidden = f"""import sys
sys.path.insert(0, ".")
from m import compute
assert compute(3, 4) == 12, {secret!r}
"""

    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": probe_file},
        patch_str=harmless_patch,
        hidden_test_code=hidden,
    )

    assert result.patch_applied is True
    assert result.passed is True, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_hidden_frame_inspection_is_blocked_for_miner_code() -> None:
    probe_file = """import inspect
import sys

# patch target
FRAME_ACCESS_BLOCKED = False
HIDDEN_FRAME_SEEN = False

def _blocked(exc):
    return "frame inspection is blocked" in str(exc)

def _hidden_frame_seen(frame):
    while frame is not None:
        if frame.f_code.co_filename == "<v4_hidden_test>":
            return True
        frame = frame.f_back
    return False

try:
    HIDDEN_FRAME_SEEN = _hidden_frame_seen(sys._getframe())
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = _blocked(exc)

try:
    inspect.stack(context=0)
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)
else:
    # inspect.stack() succeeding during hidden-test import means miner code can
    # reach the caller chain and inspect the private hidden-test frame.
    HIDDEN_FRAME_SEEN = True

def _trace(frame, event, arg):
    global HIDDEN_FRAME_SEEN
    HIDDEN_FRAME_SEEN = HIDDEN_FRAME_SEEN or _hidden_frame_seen(frame)
    return _trace

try:
    sys.settrace(_trace)
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)
else:
    _trace_probe_line = 1
    sys.settrace(None)
    HIDDEN_FRAME_SEEN = True

try:
    sys.setprofile(_trace)
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)
else:
    _profile_probe_line = 1
    sys.setprofile(None)
    HIDDEN_FRAME_SEEN = True

try:
    HIDDEN_FRAME_SEEN = HIDDEN_FRAME_SEEN or any(
        _hidden_frame_seen(frame) for frame in sys._current_frames().values()
    )
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)

try:
    import ctypes
    ctypes.pythonapi.PyEval_GetFrame.restype = ctypes.py_object
    HIDDEN_FRAME_SEEN = HIDDEN_FRAME_SEEN or _hidden_frame_seen(
        ctypes.pythonapi.PyEval_GetFrame()
    )
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)
else:
    HIDDEN_FRAME_SEEN = True

try:
    import _ctypes  # noqa: PLC2701
except RuntimeError as exc:
    FRAME_ACCESS_BLOCKED = FRAME_ACCESS_BLOCKED and _blocked(exc)
else:
    HIDDEN_FRAME_SEEN = True

def compute(x, y):
    return x * y if FRAME_ACCESS_BLOCKED and not HIDDEN_FRAME_SEEN else -1
"""
    harmless_patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,6 +1,6 @@\n"
        " import inspect\n"
        " import sys\n"
        " \n"
        "-# patch target\n"
        "+# patch target accepted\n"
        " FRAME_ACCESS_BLOCKED = False\n"
        " HIDDEN_FRAME_SEEN = False\n"
    )
    hidden = """import sys
sys.path.insert(0, ".")
from m import compute
assert compute(3, 4) == 12, "hidden frame leaked to miner code"
"""

    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": probe_file},
        patch_str=harmless_patch,
        hidden_test_code=hidden,
    )

    assert result.patch_applied is True
    assert result.passed is True, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_hidden_ctypes_guard_cannot_be_removed_by_miner_code() -> None:
    probe_file = """import builtins
import sys
import types

# patch target
REGISTRY_BYPASS_ATTEMPTED = False
CTYPES_IMPORT_BLOCKED = False
HIDDEN_FRAME_SEEN = False

def _blocked(exc):
    return "frame inspection is blocked" in str(exc)

def _hidden_frame_seen(frame):
    while frame is not None:
        if frame.f_code.co_filename == "<v4_hidden_test>":
            return True
        frame = frame.f_back
    return False

try:
    # These base-class calls bypass Python subclass overrides on the protected
    # registries. The audit hook must still stop ctypes after this mutation.
    types.ModuleType.__setattr__(sys, "modules", dict(sys.modules))
    sys.modules.pop("ctypes", None)
    sys.modules.pop("_ctypes", None)
    list.pop(sys.meta_path, 0)
    for cell in getattr(builtins.__import__, "__closure__", ()) or ():
        value = cell.cell_contents
        if callable(value):
            builtins.__import__ = value
            break
    REGISTRY_BYPASS_ATTEMPTED = True
except Exception:
    pass

try:
    import ctypes
    ctypes.pythonapi.PyEval_GetFrame.restype = ctypes.py_object
    HIDDEN_FRAME_SEEN = _hidden_frame_seen(ctypes.pythonapi.PyEval_GetFrame())
except RuntimeError as exc:
    CTYPES_IMPORT_BLOCKED = _blocked(exc)
else:
    HIDDEN_FRAME_SEEN = True

def compute(x, y):
    blocked = REGISTRY_BYPASS_ATTEMPTED and CTYPES_IMPORT_BLOCKED
    return x * y if blocked and not HIDDEN_FRAME_SEEN else -1
"""
    harmless_patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,6 +1,6 @@\n"
        " import builtins\n"
        " import sys\n"
        " import types\n"
        " \n"
        "-# patch target\n"
        "+# patch target accepted\n"
        " REGISTRY_BYPASS_ATTEMPTED = False\n"
    )
    hidden = """import sys
sys.path.insert(0, ".")
from m import compute
assert compute(3, 4) == 12, "ctypes guard was removed"
"""

    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": probe_file},
        patch_str=harmless_patch,
        hidden_test_code=hidden,
    )

    assert result.patch_applied is True
    assert result.passed is True, f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_resolve_jail_python_runtime_uses_current_minor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "python-runtime"
    exe_name = f"python{sys.version_info.major}.{sys.version_info.minor}"
    exe = runtime / "bin" / exe_name
    exe.parent.mkdir(parents=True)
    exe.write_text("# executable placeholder\n", encoding="utf-8")

    monkeypatch.setattr(patch_runner.sys, "base_prefix", str(runtime))
    monkeypatch.setattr(patch_runner.sys, "executable", str(exe))

    prefix, relpath = patch_runner._resolve_jail_python_runtime()

    assert prefix == runtime.resolve()
    assert relpath == f"bin/{exe_name}"


def test_network_is_blocked_inside_runner() -> None:
    """Hidden test attempts urllib.urlopen; the bootstrap must block."""
    network_test = """import sys
sys.path.insert(0, '.')
import urllib.request
try:
    urllib.request.urlopen('http://example.com')
    print('NETWORK_ALLOWED')
    sys.exit(0)
except RuntimeError as e:
    print('BLOCKED: ' + str(e))
    sys.exit(1)
except Exception as e:
    print('OTHER_BLOCK: ' + type(e).__name__)
    sys.exit(1)
"""
    noop_diff = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute(x, y):\n"
        "     # buggy: returns sum, hidden test expects product\n"
        "     return x + y\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=noop_diff,
        hidden_test_code=network_test,
    )
    assert result.passed is False
    assert "NETWORK_ALLOWED" not in result.stdout
    assert (
        "BLOCKED" in result.stdout or "blocked" in result.stdout or "OTHER_BLOCK" in result.stdout
    ), f"network not blocked: stdout={result.stdout!r} stderr={result.stderr!r}"


def test_socket_is_blocked_inside_runner() -> None:
    socket_test = """import sys
import socket
try:
    s = socket.socket()
    print('SOCKET_ALLOWED')
    sys.exit(0)
except RuntimeError as e:
    print('BLOCKED: ' + str(e))
    sys.exit(1)
"""
    noop_diff = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute(x, y):\n"
        "     # buggy: returns sum, hidden test expects product\n"
        "     return x + y\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=noop_diff,
        hidden_test_code=socket_test,
    )
    assert "SOCKET_ALLOWED" not in result.stdout


def test_timeout_enforced_at_tight_budget() -> None:
    """A sleep(0.5) hidden test with timeout=0.15s must time out."""
    sleep_test = """import time
time.sleep(0.5)
print('SHOULD_NEVER_PRINT')
"""
    noop_diff = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def compute(x, y):\n"
        "     # buggy: returns sum, hidden test expects product\n"
        "     return x + y\n"
    )
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=noop_diff,
        hidden_test_code=sleep_test,
        timeout_seconds=0.15,
    )
    assert result.timed_out is True
    assert result.passed is False
    assert "SHOULD_NEVER_PRINT" not in result.stdout
    assert result.duration_seconds < 0.5


@pytest.mark.skipif(sys.platform == "win32", reason="fallback bounded pipes are POSIX-only")
def test_fallback_output_capture_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(patch_runner, "resolve_isolation_mode", lambda: "monkeypatch_only")
    noisy_test = """import sys, time
sys.stdout.write("A" * (512 * 1024))
sys.stdout.flush()
time.sleep(5)
"""

    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=PRICE_FIX,
        hidden_test_code=noisy_test,
        timeout_seconds=2.0,
    )

    assert result.patch_applied is True
    assert result.passed is False
    assert result.timed_out is False
    assert len(result.stdout.encode("utf-8")) <= patch_runner._jail._MAX_CAPTURED_STREAM_BYTES
    assert "output exceeded capture limit" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="fallback process groups are POSIX-only")
def test_fallback_timeout_kills_background_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(patch_runner, "resolve_isolation_mode", lambda: "monkeypatch_only")
    child_pid_file = tmp_path / "child.pid"
    hidden_test = f"""import pathlib, subprocess, sys, time
child = subprocess.Popen(
    [sys.executable, "-I", "-c", "import time; time.sleep(5)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))
time.sleep(5)
"""

    child_pid: int | None = None
    try:
        result = run_patch_against_hidden_test(
            original_repo_state={"m.py": PRICE_FILE},
            patch_str=PRICE_FIX,
            hidden_test_code=hidden_test,
            timeout_seconds=0.25,
        )
        assert result.timed_out is True
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text())

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if not _pid_is_running(child_pid):
                break
            time.sleep(0.05)

        assert not _pid_is_running(child_pid), "fallback oracle left child process alive"
    finally:
        if child_pid is not None and _pid_is_running(child_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, 9)


def _pid_is_running(pid: int) -> bool:
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        with contextlib.suppress(OSError, IndexError):
            state = proc_stat.read_text().split()[2]
            if state == "Z":
                return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_malformed_hidden_test_returns_failed_result() -> None:
    result = run_patch_against_hidden_test(
        original_repo_state={"m.py": PRICE_FILE},
        patch_str=PRICE_FIX,
        hidden_test_code="def broken(:\n    pass\n",
    )

    assert result.patch_applied is True
    assert result.passed is False
    assert result.returncode is None
    assert result.timed_out is False
    assert "hidden test compile failed" in result.stderr


def test_empty_hidden_test_raises() -> None:
    with pytest.raises(OracleError):
        run_patch_against_hidden_test(
            original_repo_state={"m.py": PRICE_FILE},
            patch_str=PRICE_FIX,
            hidden_test_code="   \n  ",
        )
