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

import pytest

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


def test_empty_hidden_test_raises() -> None:
    with pytest.raises(OracleError):
        run_patch_against_hidden_test(
            original_repo_state={"m.py": PRICE_FILE},
            patch_str=PRICE_FIX,
            hidden_test_code="   \n  ",
        )
