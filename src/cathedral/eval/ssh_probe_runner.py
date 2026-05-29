"""SshProbeRunner — free-tier verification by SSHing into a miner-owned box.

This is the BYO Box path. Miners run their own Hermes anywhere (laptop,
home server, cloud) and authorize Cathedral SSH access by adding our
public key to ``authorized_keys`` on the box. Cathedral SSHs in, queries
the running Hermes via its local HTTP endpoint, packages the response,
SSHs out, signs the outcome.

Contrast with the two other runners:

  * ``PolarisRuntimeRunner`` (v1, legacy) — Polaris deploys our
    ``cathedral-runtime`` shim, which is an LLM call (not the agent).
  * ``PolarisDeployRunner`` (v2 paid) — Polaris deploys the canonical
    Hermes image with the miner's bundle, Cathedral hits ``/chat``
    over HTTPS. Miner pays Polaris for compute.
  * ``SshProbeRunner`` (v2 free) — Miner runs Hermes themselves.
    Cathedral SSHs in and queries the local endpoint. No Cathedral
    compute cost, no Polaris cost. **No Polaris attestation,
    so no 1.10x verified-runtime multiplier.**

The runner contract is identical to the other runners (returns
``PolarisRunResult``) so the orchestrator's dispatch stays trivial. The
free/paid distinction shows up downstream at scoring: ``manifest``
is ``None`` here (no Polaris signature chain), so the verified-runtime
multiplier is not applied.

Failure modes are explicit. Every visit produces a structured outcome
log even on total failure — silent failures are bugs. The outcome is
emitted on ``PolarisRunResult.errors`` (single string) when the visit
failed, with a code prefix matching one of ``SSH_PROBE_FAILURE_CODES``
below. Operators reading a miner's history can grep for these.

Wired by ``CATHEDRAL_EVAL_MODE=ssh-probe``. The submission row carries:

    ssh_host, ssh_port, ssh_user, hermes_port

These are registered with the submission. Cathedral does not store any
miner-side credentials; the miner authorizes us by installing
Cathedral's public SSH key on their box. Rotating the key is a
coordinated platform operation (annual).

Security boundary: Cathedral holds one universal SSH private key for
the platform. The miner adds the matching public key to their box's
``authorized_keys`` for the user they nominate. The miner controls what
that user can do — typical setup is an unprivileged user with read
access to ``~/.hermes/`` and the ability to ``curl localhost``. We
do NOT need root, sudo, or write access. Refuse to run if the
configured key path is wrong-shaped.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import blake3
import structlog

from cathedral.eval.runner_types import (
    PolarisRunnerError,
    PolarisRunResult,
)
from cathedral.v1_types import EvalTask

logger = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Failure mode codes
# --------------------------------------------------------------------------

# Every probe visit ends in exactly one outcome. The successful path is
# implicit; everything else is a failure code that ships in
# ``PolarisRunResult.errors[0]`` so a validator/operator can grep for it
# in the persisted logs. Adding a new failure mode is a deliberate API
# change — bump the list, update docs/VALIDATOR.md.
SSH_PROBE_FAILURE_CODES = (
    "connect_refused",       # SSH host unreachable, port closed, miner box down
    "auth_failed",           # wrong key, miner removed it, wrong user
    "hermes_not_found",      # no process named hermes, port not listening
    "hermes_unhealthy",      # /healthz returned non-200 or timed out
    "prompt_timeout",        # agent didn't respond within budget
    "prompt_error",          # agent returned a non-success response
    "file_missing",          # soul.md or expected profile file absent
    "package_failed",        # zip creation failed on the box
    "transfer_failed",       # cat / scp out failed mid-transfer
    "disconnect_dirty",      # ssh session died mid-operation, state unknown
    "config_invalid",        # submission row missing ssh_* fields
)


class SshProbeError(PolarisRunnerError):
    """Top-level SSH probe failure with one of ``SSH_PROBE_FAILURE_CODES``."""

    def __init__(self, code: str, detail: str) -> None:
        if code not in SSH_PROBE_FAILURE_CODES:
            raise ValueError(f"unknown ssh probe code: {code!r}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SshProbeRunnerConfig:
    """Configuration for ``SshProbeRunner``.

    ``ssh_private_key_path`` points at the Cathedral platform's universal
    private SSH key. The miner has the matching public key installed in
    their box's ``authorized_keys``. The key is **NOT** per-miner — one
    key, all miners. Rotation is a platform-wide operation.
    """

    ssh_private_key_path: str
    connect_timeout_secs: float = 10.0
    prompt_timeout_secs: float = 60.0
    visit_budget_secs: float = 300.0
    connect_retries: int = 3
    connect_retry_initial_secs: float = 1.0
    # Path on the miner box to look for the Hermes profile directory.
    # Override via env (rare) if a miner runs Hermes with a non-default
    # HERMES_HOME. Most miners will use the documented `~/.hermes/`.
    hermes_profile_dir: str = "~/.hermes"


# --------------------------------------------------------------------------
# Outcome capture
# --------------------------------------------------------------------------


@dataclass
class VisitTrace:
    """Per-visit structured trace.

    Mirrors the shape of ``PolarisDeployRunner``'s trace so downstream
    persistence is uniform. Anything that varies per visit goes here;
    one-time miner identity / config stays on ``PolarisRunResult``.
    """

    visit_started_at: str
    visit_ended_at: str | None = None
    prompts_attempted: int = 0
    prompts_succeeded: int = 0
    hermes_healthz_ok: bool = False
    files_collected: list[str] | None = None
    # Hermes's per-prompt response duration (ms). Useful for spotting
    # miners whose agents are getting slower (state bloat) over time.
    prompt_durations_ms: list[int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "visit_started_at": self.visit_started_at,
            "visit_ended_at": self.visit_ended_at,
            "prompts_attempted": self.prompts_attempted,
            "prompts_succeeded": self.prompts_succeeded,
            "hermes_healthz_ok": self.hermes_healthz_ok,
            "files_collected": self.files_collected or [],
            "prompt_durations_ms": self.prompt_durations_ms or [],
            "tier": "ssh-probe",
        }


# --------------------------------------------------------------------------
# Probe runner
# --------------------------------------------------------------------------


class SshProbeRunner:
    """Free-tier runner. SSH into the miner's box, query Hermes, leave.

    The miner registers ``ssh_host``, ``ssh_port``, ``ssh_user``, and
    ``hermes_port`` with their submission. Cathedral SSHs in using a
    platform-wide private key (matching pubkey in the miner's
    authorized_keys), POSTs the eval task to ``localhost:<hermes_port>/chat``,
    captures the response plus a small set of profile files, and zips
    them for the audit log.

    Returns a ``PolarisRunResult`` with:

      * ``output_card_json``: the Card JSON Hermes returned
      * ``polaris_agent_id`` / ``polaris_run_id``: synthetic ids
        (``ssh-probe:<miner_hotkey[:12]>``, uuid4) — not Polaris-issued,
        but the same shape so the orchestrator + storage layer don't
        branch on tier.
      * ``manifest = None``: no Polaris attestation, no 1.10x multiplier
      * ``attestation = None``: same
      * ``trace``: VisitTrace dict (see above)
      * ``errors``: empty on success; one string with the failure code
        and detail on any failure mode

    Idempotent. Each call is a fresh visit; partial-state recovery is
    explicitly not supported.
    """

    def __init__(self, config: SshProbeRunnerConfig) -> None:
        self.config = config
        if not os.path.isfile(config.ssh_private_key_path):
            raise SshProbeError(
                "config_invalid",
                f"ssh_private_key_path does not exist: {config.ssh_private_key_path}",
            )

    async def run(
        self,
        *,
        bundle_bytes: bytes,  # unused — miner already has Hermes running
        bundle_hash: str,
        task: EvalTask,
        miner_hotkey: str,
        submission: dict[str, Any] | None = None,
    ) -> PolarisRunResult:
        del bundle_bytes  # the bundle lives on the miner's box, not on the wire
        run_id = str(uuid4())
        t_start = time.monotonic()
        trace = VisitTrace(visit_started_at=datetime.now(UTC).isoformat())

        if submission is None:
            return self._failed_result(
                run_id=run_id,
                t_start=t_start,
                trace=trace,
                miner_hotkey=miner_hotkey,
                code="config_invalid",
                detail="no submission row passed; cannot resolve ssh_*",
            )

        ssh_host = submission.get("ssh_host")
        ssh_port = submission.get("ssh_port") or 22
        ssh_user = submission.get("ssh_user")
        hermes_port = submission.get("hermes_port")

        if not (ssh_host and ssh_user and hermes_port):
            return self._failed_result(
                run_id=run_id,
                t_start=t_start,
                trace=trace,
                miner_hotkey=miner_hotkey,
                code="config_invalid",
                detail=(
                    f"missing ssh_* on submission: "
                    f"host={bool(ssh_host)} user={bool(ssh_user)} "
                    f"hermes_port={bool(hermes_port)}"
                ),
            )

        try:
            return await asyncio.wait_for(
                self._do_visit(
                    run_id=run_id,
                    t_start=t_start,
                    trace=trace,
                    task=task,
                    miner_hotkey=miner_hotkey,
                    ssh_host=str(ssh_host),
                    ssh_port=int(ssh_port),
                    ssh_user=str(ssh_user),
                    hermes_port=int(hermes_port),
                ),
                timeout=self.config.visit_budget_secs,
            )
        except TimeoutError:
            return self._failed_result(
                run_id=run_id,
                t_start=t_start,
                trace=trace,
                miner_hotkey=miner_hotkey,
                code="prompt_timeout",
                detail=f"visit exceeded {self.config.visit_budget_secs}s budget",
            )
        except SshProbeError as e:
            return self._failed_result(
                run_id=run_id,
                t_start=t_start,
                trace=trace,
                miner_hotkey=miner_hotkey,
                code=e.code,
                detail=e.detail,
            )
        except Exception as e:
            logger.exception(
                "ssh_probe_unexpected",
                miner_hotkey=miner_hotkey,
                error=repr(e),
            )
            return self._failed_result(
                run_id=run_id,
                t_start=t_start,
                trace=trace,
                miner_hotkey=miner_hotkey,
                code="disconnect_dirty",
                detail=f"unexpected: {e.__class__.__name__}: {e}",
            )

    # --------------------------------------------------------------
    # Visit lifecycle
    # --------------------------------------------------------------

    async def _do_visit(
        self,
        *,
        run_id: str,
        t_start: float,
        trace: VisitTrace,
        task: EvalTask,
        miner_hotkey: str,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        hermes_port: int,
    ) -> PolarisRunResult:
        # asyncssh is imported lazily so this module can be imported in
        # environments where asyncssh isn't installed (e.g. validator
        # boxes that don't run ssh probes themselves).
        import asyncssh

        connect_kwargs = {
            "host": ssh_host,
            "port": ssh_port,
            "username": ssh_user,
            "client_keys": [self.config.ssh_private_key_path],
            "known_hosts": None,  # miners can have any host key; we trust the IP+key combo at registration time
            "connect_timeout": self.config.connect_timeout_secs,
        }

        conn = await self._connect_with_retries(asyncssh, connect_kwargs, miner_hotkey)
        try:
            # 1. Healthcheck
            healthz_url = f"http://127.0.0.1:{hermes_port}/healthz"
            health_ok = await self._curl_healthz(conn, healthz_url)
            trace.hermes_healthz_ok = health_ok
            if not health_ok:
                raise SshProbeError(
                    "hermes_unhealthy",
                    f"healthz at {healthz_url} did not return 200",
                )

            # 2. Run the eval prompt
            trace.prompts_attempted = 1
            trace.prompt_durations_ms = []
            chat_url = f"http://127.0.0.1:{hermes_port}/chat"
            t_prompt = time.monotonic()
            card_json = await self._curl_chat(conn, chat_url, task.prompt)
            trace.prompt_durations_ms.append(
                int((time.monotonic() - t_prompt) * 1000)
            )
            trace.prompts_succeeded = 1

            # 3. Collect profile files (best-effort)
            files = await self._collect_files(conn)
            trace.files_collected = list(files.keys())

            # 4. Done — close cleanly
            trace.visit_ended_at = datetime.now(UTC).isoformat()

            output_card_hash = blake3.blake3(
                json.dumps(card_json, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            task_hash = blake3.blake3(task.prompt.encode()).hexdigest()

            return PolarisRunResult(
                polaris_agent_id=f"ssh-probe:{miner_hotkey[:12]}",
                polaris_run_id=run_id,
                output_card_json=card_json,
                duration_ms=int((time.monotonic() - t_start) * 1000),
                errors=[],
                attestation=None,    # no Polaris attestation on free tier
                probe_attestation=None,  # no signed envelope on this path
                trace={
                    **trace.to_dict(),
                    "files": {
                        # Files are returned as raw bytes; the zip
                        # archive ships in the publisher's outcome log,
                        # not in the EvalRun row (too big). We mirror
                        # filenames + sizes here for the audit trail.
                        name: {"size_bytes": len(content)}
                        for name, content in files.items()
                    },
                    "output_card_hash": output_card_hash,
                    "task_hash": task_hash,
                },
                manifest=None,  # no Polaris manifest, no verified-runtime multiplier
            )
        finally:
            try:
                conn.close()
                await conn.wait_closed()
            except Exception as e:
                logger.warning(
                    "ssh_probe_disconnect_dirty",
                    miner_hotkey=miner_hotkey,
                    error=repr(e),
                )

    # --------------------------------------------------------------
    # SSH primitives
    # --------------------------------------------------------------

    async def _connect_with_retries(
        self,
        asyncssh: Any,
        connect_kwargs: dict[str, Any],
        miner_hotkey: str,
    ) -> Any:
        """Connect with exponential backoff. Returns the asyncssh connection."""
        last_err: Exception | None = None
        delay = self.config.connect_retry_initial_secs
        for attempt in range(self.config.connect_retries):
            try:
                return await asyncssh.connect(**connect_kwargs)
            except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as e:
                # Auth errors don't get retried — they won't fix themselves.
                raise SshProbeError(
                    "auth_failed",
                    f"ssh auth refused: {e.__class__.__name__}: {e}",
                ) from e
            except (TimeoutError, asyncssh.Error, OSError) as e:
                last_err = e
                logger.info(
                    "ssh_probe_connect_retry",
                    miner_hotkey=miner_hotkey,
                    attempt=attempt + 1,
                    of=self.config.connect_retries,
                    error=repr(e),
                )
                if attempt < self.config.connect_retries - 1:
                    await asyncio.sleep(delay)
                    delay *= 5.0

        raise SshProbeError(
            "connect_refused",
            (
                f"connect failed after {self.config.connect_retries} attempts: "
                f"{last_err.__class__.__name__ if last_err else 'unknown'}: {last_err}"
            ),
        )

    async def _curl_healthz(self, conn: Any, url: str) -> bool:
        """Run `curl --max-time N -fsS <url>` on the box. True if exit 0."""
        cmd = f"curl --max-time {int(self.config.connect_timeout_secs)} -fsS {url}"
        result = await conn.run(cmd, check=False, timeout=self.config.connect_timeout_secs + 5)
        return bool(result.exit_status == 0)

    async def _curl_chat(self, conn: Any, url: str, prompt: str) -> dict[str, Any]:
        """POST the eval prompt to local Hermes /chat and parse the JSON response.

        Streaming /chat output is consumed in full; the final event's
        `card_json` is what we return. Errors raise SshProbeError with
        a specific code so the failure mode is unambiguous.

        Implementation: write the JSON body to a tempfile, then curl
        --data-binary from it. Avoids shell-quoting the prompt body.
        """
        body = json.dumps({"message": prompt})
        tmp_path = f"/tmp/cathedral-probe-{uuid4()}.json"

        # 1. Write the body file
        write_cmd = (
            f"cat > {tmp_path} <<'CATHEDRAL_PROBE_EOF'\n"
            f"{body}\n"
            f"CATHEDRAL_PROBE_EOF"
        )
        write_result = await conn.run(write_cmd, check=False, timeout=10)
        if write_result.exit_status != 0:
            raise SshProbeError(
                "package_failed",
                f"could not write prompt to box tmpfile: rc={write_result.exit_status}",
            )

        # 2. Curl POST
        curl_cmd = (
            f"curl --max-time {int(self.config.prompt_timeout_secs)} "
            f"-fsS -H 'Content-Type: application/json' "
            f"--data-binary @{tmp_path} {url}; rm -f {tmp_path}"
        )
        try:
            result = await conn.run(
                curl_cmd,
                check=False,
                timeout=self.config.prompt_timeout_secs + 10,
            )
        except TimeoutError as e:
            raise SshProbeError(
                "prompt_timeout",
                f"chat request exceeded {self.config.prompt_timeout_secs}s",
            ) from e

        if result.exit_status != 0:
            stderr = (result.stderr or "")[:500]
            raise SshProbeError(
                "prompt_error",
                f"hermes /chat exited rc={result.exit_status}: {stderr}",
            )

        # Hermes /chat is NDJSON streaming — parse the final event's card_json.
        stdout = result.stdout or ""
        final_card: dict[str, Any] | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            # Hermes v2026.5.11+ emits {"type": "final", "card_json": {...}}
            # as the terminal NDJSON event when the agent's response was
            # parseable as a Card JSON. Fall back to the legacy single-blob
            # shape (entire response is one JSON object) for older Hermes.
            if event.get("type") == "final" and isinstance(
                event.get("card_json"), dict
            ):
                final_card = event["card_json"]
            elif "card_id" in event and "summary" in event:
                # Legacy: whole response is the card
                final_card = event

        if final_card is None:
            raise SshProbeError(
                "prompt_error",
                "hermes /chat returned no parseable card_json",
            )
        return final_card

    async def _collect_files(self, conn: Any) -> dict[str, bytes]:
        """Best-effort fetch of soul.md + AGENTS.md from the profile dir.

        Files that don't exist are silently skipped; only soul.md is
        required (raises file_missing if absent). The skills/ tree is
        listed but contents are NOT pulled — listing alone is enough
        for the audit trail and avoids unbounded payloads.
        """
        profile = self.config.hermes_profile_dir
        out: dict[str, bytes] = {}

        for name in ("soul.md", "AGENTS.md"):
            cat_cmd = f"cat {profile}/{name}"
            result = await conn.run(cat_cmd, check=False, timeout=10)
            if result.exit_status == 0:
                stdout = result.stdout or ""
                out[name] = stdout.encode("utf-8", errors="replace")[:64 * 1024]
            elif name == "soul.md":
                # soul.md is the canonical agent identity — its absence
                # means the box isn't running a Cathedral-compatible
                # agent.
                raise SshProbeError(
                    "file_missing",
                    f"soul.md not found at {profile}/soul.md",
                )

        # Skills directory listing only.
        ls_cmd = f"ls -la {profile}/skills/ 2>/dev/null"
        try:
            result = await conn.run(ls_cmd, check=False, timeout=10)
            if result.exit_status == 0:
                listing = (result.stdout or "")[:8 * 1024]
                out["skills_listing.txt"] = listing.encode("utf-8")
        except Exception:
            pass  # listing is informational, not load-bearing

        return out

    # --------------------------------------------------------------
    # Failure result helper
    # --------------------------------------------------------------

    def _failed_result(
        self,
        *,
        run_id: str,
        t_start: float,
        trace: VisitTrace,
        miner_hotkey: str,
        code: str,
        detail: str,
    ) -> PolarisRunResult:
        """Return a uniform PolarisRunResult for any failure mode.

        The scoring pipeline sees a card-less run; the orchestrator
        records the row with empty output and the error code in
        ``errors``. No emission goes out, but the visit is logged.
        """
        trace.visit_ended_at = datetime.now(UTC).isoformat()
        logger.warning(
            "ssh_probe_failed",
            miner_hotkey=miner_hotkey,
            code=code,
            detail=detail,
        )
        return PolarisRunResult(
            polaris_agent_id=f"ssh-probe:{miner_hotkey[:12]}",
            polaris_run_id=run_id,
            output_card_json={},
            duration_ms=int((time.monotonic() - t_start) * 1000),
            errors=[f"{code}: {detail}"],
            attestation=None,
            probe_attestation=None,
            trace={**trace.to_dict(), "failure_code": code},
            manifest=None,
        )


# --------------------------------------------------------------------------
# Zip packaging helper
# --------------------------------------------------------------------------


def package_visit(
    *,
    miner_hotkey: str,
    run_id: str,
    card_json: dict[str, Any],
    files: dict[str, bytes],
    trace: dict[str, Any],
) -> bytes:
    """Bundle the visit artifacts into a single zip.

    Returned bytes are persisted to Cathedral storage (R2 or local) as
    a per-visit audit log. The eval_runs row only carries hashes +
    filenames; the actual file bytes live here.

    Layout inside the zip:
        manifest.json    -- {miner_hotkey, run_id, trace, output_card_hash}
        card.json        -- the parsed Card JSON
        soul.md          -- the miner's agent identity (if collected)
        AGENTS.md        -- the agent index (if collected)
        skills_listing.txt -- ls -la output of skills/ (if collected)
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "miner_hotkey": miner_hotkey,
            "run_id": run_id,
            "trace": trace,
            "output_card_hash": blake3.blake3(
                json.dumps(card_json, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "packaged_at": datetime.now(UTC).isoformat(),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("card.json", json.dumps(card_json, indent=2))
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


__all__ = [
    "SSH_PROBE_FAILURE_CODES",
    "SshProbeError",
    "SshProbeRunner",
    "SshProbeRunnerConfig",
    "VisitTrace",
    "package_visit",
]
