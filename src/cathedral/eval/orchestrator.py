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
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import structlog

from cathedral.eval.eval_signer import EvalSigner
from cathedral.eval.runner_types import PolarisRunner
from cathedral.lanes.challenge_lock import ChallengeLock, SqliteChallengeLock
from cathedral.lanes.challenge_receipts import (
    ChallengeReceiptStore,
    SqliteChallengeReceiptStore,
)
from cathedral.lanes.challenge_source import (
    ChallengeRecord,
    ChallengeSource,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)
from cathedral.lanes.contract import (
    HiddenMetadata,
    PublicProblem,
)
from cathedral.lanes.synthetic_boolean_v1 import (
    FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID,
)
from cathedral.publisher import repository
from cathedral.publisher.merkle import epoch_for
from cathedral.storage import (
    HippiusClient,
)
from cathedral.v3.publisher import (
    v3_feed_enabled,
)

logger = structlog.get_logger(__name__)


_RETRY_BACKOFFS = (60, 120, 240)


# Cooldown applied to cadence-refresh rows that hit a failure path that
# does not advance MAX(eval_runs.ran_at). 1 hour is long enough to keep
# a permanently broken bundle from re-picking every tick, short enough
# that a transient outage recovers within the same cadence window.
_CADENCE_FAILURE_COOLDOWN = timedelta(hours=1)


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


class EvalOrchestrator:
    """Orchestrates the eval lifecycle for a single submission."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        hippius: HippiusClient,
        polaris: PolarisRunner | None = None,
        signer: EvalSigner,
        registry: object | None = None,
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
        # SAT (synthetic_boolean_v1) serving lives in its own card-free module.
        # The orchestrator composes one instance and delegates announcement,
        # probe/score/finalize, and receipt reconciliation to it. The SAT
        # substrate (challenge source/lock/fetch-token/receipt stores, the
        # shared db_write_lock, public base URL, hippius, signer) is passed
        # straight through so the two share one transaction gate on ctx.db.
        #
        # Imported lazily here (not module-top) to break a circular import:
        # sat_serving imports cathedral.eval.eval_signer, which runs
        # cathedral/eval/__init__.py, which eager-imports this orchestrator
        # module. A module-top `from cathedral.publisher.sat_serving import
        # SatServing` therefore fails when sat_serving is the first module
        # loaded in a fresh interpreter. Deferring it to construction time
        # (after eval_signer is fully initialized) makes sat_serving
        # independently importable while keeping the live lane unchanged.
        from cathedral.publisher.sat_serving import SatServing

        self._sat = SatServing(
            db=db,
            hippius=hippius,
            signer=signer,
            task_family_challenge_source=task_family_challenge_source,
            task_family_challenge_lock=task_family_challenge_lock,
            task_family_fetch_token_store=task_family_fetch_token_store,
            task_family_receipt_store=task_family_receipt_store,
            db_write_lock=self._db_write_lock,
            public_base_url=self._public_base_url,
        )

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

    # ------------------------------------------------------------------
    # SAT (synthetic_boolean_v1) serving — delegated to SatServing.
    #
    # The implementations live in cathedral.publisher.sat_serving so the
    # live SAT lane carries no dependency on the publisher card core. These
    # thin wrappers preserve the orchestrator's historical method surface
    # for the eval loop and the lane test suite.
    # ------------------------------------------------------------------

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
        return await self._sat.maybe_run_task_family_lanes(
            submission=submission,
            runner=runner,
            epoch=epoch,
            round_index=round_index,
            log=log,
            problem_overrides=problem_overrides,
        )

    async def reconcile_sat_receipts(
        self,
        *,
        log: structlog.stdlib.BoundLogger,
    ) -> int:
        return await self._sat.reconcile_sat_receipts(log=log)

    async def snapshot_task_family_batch_problems(
        self,
        *,
        log: structlog.stdlib.BoundLogger,
    ) -> dict[str, tuple[PublicProblem, HiddenMetadata]]:
        return await self._sat.snapshot_task_family_batch_problems(log=log)

    async def _announce_synthetic_boolean_problem(
        self,
        record: ChallengeRecord,
        *,
        log: structlog.stdlib.BoundLogger,
        family_id: str,
    ) -> tuple[PublicProblem, HiddenMetadata] | None:
        return await self._sat.announce_synthetic_boolean_problem(
            record, log=log, family_id=family_id
        )

    async def _finalize_ready_sat_winner(self, **kwargs: Any) -> bool:
        return await self._sat._finalize_ready_sat_winner(**kwargs)

    async def _finalize_sat_receipt_ordered_result(self, **kwargs: Any) -> None:
        return await self._sat._finalize_sat_receipt_ordered_result(**kwargs)

    async def _publish_resolved_sat_losers(self, **kwargs: Any) -> int:
        return await self._sat._publish_resolved_sat_losers(**kwargs)

    async def _expire_stale_sat_receipts_for_challenge(self, **kwargs: Any) -> int:
        return await self._sat._expire_stale_sat_receipts_for_challenge(**kwargs)

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
    registry: object | None = None,
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

    After the card-core strip the only live runners are the stub family
    (smoke tests) and the SAT prober:

      stub*                -> StubPolarisRunner (smoke tests / default)
      ssh-probe + v2       -> SshHermesRunner (LIVE SAT prober — native
                              `hermes chat -q` over SSH; returns the
                              TraceBundle that is the data moat)

    The legacy card runners (Bundle/Polaris-runtime/Polaris-deploy/HTTP/
    ssh-probe v1) were removed with the card eval branch. Any unrecognized
    mode falls back to the stub runner rather than a card runner.
    """
    import os

    from cathedral.eval.runner_types import StubPolarisRunner

    # Normalize operator env values the same way SAT preflight does so a
    # launch-ready check selects the same runtime path.
    mode = os.environ.get("CATHEDRAL_EVAL_MODE", "stub").strip().lower()
    if mode == "ssh-probe":
        # SAT prober — Cathedral SSHs into the miner's box and runs Hermes.
        #
        # v2 (CATHEDRAL_PROBER_VERSION=v2): SshHermesRunner. Native
        # `hermes chat -q` invocation over SSH (v1.1.7; previously
        # `hermes -z`). Snapshot-then-eval pattern per docs/HERMES.md
        # § L.1. Returns a TraceBundle with the full Hermes forensic
        # trail (state.db slice, sessions JSON, request dumps,
        # memories, skills, logs) — the data moat.
        #
        # The legacy v1 HTTP-shaped SshProbeRunner was removed with the
        # card-core strip; only v2 is supported now.
        ssh_key_path = os.environ.get("CATHEDRAL_SSH_KEY_PATH") or os.path.expanduser(
            "~/.ssh/cathedral_probe_ed25519"
        )

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
                connect_timeout_secs=float(os.environ.get("CATHEDRAL_SSH_CONNECT_TIMEOUT", "10")),
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
    # stub* and any unrecognized mode -> stub runner. The card runners that
    # previously backed bundle/polaris/polaris-deploy/http-polaris are gone.
    return StubPolarisRunner()


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
        if mode == "ssh-probe":
            # SAT prober — Cathedral SSHs into the miner's box and runs
            # Hermes. No Polaris attestation chain (manifest will be None
            # on the PolarisRunResult), so the verified-runtime multiplier
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
