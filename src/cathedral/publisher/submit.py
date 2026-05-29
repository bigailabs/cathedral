"""POST /v1/agents/submit — SAT registration + (PR5) solve-on-submit.

Cathedral SN39 moved off card-shaped Hermes bundles onto the
``synthetic_boolean_v1`` (SAT) task family lane. Per recovery-plan
Decision 1 (Option A), ``card_id`` survives on the wire as a task-family
discriminator with the single accepted value ``synthetic_boolean_v1``;
anything else 400s with a pointer to the live skill manifest.

PR5 (solve-on-submit) adds an optional ``dimacs_solution`` form field
gated by the ``CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED`` env flag. When set,
the publisher synchronously verifies the DIMACS body against the active
challenge's CNF, atomically locks the winner, writes an eval_run, and
returns the winner shape. Async SSH-attest happens later, in
``cathedral.eval.sat_attest_worker``. When the flag is off (the default),
the field is silently ignored — the legacy registration-only flow is
unchanged.

Flow:

    multipart upload  (card_id="synthetic_boolean_v1", display_name,
                       attestation_mode="ssh-probe", ssh_host, ssh_user,
                       [ssh_port], [bio], [bundle - optional, ignored],
                       [challenge_id, dimacs_solution - PR5])
       │
       ▼
    auth header dep (X-Cathedral-Hotkey, X-Cathedral-Signature)
       │
       ▼
    sr25519 signature verification (4-field or 6-field shape)
       │   - signature mismatch    -> 401
       │   - card_id != "synthetic_boolean_v1"  -> 400
       │   - attestation_mode != "ssh-probe"    -> 400
       ▼
    Preflight solve-POST challenge id (before assigning SAT seq_no)
       │   - challenge_not_active  -> 409
       │
       ▼
    UPSERT agent_submissions  (status='pending_solution' with PR5 flag on;
                                status='pending_check' on legacy flag-off path)
       │
       ▼  (only when PR5 flag is on AND dimacs_solution present)
    Verify DIMACS against active challenge
       │   - malformed/unsatisfied -> 400 + losing eval_run
       ▼
    BEGIN IMMEDIATE; atomic CAS on lane_challenge_winners
       │   - already locked       -> 409 + losing eval_run
       ▼
    signed eval_run (attestation_status='pending'),
    lane_challenge_winners row, agent_submissions.status='ranked'
       │
       ▼
    200 OK { id, eval_run_id, status: 'ranked', ... }
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import quote
from uuid import uuid4

import blake3
import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from cathedral.auth import InvalidSignatureError, verify_hotkey_signature
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import (
    TASK_FAMILY_SCHEMA_VERSION,
    TASK_FAMILY_SCHEMA_VERSION_V6,
    build_signed_task_family_row,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID as SAT_FAMILY_ID
from cathedral.lanes.synthetic_boolean_v1 import SCHEMA_VERSION as SAT_SCHEMA_VERSION
from cathedral.lanes.synthetic_boolean_v1.verify_submission import (
    SubmissionVerification,
)
from cathedral.lanes.synthetic_boolean_v1.verify_submission import (
    sha256_hex as _solution_sha256_hex,
)
from cathedral.lanes.synthetic_boolean_v1.verify_submission import (
    verify_submission as _verify_dimacs_submission,
)
from cathedral.publisher.auth_signature import HotkeyAuth, hotkey_auth_header
from cathedral.publisher.merkle import epoch_for
from cathedral.publisher.rate_limit import RateLimitError, SubmitRateGuard

if TYPE_CHECKING:
    from cathedral.lanes.challenge_source import ChallengeRecord, SqliteChallengeSource
    from cathedral.publisher.app import PublisherContext

logger = structlog.get_logger(__name__)


# SAT registration model — Decision 1, Option A.
#
# ``card_id`` survives on the wire as the task-family discriminator. Only
# ``synthetic_boolean_v1`` is accepted; ``eu-ai-act`` / ``us-ai-eo`` /
# ``uk-ai-whitepaper`` / ``singapore-pdpc`` / ``japan-meti-mic`` get a
# 400 + migration URL so existing card-era miners learn where to point.
SAT_CARD_ID = "synthetic_boolean_v1"

# ``attestation_mode`` is locked to BYO-Box / ssh-probe. ``tee`` and
# ``unverified`` were card-era only and are removed in PR2; the runners
# behind them stay in tree for now (still imported by the orchestrator)
# but the submit boundary refuses them with a clear migration message.
SSH_PROBE_MODE = "ssh-probe"

SKILL_MD_URL = "https://cathedral.computer/skill.md"

_MIGRATION_HINT = f"See {SKILL_MD_URL} for the SAT registration shape."

_MAX_BUNDLE_BYTES = 10 * 1024 * 1024  # Ignored bundles are still capped to
# keep the multipart cheap; we do NOT inspect bundle contents anymore.
_MAX_DISPLAY_NAME = 64
_MAX_BIO = 280

# PR5 (solve-on-submit) feature flag. Off by default so PR5 can be
# deployed with no behavior change; flip to "true" / "1" / "yes" once
# the publisher has been observed running cleanly with the flag off.
PR5_SOLVE_ON_SUBMIT_ENV = "CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED"

# Cap solve-POST DIMACS bodies; the dimacs verifier already enforces an
# upper bound but we 413 cheaply at the form layer for oversized inputs.
_MAX_DIMACS_BYTES = 256 * 1024 * 1024


def _pr5_solve_on_submit_enabled() -> bool:
    """True iff the PR5 solve-on-submit flag is on.

    Read on every request so an operator can flip it without a restart
    in test environments. Production deploys ship with the env var
    fixed at process start.
    """
    raw = os.environ.get(PR5_SOLVE_ON_SUBMIT_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


OPEN_WINDOW_ENV = "CATHEDRAL_OPEN_WINDOW_ENABLED"


def _open_window_enabled() -> bool:
    """True iff open-window (PAR-2) scoring is on.

    When set, a valid solve is recorded as a ranked open-window solve (every
    solver scored, no single-winner lock) emitting a schema-6 PAR-2 fact row,
    instead of the legacy first-valid winner lock. Off by default so the
    single-winner path and its contract tests are unchanged until the cutover.
    """
    raw = os.environ.get(OPEN_WINDOW_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SubmissionResponse:
    id: str
    bundle_hash: str
    status: str
    submitted_at: str


router = APIRouter()


@router.post("/v1/agents/submit", status_code=status.HTTP_202_ACCEPTED)
async def submit_agent(
    request: Request,
    # PR2: ``card_id`` is now the SAT task-family marker, not a card.
    card_id: str = Form(...),
    display_name: str = Form(...),
    # Required SAT attestation: BYO-Box ssh-probe. Hard-default and only
    # accepted value; ``tee`` / ``unverified`` 400 with a migration URL.
    attestation_mode: str = Form(default=SSH_PROBE_MODE),
    # SSH coordinates for the BYO Box probe.
    ssh_host: str | None = Form(default=None),
    ssh_port: int | None = Form(default=None),
    ssh_user: str | None = Form(default=None),
    bio: str | None = Form(default=None),
    # CRIT-1: server clock is authoritative. We accept ``submitted_at`` to
    # verify the miner's signature against their declared value (within the
    # ±5 minute skew window), but the persisted timestamp is server-side.
    submitted_at_form: str | None = Form(default=None, alias="submitted_at"),
    # ``bundle`` is OPTIONAL post-PR2. SAT miners don't ship a card bundle;
    # the field is accepted for back-compat with v1.0.x miner clients but
    # its contents are NEVER processed as a card. We still hash whatever is
    # uploaded so the signed claim's ``bundle_hash`` field has a value to
    # verify against (the miner signs zero-hash when no bundle is sent).
    bundle: UploadFile | None = File(default=None),
    # PR5 (solve-on-submit) form fields. Both are silently ignored when
    # CATHEDRAL_PR5_SOLVE_ON_SUBMIT_ENABLED is unset/false, so the wire
    # contract stays backwards-compatible.
    challenge_id: str | None = Form(default=None),
    dimacs_solution: str | None = Form(default=None),
    auth: HotkeyAuth = Depends(hotkey_auth_header),
) -> dict[str, object]:
    ctx: PublisherContext = request.app.state.ctx

    if ctx.submissions_paused:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="submissions paused",
        )

    # ----- rate limit + signature-replay dedup (WS2) -------------------------
    # Per-hotkey min-interval blunts the tight-poll/proximity advantage; the
    # replay guard closes the ±300s skew-window replay hole. Single-instance
    # publisher, so an app.state-scoped in-memory guard is sufficient.
    guard = getattr(request.app.state, "submit_guard", None)
    if guard is None:
        guard = SubmitRateGuard()
        request.app.state.submit_guard = guard
    try:
        guard.check(
            hotkey=auth.hotkey_ss58,
            signature=request.headers.get("X-Cathedral-Signature", ""),
            now=datetime.now(UTC).timestamp(),
        )
    except RateLimitError as exc:
        logger.info(
            "submit_rate_limited",
            hotkey=auth.hotkey_ss58,
            replay=exc.replay,
        )
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    # ----- card_id gate (Decision 1, Option A) -------------------------------
    # ``card_id`` is no longer a card; it's the task-family lane marker.
    # Only ``synthetic_boolean_v1`` is accepted. Anything else 400s and
    # points the caller at the live skill manifest.
    if card_id != SAT_CARD_ID:
        logger.info(
            "submit_rejected_card_id",
            hotkey=auth.hotkey_ss58,
            card_id=card_id,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"card_id must be {SAT_CARD_ID!r}; the card-era lanes were "
                f"removed in PR2. {_MIGRATION_HINT}"
            ),
        )

    # ----- attestation_mode gate --------------------------------------------
    if attestation_mode != SSH_PROBE_MODE:
        logger.info(
            "submit_rejected_attestation_mode",
            hotkey=auth.hotkey_ss58,
            attestation_mode=attestation_mode,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"attestation_mode must be {SSH_PROBE_MODE!r}; "
                f"tee / unverified / polaris* lanes were removed in PR2. "
                f"{_MIGRATION_HINT}"
            ),
        )

    # ----- ssh-probe coordinates --------------------------------------------
    if not ssh_host or not ssh_user:
        raise HTTPException(
            status_code=400,
            detail=(
                f"attestation_mode={SSH_PROBE_MODE!r} requires ssh_host and "
                f"ssh_user (ssh_port defaults to 22). {_MIGRATION_HINT}"
            ),
        )
    if ssh_port is None:
        ssh_port = 22
    if not (1 <= ssh_port <= 65535):
        raise HTTPException(status_code=400, detail=f"ssh_port out of range: {ssh_port}")
    if len(ssh_host) > 253 or len(ssh_user) > 32:
        raise HTTPException(status_code=400, detail="ssh_host / ssh_user too long")

    # ----- display_name / bio validation ------------------------------------
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name required")
    if len(display_name) > _MAX_DISPLAY_NAME:
        raise HTTPException(
            status_code=400,
            detail=f"display_name exceeds {_MAX_DISPLAY_NAME} chars",
        )
    if bio is not None and len(bio) > _MAX_BIO:
        raise HTTPException(status_code=400, detail=f"bio exceeds {_MAX_BIO} chars")

    # ----- bundle (optional, ignored) ---------------------------------------
    # The signed claim covers ``bundle_hash``. SAT miners with no bundle to
    # ship sign the blake3 of the empty string; legacy v1 miner clients that
    # still attach their card bundle have its hash verified for signature
    # purposes only — we do NOT decrypt, validate, or store the bytes.
    if bundle is not None and bundle.filename:
        raw = await _read_capped(bundle, _MAX_BUNDLE_BYTES + 1)
        if len(raw) > _MAX_BUNDLE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"bundle exceeds {_MAX_BUNDLE_BYTES // (1024 * 1024)} MiB limit",
            )
        bundle_hash = blake3.blake3(raw).hexdigest()
        bundle_size_bytes = len(raw)
        logger.info(
            "submit_bundle_ignored",
            hotkey=auth.hotkey_ss58,
            size=bundle_size_bytes,
            note="card-era bundle accepted for signature verification only",
        )
    else:
        bundle_hash = blake3.blake3(b"").hexdigest()
        bundle_size_bytes = 0

    # ----- timestamp + signature verify -------------------------------------
    server_submitted_at = datetime.now(UTC)
    server_submitted_at_iso = _ms_iso(server_submitted_at)

    if submitted_at_form:
        try:
            client_submitted_at = datetime.fromisoformat(submitted_at_form.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail="submitted_at must be ISO-8601") from e
        if client_submitted_at.tzinfo is None:
            client_submitted_at = client_submitted_at.replace(tzinfo=UTC)
        skew_secs = abs((server_submitted_at - client_submitted_at).total_seconds())
        if skew_secs > 300:
            logger.info(
                "submission_clock_skew",
                hotkey=auth.hotkey_ss58,
                skew_secs=skew_secs,
            )
            raise HTTPException(
                status_code=400,
                detail="submitted_at outside acceptable clock-skew window",
            )
        signed_submitted_at_iso = submitted_at_form
    else:
        signed_submitted_at_iso = server_submitted_at_iso

    # PR5: decide whether this is a solve-POST. If the flag is off,
    # both new fields are ignored entirely — even reading them off the
    # form is fine because we treat them as None for downstream logic.
    pr5_enabled = _pr5_solve_on_submit_enabled()
    is_solve_post = bool(
        pr5_enabled
        and dimacs_solution is not None
        and dimacs_solution.strip()
    )

    # When the flag is on, we ALWAYS expect the 6-field signed shape so
    # registration-only POSTs (under the new flag) still bind the
    # canonical payload to the empty-string challenge_id / solution hash.
    # The miner's signing helper must use the same convention. With the
    # flag off, we fall back to the legacy 4-field shape.
    if is_solve_post:
        # Bind the signature to the exact DIMACS body the miner sent.
        solution_sha256 = _solution_sha256_hex(dimacs_solution or "")
        sig_challenge_id = (challenge_id or "").strip()
        sig_solution_sha256 = solution_sha256
    elif pr5_enabled:
        # Registration-only under PR5: bind to empty strings so the
        # canonical shape stays predictable.
        solution_sha256 = ""
        sig_challenge_id = ""
        sig_solution_sha256 = ""
    else:
        # Legacy 4-field shape (flag off): pass None to keep the old
        # canonical bytes.
        solution_sha256 = ""
        sig_challenge_id = None  # type: ignore[assignment]
        sig_solution_sha256 = None  # type: ignore[assignment]

    try:
        verify_hotkey_signature(
            hotkey_ss58=auth.hotkey_ss58,
            signature_b64=auth.signature_b64,
            bundle_hash=bundle_hash,
            card_id=card_id,
            submitted_at=signed_submitted_at_iso,
            challenge_id=sig_challenge_id,
            dimacs_solution_sha256=sig_solution_sha256,
        )
    except InvalidSignatureError as e:
        # Backwards-compat fallback: if the flag is on but the miner
        # signed the legacy 4-field shape (registration-only), accept
        # it. Solve-POSTs MUST sign the 6-field shape — never accept a
        # solve-POST under the legacy shape (would let an attacker
        # replay a registration signature against an arbitrary
        # solution). This preserves the legacy registration flow while
        # the network rolls forward to the new client.
        if pr5_enabled and not is_solve_post:
            try:
                verify_hotkey_signature(
                    hotkey_ss58=auth.hotkey_ss58,
                    signature_b64=auth.signature_b64,
                    bundle_hash=bundle_hash,
                    card_id=card_id,
                    submitted_at=signed_submitted_at_iso,
                    challenge_id=None,
                    dimacs_solution_sha256=None,
                )
            except InvalidSignatureError as legacy_err:
                logger.info("submission_sig_failed", hotkey=auth.hotkey_ss58)
                raise HTTPException(
                    status_code=401, detail="invalid hotkey signature"
                ) from legacy_err
        else:
            logger.info("submission_sig_failed", hotkey=auth.hotkey_ss58)
            raise HTTPException(status_code=401, detail="invalid hotkey signature") from e

    submitted_at_iso = server_submitted_at_iso
    solve_challenge_id = (challenge_id or "").strip()
    solve_challenge_source: SqliteChallengeSource | None = None
    solve_challenge: ChallengeRecord | None = None
    if is_solve_post:
        if len(dimacs_solution or "") > _MAX_DIMACS_BYTES:
            raise HTTPException(
                status_code=413,
                detail="dimacs_solution exceeds size limit",
            )
        solve_challenge_source, solve_challenge = await _preflight_solve_post_challenge(
            ctx=ctx,
            challenge_id=solve_challenge_id,
        )

    # ----- UPSERT agent_submissions -----------------------------------------
    # PR5: a re-submission from the same hotkey + bundle_hash is now an
    # UPDATE of the SSH coordinates rather than a 409. This is a UX fix
    # for solve-POSTs: miners iterating on their solver setup expect to
    # re-register without 409. Solve-POSTs preflight the requested
    # challenge before this point so bogus challenge ids never allocate
    # challenge-local seq_no rows.
    from cathedral.publisher import repository

    async with ctx.db_write_lock:
        submission_id = await repository.upsert_sat_registration(
            ctx.db,
            miner_hotkey=auth.hotkey_ss58,
            card_id=card_id,
            bundle_hash=bundle_hash,
            bundle_size_bytes=bundle_size_bytes,
            bundle_signature=auth.signature_b64,
            display_name=display_name,
            bio=bio,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            submitted_at_iso=submitted_at_iso,
            challenge_id=solve_challenge_id if is_solve_post else None,
            initial_status="pending_solution" if pr5_enabled else "pending_check",
        )
        await ctx.db.commit()

    logger.info(
        "sat_submission_accepted",
        submission_id=submission_id,
        hotkey=auth.hotkey_ss58,
        card_id=card_id,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
        is_solve_post=is_solve_post,
    )

    # ----- (PR5) optional solve-POST branch ---------------------------------
    if is_solve_post:
        result_body = await _handle_solve_post(
            ctx=ctx,
            submission_id=submission_id,
            miner_hotkey=auth.hotkey_ss58,
            agent_display_name=display_name,
            challenge_id=solve_challenge_id,
            dimacs_solution=dimacs_solution or "",
            dimacs_solution_sha256=solution_sha256,
            submitted_at_iso=submitted_at_iso,
            challenge_source=solve_challenge_source,
            active_challenge=solve_challenge,
        )
        # The solve path returns HTTP 200, not the 202 registration default.
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=result_body,
        )

    return {
        "id": submission_id,
        "status": "pending_solution" if pr5_enabled else "pending_check",
        "submitted_at": submitted_at_iso,
    }


# --------------------------------------------------------------------------
# Solve-on-submit branch
# --------------------------------------------------------------------------


async def _select_active_sat_challenge(
    source,
    *,
    challenge_id: str | None = None,
    tier: int | None = None,
):
    """Return one active SAT challenge selected by id, tier, or default order."""
    selected_challenge_id = (challenge_id or "").strip()
    if selected_challenge_id and tier is not None:
        raise HTTPException(
            status_code=400,
            detail="use either challenge_id or tier, not both",
        )
    if tier is not None:
        if tier < 0:
            raise HTTPException(status_code=400, detail="tier must be >= 0")
        return await source.get_active_for_tier(SAT_FAMILY_ID, int(tier))
    if selected_challenge_id:
        for rec in await source.list_active(SAT_FAMILY_ID):
            if rec.challenge_id == selected_challenge_id:
                return rec
        return None
    return await source.get_active(SAT_FAMILY_ID)


async def _preflight_solve_post_challenge(
    *,
    ctx: PublisherContext,
    challenge_id: str,
) -> tuple[SqliteChallengeSource, ChallengeRecord]:
    """Validate the solve target before allocating a submission sequence.

    The SAT registration table is challenge-aware. If we assign
    ``sat_challenge_id``/``seq_no`` before checking the challenge exists and is
    active, callers can persist one pending row per bogus id. Races after this
    point are still handled in ``_handle_solve_post`` and the atomic winner
    claim path.
    """

    if not challenge_id:
        raise HTTPException(
            status_code=400,
            detail="challenge_id required when dimacs_solution is present",
        )

    source: SqliteChallengeSource | None = getattr(
        ctx, "task_family_challenge_source", None
    )
    if source is None:
        source = _resolve_challenge_source(ctx)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail="solve-on-submit not configured: challenge source missing",
        )

    active_rows = await source.list_active(SAT_FAMILY_ID)
    active = next((rec for rec in active_rows if rec.challenge_id == challenge_id), None)
    if active is not None:
        return source, active

    requested_lookup = await source.get_for_endpoint(challenge_id)
    if requested_lookup is not None and requested_lookup.status == "locked":
        from cathedral.publisher import repository

        existing = await repository._existing_winner(ctx.db, SAT_FAMILY_ID, challenge_id)
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "challenge_already_locked",
                "challenge_id": challenge_id,
                "winner_won_at": existing[1] if existing else None,
            },
        )
    if not active_rows:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "no_active_challenge",
                "requested": challenge_id,
            },
        )
    raise HTTPException(
        status_code=409,
        detail={
            "detail": "challenge_not_active",
            "active_challenge_id": active_rows[0].challenge_id,
            "active_challenge_ids": [rec.challenge_id for rec in active_rows],
            "requested": challenge_id,
        },
    )


async def _handle_solve_post(
    *,
    ctx: PublisherContext,
    submission_id: str,
    miner_hotkey: str,
    agent_display_name: str,
    challenge_id: str,
    dimacs_solution: str,
    dimacs_solution_sha256: str,
    submitted_at_iso: str,
    challenge_source: SqliteChallengeSource | None = None,
    active_challenge: ChallengeRecord | None = None,
) -> dict[str, object]:
    """Verify, lock, and persist a solve-POST.

    Separated from the main handler so the flow is readable and easier
    to unit-test. Mirrors the spec's "Server-side logic" section.

    Returns the JSON body the client receives; raises HTTPException with
    the spec's documented status codes for every error path.
    """
    from cathedral.publisher import repository

    if not challenge_id:
        raise HTTPException(
            status_code=400,
            detail="challenge_id required when dimacs_solution is present",
        )

    # The challenge source is wired onto app.state by the lifespan. In
    # tests with no lifespan the attribute may be missing; treat as 503.
    source: SqliteChallengeSource | None = challenge_source or getattr(
        ctx, "task_family_challenge_source", None
    )
    if source is None:
        # Pull from app state via the publisher's stash.
        source = _resolve_challenge_source(ctx)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail="solve-on-submit not configured: challenge source missing",
        )

    active = active_challenge
    active_rows = [] if active is not None else await source.list_active(SAT_FAMILY_ID)
    if active is None:
        active = next((rec for rec in active_rows if rec.challenge_id == challenge_id), None)
    if active is None:
        # Either no SAT challenge is active, OR the requested challenge
        # is no longer one of the active SAT tier slots. Differentiate
        # the cases for clearer client UX, and detect the normal
        # post-winner "already locked" state by looking up the row
        # directly.
        requested_lookup = await source.get_for_endpoint(challenge_id)
        if requested_lookup is not None and requested_lookup.status == "locked":
            # The challenge the miner is solving was just locked by
            # somebody else. Return the spec's "already_locked" shape
            # so the client gets a useful 409.
            existing = await repository._existing_winner(
                ctx.db, SAT_FAMILY_ID, challenge_id
            )
            # Write a losing eval_run for transparency.
            now_iso = _ms_iso(datetime.now(UTC))
            epoch = epoch_for(datetime.now(UTC))
            locked_cnf_sha = requested_lookup.cnf_sha256 or ""
            locked_num_vars = 0
            locked_num_clauses = 0
            locked_time_limit_seconds = _challenge_time_limit_seconds(requested_lookup)
            signed_loser = _build_direct_solve_signed_row(
                ctx=ctx,
                eval_run_id=str(uuid4()),
                submission_id=submission_id,
                agent_display_name=agent_display_name,
                miner_hotkey=miner_hotkey,
                challenge_id=challenge_id,
                tier=0,
                time_limit_seconds=locked_time_limit_seconds,
                cnf_sha256=locked_cnf_sha,
                num_vars=locked_num_vars,
                num_clauses=locked_num_clauses,
                dimacs_solution=dimacs_solution,
                verification=None,
                weighted_score=0.0,
                rejection_reason="challenge_already_locked",
                ran_at_iso=now_iso,
                epoch=epoch,
            )
            async with ctx.db_write_lock:
                await repository.insert_losing_eval_run(
                    ctx.db,
                    submission_id=submission_id,
                    challenge_id=challenge_id,
                    cnf_sha256=locked_cnf_sha,
                    dimacs_solution_sha256=dimacs_solution_sha256,
                    error_code="challenge_already_locked",
                    ran_at_iso=now_iso,
                    signed_row=signed_loser,
                    epoch=epoch,
                    round_index=0,
                    time_limit_seconds=locked_time_limit_seconds,
                    miner_hotkey=miner_hotkey,
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "challenge_already_locked",
                    "challenge_id": challenge_id,
                    "winner_won_at": existing[1] if existing else None,
                },
            )
        if not active_rows:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": "no_active_challenge",
                    "requested": challenge_id,
                },
            )
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "challenge_not_active",
                "active_challenge_id": active_rows[0].challenge_id,
                "active_challenge_ids": [rec.challenge_id for rec in active_rows],
                "requested": challenge_id,
            },
        )

    # Load the CNF body. SqliteChallengeSource.get_active() returns the
    # cnf_text inline for text-backed rows; for file-backed rows we read
    # the path on disk. Keep this synchronous-light: the CNF can be many
    # megabytes for tier-3.
    cnf_text = active.cnf_text
    if not cnf_text and active.cnf_path:
        try:
            import asyncio
            from pathlib import Path

            cnf_text = await asyncio.to_thread(
                Path(active.cnf_path).read_text,
                encoding="utf-8",
            )
        except OSError as e:
            logger.error(
                "solve_post_cnf_unreadable",
                challenge_id=challenge_id,
                path=active.cnf_path,
                error=str(e),
            )
            raise HTTPException(
                status_code=503,
                detail="active challenge CNF unreadable",
            ) from e
    if not cnf_text:
        raise HTTPException(
            status_code=503,
            detail="active challenge has no CNF body",
        )

    cnf_sha256 = (active.audit_metadata or {}).get("cnf_sha256") or ""
    time_limit_seconds = _challenge_time_limit_seconds(active)
    epoch = epoch_for(datetime.now(UTC))

    # ----- Verify DIMACS ----------------------------------------------------
    verification = _verify_dimacs_submission(
        cnf_text=cnf_text,
        dimacs_solution=dimacs_solution,
    )
    now_iso = _ms_iso(datetime.now(UTC))

    if not verification.ok:
        signed_loser = _build_direct_solve_signed_row(
            ctx=ctx,
            eval_run_id=str(uuid4()),
            submission_id=submission_id,
            agent_display_name=agent_display_name,
            miner_hotkey=miner_hotkey,
            challenge_id=challenge_id,
            tier=active.tier,
            time_limit_seconds=time_limit_seconds,
            cnf_sha256=cnf_sha256,
            num_vars=verification.num_vars,
            num_clauses=verification.num_clauses,
            dimacs_solution=dimacs_solution,
            verification=verification,
            weighted_score=0.0,
            rejection_reason=verification.error_code or "malformed_answer",
            ran_at_iso=now_iso,
            epoch=epoch,
        )
        # Losing eval_run for transparency, 400 with the spec's error
        # code. Write under the db_write_lock so this doesn't interleave
        # with other writers on the shared connection.
        async with ctx.db_write_lock:
            eval_run_id = await repository.insert_losing_eval_run(
                ctx.db,
                submission_id=submission_id,
                challenge_id=challenge_id,
                cnf_sha256=cnf_sha256,
                dimacs_solution_sha256=dimacs_solution_sha256,
                error_code=verification.error_code or "malformed_answer",
                ran_at_iso=now_iso,
                signed_row=signed_loser,
                epoch=epoch,
                round_index=0,
                time_limit_seconds=time_limit_seconds,
                miner_hotkey=miner_hotkey,
            )
        logger.info(
            "solve_post_invalid_dimacs",
            submission_id=submission_id,
            hotkey=miner_hotkey,
            challenge_id=challenge_id,
            error_code=verification.error_code,
            eval_run_id=eval_run_id,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "detail": verification.error_code or "malformed_answer",
                "challenge_id": challenge_id,
            },
        )

    # ----- Open-window (PAR-2) ranked scoring (WS-SCORE.C) -----------------
    # When enabled, every valid solver is recorded with a rank and a schema-6
    # PAR-2 fact row; the challenge stays active for the rest of the window.
    # No single-winner lock. Default off — falls through to the legacy path.
    if _open_window_enabled():
        challenge_value = float(getattr(active, "score_multiplier", 1.0) or 1.0)
        open_eval_run_id = str(uuid4())
        async with ctx.db_write_lock:
            ledger = await repository.record_ranked_solve(
                ctx.db,
                family_id=SAT_FAMILY_ID,
                challenge_id=challenge_id,
                miner_hotkey=miner_hotkey,
                eval_run_id=open_eval_run_id,
                weighted_score=1.0,
                solved_at_iso=now_iso,
            )
            if ledger.newly_recorded:
                signed_ranked = _build_direct_solve_signed_row(
                    ctx=ctx,
                    eval_run_id=open_eval_run_id,
                    submission_id=submission_id,
                    agent_display_name=agent_display_name,
                    miner_hotkey=miner_hotkey,
                    challenge_id=challenge_id,
                    tier=active.tier,
                    time_limit_seconds=time_limit_seconds,
                    cnf_sha256=cnf_sha256,
                    num_vars=verification.num_vars,
                    num_clauses=verification.num_clauses,
                    dimacs_solution=dimacs_solution,
                    verification=verification,
                    weighted_score=1.0,
                    rejection_reason=None,
                    ran_at_iso=now_iso,
                    epoch=epoch,
                    schema_version=TASK_FAMILY_SCHEMA_VERSION_V6,
                    challenge_value=challenge_value,
                    solve_rank=ledger.solve_rank,
                    solved=True,
                    operator=miner_hotkey,
                )
                await repository.insert_ranked_eval_run(
                    ctx.db,
                    family_id=SAT_FAMILY_ID,
                    challenge_id=challenge_id,
                    miner_hotkey=miner_hotkey,
                    submission_id=submission_id,
                    cnf_sha256=cnf_sha256,
                    dimacs_solution_sha256=dimacs_solution_sha256,
                    ran_at_iso=now_iso,
                    signed_row=signed_ranked,
                    epoch=epoch,
                    round_index=0,
                    time_limit_seconds=time_limit_seconds,
                    dimacs_solution=dimacs_solution,
                )
        logger.info(
            "solve_post_ranked",
            submission_id=submission_id,
            hotkey=miner_hotkey,
            challenge_id=challenge_id,
            solve_rank=ledger.solve_rank,
            newly_recorded=ledger.newly_recorded,
        )
        return {
            "id": submission_id,
            "eval_run_id": open_eval_run_id,
            "status": "ranked",
            "attestation_status": "pending",
            "weighted_score": 1.0,
            "solve_rank": ledger.solve_rank,
            "challenge_id": challenge_id,
            "server_ran_at": now_iso,
        }

    # ----- Atomic lock + insert (legacy single-winner) ---------------------
    signed_winner = _build_direct_solve_signed_row(
        ctx=ctx,
        eval_run_id=str(uuid4()),
        submission_id=submission_id,
        agent_display_name=agent_display_name,
        miner_hotkey=miner_hotkey,
        challenge_id=challenge_id,
        tier=active.tier,
        time_limit_seconds=time_limit_seconds,
        cnf_sha256=cnf_sha256,
        num_vars=verification.num_vars,
        num_clauses=verification.num_clauses,
        dimacs_solution=dimacs_solution,
        verification=verification,
        weighted_score=1.0,
        rejection_reason=None,
        ran_at_iso=now_iso,
        epoch=epoch,
    )
    async with ctx.db_write_lock:
        # Issue #242: pass the raw DIMACS body so atomic_claim_winner
        # writes the audit sidecar in the SAME transactional scope as
        # the eval_runs INSERT. Either both rows land or neither does
        # — critical so a later auditor never sees a verdict without
        # the body that produced it. body_sha256 mirrors
        # miner_solution_sha256 (computed at submit.py:322 as part of
        # the signed 6-field shape).
        result = await repository.atomic_claim_winner(
            ctx.db,
            family_id=SAT_FAMILY_ID,
            challenge_id=challenge_id,
            miner_hotkey=miner_hotkey,
            submission_id=submission_id,
            cnf_sha256=cnf_sha256,
            dimacs_solution_sha256=dimacs_solution_sha256,
            ran_at_iso=now_iso,
            signed_row=signed_winner,
            epoch=epoch,
            round_index=0,
            time_limit_seconds=time_limit_seconds,
            dimacs_solution=dimacs_solution,
        )

    if not result.won:
        # Someone else won the round. Write a losing eval_run for
        # leaderboard transparency, return 409 per spec.
        signed_loser = _build_direct_solve_signed_row(
            ctx=ctx,
            eval_run_id=str(uuid4()),
            submission_id=submission_id,
            agent_display_name=agent_display_name,
            miner_hotkey=miner_hotkey,
            challenge_id=challenge_id,
            tier=active.tier,
            time_limit_seconds=time_limit_seconds,
            cnf_sha256=cnf_sha256,
            num_vars=verification.num_vars,
            num_clauses=verification.num_clauses,
            dimacs_solution=dimacs_solution,
            verification=verification,
            weighted_score=0.0,
            rejection_reason="challenge_already_locked",
            ran_at_iso=now_iso,
            epoch=epoch,
        )
        async with ctx.db_write_lock:
            await repository.insert_losing_eval_run(
                ctx.db,
                submission_id=submission_id,
                challenge_id=challenge_id,
                cnf_sha256=cnf_sha256,
                dimacs_solution_sha256=dimacs_solution_sha256,
                error_code="challenge_already_locked",
                ran_at_iso=now_iso,
                signed_row=signed_loser,
                epoch=epoch,
                round_index=0,
                time_limit_seconds=time_limit_seconds,
                miner_hotkey=miner_hotkey,
            )
        logger.info(
            "solve_post_lost_race",
            submission_id=submission_id,
            hotkey=miner_hotkey,
            challenge_id=challenge_id,
            winner_hotkey=result.existing_winner_hotkey,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "detail": "challenge_already_locked",
                "challenge_id": challenge_id,
                "winner_won_at": result.existing_winner_won_at_iso,
            },
        )

    logger.info(
        "solve_post_winner",
        submission_id=submission_id,
        hotkey=miner_hotkey,
        challenge_id=challenge_id,
        eval_run_id=result.eval_run_id,
    )

    # Auto-promote the next pending challenge so the lane stays warm
    # without operator intervention. Best-effort: a promote failure does
    # NOT roll back the just-recorded win, since the winner is already
    # durable via atomic_claim_winner. This mirrors the SSH/eval path's
    # call site after PR #232.
    #
    # Issue #241: when the locked row carries a ``difficulty_label`` the
    # promotion must stay scoped to the same ``(tier, difficulty_label)``
    # pair so winning the easy 3b challenge does not retire the harder
    # 6b that is still live. ``active_scope='tier_difficulty'``
    # degrades to ``'tier'`` for unlabeled rows inside
    # ``mark_locked_and_promote_next``, so legacy data continues to
    # behave exactly as before.
    from typing import Literal, cast

    promote_scope: Literal["tier", "tier_difficulty"] = (
        "tier_difficulty" if active.difficulty_label is not None else "tier"
    )
    try:
        promoted = await source.mark_locked_and_promote_next(
            family_id=SAT_FAMILY_ID,
            challenge_id=challenge_id,
            now_iso=now_iso,
            manage_transaction=True,
            active_scope=cast("Literal['family', 'tier', 'tier_difficulty']", promote_scope),
        )
        if promoted is not None:
            logger.info(
                "solve_post_promoted_next",
                locked_challenge_id=challenge_id,
                promoted_challenge_id=promoted.challenge_id,
                tier=promoted.tier,
            )
    except Exception as exc:
        logger.exception(
            "solve_post_promote_failed",
            locked_challenge_id=challenge_id,
            error=str(exc),
        )

    return {
        "id": submission_id,
        "eval_run_id": result.eval_run_id,
        "status": "ranked",
        "attestation_status": "pending",
        "weighted_score": 1.0,
        "challenge_id": challenge_id,
        "server_ran_at": now_iso,
    }


def _challenge_time_limit_seconds(record: object) -> int:
    audit = getattr(record, "audit_metadata", None)
    if isinstance(audit, dict):
        raw = audit.get("announced_time_limit_secs") or audit.get("time_limit_seconds")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return 432000


def _build_direct_solve_signed_row(
    *,
    ctx: PublisherContext,
    eval_run_id: str,
    submission_id: str,
    agent_display_name: str,
    miner_hotkey: str,
    challenge_id: str,
    tier: int,
    time_limit_seconds: int,
    cnf_sha256: str,
    num_vars: int,
    num_clauses: int,
    dimacs_solution: str,
    verification: SubmissionVerification | None,
    weighted_score: float,
    rejection_reason: str | None,
    ran_at_iso: str,
    epoch: int,
    schema_version: int = TASK_FAMILY_SCHEMA_VERSION,
    challenge_value: float | None = None,
    solve_rank: int | None = None,
    solved: bool | None = None,
    operator: str | None = None,
) -> dict[str, object]:
    """Build the canonical signed row for direct solve POSTs.

    Defaults to schema 5 (single-winner path, byte-identical legacy shape).
    Pass ``schema_version=6`` + the PAR-2 facts for the open-window path.
    """
    epoch_salt = f"epoch_{epoch}:{SAT_FAMILY_ID}"
    problem = PublicProblem(
        task_family=SAT_FAMILY_ID,
        schema_version=SAT_SCHEMA_VERSION,
        task_id=challenge_id,
        difficulty_tier=int(tier),
        public_input={
            "format": "dimacs",
            "cnf_sha256": cnf_sha256,
            "num_vars": int(num_vars),
            "num_clauses": int(num_clauses),
        },
        time_limit_seconds=int(time_limit_seconds),
    )
    submission = Submission(
        task_id=challenge_id,
        miner_hotkey=miner_hotkey,
        answer={"dimacs_solution": dimacs_solution},
    )
    verifier_details: dict[str, object] = {
        "num_vars": int(num_vars),
        "num_clauses": int(num_clauses),
    }
    if verification is not None:
        verifier_details["dimacs_solution_sha256"] = verification.dimacs_solution_sha256
    verifier = VerifierResult(
        parsed_ok=weighted_score > 0.0,
        raw_metric=float(weighted_score),
        rejection_reason=rejection_reason,
        details=verifier_details,
    )
    score = ScoreResult(
        weighted_score=float(weighted_score),
        rejection_reason=rejection_reason,
        score_parts={"binary_correct": 1.0 if weighted_score > 0.0 else 0.0},
    )
    return build_signed_task_family_row(
        eval_run_id=eval_run_id,
        submission_id=submission_id,
        agent_display_name=agent_display_name,
        miner_hotkey=miner_hotkey,
        problem=problem,
        submission=submission,
        verifier=verifier,
        score=score,
        ran_at_iso=ran_at_iso,
        signer=ctx.signer,
        epoch_salt=epoch_salt,
        schema_version=schema_version,
        challenge_value=challenge_value,
        solve_rank=solve_rank,
        solved=solved,
        operator=operator,
    )


@router.get("/v1/synthetic-boolean/active-cnf")
async def get_active_cnf(
    request: Request,
    challenge_id: str | None = Query(default=None),
    tier: int | None = Query(default=None),
    x_cathedral_submitted_at: str = Header(default="", alias="X-Cathedral-Submitted-At"),
    auth: HotkeyAuth = Depends(hotkey_auth_header),
) -> dict[str, object]:
    """Return selected active SAT challenge metadata + a fetch token.

    Solve-on-submit miners call this to learn:
    - what challenge to solve (``challenge_id``)
    - where to fetch the CNF body (``cnf_url`` — a short-lived signed
      URL pointing at the existing ``/v1/challenges/{id}/cnf?t=...``
      surface)
    - cnf metadata (sha256, num_vars, num_clauses)
    - the announcement window (``active_since``, ``expires_at``)

    With no query string, this returns the default active challenge
    (lowest tier). Miners can pass ``?challenge_id=...`` or ``?tier=N``
    after discovering live slots from ``active-challenges``.

    The endpoint is hotkey-authenticated so only a hotkey holder can
    fetch the tokenized CNF URL. Public metadata lives on
    ``active-challenges`` / ``current-challenge``.

    Idempotent: calling it again returns the same token (existing
    ``lane_challenge_fetch_tokens`` row is reused — see
    :class:`SqliteFetchTokenStore`).
    """
    ctx: PublisherContext = request.app.state.ctx

    signed_submitted_at_iso = x_cathedral_submitted_at.strip()
    if not signed_submitted_at_iso:
        raise HTTPException(status_code=401, detail="missing X-Cathedral-Submitted-At")
    now = datetime.now(UTC)
    try:
        client_submitted_at = datetime.fromisoformat(
            signed_submitted_at_iso.replace("Z", "+00:00")
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail="X-Cathedral-Submitted-At must be ISO-8601",
        ) from e
    if client_submitted_at.tzinfo is None:
        client_submitted_at = client_submitted_at.replace(tzinfo=UTC)
    skew_secs = abs((now - client_submitted_at).total_seconds())
    if skew_secs > 300:
        logger.info(
            "active_cnf_clock_skew",
            hotkey=auth.hotkey_ss58,
            skew_secs=skew_secs,
        )
        raise HTTPException(
            status_code=400,
            detail="X-Cathedral-Submitted-At outside acceptable clock-skew window",
        )

    try:
        verify_hotkey_signature(
            hotkey_ss58=auth.hotkey_ss58,
            signature_b64=auth.signature_b64,
            bundle_hash=blake3.blake3(b"").hexdigest(),
            card_id=SAT_CARD_ID,
            submitted_at=signed_submitted_at_iso,
            challenge_id="",
            dimacs_solution_sha256="",
        )
    except InvalidSignatureError as e:
        logger.info("active_cnf_sig_failed", hotkey=auth.hotkey_ss58)
        raise HTTPException(status_code=401, detail="invalid hotkey signature") from e

    source = getattr(request.app.state, "task_family_challenge_source", None)
    tokens = getattr(request.app.state, "task_family_fetch_token_store", None)
    if source is None or tokens is None:
        raise HTTPException(
            status_code=503,
            detail="challenge surface not configured",
        )

    active = await _select_active_sat_challenge(
        source,
        challenge_id=challenge_id,
        tier=tier,
    )
    if active is None:
        selected_challenge_id = (challenge_id or "").strip()
        if selected_challenge_id:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": "challenge_not_active",
                    "requested": selected_challenge_id,
                },
            )
        if tier is not None:
            raise HTTPException(
                status_code=404,
                detail={
                    "detail": "no_active_challenge_for_tier",
                    "tier": tier,
                },
            )
        raise HTTPException(status_code=404, detail="no_active_challenge")

    audit = active.audit_metadata or {}
    cnf_sha256 = str(audit.get("cnf_sha256") or "")
    num_vars = int(audit.get("num_vars") or 0)
    num_clauses = int(audit.get("num_clauses") or 0)
    time_limit_secs = int(audit.get("announced_time_limit_secs") or 432000)

    # Mint (or reuse) a fetch token for this miner+challenge. Idempotent
    # by design — repeated calls return the same row.
    import secrets

    minted_at_iso = _ms_iso(datetime.now(UTC))
    fresh_token = secrets.token_urlsafe(32)
    async with ctx.db_write_lock:
        token_row = await tokens.mint_if_absent(
            active.challenge_id,
            fetch_token=fresh_token,
            minted_at_iso=minted_at_iso,
            announced_time_limit_secs=time_limit_secs,
        )

    base_url = (
        os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", "").strip().rstrip("/")
        or "https://api.cathedral.computer"
    )
    cnf_url = (
        f"{base_url}/api/cathedral/v1/challenges/{active.challenge_id}/cnf"
        f"?t={token_row.fetch_token}"
    )

    logger.info(
        "active_cnf_fetch",
        hotkey=auth.hotkey_ss58,
        challenge_id=active.challenge_id,
    )

    return {
        "challenge_id": active.challenge_id,
        "tier": active.tier,
        "cnf_url": cnf_url,
        "cnf_sha256": cnf_sha256,
        "num_vars": num_vars,
        "num_clauses": num_clauses,
        "active_since": token_row.minted_at_iso,
        "expires_at": _add_secs_iso(token_row.minted_at_iso, time_limit_secs),
    }


def _add_secs_iso(start_iso: str, secs: int) -> str:
    """Return start + secs as ms-precision Z ISO. Best-effort parse."""
    try:
        parsed = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return start_iso
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    from datetime import timedelta

    return _ms_iso(parsed + timedelta(seconds=int(secs)))


def _public_challenge_view(active) -> dict[str, object]:
    """Build the public, metadata-only projection of a ChallengeRecord.

    Never include ``cnf_url``, fetch tokens, raw CNF text, local paths,
    generator IDs, or any solver state. Miners still use signed
    ``active-cnf`` to fetch challenge material.
    """
    audit = active.audit_metadata or {}
    cnf_sha256 = str(audit.get("cnf_sha256") or "")
    num_vars = int(audit.get("num_vars") or 0)
    num_clauses = int(audit.get("num_clauses") or 0)
    cnf_bytes_raw = audit.get("cnf_bytes")
    try:
        cnf_bytes = int(cnf_bytes_raw) if cnf_bytes_raw is not None else None
    except (TypeError, ValueError):
        cnf_bytes = None
    if cnf_bytes is None and active.cnf_text:
        cnf_bytes = len(active.cnf_text.encode("utf-8"))

    kind = audit.get("kind")
    active_cnf_path = (
        "/api/cathedral/v1/synthetic-boolean/active-cnf"
        f"?challenge_id={quote(active.challenge_id, safe='')}"
    )
    # ``difficulty_label`` (#241) and ``score_multiplier`` (#236) are
    # operator-set publisher metadata that miners must see to choose a
    # challenge and to verify what each tier currently pays.
    score_multiplier = getattr(active, "score_multiplier", 1.0)
    try:
        score_multiplier_out = float(score_multiplier)
    except (TypeError, ValueError):
        score_multiplier_out = 1.0
    difficulty_label = getattr(active, "difficulty_label", None)
    return {
        "family_id": SAT_FAMILY_ID,
        "challenge_id": active.challenge_id,
        "status": active.status,
        "tier": active.tier,
        "difficulty_label": difficulty_label,
        "score_multiplier": score_multiplier_out,
        "kind": str(kind) if kind else None,
        "storage": "file" if active.cnf_path else "sqlite_text",
        "cnf_sha256": cnf_sha256,
        "cnf_bytes": cnf_bytes,
        "num_vars": num_vars,
        "num_clauses": num_clauses,
        "announced_time_limit_secs": _challenge_time_limit_seconds(active),
        "solve_on_submit_enabled": _pr5_solve_on_submit_enabled(),
        "win_rule": "First submitted valid SAT receipt wins.",
        "active_cnf_path": active_cnf_path,
        "submit_path": "/api/cathedral/v1/agents/submit",
    }


@router.get("/v1/synthetic-boolean/current-challenge")
async def get_current_sat_challenge(
    request: Request,
    tier: int | None = None,
    difficulty: str | None = None,
) -> dict[str, object]:
    """Return public metadata for one active SAT challenge.

    With no query string, returns the lowest-tier active challenge (legacy
    behaviour). With ``?tier=N``, returns an active challenge at that
    specific tier slot, or 404 if no active row exists there.

    With ``?difficulty=X`` (issue #241), returns the active challenge for
    the requested ``(tier, difficulty_label)`` pair. If no labeled row
    matches, falls back to the unlabeled active for that tier so legacy
    clients keep working during the multi-active rollout. Returns 404
    only when neither candidate exists.
    """
    source = getattr(request.app.state, "task_family_challenge_source", None)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail="challenge surface not configured",
        )

    if tier is None:
        if difficulty is not None:
            raise HTTPException(
                status_code=400,
                detail="difficulty requires tier",
            )
        active = await source.get_active(SAT_FAMILY_ID)
    else:
        if tier < 0:
            raise HTTPException(status_code=400, detail="tier must be >= 0")
        if difficulty is None:
            active = await source.get_active_for_tier(SAT_FAMILY_ID, tier)
        else:
            # Difficulty-scoped lookup: prefer an exact-label match, then
            # fall back to the unlabeled tier active so the legacy
            # ``?tier=N`` semantic remains intact while the operator
            # rolls out labeled rows.
            actives = await source.list_active(SAT_FAMILY_ID)
            matches = [
                rec
                for rec in actives
                if rec.tier == tier and rec.difficulty_label == difficulty
            ]
            if matches:
                active = matches[0]
            else:
                active = await source.get_active_for_tier(SAT_FAMILY_ID, tier)

    if active is None:
        raise HTTPException(status_code=404, detail="no_active_challenge")

    return _public_challenge_view(active)


@router.get("/v1/synthetic-boolean/active-challenges")
async def list_active_sat_challenges(request: Request) -> dict[str, object]:
    """Return public metadata for every currently active SAT challenge.

    Multi-tier discovery surface. With multiple actives (one per tier),
    miners can see all available challenges and pick the tier that suits
    their solver. Each entry is the same shape as ``current-challenge``;
    ordered by tier ascending then ``challenge_id`` ascending.
    """
    source = getattr(request.app.state, "task_family_challenge_source", None)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail="challenge surface not configured",
        )
    actives = await source.list_active(SAT_FAMILY_ID)
    return {
        "family_id": SAT_FAMILY_ID,
        "count": len(actives),
        "items": [_public_challenge_view(rec) for rec in actives],
    }


@router.get("/v1/synthetic-boolean/recent-wins")
async def list_recent_sat_wins(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """Return the most-recent solved SAT challenges with winner hotkey.

    Powers the leaderboard / "recently won" surface on the site so solved
    challenges remain visible alongside the active set with the winning
    miner's hotkey called out. Public metadata only: same shape as
    ``current-challenge`` for the challenge fields, plus ``winner_hotkey``
    and ``won_at`` from ``lane_challenge_winners``. No cnf_url, no tokens,
    no internal paths.

    Ordered by ``won_at`` descending; ``limit`` is clamped to [1, 100].
    """
    source = getattr(request.app.state, "task_family_challenge_source", None)
    if source is None:
        raise HTTPException(
            status_code=503,
            detail="challenge surface not configured",
        )

    # Pull winners + JOIN to lane_challenges in one round-trip. We rely on
    # the SqliteChallengeSource's internal connection — there is no
    # protocol method for this view yet because it is a denormalized
    # leaderboard projection rather than core challenge-source state.
    db = getattr(source, "_conn", None)
    if db is None:
        raise HTTPException(
            status_code=503,
            detail="challenge source backing connection not exposed",
        )
    cur = await db.execute(
        "SELECT w.challenge_id, w.miner_hotkey, w.weighted_score, w.won_at_iso, "
        "       c.tier, c.cnf_text, c.cnf_path, c.status, c.audit_metadata, "
        "       c.score_multiplier, c.difficulty_label "
        "FROM lane_challenge_winners w "
        "JOIN lane_challenges c ON c.challenge_id = w.challenge_id "
        "WHERE w.family_id = ? "
        "ORDER BY w.won_at_iso DESC LIMIT ?",
        (SAT_FAMILY_ID, int(limit)),
    )
    rows = await cur.fetchall()

    items: list[dict[str, object]] = []
    for row in rows:
        # Reuse the public projection by faking a record-shaped object so
        # the no-leak guarantee is identical to current-challenge.
        from cathedral.lanes.challenge_source import ChallengeRecord

        rec_audit = row[8]
        try:
            audit_dict = (
                rec_audit if isinstance(rec_audit, dict) else __import__("json").loads(rec_audit)
            )
        except Exception:
            audit_dict = {}
        try:
            score_multiplier_val = float(row[9]) if row[9] is not None else 1.0
        except (TypeError, ValueError):
            score_multiplier_val = 1.0
        difficulty_label_val = (
            str(row[10]) if len(row) > 10 and row[10] is not None else None
        )
        rec = ChallengeRecord(
            challenge_id=row[0],
            family_id=SAT_FAMILY_ID,
            tier=int(row[4]) if row[4] is not None else 0,
            cnf_text=row[5] or "",
            cnf_path=row[6],
            status=row[7],
            audit_metadata=audit_dict,
            score_multiplier=score_multiplier_val,
            difficulty_label=difficulty_label_val,
        )
        view = _public_challenge_view(rec)
        view["winner_hotkey"] = row[1]
        view["weighted_score"] = float(row[2]) if row[2] is not None else None
        view["won_at"] = row[3]
        items.append(view)

    return {
        "family_id": SAT_FAMILY_ID,
        "count": len(items),
        "items": items,
    }


def _resolve_challenge_source(ctx: PublisherContext):
    """Pull the challenge source off the app state via ctx if possible."""
    # The challenge source is wired in publisher.app.lifespan onto
    # ``app.state.task_family_challenge_source`` rather than the ctx
    # itself. Tests using ``build_app`` go through that lifespan so the
    # attribute should exist; for completeness we try ctx first.
    src = getattr(ctx, "task_family_challenge_source", None)
    if src is not None:
        return src
    # As a defence against ordering issues in tests, construct a fresh
    # source on the shared db connection. This is safe because
    # ``SqliteChallengeSource`` is stateless beyond the connection.
    from cathedral.lanes.challenge_source import SqliteChallengeSource

    return SqliteChallengeSource(ctx.db)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Read up to ``cap`` bytes from a SpooledTemporaryFile-backed upload."""
    out = bytearray()
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        out.extend(chunk)
        if len(out) > cap:
            break
    return bytes(out)


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC with millisecond precision and trailing ``Z``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"
