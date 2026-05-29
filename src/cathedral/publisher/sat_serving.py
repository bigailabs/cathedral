"""SAT (synthetic-boolean_v1) challenge serving, extracted from EvalOrchestrator.

This module owns the live open-window SAT lane's serving substrate:

* **announcement** — :meth:`SatServing.announce_synthetic_boolean_problem`
  mints/reuses a fetch token and builds the lane inputs for one challenge
  record, and :meth:`SatServing.snapshot_task_family_batch_problems` snapshots
  the batch-stable active formula so a poll batch races one formula.
* **probe + score** — :meth:`SatServing.maybe_run_task_family_lanes` drives the
  SAT prober (``SshHermesRunner.run_task_family_challenge``), records the
  per-attempt receipt, scores the file-backed CNF, and finalizes under
  first-submitted valid-receipt ordering.
* **receipt reconciliation** — :meth:`SatServing.reconcile_sat_receipts`
  expires stale receipt blockers and finalizes any unblocked winner from
  scheduler ticks.

It depends only on the load-bearing SAT substrate (challenge source/lock/
fetch-token/receipt stores, the ``synthetic_boolean_v1`` lane, the v5/v6 signing
keysets, ``EvalSigner``, and the trace-bundle publisher) — never on the
publisher card core (scoring_pipeline / cards / polaris_runner / task_generator).
``EvalOrchestrator`` composes one ``SatServing`` instance and delegates its
SAT methods to it so the card core can be deleted without touching the SAT lane.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import blake3
import structlog

from cathedral.eval.eval_signer import EvalSigner
from cathedral.lanes import registry as lane_registry
from cathedral.lanes.challenge_lock import ChallengeLock
from cathedral.lanes.challenge_receipts import (
    RECEIPT_STATUS_INVALID,
    RECEIPT_STATUS_VALID,
    RECEIPT_STATUS_VERIFYING,
    ChallengeReceipt,
    ChallengeReceiptStore,
    SqliteChallengeReceiptStore,
)
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    ChallengeRecord,
    ChallengeSource,
    SqliteFetchTokenStore,
)
from cathedral.lanes.contract import (
    HiddenMetadata,
    PublicProblem,
    ScoreResult,
    Submission,
    VerifierResult,
)
from cathedral.lanes.publisher import (
    TaskFamilySignedResult,
    build_generate_ctx,
    build_task_family_prompt,
    enabled_task_family_ids,
    persist_task_family_result,
    score_and_sign_task_family_stdout,
    task_family_feed_enabled,
    task_family_prober_version_warning,
    task_family_runner_skip_reason,
    task_family_tier,
)
from cathedral.lanes.sign import resign_task_family_score
from cathedral.lanes.synthetic_boolean_v1 import (
    FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID,
)
from cathedral.lanes.synthetic_boolean_v1 import (
    problem_from_challenge_record,
)
from cathedral.storage import HippiusClient

logger = structlog.get_logger(__name__)

# Verifiers refresh receipt.updated_at_iso when they take ownership. A stale
# heartbeat means the process died or task was cancelled after status=verifying.
_SAT_VERIFYING_STALE_TIMEOUT = timedelta(minutes=10)
_SAT_RECEIPT_HEARTBEAT_INTERVAL = timedelta(minutes=1)
_SAT_LOCKED_RECONCILE_LIMIT = 32


def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


def _receipt_answer_hash(stdout: str) -> str:
    # Receipt insertion runs under the publisher DB write lock and only needs a
    # stable attempt fingerprint. Do not call extract_answer() here: malformed
    # miner stdout can trigger the fallback JSON scan's worst-case behavior and
    # stall every publisher write while this lock is held. Full answer parsing
    # still happens during scoring after the receipt is durably recorded.
    return blake3.blake3(str(stdout).encode("utf-8")).hexdigest()


def _task_family_receipt_attempt_id(stable_submission_id: str) -> str:
    # The receipt table's historical column name is submission_id, but SAT
    # receipts are per answer attempt. Reusing agent_submissions.id here would
    # collapse repeated attempts against an unresolved challenge into one row.
    return f"{stable_submission_id}:attempt:{secrets.token_urlsafe(16)}"


def _task_family_signed_result_from_row(row: dict[str, Any]) -> TaskFamilySignedResult:
    verifier = VerifierResult(
        parsed_ok=float(row.get("weighted_score", 0.0)) > 0.0,
        raw_metric=float(row.get("weighted_score", 0.0)),
        rejection_reason=row.get("rejection_reason"),
        details={},
    )
    score = ScoreResult(
        weighted_score=float(row.get("weighted_score", 0.0)),
        rejection_reason=row.get("rejection_reason"),
        score_parts=dict(row.get("score_parts") or {}),
    )
    submission = Submission(
        task_id=str(row.get("task_id_public") or ""),
        miner_hotkey=str(row.get("miner_hotkey") or ""),
        answer={},
    )
    return TaskFamilySignedResult(
        row=row,
        verifier=verifier,
        score=score,
        prompt="",
        submission=submission,
    )


def _task_family_receipt_submission_row(receipt: ChallengeReceipt) -> dict[str, str]:
    # Receipt IDs are attempt scoped for SAT ordering. Eval rows still need the
    # durable agent_submissions.id FK, which is preserved in the signed payload.
    signed_row = receipt.signed_row or {}
    return {
        "id": str(
            signed_row.get("agent_id")
            or signed_row.get("submission_id")
            or receipt.submission_id
        ),
        "miner_hotkey": str(signed_row.get("miner_hotkey") or receipt.miner_hotkey),
    }


@dataclass
class _ReceiptHeartbeatState:
    stop: asyncio.Event | None = None
    task: asyncio.Task[Any] | None = None


class SatServing:
    """Serves the live synthetic-boolean_v1 SAT lane.

    Owns SAT announcement, probe/score/finalize, and receipt reconciliation.
    Constructed with the same SAT substrate ``EvalOrchestrator`` previously
    held inline; the orchestrator now composes one of these and delegates.
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        hippius: HippiusClient,
        signer: EvalSigner,
        task_family_challenge_source: ChallengeSource | None = None,
        task_family_challenge_lock: ChallengeLock | None = None,
        task_family_fetch_token_store: SqliteFetchTokenStore | None = None,
        task_family_receipt_store: ChallengeReceiptStore | None = None,
        db_write_lock: asyncio.Lock | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self.db = db
        self.hippius = hippius
        self.signer = signer
        self._task_family_challenge_source = task_family_challenge_source
        self._task_family_challenge_lock = task_family_challenge_lock
        self._task_family_fetch_token_store = task_family_fetch_token_store
        self._task_family_receipt_store = task_family_receipt_store
        # SAT winner finalization opens explicit BEGIN/COMMIT windows on this
        # shared aiosqlite connection. This lock is injected from
        # PublisherContext in production so route handlers and SAT writers
        # share one transaction gate; otherwise an unrelated route commit can
        # close the finalizer transaction early.
        self._db_write_lock = db_write_lock or asyncio.Lock()
        # Constructor wins; env is the fallback. An empty string is
        # treated as missing. See announce_synthetic_boolean_problem.
        self._public_base_url = (
            public_base_url or os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "") or ""
        ).strip()

    async def _maybe_publish_bundle(
        self,
        trace_bundle: Any,
        log: Any,
    ) -> Any | None:
        """If the runner produced a TraceBundle on disk AND the v2 emit
        flag is set, upload it to Hippius + sign the manifest. Returns
        the PublishedArtifact for the SAT trace_json to consume, or None.

        Failures here MUST NOT crash the eval — we log + return None so
        the eval still scores under v1 wire shape.
        """
        if trace_bundle is None:
            return None
        if os.environ.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "").lower() != "true":
            # Flag's off — skip the Hippius round-trip entirely.
            return None
        try:
            from cathedral.eval.bundle_publisher import EvalArtifactPublisher

            publisher = EvalArtifactPublisher(hippius=self.hippius, signer=self.signer)
            artifact = await publisher.publish(trace_bundle)
            log.info(
                "eval_artifact_published",
                eval_id=artifact.eval_id,
                manifest_hash=artifact.manifest_hash,
            )
            return artifact
        except Exception as exc:
            log.warning(
                "eval_artifact_publish_failed",
                error=str(exc),
            )
            return None

    async def maybe_run_task_family_lanes(
        self,
        *,
        submission: dict[str, Any],
        runner: Any,
        epoch: int,
        round_index: int,
        log: structlog.stdlib.BoundLogger,
        problem_overrides: dict[str, tuple[PublicProblem, HiddenMetadata]] | None = None,
    ) -> bool:
        if not task_family_feed_enabled():
            return False

        warning = task_family_prober_version_warning()
        if warning is not None:
            log.warning("task_family_launch_guard", **warning)

        skip = task_family_runner_skip_reason(runner)
        if skip is not None:
            log.info("task_family_skipped", **skip)
            return False
        run_challenge = runner.run_task_family_challenge

        miner_hotkey = str(submission["miner_hotkey"])
        submission_card_id = str(submission["card_id"])
        issued_at_iso = _ms_iso(datetime.now(UTC))
        ran_any = False
        # Lane gating: only probe a submission with the task family it
        # explicitly opted into via agent_submissions.card_id. Before this
        # gate, the task-family feed iterated every enabled family per
        # submission, which caused card-mining baseline-runners (e.g.
        # eu-ai-act) to receive SAT prompts they never registered for.
        # Mismatch is the common case, so aggregate to one debug log per
        # submission rather than per (submission, family) pair.
        enabled_families = enabled_task_family_ids()
        gated_families = [fid for fid in enabled_families if fid == submission_card_id]
        skipped_families = [fid for fid in enabled_families if fid != submission_card_id]
        if skipped_families:
            log.debug(
                "task_family_skipped",
                reason="submission_card_id_mismatch",
                submission_id=str(submission["id"]),
                miner_hotkey=miner_hotkey,
                submission_card_id=submission_card_id,
                skipped_family_ids=skipped_families,
            )
        for family_id in gated_families:
            try:
                lane = lane_registry.lookup(family_id)
            except KeyError:
                log.warning("task_family_skipped", family_id=family_id, reason="unregistered")
                continue

            if problem_overrides is not None and family_id in problem_overrides:
                loaded: tuple[PublicProblem | None, HiddenMetadata | None] | None = (
                    problem_overrides[family_id]
                )
            else:
                loaded = await self._load_task_family_problem(
                    lane=lane,
                    family_id=family_id,
                    miner_hotkey=miner_hotkey,
                    epoch=epoch,
                    round_index=round_index,
                    issued_at_iso=issued_at_iso,
                    log=log,
                )
            if loaded is None:
                continue
            problem, hidden = loaded
            if problem is None or hidden is None:
                log.info("task_family_skipped", family_id=family_id, reason="stub")
                continue

            prompt = build_task_family_prompt(problem)
            receipt_store = self._task_family_receipt_store
            receipt: ChallengeReceipt | None = None
            receipt_attempt_id = _task_family_receipt_attempt_id(str(submission["id"]))
            receipt_heartbeat = _ReceiptHeartbeatState()

            def _start_receipt_heartbeat(
                receipt_to_heartbeat: ChallengeReceipt,
                *,
                callback_receipt_store: ChallengeReceiptStore,
                heartbeat: _ReceiptHeartbeatState = receipt_heartbeat,
            ) -> None:
                if heartbeat.task is not None:
                    return
                heartbeat.stop = asyncio.Event()
                heartbeat.task = asyncio.create_task(
                    self._run_sat_receipt_heartbeat(
                        receipt_store=callback_receipt_store,
                        receipt=receipt_to_heartbeat,
                        stop=heartbeat.stop,
                    )
                )

            async def _stop_receipt_heartbeat(
                *,
                heartbeat: _ReceiptHeartbeatState = receipt_heartbeat,
            ) -> None:
                if heartbeat.stop is not None:
                    heartbeat.stop.set()
                if heartbeat.task is not None:
                    await asyncio.gather(heartbeat.task, return_exceptions=True)

            async def _record_receipt(
                stdout: str,
                stdout_received_at_iso: str,
                *,
                callback_family_id: str = family_id,
                callback_miner_hotkey: str = miner_hotkey,
                callback_problem: PublicProblem = problem,
                callback_receipt_attempt_id: str = receipt_attempt_id,
                callback_receipt_store: ChallengeReceiptStore | None = receipt_store,
            ) -> None:
                nonlocal receipt
                if (
                    callback_family_id != SYNTHETIC_BOOLEAN_FAMILY_ID
                    or callback_receipt_store is None
                    or callback_problem.task_id is None
                ):
                    return
                async with self._db_write_lock:
                    receipt = await callback_receipt_store.record_receipt(
                        family_id=callback_family_id,
                        challenge_id=callback_problem.task_id,
                        submission_id=callback_receipt_attempt_id,
                        miner_hotkey=callback_miner_hotkey,
                        received_at_iso=stdout_received_at_iso,
                        answer_hash=_receipt_answer_hash(stdout),
                        recorded_at_iso=_ms_iso(datetime.now(UTC)),
                    )
                    # SshHermesRunner calls this as soon as stdout arrives,
                    # before slower trace collection. Claim verifier ownership
                    # here so another scheduler tick cannot expire this
                    # first-submitted answer as an abandoned unverified receipt.
                    receipt = await callback_receipt_store.update_status(
                        family_id=receipt.family_id,
                        challenge_id=receipt.challenge_id,
                        submission_id=receipt.submission_id,
                        status=RECEIPT_STATUS_VERIFYING,
                        now_iso=_ms_iso(datetime.now(UTC)),
                    )
                    _start_receipt_heartbeat(
                        receipt,
                        callback_receipt_store=callback_receipt_store,
                    )

            run_kwargs: dict[str, Any] = {
                "problem": problem,
                "prompt": prompt,
                "miner_hotkey": miner_hotkey,
                "submission": submission,
            }
            if family_id == SYNTHETIC_BOOLEAN_FAMILY_ID and receipt_store is not None:
                run_kwargs["receipt_callback"] = _record_receipt
            try:
                hermes_run = await run_challenge(
                    **run_kwargs,
                )
            except Exception:
                await _stop_receipt_heartbeat()
                raise
            epoch_salt = f"epoch_{epoch}:{family_id}"
            if family_id == SYNTHETIC_BOOLEAN_FAMILY_ID and receipt_store is not None:
                if receipt is None:
                    received_at = (
                        getattr(hermes_run, "stdout_received_at_iso", None)
                        or _ms_iso(datetime.now(UTC))
                    )
                    async with self._db_write_lock:
                        receipt = await receipt_store.record_receipt(
                            family_id=family_id,
                            challenge_id=problem.task_id,
                            submission_id=receipt_attempt_id,
                            miner_hotkey=miner_hotkey,
                            received_at_iso=received_at,
                            answer_hash=_receipt_answer_hash(hermes_run.stdout),
                            recorded_at_iso=_ms_iso(datetime.now(UTC)),
                        )
                        # Fallback runners may not support the stdout callback;
                        # still claim ownership in the same write gate as the
                        # receipt insert so reconciliation cannot steal it.
                        receipt = await receipt_store.update_status(
                            family_id=receipt.family_id,
                            challenge_id=receipt.challenge_id,
                            submission_id=receipt.submission_id,
                            status=RECEIPT_STATUS_VERIFYING,
                            now_iso=_ms_iso(datetime.now(UTC)),
                        )
                        _start_receipt_heartbeat(
                            receipt,
                            callback_receipt_store=receipt_store,
                        )
                # Deterministic SAT scoring may stream a large file-backed CNF.
                # Take receipt ownership before scoring so scheduler
                # reconciliation cannot expire this in-progress verifier as an
                # abandoned unverified receipt.
                await self._mark_sat_receipt_verifying(
                    receipt_store=receipt_store,
                    receipt=receipt,
                )
                try:
                    signed = await self._score_and_sign_task_family_stdout(
                        lane=lane,
                        problem=problem,
                        hidden=hidden,
                        submission_row=submission,
                        stdout=hermes_run.stdout,
                        ran_at_iso=_ms_iso(datetime.now(UTC)),
                        epoch_salt=epoch_salt,
                    )
                    assert receipt is not None
                    await self._finalize_sat_receipt_ordered_result(
                        receipt_store=receipt_store,
                        receipt=receipt,
                        lane=lane,
                        problem=problem,
                        hidden=hidden,
                        submission=submission,
                        hermes_run=hermes_run,
                        signed=signed,
                        epoch=epoch,
                        round_index=round_index,
                        epoch_salt=epoch_salt,
                        log=log,
                    )
                finally:
                    await _stop_receipt_heartbeat()
                log.info(
                    "task_family_eval_complete",
                    family_id=family_id,
                    task_id_public=signed.row.get("task_id_public"),
                    weighted_score=signed.row.get("weighted_score"),
                )
                ran_any = True
                continue
            signed = await self._score_and_sign_task_family_stdout(
                lane=lane,
                problem=problem,
                hidden=hidden,
                submission_row=submission,
                stdout=hermes_run.stdout,
                ran_at_iso=_ms_iso(datetime.now(UTC)),
                epoch_salt=epoch_salt,
            )
            challenge_lock = self._task_family_challenge_lock
            sat_lock_candidate = (
                family_id == SYNTHETIC_BOOLEAN_FAMILY_ID
                and challenge_lock is not None
                and float(signed.score.weighted_score) >= 1.0
            )
            locked = None
            promoted = None

            async def _build_trace_json(run: Any = hermes_run) -> dict[str, Any]:
                trace_bundle = getattr(run, "trace_bundle", None)
                published_artifact = await self._maybe_publish_bundle(trace_bundle, log)
                out: dict[str, Any] = dict(run.trace or {})
                if trace_bundle is not None:
                    out["bundle_blake3"] = trace_bundle.bundle_blake3
                    out["cathedral_eval_round"] = trace_bundle.cathedral_eval_round
                if published_artifact is not None:
                    out["bundle_url"] = published_artifact.bundle_url
                    out["manifest_url"] = published_artifact.manifest_url
                    out["manifest_hash"] = published_artifact.manifest_hash
                return out

            if sat_lock_candidate:
                assert challenge_lock is not None
                trace_json = await _build_trace_json()
                async with self._db_write_lock:
                    await self.db.execute("BEGIN IMMEDIATE")
                    try:
                        locked = await challenge_lock.try_lock(
                            family_id=family_id,
                            challenge_id=problem.task_id,
                            miner_hotkey=miner_hotkey,
                            eval_run_id=str(signed.row["id"]),
                            weighted_score=float(signed.score.weighted_score),
                            won_at_iso=str(signed.row["ran_at"]),
                            commit=False,
                        )
                        if locked is None:
                            signed = await self._score_and_sign_task_family_stdout(
                                lane=lane,
                                problem=problem,
                                hidden=hidden,
                                submission_row=submission,
                                stdout=hermes_run.stdout,
                                ran_at_iso=str(signed.row["ran_at"]),
                                eval_run_id=str(signed.row["id"]),
                                epoch_salt=epoch_salt,
                                score_override=ScoreResult(
                                    weighted_score=0.0,
                                    rejection_reason="challenge_already_locked",
                                    score_parts={"binary_correct": 0.0, "lock_winner": 0.0},
                                ),
                            )

                        await persist_task_family_result(
                            self.db,
                            submission_row=submission,
                            problem=problem,
                            signed=signed,
                            epoch=epoch,
                            round_index=round_index,
                            duration_ms=int(hermes_run.duration_ms),
                            trace_json=trace_json,
                            feed_enabled=True,
                            commit=False,
                        )
                        challenge_source = self._task_family_challenge_source
                        if locked is not None and challenge_source is not None:
                            promoted = (
                                await challenge_source.mark_locked_and_promote_next(
                                    family_id=family_id,
                                    challenge_id=problem.task_id,
                                    now_iso=str(signed.row["ran_at"]),
                                    manage_transaction=False,
                                    active_scope="tier",
                                )
                            )
                        await self.db.commit()
                    except Exception:
                        await self.db.rollback()
                        raise

                if locked is None:
                    log.info(
                        "task_family_challenge_already_locked",
                        family_id=family_id,
                        challenge_id_public=signed.row.get("task_id_public"),
                        miner_hotkey=miner_hotkey,
                    )
                else:
                    log.info(
                        "task_family_challenge_winner_recorded",
                        family_id=family_id,
                        challenge_id_public=signed.row.get("task_id_public"),
                        miner_hotkey=miner_hotkey,
                    )
                    log.info(
                        "task_family_challenge_locked",
                        family_id=family_id,
                        challenge_id_public=signed.row.get("task_id_public"),
                        miner_hotkey=miner_hotkey,
                        promoted_challenge_id=(promoted.challenge_id if promoted else None),
                    )
            else:
                trace_json = await _build_trace_json()
                async with self._db_write_lock:
                    await persist_task_family_result(
                        self.db,
                        submission_row=submission,
                        problem=problem,
                        signed=signed,
                        epoch=epoch,
                        round_index=round_index,
                        duration_ms=int(hermes_run.duration_ms),
                        trace_json=trace_json,
                        feed_enabled=True,
                    )
            log.info(
                "task_family_eval_complete",
                family_id=family_id,
                task_id_public=signed.row.get("task_id_public"),
                weighted_score=signed.row.get("weighted_score"),
            )
            ran_any = True
        return ran_any

    async def _mark_sat_receipt_verifying(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        receipt: ChallengeReceipt,
    ) -> None:
        # updated_at_iso is the verifier heartbeat used by crash recovery.
        # It must reflect when this process owns the receipt, not when stdout
        # first arrived, or slow live verifiers can look stale.
        verifying_started_iso = _ms_iso(datetime.now(UTC))
        async with self._db_write_lock:
            await receipt_store.update_status(
                family_id=receipt.family_id,
                challenge_id=receipt.challenge_id,
                submission_id=receipt.submission_id,
                status=RECEIPT_STATUS_VERIFYING,
                now_iso=verifying_started_iso,
            )

    async def _run_sat_receipt_heartbeat(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        receipt: ChallengeReceipt,
        stop: asyncio.Event,
    ) -> None:
        """Refresh a live SAT verifier's ownership until scoring finalizes.

        Receipt reconciliation expires stale ``verifying`` rows to recover
        from crashed publishers. Long file-backed verification and trace
        collection are legitimate live work, so the owner must keep
        ``updated_at_iso`` fresh for the whole scoring/finalization window.
        """
        interval = max(
            0.01,
            min(
                _SAT_RECEIPT_HEARTBEAT_INTERVAL.total_seconds(),
                _SAT_VERIFYING_STALE_TIMEOUT.total_seconds() / 3,
            ),
        )
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

            try:
                async with self._db_write_lock:
                    current = await receipt_store.get(
                        family_id=receipt.family_id,
                        challenge_id=receipt.challenge_id,
                        submission_id=receipt.submission_id,
                    )
                    if current is None or current.status != RECEIPT_STATUS_VERIFYING:
                        return
                    await receipt_store.update_status(
                        family_id=receipt.family_id,
                        challenge_id=receipt.challenge_id,
                        submission_id=receipt.submission_id,
                        status=RECEIPT_STATUS_VERIFYING,
                        now_iso=_ms_iso(datetime.now(UTC)),
                    )
            except Exception as exc:
                logger.warning(
                    "task_family_receipt_heartbeat_failed",
                    family_id=receipt.family_id,
                    challenge_id=receipt.challenge_id,
                    submission_id=receipt.submission_id,
                    error=str(exc),
                )
                return

    async def _score_and_sign_task_family_stdout(
        self,
        *,
        lane: Any,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission_row: dict[str, Any],
        stdout: str,
        ran_at_iso: str,
        epoch_salt: str,
        eval_run_id: str | None = None,
        score_override: ScoreResult | None = None,
    ) -> TaskFamilySignedResult:
        # File-backed SAT verification streams, hashes, and evaluates the CNF.
        # Run the scorer in a worker thread from async eval paths so large
        # launch CNFs do not stall receipt reconciliation, CNF serving, or
        # unrelated evaluations on the publisher event loop.
        return await asyncio.to_thread(
            score_and_sign_task_family_stdout,
            lane=lane,
            problem=problem,
            hidden=hidden,
            submission_row=submission_row,
            stdout=stdout,
            ran_at_iso=ran_at_iso,
            signer=self.signer,
            eval_run_id=eval_run_id,
            epoch_salt=epoch_salt,
            score_override=score_override,
        )

    async def _eval_run_exists(self, eval_run_id: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM eval_runs WHERE id = ? LIMIT 1",
            (eval_run_id,),
        )
        row = await cur.fetchone()
        return row is not None

    async def _finalize_sat_receipt_ordered_result(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        receipt: ChallengeReceipt,
        lane: Any,
        problem: PublicProblem,
        hidden: HiddenMetadata,
        submission: dict[str, Any],
        hermes_run: Any,
        signed: TaskFamilySignedResult,
        epoch: int,
        round_index: int,
        epoch_salt: str,
        log: structlog.stdlib.BoundLogger,
    ) -> None:
        """Persist SAT rows under first-submitted valid-receipt ordering.

        A valid answer does not immediately win. It becomes eligible only
        when every earlier receipt for the same challenge has resolved
        invalid/expired. Invalid answers can be published immediately as
        zero-score rows. The winning valid row is persisted before the
        challenge lock/source are advanced so a persistence failure cannot
        silently retire the formula.
        """

        async def _build_trace_json(run: Any = hermes_run) -> dict[str, Any]:
            trace_bundle = getattr(run, "trace_bundle", None)
            published_artifact = await self._maybe_publish_bundle(trace_bundle, log)
            out: dict[str, Any] = dict(run.trace or {})
            if trace_bundle is not None:
                out["bundle_blake3"] = trace_bundle.bundle_blake3
                out["cathedral_eval_round"] = trace_bundle.cathedral_eval_round
            if published_artifact is not None:
                out["bundle_url"] = published_artifact.bundle_url
                out["manifest_url"] = published_artifact.manifest_url
                out["manifest_hash"] = published_artifact.manifest_hash
            return out

        now_iso = str(signed.row["ran_at"])
        trace_json = await _build_trace_json()
        is_valid = float(signed.score.weighted_score) >= 1.0
        terminal_status = RECEIPT_STATUS_VALID if is_valid else RECEIPT_STATUS_INVALID
        async with self._db_write_lock:
            # The signed result payload and terminal status are one durability
            # boundary. A crash with signed_row persisted but status still
            # "verifying" lets receipt reconciliation expire a first valid
            # solution and incorrectly promote a later receipt.
            if isinstance(receipt_store, SqliteChallengeReceiptStore):
                await self.db.execute("BEGIN IMMEDIATE")
                try:
                    await receipt_store.attach_result(
                        family_id=receipt.family_id,
                        challenge_id=receipt.challenge_id,
                        submission_id=receipt.submission_id,
                        eval_run_id=str(signed.row["id"]),
                        signed_row=signed.row,
                        trace_json=trace_json,
                        duration_ms=int(hermes_run.duration_ms),
                        epoch=epoch,
                        round_index=round_index,
                        now_iso=now_iso,
                        commit=False,
                    )
                    await receipt_store.update_status(
                        family_id=receipt.family_id,
                        challenge_id=receipt.challenge_id,
                        submission_id=receipt.submission_id,
                        status=terminal_status,
                        now_iso=now_iso,
                        rejection_reason=None if is_valid else signed.row.get("rejection_reason"),
                        verifier_details_hash=str(signed.row["verifier_details_hash"]),
                        commit=False,
                    )
                    await self.db.commit()
                except Exception:
                    await self.db.rollback()
                    raise
            else:
                await receipt_store.attach_result(
                    family_id=receipt.family_id,
                    challenge_id=receipt.challenge_id,
                    submission_id=receipt.submission_id,
                    eval_run_id=str(signed.row["id"]),
                    signed_row=signed.row,
                    trace_json=trace_json,
                    duration_ms=int(hermes_run.duration_ms),
                    epoch=epoch,
                    round_index=round_index,
                    now_iso=now_iso,
                )
                await receipt_store.update_status(
                    family_id=receipt.family_id,
                    challenge_id=receipt.challenge_id,
                    submission_id=receipt.submission_id,
                    status=terminal_status,
                    now_iso=now_iso,
                    rejection_reason=None if is_valid else signed.row.get("rejection_reason"),
                    verifier_details_hash=str(signed.row["verifier_details_hash"]),
                )

        async def _maybe_finalize_ready_winner(*, publish_current_loser: bool) -> None:
            await self._expire_stale_sat_receipts(
                receipt_store=receipt_store,
                receipt=receipt,
                problem=problem,
                now_iso=now_iso,
            )
            await self._finalize_ready_sat_winner(
                receipt_store=receipt_store,
                family_id=receipt.family_id,
                challenge_id=receipt.challenge_id,
                problem=problem,
                log=log,
                current_receipt=receipt if publish_current_loser else None,
                current_lane=lane if publish_current_loser else None,
                current_hidden=hidden if publish_current_loser else None,
                current_submission=submission if publish_current_loser else None,
                current_hermes_run=hermes_run if publish_current_loser else None,
                current_signed=signed if publish_current_loser else None,
                current_epoch=epoch if publish_current_loser else None,
                current_round_index=round_index if publish_current_loser else None,
                current_epoch_salt=epoch_salt if publish_current_loser else None,
                current_trace_json=trace_json if publish_current_loser else None,
                log_waiting=publish_current_loser,
            )

        if not is_valid:
            async with self._db_write_lock:
                await persist_task_family_result(
                    self.db,
                    submission_row=submission,
                    problem=problem,
                    signed=signed,
                    epoch=epoch,
                    round_index=round_index,
                    duration_ms=int(hermes_run.duration_ms),
                    trace_json=trace_json,
                    feed_enabled=True,
                )
            # An invalid/expired earlier receipt can unblock a later valid
            # receipt that already resolved and is waiting in the receipt table.
            await _maybe_finalize_ready_winner(publish_current_loser=False)
            return

        await _maybe_finalize_ready_winner(publish_current_loser=True)

    async def _finalize_ready_sat_winner(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        family_id: str,
        challenge_id: str,
        problem: PublicProblem,
        log: structlog.stdlib.BoundLogger,
        current_receipt: ChallengeReceipt | None = None,
        current_lane: Any | None = None,
        current_hidden: HiddenMetadata | None = None,
        current_submission: dict[str, Any] | None = None,
        current_hermes_run: Any | None = None,
        current_signed: TaskFamilySignedResult | None = None,
        current_epoch: int | None = None,
        current_round_index: int | None = None,
        current_epoch_salt: str | None = None,
        current_trace_json: dict[str, Any] | None = None,
        log_waiting: bool = False,
    ) -> bool:
        selected_winner = await receipt_store.select_winner(
            family_id=family_id,
            challenge_id=challenge_id,
        )
        if selected_winner is None:
            if log_waiting and current_receipt is not None:
                log.info(
                    "task_family_receipt_waiting_for_earlier",
                    family_id=family_id,
                    challenge_id_public=(
                        current_signed.row.get("task_id_public")
                        if current_signed is not None
                        else None
                    ),
                    submission_id=current_receipt.submission_id,
                )
            return False

        challenge_lock = self._task_family_challenge_lock

        async def _publish_current_as_loser_if_unpublished(
            winner: ChallengeReceipt,
        ) -> None:
            if (
                current_receipt is None
                or current_lane is None
                or current_hidden is None
                or current_submission is None
                or current_hermes_run is None
                or current_signed is None
                or current_epoch is None
                or current_round_index is None
                or current_epoch_salt is None
                or winner.submission_id == current_receipt.submission_id
            ):
                return
            if await self._eval_run_exists(str(current_signed.row["id"])):
                return
            loser = self._sat_loser_result(
                original=current_signed,
                reason="challenge_already_locked",
            )
            await persist_task_family_result(
                self.db,
                submission_row=current_submission,
                problem=problem,
                signed=loser,
                epoch=current_epoch,
                round_index=current_round_index,
                duration_ms=int(current_hermes_run.duration_ms),
                trace_json=current_trace_json,
                feed_enabled=True,
            )

        winner_to_publish_losers: ChallengeReceipt | None = None
        promoted_for_log = None
        async with self._db_write_lock:
            # Re-select inside the process lock. Invalid/expired receipts can
            # unblock a valid receipt that previously waited behind them, and
            # concurrent tasks can otherwise act on stale winner state.
            winner = await receipt_store.select_winner(
                family_id=selected_winner.family_id,
                challenge_id=selected_winner.challenge_id,
            )
            if winner is None:
                return False
            if winner.signed_row is None or winner.eval_run_id is None:
                log.warning(
                    "task_family_winner_missing_private_payload",
                    family_id=winner.family_id,
                    challenge_id=winner.challenge_id,
                    submission_id=winner.submission_id,
                )
                return False

            # The lock read has to happen inside the process lock: any earlier
            # read can become stale while another task is committing the same
            # challenge winner on this shared SQLite connection.
            existing_lock = (
                await challenge_lock.get_winner(
                    family_id=winner.family_id,
                    challenge_id=winner.challenge_id,
                )
                if challenge_lock is not None
                else None
            )

            if existing_lock is not None:
                await _publish_current_as_loser_if_unpublished(winner)
                return False

            winner_submission = _task_family_receipt_submission_row(winner)
            winner_result = _task_family_signed_result_from_row(winner.signed_row)
            locked = None
            promoted = None
            # Use the transaction-time lock timestamp for challenge-source
            # updated_at_iso. The CNF endpoint's locked grace window is
            # anchored there, and winner.signed_row["ran_at"] may be minutes
            # older if the valid receipt waited behind earlier receipts.
            lock_commit_iso = _ms_iso(datetime.now(UTC))
            lock_lost_after_recheck = False
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                result_to_persist = winner_result
                if challenge_lock is not None:
                    locked = await challenge_lock.try_lock(
                        family_id=winner.family_id,
                        challenge_id=winner.challenge_id,
                        miner_hotkey=winner.miner_hotkey,
                        eval_run_id=winner.eval_run_id,
                        weighted_score=float(winner.signed_row["weighted_score"]),
                        won_at_iso=lock_commit_iso,
                        commit=False,
                    )
                    if locked is None:
                        # The earlier get_winner() is only a stale-read guard.
                        # A separate publisher process can still win the
                        # INSERT OR IGNORE race before this transaction reaches
                        # try_lock(). Never publish this receipt as full score
                        # unless this process actually owns the durable lock.
                        result_to_persist = self._sat_loser_result(
                            original=winner_result,
                            reason="challenge_already_locked",
                        )
                        lock_lost_after_recheck = True
                await persist_task_family_result(
                    self.db,
                    submission_row=winner_submission,
                    problem=problem,
                    signed=result_to_persist,
                    epoch=int(winner.epoch if winner.epoch is not None else 0),
                    round_index=int(winner.round_index if winner.round_index is not None else 0),
                    duration_ms=int(winner.duration_ms if winner.duration_ms is not None else 0),
                    trace_json=winner.trace_json,
                    feed_enabled=True,
                    commit=False,
                )
                if locked is not None and self._task_family_challenge_source is not None:
                    promoted = (
                        await self._task_family_challenge_source.mark_locked_and_promote_next(
                            family_id=winner.family_id,
                            challenge_id=winner.challenge_id,
                            now_iso=lock_commit_iso,
                            manage_transaction=False,
                            active_scope="tier",
                        )
                        )
                await self.db.commit()
                if not lock_lost_after_recheck:
                    winner_to_publish_losers = winner
                    promoted_for_log = promoted
            except Exception:
                await self.db.rollback()
                raise

            if lock_lost_after_recheck:
                log.info(
                    "task_family_challenge_already_locked",
                    family_id=winner.family_id,
                    challenge_id_public=winner.signed_row.get("task_id_public"),
                    miner_hotkey=winner.miner_hotkey,
                )
                return False

            if locked is not None:
                log.info(
                    "task_family_challenge_locked",
                    family_id=winner.family_id,
                    challenge_id_public=winner.signed_row.get("task_id_public"),
                    miner_hotkey=winner.miner_hotkey,
                    promoted_challenge_id=(
                        promoted_for_log.challenge_id if promoted_for_log else None
                    ),
                )

        if winner_to_publish_losers is not None:
            await self._publish_resolved_sat_losers(
                receipt_store=receipt_store,
                winner=winner_to_publish_losers,
                problem=problem,
                reason="challenge_already_locked",
            )
            if self._task_family_challenge_source is not None:
                await self._mark_locked_sat_losers_published(
                    challenge_source=self._task_family_challenge_source,
                    family_id=winner_to_publish_losers.family_id,
                    challenge_id=winner_to_publish_losers.challenge_id,
                )
            return True
        return False

    async def _mark_locked_sat_losers_published(
        self,
        *,
        challenge_source: ChallengeSource,
        family_id: str,
        challenge_id: str,
    ) -> None:
        async with self._db_write_lock:
            # This marker bounds scheduler reconciliation: once all currently
            # valid loser receipts for a locked challenge have been published
            # or observed as already persisted, future ticks can skip the old
            # formula without re-announcing it and re-walking receipts.
            await challenge_source.mark_locked_loser_reconciliation_complete(
                family_id=family_id,
                challenge_id=challenge_id,
                now_iso=_ms_iso(datetime.now(UTC)),
            )

    async def _publish_resolved_sat_losers(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        winner: ChallengeReceipt,
        problem: PublicProblem,
        reason: str,
    ) -> int:
        published = 0
        for candidate in await receipt_store.list_for_challenge(
            family_id=winner.family_id,
            challenge_id=winner.challenge_id,
        ):
            if candidate.submission_id == winner.submission_id:
                continue
            if candidate.status != RECEIPT_STATUS_VALID or candidate.signed_row is None:
                continue
            loser_row = resign_task_family_score(
                candidate.signed_row,
                signer=self.signer,
                weighted_score=0.0,
                score_parts={"binary_correct": 0.0, "receipt_winner": 0.0},
                rejection_reason=reason,
            )
            async with self._db_write_lock:
                # Loser publication commits on the same aiosqlite connection
                # used by SAT winner transactions. Keep the duplicate check
                # and insert behind the shared write gate so this commit cannot
                # close another task's BEGIN IMMEDIATE window early.
                candidate_eval_run_id = candidate.eval_run_id or candidate.signed_row.get("id")
                if candidate_eval_run_id is not None and await self._eval_run_exists(
                    str(candidate_eval_run_id)
                ):
                    continue
                await persist_task_family_result(
                    self.db,
                    submission_row=_task_family_receipt_submission_row(candidate),
                    problem=problem,
                    signed=_task_family_signed_result_from_row(loser_row),
                    epoch=int(candidate.epoch if candidate.epoch is not None else 0),
                    round_index=int(
                        candidate.round_index if candidate.round_index is not None else 0
                    ),
                    duration_ms=int(
                        candidate.duration_ms if candidate.duration_ms is not None else 0
                    ),
                    trace_json=candidate.trace_json,
                    feed_enabled=True,
                )
                published += 1
        return published

    async def reconcile_sat_receipts(
        self,
        *,
        log: structlog.stdlib.BoundLogger,
    ) -> int:
        """Expire stale SAT receipt blockers and finalize any unblocked winner.

        This runs from scheduler ticks, not only from miner submissions. That
        gives a later valid receipt a deterministic re-check after an earlier
        unverified receipt times out, even if no further miner answers arrive.
        """
        receipt_store = self._task_family_receipt_store
        challenge_source = self._task_family_challenge_source
        if receipt_store is None or challenge_source is None:
            return 0
        if SYNTHETIC_BOOLEAN_FAMILY_ID not in enabled_task_family_ids():
            return 0

        finalized = 0
        try:
            active_records = await challenge_source.list_for_family(
                SYNTHETIC_BOOLEAN_FAMILY_ID,
                status=CHALLENGE_STATUS_ACTIVE,
            )
            locked_records = await challenge_source.list_locked_needing_loser_reconciliation(
                SYNTHETIC_BOOLEAN_FAMILY_ID,
                limit=_SAT_LOCKED_RECONCILE_LIMIT,
            )
        except Exception as exc:
            log.warning("task_family_receipt_reconcile_failed", error=str(exc)[:256])
            return 0

        for record in active_records:
            announced = await self.announce_synthetic_boolean_problem(
                record,
                log=log,
                family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
            )
            if announced is None:
                continue
            problem, _hidden = announced
            now_iso = _ms_iso(datetime.now(UTC))
            await self._expire_stale_sat_receipts_for_challenge(
                receipt_store=receipt_store,
                family_id=record.family_id,
                challenge_id=record.challenge_id,
                problem=problem,
                now_iso=now_iso,
            )
            if await self._finalize_ready_sat_winner(
                receipt_store=receipt_store,
                family_id=record.family_id,
                challenge_id=record.challenge_id,
                problem=problem,
                log=log,
            ):
                finalized += 1
        for record in locked_records:
            winner = await receipt_store.select_winner(
                family_id=record.family_id,
                challenge_id=record.challenge_id,
            )
            if winner is None or winner.signed_row is None:
                # Older publishers could lock SAT challenges before receipt
                # rows existed. There is no signed payload to publish, so mark
                # the legacy lock reconciled; otherwise the bounded dirty query
                # would pick the same historical row on every scheduler tick.
                await self._mark_locked_sat_losers_published(
                    challenge_source=challenge_source,
                    family_id=record.family_id,
                    challenge_id=record.challenge_id,
                )
                continue
            announced = await self.announce_synthetic_boolean_problem(
                record,
                log=log,
                family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
            )
            if announced is None:
                continue
            problem, _hidden = announced
            # Winner/lock/source commits before loser publication. If the
            # publisher exits in that gap, reconciliation revisits only locked
            # challenges without the durable loser-published marker; successful
            # publication sets the marker so historical solved formulas do not
            # get re-announced and receipt-walked on every scheduler tick.
            finalized += await self._publish_resolved_sat_losers(
                receipt_store=receipt_store,
                winner=winner,
                problem=problem,
                reason="challenge_already_locked",
            )
            await self._mark_locked_sat_losers_published(
                challenge_source=challenge_source,
                family_id=winner.family_id,
                challenge_id=winner.challenge_id,
            )
        return finalized

    async def _expire_stale_sat_receipts(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        receipt: ChallengeReceipt,
        problem: PublicProblem,
        now_iso: str,
    ) -> None:
        await self._expire_stale_sat_receipts_for_challenge(
            receipt_store=receipt_store,
            family_id=receipt.family_id,
            challenge_id=receipt.challenge_id,
            problem=problem,
            now_iso=now_iso,
        )

    async def _expire_stale_sat_receipts_for_challenge(
        self,
        *,
        receipt_store: ChallengeReceiptStore,
        family_id: str,
        challenge_id: str,
        problem: PublicProblem,
        now_iso: str,
    ) -> int:
        try:
            now = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            return 0
        cutoff = now - timedelta(seconds=int(problem.time_limit_seconds))
        cutoff_iso = _ms_iso(cutoff)
        verifying_cutoff_iso = _ms_iso(now - _SAT_VERIFYING_STALE_TIMEOUT)
        async with self._db_write_lock:
            return await receipt_store.expire_unresolved_before(
                family_id=family_id,
                challenge_id=challenge_id,
                cutoff_received_at_iso=cutoff_iso,
                now_iso=now_iso,
                rejection_reason="receipt_timed_out",
                verifying_cutoff_updated_at_iso=verifying_cutoff_iso,
            )

    def _sat_loser_result(
        self,
        *,
        original: TaskFamilySignedResult,
        reason: str,
    ) -> TaskFamilySignedResult:
        # The answer was already verified to produce ``original``. Re-sign
        # that hash-only row as a loser instead of re-running file-backed SAT
        # verification on the event loop.
        loser_row = resign_task_family_score(
            original.row,
            signer=self.signer,
            weighted_score=0.0,
            score_parts={"binary_correct": 0.0, "receipt_winner": 0.0},
            rejection_reason=reason,
        )
        return _task_family_signed_result_from_row(loser_row)

    async def announce_synthetic_boolean_problem(
        self,
        record: ChallengeRecord,
        *,
        log: structlog.stdlib.BoundLogger,
        family_id: str,
    ) -> tuple[PublicProblem, HiddenMetadata] | None:
        """Mint (or reuse) a fetch token and build lane inputs for one record.

        Returns ``None`` and logs a structured warning when the feed is
        enabled but ``CATHEDRAL_PUBLIC_BASE_URL`` is unset: a broken URL
        in a miner prompt is worse than skipping a tick. Returns ``None``
        when the token store isn't wired (test paths that pass the
        challenge source but skip token plumbing). The caller treats
        ``None`` the same way it treats a missing challenge.
        """
        if not self._public_base_url:
            log.warning(
                "task_family_skipped",
                family_id=family_id,
                reason="missing_public_base_url",
                required_env="CATHEDRAL_PUBLIC_BASE_URL",
            )
            return None
        if self._task_family_fetch_token_store is None:
            log.warning(
                "task_family_skipped",
                family_id=family_id,
                reason="missing_fetch_token_store",
            )
            return None
        # INSERT OR IGNORE inside mint_if_absent guarantees every miner
        # in the same announcement window gets the same token; a re-mint
        # on a later tick is a no-op once the row exists. The lane's
        # DEFAULT_TIME_LIMIT_SECONDS is captured at mint time so the
        # endpoint's grace window survives later tier/release tweaks.
        from cathedral.lanes.synthetic_boolean_v1 import DEFAULT_TIME_LIMIT_SECONDS

        token = secrets.token_urlsafe(32)
        minted_at_iso = _ms_iso(datetime.now(UTC))
        try:
            async with self._db_write_lock:
                row = await self._task_family_fetch_token_store.mint_if_absent(
                    record.challenge_id,
                    fetch_token=token,
                    minted_at_iso=minted_at_iso,
                    announced_time_limit_secs=DEFAULT_TIME_LIMIT_SECONDS,
                )
        except Exception as exc:
            log.error(
                "task_family_skipped",
                family_id=family_id,
                reason="fetch_token_mint_failed",
                error=str(exc)[:256],
            )
            return None
        return problem_from_challenge_record(
            record,
            public_base_url=self._public_base_url,
            fetch_token=row.fetch_token,
            time_limit_seconds=DEFAULT_TIME_LIMIT_SECONDS,
        )

    async def snapshot_task_family_batch_problems(
        self,
        *,
        log: structlog.stdlib.BoundLogger,
    ) -> dict[str, tuple[PublicProblem, HiddenMetadata]]:
        """Snapshot batch-stable task-family problems.

        For the SAT first launch, a poll batch should race one active
        formula. Without this snapshot, miner A can solve and promote the
        next formula before miner B loads the task, so the same poll batch
        accidentally spans two formulas. Families without an active
        challenge source still fall back to per-miner generation.
        """
        out: dict[str, tuple[PublicProblem, HiddenMetadata]] = {}
        if not task_family_feed_enabled() or self._task_family_challenge_source is None:
            return out
        for family_id in enabled_task_family_ids():
            if family_id != SYNTHETIC_BOOLEAN_FAMILY_ID:
                continue
            try:
                lane_registry.lookup(family_id)
            except KeyError:
                log.warning("task_family_skipped", family_id=family_id, reason="unregistered")
                continue
            record = await self._task_family_challenge_source.get_active(family_id)
            if record is None:
                log.info("task_family_skipped", family_id=family_id, reason="no_active_challenge")
                continue
            announced = await self.announce_synthetic_boolean_problem(
                record, log=log, family_id=family_id
            )
            if announced is None:
                continue
            out[family_id] = announced
        return out

    async def _load_task_family_problem(
        self,
        *,
        lane: Any,
        family_id: str,
        miner_hotkey: str,
        epoch: int,
        round_index: int,
        issued_at_iso: str,
        log: structlog.stdlib.BoundLogger,
    ) -> tuple[PublicProblem | None, HiddenMetadata | None] | None:
        if family_id == SYNTHETIC_BOOLEAN_FAMILY_ID and self._task_family_challenge_source:
            record = await self._task_family_challenge_source.get_active(family_id)
            if record is None:
                log.info("task_family_skipped", family_id=family_id, reason="no_active_challenge")
                return None
            return await self.announce_synthetic_boolean_problem(
                record, log=log, family_id=family_id
            )

        tier = task_family_tier(family_id)
        ctx = build_generate_ctx(
            family_id=family_id,
            miner_hotkey=miner_hotkey,
            epoch=epoch,
            round_index=round_index,
            tier=tier,
            issued_at_iso=issued_at_iso,
        )
        try:
            generated: tuple[PublicProblem | None, HiddenMetadata | None] = lane.generate(ctx)
            return generated
        except NotImplementedError:
            return (None, None)
