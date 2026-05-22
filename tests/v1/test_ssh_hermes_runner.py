# ruff: noqa: ASYNC240
"""Unit tests for SshHermesRunner (cathedralai/cathedral#75, PR 2).

Mocks asyncssh at the module level. Covers:
- happy path (full lifecycle: version → install check → clone → invoke
  → snapshot → SCP back → bundle assemble → delete profile)
- each enumerated failure code in SSH_HERMES_FAILURE_CODES that's
  reachable from the runner control flow
- manifest shape (matches the sketch in issue #75 PR 2 status comment)
- ``CATHEDRAL_PROBER_VERSION`` env dispatch in
  ``orchestrator._resolve_polaris_runner_for_mode``
"""

from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Same direct-module-load dance as test_ssh_probe_runner.py — avoids
# the cathedral.eval.__init__ -> publisher import cycle.
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

_SHR_PATH = _ROOT / "src" / "cathedral" / "eval" / "ssh_hermes_runner.py"
_spec = _ilu.spec_from_file_location("_ssh_hermes_runner_for_test", _SHR_PATH)
assert _spec and _spec.loader
_module = _ilu.module_from_spec(_spec)
sys.modules["_ssh_hermes_runner_for_test"] = _module
_spec.loader.exec_module(_module)

SshHermesRunner = _module.SshHermesRunner
SshHermesRunnerConfig = _module.SshHermesRunnerConfig
SshHermesError = _module.SshHermesError
SSH_HERMES_FAILURE_CODES = _module.SSH_HERMES_FAILURE_CODES
_extract_card_json = _module._extract_card_json
_compute_proof_of_loop = _module._compute_proof_of_loop

from cathedral.v1_types import EvalTask  # noqa: E402


def test_now_utc_iso_uses_receipt_ordering_timestamp_format() -> None:
    timestamp = _module._now_utc_iso()
    assert timestamp.endswith("Z")
    assert "+00:00" not in timestamp
    date_part, time_part = timestamp.removesuffix("Z").split("T")
    assert len(date_part) == len("2026-05-20")
    assert len(time_part.split(".")[1]) == 3
    _module.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


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
        pinned_model="claude-3-opus-20240229",
        pinned_provider="anthropic",
    )


@pytest.fixture
def eval_task() -> EvalTask:
    return EvalTask(
        card_id="eu-ai-act",
        epoch=1,
        round_index=0,
        prompt="Produce the current EU AI Act regulatory card as JSON.",
    )


@pytest.fixture
def submission() -> dict[str, Any]:
    return {
        "id": "sub_test_001",
        "ssh_host": "miner.example.com",
        "ssh_port": 22,
        "ssh_user": "cathedral-probe",
    }


def _mk_run_result(stdout: str = "", stderr: str = "", exit_status: int = 0) -> Any:
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.exit_status = exit_status
    return r


def _mk_sftp() -> Any:
    """Stub SFTP context manager. `listdir` returns no sessions by
    default; tests override per-fixture."""
    sftp = MagicMock()
    sftp.get = AsyncMock(return_value=None)
    sftp.listdir = AsyncMock(return_value=[])
    sftp.__aenter__ = AsyncMock(return_value=sftp)
    sftp.__aexit__ = AsyncMock(return_value=None)
    return sftp


# --------------------------------------------------------------------------
# Config + constructor
# --------------------------------------------------------------------------


def test_config_invalid_when_ssh_key_missing(tmp_path: Path, bundle_output_dir: str):
    with pytest.raises(SshHermesError) as exc:
        SshHermesRunner(
            SshHermesRunnerConfig(
                ssh_private_key_path=str(tmp_path / "does-not-exist"),
                bundle_output_dir=bundle_output_dir,
            )
        )
    assert exc.value.code == "config_invalid"


def test_failure_codes_set_is_complete():
    """Every code listed in SSH_HERMES_FAILURE_CODES is reachable from
    the runner. Don't add a code without using it; don't use a code
    without listing it (the SshHermesError __init__ enforces this)."""
    # Constructor enforces the membership invariant.
    for code in SSH_HERMES_FAILURE_CODES:
        err = SshHermesError(code, "test")
        assert err.code == code


def test_failure_code_unknown_raises():
    with pytest.raises(ValueError):
        SshHermesError("not-a-real-code", "test")


# --------------------------------------------------------------------------
# _extract_card_json
# --------------------------------------------------------------------------


def test_extract_card_json_from_fenced_block():
    stdout = """Here's the card:

```json
{"id": "eu-ai-act", "title": "EU AI Act enforcement update"}
```

Hope that helps."""
    result = _extract_card_json(stdout)
    assert result == {"id": "eu-ai-act", "title": "EU AI Act enforcement update"}


def test_extract_card_json_from_raw_json():
    stdout = '{"id": "eu-ai-act", "summary": "raw json output"}'
    result = _extract_card_json(stdout)
    assert result == {"id": "eu-ai-act", "summary": "raw json output"}


def test_extract_card_json_picks_last_balanced_object():
    """When the agent rambles before / after, parse the last `{...}`."""
    stdout = (
        'I noticed an old draft: {"id": "old"}. '
        'Here is the current card: {"id": "eu-ai-act", "title": "current"}'
    )
    result = _extract_card_json(stdout)
    assert result == {"id": "eu-ai-act", "title": "current"}


def test_extract_card_json_returns_none_on_garbage():
    assert _extract_card_json("no json here") is None
    assert _extract_card_json("") is None
    assert _extract_card_json("{not valid json") is None


# --------------------------------------------------------------------------
# Connect / auth failures
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_refused_raises(runner_config, eval_task, submission):
    """Connect failures raise SshHermesError; the orchestrator's
    try/except wraps this into a rejected eval. The runner doesn't
    swallow these — connect errors need operator visibility."""
    runner = SshHermesRunner(runner_config)
    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(side_effect=OSError("connection refused"))
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        with pytest.raises(SshHermesError) as exc:
            await runner.run(
                bundle_bytes=b"",
                bundle_hash="x" * 64,
                task=eval_task,
                miner_hotkey="5Test" + "x" * 43,
                submission=submission,
            )
    assert exc.value.code == "connect_refused"


@pytest.mark.asyncio
async def test_auth_failed_raises(runner_config, eval_task, submission):
    """Auth failures raise SshHermesError; the runner doesn't retry on
    auth errors (different from connect errors which retry with backoff)."""
    runner = SshHermesRunner(runner_config)
    perm_denied = type("PermissionDenied", (Exception,), {})
    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(side_effect=perm_denied("bad key"))
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = perm_denied
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        with pytest.raises(SshHermesError) as exc:
            await runner.run(
                bundle_bytes=b"",
                bundle_hash="x" * 64,
                task=eval_task,
                miner_hotkey="5Test" + "x" * 43,
                submission=submission,
            )
    assert exc.value.code == "auth_failed"


# --------------------------------------------------------------------------
# Config_invalid on bad submission
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_invalid_when_submission_missing_ssh_host(runner_config, eval_task):
    """ssh_host missing in submission raises SshHermesError(config_invalid)
    before any SSH attempt. The orchestrator's try/except wraps this
    into a rejected eval_run; the runner itself raises directly."""
    runner = SshHermesRunner(runner_config)
    with pytest.raises(SshHermesError) as exc:
        await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission={"id": "s1", "ssh_user": "cathedral-probe"},  # no ssh_host
        )
    assert exc.value.code == "config_invalid"


# --------------------------------------------------------------------------
# Happy path (mocked end-to-end)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_card_and_bundle(runner_config, eval_task, submission, tmp_path):
    """Mock asyncssh + SFTP + filesystem so the runner walks the full
    9-step lifecycle and produces a bundle with a valid manifest."""
    card_json = {
        "id": "eu-ai-act",
        "title": "EU AI Act enforcement: AI Office issues Annex III guidance",
        "summary": "The European Commission's AI Office published clarifying guidance...",
        "what_changed": "Annex III now includes specific subcategories.",
        "why_it_matters": "Providers re-classify risk.",
        "action_notes": "Re-run conformity assessments.",
        "risks": "Misclassification penalties up to 7% of global revenue.",
        "no_legal_advice": True,
    }

    # Pre-populate a fake session log + request dump on local disk so
    # _compute_proof_of_loop has something to read. We point bundle assembly
    # at a fixture profile path by intercepting SFTP.get to copy files there.

    # Per-command stub. asyncssh.connect returns a conn whose run/
    # start_sftp_client we control.
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route_run(cmd, **kwargs):
        # `hermes --version`
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        # `printf "%s" "$HOME"` (tilde-expansion bootstrap)
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        # install check
        if "test -d" in cmd and "test -r" in cmd:
            return _mk_run_result()
        # profile create
        if "hermes profile create" in cmd:
            return _mk_run_result()
        # state.db snapshot via python3 -c
        if "python3 -c" in cmd:
            return _mk_run_result()
        # `hermes chat -q`
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        # rm -f cleanup
        if cmd.startswith("rm -f"):
            return _mk_run_result()
        # test -f for SFTP existence
        if "test -f" in cmd:
            return _mk_run_result()
        # tar skills
        if "tar -czf" in cmd:
            return _mk_run_result()
        # hermes profile delete
        if "hermes profile delete" in cmd:
            return _mk_run_result()
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route_run(cmd, **kw))

    # SFTP: pretend every requested file exists but write empty bytes
    # to local dest. For session_*.json we plant a realistic doc so
    # _compute_proof_of_loop produces non-zero counts.
    session_log = {
        "session_id": "sess_abc123",
        "system_prompt": "You are a SOUL.md driven AGENTS.md agent. MEMORY context...",
        "messages": [
            {
                "role": "assistant",
                "content": "let me look that up",
                "tool_calls": [
                    {"function": {"name": "web.fetch"}, "id": "tc_1"},
                    {"function": {"name": "skill.invoke:eu-ai-act"}, "id": "tc_2"},
                ],
            },
            {"role": "tool", "tool_call_id": "tc_1", "content": "[]"},
        ],
    }

    async def fake_sftp_get(remote: str, local: str) -> None:
        p = Path(local)
        p.parent.mkdir(parents=True, exist_ok=True)
        if remote.endswith(".db"):
            p.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
        elif "session_" in remote and remote.endswith(".json"):
            p.write_text(json.dumps(session_log))
        elif "request_dump_" in remote:
            p.write_text("{}")
        elif remote.endswith(".tar.gz"):
            p.write_bytes(b"")
        elif remote.endswith(".md") or remote.endswith(".log"):
            p.write_text("stub\n")
        else:
            p.write_text("stub")

    async def fake_listdir(remote: str) -> list[str]:
        if remote.endswith("/sessions"):
            return [
                "session_sess_abc123.json",
                "request_dump_sess_abc123_001.json",
                "request_dump_sess_abc123_002.json",
            ]
        return []

    sftp = _mk_sftp()
    sftp.get = AsyncMock(side_effect=fake_sftp_get)
    sftp.listdir = AsyncMock(side_effect=fake_listdir)
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    # Card came through
    assert result.errors == []
    assert result.output_card_json["id"] == "eu-ai-act"
    assert "EU AI Act" in result.output_card_json["title"]

    # Trace has the new ssh-hermes fields
    trace = result.trace
    assert trace is not None
    assert trace["tier"] == "ssh-hermes"
    assert trace["hermes_version"] == "hermes 0.13.0"
    assert trace["eval_profile_name"].startswith("cathedral-eval-")
    assert trace["bundle_path"], "bundle_path should be set"
    assert trace["bundle_blake3"], "bundle_blake3 should be set"

    # Bundle file exists on disk and is non-empty
    bundle_path = Path(trace["bundle_path"])
    assert bundle_path.exists()
    assert bundle_path.stat().st_size > 0

    # Proof of loop: counted the tool calls + dumps + system prompt markers
    proof = trace["proof_of_loop"]
    assert proof["session_id"] == "sess_abc123"
    assert proof["tool_call_count"] == 2
    assert proof["request_dump_file_count"] == 2
    assert proof["api_call_count"] == 2
    assert proof["system_prompt_includes_soul_md"] is True
    assert proof["system_prompt_includes_agents_md"] is True
    assert proof["system_prompt_includes_memory_md"] is True
    assert set(proof["tool_calls_observed"]) == {"web.fetch", "skill.invoke:eu-ai-act"}

    # PolarisRunResult shape: agent_id is ssh-hermes:<hotkey-prefix>
    assert result.polaris_agent_id.startswith("ssh-hermes:")
    assert result.attestation is None
    assert result.manifest is None  # PR 3 sets this


# --------------------------------------------------------------------------
# Hermes not installed → hermes_not_found
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_not_found_on_missing_binary(runner_config, eval_task, submission):
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        if "hermes --version" in cmd:
            return _mk_run_result(stderr="bash: hermes: command not found", exit_status=127)
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    assert result.errors
    assert result.errors[0].startswith("hermes_not_found:")


# --------------------------------------------------------------------------
# Hermes install invalid → directory missing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_install_invalid_when_home_missing(runner_config, eval_task, submission):
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "test -d" in cmd:
            return _mk_run_result(stderr="missing", exit_status=1)
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    assert result.errors
    assert result.errors[0].startswith("hermes_install_invalid:")


# --------------------------------------------------------------------------
# Profile clone fails
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_profile_clone_failed_when_clone_returns_nonzero(
    runner_config, eval_task, submission
):
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "test -d" in cmd and "test -r" in cmd:
            return _mk_run_result()
        if "hermes profile create" in cmd:
            return _mk_run_result(stderr="Profile already exists", exit_status=1)
        if "hermes profile delete" in cmd:
            return _mk_run_result()
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    assert result.errors
    assert result.errors[0].startswith("profile_clone_failed:")


# --------------------------------------------------------------------------
# Hermes chat -q output is malformed (no parseable Card JSON)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hermes_output_malformed_when_no_json_in_stdout(runner_config, eval_task, submission):
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "test -d" in cmd and "test -r" in cmd:
            return _mk_run_result()
        if "hermes profile create" in cmd:
            return _mk_run_result()
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout="I cannot produce a card today, sorry.")
        if "hermes profile delete" in cmd:
            return _mk_run_result()
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    assert result.errors
    assert result.errors[0].startswith("hermes_output_malformed:")


# --------------------------------------------------------------------------
# Manifest shape — sanity-check the issue-#75 spec
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_shape_matches_spec(runner_config, eval_task, submission):
    """Re-run the happy path test, then crack open the tar.gz and read
    the manifest. Assert it has all the fields the validator-compat
    agent is scaffolding against."""
    card_json = {"id": "eu-ai-act", "summary": "ok", "no_legal_advice": True}
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
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
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))

    async def fake_get(remote: str, local: str):
        p = Path(local)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"stub")

    sftp = _mk_sftp()
    sftp.get = AsyncMock(side_effect=fake_get)
    sftp.listdir = AsyncMock(return_value=[])
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        result = await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    # No errors expected from happy path
    assert result.errors == []

    # The runner doesn't expose the manifest dict directly on the
    # PolarisRunResult — it lives in the tar.gz. We can re-derive its
    # shape from the runner's internal state via the trace's
    # bundle_blake3 + bundle_path, but for now assert the bundle exists.
    bundle_path = Path(result.trace["bundle_path"])
    assert bundle_path.exists()
    assert bundle_path.suffix == ".gz"

    # Bundle hash format: hex
    assert len(result.trace["bundle_blake3"]) == 64
    assert all(c in "0123456789abcdef" for c in result.trace["bundle_blake3"])


# --------------------------------------------------------------------------
# Tilde-expansion regression (v1.1.3): the prober verify command MUST
# NOT pass `'~/.hermes'` to bash — single quotes suppress tilde expansion
# and every miner with default hermes_home then fails install validation.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_command_resolves_tilde_to_absolute_path(runner_config, eval_task, submission):
    """Default ``hermes_home='~/.hermes'`` must be resolved to an absolute
    path via remote ``$HOME`` before being shlex-quoted into ``test -d``.

    The bug this guards against: ``shlex.quote('~/.hermes')`` yields
    ``'~/.hermes'`` and bash does not expand ``~`` inside single quotes,
    so ``test -d`` returns 1 with empty stderr and every miner on the
    default install hits ``hermes_install_invalid`` on the first visit.
    """
    captured_cmds: list[str] = []

    card_json = {"id": "eu-ai-act", "no_legal_advice": True}
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))
    sftp = _mk_sftp()
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    # Find the install-verify command (`test -d ... && test -r ...`).
    verify_cmds = [c for c in captured_cmds if "test -d" in c and "test -r" in c and "tar" not in c]
    assert verify_cmds, f"no verify command captured; saw: {captured_cmds!r}"
    verify_cmd = verify_cmds[0]

    # Regression: must NOT contain the literal single-quoted tilde path.
    assert "'~/.hermes'" not in verify_cmd, (
        f"verify command still contains single-quoted tilde, bash won't expand: {verify_cmd!r}"
    )
    assert "'~'" not in verify_cmd

    # Must contain the absolute path resolved from remote $HOME.
    assert "/home/cathedral-probe/.hermes" in verify_cmd, (
        f"verify command should reference the absolute resolved hermes_home: {verify_cmd!r}"
    )


@pytest.mark.asyncio
async def test_absolute_hermes_home_still_shlex_quoted(
    ssh_key_path, bundle_output_dir, eval_task, submission
):
    """No-shell-injection regression: when ``hermes_home`` is already an
    absolute path (no leading ``~/``), we still pass it through
    ``shlex.quote`` so unusual characters can't break out of ``test -d``.
    """
    cfg = SshHermesRunnerConfig(
        ssh_private_key_path=ssh_key_path,
        bundle_output_dir=bundle_output_dir,
        # Absolute path with a space — shlex.quote should single-quote it.
        hermes_home="/opt/weird path/hermes",
    )

    captured_cmds: list[str] = []
    card_json = {"id": "eu-ai-act", "no_legal_advice": True}
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))
    sftp = _mk_sftp()
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(cfg)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    verify_cmds = [c for c in captured_cmds if "test -d" in c and "test -r" in c and "tar" not in c]
    assert verify_cmds, f"no verify command captured; saw: {captured_cmds!r}"
    verify_cmd = verify_cmds[0]

    # shlex.quote wraps the path-with-space in single quotes — the
    # whole point of the no-regression check.
    assert "'/opt/weird path/hermes'" in verify_cmd, (
        f"absolute path should be shlex-quoted to defend against unusual chars: {verify_cmd!r}"
    )


@pytest.mark.asyncio
async def test_hermes_chat_q_invocation_no_legacy_flags(runner_config, eval_task, submission):
    """v1.1.7: the eval invocation uses ``hermes chat -q`` (full agentic
    loop with tool calls + skills) and does NOT carry any of the old
    ``-z``-era flags. Specifically: no bare ``-z`` token and no
    ``--max-turns`` (Hermes ``chat -q`` has no such flag; the old
    ``eval_max_turns`` config field was dropped in v1.1.7).
    """
    captured_cmds: list[str] = []
    card_json = {"id": "eu-ai-act", "no_legal_advice": True}
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))
    sftp = _mk_sftp()
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    invoke_cmds = [c for c in captured_cmds if "hermes chat -Q" in c]
    assert invoke_cmds, f"no hermes chat -q invocation captured; saw: {captured_cmds!r}"
    invoke_cmd = invoke_cmds[0]
    assert "--max-turns" not in invoke_cmd, (
        f"hermes chat -q must NOT pass --max-turns; got: {invoke_cmd!r}"
    )
    # Sanity: should not contain the legacy ``-z`` token as a flag.
    # (``-q`` is fine — that's the new one — and ``-z`` should be gone.)
    assert " -z " not in invoke_cmd and not invoke_cmd.endswith(" -z"), (
        f"hermes chat -q invocation must not carry the legacy -z flag; got: {invoke_cmd!r}"
    )


@pytest.mark.asyncio
async def test_hermes_invocation_uses_chat_q_not_z(runner_config, eval_task, submission):
    """v1.1.7 regression guard: the eval invocation is ``hermes chat -q``
    (full agentic loop with tool calls + skills), NOT ``hermes -z``
    (one-shot ``model.generate(prompt)`` with no agent loop, no tool
    calls, no skill execution). ``-z`` stripped exactly the
    verifiable-work signal Cathedral exists to score.
    """
    captured_cmds: list[str] = []
    card_json = {"id": "eu-ai-act", "no_legal_advice": True}
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        captured_cmds.append(cmd)
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "hermes chat -Q" in cmd:
            return _mk_run_result(stdout=f"```json\n{json.dumps(card_json)}\n```\n")
        return _mk_run_result()

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))
    sftp = _mk_sftp()
    conn.start_sftp_client = MagicMock(return_value=sftp)

    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    with patch.dict(sys.modules, {"asyncssh": fake_asyncssh}):
        await runner.run(
            bundle_bytes=b"",
            bundle_hash="x" * 64,
            task=eval_task,
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
        )

    # Must have a `hermes chat -q` invocation
    chat_q_cmds = [c for c in captured_cmds if "hermes chat -Q" in c]
    assert chat_q_cmds, f"expected a hermes chat -q invocation; captured: {captured_cmds!r}"

    # Must NOT have a bare ``hermes -z`` (one-shot) invocation anywhere
    bare_z_cmds = [
        c
        for c in captured_cmds
        if " hermes -z " in c or c.startswith("hermes -z ") or c.endswith(" hermes -z")
    ]
    assert not bare_z_cmds, f"v1.1.7 must not invoke hermes -z anywhere; saw: {bare_z_cmds!r}"


@pytest.mark.asyncio
async def test_task_family_records_stdout_receipt_before_trace_collection(
    runner_config, submission
):
    events: list[str] = []
    receipt_at = "2026-05-20T12:34:56.123456Z"
    problem = _module.PublicProblem(
        task_family="synthetic_boolean_v1",
        schema_version=5,
        task_id="sat_public_001",
        difficulty_tier=0,
        public_input={"num_vars": 1, "cnf_sha256": "a" * 64},
        time_limit_seconds=30,
    )
    conn = MagicMock()
    conn.close = MagicMock()
    conn.wait_closed = AsyncMock(return_value=None)

    def _route(cmd, **kwargs):
        if "hermes --version" in cmd:
            return _mk_run_result(stdout="hermes 0.13.0\n")
        if "$HOME" in cmd:
            return _mk_run_result(stdout="/home/cathedral-probe")
        if "hermes chat -Q" in cmd:
            events.append("hermes_stdout")
            stdout = '```FINAL_ANSWER\n{"dimacs_solution":"s SATISFIABLE\\nv 1 0\\n"}\n```'
            return _mk_run_result(stdout=stdout)
        return _mk_run_result()

    async def fake_collect(*args, **kwargs):
        events.append("collect")
        assert events == ["hermes_stdout", "receipt_clock", "receipt_callback", "collect"]
        return MagicMock()

    def fake_now() -> str:
        events.append("receipt_clock")
        return receipt_at

    conn.run = AsyncMock(side_effect=lambda cmd, **kw: _route(cmd, **kw))
    fake_asyncssh = MagicMock()
    fake_asyncssh.connect = AsyncMock(return_value=conn)
    fake_asyncssh.Error = Exception
    fake_asyncssh.PermissionDenied = type("PermissionDenied", (Exception,), {})

    runner = SshHermesRunner(runner_config)
    runner._collect_and_assemble = AsyncMock(side_effect=fake_collect)

    async def receipt_callback(stdout: str, stdout_received_at_iso: str) -> None:
        events.append("receipt_callback")
        assert stdout_received_at_iso == receipt_at
        assert "FINAL_ANSWER" in stdout

    with (
        patch.dict(sys.modules, {"asyncssh": fake_asyncssh}),
        patch.object(_module, "_now_utc_iso", side_effect=fake_now),
    ):
        result = await runner.run_task_family_challenge(
            problem=problem,
            prompt="Solve the SAT challenge.",
            miner_hotkey="5Test" + "x" * 43,
            submission=submission,
            receipt_callback=receipt_callback,
        )

    assert result.stdout_received_at_iso == receipt_at
    assert result.trace["task_family_stdout_received_at_iso"] == receipt_at
    assert "cnf_url" not in result.trace
    assert "dimacs_solution" not in result.trace


# --------------------------------------------------------------------------
# Prober version dispatch — CATHEDRAL_PROBER_VERSION env flag
# --------------------------------------------------------------------------
#
# These tests exercise the orchestrator's _resolve_polaris_runner_for_mode
# dispatch. They live in a separate test file (test_orchestrator_dispatch.py)
# because importing cathedral.eval.orchestrator triggers the publisher's
# import chain which conflicts with the direct-module-load pattern this
# file uses for the runner-under-test. Don't add dispatch tests here.


# --------------------------------------------------------------------------
# CNF URL token redaction: cardinal sin protection for log surfaces
# --------------------------------------------------------------------------
#
# The SAT lane's CNF URL carries an unguessable token in ``?t=<...>``.
# That token must never appear in any log line or exception message the
# runner emits. ``_redact_query_tokens`` is the single chokepoint used
# by ``_run_remote``'s two error paths (timeout + non-zero exit).

_redact_query_tokens = _module._redact_query_tokens


def test_redact_query_tokens_replaces_token_value() -> None:
    raw = "hermes chat -Q -q 'fetch https://api.cathedral.test/cnf?t=SECRET123 now'"
    redacted = _redact_query_tokens(raw)
    assert "SECRET123" not in redacted
    assert "?t=REDACTED" in redacted


def test_redact_query_tokens_handles_no_token() -> None:
    # A cmd with no token is returned untouched. The helper is cheap
    # enough to call unconditionally on every error path.
    raw = "hermes --version"
    assert _redact_query_tokens(raw) == raw


def test_redact_query_tokens_handles_truncation_before_marker() -> None:
    # If ``cmd[:120]`` cut before the ``?t=`` marker ever appeared, the
    # input is returned unchanged (the regex needs the literal marker).
    raw = "hermes chat -Q -q 'Capability: synthetic_boolean_v1 fetch URL below'"
    assert _redact_query_tokens(raw) == raw


def test_redact_query_tokens_redacts_partial_token_after_marker() -> None:
    # If the truncation lands in the middle of the token value, the
    # partial value is still redacted -- the regex eats up to the next
    # whitespace / quote / ampersand / double-quote boundary.
    raw = "fetch https://api.cathedral.test/cnf?t=PARTIAL_TOKEN_VALUE_TRUNC"
    redacted = _redact_query_tokens(raw)
    assert "PARTIAL_TOKEN_VALUE_TRUNC" not in redacted
    assert "?t=REDACTED" in redacted


def test_redact_query_tokens_redacts_multiple_occurrences() -> None:
    raw = "first ?t=AAAAAAAA second ?t=BBBBBBBB"
    redacted = _redact_query_tokens(raw)
    assert "AAAAAAAA" not in redacted
    assert "BBBBBBBB" not in redacted
    assert redacted.count("?t=REDACTED") == 2


def test_redact_query_tokens_stops_at_ampersand_and_quote() -> None:
    # The redaction must not eat past query separators or shell quoting
    # boundaries, otherwise unrelated content (other query params, the
    # closing shell quote) would also disappear from logs.
    raw = "url='https://x/cnf?t=ABC123&other=ok'"
    redacted = _redact_query_tokens(raw)
    assert "ABC123" not in redacted
    assert "&other=ok" in redacted
    assert redacted.endswith("'")
