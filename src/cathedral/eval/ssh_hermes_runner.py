# ruff: noqa: ASYNC240
# ASYNC240 (use trio.Path / anyio.path in async functions) is opt-out:
# Cathedral is asyncio-based, not trio/anyio. Local FS ops during bundle
# assembly are sub-millisecond and not a blocking concern for the
# orchestrator's event loop.
"""SSH + Hermes CLI prober (cathedralai/cathedral#75, PR 2).

The v1.0.x ``SshProbeRunner`` assumed the miner ran an HTTP server
exposing ``/healthz`` + ``/chat``. That assumption was wrong: Hermes
is CLI-shaped, not HTTP-shaped. The ``cathedral-runtime`` Docker image
existed solely to wrap Hermes in a custom HTTP shim. This runner is
the canonical replacement.

Per-eval lifecycle (``docs/HERMES.md`` § L.1):

1. SSH in as ``ssh_user@ssh_host:ssh_port`` using the platform-wide
   prober key (``/.well-known/cathedral-ssh-key.pub``).
2. ``hermes --version`` — verify Hermes is installed.
3. ``hermes profile create cathedral-eval-<round> --clone-all`` — full
   copy of the miner's primary profile via Hermes's own copytree.
   Replaces the dossier's "backup + restore from zip" sketch: per
   ``hermes_cli/profiles.py:540-617`` ``--clone-all`` is the
   canonical way to fork a profile and is faster than the zip
   roundtrip.
4. Issue ``HERMES_HOME=<eval_profile> hermes chat -q -Q "<task>"``.
   ``hermes chat -q`` runs the full agentic loop (tool calls, skill
   execution, multiple model turns, memory reads) and writes the
   full forensic trail (session JSON, request dumps, SQLite rows) to
   the eval profile (``docs/HERMES.md`` § A.1). v1.1.7 switched from
   ``hermes -z`` (one-shot, no agentic loop) — see PR for context.
5. Snapshot the eval profile's ``state.db`` via SQLite's backup() API
   over SSH (matches ``hermes_cli/backup.py:_safe_copy_db`` — consistent
   WAL snapshot without explicit ``PRAGMA wal_checkpoint(TRUNCATE)``).
6. SCP back: ``state.db`` snapshot, ``sessions/session_<id>.json``,
   every ``sessions/request_dump_<id>_*.json``, ``memories/``,
   ``skills/``, ``logs/agent.log`` + ``errors.log``.
7. Assemble local trace bundle (tar.gz on the Cathedral-side host).
   Compute per-file sha256 + bundle blake3. Build the manifest
   (``v1.cathedral.eval.manifest``).
8. Proof-of-loop checks: count tool_calls / api_call / request_dump
   counts, verify the assembled system_prompt includes SOUL.md +
   AGENTS.md + MEMORY.md.
9. ``hermes profile delete cathedral-eval-<round>`` — tear down.
   Primary profile untouched throughout.

The runner returns a ``PolarisRunResult`` whose ``output_card_json``
is the parsed Card from ``hermes chat -q`` stdout. **The trace bundle is
written to local disk** (``self.config.bundle_output_dir``) and the
runner does NOT upload it — PR 3 (Hippius adapter) handles upload.
The bundle path is returned via the ``trace`` field for the
orchestrator to forward.

Gated behind ``CATHEDRAL_PROBER_VERSION=v2``. v1 (the legacy
``SshProbeRunner``) remains the default until we smoke-test v2 on
the rented Polaris box.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import re
import shlex
import tarfile
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import blake3
import structlog

from cathedral.eval.polaris_runner import (
    PolarisRunnerError,
    PolarisRunResult,
)
from cathedral.lanes.contract import PublicProblem
from cathedral.v1_types import EvalTask
from cathedral.v3.claim_extraction import (
    ClaimExtractionError,
    extract_claim,
    is_repair_worthy,
)
from cathedral.v3.corpus.schema import ChallengeRow
from cathedral.v3.prompts import (
    build_bug_isolation_prompt,
    build_bug_isolation_repair_prompt,
)

logger = structlog.get_logger(__name__)


# Redact `?t=<token>` query values before any cmd / prompt string lands
# in a log line or error message. The synthetic_boolean_v1 CNF URL is
# the only token-bearing query string Cathedral currently mints, but
# the pattern is shape-only: any `?t=` value is redacted regardless of
# where it came from. Cardinal sin per the CNF URL transport: the
# token must never appear in logs or in exception messages that may
# get forwarded to operators or third-party log sinks.
_QUERY_TOKEN_RE = re.compile(r"(\?t=)[^\s'&\"]+")
# Binary trace artifacts can be SQLite pages where a bare URL text field is
# followed immediately by NUL/control/record bytes. Use a token-character
# allowlist here instead of "anything until whitespace" so redaction cannot
# consume and rewrite non-token database bytes.
_QUERY_TOKEN_BYTES_RE = re.compile(rb"(\?t=)([A-Za-z0-9._~%+-]+)")


def _redact_query_tokens(s: str) -> str:
    """Replace any ``?t=<value>`` substring with ``?t=REDACTED``.

    Safe to call on partial truncations (`cmd[:120]`) because the
    regex only fires when the literal ``?t=`` marker is present. If
    the truncation cut before the token, the input is returned
    unchanged. If the token straddles the truncation boundary, the
    partial token value is still redacted.
    """
    return _QUERY_TOKEN_RE.sub(r"\1REDACTED", s)


def _redact_query_token_bytes(blob: bytes) -> bytes:
    """Redact token values without changing byte length.

    Trace artifacts include SQLite snapshots and JSON logs. Keeping replacement
    lengths stable lets us scrub token bytes in binary SQLite pages without
    shifting offsets or corrupting the file structure before bundling.
    """

    def replacement(match: re.Match[bytes]) -> bytes:
        token = match.group(2)
        marker = b"REDACTED"
        if len(token) <= len(marker):
            redacted = b"X" * len(token)
        else:
            redacted = marker + (b"X" * (len(token) - len(marker)))
        return match.group(1) + redacted

    return _QUERY_TOKEN_BYTES_RE.sub(replacement, blob)


def _redact_query_tokens_in_artifact_tree(root: Path) -> None:
    """Scrub CNF fetch tokens from local files before trace publication."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if _is_tar_gz_artifact(path):
            _redact_query_tokens_in_tar_gz(path)
            continue
        # Do not byte-edit unknown compressed archives in place: changing
        # compressed bytes would invalidate container checksums. Known Hermes
        # tarballs are handled above by unpacking and repacking each member.
        if path.suffix in {".gz", ".zip"}:
            continue
        try:
            original = path.read_bytes()
        except OSError:
            continue
        redacted = _redact_query_token_bytes(original)
        if redacted == original:
            continue
        path.write_bytes(redacted)


def _is_tar_gz_artifact(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".tar.gz") or name.endswith(".tgz")


def _redact_query_tokens_in_tar_gz(path: Path) -> None:
    """Repack a tar.gz artifact after redacting token-bearing file members.

    Hermes skills are collected as ``skills.tar.gz``. They can contain prompt
    copies or logs, so skipping compressed archives would reintroduce CNF token
    leaks into the final public trace bundle.
    """
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = tmp.name
        with tarfile.open(path, "r:gz") as source, tarfile.open(tmp_path, "w:gz") as target:
            for member in source.getmembers():
                out_member = copy.copy(member)
                if not member.isfile():
                    target.addfile(out_member)
                    continue
                extracted = source.extractfile(member)
                if extracted is None:
                    out_member.size = 0
                    target.addfile(out_member, io.BytesIO(b""))
                    continue
                redacted = _redact_query_token_bytes(extracted.read())
                out_member.size = len(redacted)
                target.addfile(out_member, io.BytesIO(redacted))
        os.replace(tmp_path, path)
        tmp_path = None
    except (tarfile.TarError, OSError) as exc:
        logger.warning(
            "ssh_hermes_trace_archive_redaction_failed",
            path=str(path),
            error=str(exc),
        )
    finally:
        if tmp_path is not None:
            with suppress(FileNotFoundError):
                os.unlink(tmp_path)


def _now_utc_iso() -> str:
    # SAT receipt ordering uses SQLite text comparison, so callback
    # timestamps must use the same fixed-width millisecond-Z form as the
    # fallback and reconciler paths.
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Failure codes
# --------------------------------------------------------------------------

# Subset of the SSH probe codes that still apply, plus new ones for
# the CLI invocation path. Old codes that no longer happen (``hermes_unhealthy``,
# ``package_failed``) are intentionally absent. New codes:
#   * ``hermes_install_invalid``: `~/.hermes/` missing / unreadable
#   * ``hermes_invocation_failed``: `hermes chat -q` exited non-zero
#   * ``hermes_output_malformed``: stdout didn't parse as a Card JSON
#   * ``profile_clone_failed``: `hermes profile create --clone-all` failed
#   * ``bundle_assembly_failed``: client-side tar.gz / hash step failed
SSH_HERMES_FAILURE_CODES: tuple[str, ...] = (
    "connect_refused",
    "auth_failed",
    "hermes_not_found",
    "hermes_install_invalid",
    "hermes_invocation_failed",
    "hermes_output_malformed",
    "hermes_stdout_oversized",
    "profile_clone_failed",
    "prompt_timeout",
    "transfer_failed",
    "bundle_assembly_failed",
    "disconnect_dirty",
    "config_invalid",
)


DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES = 8 * 1024 * 1024
_SSH_STDOUT_READ_CHUNK_BYTES = 64 * 1024


class SshHermesError(PolarisRunnerError):
    """Top-level SSH+Hermes failure with one of ``SSH_HERMES_FAILURE_CODES``."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in SSH_HERMES_FAILURE_CODES:
            raise ValueError(f"unknown ssh-hermes code: {code!r}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SshHermesRunnerConfig:
    """Configuration for ``SshHermesRunner``.

    ``ssh_private_key_path`` is the platform-wide private key whose
    matching public key the miner installs in their ``ssh_user``'s
    ``~/.ssh/authorized_keys``. Same key, all miners.

    ``bundle_output_dir`` is the local directory where assembled
    trace bundles land. PR 3's Hippius adapter reads from this dir.
    """

    ssh_private_key_path: str
    bundle_output_dir: str
    connect_timeout_secs: float = 10.0
    # Bumped 120s -> 300s in v1.1.7 to give the ``hermes chat -q`` agentic
    # loop room. One-shot ``hermes -z`` was fast (model.generate once);
    # the full loop has to think, call tools (URL fetch, BLAKE3 hash,
    # etc.), wait for tool results, and think again, so 120s was tight.
    eval_timeout_secs: float = 300.0
    transfer_timeout_secs: float = 120.0
    connect_retries: int = 3
    connect_retry_initial_secs: float = 1.0
    # Path on the miner box where Hermes lives. Almost always
    # ``~/.hermes`` per Hermes's defaults (``hermes_cli/profiles.py``).
    # If this starts with ``~/`` we resolve it to an absolute path at
    # session start by querying ``$HOME`` on the miner box, so that
    # downstream ``shlex.quote`` calls and SFTP paths see a real
    # filesystem path (bash does NOT expand ``~`` inside single quotes
    # and SFTP does no expansion at all).
    hermes_home: str = "~/.hermes"
    # Pinned model + provider for the eval invocation. Set via env on
    # the orchestrator side so all evals against a given card use the
    # same model — neutralizes one source of cross-miner variance.
    pinned_model: str | None = None
    pinned_provider: str | None = None
    # SAT miners control Hermes stdout. Keep task-family answers bounded
    # before receipt hashing and DIMACS parsing run in the publisher process.
    task_family_stdout_limit_bytes: int = DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES


# --------------------------------------------------------------------------
# Manifest shape (sketched in issue #75 PR 2 status comment)
# --------------------------------------------------------------------------


_MANIFEST_VERSION = 1


@dataclass
class ManifestFile:
    """One entry in the trace bundle manifest."""

    path: str
    sha256: str
    byte_length: int
    content_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "content_type": self.content_type,
        }


@dataclass
class ProofOfLoop:
    """Denormalized "did the agent actually run the full Hermes loop"
    summary, derived from the SQLite slice + request dump files.
    Cathedral's scorer reads this for cheap spot-checks; the
    underlying truth still lives in ``state.db`` + ``request_dump_*.json``.
    """

    session_id: str | None = None
    tool_call_count: int = 0
    api_call_count: int = 0
    request_dump_file_count: int = 0
    system_prompt_includes_soul_md: bool = False
    system_prompt_includes_agents_md: bool = False
    system_prompt_includes_memory_md: bool = False
    tool_calls_observed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tool_call_count": self.tool_call_count,
            "api_call_count": self.api_call_count,
            "request_dump_file_count": self.request_dump_file_count,
            "system_prompt_includes_soul_md": self.system_prompt_includes_soul_md,
            "system_prompt_includes_agents_md": self.system_prompt_includes_agents_md,
            "system_prompt_includes_memory_md": self.system_prompt_includes_memory_md,
            "tool_calls_observed": self.tool_calls_observed,
        }


@dataclass
class TraceBundle:
    """Local artifact produced by the runner. PR 3 picks it up by path."""

    eval_id: str
    submission_id: str
    cathedral_eval_round: str
    bundle_tar_path: Path
    manifest: dict[str, Any]
    # blake3 hex of the tar.gz blob. This is the value PR 4 will sign
    # as `eval_artifact_manifest_hash`'s underlying anchor (after
    # being included inside the manifest, then the manifest's
    # canonical-JSON blake3 becomes the signed-payload field).
    bundle_blake3: str


# --------------------------------------------------------------------------
# Visit trace (mirrors SshProbeRunner.VisitTrace for orchestrator parity)
# --------------------------------------------------------------------------


@dataclass
class HermesVisitTrace:
    visit_started_at: str
    visit_ended_at: str | None = None
    hermes_version: str | None = None
    eval_profile_name: str | None = None
    invocation_duration_ms: int | None = None
    files_collected: list[str] = field(default_factory=list)
    bundle_path: str | None = None
    bundle_blake3: str | None = None
    proof_of_loop: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "visit_started_at": self.visit_started_at,
            "visit_ended_at": self.visit_ended_at,
            "hermes_version": self.hermes_version,
            "eval_profile_name": self.eval_profile_name,
            "invocation_duration_ms": self.invocation_duration_ms,
            "files_collected": self.files_collected,
            "bundle_path": self.bundle_path,
            "bundle_blake3": self.bundle_blake3,
            "proof_of_loop": self.proof_of_loop or {},
            "tier": "ssh-hermes",
        }


@dataclass
class BugIsolationHermesRun:
    """Raw Hermes result for one bug_isolation_v1 challenge."""

    stdout: str
    repair_stdout: str | None = None
    duration_ms: int = 0
    trace: dict[str, Any] = field(default_factory=dict)
    trace_bundle: TraceBundle | None = None


@dataclass
class TaskFamilyHermesRun:
    """Raw Hermes result for one Task Family challenge."""

    stdout: str
    stdout_received_at_iso: str | None = None
    duration_ms: int = 0
    trace: dict[str, Any] = field(default_factory=dict)
    trace_bundle: TraceBundle | None = None


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


class SshHermesRunner:
    """v1.1.0 prober. CLI-shaped, ``hermes chat -q`` over SSH (v1.1.7).

    The miner registers ``ssh_host``, ``ssh_port``, ``ssh_user`` with
    their submission (``hermes_port`` is deprecated in v1.1.0 per
    PR #77). Cathedral SSHs in, runs Hermes natively, captures the
    full forensic trail, and assembles a signed bundle.

    Returns a ``PolarisRunResult`` shaped the same as
    ``SshProbeRunner.run`` so the orchestrator + storage layer don't
    have to branch on which v2-tier runner produced the result. The
    new bits ride in ``trace`` (the ``HermesVisitTrace`` dict).
    """

    def __init__(self, config: SshHermesRunnerConfig) -> None:
        self.config = config
        if not os.path.isfile(config.ssh_private_key_path):
            raise SshHermesError(
                "config_invalid",
                f"ssh_private_key_path does not exist: {config.ssh_private_key_path}",
            )
        out_dir = Path(config.bundle_output_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(out_dir, os.W_OK):
            raise SshHermesError(
                "config_invalid",
                f"bundle_output_dir not writable: {out_dir}",
            )

    async def run(
        self,
        *,
        bundle_bytes: bytes,  # unused — miner runs Hermes themselves
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_bytes  # quiet linter
        if submission is None:
            raise SshHermesError("config_invalid", "submission required for ssh-hermes")

        ssh_host = submission.get("ssh_host")
        ssh_port = submission.get("ssh_port") or 22
        ssh_user = submission.get("ssh_user")
        if not (ssh_host and ssh_user):
            raise SshHermesError(
                "config_invalid",
                "submission missing ssh_host/ssh_user "
                f"(ssh_host={bool(ssh_host)} ssh_user={bool(ssh_user)})",
            )

        run_id = str(uuid.uuid4())
        eval_round = f"{task.card_id}-{task.epoch}-{task.round_index}-{run_id[:8]}"
        eval_profile = f"cathedral-eval-{eval_round}"
        trace = HermesVisitTrace(visit_started_at=datetime.now(UTC).isoformat())
        t_start = time.monotonic()

        # asyncssh imported lazily so this module can be imported on
        # validator boxes that don't run probes themselves.
        import asyncssh

        conn = await self._connect_with_retries(
            asyncssh,
            host=ssh_host,
            port=int(ssh_port),
            username=ssh_user,
            miner_hotkey=miner_hotkey,
        )
        profile_created = False
        profile_deleted = False
        try:
            # 1. hermes --version (also confirms binary on PATH)
            hermes_version = await self._hermes_version(conn)
            trace.hermes_version = hermes_version

            # Resolve ``hermes_home`` to an absolute path once. Bash does
            # not expand ``~`` inside single quotes (which is what
            # ``shlex.quote`` produces) and SFTP performs no expansion at
            # all, so every downstream consumer needs a real path.
            resolved_home = await self._resolve_hermes_home(conn)

            # 2. Validate the Hermes install
            await self._verify_hermes_install(conn, resolved_home)

            # 3. Snapshot the primary profile into the eval profile
            await self._clone_profile(conn, eval_profile)
            profile_created = True
            trace.eval_profile_name = eval_profile

            # 4. Run `hermes chat -q`. Captures stdout (= Card JSON) and
            #    the eval profile's full forensic trail (including the
            #    agentic loop's tool calls + skill executions) to disk
            #    on miner box.
            t_invoke = time.monotonic()
            # Build a fully-formed task prompt that:
            #   (a) carries the deterministic task question, and
            #   (b) carries the source_pool URLs so the agent fetches
            #       through skills instead of hallucinating citations from
            #       training data.
            sources_block = ""
            if task.sources:
                src_lines = []
                for s in task.sources:
                    # Each Source has alias='class' but field name class_.
                    cls = getattr(s, "class_", None) or getattr(s, "source_class", None) or "other"
                    cls_val = cls.value if hasattr(cls, "value") else str(cls)
                    src_lines.append(f"- {s.url}  [class: {cls_val}]")
                sources_block = (
                    "\n\nSource pool to fetch via the fetch_url skill "
                    "(use each, do not invent URLs):\n" + "\n".join(src_lines)
                )
            enriched_prompt = task.prompt + sources_block

            card_json, hermes_stdout = await self._invoke_hermes(
                conn,
                eval_profile=eval_profile,
                prompt=enriched_prompt,
                eval_round=eval_round,
                resolved_home=resolved_home,
            )
            trace.invocation_duration_ms = int((time.monotonic() - t_invoke) * 1000)

            # 5-7. Pull the trace artifacts back; assemble bundle locally.
            bundle = await self._collect_and_assemble(
                conn,
                eval_profile=eval_profile,
                eval_id=run_id,
                submission_id=str(submission["id"]),
                eval_round=eval_round,
                hermes_version=hermes_version,
                card_json=card_json,
                hermes_stdout=hermes_stdout,
                resolved_home=resolved_home,
            )
            trace.files_collected = [f["path"] for f in bundle.manifest["files"]]
            trace.bundle_path = str(bundle.bundle_tar_path)
            trace.bundle_blake3 = bundle.bundle_blake3
            trace.proof_of_loop = bundle.manifest["proof_of_loop"]

            # 8. Tear down the eval profile. Primary profile untouched.
            await self._delete_profile(conn, eval_profile)
            profile_deleted = True

            trace.visit_ended_at = datetime.now(UTC).isoformat()

            return PolarisRunResult(
                polaris_agent_id=f"ssh-hermes:{miner_hotkey[:12]}",
                polaris_run_id=run_id,
                output_card_json=card_json,
                duration_ms=int((time.monotonic() - t_start) * 1000),
                errors=[],
                attestation=None,  # no Polaris attestation on free tier
                probe_attestation=None,  # no signed envelope on this path
                trace=trace.to_dict(),
                manifest=None,  # PR 3 sets the signed Hippius manifest URL
                trace_bundle=bundle,  # PR 5: orchestrator picks this up
            )

        except SshHermesError as exc:
            trace.visit_ended_at = datetime.now(UTC).isoformat()
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                    profile_deleted = True
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            return PolarisRunResult(
                polaris_agent_id=f"ssh-hermes:{miner_hotkey[:12]}",
                polaris_run_id=run_id,
                output_card_json={
                    "id": task.card_id,
                    "_ssh_hermes_failed": True,
                    "failure_code": exc.code,
                    "failure_detail": exc.detail[:512],
                },
                duration_ms=int((time.monotonic() - t_start) * 1000),
                errors=[f"{exc.code}: {exc.detail}"],
                attestation=None,
                probe_attestation=None,
                trace=trace.to_dict(),
                manifest=None,
            )
        finally:
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            try:
                conn.close()
                await conn.wait_closed()
            except Exception as close_exc:
                logger.debug("ssh_hermes_close_error", error=str(close_exc))

    async def run_bug_isolation_challenge(
        self,
        *,
        challenge: ChallengeRow,
        miner_hotkey: str,
        submission: dict[str, Any],
    ) -> BugIsolationHermesRun:
        """Run one bug_isolation_v1 prompt over SSH Hermes.

        This is the v3 publisher transport. It deliberately returns raw
        stdout instead of parsing or scoring. The publisher owns parse,
        score, sign, and persistence so the hidden oracle never leaves
        the publisher process.
        """
        ssh_host = submission.get("ssh_host")
        ssh_port = submission.get("ssh_port") or 22
        ssh_user = submission.get("ssh_user")
        if not (ssh_host and ssh_user):
            raise SshHermesError(
                "config_invalid",
                "submission missing ssh_host/ssh_user "
                f"(ssh_host={bool(ssh_host)} ssh_user={bool(ssh_user)})",
            )

        run_id = str(uuid.uuid4())
        eval_round = f"bug-isolation-{challenge.id}-{run_id[:8]}"
        eval_profile = f"cathedral-eval-{eval_round}"
        trace = HermesVisitTrace(visit_started_at=datetime.now(UTC).isoformat())
        t_start = time.monotonic()

        import asyncssh

        conn = await self._connect_with_retries(
            asyncssh,
            host=ssh_host,
            port=int(ssh_port),
            username=ssh_user,
            miner_hotkey=miner_hotkey,
        )
        profile_created = False
        profile_deleted = False
        try:
            hermes_version = await self._hermes_version(conn)
            trace.hermes_version = hermes_version
            resolved_home = await self._resolve_hermes_home(conn)
            await self._verify_hermes_install(conn, resolved_home)
            await self._clone_profile(conn, eval_profile)
            profile_created = True
            trace.eval_profile_name = eval_profile

            t_invoke = time.monotonic()
            stdout = await self._invoke_hermes_text(
                conn,
                eval_profile=eval_profile,
                prompt=build_bug_isolation_prompt(challenge),
                eval_round=eval_round,
                resolved_home=resolved_home,
            )
            trace.invocation_duration_ms = int((time.monotonic() - t_invoke) * 1000)

            # One-shot repair: only when the first stdout had no parseable
            # JSON at all (not when JSON came back with the wrong shape).
            public_view = challenge.public_view()
            challenge_id_public = public_view["challenge_id"]
            repair_stdout: str | None = None
            try:
                extract_claim(stdout)
            except ClaimExtractionError as exc:
                if is_repair_worthy(exc):
                    repair_stdout = await self._invoke_hermes_text(
                        conn,
                        eval_profile=eval_profile,
                        prompt=build_bug_isolation_repair_prompt(challenge_id_public),
                        eval_round=eval_round,
                        resolved_home=resolved_home,
                    )

            synthetic_card: dict[str, Any] = {
                "task_type": "bug_isolation_v1",
                "challenge_id_public": challenge_id_public,
            }
            extra: dict[str, str] | None = (
                {"hermes_repair_stdout.txt": repair_stdout} if repair_stdout is not None else None
            )
            bundle = await self._collect_and_assemble(
                conn,
                eval_profile=eval_profile,
                eval_id=run_id,
                submission_id=str(submission["id"]),
                eval_round=eval_round,
                hermes_version=hermes_version,
                card_json=synthetic_card,
                hermes_stdout=stdout,
                resolved_home=resolved_home,
                extra_files=extra,
            )

            trace.visit_ended_at = datetime.now(UTC).isoformat()
            await self._delete_profile(conn, eval_profile)
            profile_deleted = True
            return BugIsolationHermesRun(
                stdout=stdout,
                repair_stdout=repair_stdout,
                duration_ms=int((time.monotonic() - t_start) * 1000),
                trace=trace.to_dict(),
                trace_bundle=bundle,
            )
        except SshHermesError:
            trace.visit_ended_at = datetime.now(UTC).isoformat()
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                    profile_deleted = True
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            raise
        finally:
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            try:
                conn.close()
                await conn.wait_closed()
            except Exception as close_exc:
                logger.debug("ssh_hermes_close_error", error=str(close_exc))

    async def run_task_family_challenge(
        self,
        *,
        problem: PublicProblem,
        prompt: str,
        miner_hotkey: str,
        submission: dict[str, Any],
        receipt_callback: Any | None = None,
    ) -> TaskFamilyHermesRun:
        """Run one generic Task Family prompt over SSH Hermes.

        Returns raw stdout plus the trace bundle. The publisher owns answer
        extraction, deterministic verification, scoring, signing, and
        persistence.
        """
        ssh_host = submission.get("ssh_host")
        ssh_port = submission.get("ssh_port") or 22
        ssh_user = submission.get("ssh_user")
        if not (ssh_host and ssh_user):
            raise SshHermesError(
                "config_invalid",
                "submission missing ssh_host/ssh_user "
                f"(ssh_host={bool(ssh_host)} ssh_user={bool(ssh_user)})",
            )

        run_id = str(uuid.uuid4())
        task_slug = blake3.blake3(problem.task_id.encode("utf-8")).hexdigest()[:12]
        eval_round = f"{problem.task_family}-{task_slug}-{run_id[:8]}"
        eval_profile = f"cathedral-eval-{eval_round}"
        trace = HermesVisitTrace(visit_started_at=datetime.now(UTC).isoformat())
        t_start = time.monotonic()

        import asyncssh

        conn = await self._connect_with_retries(
            asyncssh,
            host=ssh_host,
            port=int(ssh_port),
            username=ssh_user,
            miner_hotkey=miner_hotkey,
        )
        profile_created = False
        profile_deleted = False
        receipt_committed = False
        try:
            hermes_version = await self._hermes_version(conn)
            trace.hermes_version = hermes_version
            resolved_home = await self._resolve_hermes_home(conn)
            await self._verify_hermes_install(conn, resolved_home)
            await self._clone_profile(conn, eval_profile)
            profile_created = True
            trace.eval_profile_name = eval_profile

            t_invoke = time.monotonic()
            stdout = await self._invoke_hermes_text(
                conn,
                eval_profile=eval_profile,
                prompt=prompt,
                eval_round=eval_round,
                resolved_home=resolved_home,
                # SAT receipts are ordered by first valid answer, so the
                # Hermes call must use the challenge's advertised wall-clock
                # budget instead of the runner-wide default.
                timeout_secs=float(problem.time_limit_seconds),
                # This path handles miner-controlled SAT answers. The SSH
                # process is streamed with this cap, so oversized output is
                # killed before receipt processing or DIMACS parsing can
                # allocate on the full blob.
                max_stdout_bytes=self.config.task_family_stdout_limit_bytes,
            )
            stdout_received_at_iso = _now_utc_iso()
            if receipt_callback is not None:
                await receipt_callback(stdout, stdout_received_at_iso)
                receipt_committed = True
            trace.invocation_duration_ms = int((time.monotonic() - t_invoke) * 1000)

            synthetic_card: dict[str, Any] = {
                "task_type": problem.task_family,
                "task_id": problem.task_id,
            }
            try:
                bundle = await self._collect_and_assemble(
                    conn,
                    eval_profile=eval_profile,
                    eval_id=run_id,
                    submission_id=str(submission["id"]),
                    eval_round=eval_round,
                    hermes_version=hermes_version,
                    card_json=synthetic_card,
                    hermes_stdout=stdout,
                    resolved_home=resolved_home,
                )
            except Exception as exc:
                if not receipt_committed:
                    raise
                # SAT ordering is based on stdout receipt time. Once the
                # receipt callback commits a verifying row, post-stdout trace
                # collection must not make a valid first answer disappear.
                # Return a degraded trace so the orchestrator can still score
                # and finalize the captured stdout.
                trace.visit_ended_at = datetime.now(UTC).isoformat()
                failure_code = (
                    exc.code if isinstance(exc, SshHermesError) else "trace_collection_failed"
                )
                logger.warning(
                    "ssh_hermes_task_family_trace_collection_failed_after_receipt",
                    eval_profile=eval_profile,
                    failure_code=failure_code,
                    error=str(exc)[:300],
                )
                if profile_created and not profile_deleted:
                    try:
                        await self._delete_profile(conn, eval_profile)
                        profile_deleted = True
                    except Exception as cleanup_exc:
                        logger.warning(
                            "ssh_hermes_cleanup_failed",
                            eval_profile=eval_profile,
                            error=str(cleanup_exc),
                        )
                trace_dict = trace.to_dict()
                trace_dict["task_family_stdout_received_at_iso"] = stdout_received_at_iso
                trace_dict["trace_collection_failed_after_receipt"] = True
                trace_dict["trace_collection_failure_code"] = failure_code
                return TaskFamilyHermesRun(
                    stdout=stdout,
                    stdout_received_at_iso=stdout_received_at_iso,
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    trace=trace_dict,
                    trace_bundle=None,
                )

            trace.visit_ended_at = datetime.now(UTC).isoformat()
            cleanup_failed_after_receipt = False
            try:
                await self._delete_profile(conn, eval_profile)
                profile_deleted = True
            except Exception as cleanup_exc:
                if not receipt_committed:
                    raise
                # SAT receipts are already committed before trace/profile
                # teardown. Cleanup must stay best-effort here so a valid
                # first answer is not lost because the miner box rejected
                # profile deletion after stdout was captured.
                cleanup_failed_after_receipt = True
                logger.warning(
                    "ssh_hermes_cleanup_failed_after_task_family_receipt",
                    eval_profile=eval_profile,
                    error=str(cleanup_exc),
                )
            trace_dict = trace.to_dict()
            trace_dict["task_family_stdout_received_at_iso"] = stdout_received_at_iso
            if cleanup_failed_after_receipt:
                trace_dict["profile_cleanup_failed_after_receipt"] = True
            return TaskFamilyHermesRun(
                stdout=stdout,
                stdout_received_at_iso=stdout_received_at_iso,
                duration_ms=int((time.monotonic() - t_start) * 1000),
                trace=trace_dict,
                trace_bundle=bundle,
            )
        except SshHermesError:
            trace.visit_ended_at = datetime.now(UTC).isoformat()
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                    profile_deleted = True
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            raise
        finally:
            if profile_created and not profile_deleted:
                try:
                    await self._delete_profile(conn, eval_profile)
                except Exception as cleanup_exc:
                    logger.warning(
                        "ssh_hermes_cleanup_failed",
                        eval_profile=eval_profile,
                        error=str(cleanup_exc),
                    )
            try:
                conn.close()
                await conn.wait_closed()
            except Exception as close_exc:
                logger.debug("ssh_hermes_close_error", error=str(close_exc))

    # ----------------------------------------------------------------------
    # SSH helpers
    # ----------------------------------------------------------------------

    async def _connect_with_retries(
        self,
        asyncssh: Any,
        *,
        host: str,
        port: int,
        username: str,
        miner_hotkey: str,
    ) -> Any:
        connect_kwargs: dict[str, Any] = {
            "host": host,
            "port": port,
            "username": username,
            "client_keys": [self.config.ssh_private_key_path],
            # Miners can have any host key; trust IP+key at registration time.
            "known_hosts": None,
            "connect_timeout": self.config.connect_timeout_secs,
        }
        backoff = self.config.connect_retry_initial_secs
        last_err: Exception | None = None
        for attempt in range(1, self.config.connect_retries + 1):
            try:
                return await asyncssh.connect(**connect_kwargs)
            except asyncssh.PermissionDenied as e:
                raise SshHermesError("auth_failed", str(e)) from e
            except (OSError, asyncssh.Error) as e:
                last_err = e
                logger.warning(
                    "ssh_hermes_connect_attempt_failed",
                    attempt=attempt,
                    host=host,
                    miner_hotkey=miner_hotkey,
                    error=str(e),
                )
                if attempt < self.config.connect_retries:
                    await asyncio.sleep(backoff)
                    backoff *= 2
        raise SshHermesError(
            "connect_refused",
            (
                f"could not connect to {host}:{port} after "
                f"{self.config.connect_retries} attempts: {last_err}"
            ),
        )

    async def _run_remote(
        self,
        conn: Any,
        cmd: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 — `timeout` is the API we want
        failure_code: str = "hermes_invocation_failed",
        check: bool = True,
    ) -> tuple[str, str, int]:
        """Run a remote command. Returns (stdout, stderr, exit_status).

        When ``check=True`` and exit_status != 0, raises
        ``SshHermesError(failure_code, ...)``. ``timeout`` is in seconds;
        on timeout raises ``SshHermesError("prompt_timeout", ...)``.
        """
        try:
            result = await asyncio.wait_for(
                conn.run(cmd, check=False),
                timeout=timeout,
            )
        except TimeoutError as e:
            raise SshHermesError(
                "prompt_timeout",
                f"command timed out after {timeout}s: {_redact_query_tokens(cmd[:120])}",
            ) from e
        stdout = (
            (result.stdout or "")
            if isinstance(result.stdout, str)
            else ((result.stdout or b"").decode("utf-8", errors="replace"))
        )
        stderr = (
            (result.stderr or "")
            if isinstance(result.stderr, str)
            else ((result.stderr or b"").decode("utf-8", errors="replace"))
        )
        exit_status = int(result.exit_status or 0)
        if check and exit_status != 0:
            raise SshHermesError(
                failure_code,
                f"exit={exit_status} cmd={_redact_query_tokens(cmd[:120])!r} "
                f"stderr={_redact_query_tokens(stderr[:300])}",
            )
        return stdout, stderr, exit_status

    async def _run_remote_limited_stdout(
        self,
        conn: Any,
        cmd: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 — `timeout` is the API we want
        max_stdout_bytes: int,
    ) -> tuple[str, str, int]:
        """Run a remote command while streaming and bounding stdout bytes."""

        import asyncssh

        if max_stdout_bytes < 1:
            raise SshHermesError(
                "config_invalid",
                f"max_stdout_bytes must be positive: {max_stdout_bytes}",
            )

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout

        def remaining_timeout() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            return remaining

        process: Any | None = None
        try:
            process = await asyncio.wait_for(
                conn.create_process(
                    cmd,
                    stdin=asyncssh.DEVNULL,
                    # Stderr is miner-controlled too; fold it into the same
                    # bounded stream so logs cannot bypass the SAT stdout cap.
                    stderr=asyncssh.STDOUT,
                    encoding=None,
                ),
                timeout=remaining_timeout(),
            )

            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = await asyncio.wait_for(
                    process.stdout.read(_SSH_STDOUT_READ_CHUNK_BYTES),
                    timeout=remaining_timeout(),
                )
                if not chunk:
                    break
                raw = (
                    chunk.encode("utf-8", errors="replace")
                    if isinstance(chunk, str)
                    else bytes(chunk)
                )
                total += len(raw)
                if total > max_stdout_bytes:
                    await self._stop_remote_process(process)
                    raise SshHermesError(
                        "hermes_stdout_oversized",
                        (
                            f"stdout exceeded {max_stdout_bytes} bytes for "
                            f"cmd={_redact_query_tokens(cmd[:120])!r}"
                        ),
                    )
                chunks.append(raw)

            result = await asyncio.wait_for(
                process.wait(check=False),
                timeout=remaining_timeout(),
            )
        except TimeoutError as e:
            if process is not None:
                await self._stop_remote_process(process)
            raise SshHermesError(
                "prompt_timeout",
                f"command timed out after {timeout}s: {_redact_query_tokens(cmd[:120])}",
            ) from e

        stdout = b"".join(chunks).decode("utf-8", errors="replace")
        exit_status = int(getattr(result, "exit_status", 0) or 0)
        stderr = stdout if exit_status != 0 else ""
        return stdout, stderr, exit_status

    async def _stop_remote_process(self, process: Any) -> None:
        with suppress(Exception):
            process.terminate()
        with suppress(Exception):
            await asyncio.wait_for(process.wait_closed(), timeout=2.0)
            return
        with suppress(Exception):
            process.kill()
        with suppress(Exception):
            await asyncio.wait_for(process.wait_closed(), timeout=2.0)

    # ----------------------------------------------------------------------
    # Hermes-specific steps
    # ----------------------------------------------------------------------

    async def _hermes_version(self, conn: Any) -> str:
        stdout, stderr, exit_status = await self._run_remote(
            conn,
            "hermes --version",
            timeout=15.0,
            check=False,
        )
        if exit_status != 0:
            if "command not found" in stderr.lower() or "not found" in stderr.lower():
                raise SshHermesError(
                    "hermes_not_found",
                    f"`hermes --version` returned {exit_status}: {stderr[:200]}",
                )
            raise SshHermesError(
                "hermes_invocation_failed",
                f"`hermes --version` returned {exit_status}: {stderr[:200]}",
            )
        return stdout.strip()

    async def _resolve_hermes_home(self, conn: Any) -> str:
        """Resolve ``self.config.hermes_home`` to an absolute path on the
        miner box, expanding any leading ``~/`` against the remote login
        user's ``$HOME``. Absolute paths are returned unchanged.

        Why: ``shlex.quote("~/.hermes")`` produces ``'~/.hermes'`` and
        bash does NOT expand ``~`` inside single quotes. SFTP does no
        expansion at all. Every downstream consumer needs a real path.
        """
        home = self.config.hermes_home
        if not home.startswith("~/") and home != "~":
            return home.rstrip("/")
        # Use printf to avoid the trailing newline ``echo`` adds.
        stdout, stderr, exit_status = await self._run_remote(
            conn,
            'printf "%s" "$HOME"',
            timeout=10.0,
            check=False,
        )
        remote_home = stdout.strip()
        if exit_status != 0 or not remote_home:
            raise SshHermesError(
                "hermes_install_invalid",
                f"could not resolve remote $HOME (exit={exit_status} stderr={stderr[:200]})",
            )
        tail = "" if home == "~" else home[1:]  # keep leading slash from "/...."
        absolute = remote_home.rstrip("/") + tail
        return absolute.rstrip("/")

    async def _verify_hermes_install(self, conn: Any, resolved_home: str) -> None:
        """Check that ``~/.hermes/`` (or configured ``hermes_home``) exists
        and is readable. We can't write here — we use ``hermes profile
        create`` for that — but a missing/broken install fails fast.

        ``resolved_home`` is the absolute path on the miner box (see
        ``_resolve_hermes_home``). We then ``shlex.quote`` it to keep
        the no-shell-injection guarantee on hostile/unusual paths.
        """
        home = shlex.quote(resolved_home)
        cmd = f"test -d {home} && test -r {home}"
        _, stderr, exit_status = await self._run_remote(conn, cmd, timeout=10.0, check=False)
        if exit_status != 0:
            raise SshHermesError(
                "hermes_install_invalid",
                f"hermes_home check failed at {resolved_home}: {stderr[:200]}",
            )

    async def _clone_profile(self, conn: Any, eval_profile: str) -> None:
        """Use `hermes profile create --clone-all` to fork the active
        profile into an isolated eval profile. Source confirmed at
        ``hermes_cli/profiles.py:540-617`` — ``--clone-all`` is the
        canonical full copytree path.
        """
        cmd = f"hermes profile create {shlex.quote(eval_profile)} --clone-all"
        _, stderr, exit_status = await self._run_remote(conn, cmd, timeout=120.0, check=False)
        if exit_status != 0:
            raise SshHermesError(
                "profile_clone_failed",
                f"`{cmd}` returned {exit_status}: {stderr[:300]}",
            )

    async def _delete_profile(self, conn: Any, eval_profile: str) -> None:
        # ``hermes profile delete <name>`` exists per
        # ``hermes_cli/profiles.py:718``. Best-effort — don't raise.
        cmd = f"hermes profile delete {shlex.quote(eval_profile)} --yes"
        try:
            await self._run_remote(conn, cmd, timeout=30.0, check=False)
        except Exception as e:
            logger.warning(
                "ssh_hermes_profile_delete_failed",
                eval_profile=eval_profile,
                error=str(e),
            )

    async def _invoke_hermes(
        self,
        conn: Any,
        *,
        eval_profile: str,
        prompt: str,
        eval_round: str,
        resolved_home: str,
    ) -> tuple[dict[str, Any], str]:
        """Run `hermes chat -q "<prompt>"` against the eval profile.
        Returns (parsed_card_json, raw_stdout).

        v1.1.7: switched from ``hermes -z`` (one-shot
        ``model.generate(prompt)`` with no tool calls, no skill
        execution, no agentic loop) to ``hermes chat -q`` (full agentic
        loop: tool calls, skill execution, multiple model turns,
        memory reads). Cathedral's value prop is verifying the agent
        actually did work (fetching source URLs, calling tools,
        reasoning across turns) — ``-z`` stripped exactly that out.

        ``hermes chat -q`` writes plain text to stdout, with no JSON
        envelope, so we still instruct the agent in the prompt to
        emit a fenced JSON block, then parse it.
        """
        # Cathedral always wraps the card_definition's task_template with a
        # JSON-only output contract. Without this, models with tool access
        # branch into research-mode (writing files, narrating progress) instead
        # of emitting the inline Card JSON. The agent's soul.md describes the
        # schema; this prefix is the imperative reminder bound to THIS call.
        wrapped_prompt = (
            "Output EXACTLY one JSON object matching the Cathedral Card schema "
            "from your soul.md. No prose, no file writes, no narration. The "
            "first character of your response MUST be '{' and the last MUST be "
            "'}'. Do not write files, do not list directories, do not say "
            "'I've compiled' or 'see the full report' — just emit the JSON.\n\n"
            "Task:\n"
            f"{prompt}"
        )
        stdout = await self._invoke_hermes_text(
            conn,
            eval_profile=eval_profile,
            prompt=wrapped_prompt,
            eval_round=eval_round,
            resolved_home=resolved_home,
        )
        card_json = _extract_card_json(stdout)
        if card_json is None:
            raise SshHermesError(
                "hermes_output_malformed",
                (
                    "hermes chat -q stdout had no parseable Card JSON; "
                    f"first 200 chars: {stdout[:200]!r}"
                ),
            )
        return card_json, stdout

    async def _invoke_hermes_text(
        self,
        conn: Any,
        *,
        eval_profile: str,
        prompt: str,
        eval_round: str,
        resolved_home: str,
        timeout_secs: float | None = None,
        max_stdout_bytes: int | None = None,
    ) -> str:
        """Run ``hermes chat -q`` and return raw stdout."""
        hermes_home = self._profile_path(eval_profile, resolved_home)
        envs = [f"HERMES_HOME={shlex.quote(hermes_home)}"]
        if self.config.pinned_provider:
            envs.append(f"HERMES_INFERENCE_PROVIDER={shlex.quote(self.config.pinned_provider)}")
        if self.config.pinned_model:
            envs.append(f"HERMES_INFERENCE_MODEL={shlex.quote(self.config.pinned_model)}")

        cmd = " ".join(envs) + f" hermes chat -Q -q {shlex.quote(prompt)}"
        timeout = timeout_secs if timeout_secs is not None else self.config.eval_timeout_secs
        if max_stdout_bytes is None:
            stdout, stderr, exit_status = await self._run_remote(
                conn,
                cmd,
                timeout=timeout,
                check=False,
            )
        else:
            stdout, stderr, exit_status = await self._run_remote_limited_stdout(
                conn,
                cmd,
                timeout=timeout,
                max_stdout_bytes=max_stdout_bytes,
            )
        if exit_status != 0:
            raise SshHermesError(
                "hermes_invocation_failed",
                (
                    f"hermes chat -q exited {exit_status} for "
                    f"eval_round={eval_round}: "
                    f"{_redact_query_tokens((stderr or stdout)[:300])}"
                ),
            )
        return stdout

    def _profile_path(self, eval_profile: str, resolved_home: str) -> str:
        # Mirrors hermes_cli/profiles.py: profiles live at
        # $HERMES_HOME/profiles/<name>/. Default HERMES_HOME is ~/.hermes.
        # ``resolved_home`` is the absolute path returned by
        # ``_resolve_hermes_home`` — see that method for why we must not
        # use ``self.config.hermes_home`` directly here.
        return f"{resolved_home.rstrip('/')}/profiles/{eval_profile}"

    # ----------------------------------------------------------------------
    # Collection + bundle assembly
    # ----------------------------------------------------------------------

    async def _collect_and_assemble(
        self,
        conn: Any,
        *,
        eval_profile: str,
        eval_id: str,
        submission_id: str,
        eval_round: str,
        hermes_version: str,
        card_json: dict[str, Any],
        hermes_stdout: str,
        resolved_home: str,
        extra_files: dict[str, str] | None = None,
    ) -> TraceBundle:
        """Snapshot state.db via sqlite3.backup(), SCP back the rest,
        compute hashes, write manifest, tar.gz the bundle, blake3 it.
        """
        profile_path = self._profile_path(eval_profile, resolved_home)
        local_root = Path(tempfile.mkdtemp(prefix=f"cathedral-eval-{eval_round}-"))
        try:
            # Snapshot state.db via Hermes's own consistent-backup pattern
            # (hermes_cli/backup.py:_safe_copy_db). The Python one-liner
            # runs on the miner box and writes the snapshot to /tmp;
            # we SCP that file rather than the live state.db + WAL trio.
            # S108 justification: /tmp on the miner's box is the right
            # place for an ephemeral file we delete in the same session.
            # eval_round contains a UUID suffix so collisions
            # across concurrent evals are not possible.
            snapshot_remote = f"/tmp/cathedral-state-{eval_round}.db"  # noqa: S108
            sqlite_oneliner = (
                f"import sqlite3; "
                f"s=sqlite3.connect('file:{profile_path}/state.db?mode=ro', uri=True); "
                f"d=sqlite3.connect({snapshot_remote!r}); "
                f"s.backup(d); d.close(); s.close()"
            )
            _, stderr, exit_status = await self._run_remote(
                conn,
                f"python3 -c {shlex.quote(sqlite_oneliner)}",
                timeout=60.0,
                check=False,
            )
            if exit_status != 0:
                raise SshHermesError(
                    "transfer_failed",
                    f"state.db snapshot failed: {stderr[:200]}",
                )

            # SCP the snapshot + the other forensic files.
            files_to_pull = [
                # (remote_path, local_relative_path)
                (snapshot_remote, "state.db"),
                (f"{profile_path}/SOUL.md", "SOUL.md"),
                (f"{profile_path}/memories/MEMORY.md", "memories/MEMORY.md"),
                (f"{profile_path}/memories/USER.md", "memories/USER.md"),
                (f"{profile_path}/logs/agent.log", "logs/agent.log"),
                (f"{profile_path}/logs/errors.log", "logs/errors.log"),
            ]
            for remote_path, local_rel in files_to_pull:
                dest = local_root / local_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    await asyncio.wait_for(
                        conn.run(
                            f"test -f {shlex.quote(remote_path)}",
                            check=False,
                        ),
                        timeout=10.0,
                    )
                    import asyncssh  # noqa: F401 — already imported

                    async with conn.start_sftp_client() as sftp:
                        try:
                            await sftp.get(remote_path, str(dest))
                        except (OSError, Exception) as e:
                            logger.info(
                                "ssh_hermes_optional_file_missing",
                                remote=remote_path,
                                error=str(e),
                            )
                            continue
                except Exception as e:
                    logger.warning(
                        "ssh_hermes_file_pull_failed",
                        remote=remote_path,
                        error=str(e),
                    )

            # Sessions and request dumps — variable count. List the dir,
            # pull each. session_*.json is the per-session log; the
            # request_dump_*.json files are per-API-call traces.
            try:
                async with conn.start_sftp_client() as sftp:
                    sessions_remote = f"{profile_path}/sessions"
                    try:
                        entries = await sftp.listdir(sessions_remote)
                    except (OSError, Exception):
                        entries = []
                    for entry in entries:
                        if not (entry.startswith("session_") or entry.startswith("request_dump_")):
                            continue
                        if not entry.endswith(".json"):
                            continue
                        remote_file = f"{sessions_remote}/{entry}"
                        local_file = local_root / "sessions" / entry
                        local_file.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            await sftp.get(remote_file, str(local_file))
                        except Exception as e:
                            logger.warning(
                                "ssh_hermes_session_file_pull_failed",
                                remote=remote_file,
                                error=str(e),
                            )
            except Exception as e:
                logger.warning("ssh_hermes_sessions_dir_pull_failed", error=str(e))

            # Skills tree — preserve the agent's accumulated learning.
            # Recursive copy via tar over SSH (rsync would be nicer but
            # we don't want a hard dependency on rsync being installed).
            skills_remote = f"{profile_path}/skills"
            # See snapshot_remote above for the /tmp justification.
            skills_tarball_remote = f"/tmp/cathedral-skills-{eval_round}.tar.gz"  # noqa: S108
            _, _, ts_exit = await self._run_remote(
                conn,
                (
                    f"test -d {shlex.quote(skills_remote)} && "
                    f"tar -czf {shlex.quote(skills_tarball_remote)} -C "
                    f"{shlex.quote(profile_path)} skills || true"
                ),
                timeout=60.0,
                check=False,
            )
            if ts_exit == 0:
                try:
                    async with conn.start_sftp_client() as sftp:
                        local_skills_tar = local_root / "skills.tar.gz"
                        try:
                            await sftp.get(skills_tarball_remote, str(local_skills_tar))
                        except Exception as e:
                            logger.info(
                                "ssh_hermes_skills_tarball_missing",
                                error=str(e),
                            )
                except Exception as e:
                    logger.warning("ssh_hermes_skills_pull_failed", error=str(e))
                # Clean up the remote tarball best-effort
                try:
                    await self._run_remote(
                        conn,
                        f"rm -f {shlex.quote(skills_tarball_remote)}",
                        timeout=10.0,
                        check=False,
                    )
                except Exception:  # noqa: S110 — best-effort cleanup
                    pass

            # Clean up the remote state.db snapshot
            try:
                await self._run_remote(
                    conn,
                    f"rm -f {shlex.quote(snapshot_remote)}",
                    timeout=10.0,
                    check=False,
                )
            except Exception:  # noqa: S110 — best-effort cleanup
                pass

            # Also preserve the raw `hermes chat -q` stdout for audit
            (local_root / "hermes_stdout.txt").write_text(hermes_stdout, encoding="utf-8")

            # Per-caller sidecar files (e.g. v3 bug_isolation repair stdout).
            if extra_files:
                for rel_path, content in extra_files.items():
                    dest = local_root / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content, encoding="utf-8")

            # The SAT prompt must carry an authorized cnf_url to the miner, but
            # trace bundles are public artifacts. Scrub token-shaped ?t= values
            # from every collected uncompressed artifact before proof, manifest,
            # hashes, and tarball creation.
            _redact_query_tokens_in_artifact_tree(local_root)

            # Compute proof-of-loop from what we collected
            proof = _compute_proof_of_loop(local_root)

            # Build the manifest
            files_list: list[ManifestFile] = []
            for p in sorted(local_root.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(local_root).as_posix()
                files_list.append(
                    ManifestFile(
                        path=rel,
                        sha256=_sha256_of(p),
                        byte_length=p.stat().st_size,
                        content_type=_content_type_for(rel),
                    )
                )

            # tar.gz the lot
            bundle_dir = Path(self.config.bundle_output_dir).expanduser()
            bundle_dir.mkdir(parents=True, exist_ok=True)
            bundle_path = bundle_dir / f"cathedral-eval-{eval_round}.tar.gz"
            with tarfile.open(bundle_path, "w:gz") as tar:
                tar.add(local_root, arcname=".")

            bundle_hash = _blake3_of(bundle_path)

            manifest: dict[str, Any] = {
                "manifest_version": _MANIFEST_VERSION,
                "eval_id": eval_id,
                "submission_id": submission_id,
                "cathedral_eval_round": eval_round,
                "captured_at": datetime.now(UTC).isoformat(),
                "hermes_version": hermes_version,
                "files": [f.to_dict() for f in files_list],
                "proof_of_loop": proof.to_dict(),
                "bundle_blake3": bundle_hash,
            }

            return TraceBundle(
                eval_id=eval_id,
                submission_id=submission_id,
                cathedral_eval_round=eval_round,
                bundle_tar_path=bundle_path,
                manifest=manifest,
                bundle_blake3=bundle_hash,
            )
        except SshHermesError:
            raise
        except Exception as e:
            raise SshHermesError("bundle_assembly_failed", str(e)) from e
        finally:
            # Wipe the local extraction dir; the tar.gz under
            # bundle_output_dir is the durable artifact.
            try:
                import shutil

                shutil.rmtree(local_root, ignore_errors=True)
            except Exception:  # noqa: S110 — best-effort cleanup
                pass


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _blake3_of(p: Path) -> str:
    h = blake3.blake3()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _content_type_for(rel_path: str) -> str:
    if rel_path.endswith(".db"):
        return "application/vnd.sqlite3"
    if rel_path.endswith(".json"):
        return "application/json"
    if rel_path.endswith(".md"):
        return "text/markdown"
    if rel_path.endswith(".log") or rel_path.endswith(".txt"):
        return "text/plain"
    if rel_path.endswith(".tar.gz") or rel_path.endswith(".tgz"):
        return "application/gzip"
    return "application/octet-stream"


def _extract_card_json(stdout: str) -> dict[str, Any] | None:
    """Pull a Card JSON out of the agent's stdout.

    `hermes chat -q` returns plain text. We instruct the agent (via the
    prompt) to emit the Card as a fenced ```json block. Strategy:
    look for the LAST balanced JSON object in stdout. If the agent
    emits the card in a fenced block, the parser finds it; if it
    emits raw JSON, we still parse the last `{...}`.
    """
    text = stdout.strip()
    if not text:
        return None
    # Strip fenced blocks
    if "```" in text:
        # Take the last fenced block contents
        parts = text.split("```")
        # Even-indexed parts are outside fences; odd-indexed inside.
        # The LAST inside-fence block is most likely the answer.
        candidates = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                # Strip optional language tag on first line
                lines = part.split("\n", 1)
                inner = (
                    lines[1] if len(lines) == 2 and not lines[0].strip().startswith("{") else part
                )
                candidates.append(inner.strip())
        for cand in reversed(candidates):
            try:
                obj = json.loads(cand)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    # Try the whole stdout as JSON
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Balanced object scan — collect every balanced {...} then return
    # the LAST one that parses as a dict. The "last" rule matches how
    # an agent writes: preliminary thinking before the final answer
    # should be discarded.
    candidates_str: list[str] = []
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        start = i
        end = -1
        for j in range(i, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            break  # unbalanced tail; stop
        candidates_str.append(text[start : end + 1])
        i = end + 1
    for cand in reversed(candidates_str):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def _compute_proof_of_loop(local_root: Path) -> ProofOfLoop:
    """Read the SQLite slice + sessions JSONs to count tool calls etc.

    Cheap to compute, useful for spot-checking miners who skipped the
    full Hermes loop. The underlying truth lives in state.db.
    """
    proof = ProofOfLoop()

    # request_dump file count
    sessions_dir = local_root / "sessions"
    if sessions_dir.is_dir():
        request_dumps = list(sessions_dir.glob("request_dump_*.json"))
        proof.request_dump_file_count = len(request_dumps)

        # Find the (most recent) session_*.json to read the system_prompt
        session_files = sorted(sessions_dir.glob("session_*.json"))
        if session_files:
            try:
                doc = json.loads(session_files[-1].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                doc = None
            if isinstance(doc, dict):
                proof.session_id = doc.get("session_id")
                sys_prompt = doc.get("system_prompt") or ""
                if isinstance(sys_prompt, str):
                    proof.system_prompt_includes_soul_md = (
                        "SOUL" in sys_prompt or "Soul" in sys_prompt
                    )
                    proof.system_prompt_includes_agents_md = (
                        "AGENTS" in sys_prompt or "AGENTS.md" in sys_prompt
                    )
                    proof.system_prompt_includes_memory_md = "MEMORY" in sys_prompt
                messages = doc.get("messages") or []
                if isinstance(messages, list):
                    seen_tools: set[str] = set()
                    for m in messages:
                        if not isinstance(m, dict):
                            continue
                        tool_calls = m.get("tool_calls")
                        if isinstance(tool_calls, list) and tool_calls:
                            proof.tool_call_count += len(tool_calls)
                            for tc in tool_calls:
                                if isinstance(tc, dict):
                                    fn = tc.get("function") or {}
                                    name = fn.get("name") if isinstance(fn, dict) else None
                                    if isinstance(name, str):
                                        seen_tools.add(name)
                    proof.tool_calls_observed = sorted(seen_tools)

    # api_call_count == request_dump_file_count for this version of Hermes
    # (one dump per LLM API call). When/if that invariant breaks, read
    # the SQLite slice's `sessions.api_call_count` column instead.
    proof.api_call_count = proof.request_dump_file_count
    return proof


__all__ = [
    "SSH_HERMES_FAILURE_CODES",
    "BugIsolationHermesRun",
    "HermesVisitTrace",
    "ManifestFile",
    "ProofOfLoop",
    "SshHermesError",
    "SshHermesRunner",
    "SshHermesRunnerConfig",
    "TaskFamilyHermesRun",
    "TraceBundle",
]
