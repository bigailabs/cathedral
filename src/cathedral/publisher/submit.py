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
    UPSERT agent_submissions  (status='pending_solution' with PR5 flag on;
                                status='pending_check' on legacy flag-off path)
       │
       ▼  (only when PR5 flag is on AND dimacs_solution present)
    Verify DIMACS against active challenge
       │   - challenge_not_active  -> 409
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
from uuid import uuid4

import blake3
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from cathedral.auth import InvalidSignatureError, verify_hotkey_signature
from cathedral.lanes.contract import PublicProblem, ScoreResult, Submission, VerifierResult
from cathedral.lanes.sign import build_signed_task_family_row
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

if TYPE_CHECKING:
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

    # ----- UPSERT agent_submissions -----------------------------------------
    # PR5: a re-submission from the same hotkey + bundle_hash is now an
    # UPDATE of the SSH coordinates rather than a 409. This is a UX fix
    # for solve-POSTs: miners iterating on their solver setup expect to
    # re-register without 409.
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
        if len(dimacs_solution or "") > _MAX_DIMACS_BYTES:
            raise HTTPException(
                status_code=413,
                detail="dimacs_solution exceeds size limit",
            )
        result_body = await _handle_solve_post(
            ctx=ctx,
            submission_id=submission_id,
            miner_hotkey=auth.hotkey_ss58,
            agent_display_name=display_name,
            challenge_id=(challenge_id or "").strip(),
            dimacs_solution=dimacs_solution or "",
            dimacs_solution_sha256=solution_sha256,
            submitted_at_iso=submitted_at_iso,
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
# PR5 solve-on-submit branch
# --------------------------------------------------------------------------


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
) -> dict[str, object]:
    """Verify, lock, and persist a solve-POST.

    Separated from the main handler so the flow is readable and easier
    to unit-test. Mirrors the spec's "Server-side logic" section.

    Returns the JSON body the client receives; raises HTTPException with
    the spec's documented status codes for every error path.
    """
    from cathedral.lanes.challenge_source import SqliteChallengeSource
    from cathedral.publisher import repository

    if not challenge_id:
        raise HTTPException(
            status_code=400,
            detail="challenge_id required when dimacs_solution is present",
        )

    # The challenge source is wired onto app.state by the lifespan. In
    # tests with no lifespan the attribute may be missing; treat as 503.
    source: SqliteChallengeSource | None = getattr(
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

    active = await source.get_active(SAT_FAMILY_ID)
    if active is None or active.challenge_id != challenge_id:
        # Either no challenge is active, OR a different challenge is
        # active. Differentiate the two cases for clearer client UX,
        # and detect the "challenge already locked" case by looking up
        # the requested row directly (locked rows aren't 'active' but
        # they're a normal post-winner state).
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
        if active is None:
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
                "active_challenge_id": active.challenge_id,
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

    # ----- Atomic lock + insert --------------------------------------------
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
) -> dict[str, object]:
    """Build the canonical signed schema-5 row for direct solve POSTs."""
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
    )


@router.get("/v1/synthetic-boolean/active-cnf")
async def get_active_cnf(
    request: Request,
    auth: HotkeyAuth = Depends(hotkey_auth_header),
) -> dict[str, object]:
    """Return the currently-active SAT challenge metadata + a fetch token.

    PR5 (solve-on-submit) miners call this to learn:
    - what challenge to solve (``challenge_id``)
    - where to fetch the CNF body (``cnf_url`` — a short-lived signed
      URL pointing at the existing ``/v1/challenges/{id}/cnf?t=...``
      surface)
    - cnf metadata (sha256, num_vars, num_clauses)
    - the announcement window (``active_since``, ``expires_at``)

    The endpoint is hotkey-authenticated so only registered miners can
    fetch. (Open Question 4: auth gives anti-precompute; spec leans
    "auth, short TTL on token".)

    Idempotent: calling it again returns the same token (existing
    ``lane_challenge_fetch_tokens`` row is reused — see
    :class:`SqliteFetchTokenStore`).
    """
    source = getattr(request.app.state, "task_family_challenge_source", None)
    tokens = getattr(request.app.state, "task_family_fetch_token_store", None)
    if source is None or tokens is None:
        raise HTTPException(
            status_code=503,
            detail="challenge surface not configured",
        )

    active = await source.get_active(SAT_FAMILY_ID)
    if active is None:
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
