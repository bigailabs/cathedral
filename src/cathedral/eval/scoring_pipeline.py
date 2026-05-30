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

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import aiosqlite
import blake3
import structlog

from cathedral.cards import preflight, score_card
from cathedral.cards.preflight import PreflightError
from cathedral.cards.registry import CardRegistry, RegistryEntry
from cathedral.eval.eval_signer import EvalSigner
from cathedral.eval.scoring import (
    FIRST_MOVER_DELTA,
    FIRST_MOVER_PENALTY_MULTIPLIER,
    FIRST_MOVER_WINDOW_DAYS,
    first_mover_multiplier,
)
from cathedral.eval.v2_payload import _SIGNED_KEYS_BY_VERSION  # noqa: F401
from cathedral.eval.v2_payload import card_excerpt as _card_excerpt
from cathedral.types import Card

logger = structlog.get_logger(__name__)


_FIRST_MOVER_WINDOW_DAYS = FIRST_MOVER_WINDOW_DAYS
_FIRST_MOVER_DELTA_THRESHOLD = FIRST_MOVER_DELTA
_FIRST_MOVER_PENALTY_MULTIPLIER = FIRST_MOVER_PENALTY_MULTIPLIER

# Verified-runtime multiplier for Tier A (Polaris-hosted, attested) runs.
# cathedralai/cathedral#70: gated behind CATHEDRAL_ENABLE_POLARIS_DEPLOY;
# kept in source so re-entry is a single env flip. DO NOT REMOVE — dead
# code by design until Tier A returns as a paid tier.
_TIER_A_MULTIPLIER = 1.10


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


# EvalSigner moved to cathedral.eval.eval_signer (WS7) so the live SAT signing
# path does not depend on this card-scoring module. Re-exported above for
# back-compat with card-era importers that are being removed.


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
    score_dict: dict[str, Any] = {
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

    # First-mover delta lookup
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
    import os as _os

    _tier_a_enabled = _os.environ.get("CATHEDRAL_ENABLE_POLARIS_DEPLOY", "").lower() == "true"
    verified_multiplier = _TIER_A_MULTIPLIER if (polaris_verified and _tier_a_enabled) else 1.0
    weighted_final = min(1.0, weighted_after_first_mover * verified_multiplier)

    # Operator kill switch: when CATHEDRAL_ZERO_ALL_SCORES=true, every signed
    # row goes out with weighted_score=0.0. Validators average pulled scores
    # in a rolling window; a sustained stream of zeros collapses every
    # miner's average to 0, which in chain/weights.apply_burn falls into the
    # "no positive scores" branch and routes 100% of weight to burn_uid. This
    # is the publisher-side equivalent of forced_burn_percentage=100.0 but
    # requires zero validator updates and works against the full fleet,
    # including operators on stale tags. Flip back to false to restore
    # normal scoring on next eval cycle.
    _zero_all_scores = _os.environ.get("CATHEDRAL_ZERO_ALL_SCORES", "").lower() == "true"
    if _zero_all_scores:
        logger.info(
            "score_killswitch_engaged",
            submission_id=submission_id,
            card_id=card_id,
            weighted_pre=weighted_pre,
            weighted_final_before_killswitch=weighted_final,
            reason="CATHEDRAL_ZERO_ALL_SCORES=true",
        )
        weighted_final = 0.0
        weighted_after_first_mover = 0.0
        weighted_pre = 0.0

    # CRIT-8: hash the literal `output_card_json` bytes that the publisher
    # both STORES (eval_runs.output_card_json) AND SERVES (EvalOutput.output_card).
    # Do NOT hash the Pydantic-rendered Card — Pydantic re-rendering applies
    # defaults/normalization and yields different bytes than what gets served,
    # making the hash externally unverifiable.
    output_card_hash = card_hash(output_card_json)
    eval_run_id = str(uuid4())
    ran_at = datetime.now(UTC)
    ran_at_iso = _ms_iso(ran_at)

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

    # v1.1.0 PR 4: eval-output schema version dispatch.
    # CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true tells the publisher to
    # emit the new shape. Validators dispatch per-record via the
    # `eval_output_schema_version` field on the wire (see
    # validator/pull_loop.py _SIGNED_KEYS_BY_VERSION).
    import os as _os

    emit_v2 = (
        _os.environ.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "").lower() == "true"
        and published_artifact is not None
    )
    schema_version = 2 if emit_v2 else 1

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
    from cathedral.publisher import repository

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

    from cathedral.publisher import repository

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
