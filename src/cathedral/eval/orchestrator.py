"""Eval scheduler loop (CONTRACTS.md Section 6 step 3).

SELECT submissions WHERE status='queued' ORDER BY submitted_at ASC
    LIMIT max_concurrent
per submission:
    1. UPDATE status='evaluating'
    2. resolve epoch + round_index for this card
    3. generate EvalTask deterministically
    4. fetch encrypted bundle from Hippius, decrypt to temp dir
    5. POST to Polaris orchestrator: spawn hermes container, run task
    6. capture container stdout last line as Card JSON
    7. terminate container, delete ephemeral volume
on Polaris API failure: retry up to 3x with exponential backoff
    (60s, 120s, 240s); after 3 failures, leave status='evaluating'
    for the operator dashboard
on bundle decryption failure: status='rejected'
on Card JSON parse failure: record EvalRun with errors=[...], score=0
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
import blake3
import structlog

from cathedral.cards.registry import CardRegistry
from cathedral.eval.polaris_runner import PolarisRunner, PolarisRunnerError
from cathedral.eval.scoring_pipeline import EvalSigner, score_and_sign
from cathedral.eval.task_generator import generate_task
from cathedral.lanes import registry as lane_registry
from cathedral.lanes.challenge_lock import ChallengeLock, SqliteChallengeLock
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
    SqliteChallengeSource,
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
from cathedral.publisher import repository
from cathedral.publisher.merkle import epoch_for
from cathedral.storage import (
    DecryptionError,
    HippiusClient,
    HippiusError,
    decrypt_bundle,
    safe_extract_zip,
)
from cathedral.storage.bundle_extractor import BundleStructureError
from cathedral.v3.corpus.private_loader import load_private_corpus
from cathedral.v3.corpus.sampler import sample_challenge_id_for_hotkey
from cathedral.v3.publisher import (
    persist_bug_isolation_result,
    publish_score_sidecar,
    score_and_sign_bug_isolation_stdout,
    v3_feed_enabled,
)
from cathedral.v3.score_sidecar import build_score_record

logger = structlog.get_logger(__name__)


_RETRY_BACKOFFS = (60, 120, 240)


# Cooldown applied to cadence-refresh rows that hit a failure path that
# does not advance MAX(eval_runs.ran_at). 1 hour is long enough to keep
# a permanently broken bundle from re-picking every tick, short enough
# that a transient outage recovers within the same cadence window.
_CADENCE_FAILURE_COOLDOWN = timedelta(hours=1)
# Verifiers refresh receipt.updated_at_iso when they take ownership. A stale
# heartbeat means the process died or task was cancelled after status=verifying.
_SAT_VERIFYING_STALE_TIMEOUT = timedelta(minutes=10)
_SAT_RECEIPT_HEARTBEAT_INTERVAL = timedelta(minutes=1)
_SAT_LOCKED_RECONCILE_LIMIT = 32


def _retry_backoffs() -> tuple[float, ...]:
    """Production retry policy (CONTRACTS.md §6 'Timeouts and policies').

    Tests set `CATHEDRAL_FAST_RETRIES=1` (or any `CATHEDRAL_EVAL_MODE`
    starting with `stub`) to keep ticks bounded — same 3-attempt policy
    but with zero sleep between attempts.
    """
    import os

    if os.environ.get("CATHEDRAL_FAST_RETRIES") == "1" or os.environ.get(
        "CATHEDRAL_EVAL_MODE", ""
    ).strip().lower().startswith("stub"):
        return (0.0, 0.0, 0.0)
    return _RETRY_BACKOFFS


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
class _RoundCounter:
    """Track per-card round_index across the current epoch."""

    epoch: int
    counter: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def next_index(self, card_id: str, current_epoch: int) -> int:
        if current_epoch != self.epoch:
            self.epoch = current_epoch
            self.counter.clear()
        idx = self.counter[card_id]
        self.counter[card_id] = idx + 1
        return idx


@dataclass
class _ReceiptHeartbeatState:
    stop: asyncio.Event | None = None
    task: asyncio.Task[Any] | None = None


class EvalOrchestrator:
    """Orchestrates the eval lifecycle for a single submission."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        hippius: HippiusClient,
        polaris: PolarisRunner | None = None,
        signer: EvalSigner,
        registry: CardRegistry,
        runner_for: Callable[[dict[str, Any]], PolarisRunner] | None = None,
        task_family_challenge_source: ChallengeSource | None = None,
        task_family_challenge_lock: ChallengeLock | None = None,
        task_family_fetch_token_store: SqliteFetchTokenStore | None = None,
        task_family_receipt_store: ChallengeReceiptStore | None = None,
        db_write_lock: asyncio.Lock | None = None,
        public_base_url: str | None = None,
    ) -> None:
        """Construct an orchestrator with either a single runner or a
        per-submission runner factory.

        `polaris` (legacy) — one runner used for every submission. Kept
        for back-compat with existing callers and tests.

        `runner_for` (preferred) — a callable `submission -> PolarisRunner`.
        Lets the orchestrator dispatch on the submission's
        `attestation_mode` so Tier A (polaris-hosted) goes to
        `PolarisRuntimeRunner`, BYO miners go to `BundleCardRunner`,
        and unverified discovery submissions are filtered out at the
        queue layer before reaching here.

        Exactly one of `polaris` or `runner_for` must be supplied;
        if both are present, `runner_for` wins.
        """
        if polaris is None and runner_for is None:
            raise ValueError("must supply polaris= or runner_for=")
        self.db = db
        self.hippius = hippius
        self._fixed_polaris = polaris
        self._runner_for = runner_for
        self.signer = signer
        self.registry = registry
        self._task_family_challenge_source = task_family_challenge_source
        self._task_family_challenge_lock = task_family_challenge_lock
        self._task_family_fetch_token_store = task_family_fetch_token_store
        self._task_family_receipt_store = task_family_receipt_store
        # SAT winner finalization opens explicit BEGIN/COMMIT windows on this
        # shared aiosqlite connection. This lock is injected from
        # PublisherContext in production so route handlers and orchestrator
        # writers share one transaction gate; otherwise an unrelated route
        # commit can close the finalizer transaction early.
        self._db_write_lock = db_write_lock or asyncio.Lock()
        # Constructor wins; env is the fallback. An empty string is
        # treated as missing. See _announce_synthetic_boolean_problem.
        self._public_base_url = (
            public_base_url or os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "") or ""
        ).strip()
        self._round_counter = _RoundCounter(epoch=epoch_for(datetime.now(UTC)))
        self._failure_counts: dict[str, int] = defaultdict(int)
        # Per-submission cooldown for cadence-refresh rows that hit a
        # retryable or terminal failure. Cadence rows stay 'ranked'
        # through failures (so the leaderboard doesn't churn), and the
        # cadence query uses MAX(eval_runs.ran_at) — which does NOT
        # advance on a failure that never reached score_and_sign. Without
        # this cooldown, a permanently broken bundle would re-pick on
        # every loop tick, spam logs, and consume cadence slots. Process-
        # local by design: a restart drops the cooldowns, which is
        # acceptable (the row will be picked once, fail once, and re-cool).
        self._cadence_cooldown_until: dict[str, datetime] = {}

    @property
    def polaris(self) -> PolarisRunner:
        """Back-compat: a few callers read `orch.polaris` directly. When
        a per-submission factory is configured this returns whatever the
        factory yields for an empty submission, which is fine for the
        cases that use this — they're inspecting type, not running."""
        if self._fixed_polaris is not None:
            return self._fixed_polaris
        assert self._runner_for is not None
        return self._runner_for({})

    def _resolve_runner(self, submission: dict[str, Any]) -> PolarisRunner:
        if self._runner_for is not None:
            return self._runner_for(submission)
        assert self._fixed_polaris is not None
        return self._fixed_polaris

    async def _maybe_publish_bundle(
        self,
        trace_bundle: Any,
        log: Any,
    ) -> Any | None:
        """If the runner produced a TraceBundle on disk AND the v2 emit
        flag is set, upload it to Hippius + sign the manifest. Returns
        the PublishedArtifact for score_and_sign to consume, or None.

        Failures here MUST NOT crash the eval — we log + return None so
        the eval still scores under v1 wire shape. The whole point of
        the dual-publish window is that v2 publishing is best-effort
        during the transition; the canonical record is still the v1
        signed payload until cutover.
        """
        if trace_bundle is None:
            return None
        import os as _os

        if _os.environ.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "").lower() != "true":
            # Flag's off — skip the Hippius round-trip entirely.
            # Storage cost matters here: every eval would otherwise
            # upload an encrypted tar.gz on every cadence tick even
            # though we're not emitting v2 on the wire.
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
            # Best-effort. v1 emission continues; the eval scores under
            # the legacy wire shape and the trace bundle stays on local
            # disk for retry on the next cadence tick.
            log.warning(
                "eval_artifact_publish_failed",
                error=str(exc),
            )
            return None

    async def evaluate_one(
        self,
        submission: dict[str, Any],
        *,
        v3_active_hotkeys: list[str] | None = None,
        task_family_problem_overrides: dict[str, tuple[PublicProblem, HiddenMetadata]]
        | None = None,
    ) -> None:
        log = logger.bind(submission_id=submission["id"], card_id=submission["card_id"])

        # Capture the entry status so failure paths know whether this is
        # a first-time eval (queued) or a cadence refresh of a previously
        # ranked row. A cadence refresh that fails must leave the row as
        # 'ranked' with its prior score intact — flipping to 'rejected'
        # would silently strip a working agent off the leaderboard.
        original_status = submission.get("status")
        is_cadence_refresh = original_status == "ranked"

        if submission.get("card_id") == SYNTHETIC_BOOLEAN_FAMILY_ID:
            if not is_cadence_refresh:
                async with self._db_write_lock:
                    await repository.update_submission_status(
                        self.db, submission["id"], status="evaluating"
                    )
            epoch = epoch_for(datetime.now(UTC))
            round_index = self._round_counter.next_index(SYNTHETIC_BOOLEAN_FAMILY_ID, epoch)
            runner = self._resolve_runner(submission)
            ran = await self._maybe_run_task_family_lanes(
                submission=submission,
                runner=runner,
                epoch=epoch,
                round_index=round_index,
                log=log,
                problem_overrides=task_family_problem_overrides,
            )
            async with self._db_write_lock:
                if ran:
                    cur = await self.db.execute(
                        "SELECT weighted_score FROM eval_runs "
                        "WHERE submission_id=? ORDER BY ran_at DESC LIMIT 1",
                        (submission["id"],),
                    )
                    row = await cur.fetchone()
                    await repository.update_submission_score(
                        self.db,
                        submission["id"],
                        current_score=float(row[0]) if row is not None else 0.0,
                        current_rank=0,
                    )
                elif not is_cadence_refresh:
                    await repository.update_submission_status(
                        self.db, submission["id"], status="pending_check"
                    )
            return

        card_def = await repository.get_card_definition(self.db, submission["card_id"])
        if card_def is None:
            await self._fail_terminal(
                submission,
                log,
                reason="card definition missing",
                is_cadence_refresh=is_cadence_refresh,
                event="eval_card_def_missing",
            )
            return

        # Only first-time rows get flipped to 'evaluating'. Cadence
        # refresh rows stay 'ranked' until score_and_sign commits a fresh
        # score (which sets status='ranked' again via update_submission_score),
        # so public leaderboard surfaces never see the row in an
        # in-flight 'evaluating' state.
        if not is_cadence_refresh:
            async with self._db_write_lock:
                await repository.update_submission_status(
                    self.db, submission["id"], status="evaluating"
                )

        # Generate deterministic task for this round
        epoch = epoch_for(datetime.now(UTC))
        round_index = self._round_counter.next_index(submission["card_id"], epoch)
        task = generate_task(
            card_id=submission["card_id"],
            epoch=epoch,
            round_index=round_index,
            card_definition=card_def,
        )

        # Fetch + decrypt bundle
        try:
            ciphertext = await self.hippius.get_bundle(submission["bundle_blob_key"])
        except HippiusError as e:
            await self._on_retryable_failure(
                submission, log, f"hippius get: {e}", is_cadence_refresh=is_cadence_refresh
            )
            return

        try:
            plaintext = decrypt_bundle(ciphertext, submission["encryption_key_id"])
        except DecryptionError as e:
            await self._fail_terminal(
                submission,
                log,
                reason="bundle decryption failed",
                is_cadence_refresh=is_cadence_refresh,
                event="eval_bundle_decrypt_failed",
                error=str(e),
            )
            return

        # Extract to ephemeral dir, then immediately drop the path —
        # Polaris will get the bundle bytes directly via the runner API
        # (we keep the extraction step here so adversarial-zip checks
        # still run). Wipe the dir afterwards regardless of outcome.
        tmp_root = Path(tempfile.mkdtemp(prefix="cathedral-eval-"))
        try:
            try:
                safe_extract_zip(plaintext, tmp_root)
            except BundleStructureError as e:
                await self._fail_terminal(
                    submission,
                    log,
                    reason=f"bundle structure: {e}",
                    is_cadence_refresh=is_cadence_refresh,
                    event="eval_bundle_structure_invalid",
                    error=str(e),
                )
                return

            polaris_errors: list[str] = []
            polaris_result = None
            backoffs = _retry_backoffs()
            # Per-submission dispatch: Tier A polaris-hosted miners
            # route to PolarisRuntimeRunner, BYO miners route to
            # BundleCardRunner. Discovery-mode rows are filtered out
            # before they reach the queue (publisher/submit.py).
            runner = self._resolve_runner(submission)
            for attempt, backoff in enumerate(backoffs, start=1):
                try:
                    polaris_result = await runner.run(
                        bundle_bytes=plaintext,
                        bundle_hash=submission["bundle_hash"],
                        task=task,
                        miner_hotkey=submission["miner_hotkey"],
                        submission=submission,
                    )
                    break
                except PolarisRunnerError as e:
                    polaris_errors.append(f"attempt {attempt}: {e}")
                    log.warning(
                        "eval_polaris_attempt_failed",
                        attempt=attempt,
                        error=str(e),
                    )
                    if attempt < len(backoffs) and backoff > 0:
                        await asyncio.sleep(backoff)

            if polaris_result is None:
                # Persist a zero-score eval_run with errors so the public
                # API surfaces the failure (CONTRACTS.md §6 step 3-4 — the
                # contract test asserts on either weighted_score=0 OR
                # errors!=None). Status moves to 'rejected' to match the
                # 'evaluating -> rejected' state machine arrow.
                self._failure_counts[submission["id"]] += 1
                log.error(
                    "eval_polaris_exhausted_retries",
                    errors=polaris_errors,
                )
                async with self._db_write_lock:
                    await score_and_sign(
                        self.db,
                        submission=submission,
                        epoch=epoch,
                        round_index=round_index,
                        polaris_agent_id="polaris-unavailable",
                        polaris_run_id=f"failed-{submission['id'][:8]}",
                        task_json=task.model_dump(mode="json"),
                        output_card_json={
                            "id": submission["card_id"],
                            "_polaris_unreachable": True,
                        },
                        duration_ms=0,
                        polaris_errors=polaris_errors or ["polaris exhausted retries"],
                        registry=self.registry,
                        signer=self.signer,
                    )
                # First-eval rows that exhaust retries get rejected. A
                # cadence refresh that exhausts retries leaves status as
                # 'ranked' — score_and_sign above already folded the 0
                # into the 30-day rolling avg, so the score itself
                # signals degradation without stripping the row off the
                # leaderboard.
                if not is_cadence_refresh:
                    async with self._db_write_lock:
                        await repository.update_submission_status(
                            self.db,
                            submission["id"],
                            status="rejected",
                            rejection_reason="polaris exhausted retries",
                        )
                return

            attestation_dict = (
                polaris_result.attestation.to_storage_dict()
                if polaris_result.attestation is not None
                else None
            )

            # v1.1.0 PR 5: when the runner produced a TraceBundle on disk
            # (currently only SshHermesRunner does this), upload it to
            # Hippius and sign the manifest. score_and_sign uses the
            # returned PublishedArtifact to emit a v2 signed payload
            # when CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true; until that flag
            # flips, the artifact is published but the v1 wire shape is
            # still emitted (dual-publish window).
            published_artifact = await self._maybe_publish_bundle(polaris_result.trace_bundle, log)

            async with self._db_write_lock:
                await score_and_sign(
                    self.db,
                    submission=submission,
                    epoch=epoch,
                    round_index=round_index,
                    polaris_agent_id=polaris_result.polaris_agent_id,
                    polaris_run_id=polaris_result.polaris_run_id,
                    task_json=task.model_dump(mode="json"),
                    output_card_json=polaris_result.output_card_json,
                    duration_ms=polaris_result.duration_ms,
                    polaris_errors=polaris_errors + polaris_result.errors,
                    registry=self.registry,
                    signer=self.signer,
                    polaris_attestation=attestation_dict,
                    # v2 additions — None for every runner except
                    # PolarisDeployRunner, which always populates them.
                    trace_json=polaris_result.trace,
                    polaris_manifest=polaris_result.manifest,
                    published_artifact=published_artifact,
                )
            try:
                await self._maybe_run_v3_bug_isolation(
                    submission=submission,
                    runner=runner,
                    epoch=epoch,
                    round_index=round_index,
                    log=log,
                    active_hotkeys=v3_active_hotkeys,
                )
            except Exception as exc:
                log.exception("v3_bug_isolation_failed", error=str(exc))
            try:
                await self._maybe_run_task_family_lanes(
                    submission=submission,
                    runner=runner,
                    epoch=epoch,
                    round_index=round_index,
                    log=log,
                    problem_overrides=task_family_problem_overrides,
                )
            except Exception as exc:
                log.exception("task_family_eval_failed", error=str(exc))
            log.info("eval_run_complete", epoch=epoch, round_index=round_index)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
            # Drop the plaintext binding so GC can reclaim it on the next
            # collector pass. Best-effort — Python doesn't guarantee
            # zeroing, but losing the only reference is the closest we
            # get without ctypes-level memzero.
            plaintext = b""

    async def _maybe_run_v3_bug_isolation(
        self,
        *,
        submission: dict[str, Any],
        runner: Any,
        epoch: int,
        round_index: int,
        log: structlog.stdlib.BoundLogger,
        active_hotkeys: list[str] | None = None,
    ) -> None:
        if not v3_feed_enabled():
            return

        run_challenge = getattr(runner, "run_bug_isolation_challenge", None)
        if run_challenge is None:
            log.info("v3_bug_isolation_skipped", reason="runner_unsupported")
            return

        corpus = load_private_corpus()
        if not corpus:
            log.info("v3_bug_isolation_skipped", reason="empty_corpus")
            return

        if active_hotkeys is None:
            active_hotkeys = await _v3_active_hotkeys_snapshot(self.db)
        miner_hotkey = str(submission["miner_hotkey"])
        if miner_hotkey not in active_hotkeys:
            active_hotkeys = [*active_hotkeys, miner_hotkey]

        corpus_by_id = {row.id: row for row in corpus}
        challenge_row_id = sample_challenge_id_for_hotkey(
            hotkey=miner_hotkey,
            active_hotkeys=active_hotkeys,
            corpus_ids=list(corpus_by_id),
            epoch_number=epoch,
        )
        challenge = corpus_by_id[challenge_row_id]
        hermes_run = await run_challenge(
            challenge=challenge,
            miner_hotkey=miner_hotkey,
            submission=submission,
        )
        signed = score_and_sign_bug_isolation_stdout(
            challenge=challenge,
            submission=submission,
            stdout=hermes_run.stdout,
            ran_at_iso=_ms_iso(datetime.now(UTC)),
            signer=self.signer,
            repair_stdout=getattr(hermes_run, "repair_stdout", None),
            epoch_salt=f"epoch_{epoch}:bug_isolation_v1",
        )

        # Publish the trace bundle + private score sidecar.
        # Both are best-effort: a sidecar upload failure must not crash
        # the eval. The score row + signed payload still land in the
        # publisher DB; the sidecar is data substrate for later training,
        # not a precondition for emitting the v3 row.
        trace_bundle = getattr(hermes_run, "trace_bundle", None)
        published_artifact = await self._maybe_publish_bundle(trace_bundle, log)
        score_sidecar_url: str | None = None
        if trace_bundle is not None and published_artifact is not None:
            try:
                score_record = build_score_record(
                    signed_row=signed.row,
                    challenge=challenge,
                    submission=submission,
                    duration_ms=int(hermes_run.duration_ms),
                    repair_was_attempted=signed.dispatch.repair_was_attempted,
                    package_blake3=trace_bundle.bundle_blake3,
                    manifest_hash=published_artifact.manifest_hash,
                )
                score_sidecar_url = await publish_score_sidecar(
                    hippius=self.hippius,
                    eval_id=trace_bundle.eval_id,
                    score_record=score_record,
                )
                log.info(
                    "v3_score_sidecar_published",
                    eval_id=trace_bundle.eval_id,
                    url=score_sidecar_url,
                )
            except Exception as exc:
                log.warning(
                    "v3_score_sidecar_publish_failed",
                    eval_id=getattr(trace_bundle, "eval_id", None),
                    error=str(exc),
                )

        # Enrich trace_json with bundle/manifest/sidecar handles so the
        # operator-only export and catalog can locate the package later.
        # These fields stay out of output_card_json (public feed firewall).
        trace_json: dict[str, Any] = dict(hermes_run.trace or {})
        if trace_bundle is not None:
            trace_json["bundle_blake3"] = trace_bundle.bundle_blake3
            trace_json["cathedral_eval_round"] = trace_bundle.cathedral_eval_round
        if published_artifact is not None:
            trace_json["bundle_url"] = published_artifact.bundle_url
            trace_json["manifest_url"] = published_artifact.manifest_url
            trace_json["manifest_hash"] = published_artifact.manifest_hash
        if score_sidecar_url is not None:
            trace_json["score_record_url"] = score_sidecar_url

        async with self._db_write_lock:
            await persist_bug_isolation_result(
                self.db,
                submission=submission,
                challenge=challenge,
                signed=signed,
                epoch=epoch,
                round_index=round_index,
                duration_ms=int(hermes_run.duration_ms),
                trace_json=trace_json,
            )
        log.info(
            "v3_bug_isolation_eval_complete",
            challenge_id_public=signed.row.get("challenge_id_public"),
            weighted_score=signed.row.get("weighted_score"),
        )

    async def _maybe_run_task_family_lanes(
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
            announced = await self._announce_synthetic_boolean_problem(
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
            announced = await self._announce_synthetic_boolean_problem(
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

    async def _announce_synthetic_boolean_problem(
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
            announced = await self._announce_synthetic_boolean_problem(
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
            return await self._announce_synthetic_boolean_problem(
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

    async def _on_retryable_failure(
        self,
        submission: dict[str, Any],
        log: structlog.stdlib.BoundLogger,
        reason: str,
        *,
        is_cadence_refresh: bool = False,
    ) -> None:
        self._failure_counts[submission["id"]] += 1
        if self._failure_counts[submission["id"]] >= 3:
            # Cadence refresh exhausted: keep the row 'ranked' with its
            # prior score — the cadence loop will pick it up again next
            # window. First-eval exhausted: terminal rejection.
            if is_cadence_refresh:
                self._arm_cadence_cooldown(submission["id"])
                log.error(
                    "eval_retryable_exhausted_cadence_kept_ranked",
                    reason=reason,
                )
            else:
                async with self._db_write_lock:
                    await repository.update_submission_status(
                        self.db,
                        submission["id"],
                        status="rejected",
                        rejection_reason=reason,
                    )
                log.error("eval_retryable_exhausted", reason=reason)
        else:
            if is_cadence_refresh:
                # Cadence rows are not in 'evaluating' (we never flipped
                # them) — nothing to restore. Arm a cooldown so a row
                # whose underlying bundle is permanently broken doesn't
                # re-pick on every tick (the cadence query uses
                # MAX(eval_runs.ran_at), which a failed retryable does
                # not advance).
                self._arm_cadence_cooldown(submission["id"])
                log.warning(
                    "eval_retry_cadence_kept_ranked",
                    reason=reason,
                    attempts=self._failure_counts[submission["id"]],
                )
            else:
                # First-eval: re-queue from 'evaluating' so the next tick
                # picks it up again.
                async with self._db_write_lock:
                    await repository.update_submission_status(
                        self.db, submission["id"], status="queued"
                    )
                log.warning(
                    "eval_retry_queued",
                    reason=reason,
                    attempts=self._failure_counts[submission["id"]],
                )

    def _arm_cadence_cooldown(self, submission_id: str) -> None:
        """Mark a cadence row as in-cooldown so the loop's batch picker
        skips it for `_CADENCE_FAILURE_COOLDOWN`. Idempotent — repeated
        failures just push the deadline forward."""
        self._cadence_cooldown_until[submission_id] = datetime.now(UTC) + _CADENCE_FAILURE_COOLDOWN

    def is_cadence_in_cooldown(self, submission_id: str) -> bool:
        deadline = self._cadence_cooldown_until.get(submission_id)
        if deadline is None:
            return False
        if datetime.now(UTC) >= deadline:
            # Cooldown expired; drop the entry so the dict doesn't grow
            # unboundedly across long-lived processes.
            del self._cadence_cooldown_until[submission_id]
            return False
        return True

    async def _fail_terminal(
        self,
        submission: dict[str, Any],
        log: structlog.stdlib.BoundLogger,
        *,
        reason: str,
        is_cadence_refresh: bool,
        event: str,
        error: str | None = None,
    ) -> None:
        """Terminal failure (e.g. decryption, structure, missing card_def).

        First-eval rows go to 'rejected' as before. Cadence refresh rows
        stay 'ranked' — a previously verified bundle failing decryption
        once should not strip the agent off the leaderboard. The cadence
        loop will pick it up again on the next tick, and if the failure
        is permanent the operator will see the repeated log lines.
        """
        if is_cadence_refresh:
            # No eval_run will be written (terminal failure before
            # score_and_sign), so MAX(ran_at) doesn't advance. Cool down
            # so the cadence picker doesn't immediately re-select.
            self._arm_cadence_cooldown(submission["id"])
            if error is not None:
                log.error(event + "_cadence_kept_ranked", reason=reason, error=error)
            else:
                log.warning(event + "_cadence_kept_ranked", reason=reason)
            return
        async with self._db_write_lock:
            await repository.update_submission_status(
                self.db,
                submission["id"],
                status="rejected",
                rejection_reason=reason,
            )
        if error is not None:
            log.error(event, error=error)
        else:
            log.warning(event)


# --------------------------------------------------------------------------
# Background loop
# --------------------------------------------------------------------------


async def run_eval_loop(
    *,
    db: aiosqlite.Connection,
    hippius: HippiusClient,
    polaris: PolarisRunner | None = None,
    runner_for: Callable[[dict[str, Any]], PolarisRunner] | None = None,
    signer: EvalSigner,
    registry: CardRegistry,
    poll_interval_secs: float = 10.0,
    max_concurrent: int = 2,
    stop: asyncio.Event | None = None,
    task_family_challenge_source: ChallengeSource | None = None,
    task_family_challenge_lock: ChallengeLock | None = None,
    task_family_fetch_token_store: SqliteFetchTokenStore | None = None,
    task_family_receipt_store: ChallengeReceiptStore | None = None,
    db_write_lock: asyncio.Lock | None = None,
    public_base_url: str | None = None,
) -> None:
    """Long-running scheduler: picks queued submissions and evals them.

    Pass either `polaris=` (legacy single runner) OR `runner_for=` (a
    callable that returns a runner per submission). Production wants
    `runner_for=` so polaris-tier submissions route to
    `PolarisRuntimeRunner` while BYO go to `BundleCardRunner` etc.

    Single-writer design: each submission is updated to 'evaluating'
    atomically before the work begins, so two concurrent loop iterations
    never pick the same row.
    """
    stop = stop or asyncio.Event()
    orchestrator = EvalOrchestrator(
        db=db,
        hippius=hippius,
        polaris=polaris,
        runner_for=runner_for,
        signer=signer,
        registry=registry,
        task_family_challenge_source=task_family_challenge_source,
        task_family_challenge_lock=task_family_challenge_lock,
        task_family_fetch_token_store=task_family_fetch_token_store,
        task_family_receipt_store=task_family_receipt_store,
        db_write_lock=db_write_lock,
        public_base_url=public_base_url,
    )
    sem = asyncio.Semaphore(max_concurrent)

    while not stop.is_set():
        try:
            await orchestrator.reconcile_sat_receipts(log=logger)
        except Exception as e:
            logger.warning("task_family_receipt_reconcile_failed", error=str(e)[:256])
        # v1.1.0 PR 5 — cadence scheduler. Two queue sources:
        #   1. status='queued' rows (first eval after submit)
        #   2. status='ranked' rows whose card cadence window expired
        # Merged into one batch per tick, capped at max_concurrent.
        # First-eval rows prioritized (they're new miners waiting).
        try:
            queued = await repository.queued_submissions(db, limit=max_concurrent)
        except aiosqlite.Error as e:
            logger.warning("eval_loop_query_failed", error=str(e))
            await _sleep_or_stop(stop, poll_interval_secs)
            continue

        remaining = max_concurrent - len(queued)
        due: list[dict[str, Any]] = []
        if remaining > 0:
            try:
                # Over-fetch then filter cooldown rows in-process. Without
                # the over-fetch, a slot taken by a cooled-down row would
                # leave the batch short even when other due rows exist.
                # Cap is bounded by the cadence-query limit semantics.
                raw_due = await repository.submissions_due_for_cadence(
                    db,
                    now=datetime.now(UTC),
                    limit=remaining * 4,
                )
                for row in raw_due:
                    if orchestrator.is_cadence_in_cooldown(row["id"]):
                        continue
                    due.append(row)
                    if len(due) >= remaining:
                        break
            except aiosqlite.Error as e:
                logger.warning("eval_loop_cadence_query_failed", error=str(e))
                # Cadence query failure should not block queued processing
                due = []

        batch = list(queued) + list(due)

        if not batch:
            await _sleep_or_stop(stop, poll_interval_secs)
            continue

        logger.info(
            "eval_loop_batch",
            queued_count=len(queued),
            cadence_count=len(due),
        )
        try:
            v3_active_hotkeys = await _v3_active_hotkeys_snapshot(db) if v3_feed_enabled() else None
        except aiosqlite.Error as e:
            logger.warning("v3_active_hotkeys_query_failed", error=str(e))
            v3_active_hotkeys = None
        try:
            task_family_problem_overrides = await orchestrator.snapshot_task_family_batch_problems(
                log=logger
            )
        except Exception as e:
            logger.warning("task_family_batch_snapshot_failed", error=str(e))
            task_family_problem_overrides = {}

        async def _process(
            s: dict[str, Any],
            active_hotkeys: list[str] | None = v3_active_hotkeys,
            problem_overrides: dict[str, tuple[PublicProblem, HiddenMetadata]] = (
                task_family_problem_overrides
            ),
        ) -> None:
            async with sem:
                try:
                    await orchestrator.evaluate_one(
                        s,
                        v3_active_hotkeys=active_hotkeys,
                        task_family_problem_overrides=problem_overrides,
                    )
                except Exception as e:
                    logger.exception("eval_one_crashed", submission_id=s["id"], error=str(e))
                    # An uncaught exception inside evaluate_one would
                    # otherwise strand the row. For first-eval (queued)
                    # rows that we flipped to 'evaluating', route through
                    # the existing 3-attempt retry policy so the row
                    # either re-queues or eventually rejects rather than
                    # sitting in 'evaluating' forever. Cadence rows
                    # stayed 'ranked' the whole time — nothing to do.
                    if s.get("status") in {"pending_check", "queued"}:
                        await orchestrator._on_retryable_failure(
                            s, logger.bind(submission_id=s["id"]), f"evaluate_one crash: {e}"
                        )

        await asyncio.gather(*[_process(s) for s in batch])


async def _v3_active_hotkeys_snapshot(db: aiosqlite.Connection) -> list[str]:
    ranked, _total = await repository.list_submissions_all(
        db,
        verified_only=True,
        ranked_only=True,
        limit=10000,
    )
    return [
        str(row["miner_hotkey"])
        for row in ranked
        if isinstance(row.get("miner_hotkey"), str) and row["miner_hotkey"]
    ]


async def _sleep_or_stop(stop: asyncio.Event, secs: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=secs)
    except TimeoutError:
        pass


# --------------------------------------------------------------------------
# Test-friendly entry point
# --------------------------------------------------------------------------


async def _evaluating_submissions(conn: Any, limit: int = 10) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM agent_submissions WHERE status='evaluating' "
        "ORDER BY submitted_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def _resolve_polaris_runner_for_mode(mode: str) -> PolarisRunner:
    """Build a runner for an explicit attestation mode.

    Mirrors `_resolve_polaris_runner_from_env` but skips the env lookup —
    the per-submission dispatch already knows which tier this row is.
    """
    import os as _os

    _saved = _os.environ.get("CATHEDRAL_EVAL_MODE")
    _os.environ["CATHEDRAL_EVAL_MODE"] = mode
    try:
        return _resolve_polaris_runner_from_env()
    finally:
        if _saved is None:
            _os.environ.pop("CATHEDRAL_EVAL_MODE", None)
        else:
            _os.environ["CATHEDRAL_EVAL_MODE"] = _saved


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        # Keep runtime dispatch aligned with SAT launch preflight: a typo in
        # the stdout cap should warn and use the default, not brick ssh-probe
        # v2 runner construction after preflight already reported fallback.
        logger.warning("invalid_int_env", env=name, value=raw, default=default)
        return default


def _resolve_polaris_runner_from_env() -> PolarisRunner:
    """Re-build a Polaris runner from the current env so monkeypatched
    `CATHEDRAL_EVAL_MODE` mid-test takes effect on the next tick.

    Mode dispatch (CONTRACTS.md §6 + Tier A Polaris-runtime addendum +
    v2 Polaris-native deploy):

      stub*                -> StubPolarisRunner family (smoke tests)
      stub-fail-polaris    -> FailingStubPolarisRunner
      stub-bad-card        -> MalformedStubPolarisRunner
      bundle               -> BundleCardRunner (BYO-compute path)
      polaris              -> PolarisRuntimeRunner (legacy Tier A —
                              cathedral-runtime shim; kept as backup
                              during v2 migration per POLARIS_NATIVE_V2.md)
      polaris-deploy       -> PolarisDeployRunner (v2 paid — real Hermes
                              via Polaris's native deploy pipeline)
      ssh-probe            -> SshProbeRunner (v2 free — Cathedral SSHs
                              into the miner's box, no Polaris attestation,
                              no verified-runtime multiplier)
      http-polaris (legacy)-> HttpPolarisRunner
      anything else        -> HttpPolarisRunner (legacy default)
    """
    import os

    from cathedral.eval.polaris_deploy_runner import (
        PolarisDeployRunner,
        PolarisDeployRunnerConfig,
    )
    from cathedral.eval.polaris_runner import (
        BundleCardRunner,
        FailingStubPolarisRunner,
        HippiusPresignedUrlResolver,
        HttpPolarisRunner,
        HttpPolarisRunnerConfig,
        MalformedStubPolarisRunner,
        PolarisRunnerError,
        PolarisRuntimeRunner,
        PolarisRuntimeRunnerConfig,
        StubPolarisRunner,
    )

    # Normalize operator env values the same way SAT preflight does so a
    # launch-ready check selects the same runtime path.
    mode = os.environ.get("CATHEDRAL_EVAL_MODE", "stub").strip().lower()
    if mode == "stub-fail-polaris":
        return FailingStubPolarisRunner()
    if mode == "stub-bad-card":
        return MalformedStubPolarisRunner()
    if mode.startswith("stub"):
        return StubPolarisRunner()
    if mode == "bundle":
        return BundleCardRunner()
    if mode == "ssh-probe":
        # Tier B free tier — Cathedral SSHs into the miner's box.
        #
        # v1 (legacy, default): SshProbeRunner. Assumes the miner runs
        # an HTTP server exposing /healthz + /chat. Built on the wrong
        # premise that Hermes is HTTP-shaped (cathedralai/cathedral#75).
        #
        # v2 (CATHEDRAL_PROBER_VERSION=v2): SshHermesRunner. Native
        # `hermes chat -q` invocation over SSH (v1.1.7; previously
        # `hermes -z`). Snapshot-then-eval pattern per docs/HERMES.md
        # § L.1. Returns a TraceBundle with the full Hermes forensic
        # trail (state.db slice, sessions JSON, request dumps,
        # memories, skills, logs) — the data moat.
        #
        # Default is v1 while we smoke-test v2 on the rented Polaris
        # box. Flip via env var when ready to cut over.
        prober_version = os.environ.get("CATHEDRAL_PROBER_VERSION", "v1").strip().lower()

        ssh_key_path = os.environ.get("CATHEDRAL_SSH_KEY_PATH") or os.path.expanduser(
            "~/.ssh/cathedral_probe_ed25519"
        )

        if prober_version == "v2":
            from cathedral.eval.ssh_hermes_runner import (
                DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES,
                SshHermesRunner,
                SshHermesRunnerConfig,
            )

            bundle_dir = (
                os.environ.get("CATHEDRAL_BUNDLE_OUTPUT_DIR") or "/var/lib/cathedral/eval-bundles"
            )
            # ``CATHEDRAL_HERMES_MAX_TURNS`` was removed in v1.1.7 along
            # with the ``eval_max_turns`` config field: ``hermes chat -q``
            # has no equivalent CLI flag, and we want the full agentic
            # loop now rather than a single-turn cap.

            return SshHermesRunner(
                SshHermesRunnerConfig(
                    ssh_private_key_path=ssh_key_path,
                    bundle_output_dir=bundle_dir,
                    connect_timeout_secs=float(
                        os.environ.get("CATHEDRAL_SSH_CONNECT_TIMEOUT", "10")
                    ),
                    eval_timeout_secs=float(os.environ.get("CATHEDRAL_SSH_EVAL_TIMEOUT", "300")),
                    transfer_timeout_secs=float(
                        os.environ.get("CATHEDRAL_SSH_TRANSFER_TIMEOUT", "120")
                    ),
                    pinned_model=os.environ.get("CATHEDRAL_HERMES_PINNED_MODEL"),
                    pinned_provider=os.environ.get("CATHEDRAL_HERMES_PINNED_PROVIDER"),
                    task_family_stdout_limit_bytes=_positive_int_env(
                        "CATHEDRAL_TASK_FAMILY_STDOUT_MAX_BYTES",
                        DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES,
                    ),
                )
            )

        # v1 (default — legacy HTTP-shaped path)
        from cathedral.eval.ssh_probe_runner import (
            SshProbeRunner,
            SshProbeRunnerConfig,
        )

        return SshProbeRunner(
            SshProbeRunnerConfig(
                ssh_private_key_path=ssh_key_path,
                connect_timeout_secs=float(os.environ.get("CATHEDRAL_SSH_CONNECT_TIMEOUT", "10")),
                prompt_timeout_secs=float(os.environ.get("CATHEDRAL_SSH_PROMPT_TIMEOUT", "60")),
                visit_budget_secs=float(os.environ.get("CATHEDRAL_SSH_VISIT_BUDGET", "300")),
            )
        )
    if mode == "polaris-deploy":
        # v2 — Polaris-native Hermes deploy. Skips the cathedral-runtime
        # image entirely; uses the standard marketplace-eval pipeline
        # against `ghcr.io/bigailabs/polaris-hermes`. Requires the
        # publisher app for the Hippius client (presigned URLs).
        from cathedral.publisher.app import latest_ctx

        ctx = latest_ctx()
        if ctx is None:
            raise PolarisRunnerError(
                "CATHEDRAL_EVAL_MODE=polaris-deploy requires the publisher "
                "app to be running so the HippiusClient is available for "
                "presigned bundle URLs"
            )
        attestation_key = os.environ.get("POLARIS_ATTESTATION_PUBLIC_KEY", "").strip()
        if not attestation_key:
            raise PolarisRunnerError(
                "CATHEDRAL_EVAL_MODE=polaris-deploy requires POLARIS_ATTESTATION_PUBLIC_KEY"
            )
        kek_hex = (
            os.environ.get("CATHEDRAL_BUNDLE_KEK")
            or os.environ.get("CATHEDRAL_KEK_HEX")
            or os.environ.get("CATHEDRAL_MASTER_ENCRYPTION_KEY")
            or ""
        )
        if not kek_hex:
            raise PolarisRunnerError(
                "CATHEDRAL_EVAL_MODE=polaris-deploy requires CATHEDRAL_BUNDLE_KEK"
            )
        chutes_pin = (os.environ.get("CATHEDRAL_PIN_CHUTES_KEY") or "").lower() in {
            "1",
            "true",
            "yes",
        }
        return PolarisDeployRunner(
            PolarisDeployRunnerConfig(
                base_url=os.environ.get("POLARIS_BASE_URL", "https://api.polaris.computer"),
                api_token=os.environ.get("POLARIS_API_TOKEN", ""),
                bundle_url_resolver=HippiusPresignedUrlResolver(ctx.hippius),
                attestation_public_key_hex=attestation_key,
                bundle_encryption_key_hex=kek_hex,
                ttl_minutes=int(os.environ.get("POLARIS_DEPLOY_TTL_MINUTES", "30")),
                pin_chutes_key=chutes_pin,
                chutes_api_key=os.environ.get("CHUTES_API_KEY", "") if chutes_pin else "",
            )
        )
    if mode in {"polaris", "polaris-runtime"}:
        # Tier A — Polaris-hosted miners. Polaris fetches the bundle via
        # presigned URL, runs Cathedral's runtime image, signs an
        # attestation over the result.
        from cathedral.publisher.app import latest_ctx

        ctx = latest_ctx()
        if ctx is None:
            raise PolarisRunnerError(
                "CATHEDRAL_EVAL_MODE=polaris requires the publisher app to be "
                "running so the HippiusClient is available for presigned URLs"
            )
        attestation_key = os.environ.get("POLARIS_ATTESTATION_PUBLIC_KEY", "").strip()
        if not attestation_key:
            raise PolarisRunnerError(
                "CATHEDRAL_EVAL_MODE=polaris requires POLARIS_ATTESTATION_PUBLIC_KEY"
            )
        return PolarisRuntimeRunner(
            PolarisRuntimeRunnerConfig(
                base_url=os.environ.get("POLARIS_BASE_URL", "https://api.polaris.computer"),
                api_token=os.environ.get("POLARIS_API_TOKEN", ""),
                submission_id=os.environ.get("POLARIS_CATHEDRAL_RUNTIME_SUBMISSION_ID", ""),
                attestation_public_key_hex=attestation_key,
                bundle_url_resolver=HippiusPresignedUrlResolver(ctx.hippius),
                bundle_encryption_key_hex=(
                    os.environ.get("CATHEDRAL_BUNDLE_KEK")
                    or os.environ.get("CATHEDRAL_KEK_HEX")
                    or os.environ.get("CATHEDRAL_MASTER_ENCRYPTION_KEY")
                    or ""
                ),
            )
        )
    return HttpPolarisRunner(
        HttpPolarisRunnerConfig(
            base_url=os.environ.get("POLARIS_BASE_URL", "https://api.polaris.computer"),
            api_token=os.environ.get("POLARIS_API_TOKEN", ""),
        )
    )


async def _run_once_async() -> int:
    """Process queued / evaluating submissions in two phases per tick so
    the state-machine transitions queued -> evaluating -> ranked|rejected
    are observable across separate `run_once()` calls (per CONTRACTS.md
    §6 status arrows).

    Phase 1 (per call): promote up to N queued submissions to
    'evaluating'. Phase 2 (next call): finish evaluating + rank.

    Returns the number of submissions advanced this tick.
    """
    from cathedral.publisher.app import latest_ctx

    ctx = latest_ctx()
    if ctx is None:
        return 0
    from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as _CHALLENGE_LOCK_SCHEMA
    from cathedral.lanes.challenge_receipts import SQLITE_SCHEMA as _CHALLENGE_RECEIPT_SCHEMA
    from cathedral.lanes.challenge_source import SQLITE_SCHEMA as _CHALLENGE_SOURCE_SCHEMA

    db_write_lock = getattr(ctx, "db_write_lock", asyncio.Lock())
    async with db_write_lock:
        await ctx.db.executescript(_CHALLENGE_SOURCE_SCHEMA)
        await ctx.db.executescript(_CHALLENGE_LOCK_SCHEMA)
        await ctx.db.executescript(_CHALLENGE_RECEIPT_SCHEMA)
        await ctx.db.commit()

    # Per-submission runner dispatch. The submission's `attestation_mode`
    # column (added by the submit-attestation-modes PR) tells us whether
    # a miner opted into Tier A (polaris) or BYO (bundle). The env-level
    # CATHEDRAL_EVAL_MODE remains the override for stub/legacy paths and
    # the fallback whenever attestation_mode is unset or its required
    # env vars aren't configured — that way tests and dev environments
    # that pin a single mode globally keep working without seeding extra
    # config per submission.
    import os as _os

    def runner_for(submission: dict[str, Any]) -> PolarisRunner:
        mode = (submission.get("attestation_mode") or "").strip().lower()
        env_mode = _os.environ.get("CATHEDRAL_EVAL_MODE", "").strip().lower()
        has_polaris_key = bool(_os.environ.get("POLARIS_ATTESTATION_PUBLIC_KEY"))
        if env_mode.startswith("stub"):
            r = _resolve_polaris_runner_from_env()
            logger.info(
                "runner_dispatch",
                submission_id=submission.get("id"),
                attestation_mode=mode,
                env_mode=env_mode,
                chosen=type(r).__name__,
                reason="stub-env-wins",
            )
            return r
        if mode == "polaris-deploy" and has_polaris_key:
            # v2 — opted into the Polaris-native Hermes flow.
            r = _resolve_polaris_runner_for_mode("polaris-deploy")
            logger.info(
                "runner_dispatch",
                submission_id=submission.get("id"),
                attestation_mode=mode,
                env_mode=env_mode,
                chosen=type(r).__name__,
                reason="polaris-deploy-tier-v2",
            )
            return r
        if mode == "polaris" and has_polaris_key:
            r = _resolve_polaris_runner_for_mode("polaris")
            logger.info(
                "runner_dispatch",
                submission_id=submission.get("id"),
                attestation_mode=mode,
                env_mode=env_mode,
                chosen=type(r).__name__,
                reason="polaris-tier",
            )
            return r
        if mode == "tee":
            r = _resolve_polaris_runner_for_mode("bundle")
            logger.info(
                "runner_dispatch",
                submission_id=submission.get("id"),
                attestation_mode=mode,
                env_mode=env_mode,
                chosen=type(r).__name__,
                reason="tee-pre-verified",
            )
            return r
        if mode == "ssh-probe":
            # v2 free tier — Cathedral SSHs into the miner's box. No
            # Polaris attestation chain (manifest will be None on the
            # PolarisRunResult), so the verified-runtime 1.10x multiplier
            # is not applied at scoring.
            r = _resolve_polaris_runner_for_mode("ssh-probe")
            logger.info(
                "runner_dispatch",
                submission_id=submission.get("id"),
                attestation_mode=mode,
                env_mode=env_mode,
                chosen=type(r).__name__,
                reason="ssh-probe-tier-free",
            )
            return r
        r = _resolve_polaris_runner_from_env()
        logger.info(
            "runner_dispatch",
            submission_id=submission.get("id"),
            attestation_mode=mode,
            env_mode=env_mode,
            chosen=type(r).__name__,
            reason="env-fallback",
            polaris_key_present=has_polaris_key,
        )
        return r

    orch = EvalOrchestrator(
        db=ctx.db,
        hippius=ctx.hippius,
        runner_for=runner_for,
        signer=ctx.signer,
        registry=ctx.registry,
        task_family_challenge_source=SqliteChallengeSource(ctx.db),
        task_family_challenge_lock=SqliteChallengeLock(ctx.db),
        task_family_fetch_token_store=SqliteFetchTokenStore(ctx.db),
        task_family_receipt_store=SqliteChallengeReceiptStore(ctx.db),
        db_write_lock=db_write_lock,
        public_base_url=_os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "").strip() or None,
    )

    advanced = 0
    try:
        await orch.reconcile_sat_receipts(log=logger)
    except Exception as e:
        logger.warning("task_family_receipt_reconcile_failed", error=str(e)[:256])

    # Phase 2: finish in-flight evaluating rows from a previous tick.
    in_flight = await _evaluating_submissions(ctx.db, limit=10)
    for s in in_flight:
        try:
            await orch.evaluate_one(s)
            advanced += 1
        except Exception as e:
            logger.exception("eval_run_once_crashed", submission_id=s["id"], error=str(e))
            # Same recovery as run_eval_loop._process: phase-2 rows were
            # all promoted from 'queued' in a prior phase-1 tick. Route
            # through the retry policy so a crashed eval re-queues or
            # rejects rather than sitting in 'evaluating' forever.
            await orch._on_retryable_failure(
                s, logger.bind(submission_id=s["id"]), f"evaluate_one crash: {e}"
            )

    # Phase 1: promote queued -> evaluating (work happens next tick).
    queued = await repository.queued_submissions(ctx.db, limit=10)
    for s in queued:
        async with db_write_lock:
            await repository.update_submission_status(ctx.db, s["id"], status="evaluating")
        advanced += 1

    return advanced


def run_once() -> int:
    """Synchronous wrapper around `_run_once_async` for the test harness."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Inside an async context already — schedule and wait.
            return asyncio.run_coroutine_threadsafe(_run_once_async(), loop).result(timeout=60)
    except RuntimeError:
        pass
    return asyncio.run(_run_once_async())


# Aliases the contract test probes for.
tick = run_once
process_one = run_once
schedule_once = run_once
