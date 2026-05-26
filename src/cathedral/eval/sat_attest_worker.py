"""Async SSH-attest worker for PR5 (solve-on-submit) winners.

When a miner POSTs a valid DIMACS solution to ``/v1/agents/submit``, the
publisher synchronously verifies the answer, locks the round, writes a
signed ``eval_runs`` row, and marks ``attestation_status='pending'``.
This worker is the second half of the pipeline: it SSHes into the
miner's box to confirm that the registered environment really exists,
and on success flips the audit status to ``attested``. The row is already
eligible for signed weights before this audit completes.

The attest does NOT re-verify the SAT answer (already mathematically
correct at solve-POST time). It exists to defeat the fraud mode where a
miner outsources solving to a different box but registered SSH pointing
elsewhere; the attest pass is the proof that the registered endpoint is
real.

Anti-cheat semantics:
- Confirms the miner's SSH endpoint accepts our key.
- Snapshots ``~/.hermes`` profile to ``cathedral-attest-<eval_run_id>``
  (touches the env, exercises filesystem auth, sanity-checks Hermes
  install).
- Best-effort cleanup of the snapshot profile.

Hard fail:
- ``eval_runs.attestation_status = 'failed'``
- ``eval_runs.errors = ["attest_failed: <reason>"]``

The worker deliberately does not revoke a mathematically valid solve. SSH
attest is an audit trail, not a payment gate.

Does NOT reopen the challenge in v1.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aiosqlite
import structlog

from cathedral.eval.scoring_pipeline import EvalSigner
from cathedral.publisher import repository

logger = structlog.get_logger(__name__)


_DEFAULT_POLL_INTERVAL_SECS = 30.0
_DEFAULT_MAX_CONCURRENT = 2
_DEFAULT_SSH_PORT = 22


class _AttestError(Exception):
    """Raised by the attest probe to signal a durable audit failure.

    The string carries the reason code stored in
    ``eval_runs.errors``. Distinguished from generic exceptions so a
    transient SSH timeout keeps retrying instead of marking the audit failed.
    """


async def run_sat_attest_loop(
    *,
    db: aiosqlite.Connection,
    signer: EvalSigner,
    db_write_lock: asyncio.Lock,
    stop: asyncio.Event,
    runner_factory: Any | None = None,
    poll_interval_secs: float = _DEFAULT_POLL_INTERVAL_SECS,
    max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
) -> None:
    """Background loop: drain ``attestation_status='pending'`` rows.

    ``runner_factory`` is a callable returning an object with an async
    ``attest(*, ssh_host, ssh_port, ssh_user, eval_run_id) -> None``
    method. Production wires this to an ``SshHermesRunner``; tests pass
    a fake. When ``None``, attempts to build the production runner from
    env; on failure (no SSH key on disk) the loop logs once and exits
    quietly — the legacy SSH-pushed eval path still works.
    """
    runner_factory = runner_factory or _default_runner_factory
    try:
        runner = runner_factory()
    except Exception as e:
        logger.warning(
            "sat_attest_loop_not_started",
            reason="runner_factory_failed",
            error=str(e),
        )
        return

    sem = asyncio.Semaphore(max_concurrent)
    logger.info(
        "sat_attest_loop_started",
        poll_interval_secs=poll_interval_secs,
        max_concurrent=max_concurrent,
    )
    while not stop.is_set():
        try:
            pending = await repository.list_pending_sat_attest(db, limit=max_concurrent * 4)
        except Exception as e:
            logger.warning("sat_attest_list_failed", error=str(e))
            await _sleep_or_stop(stop, poll_interval_secs)
            continue

        if not pending:
            await _sleep_or_stop(stop, poll_interval_secs)
            continue

        tasks = [
            asyncio.create_task(
                _attest_one(
                    row=row,
                    runner=runner,
                    db=db,
                    db_write_lock=db_write_lock,
                    sem=sem,
                )
            )
            for row in pending
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await _sleep_or_stop(stop, poll_interval_secs)


async def _sleep_or_stop(stop: asyncio.Event, secs: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except TimeoutError:
        pass


async def _attest_one(
    *,
    row: dict[str, Any],
    runner: Any,
    db: aiosqlite.Connection,
    db_write_lock: asyncio.Lock,
    sem: asyncio.Semaphore,
) -> None:
    eval_run_id = row["eval_run_id"]
    miner_hotkey = row["miner_hotkey"]
    ssh_host = row["ssh_host"]
    ssh_port = row["ssh_port"] or _DEFAULT_SSH_PORT
    ssh_user = row["ssh_user"]
    if not (ssh_host and ssh_user):
        # No coordinates to attest against — mark the row as failed so
        # we don't re-pick it forever.
        async with db_write_lock:
            await repository.mark_sat_attest_failed(
                db,
                eval_run_id=eval_run_id,
                reason="missing_ssh_coordinates",
            )
        logger.warning(
            "sat_attest_missing_ssh",
            eval_run_id=eval_run_id,
            hotkey=miner_hotkey,
        )
        return

    async with sem:
        try:
            await _run_attest_probe(
                runner=runner,
                eval_run_id=eval_run_id,
                miner_hotkey=miner_hotkey,
                ssh_host=ssh_host,
                ssh_port=int(ssh_port),
                ssh_user=ssh_user,
            )
        except _AttestError as e:
            async with db_write_lock:
                await repository.mark_sat_attest_failed(
                    db,
                    eval_run_id=eval_run_id,
                    reason=str(e),
                )
            logger.warning(
                "sat_attest_failed",
                eval_run_id=eval_run_id,
                hotkey=miner_hotkey,
                ssh_host=ssh_host,
                reason=str(e),
            )
            return
        except Exception as e:
            # Transient errors (network blip, transport timeout) should
            # leave the row in 'pending' so we re-attempt next tick.
            # Only ``_AttestError`` records a durable audit failure.
            logger.warning(
                "sat_attest_transient_error",
                eval_run_id=eval_run_id,
                hotkey=miner_hotkey,
                ssh_host=ssh_host,
                error=str(e),
            )
            return

        # ----- Success: mark audit passed ---------------------------------
        # Direct solve rows are signed synchronously before they enter the
        # payment path. The attest worker must not re-sign a different payload;
        # it only records that the registered SSH endpoint passed audit.
        async with db_write_lock:
            await repository.mark_sat_attest_passed(
                db,
                eval_run_id=eval_run_id,
            )
        logger.info(
            "sat_attest_passed",
            eval_run_id=eval_run_id,
            hotkey=miner_hotkey,
        )


async def _run_attest_probe(
    *,
    runner: Any,
    eval_run_id: str,
    miner_hotkey: str,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
) -> None:
    """Call the runner's attest entry point.

    The runner contract is intentionally narrow: an ``async def
    attest(...)`` method that returns ``None`` on success and raises
    ``_AttestError`` (or any other exception, treated as transient)
    on failure.
    """
    attest = getattr(runner, "attest", None)
    if attest is None or not callable(attest):
        raise _AttestError("runner missing attest method")
    await attest(
        eval_run_id=eval_run_id,
        miner_hotkey=miner_hotkey,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
    )


# --------------------------------------------------------------------------
# Default production runner: SSH probe of ~/.hermes
# --------------------------------------------------------------------------


def _default_runner_factory() -> SatAttestProbeRunner:
    """Build the default SSH attest probe from env.

    Pulls ``CATHEDRAL_SSH_KEY_PATH`` (materialized at app startup); if
    missing, raises so the loop logs once and exits.
    """
    key_path = os.environ.get("CATHEDRAL_SSH_KEY_PATH", "").strip()
    if not key_path:
        raise RuntimeError(
            "CATHEDRAL_SSH_KEY_PATH not set; SAT attest worker disabled"
        )
    return SatAttestProbeRunner(ssh_private_key_path=key_path)


class SatAttestProbeRunner:
    """SSH probe runner for PR5 attest.

    Connects to the miner's box, snapshots ``~/.hermes`` to a uniquely
    named probe profile, and removes it on success. The probe DOES NOT
    invoke Hermes — the attest's job is to confirm the registered
    endpoint accepts our key and has a Hermes install, not to verify
    a synthetic challenge (that's future work in a v2 attest mode).
    """

    def __init__(
        self,
        *,
        ssh_private_key_path: str,
        connect_timeout_secs: float = 10.0,
        run_timeout_secs: float = 30.0,
        hermes_home: str = "~/.hermes",
    ) -> None:
        self._ssh_private_key_path = ssh_private_key_path
        self._connect_timeout_secs = connect_timeout_secs
        self._run_timeout_secs = run_timeout_secs
        self._hermes_home = hermes_home

    async def attest(
        self,
        *,
        eval_run_id: str,
        miner_hotkey: str,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
    ) -> None:
        try:
            import asyncssh  # type: ignore
        except ImportError as e:
            # asyncssh is the same dep ssh_hermes_runner uses; if
            # missing, treat as transient (operator misconfig).
            raise RuntimeError(f"asyncssh not installed: {e}") from e

        snapshot_name = f"cathedral-attest-{eval_run_id[:12]}"
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host=ssh_host,
                    port=ssh_port,
                    username=ssh_user,
                    client_keys=[self._ssh_private_key_path],
                    known_hosts=None,
                ),
                timeout=self._connect_timeout_secs,
            )
        except asyncssh.PermissionDenied as e:
            raise _AttestError(f"ssh_auth_failed: {e}") from e
        except (OSError, asyncssh.Error, TimeoutError) as e:
            # Transient — let the caller retry next tick.
            raise RuntimeError(f"ssh_connect_failed: {e}") from e

        try:
            # Verify ~/.hermes exists. Use a shell test rather than
            # invoking hermes itself — keeps the attest cheap and
            # avoids killing miner agentic-loop state.
            check_cmd = (
                f"test -d {_shell_quote(self._hermes_home)} && echo ok"
            )
            result = await asyncio.wait_for(
                conn.run(check_cmd, check=False),
                timeout=self._run_timeout_secs,
            )
            stdout = _decode(result.stdout)
            if (result.exit_status or 0) != 0 or "ok" not in stdout:
                raise _AttestError(
                    f"hermes_home_missing: {self._hermes_home}"
                )

            # Best-effort snapshot for audit. Use cp -a so symlinks
            # survive. We DON'T fail attest on snapshot failure because
            # operator boxes have varying disk layout — the SSH-auth
            # check is the actual gate.
            snapshot_path = f"~/.cathedral-attest/{snapshot_name}"
            snap_cmd = (
                f"mkdir -p ~/.cathedral-attest && "
                f"cp -a {_shell_quote(self._hermes_home)} "
                f"{_shell_quote(snapshot_path)}"
            )
            try:
                await asyncio.wait_for(
                    conn.run(snap_cmd, check=False),
                    timeout=self._run_timeout_secs,
                )
            except Exception as e:
                logger.debug(
                    "sat_attest_snapshot_skipped",
                    eval_run_id=eval_run_id,
                    error=str(e),
                )

            # Best-effort cleanup of the snapshot — keep miner disk
            # clean. Failure here is fine.
            try:
                cleanup_cmd = f"rm -rf {_shell_quote(snapshot_path)}"
                await asyncio.wait_for(
                    conn.run(cleanup_cmd, check=False),
                    timeout=self._run_timeout_secs,
                )
            except Exception as e:
                logger.debug("sat_attest_cleanup_failed", eval_run_id=eval_run_id, error=str(e))
        finally:
            try:
                conn.close()
                await conn.wait_closed()
            except Exception as e:
                logger.debug("sat_attest_close_failed", eval_run_id=eval_run_id, error=str(e))


def _shell_quote(value: str) -> str:
    """Minimal POSIX shell quoting. Same convention as ssh_hermes_runner."""
    if not value:
        return "''"
    if all(
        ch.isalnum() or ch in "_-./~"
        for ch in value
    ):
        return value
    escaped = value.replace("'", "'\\''")
    return f"'{escaped}'"


def _decode(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


__all__ = [
    "SatAttestProbeRunner",
    "run_sat_attest_loop",
]
