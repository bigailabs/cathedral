"""Public GET endpoints surviving the PR2 card-era purge.

Card-era reads (`/v1/agents`, `/v1/agents/{id}`, `/v1/cards/{id}/*`,
`/v1/discovery/recent`, `/v1/merkle/{epoch}`, `/v1/miners/{hotkey}/agents`)
have been removed; the publisher now exposes only the surfaces that the
SAT lane needs:

  - GET /v1/leaderboard           — SAT leaderboard (filtered by card param)
  - GET /v1/leaderboard/recent    — cross-card recent EvalRun feed (validator
                                    pull loop)
  - GET /health                   — infra health probe

The 12 dead routes are stubbed elsewhere (see
``cathedral.publisher.dead_routes``) returning HTTP 410 with a pointer to
``https://cathedral.computer/skill.md``.

All responses match the wire shapes in CONTRACTS.md.
Errors always render as ``{"detail": "<string>"}`` (Section 9 lock #3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status

from cathedral.lanes.publisher import task_family_feed_enabled
from cathedral.publisher import repository
from cathedral.v3.publisher import v3_feed_enabled

if TYPE_CHECKING:
    from cathedral.publisher.app import PublisherContext

logger = structlog.get_logger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


async def get_active_card_definition_or_404(db: Any, card_id: str) -> dict[str, Any]:
    """Look up a card_definitions row by id; 404 if missing OR archived.

    Retained because ``/v1/leaderboard`` still validates the ``card`` query
    parameter against ``card_definitions`` (the leaderboard is per-card and
    a typo should 404 rather than render an empty page).
    """
    card_def = await repository.get_card_definition(db, card_id)
    if card_def is None:
        raise HTTPException(status_code=404, detail="card not found")
    if card_def.get("status") != "active":
        raise HTTPException(status_code=404, detail=f"card not active: {card_id}")
    return card_def


# --------------------------------------------------------------------------
# 2.8 GET /v1/leaderboard
# --------------------------------------------------------------------------


@router.get("/v1/leaderboard")
async def get_leaderboard(
    request: Request,
    card: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    if not card:
        raise HTTPException(status_code=400, detail="card parameter required")
    await get_active_card_definition_or_404(ctx.db, card)
    # Pull more than `limit` because we dedupe by hotkey below — a single
    # mason can have submitted many bundles, only their best should occupy
    # a leaderboard slot. Cap at the repository's ceiling (200) so a wide
    # field of repeat submitters still narrows down to `limit` unique
    # masons.
    submissions = await repository.list_submissions_for_card(
        ctx.db, card, sort="score", limit=200, offset=0
    )
    items = _dedupe_leaderboard_by_hotkey(submissions, limit=limit)
    return {"items": items, "computed_at": _now_iso()}


# --------------------------------------------------------------------------
# 2.9 GET /v1/leaderboard/recent
# --------------------------------------------------------------------------


@router.get("/v1/leaderboard/recent")
async def get_leaderboard_recent(
    request: Request,
    since: str | None = Query(default=None),
    since_ran_at: str | None = Query(default=None),
    since_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Cross-card recent eval feed for the validator pull loop.

    v1.1.0 introduces a tuple-shaped cursor ``(since_ran_at, since_id)``
    with strict `>` row-value comparison, replacing the v1.0.x
    ``since=<ran_at>`` cursor with ``>=`` comparison. The new cursor is
    a total order on ``(ran_at, id)`` so it cannot drop or duplicate
    rows at millisecond collisions — the failure mode the cadence eval
    load surfaced. See ``2026-05-12-track-3-pull-cursor-audit.md``
    Risk 2.

    Back-compat: v1.0.7 validators only send ``since`` (no
    ``since_id``). We **distinguish "not sent" from "sent empty"** —
    FastAPI's ``Query(default=None)`` leaves ``since_id is None`` when
    the client omitted the param, and ``since_id == ""`` when the
    client sent it with an empty value. The two cases route to
    different SQL predicates:

    * ``since_id is None`` (legacy): ``WHERE ran_at > ?`` — strict `>`
      on the single timestamp, no tuple comparison.
    * ``since_id`` is a string (v1.1.0 tuple cursor, even ``""``):
      ``WHERE (ran_at, id) > (?, ?)``.

    Response: dual-emits both legacy ``next_since`` and the v1.1.0
    pair ``next_since_ran_at`` + ``next_since_id``. Old validators
    read the former; new validators read the latter.
    """
    ctx: PublisherContext = request.app.state.ctx

    # Resolve cursor source: prefer v1.1.0 tuple if present, else legacy.
    if since_ran_at is not None:
        cursor_ran_at = since_ran_at
    elif since is not None:
        cursor_ran_at = since
    else:
        raise HTTPException(
            status_code=400,
            detail="missing cursor: pass since_ran_at (v1.1.0) or since (v1.0.x)",
        )

    since_dt = _parse_since(cursor_ran_at)
    if since_dt is None:
        raise HTTPException(
            status_code=400,
            detail="since / since_ran_at must be ISO-8601",
        )

    if since_ran_at is not None:
        cursor_id: str | None = since_id or ""
    else:
        cursor_id = since_id

    include_v3 = v3_feed_enabled()
    include_task_families = task_family_feed_enabled()
    runs = await repository.list_eval_runs_recent(
        ctx.db,
        since=since_dt,
        since_id=cursor_id,
        limit=limit,
        include_v3=include_v3,
        include_task_families=include_task_families,
    )
    items: list[dict[str, Any]] = []
    for r in runs:
        sub = await repository.get_agent_submission(ctx.db, r["submission_id"])
        if sub:
            items.append(_eval_run_to_output(r, sub))

    if len(items) == limit and runs:
        last_row = runs[-1]
        next_since_ran_at = items[-1]["ran_at"]
        next_since_id = str(last_row.get("id") or "")
        next_since_legacy: str | None = items[-1]["ran_at"]
    else:
        next_since_ran_at = None
        next_since_id = None
        next_since_legacy = None

    latest_epoch = await repository.latest_merkle_epoch(ctx.db)
    return {
        "items": items,
        # Legacy cursor for v1.0.x validators.
        "next_since": next_since_legacy,
        # v1.1.0 tuple cursor for the audited pull loop.
        "next_since_ran_at": next_since_ran_at,
        "next_since_id": next_since_id,
        "merkle_epoch_latest": latest_epoch,
    }


# --------------------------------------------------------------------------
# 2.12 GET /health
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# GET /v1/miners/{hotkey}/submissions  — miner-facing status feed
# --------------------------------------------------------------------------


@router.get("/v1/miners/{hotkey}/submissions")
async def get_miner_submissions(
    hotkey: str,
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Return the most recent submissions for a miner hotkey, enriched with
    eval-run status where available.

    Unknown hotkeys return ``count: 0`` with an empty ``items`` list —
    never a 4xx, because miners poll this before their first submission
    lands and an error would be confusing.

    Each item exposes:
    - ``challenge_id``       — from the eval-run task_json when present
    - ``tier``               — difficulty_tier from task_json, or null
    - ``status``             — the submission's current status string
    - ``rejection_reason``   — from the submission row, or null
    - ``weighted_score``     — from the most-recent eval run, or null
    - ``solve_rank``         — from the eval-run task_json, or null
    - ``ran_at``             — timestamp of the most-recent eval run, or null
    - ``created_at``         — submission's submitted_at timestamp
    """
    ctx: PublisherContext = request.app.state.ctx

    # Fetch all submissions for this hotkey then slice to `limit`.
    # list_submissions_by_hotkey returns newest-first (ORDER BY submitted_at DESC).
    all_subs = await repository.list_submissions_by_hotkey(ctx.db, hotkey)
    subs = all_subs[:limit]

    items: list[dict[str, Any]] = []
    for sub in subs:
        # Fetch the most recent eval run for this submission (limit=1).
        runs = await repository.list_eval_runs_for_submission(ctx.db, sub["id"], limit=1)
        latest_run = runs[0] if runs else None

        # Extract fields from the eval run's task_json when present.
        task_json: dict[str, Any] = {}
        output_json: dict[str, Any] = {}
        if latest_run:
            raw_task = latest_run.get("task_json")
            task_json = raw_task if isinstance(raw_task, dict) else {}
            raw_output = latest_run.get("output_card_json")
            output_json = raw_output if isinstance(raw_output, dict) else {}

        # Derive challenge_id: prefer eval task_json, fall back to
        # card_id (the task-family discriminator on the submission itself).
        challenge_id = (
            task_json.get("challenge_id")
            or task_json.get("task_id")
            or output_json.get("challenge_id")
            or sub.get("card_id")
        )

        tier = task_json.get("difficulty_tier") if task_json else None

        rejection_reason = (
            sub.get("rejection_reason")
            or (output_json.get("rejection_reason") if output_json else None)
        )

        items.append(
            {
                "submission_id": sub["id"],
                "challenge_id": challenge_id,
                "tier": tier,
                "status": sub.get("status"),
                "rejection_reason": rejection_reason,
                "weighted_score": latest_run["weighted_score"] if latest_run else None,
                "solve_rank": task_json.get("solve_rank") if task_json else None,
                "ran_at": latest_run["ran_at"] if latest_run else None,
                "created_at": sub.get("submitted_at"),
            }
        )

    return {"hotkey": hotkey, "count": len(items), "items": items}


@router.get("/health")
async def get_health(request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    checks: dict[str, str] = {"db": "ok"}
    try:
        cur = await ctx.db.execute("SELECT 1")
        await cur.fetchone()
    except Exception:
        checks["db"] = "fail"

    # Storage check: weak by design. We probe whether the client is
    # constructed (== env vars are set), not whether HeadBucket succeeds.
    if ctx.hippius is not None:
        try:
            healthy = await ctx.hippius.healthcheck()
            checks["hippius"] = "ok" if healthy else "degraded"
        except Exception:
            checks["hippius"] = "degraded"
    else:
        checks["hippius"] = "skipped"

    checks["polaris"] = "ok"  # publisher does not poll Polaris directly

    fatal = checks["db"] != "ok"
    payload = {
        "status": "ok" if all(v in ("ok", "skipped") for v in checks.values()) else "degraded",
        "checks": checks,
    }
    if fatal:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=payload,
        )
    return payload


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _eval_run_to_output(run: dict[str, Any], sub: dict[str, Any]) -> dict[str, Any]:
    """Build the public EvalOutput projection (CONTRACTS.md §1.10 + L8).

    Keeps the multi-schema projection so ``/v1/leaderboard/recent`` returns
    schema-3 / schema-5 task-family rows correctly to the validator pull
    loop. Schema-1 / schema-2 (card-era) projections are retained because
    legacy rows may still live in the DB even after the card-era write
    paths are gone.
    """
    schema_version = int(run.get("eval_output_schema_version") or 1)
    if schema_version == 3:
        task_json = run.get("task_json") or {}
        output = run.get("output_card_json") or {}
        return {
            "id": run["id"],
            "agent_id": sub["id"],
            "agent_display_name": sub["display_name"],
            "miner_hotkey": sub["miner_hotkey"],
            "task_type": "bug_isolation_v1",
            "challenge_id": task_json.get("challenge_id"),
            "challenge_id_public": task_json.get("challenge_id_public")
            or output.get("challenge_id_public"),
            "epoch_salt": task_json.get("epoch_salt"),
            "weighted_score": run["weighted_score"],
            "score_parts": run["score_parts"],
            "claim": output.get("claim"),
            "ran_at": run["ran_at"],
            "eval_output_schema_version": 3,
            "cathedral_signature": run["cathedral_signature"],
            "failure_reason": output.get("failure_reason"),
            "merkle_epoch": run.get("merkle_epoch"),
        }
    if schema_version == 5:
        task_json = run.get("task_json") or {}
        output = run.get("output_card_json") or {}
        return {
            "id": run["id"],
            "agent_id": sub["id"],
            "agent_display_name": sub["display_name"],
            "miner_hotkey": sub["miner_hotkey"],
            "task_type": task_json.get("task_type") or output.get("task_type"),
            "task_id_public": task_json.get("task_id_public") or output.get("task_id_public"),
            "epoch_salt": task_json.get("epoch_salt"),
            "difficulty_tier": task_json.get("difficulty_tier")
            if "difficulty_tier" in task_json
            else output.get("difficulty_tier"),
            "weighted_score": run["weighted_score"],
            "score_parts": run["score_parts"],
            "answer_hash": task_json.get("answer_hash") or output.get("answer_hash"),
            "verifier_details_hash": task_json.get("verifier_details_hash")
            or output.get("verifier_details_hash"),
            "rejection_reason": output.get("rejection_reason"),
            "ran_at": run["ran_at"],
            "eval_output_schema_version": 5,
            "cathedral_signature": run["cathedral_signature"],
            "merkle_epoch": run.get("merkle_epoch"),
        }
    if schema_version == 2:
        excerpt_raw = run.get("eval_card_excerpt")
        if isinstance(excerpt_raw, str):
            try:
                eval_card_excerpt = json.loads(excerpt_raw)
            except (json.JSONDecodeError, TypeError):
                eval_card_excerpt = None
        else:
            eval_card_excerpt = excerpt_raw
        return {
            "id": run["id"],
            "agent_id": sub["id"],
            "agent_display_name": sub["display_name"],
            "card_id": sub["card_id"],
            "eval_card_excerpt": eval_card_excerpt,
            "eval_artifact_manifest_hash": run.get("eval_artifact_manifest_hash"),
            "weighted_score": run["weighted_score"],
            "ran_at": run["ran_at"],
            "eval_output_schema_version": 2,
            "cathedral_signature": run["cathedral_signature"],
            "eval_artifact_bundle_url": run.get("eval_artifact_bundle_url"),
            "eval_artifact_manifest_url": run.get("eval_artifact_manifest_url"),
            "merkle_epoch": run.get("merkle_epoch"),
        }
    return {
        "id": run["id"],
        "agent_id": sub["id"],
        "agent_display_name": sub["display_name"],
        "card_id": sub["card_id"],
        "output_card": run["output_card_json"],
        "output_card_hash": run["output_card_hash"],
        "weighted_score": run["weighted_score"],
        "polaris_verified": bool(run.get("polaris_verified", 0)),
        "polaris_attestation": run.get("polaris_attestation"),
        "ran_at": run["ran_at"],
        "cathedral_signature": run["cathedral_signature"],
        "merkle_epoch": run.get("merkle_epoch"),
    }


def _dedupe_leaderboard_by_hotkey(
    submissions: list[dict[str, Any]], *, limit: int
) -> list[dict[str, Any]]:
    """Collapse repeat submissions from the same miner to one entry.

    Each Bittensor hotkey is one mason. A mason who submits the same
    bundle five times — or five different bundles, one after the other —
    should occupy one leaderboard slot, representing their best scored
    card.
    """
    seen_hotkeys: set[str] = set()
    items: list[dict[str, Any]] = []
    for s in submissions:
        if s["current_score"] is None:
            continue
        if s.get("status") != "ranked":
            continue
        hk = s["miner_hotkey"]
        if hk in seen_hotkeys:
            continue
        seen_hotkeys.add(hk)
        items.append(_submission_to_leaderboard_entry(s))
        if len(items) >= limit:
            break
    return items


def _submission_to_leaderboard_entry(sub: dict[str, Any]) -> dict[str, Any]:
    last_eval_at = sub.get("latest_eval_at") or sub.get("submitted_at") or _now_iso()
    return {
        "agent_id": sub["id"],
        "display_name": sub["display_name"],
        "logo_url": sub["logo_url"],
        "miner_hotkey": sub["miner_hotkey"],
        "card_id": sub["card_id"],
        "current_score": sub["current_score"] or 0.0,
        "current_rank": sub["current_rank"] or 0,
        "last_eval_at": last_eval_at,
    }


def _parse_since(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
