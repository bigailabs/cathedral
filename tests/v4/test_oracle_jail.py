"""Filesystem jail regression tests for the publisher-side oracle.

Finding 1 of the PR #133 review: Fred's local probe showed that a
miner patch could read ``/etc/hosts`` during hidden-test import and
leak its contents through stdout. The fix is the Linux fs-jail
(``cathedral.v4.oracle.jail``); these tests pin two contracts:

  1. ``test_jail_blocks_etc_hosts_read`` -- on Linux the jail makes
     ``/etc/hosts`` unreachable from the child (ENOENT) and no
     plausible contents (``localhost`` / ``127.0.0.1``) appear in
     stdout / stderr. Skipped on non-Linux because the jail is a
     Linux-only mechanism; the production startup check refuses to
     run a publisher-class workload on macOS.
  2. ``test_assert_production_isolation_refuses_monkeypatch`` --
     when ``CATHEDRAL_V4_ENV=production`` and the resolved mode is
     ``monkeypatch_only`` (the macOS dev posture), the runner's
     startup check raises ``RuntimeError`` rather than silently
     downgrading. Verified on every platform because the function
     is platform-independent CPU code.

The jail test is structurally complete on macOS even though it is
skipped: it can be executed verbatim on a Linux CI runner (or any
Linux box) to confirm the regression is fixed.
"""

from __future__ import annotations

import difflib
import os
import platform
import textwrap
from unittest import mock

import pytest

from cathedral.v4.oracle.patch_runner import (
    PATCH_RUNNER_TIMEOUT_SECONDS,
    PatchRunResult,
    assert_production_isolation,
    resolve_isolation_mode,
    run_patch_against_hidden_test,
)

# Files the jail must keep out of reach. Each entry: (path, sentinel
# substring we expect to see in the file's real contents on a
# typical host; used to detect leakage through stdout).
_HOST_FILES = [
    ("/etc/hosts", "localhost"),
    ("/etc/hosts", "127.0.0.1"),
    ("/etc/passwd", "root:"),
]


# The probe code injected into the patched module. It runs at module
# import time. If the read succeeds (jail bypassed) it emits a
# HOST_READ: marker carrying the file contents. If the read raises
# (jail working) it emits a HOST_BLOCKED: marker carrying the errno
# repr. Both markers are observable through stderr; the test asserts
# the BLOCKED marker is present (proof the probe ran) and the READ
# marker is absent (proof the jail held).
_LEAK_PROBE_SOURCE = textwrap.dedent(
    """
    import sys as _v4_sys

    def _v4_probe(path):
        try:
            with open(path, 'rb') as _f:
                _data = _f.read()
        except OSError as _e:
            _v4_sys.stderr.write('HOST_BLOCKED:' + path + ':' + repr(_e) + '\\n')
            return None
        _v4_sys.stderr.write(
            'HOST_READ:' + path + ':' + _data.decode('utf-8', 'replace') + '\\n'
        )
        return _data

    _v4_probe('/etc/hosts')
    _v4_probe('/etc/passwd')
    """
).strip()


# Initial contents of the file the patch will mutate. Deliberately
# small and stable so the unified diff is trivial to compute against
# what the runner materializes.
_PROBE_TARGET_RELPATH = "probe_target.py"
_PROBE_TARGET_INITIAL = textwrap.dedent(
    """
    # cathedral v4 jail probe target
    VALUE = 0
    """
).lstrip()


def _build_probe_patch(relpath: str, original: str, probe: str) -> str:
    """Return a valid unified diff that prepends ``probe`` to ``original``.

    Constructed with ``difflib.unified_diff`` so the patch matches
    whatever the runner sees on disk. The from/to prefixes match the
    ``a/`` / ``b/`` convention the in-tree applier expects.
    """
    original_lines = original.splitlines(keepends=True)
    patched_lines = (probe + "\n").splitlines(keepends=True) + original_lines
    return "".join(
        difflib.unified_diff(
            original_lines,
            patched_lines,
            fromfile=f"a/{relpath}",
            tofile=f"b/{relpath}",
            n=3,
        )
    )


@pytest.mark.skipif(platform.system() != "Linux", reason="jail requires Linux unshare(1) >= 2.36")
def test_jail_blocks_etc_hosts_read() -> None:
    """Prove the Linux fs-jail blocks /etc/hosts reads from miner code.

    Mirrors Fred's local probe: a miner-submitted unified diff
    injects a host-file read into a module the hidden test imports.
    Inside the jail the read MUST fail with OSError (ENOENT /
    EACCES) and the file contents (``localhost``, ``127.0.0.1``,
    ``root:``) MUST NOT appear in stdout or stderr.

    The test is dual-asserted to defeat false reassurance:

      * ``patch_applied=True`` -- the probe actually got injected.
      * ``HOST_BLOCKED:`` present -- the probe code actually ran.
      * ``HOST_READ:`` absent -- the jail actually blocked the read.

    If the probe is silently rejected before the subprocess fires
    (the original bug), both markers will be absent and the
    ``HOST_BLOCKED`` assertion will fail loudly rather than passing
    trivially.
    """
    # Sanity: the host actually has the files we're trying to leak.
    # If not, the test would pass trivially even without isolation.
    assert os.path.exists("/etc/hosts"), "test precondition: host has /etc/hosts"

    # Workspace state: a single small module the hidden test will
    # import. The miner patch prepends the probe to this module so
    # the probe fires at import time.
    workspace = {_PROBE_TARGET_RELPATH: _PROBE_TARGET_INITIAL}
    probe_patch = _build_probe_patch(
        relpath=_PROBE_TARGET_RELPATH,
        original=_PROBE_TARGET_INITIAL,
        probe=_LEAK_PROBE_SOURCE,
    )
    assert probe_patch, "difflib produced an empty diff -- probe construction is broken"

    hidden = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, '.')
        import probe_target  # noqa: F401 -- import side effect IS the probe
        assert True, 'probe import complete'
        """
    ).lstrip()

    result: PatchRunResult = run_patch_against_hidden_test(
        original_repo_state=workspace,
        patch_str=probe_patch,
        hidden_test_code=hidden,
        timeout_seconds=PATCH_RUNNER_TIMEOUT_SECONDS,
    )

    # The probe was injected -- if this fails the test proves nothing
    # about the jail because the malicious code never ran.
    assert result.patch_applied is True, (
        f"probe patch was rejected before subprocess fired; stderr={result.stderr[:400]!r}"
    )

    # The run executed under the Linux fs-jail (not a weaker mode).
    assert result.isolation_mode == "jailed", (
        f"expected jail isolation, got {result.isolation_mode!r}; "
        f"stdout={result.stdout[:200]!r} stderr={result.stderr[:200]!r}"
    )

    # The probe DID run (jail did not silently bypass execution).
    # Absence of this marker means the malicious code never executed,
    # which would make every other assertion a false negative.
    combined = result.stdout + result.stderr
    assert "HOST_BLOCKED:" in combined, (
        f"leak probe never executed; isolation may be silently bypassed. "
        f"stdout={result.stdout[:400]!r} stderr={result.stderr[:400]!r}"
    )

    # The jail held -- no leak markers, no host-file contents.
    assert "HOST_READ:" not in result.stdout, f"jail leak via stdout: {result.stdout!r}"
    assert "HOST_READ:" not in result.stderr, f"jail leak via stderr: {result.stderr!r}"

    for _path, sentinel in _HOST_FILES:
        assert sentinel not in result.stdout, (
            f"host-file sentinel {sentinel!r} leaked through stdout"
        )
        assert sentinel not in result.stderr, (
            f"host-file sentinel {sentinel!r} leaked through stderr"
        )


def test_assert_production_isolation_refuses_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production startup check refuses to start under monkeypatch_only.

    Pattern: with CATHEDRAL_V4_ENV=production set, calling
    ``assert_production_isolation(mode='monkeypatch_only')`` raises
    RuntimeError. With the env var unset the same call is a no-op.
    """
    monkeypatch.setenv("CATHEDRAL_V4_ENV", "production")
    with pytest.raises(RuntimeError, match="isolation"):
        assert_production_isolation(mode="monkeypatch_only")

    # The error message names the env var and the resolved mode so
    # operators can debug without spelunking source.
    monkeypatch.setenv("CATHEDRAL_V4_ENV", "production")
    try:
        assert_production_isolation(mode="monkeypatch_only")
    except RuntimeError as exc:
        assert "CATHEDRAL_V4_ENV" in str(exc)
        assert "monkeypatch_only" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")

    # unshare_n_only is ALSO refused -- only jailed passes.
    with pytest.raises(RuntimeError, match="isolation"):
        assert_production_isolation(mode="unshare_n_only")

    # jailed passes.
    assert_production_isolation(mode="jailed")  # no raise


def test_assert_production_isolation_noop_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside production, weak isolation is allowed (dev convenience)."""
    monkeypatch.delenv("CATHEDRAL_V4_ENV", raising=False)
    # Every mode is acceptable when env is unset.
    assert_production_isolation(mode="monkeypatch_only")
    assert_production_isolation(mode="unshare_n_only")
    assert_production_isolation(mode="jailed")

    # And the default-resolved mode also passes.
    assert_production_isolation()


def test_run_patch_against_hidden_test_refuses_production_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner itself enforces the production check on every call.

    Even if the publisher worker never called
    ``assert_production_isolation`` at boot, the runner re-checks on
    every invocation so a misconfigured deployment cannot
    accumulate weak-isolation runs.
    """
    monkeypatch.setenv("CATHEDRAL_V4_ENV", "production")

    # Force the resolver to report a non-jailed mode so the check
    # fires regardless of host capability.
    with mock.patch(
        "cathedral.v4.oracle.patch_runner.resolve_isolation_mode",
        return_value="monkeypatch_only",
    ):
        with pytest.raises(RuntimeError, match="isolation"):
            run_patch_against_hidden_test(
                original_repo_state={"x.py": ""},
                patch_str="--- a/x.py\n+++ b/x.py\n",
                hidden_test_code="assert True",
            )


def test_resolve_isolation_mode_macos_returns_monkeypatch() -> None:
    """On macOS the resolver must return monkeypatch_only.

    Pin the platform contract: macOS dev hosts cannot get the fs
    jail, and the resolver should report so honestly. The runner
    relies on that honesty for the production startup check to bite.
    """
    if platform.system() != "Darwin":
        pytest.skip("macOS-only contract")
    assert resolve_isolation_mode() == "monkeypatch_only"


@pytest.mark.skipif(platform.system() != "Linux", reason="jail requires Linux unshare(1) >= 2.36")
def test_jail_timeout_kills_sleeping_child_promptly() -> None:
    """The jailed path kills a sleeping workload before its sleep ends.

    Regression: previously the timeout fired against the outer unshare(1)
    wrapper while the inner workload python kept running long enough that
    a time.sleep(0.5) completed despite a 0.15s budget. The pgrp-kill
    semantics in jail.run_in_jail must:

      * report timed_out=True
      * report passed=False
      * complete in well under the sleep duration (we use 0.4s as a
        generous ceiling -- the kernel needs a few ms to deliver SIGKILL
        but we should be nowhere near 0.5s)
      * never let the SHOULD_NEVER_PRINT marker reach stdout
      * leave no orphan in our caller-visible process tree (waitpid
        WNOHANG returns 0 immediately because the pgrp kill reaped
        everything by the time communicate() returned)

    The test dual-asserts isolation_mode='jailed' and patch_applied=True
    so a regression that silently falls back to a weaker mode or rejects
    the patch fails loudly rather than passing trivially.
    """
    sleep_test = textwrap.dedent(
        """
        import time
        time.sleep(0.5)
        print('SHOULD_NEVER_PRINT')
        """
    ).lstrip()
    noop_diff = textwrap.dedent(
        """\
        --- a/m.py
        +++ b/m.py
        @@ -1,3 +1,3 @@
         def compute(x, y):
             # buggy: returns sum, hidden test expects product
             return x + y
        """
    )
    original = textwrap.dedent(
        """\
        def compute(x, y):
            # buggy: returns sum, hidden test expects product
            return x + y
        """
    )

    result: PatchRunResult = run_patch_against_hidden_test(
        original_repo_state={"m.py": original},
        patch_str=noop_diff,
        hidden_test_code=sleep_test,
        timeout_seconds=0.15,
    )

    assert result.patch_applied is True, (
        f"noop patch was rejected before subprocess fired; stderr={result.stderr[:400]!r}"
    )
    assert result.isolation_mode == "jailed", (
        f"expected jail isolation, got {result.isolation_mode!r}"
    )
    assert result.timed_out is True, (
        f"jailed sleep(0.5) was not killed; passed={result.passed} "
        f"returncode={result.returncode} duration={result.duration_seconds} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert result.passed is False
    assert result.duration_seconds < 0.4, (
        f"jail kill was slow: {result.duration_seconds}s; pgrp kill should "
        f"reap within a few ms of the 0.15s budget"
    )
    assert "SHOULD_NEVER_PRINT" not in result.stdout, (
        f"workload print leaked despite timeout: stdout={result.stdout!r}"
    )

    # No orphan: any host-side child the jail spawned should already be
    # reaped by the time communicate() returned. waitpid WNOHANG on -1
    # returns (0, 0) when there are no zombie children to collect.
    try:
        pid, status = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        # No children at all -- also a clean state.
        pid, status = 0, 0
    assert pid == 0, f"orphan child detected after jail kill: pid={pid} status={status}"
