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

# patch target
SECRET = "HIDDEN_SOURCE_" + "SENTINEL_9371"
LEAKED = any(
    SECRET in path.read_text(errors="ignore")
    for path in pathlib.Path(".").rglob("*.py")
)

def compute(x, y):
    return -1 if LEAKED else x * y
"""
    harmless_patch = (
        "--- a/m.py\n"
        "+++ b/m.py\n"
        "@@ -1,6 +1,6 @@\n"
        " import pathlib\n"
        " \n"
        "-# patch target\n"
        "+# patch target accepted\n"
        " SECRET = \"HIDDEN_SOURCE_\" + \"SENTINEL_9371\"\n"
        " LEAKED = any(\n"
        "     SECRET in path.read_text(errors=\"ignore\")\n"
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


def test_empty_hidden_test_raises() -> None:
    with pytest.raises(OracleError):
        run_patch_against_hidden_test(
            original_repo_state={"m.py": PRICE_FILE},
            patch_str=PRICE_FIX,
            hidden_test_code="   \n  ",
        )
