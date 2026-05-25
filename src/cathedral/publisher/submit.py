"""POST /v1/agents/submit — SAT-only registration handler (PR2).

Cathedral SN39 moved off card-shaped Hermes bundles onto the
``synthetic_boolean_v1`` (SAT) task family lane. Per recovery-plan
Decision 1 (Option A), ``card_id`` survives on the wire as a task-family
discriminator with the single accepted value ``synthetic_boolean_v1``;
anything else 400s with a pointer to the live skill manifest.

Flow:

    multipart upload  (card_id="synthetic_boolean_v1", display_name,
                       attestation_mode="ssh-probe", ssh_host, ssh_user,
                       [ssh_port], [bio], [bundle - optional, ignored])
       │
       ▼
    auth header dep (X-Cathedral-Hotkey, X-Cathedral-Signature)
       │
       ▼
    sr25519 signature over canonical_json({bundle_hash, card_id,
                                           miner_hotkey, submitted_at})
       │   - signature mismatch    -> 401
       │   - card_id != "synthetic_boolean_v1"  -> 400
       │   - attestation_mode != "ssh-probe"    -> 400
       ▼
    INSERT agent_submissions (status='pending_check', SAT shape, no
                              card-era scoring side-effects)
       │   - UNIQUE violation -> 409
       ▼
    202 Accepted { id, status="pending_check", submitted_at }
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
import blake3
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from cathedral.auth import InvalidSignatureError, verify_hotkey_signature
from cathedral.publisher.auth_signature import HotkeyAuth, hotkey_auth_header

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
    auth: HotkeyAuth = Depends(hotkey_auth_header),
) -> dict[str, str]:
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

    try:
        verify_hotkey_signature(
            hotkey_ss58=auth.hotkey_ss58,
            signature_b64=auth.signature_b64,
            bundle_hash=bundle_hash,
            card_id=card_id,
            submitted_at=signed_submitted_at_iso,
        )
    except InvalidSignatureError as e:
        logger.info("submission_sig_failed", hotkey=auth.hotkey_ss58)
        raise HTTPException(status_code=401, detail="invalid hotkey signature") from e

    submitted_at = server_submitted_at
    submitted_at_iso = server_submitted_at_iso

    # ----- INSERT agent_submissions -----------------------------------------
    # SAT registrations skip the card-era scoring side-effects entirely:
    #   - no similarity dedupe
    #   - no first-mover anchor
    #   - no Merkle epoch close
    #   - no card scoring pipeline kick-off
    #   - no card_definitions write
    # The row lands in 'pending_check' so the SAT prober loop can pick it
    # up and start running probes against the miner's box.
    submission_id = str(uuid4())
    from cathedral.publisher import repository

    try:
        async with ctx.db_write_lock:
            await repository.insert_agent_submission(
                ctx.db,
                id=submission_id,
                miner_hotkey=auth.hotkey_ss58,
                card_id=card_id,
                bundle_blob_key="",
                bundle_hash=bundle_hash,
                bundle_size_bytes=bundle_size_bytes,
                encryption_key_id="",
                bundle_signature=auth.signature_b64,
                display_name=display_name,
                bio=bio,
                logo_url=None,
                soul_md_preview=None,
                # SAT registrations have no fingerprint surface — leave the
                # column populated with a stable sentinel so the schema-level
                # NOT NULL constraint is satisfied without inviting collisions.
                metadata_fingerprint="",
                similarity_check_passed=True,
                rejection_reason=None,
                status="pending_check",
                submitted_at=submitted_at,
                submitted_at_iso=submitted_at_iso,
                first_mover_at=None,
                attestation_mode=SSH_PROBE_MODE,
                attestation_type=None,
                attestation_blob=None,
                attestation_verified_at=None,
                discovery_only=False,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                hermes_port=None,
            )
    except aiosqlite.IntegrityError as e:
        logger.warning(
            "submission_rejected_409_integrity",
            hotkey=auth.hotkey_ss58,
            card_id=card_id,
            bundle_hash=bundle_hash,
            error=str(e),
        )
        raise HTTPException(status_code=409, detail="duplicate submission") from e

    logger.info(
        "sat_submission_accepted",
        submission_id=submission_id,
        hotkey=auth.hotkey_ss58,
        card_id=card_id,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_port=ssh_port,
    )

    return {
        "id": submission_id,
        "status": "pending_check",
        "submitted_at": submitted_at_iso,
    }


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
