"""POST /v1/agents/submit handler.

Flow (CONTRACTS.md Section 6 step 1 + 2):

    multipart upload  (bundle, card_id, display_name,
                       attestation_mode, [attestation, attestation_type])
       │
       ▼
    auth header dep (X-Cathedral-Hotkey, X-Cathedral-Signature)
       │
       ▼
    bundle bytes -> blake3 = bundle_hash (SERVER-COMPUTED)
       │
       ▼
    verify hotkey signature against canonical_json({bundle_hash,
                                                    card_id, hotkey,
                                                    submitted_at})
       │   - signature mismatch       -> 401
       │   - card_id unknown          -> 404
       │   - bundle structure invalid -> 422
       │   - bundle too large         -> 413
       ▼
    branch on attestation_mode:
       │
       ├── 'polaris' (default, back-compat) ────────────────────────────┐
       │     run similarity + encrypt + Hippius PUT + INSERT 'queued'   │
       │                                                                │
       ├── 'tee' ─────────────────────────────────────────────────────┐ │
       │     verify Nitro / TDX / SEV-SNP attestation chain + binding │ │
       │       - chain / sig / binding bad -> 401                     │ │
       │       - PCR not in approved list  -> 401                     │ │
       │       - TDX / SEV-SNP unsupported -> 501                     │ │
       │     run similarity + encrypt + Hippius PUT + INSERT          │ │
       │     attestation_blob / verified_at persisted alongside       │ │
       │                                                              │ │
       └── 'unverified' ──────────────────────────────────────────────┘ │
             skip similarity, encrypt + Hippius PUT,                    │
             INSERT 'discovery' / discovery_only=true,                  │
             do NOT enqueue eval                                        │
                                                                        ▼
       INSERT agent_submissions
         │   - UNIQUE violation -> 409
         ▼
       202 Accepted { id, bundle_hash, status, submitted_at }
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import aiosqlite
import blake3
import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status

from cathedral.attestation import (
    ATTESTATION_MODES,
    ATTESTATION_TYPES,
    AttestationResult,
    InvalidAttestationError,
    UnapprovedRuntimeError,
    UnsupportedAttestationTypeError,
    verify_attestation,
)
from cathedral.auth import InvalidSignatureError, verify_hotkey_signature
from cathedral.publisher import repository, similarity
from cathedral.publisher.auth_signature import HotkeyAuth, hotkey_auth_header
from cathedral.publisher.similarity import SimilarityRejection
from cathedral.storage import (
    BundleStructureError,
    BundleTooLargeError,
    EncryptionError,
    HippiusError,
    encrypt_bundle,
    validate_hermes_bundle,
)

if TYPE_CHECKING:
    from cathedral.publisher.app import PublisherContext

logger = structlog.get_logger(__name__)


_MAX_BUNDLE_BYTES = 10 * 1024 * 1024
_MAX_LOGO_BYTES = 200 * 1024
_ALLOWED_LOGO_TYPES = {"image/png", "image/jpeg", "image/webp"}
_LOGO_EXT_BY_TYPE = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_MAX_DISPLAY_NAME = 64
_MAX_BIO = 280

# Real Nitro attestation docs are typically 4-8 KiB; TDX / SEV-SNP are
# bigger but still well under 64 KiB. Cap to keep the multipart cheap and
# to refuse silly payloads early.
_MAX_ATTESTATION_BYTES = 64 * 1024


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
    bundle: UploadFile = File(...),
    card_id: str = Form(...),
    display_name: str = Form(...),
    bio: str | None = Form(default=None),
    # CRIT-1: `submitted_at` MUST come from the server clock. We accept
    # the form field for backward-compat with existing miners but IGNORE
    # its value — the server-generated timestamp is always authoritative.
    # The miner must sign over the server-issued timestamp returned in the
    # response (or call the prospective /v1/server-time endpoint).
    submitted_at_form: str | None = Form(default=None, alias="submitted_at"),
    logo: UploadFile | None = File(default=None),
    # Per cathedralai/cathedral#70 the v1 default is 'ssh-probe', the
    # BYO Box live path. Polaris modes ('polaris', 'polaris-deploy') are
    # accepted only when CATHEDRAL_ENABLE_POLARIS_DEPLOY=true; off by
    # default in production, they 400 with rejection_reason
    # 'tier_a_disabled_for_v1' and a pointer to the BYO Box docs.
    attestation_mode: str = Form(default="ssh-probe"),
    attestation: str | None = Form(default=None),
    attestation_type: str | None = Form(default=None),
    # BYO Box (ssh-probe). Only required when attestation_mode='ssh-probe';
    # ignored for every other mode. The miner must have Cathedral's public SSH
    # key installed in ~ssh_user/.ssh/authorized_keys on ssh_host.
    ssh_host: str | None = Form(default=None),
    ssh_port: int | None = Form(default=None),
    ssh_user: str | None = Form(default=None),
    # `hermes_port` is deprecated as of v1.1.0 (cathedralai/cathedral#75,
    # PR #77 — Hermes is CLI-shaped, not HTTP-shaped; the probe SSHes
    # in and runs `hermes` rather than curling an HTTP endpoint).
    # Accepted from the wire for back-compat with v1.0.x miner clients
    # but its value is logged and ignored — every new submission
    # persists hermes_port=NULL.
    #
    # Removable in v1.2.0 once all v1.0.x miner clients have upgraded.
    # Search marker: `removable-in-v1-2-0`.
    hermes_port: int | None = Form(default=None),
    auth: HotkeyAuth = Depends(hotkey_auth_header),
) -> dict[str, str]:
    ctx: PublisherContext = request.app.state.ctx

    if ctx.submissions_paused:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="submissions paused",
        )

    # ----- attestation_mode validation (cheap, do first) -----------------
    # Reject bad mode values before we read 10 MiB of bundle. Type
    # validation for `tee` happens after we've parsed the bundle so the
    # 401 paths can include the bundle_hash in their structured logs.
    if attestation_mode not in ATTESTATION_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid attestation_mode: {attestation_mode!r}; "
                f"must be one of {sorted(ATTESTATION_MODES)}"
            ),
        )

    # ----- Polaris deploy gate (cathedralai/cathedral#70) ----------------
    # Both Polaris-flavored modes route to Cathedral-owned compute and
    # depend on shared Verda balance + cathedral-runtime availability. v1
    # keeps the live path on BYO Box (ssh-probe / bundle / tee /
    # unverified) until Polaris deployment has proper isolation.
    # The runner code stays;
    # the door is closed at the submit boundary.
    import os as _os

    _tier_a = {"polaris", "polaris-deploy"}
    _tier_a_enabled = _os.environ.get("CATHEDRAL_ENABLE_POLARIS_DEPLOY", "").lower() == "true"
    if attestation_mode in _tier_a and not _tier_a_enabled:
        raise HTTPException(
            status_code=400,
            detail=(
                "tier_a_disabled_for_v1: attestation_mode "
                f"{attestation_mode!r} is not accepted on v1. "
                "Mine on BYO Box instead; see "
                "https://api.cathedral.computer/skill.md for the "
                "ssh-probe flow. Polaris deployment is not a live v1 path; "
                "track cathedralai/cathedral#70 for status."
            ),
        )

    # ----- ssh-probe coordinates (BYO Box) -------------------------------
    # When attestation_mode='ssh-probe', the miner runs Hermes themselves
    # and Cathedral SSHs in to invoke `hermes chat -q "<task>"` (v1.1.7;
    # was `hermes -z` pre-v1.1.7) as a subprocess.
    # ssh_host + ssh_user are required; ssh_port defaults to 22. For
    # every other mode, ssh_* fields are silently ignored — we don't
    # want a typo in a BYO Box field to block a gated Polaris submission.
    #
    # `hermes_port` is logged for back-compat observation (v1.0.x clients
    # may still send it) but is never persisted on new rows. Hermes is
    # CLI-shaped; there is no HTTP endpoint to point at. See issue #75.
    if hermes_port is not None:
        logger.info(
            "submit_hermes_port_ignored",
            value=hermes_port,
            mode=attestation_mode,
            note="hermes_port deprecated in v1.1.0 (issue #75)",
        )
    hermes_port = None

    if attestation_mode == "ssh-probe":
        if not ssh_host or not ssh_user:
            raise HTTPException(
                status_code=400,
                detail=(
                    "attestation_mode='ssh-probe' requires ssh_host and "
                    "ssh_user (ssh_port defaults to 22). See "
                    "cathedral.computer/verification for the free-tier "
                    "registration shape."
                ),
            )
        if ssh_port is None:
            ssh_port = 22
        if not (1 <= ssh_port <= 65535):
            raise HTTPException(
                status_code=400,
                detail=f"ssh_port out of range: {ssh_port}",
            )
        if len(ssh_host) > 253 or len(ssh_user) > 32:
            raise HTTPException(status_code=400, detail="ssh_host / ssh_user too long")
    else:
        # Don't persist stale ssh_* on non-ssh-probe submissions.
        ssh_host = None
        ssh_port = None
        ssh_user = None

    # ----- form validation ---------------------------------------------
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

    if not bundle.filename or not bundle.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="bundle must be a .zip file")

    # ----- bundle bytes ------------------------------------------------
    raw = await _read_capped(bundle, _MAX_BUNDLE_BYTES + 1)
    if len(raw) > _MAX_BUNDLE_BYTES:
        # CONTRACTS.md L6: oversized bundle returns 413 Payload Too Large.
        raise HTTPException(
            status_code=413,
            detail=f"bundle exceeds {_MAX_BUNDLE_BYTES // (1024 * 1024)} MiB limit",
        )

    try:
        validate_hermes_bundle(raw)
    except BundleTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e
    except BundleStructureError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    bundle_hash = blake3.blake3(raw).hexdigest()

    # ----- card definition --------------------------------------------
    card_def = await repository.get_card_definition(ctx.db, card_id)
    if card_def is None:
        raise HTTPException(status_code=404, detail=f"card not found: {card_id}")
    if card_def["status"] != "active":
        raise HTTPException(status_code=404, detail=f"card not active: {card_id}")

    # ----- timestamp + signature verify --------------------------------
    # CONTRACTS.md §4.1 + CRIT-1: server clock is the SOLE source of truth
    # for `submitted_at` recorded in agent_submissions and propagated to
    # `first_mover_at`. The client-supplied form field (if any) is used
    # ONLY to verify the hotkey signature — its value is NEVER persisted
    # and NEVER used for first-mover anchoring. A backdated client value
    # may still verify the signature, but it loses the first-mover race
    # to whoever actually arrived at the server first. Additionally, a
    # client value too far from server time is rejected outright to close
    # the obvious far-future / far-past window without relying on the
    # downstream first-mover comparison alone.
    server_submitted_at = datetime.now(UTC)
    server_submitted_at_iso = _ms_iso(server_submitted_at)

    if submitted_at_form:
        try:
            client_submitted_at = datetime.fromisoformat(submitted_at_form.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(status_code=400, detail="submitted_at must be ISO-8601") from e
        if client_submitted_at.tzinfo is None:
            client_submitted_at = client_submitted_at.replace(tzinfo=UTC)
        # ±5 minute clock-skew window — anything wider is considered a
        # backdating / forward-dating attempt.
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
    _ = request  # keep for trace-id middleware compatibility

    # Bind the persisted timestamp to the server clock — this is what
    # `first_mover_at` and the response carry. The client's value (if any)
    # was used solely to verify the signature.
    submitted_at = server_submitted_at
    submitted_at_iso = server_submitted_at_iso

    # ----- TEE attestation verification (mode='tee' only) ----------------
    # Done after signature verification so a miner can't burn our crypto
    # budget without a valid hotkey. Order:
    #   1. validate the form fields are well-formed
    #   2. decode the blob (base64)
    #   3. dispatch to Nitro / TDX / SEV-SNP verifier
    # Returns either an `AttestationResult` (tee mode) or None.
    attestation_result: AttestationResult | None = None
    attestation_blob_bytes: bytes | None = None
    if attestation_mode == "tee":
        attestation_result, attestation_blob_bytes = await _verify_tee_attestation(
            attestation=attestation,
            attestation_type=attestation_type,
            bundle_hash=bundle_hash,
            card_id=card_id,
            hotkey=auth.hotkey_ss58,
        )

    # ----- similarity check --------------------------------------------
    # CONTRACTS.md L7: similarity rejection returns 202 with status=rejected
    # + rejection_reason; EXCEPT for "exact bundle duplicate" which the
    # contract test pins as 409 (cross-hotkey same-bundle) and 409 for
    # same-hotkey duplicate (idx_agent_unique). Other similarity reasons
    # (fingerprint, fuzzy display name) go through the 202+rejected path.
    #
    # `unverified` (discovery) submissions skip similarity entirely:
    # fingerprint clashes are expected and not meaningful for discovery
    # since these never enter the eval queue. We still compute the
    # fingerprint so the row is consistent with the rest of the table.
    rejection_reason: str | None = None
    sim_metadata_fingerprint: str
    sim_display_name_norm: str
    if attestation_mode == "unverified":
        sim_metadata_fingerprint = similarity.metadata_fingerprint(
            display_name=display_name, bundle_size_bytes=len(raw)
        )
        sim_display_name_norm = similarity.normalize_display_name(display_name)
        _ = sim_display_name_norm  # reserved for future use
    else:
        try:
            sim = await similarity.run_similarity_check(
                ctx.db,
                miner_hotkey=auth.hotkey_ss58,
                card_id=card_id,
                display_name=display_name,
                bundle_hash=bundle_hash,
                bundle_size_bytes=len(raw),
            )
            sim_metadata_fingerprint = sim.metadata_fingerprint
            sim_display_name_norm = sim.display_name_norm
            _ = sim_display_name_norm  # reserved for future use
        except SimilarityRejection as e:
            msg = str(e)
            # "duplicate submission" = same hotkey same bundle (covered by
            # UNIQUE index too); "exact bundle duplicate" = cross-hotkey.
            # Both 409.
            if msg in {"duplicate submission", "exact bundle duplicate"}:
                logger.warning(
                    "submission_rejected_409_similarity",
                    hotkey=auth.hotkey_ss58,
                    card_id=card_id,
                    bundle_hash=bundle_hash,
                    reason=msg,
                )
                raise HTTPException(status_code=409, detail=msg) from e
            # Other similarity rejections: 202 + status=rejected.
            rejection_reason = msg
            sim_metadata_fingerprint = similarity.metadata_fingerprint(
                display_name=display_name, bundle_size_bytes=len(raw)
            )
            sim_display_name_norm = similarity.normalize_display_name(display_name)

    # ----- logo upload (optional, before bundle so we can reference URL) -
    submission_id = str(uuid4())
    logo_url: str | None = None

    # If similarity check rejected (non-exact-duplicate path), persist the
    # row with status=rejected and return 202 with rejection_reason. Skip
    # bundle upload + first-mover anchor.
    if rejection_reason is not None:
        async with ctx.db_write_lock:
            await repository.insert_agent_submission(
                ctx.db,
                id=submission_id,
                miner_hotkey=auth.hotkey_ss58,
                card_id=card_id,
                bundle_blob_key="",
                bundle_hash=bundle_hash,
                bundle_size_bytes=len(raw),
                encryption_key_id="",
                bundle_signature=auth.signature_b64,
                display_name=display_name,
                bio=bio,
                logo_url=None,
                soul_md_preview=None,
                metadata_fingerprint=sim_metadata_fingerprint,
                similarity_check_passed=False,
                rejection_reason=rejection_reason,
                status="rejected",
                submitted_at=submitted_at,
                submitted_at_iso=submitted_at_iso,
                first_mover_at=None,
            )
        logger.info(
            "submission_rejected_similarity",
            submission_id=submission_id,
            hotkey=auth.hotkey_ss58,
            card_id=card_id,
            reason=rejection_reason,
        )
        return {
            "id": submission_id,
            "bundle_hash": bundle_hash,
            "status": "rejected",
            "rejection_reason": rejection_reason,
            "submitted_at": submitted_at_iso,
        }

    if logo is not None and logo.filename:
        if logo.content_type not in _ALLOWED_LOGO_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"logo content type not allowed: {logo.content_type}",
            )
        logo_bytes = await _read_capped(logo, _MAX_LOGO_BYTES + 1)
        if len(logo_bytes) > _MAX_LOGO_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"logo exceeds {_MAX_LOGO_BYTES // 1024} KiB limit",
            )
        try:
            logo_url = await ctx.hippius.put_logo(
                submission_id,
                logo_bytes,
                content_type=logo.content_type or "image/png",
                ext=_LOGO_EXT_BY_TYPE.get(logo.content_type or "", "png"),
            )
        except HippiusError as e:
            logger.warning("logo_upload_failed", error=str(e))
            # Logo is optional; don't fail the whole submission.
            logo_url = None

    # ----- encrypt + upload bundle -------------------------------------
    try:
        encrypted = encrypt_bundle(raw)
    except EncryptionError as e:
        logger.error("encrypt_failed", error=str(e))
        raise HTTPException(status_code=500, detail="bundle encryption failed") from e

    try:
        blob_key = await ctx.hippius.put_bundle(
            submission_id,
            encrypted.ciphertext,
            bundle_hash_hex=bundle_hash,
        )
    except HippiusError as e:
        logger.warning("bundle_upload_failed", error=str(e))
        raise HTTPException(status_code=503, detail="bundle storage unavailable") from e

    # ----- first-mover anchor -----------------------------------------
    # Unverified (discovery) submissions never enter scoring, so the
    # first-mover anchor does not apply — anchoring a discovery row would
    # poison a later polaris/tee miner's first-mover claim on the same
    # fingerprint. Skip the anchor and persist `first_mover_at = NULL`.
    first_mover_dt: datetime | None
    if attestation_mode == "unverified":
        first_mover_dt = None
    else:
        existing_first = await repository.first_mover_for_fingerprint(
            ctx.db, card_id, sim_metadata_fingerprint
        )
        first_mover_at = existing_first["first_mover_at"] if existing_first else submitted_at_iso
        if isinstance(first_mover_at, str):
            try:
                first_mover_dt = datetime.fromisoformat(first_mover_at.replace("Z", "+00:00"))
            except ValueError:
                first_mover_dt = submitted_at
        else:
            first_mover_dt = first_mover_at

    # ----- INSERT -----------------------------------------------------
    # Status branches on attestation_mode:
    #   * unverified -> 'discovery' (never enters eval queue)
    #   * polaris/tee -> 'queued'   (existing eval pipeline picks up)
    submission_status = "discovery" if attestation_mode == "unverified" else "queued"
    discovery_only = attestation_mode == "unverified"
    try:
        async with ctx.db_write_lock:
            # Share the PublisherContext write gate with SAT finalization:
            # insert_agent_submission() commits on ctx.db, and a concurrent
            # commit would otherwise close a finalizer BEGIN IMMEDIATE early.
            await repository.insert_agent_submission(
                ctx.db,
                id=submission_id,
                miner_hotkey=auth.hotkey_ss58,
                card_id=card_id,
                bundle_blob_key=blob_key,
                bundle_hash=bundle_hash,
                bundle_size_bytes=len(raw),
                encryption_key_id=encrypted.encryption_key_id,
                bundle_signature=auth.signature_b64,
                display_name=display_name,
                bio=bio,
                logo_url=logo_url,
                soul_md_preview=None,  # populated post-decrypt during eval
                metadata_fingerprint=sim_metadata_fingerprint,
                similarity_check_passed=True,
                rejection_reason=None,
                status=submission_status,
                submitted_at=submitted_at,
                submitted_at_iso=submitted_at_iso,
                first_mover_at=first_mover_dt,
                attestation_mode=attestation_mode,
                attestation_type=(attestation_type if attestation_mode == "tee" else None),
                attestation_blob=(attestation_blob_bytes if attestation_mode == "tee" else None),
                attestation_verified_at=(
                    attestation_result.verified_at if attestation_result is not None else None
                ),
                discovery_only=discovery_only,
                ssh_host=ssh_host,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                hermes_port=hermes_port,
            )
    except aiosqlite.IntegrityError as e:
        # idx_agent_unique violation = same hotkey + same card + same bundle.
        # Best-effort cleanup of the freshly uploaded blob.
        logger.warning(
            "submission_rejected_409_integrity",
            hotkey=auth.hotkey_ss58,
            card_id=card_id,
            bundle_hash=bundle_hash,
            attestation_mode=attestation_mode,
            error=str(e),
        )
        try:
            await ctx.hippius.delete_bundle(blob_key)
        except HippiusError:
            pass
        raise HTTPException(status_code=409, detail="duplicate submission") from e

    logger.info(
        "submission_accepted",
        submission_id=submission_id,
        hotkey=auth.hotkey_ss58,
        card_id=card_id,
        bundle_hash=bundle_hash,
        size=len(raw),
        attestation_mode=attestation_mode,
    )

    # Unverified responses surface 'discovery' so the miner sees clearly
    # that no eval will run. Polaris / tee submissions keep the existing
    # 'pending_check' soft state until the eval pipeline picks them up.
    response_status = "discovery" if attestation_mode == "unverified" else "pending_check"
    return {
        "id": submission_id,
        "bundle_hash": bundle_hash,
        "status": response_status,
        "submitted_at": submitted_at_iso,
    }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def _verify_tee_attestation(
    *,
    attestation: str | None,
    attestation_type: str | None,
    bundle_hash: str,
    card_id: str,
    hotkey: str,
) -> tuple[AttestationResult, bytes]:
    """Verify a TEE attestation submitted alongside the bundle.

    Returns ``(result, blob_bytes)`` on success; raises HTTPException with
    a contract-shaped detail on failure:

      * 400 — missing fields / oversized blob / not base64
      * 401 — bad signature / chain / binding / unapproved runtime
      * 501 — TDX or SEV-SNP (verifier pending)
    """
    if not attestation:
        raise HTTPException(
            status_code=400,
            detail="attestation_mode=tee requires the attestation form field",
        )
    if not attestation_type:
        raise HTTPException(
            status_code=400,
            detail=(
                "attestation_mode=tee requires the attestation_type form field "
                f"(one of {sorted(ATTESTATION_TYPES)})"
            ),
        )
    if attestation_type not in ATTESTATION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid attestation_type: {attestation_type!r}; "
                f"must be one of {sorted(ATTESTATION_TYPES)}"
            ),
        )

    try:
        blob_bytes = base64.b64decode(attestation, validate=True)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"attestation is not valid base64: {e}",
        ) from e

    if len(blob_bytes) > _MAX_ATTESTATION_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(f"attestation exceeds {_MAX_ATTESTATION_BYTES // 1024} KiB limit"),
        )

    try:
        result = verify_attestation(
            attestation_type=attestation_type,
            attestation_bytes=blob_bytes,
            bundle_hash=bundle_hash,
            card_id=card_id,
        )
    except UnsupportedAttestationTypeError as e:
        # Surfaces 501 — explicit so the next agent (TDX / SEV-SNP) knows
        # exactly which path to wire.
        logger.info(
            "tee_attestation_unsupported",
            hotkey=hotkey,
            attestation_type=attestation_type,
        )
        raise HTTPException(status_code=501, detail=str(e)) from e
    except UnapprovedRuntimeError as e:
        logger.info(
            "tee_attestation_unapproved_runtime",
            hotkey=hotkey,
            attestation_type=attestation_type,
            error=str(e),
        )
        raise HTTPException(status_code=401, detail=f"tee attestation invalid: {e}") from e
    except InvalidAttestationError as e:
        logger.info(
            "tee_attestation_invalid",
            hotkey=hotkey,
            attestation_type=attestation_type,
            error=str(e),
        )
        raise HTTPException(status_code=401, detail=f"tee attestation invalid: {e}") from e

    return result, blob_bytes


async def _read_capped(upload: UploadFile, cap: int) -> bytes:
    """Read up to `cap` bytes from a SpooledTemporaryFile-backed upload.

    Reads in 64 KiB chunks so we never accidentally pull a multi-GiB
    body into memory if the client lies about content-length.
    """
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
    """ISO-8601 UTC with millisecond precision and trailing `Z`."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"
