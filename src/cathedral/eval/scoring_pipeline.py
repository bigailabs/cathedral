"""Decode card -> preflight -> score -> first-mover delta -> sign -> store.

CONTRACTS.md Section 6 step 4-5. The scorer (`cathedral.cards.score_card`)
and preflight (`cathedral.cards.preflight`) are reused from the existing
codebase.

First-mover delta logic (Section 7.2):
    if submission is the first mover for (card_id, fingerprint):
        multiplier = 1.0
    elif weighted >= incumbent_best + 0.05:
        multiplier = 1.0       # late but materially better
    elif (now - first_mover_at).days > 30:
        multiplier = 1.0       # window closed
    else:
        multiplier = 0.50      # within window, not materially better
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
import blake3
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.cards import preflight, score_card
from cathedral.cards.preflight import PreflightError
from cathedral.cards.registry import CardRegistry, RegistryEntry
from cathedral.eval.scorer_metrics import (
    bump_audit_failure,
    set_scorer_misconfig,
)
from cathedral.eval.scoring import (
    FIRST_MOVER_DELTA,
    FIRST_MOVER_PENALTY_MULTIPLIER,
    FIRST_MOVER_WINDOW_DAYS,
    first_mover_multiplier,
)
from cathedral.publisher import repository
from cathedral.types import Card
from cathedral.v1_types import canonical_json

logger = structlog.get_logger(__name__)


_FIRST_MOVER_WINDOW_DAYS = FIRST_MOVER_WINDOW_DAYS
_FIRST_MOVER_DELTA_THRESHOLD = FIRST_MOVER_DELTA
_FIRST_MOVER_PENALTY_MULTIPLIER = FIRST_MOVER_PENALTY_MULTIPLIER

# Verified-runtime multiplier for Tier A (Polaris-hosted, attested) runs.
# cathedralai/cathedral#70: gated behind CATHEDRAL_ENABLE_POLARIS_DEPLOY;
# kept in source so re-entry is a single env flip. DO NOT REMOVE — dead
# code by design until Tier A returns as a paid tier.
_TIER_A_MULTIPLIER = 1.10


# cathedralai/cathedral#156: companion config the v2 scorer requires.
# An operator who flips CATHEDRAL_SCORER=v2 without also flipping
# CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true ends up silently scoring every
# eval as ``trace_bundle_missing`` because the v2 wire path is dormant
# while the v1 wire path keeps emitting under the old schema. The
# startup probe + per-call probe below catch this and surface it as a
# loud warning + a SCORER_MISCONFIG_GAUGE entry the validator runbook
# tells operators to alert on.
_V2_SCORER_COMPANION_FLAGS: tuple[str, ...] = ("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD",)


def _v2_scorer_selected(env: dict[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return values.get("CATHEDRAL_SCORER", "").lower() == "v2"


def _v2_companion_missing(env: dict[str, str] | None = None) -> list[str]:
    """Return the companion flag keys the v2 scorer needs but that are unset.

    Empty list means the configuration is consistent. A non-empty list is
    the misconfiguration signal: the gauge gets bumped for each missing
    key so operators see exactly what to flip.
    """
    values = os.environ if env is None else env
    missing: list[str] = []
    for key in _V2_SCORER_COMPANION_FLAGS:
        if values.get(key, "").lower() != "true":
            missing.append(key)
    return missing


def check_v2_scorer_activation(env: dict[str, str] | None = None) -> list[str]:
    """Loud startup probe for the v2 scorer activation flags.

    Call from validator/publisher process startup. Returns the missing
    companion-flag keys (empty when consistent). Emits a single
    ``scorer_v2_misconfigured`` warning per missing key and sets the
    ``SCORER_MISCONFIG_GAUGE`` so dashboards and the runbook alert can
    fire without waiting for the first eval to hit the per-call probe.
    The gauge is cleared for any companion that IS set so the alert
    auto-resolves when the operator fixes the flag.
    """
    values = os.environ if env is None else env
    if not _v2_scorer_selected(values):
        for key in _V2_SCORER_COMPANION_FLAGS:
            set_scorer_misconfig(key, 0)
        return []
    missing = _v2_companion_missing(values)
    for key in _V2_SCORER_COMPANION_FLAGS:
        set_scorer_misconfig(key, 1 if key in missing else 0)
    if missing:
        logger.warning(
            "scorer_v2_misconfigured",
            scorer="v2",
            missing_companions=missing,
            remediation=(
                "set CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true or unset "
                "CATHEDRAL_SCORER; see docs/validator/RUNBOOK.md"
            ),
        )
    return missing


# Re-export from v2_payload (no-publisher-cycle module) so existing
# imports work; the keyset is canonical there. Cross-branch contract
# with validator-compat's _SIGNED_KEYS_BY_VERSION (validator/pull_loop.py).
from cathedral.eval.v2_payload import _SIGNED_KEYS_BY_VERSION  # noqa: F401


@dataclass(frozen=True)
class ScoredEval:
    """Result the orchestrator persists into `eval_runs`."""

    eval_run_id: str
    output_card_json: dict[str, Any]
    output_card_hash: str
    score_parts: dict[str, Any]
    weighted_score: float
    weighted_score_pre_multiplier: float
    multiplier: float
    cathedral_signature: str
    errors: list[str]
    polaris_verified: bool = False
    polaris_attestation: dict[str, Any] | None = None


class EvalSigner:
    """Wraps `Ed25519PrivateKey` for signing eval-run records.

    Loaded from `CATHEDRAL_EVAL_SIGNING_KEY` (32-byte raw private key in
    hex), matching the Polaris convention (`POLARIS_CATHEDRAL_SIGNING_KEY`).
    """

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._sk = private_key

    @classmethod
    def from_env_hex(cls, hex_str: str) -> EvalSigner:
        try:
            raw = bytes.fromhex(hex_str.strip())
        except ValueError as e:
            raise ValueError("CATHEDRAL_EVAL_SIGNING_KEY must be hex") from e
        if len(raw) != 32:
            raise ValueError(f"signing key must be 32 bytes, got {len(raw)}")
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    def sign(self, eval_run_dict: dict[str, Any]) -> str:
        payload = canonical_json(eval_run_dict)
        return base64.b64encode(self._sk.sign(payload)).decode("ascii")


# Back-compat alias — _card_excerpt was inline here in PR 4 v1; moved
# to v2_payload.card_excerpt in v2 so cross-branch tests can import
# without the publisher cycle. Keep the alias for any existing internal
# callers; new code should import from cathedral.eval.v2_payload.
from cathedral.eval.v2_payload import card_excerpt as _card_excerpt  # noqa: F401


def card_hash(card: Card | dict[str, Any]) -> str:
    """`blake3(canonical_json(card))` — used as the leaf input.

    CRIT-8: must hash the EXACT bytes that the publisher serves (and stores
    in `eval_runs.output_card_json`). For dict input we hash the canonical
    serialization of the dict directly. For Card input (legacy callers /
    tests) we render via Pydantic; that path is NOT used by the live scoring
    pipeline — the live pipeline always passes the literal `output_card_json`
    dict so the hash matches `blake3(canonical_json(served_output_card))`.
    """
    if isinstance(card, Card):
        d = card.model_dump(by_alias=True, mode="json")
    else:
        d = card
    payload = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return blake3.blake3(payload).hexdigest()


async def score_and_sign(
    conn: aiosqlite.Connection,
    *,
    submission: dict[str, Any],
    epoch: int,
    round_index: int,
    polaris_agent_id: str,
    polaris_run_id: str,
    task_json: dict[str, Any],
    output_card_json: dict[str, Any],
    duration_ms: int,
    polaris_errors: list[str],
    registry: CardRegistry,
    signer: EvalSigner,
    polaris_attestation: dict[str, Any] | None = None,
    trace_json: dict[str, Any] | None = None,
    polaris_manifest: dict[str, Any] | None = None,
    published_artifact: Any | None = None,
) -> ScoredEval:
    """Run preflight + scorer, apply first-mover delta, build + sign EvalRun.

    Inserts the row into ``eval_runs`` and updates ``agent_submissions``
    rolling score + rank in a **single** database transaction (commits
    once at the end; rolls back if anything after the insert fails).
    Returns the persisted record for caller use.

    v1.1.0 (cathedralai/cathedral#75 PR 4): when
    ``published_artifact`` is supplied (an
    ``EvalArtifactPublisher.PublishedArtifact``) AND
    ``CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true``, the signed payload
    follows the v2 schema (``eval_card_excerpt`` +
    ``eval_artifact_manifest_hash`` instead of ``output_card`` +
    ``output_card_hash``) and the eval_run carries
    ``eval_output_schema_version=2``. Otherwise the v1 wire shape is
    emitted unchanged. The DB row always populates BOTH column
    families when the artifact is available so the env flag can be
    flipped at any time without re-running evals.
    """
    errors = list(polaris_errors)
    miner_hotkey = submission["miner_hotkey"]
    submission_id = submission["id"]
    card_id = submission["card_id"]

    # cathedralai/cathedral#75 PR 4 / #156: decide the eval-output
    # schema version up front so the v2 path can take its early-exit
    # branches (skip the v1-only first-mover delta, omit the v1 score
    # dimensions) before any v1-shaped state is built up.
    emit_v2 = (
        os.environ.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "").lower() == "true"
        and published_artifact is not None
    )
    schema_version = 2 if emit_v2 else 1

    # #156: per-call misconfiguration probe. ``CATHEDRAL_SCORER=v2``
    # was set explicitly by the operator, so they expect every row to
    # ride the v2 wire shape. If a row reaches scoring without the
    # companion flag or without a published artifact, surface it loud
    # and bump the gauge instead of silently scoring it as a zero-
    # weighted ``trace_bundle_missing``. The startup probe (see
    # ``check_v2_scorer_activation``) sets the env-level signal; this
    # block catches the runtime case where the bundle never lands.
    if _v2_scorer_selected() and not emit_v2:
        missing_companions = _v2_companion_missing()
        reason = "missing_companion_flag" if missing_companions else "trace_bundle_missing"
        for key in missing_companions:
            set_scorer_misconfig(key, 1)
        set_scorer_misconfig("trace_bundle_missing", 0 if published_artifact is not None else 1)
        logger.warning(
            "scorer_v2_misconfigured",
            scorer="v2",
            reason=reason,
            missing_companions=missing_companions,
            has_published_artifact=published_artifact is not None,
            submission_id=str(submission_id),
            remediation=(
                "set CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true and confirm "
                "the publisher emits a PublishedArtifact; see "
                "docs/validator/RUNBOOK.md"
            ),
        )

    # #156: durable v2 audit write happens before any of the scoring
    # work so the audit row survives a downstream rollback. Swallowed
    # failures bump SCORER_METRICS.AUDIT_FAILURE_COUNTER per exception
    # class; we keep going either way so the eval still gets scored.
    eval_run_id = str(uuid4())
    ran_at = datetime.now(UTC)
    ran_at_iso = _ms_iso(ran_at)
    if emit_v2:
        try:
            await repository.record_v2_audit(
                conn,
                audit_id=str(uuid4()),
                eval_run_id=eval_run_id,
                submission_id=str(submission_id),
                miner_hotkey=str(miner_hotkey),
                card_id=str(card_id),
                schema_version=schema_version,
                manifest_hash=getattr(published_artifact, "manifest_hash", None),
                bundle_url=getattr(published_artifact, "bundle_url", None),
                weighted_score_pre=None,
                written_at=ran_at,
            )
        except Exception as audit_exc:
            bump_audit_failure(type(audit_exc).__name__)
            logger.warning(
                "v2_audit_write_failed",
                eval_run_id=eval_run_id,
                submission_id=str(submission_id),
                exception_class=type(audit_exc).__name__,
                error=str(audit_exc),
            )

    # Card decode + preflight + score.
    # CONTRACTS §1.4: worker_owner_hotkey, polaris_agent_id, and id are
    # "filled by validator from claim" — they MUST come from server-trusted
    # state and OVERRIDE anything the bundle's agent emitted (CRIT-9). Use
    # unconditional assignment, never setdefault.
    raw_card = dict(output_card_json)
    raw_card["worker_owner_hotkey"] = miner_hotkey
    raw_card["polaris_agent_id"] = polaris_agent_id
    raw_card["id"] = card_id
    # Re-pin in the storage dict too so any downstream view of
    # output_card_json sees the trusted attribution.
    output_card_json = dict(output_card_json)
    output_card_json["worker_owner_hotkey"] = miner_hotkey
    output_card_json["polaris_agent_id"] = polaris_agent_id
    output_card_json["id"] = card_id

    weighted_pre = 0.0
    # #156: v1 rows carry the six-dimension breakdown; v2 rows do not.
    # On the v2 path we leave score_parts empty so the stored row does
    # not advertise misleading zero v1 dimensions that the v2 lane
    # never produced. The v1 path keeps the zero-default behaviour so
    # an early preflight/validation failure still shows the dimension
    # names (with zeros) consumers already expect.
    if emit_v2:
        score_dict: dict[str, Any] = {}
    else:
        score_dict = {
            "source_quality": 0.0,
            "freshness": 0.0,
            "specificity": 0.0,
            "usefulness": 0.0,
            "clarity": 0.0,
            "maintenance": 0.0,
        }
    try:
        card = Card.model_validate(raw_card)
    except (ValueError, TypeError) as e:
        errors.append(f"card validation: {e}")
        card = None  # type: ignore[assignment]

    if card is not None:
        try:
            preflight(card)
            entry: RegistryEntry | None = registry.lookup(card.id)
            parts = score_card(card, entry)
            score_dict = parts.model_dump()
            weighted_pre = parts.weighted()
            logger.info(
                "scoring_dimensions",
                submission_id=submission_id,
                card_id=card_id,
                weighted_pre=weighted_pre,
                parts=parts.model_dump(),
                entry_found=entry is not None,
            )
        except PreflightError as e:
            errors.append(f"preflight: {e}")
            logger.warning(
                "preflight_rejected",
                submission_id=submission_id,
                error=str(e),
            )
    else:
        logger.warning(
            "card_validation_failed",
            submission_id=submission_id,
            errors=[e for e in errors if "card validation" in e],
        )

    # #156: the first-mover delta is a v1-only concept — it lives on the
    # ``metadata_fingerprint`` similarity key that the v1 submit path
    # writes per Card+bundle. v2 rows come from the Polaris-native
    # deploy / Hermes pipeline and do not carry a meaningful
    # fingerprint, so the v1 multiplier query against
    # ``first_mover_for_fingerprint`` is undefined for them. Gate the
    # call here and emit a 1.0 pass-through so v2 weighted_score is
    # the raw preflight + scorer output.
    if emit_v2:
        multiplier = 1.0
    else:
        multiplier = await _first_mover_multiplier(
            conn,
            submission=submission,
            weighted_score_pre=weighted_pre,
        )
    weighted_after_first_mover = weighted_pre * multiplier

    # Verified-runtime multiplier per CONTRACTS.md §7.3 + Fred's Moltbook
    # decision: BYO-compute miners score normally. Miners whose eval ran
    # on Polaris and produced a verified Ed25519 attestation get a 1.10x
    # quality bonus. Capped at 1.0 afterwards so a top-tier BYO miner can
    # still hit the ceiling.
    #
    # Tier-A (Polaris-runtime) flow: the runner only returns a non-None
    # attestation after verifying the signature and re-deriving both the
    # task_hash and output_hash. A `polaris_attestation` dict here means
    # verification already succeeded; we just record it.
    #
    # Legacy flow (HttpPolarisRunner / stubs): no attestation, but
    # `polaris_agent_id` is non-empty when Polaris ran the work. Keep
    # that path eligible for the multiplier so existing stub tests
    # continue to assert the historical behaviour.
    # v2 Polaris-native deploys carry a verified manifest, NOT a per-task
    # attestation — the manifest is the unit of trust because Polaris
    # signs the deployment itself, not each /chat round trip. Treat a
    # non-None manifest as equivalent to a non-None attestation for the
    # purposes of the verified-runtime multiplier.
    polaris_verified = (
        polaris_attestation is not None or polaris_manifest is not None or bool(polaris_agent_id)
    )
    # cathedralai/cathedral#70: the 1.10x verified-runtime multiplier is
    # gated behind CATHEDRAL_ENABLE_POLARIS_DEPLOY. v1 ships with the flag
    # off so every scored run uses the raw weighted score.
    _tier_a_enabled = os.environ.get("CATHEDRAL_ENABLE_POLARIS_DEPLOY", "").lower() == "true"
    verified_multiplier = _TIER_A_MULTIPLIER if (polaris_verified and _tier_a_enabled) else 1.0
    weighted_final = min(1.0, weighted_after_first_mover * verified_multiplier)

    # CRIT-8: hash the literal `output_card_json` bytes that the publisher
    # both STORES (eval_runs.output_card_json) AND SERVES (EvalOutput.output_card).
    # Do NOT hash the Pydantic-rendered Card — Pydantic re-rendering applies
    # defaults/normalization and yields different bytes than what gets served,
    # making the hash externally unverifiable.
    output_card_hash = card_hash(output_card_json)

    # CONTRACTS.md §1.10 + §4.2 + L8 + tests/v1: the cathedral_signature
    # covers the public EvalOutput projection (the wire shape the validator
    # pull loop verifies). Signing over the projection means downstream
    # verifiers don't have to invert the public response back into the
    # storage row to verify. `output_card_hash` is included per L8 so the
    # frontend / validators can pin the byte-exact card the cathedral
    # signed. `merkle_epoch` is appended post-anchor and is EXCLUDED from
    # the signed bytes via `canonical_json` (see v1_types).
    #
    # TODO(v1.2.0, miner-side rewrite): once the new eval data model
    # lands (eval_card_excerpt / eval_artifact_manifest /
    # eval_artifact_bundle_url, per the Hermes re-alignment brief), add
    # `eval_output_schema_version` to this signed payload and bump it
    # to 2 here. The validator's verifier already dispatches on that
    # field via `_SIGNED_KEYS_BY_VERSION` in
    # cathedral.validator.pull_loop — register the new key set there
    # alongside this bump. Keep version=1 emission during the dual-write
    # cutover window so v1.1.0 validators continue verifying.
    display_name = submission.get("display_name", "")

    # v1.1.0 PR 4 + #156: ``emit_v2`` and ``schema_version`` were
    # decided at the top of this function so the v1-only first-mover
    # delta and the v1-only score dimensions could be skipped on v2
    # rows. Reuse them here without rebinding.

    # Build the v2 excerpt by default whenever the artifact bundle was
    # produced — the DB column lights up in BOTH schemas so the env
    # flag can be flipped without re-running evals. The excerpt is the
    # subset of fields the site renders. Cheap to compute; we already
    # have the dict.
    eval_card_excerpt: dict[str, Any] | None = None
    if published_artifact is not None:
        # Project the scored card down to the display surface. The
        # full card stays in output_card_json for legacy v1 reads
        # during the dual-publish window.
        eval_card_excerpt = _card_excerpt(output_card_json)

    if emit_v2:
        # v2 keyset (cathedralai/cathedral#75 my answer to Q2 on the
        # issue thread): drops output_card, output_card_hash,
        # polaris_verified. Adds eval_card_excerpt and
        # eval_artifact_manifest_hash. The schema_version field is
        # itself NOT in the signed bytes — it's a routing hint
        # validators use to pick the right keyset. Tampering with it
        # routes verification to a non-matching keyset and the sig
        # fails by mismatch on the underlying bytes (per the
        # validator's _SIGNED_KEYS_BY_VERSION docstring).
        public_payload: dict[str, Any] = {
            "id": eval_run_id,
            "agent_id": str(submission_id),
            "agent_display_name": display_name,
            "card_id": card_id,
            "eval_card_excerpt": eval_card_excerpt,
            "eval_artifact_manifest_hash": published_artifact.manifest_hash,
            "weighted_score": weighted_final,
            "ran_at": ran_at_iso,
        }
    else:
        # v1 keyset — unchanged from the legacy shape. CONTRACTS.md
        # §1.10 + §4.2 + L8. The signature covers the projection;
        # `merkle_epoch` is appended post-anchor and excluded.
        public_payload = {
            "id": eval_run_id,
            "agent_id": str(submission_id),
            "agent_display_name": display_name,
            "card_id": card_id,
            "output_card": output_card_json,
            "output_card_hash": output_card_hash,
            "weighted_score": weighted_final,
            "polaris_verified": polaris_verified,
            "ran_at": ran_at_iso,
        }
    signature = signer.sign(public_payload)

    # Single transaction for eval_runs INSERT + agent_submissions score/rank
    # UPDATE (cathedralai/cathedral#69). A crash between the two former
    # commits left a signed eval_run with a stale submission row.
    await repository.insert_eval_run(
        conn,
        id=eval_run_id,
        submission_id=str(submission_id),
        epoch=epoch,
        round_index=round_index,
        polaris_agent_id=polaris_agent_id,
        polaris_run_id=polaris_run_id,
        task_json=task_json,
        output_card_json=output_card_json,
        output_card_hash=output_card_hash,
        score_parts=score_dict,
        weighted_score=weighted_final,
        ran_at=ran_at,
        ran_at_iso=ran_at_iso,
        duration_ms=duration_ms,
        errors=errors if errors else None,
        cathedral_signature=signature,
        polaris_verified=polaris_verified,
        polaris_attestation=polaris_attestation,
        trace_json=trace_json,
        polaris_manifest=polaris_manifest,
        # Dual-publish: write the v2 column family on every eval where
        # the artifact was produced, regardless of which payload was
        # signed. Flipping CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD swaps the
        # wire emission without requiring a re-run.
        eval_card_excerpt=eval_card_excerpt,
        eval_artifact_manifest_hash=(
            published_artifact.manifest_hash if published_artifact is not None else None
        ),
        eval_artifact_bundle_url=(
            published_artifact.bundle_url if published_artifact is not None else None
        ),
        eval_artifact_manifest_url=(
            published_artifact.manifest_url if published_artifact is not None else None
        ),
        eval_output_schema_version=schema_version,
        commit=False,
    )
    try:
        # Update rolling 30-day average + rank
        avg = await repository.rolling_avg_score(conn, str(submission_id), days=30)
        if avg is None:
            avg = weighted_final
        # Exclude this submission from the rank query — its current_score row
        # still holds the PREVIOUS avg until update_submission_score below
        # commits the new one. Without exclusion, a cadence refresh that
        # lowers the score would count the row's own old higher score and
        # over-rank it by one. Pre-PR-#117 this was masked because the row
        # was flipped to status='evaluating' before this point (and the rank
        # query filters status='ranked'); cadence rows now stay 'ranked'.
        rank = await _compute_rank(conn, card_id, avg, exclude_submission_id=str(submission_id))
        await repository.update_submission_score(
            conn,
            str(submission_id),
            current_score=avg,
            current_rank=rank,
            commit=False,
        )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    logger.info(
        "eval_run_persisted",
        eval_run_id=eval_run_id,
        submission_id=submission_id,
        weighted=weighted_final,
        multiplier=multiplier,
        rank=rank,
    )

    return ScoredEval(
        eval_run_id=eval_run_id,
        output_card_json=output_card_json,
        output_card_hash=output_card_hash,
        score_parts=score_dict,
        weighted_score=weighted_final,
        weighted_score_pre_multiplier=weighted_pre,
        multiplier=multiplier,
        cathedral_signature=signature,
        errors=errors,
        polaris_verified=polaris_verified,
        polaris_attestation=polaris_attestation,
    )


# --------------------------------------------------------------------------
# First-mover helpers
# --------------------------------------------------------------------------


async def _first_mover_multiplier(
    conn: aiosqlite.Connection,
    *,
    submission: dict[str, Any],
    weighted_score_pre: float,
) -> float:
    fingerprint = submission["metadata_fingerprint"]
    card_id = submission["card_id"]
    submission_id = submission["id"]

    first = await repository.first_mover_for_fingerprint(conn, card_id, fingerprint)
    is_first = first is None or str(first["id"]) == str(submission_id)

    submitted_at = _parse_iso(submission.get("submitted_at"))
    incumbent_best = await repository.incumbent_best_score(conn, card_id, submitted_at) or 0.0
    first_mover_at = _parse_iso(
        first.get("first_mover_at") if first else submission.get("first_mover_at")
    )
    days_since = (datetime.now(UTC) - first_mover_at).days

    return first_mover_multiplier(
        is_first_mover=is_first,
        weighted_score=weighted_score_pre,
        incumbent_best_weighted=incumbent_best,
        days_since_first=days_since,
    )


async def _compute_rank(
    conn: aiosqlite.Connection,
    card_id: str,
    my_score: float,
    *,
    exclude_submission_id: str | None = None,
) -> int:
    """1-indexed rank within `card_id` by current_score DESC.

    `exclude_submission_id`, when set, drops the row with that id from
    the COUNT — the caller is about to overwrite that row's
    current_score and is asking "where will I rank against everyone
    else's current score?". Without this, a cadence-refresh row whose
    score is dropping would count its own pre-write higher score.
    """
    if exclude_submission_id is None:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM agent_submissions "
            "WHERE card_id = ? AND status='ranked' AND current_score > ?",
            (card_id, my_score),
        )
    else:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM agent_submissions "
            "WHERE card_id = ? AND status='ranked' AND current_score > ? "
            "AND id != ?",
            (card_id, my_score, exclude_submission_id),
        )
    row = await cur.fetchone()
    higher = int(row[0]) if row else 0
    return higher + 1


def _parse_iso(s: str | datetime | None) -> datetime:
    if s is None:
        return datetime.now(UTC)
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(UTC)


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, ms precision, trailing Z (CONTRACTS.md §9 lock #6)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


__all__ = [
    "EvalSigner",
    "ScoredEval",
    "card_hash",
    "score_and_sign",
]


def _unused(_: timedelta) -> None:
    """Keep `timedelta` import used for type clarity above; harmless."""
    return None
