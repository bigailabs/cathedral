# ruff: noqa: ASYNC240
"""bug_isolation_v1 path through SshHermesRunner.run_bug_isolation_challenge.

These tests are about the v3 full-Hermes-package collection wiring:
trace_bundle is built every run, exactly one repair shot fires when (and
only when) the first stdout has no parseable JSON, and the synthetic
card written into the bundle never includes hidden oracle fields.
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sys
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Direct-module-load to dodge the cathedral.eval.__init__ -> publisher
# import cycle: matches the pattern in tests/v1/test_ssh_hermes_runner.py.
_ROOT = Path(__file__).resolve().parents[2]

if "cathedral.eval.polaris_runner" in sys.modules:
    _pr = sys.modules["cathedral.eval.polaris_runner"]
else:
    _PR_PATH = _ROOT / "src" / "cathedral" / "eval" / "polaris_runner.py"
    _pr_spec = _ilu.spec_from_file_location("cathedral.eval.polaris_runner", _PR_PATH)
    assert _pr_spec and _pr_spec.loader
    _pr = _ilu.module_from_spec(_pr_spec)
    sys.modules["cathedral.eval.polaris_runner"] = _pr
    _pr_spec.loader.exec_module(_pr)

if "_ssh_hermes_runner_for_test" in sys.modules:
    _module = sys.modules["_ssh_hermes_runner_for_test"]
else:
    _SHR_PATH = _ROOT / "src" / "cathedral" / "eval" / "ssh_hermes_runner.py"
    _spec = _ilu.spec_from_file_location("_ssh_hermes_runner_for_test", _SHR_PATH)
    assert _spec and _spec.loader
    _module = _ilu.module_from_spec(_spec)
    sys.modules["_ssh_hermes_runner_for_test"] = _module
    _spec.loader.exec_module(_module)

from cathedral.v3.corpus.schema import ChallengeRow  # noqa: E402

SshHermesRunner = _module.SshHermesRunner
SshHermesRunnerConfig = _module.SshHermesRunnerConfig
SshHermesError = _module.SshHermesError
BugIsolationHermesRun = _module.BugIsolationHermesRun


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def ssh_key_path(tmp_path: Path) -> str:
    k = tmp_path / "fake_id_ed25519"
    k.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
    return str(k)


@pytest.fixture
def bundle_output_dir(tmp_path: Path) -> str:
    d = tmp_path / "bundles"
    d.mkdir()
    return str(d)


@pytest.fixture
def runner_config(ssh_key_path: str, bundle_output_dir: str) -> SshHermesRunnerConfig:
    return SshHermesRunnerConfig(
        ssh_private_key_path=ssh_key_path,
        bundle_output_dir=bundle_output_dir,
        connect_timeout_secs=5.0,
        eval_timeout_secs=30.0,
        transfer_timeout_secs=10.0,
        connect_retries=2,
        connect_retry_initial_secs=0.01,
    )


@pytest.fixture
def submission() -> dict[str, Any]:
    return {
        "id": "sub_bug_iso_001",
        "ssh_host": "miner.example.invalid",
        "ssh_port": 22,
        "ssh_user": "cathedral-probe",
    }


@pytest.fixture
def challenge() -> ChallengeRow:
    return ChallengeRow(
        id="pilot_alpha",
        repo="https://example.invalid/project",
        commit="a" * 40,
        issue_text="Calling parse_config with an empty section crashes.",
        culprit_file="src/project/config.py",
        culprit_symbol="parse_config",
        line_range=(40, 55),
        required_failure_keywords=("empty", "section", "crash"),
        difficulty="easy",
        bucket="input_validation",
        source_url="https://example.invalid/commit/" + "b" * 40,
    )


def _mk_run_result(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.exit_status = exit_status
    return r


def _mk_sftp() -> Any:
    sftp = MagicMock()
    sftp.get = AsyncMock(return_value=None)
    sftp.listdir = AsyncMock(return_value=[])
    sftp.__aenter__ = AsyncMock(return_value=sftp)
    sftp.__aexit__ = AsyncMock(return_value=None)
    return sftp


def _valid_final_answer(challenge_id_public: str = "ch_pilot_alpha") -> str:
    return (
        "Some reasoning text.\n\n"
        "```FINAL_ANSWER\n"
        "{\n"
        f'  "challenge_id": "{challenge_id_public}",\n'
        '  "culprit_file": "src/project/config.py",\n'
        '  "culprit_symbol": "parse_config",\n'
        '  "line_range": [40, 55],\n'
        '  "failure_mode": "empty section crash"\n'
        "}\n"
        "```\n"
    )


def _build_fake_conn(invoke_responses: list[str]) -> tuple[Any, list[str]]:
    """Build a fake asyncssh conn. ``invoke_responses`` are returned, in
    order, for each ``hermes chat`` call. Returns (conn, captured_cmds)."""
    captured: list[str] = []
    invoke_iter = iter(invoke_responses)

    def _route(cmd: str, **kwargs: Any) -> Any:
        captured.append(cmd)
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "test -d" in cmd and "test -r" in cmd:
            return _mk_run_result()
        if "hermes profile create" in cmd:
            return _mk_run_result()
        if "python3 -c" in cmd:
            return _mk_run_result()
        if "hermes chat -Q" in cmd:
            try:
                return _mk_run_result(stdout=next(invoke_iter))
            except StopIteration:
                raise AssertionError(
                    f"hermes chat called more times than expected; cmd={cmd!r}"
                ) from None
        if cmd.startswith("rm -f") or "rm -f " in cmd:
            return _mk_run_result()
        if "test -f" in cmd:
            return _mk_run_result()
        if "tar -czf" in cmd:
            return _mk_run_result()
        if "hermes profile delete" in cmd:
            return _mk_run_result()
        return _mk_run_result()

    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)
    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    sftp = _mk_sftp()

    async def fake_get(remote: str, local: str) -> None:
        p = Path(local)
        p.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith(".db"):
            p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        elif remote.endswith(".tar.gz"):
            p.write_bytes(b"")
        else:
            p.write_text("stub\n")

    sftp.get = AsyncMock(side_effect=fake_get)
    sftp.listdir = AsyncMock(return_value=[])
    conn.start_sftp_client = MagicMock(return_value=sftp)
    return conn, captured


def _fake_asyncssh(conn: Any) -> Any:
    fake = MagicMock()
    fake.connect = AsyncMock(return_value=conn)
    fake.Error = Exception
    fake.PermissionDenied = type("PermissionDenied", (Exception,), {})
    return fake


# --------------------------------------------------------------------------
# Test 1: happy path: bundle is created on first-attempt success.
# --------------------------------------------------------------------------


async def test_first_attempt_success_creates_trace_bundle(
    runner_config, submission, challenge
) -> None:
    conn, captured = _build_fake_conn([_valid_final_answer()])
    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": _fake_asyncssh(conn)}):
        run = await runner.run_bug_isolation_challenge(
            challenge=challenge,
            miner_hotkey="5BugIso" + "x" * 41,
            submission=submission,
        )

    assert isinstance(run, BugIsolationHermesRun)
    assert run.repair_stdout is None
    assert run.trace_bundle is not None
    assert run.trace_bundle.bundle_tar_path.exists()
    assert run.trace_bundle.bundle_tar_path.stat().st_size > 0

    # Exactly one hermes chat call (no repair).
    chat_cmds = [c for c in captured if "hermes chat -Q" in c]
    assert len(chat_cmds) == 1

    # The synthetic card (and the bundle generally) must not carry
    # hidden oracle fields. Crack the tar and grep every file.
    forbidden = {
        challenge.culprit_file,
        challenge.culprit_symbol or "<<no_symbol>>",
        f"{challenge.line_range[0]},{challenge.line_range[1]}",
    }
    forbidden.discard("<<no_symbol>>")
    forbidden_substrings = (
        "culprit_file",
        "culprit_symbol",
        "line_range",
        "required_failure_keywords",
    )
    with tarfile.open(run.trace_bundle.bundle_tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            blob = f.read()
            # hermes_stdout.txt legitimately contains the model's claim
            # (which may echo oracle field NAMES like culprit_file as
            # part of the JSON it wrote). Only enforce the no-oracle
            # check on the manifest + synthetic-card-shaped surfaces.
            if member.name.endswith("hermes_stdout.txt"):
                continue
            if member.name.endswith("hermes_repair_stdout.txt"):
                continue
            text = blob.decode("utf-8", errors="replace")
            for needle in forbidden_substrings:
                assert needle not in text, (
                    f"oracle field {needle!r} leaked into bundle file {member.name!r}"
                )

    # Manifest carries only the synthetic-card task_type + public id,
    # never the hidden oracle. Spot-check the in-memory manifest.
    manifest = run.trace_bundle.manifest
    assert manifest["eval_id"]
    assert manifest["submission_id"] == "sub_bug_iso_001"
    assert "culprit_file" not in json.dumps(manifest)
    assert "required_failure_keywords" not in json.dumps(manifest)


# --------------------------------------------------------------------------
# Test 2: malformed first stdout triggers exactly one repair.
# --------------------------------------------------------------------------


async def test_malformed_first_stdout_triggers_exactly_one_repair(
    runner_config, submission, challenge
) -> None:
    malformed = "I cannot find a single FINAL_ANSWER block, sorry.\n"
    good = _valid_final_answer()
    conn, captured = _build_fake_conn([malformed, good])
    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": _fake_asyncssh(conn)}):
        run = await runner.run_bug_isolation_challenge(
            challenge=challenge,
            miner_hotkey="5BugIso" + "x" * 41,
            submission=submission,
        )

    # The first stdout (malformed) survives on the dataclass; repair on the side.
    assert run.stdout == malformed
    assert run.repair_stdout == good

    # Exactly two hermes chat calls.
    chat_cmds = [c for c in captured if "hermes chat -Q" in c]
    assert len(chat_cmds) == 2

    # Bundle includes the repair sidecar.
    assert run.trace_bundle is not None
    found_repair = False
    with tarfile.open(run.trace_bundle.bundle_tar_path, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith("hermes_repair_stdout.txt"):
                found_repair = True
                f = tar.extractfile(member)
                assert f is not None
                assert f.read().decode("utf-8") == good
    assert found_repair, "expected hermes_repair_stdout.txt sidecar in bundle"


# --------------------------------------------------------------------------
# Test 3: valid JSON with wrong shape does NOT repair.
# --------------------------------------------------------------------------


async def test_valid_wrong_shape_json_does_not_repair(
    runner_config, submission, challenge
) -> None:
    # JSON parses fine but is missing culprit_file (required field). The
    # extractor raises ClaimExtractionError("missing_required_fields", ...)
    # which is_repair_worthy returns False for.
    wrong_shape = (
        "```FINAL_ANSWER\n"
        '{ "challenge_id": "ch_pilot_alpha", "line_range": [1,2], '
        '"failure_mode": "x" }\n'
        "```\n"
    )
    conn, captured = _build_fake_conn([wrong_shape])
    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": _fake_asyncssh(conn)}):
        run = await runner.run_bug_isolation_challenge(
            challenge=challenge,
            miner_hotkey="5BugIso" + "x" * 41,
            submission=submission,
        )

    assert run.repair_stdout is None
    chat_cmds = [c for c in captured if "hermes chat -Q" in c]
    assert len(chat_cmds) == 1
    assert run.trace_bundle is not None


# --------------------------------------------------------------------------
# Test 4: bundle assembly failure still cleans up the profile.
# --------------------------------------------------------------------------


async def test_profile_cleanup_on_generic_exception(
    runner_config, submission, challenge, monkeypatch
) -> None:
    conn, _captured = _build_fake_conn([_valid_final_answer()])

    runner = SshHermesRunner(runner_config)

    # Force _collect_and_assemble to blow up with a generic exception
    # (which the wrapper turns into SshHermesError("bundle_assembly_failed")).
    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise SshHermesError("bundle_assembly_failed", "synthetic explosion")

    monkeypatch.setattr(runner, "_collect_and_assemble", boom)

    delete_calls: list[str] = []
    original_delete = runner._delete_profile

    async def spy_delete(c: Any, eval_profile: str) -> None:
        delete_calls.append(eval_profile)
        await original_delete(c, eval_profile)

    monkeypatch.setattr(runner, "_delete_profile", spy_delete)

    with patch.dict(sys.modules, {"asyncssh": _fake_asyncssh(conn)}):
        with pytest.raises(SshHermesError) as exc:
            await runner.run_bug_isolation_challenge(
                challenge=challenge,
                miner_hotkey="5BugIso" + "x" * 41,
                submission=submission,
            )

    assert exc.value.code == "bundle_assembly_failed"
    assert len(delete_calls) == 1
    assert delete_calls[0].startswith("cathedral-eval-bug-isolation-")
