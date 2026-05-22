"""Public GET endpoints (CONTRACTS.md Section 2.2 - 2.11).

All responses match the wire shapes in Section 1 / Section 2 exactly.
Errors always render as `{"detail": "<string>"}` (Section 9 lock #3).
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

    Mirrors the submit gate at ``publisher/submit.py`` so every public
    ``/v1/cards/{card_id}/*`` surface treats archived cards as invisible.
    Without this, archived launch-plan cards keep advertising history,
    discovery, feed, attempts, and eval-spec content even though new
    submits are rejected, which leads miners and the site to build
    against cards they cannot actually submit to.

    Returns the card_def dict on success. Raises HTTPException(404)
    otherwise; callers do not need to add their own status check.
    """
    card_def = await repository.get_card_definition(db, card_id)
    if card_def is None:
        raise HTTPException(status_code=404, detail="card not found")
    if card_def.get("status") != "active":
        raise HTTPException(status_code=404, detail=f"card not active: {card_id}")
    return card_def


# --------------------------------------------------------------------------
# 2.2 GET /v1/agents/{id}
# --------------------------------------------------------------------------


@router.get("/v1/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    sub = await repository.get_agent_submission(ctx.db, agent_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="agent not found")

    runs = await repository.list_eval_runs_for_submission(ctx.db, agent_id, limit=20)
    score_history = [{"date": r["ran_at"], "score": r["weighted_score"]} for r in reversed(runs)]
    return {
        "id": sub["id"],
        "display_name": sub["display_name"],
        "bio": sub["bio"],
        "logo_url": sub["logo_url"],
        "miner_hotkey": sub["miner_hotkey"],
        "card_id": sub["card_id"],
        "bundle_hash": sub["bundle_hash"],
        "bundle_size_bytes": sub["bundle_size_bytes"],
        "status": sub["status"],
        "current_score": sub["current_score"],
        "current_rank": sub["current_rank"],
        "submitted_at": sub["submitted_at"],
        # attestation_mode is exposed so the frontend can branch the
        # agent profile UI: verified (polaris/tee) shows score/rank/
        # eval history; unverified shows the discovery banner.
        "attestation_mode": sub.get("attestation_mode", "polaris"),
        "recent_evals": [_eval_run_to_output(r, sub) for r in runs],
        "score_history": score_history,
    }


# --------------------------------------------------------------------------
# 2.3 GET /v1/agents
# --------------------------------------------------------------------------


@router.get("/v1/agents")
async def list_agents(
    request: Request,
    card: str | None = Query(default=None),
    sort: str = Query(default="score"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    if sort not in ("score", "recent", "oldest"):
        raise HTTPException(status_code=400, detail=f"invalid sort: {sort}")
    if card:
        items = await repository.list_submissions_for_card(
            ctx.db, card, sort=sort, limit=limit, offset=offset
        )
        # No COUNT(*) for filtered query — return len; UI uses next-page
        # heuristic on the `items` length anyway.
        total = len(items)
    else:
        items, total = await repository.list_submissions_all(
            ctx.db, sort=sort, limit=limit, offset=offset
        )
    return {
        "items": [
            _submission_to_leaderboard_entry(s)
            for s in items
            if s["current_score"] is not None and s.get("status") == "ranked"
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --------------------------------------------------------------------------
# 2.4 GET /v1/cards/{card_id}
# --------------------------------------------------------------------------


@router.get("/v1/cards/{card_id}")
async def get_card_summary(card_id: str, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    card_def = await get_active_card_definition_or_404(ctx.db, card_id)

    # `best_eval` and `agent_count` are both verified-surface only: the
    # public card overview should reflect only attested agents. Discovery
    # rows are surfaced separately at `/v1/cards/{id}/discovery`.
    best = await repository.best_eval_run_for_card(ctx.db, card_id)
    agent_count = await repository.count_verified_agents_for_card(ctx.db, card_id)
    latest = max(
        (r["ran_at"] for r in await repository.list_eval_runs_for_card(ctx.db, card_id, limit=1)),
        default=None,
    )

    best_eval_output = None
    if best is not None:
        owner_sub = await repository.get_agent_submission(ctx.db, best["submission_id"])
        if owner_sub:
            best_eval_output = _eval_run_to_output(best, owner_sub)

    return {
        "card_id": card_def["id"],
        "best_eval": best_eval_output,
        "definition": {
            "id": card_def["id"],
            "display_name": card_def["display_name"],
            "jurisdiction": card_def["jurisdiction"],
            "topic": card_def["topic"],
            "description": card_def["description"],
            "status": card_def["status"],
        },
        "agent_count": agent_count,
        "latest_eval_at": latest,
    }


# --------------------------------------------------------------------------
# 2.5 GET /v1/cards/{card_id}/history
# --------------------------------------------------------------------------


@router.get("/v1/cards/{card_id}/history")
async def get_card_history(
    card_id: str,
    request: Request,
    agent_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    since: str | None = Query(default=None),
) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    await get_active_card_definition_or_404(ctx.db, card_id)
    since_dt = _parse_since(since)

    if agent_id:
        runs = await repository.list_eval_runs_for_submission(ctx.db, agent_id, limit=limit)
        if since_dt:
            since_iso = since_dt.isoformat()
            runs = [r for r in runs if r.get("ran_at") and r["ran_at"] >= since_iso]
    else:
        runs = await repository.list_eval_runs_for_card(
            ctx.db, card_id, since=since_dt, limit=limit
        )

    items: list[dict[str, Any]] = []
    for r in runs:
        sub = await repository.get_agent_submission(ctx.db, r["submission_id"])
        if sub:
            items.append(_eval_run_to_output(r, sub))
    next_since = items[-1]["ran_at"] if len(items) == limit else None
    return {"items": items, "next_since": next_since}


# --------------------------------------------------------------------------
# 2.6 GET /v1/cards/{card_id}/eval-spec
# --------------------------------------------------------------------------


@router.get("/v1/cards/{card_id}/eval-spec")
async def get_card_eval_spec(card_id: str, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    card_def = await get_active_card_definition_or_404(ctx.db, card_id)
    return {
        "card_id": card_def["id"],
        "display_name": card_def["display_name"],
        "jurisdiction": card_def["jurisdiction"],
        "description_md": card_def["description"],
        "eval_spec_md": card_def["eval_spec_md"],
        "scoring_rubric": card_def["scoring_rubric"],
        "task_templates": card_def["task_templates"],
        "source_pool": card_def["source_pool"],
        "refresh_cadence_hours": card_def["refresh_cadence_hours"],
    }


# --------------------------------------------------------------------------
# Discovery surface — unverified submissions
# --------------------------------------------------------------------------
#
# The leaderboard (above) is the verified surface: only `attestation_mode`
# in `('polaris','tee')` rows. Discovery rows live here on a separate axis:
#
#   GET /v1/cards/{id}/discovery        — per-card unverified list
#   GET /v1/cards/{id}/discovery/count  — cheap count for tile/header
#   GET /v1/discovery/recent            — cross-card feed for /research
#
# Discovery rows have `attestation_mode='unverified'` and `status='discovery'`.
# They never enter the eval queue, never accrue scores, never earn TAO.
# They exist for browsing, citation, and acquisition discovery.


@router.get("/v1/cards/{card_id}/discovery")
async def get_card_discovery(
    card_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    await get_active_card_definition_or_404(ctx.db, card_id)
    items = await repository.list_discovery_submissions_for_card(
        ctx.db, card_id, limit=limit, offset=offset
    )
    total = await repository.count_discovery_submissions_for_card(ctx.db, card_id)
    return {
        "items": [_submission_to_discovery_item(s) for s in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/v1/cards/{card_id}/discovery/count")
async def get_card_discovery_count(card_id: str, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    await get_active_card_definition_or_404(ctx.db, card_id)
    total = await repository.count_discovery_submissions_for_card(ctx.db, card_id)
    return {"total": total}


@router.get("/v1/discovery/recent")
async def get_discovery_recent(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Cross-card discovery feed used by the top-level /research page."""
    ctx: PublisherContext = request.app.state.ctx
    items = await repository.list_discovery_submissions_recent(ctx.db, limit=limit, offset=offset)
    return {
        "items": [_submission_to_discovery_item(s) for s in items],
        "limit": limit,
        "offset": offset,
    }


# --------------------------------------------------------------------------
# 2.7 GET /v1/cards/{card_id}/feed
# --------------------------------------------------------------------------


@router.get("/v1/cards/{card_id}/feed")
async def get_card_feed(
    card_id: str,
    request: Request,
    since: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    await get_active_card_definition_or_404(ctx.db, card_id)
    since_dt = _parse_since(since)
    runs = await repository.list_eval_runs_for_card(ctx.db, card_id, since=since_dt, limit=limit)
    items: list[dict[str, Any]] = []
    for r in runs:
        sub = await repository.get_agent_submission(ctx.db, r["submission_id"])
        if sub:
            items.append(_eval_run_to_output(r, sub))
    next_since = items[-1]["ran_at"] if len(items) == limit else None
    return {"items": items, "next_since": next_since}


# --------------------------------------------------------------------------
# v1.1.4 GET /v1/cards/{card_id}/attempts — public failed-evals feed
# --------------------------------------------------------------------------


@router.get("/v1/cards/{card_id}/attempts")
async def get_card_attempts(
    card_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """Recent eval_runs for a card INCLUDING failed attempts.

    Counterpart to ``GET /v1/cards/{card_id}/feed`` which is the
    score-leaderboard surface. ``/attempts`` returns the same rows
    PLUS failed ones (``_ssh_hermes_failed=true`` or
    ``weighted_score=0``) so the site's empty-state design (PR #119)
    can show real network activity even before any card scores above
    zero. Ordered ``ran_at DESC``, default 20 rows.

    Per-row shape matches the existing EvalOutput projection used by
    ``/feed`` so site renderers stay aligned. The ``miner_hotkey``
    field is added on top so the site can attribute failed attempts.
    """
    ctx: PublisherContext = request.app.state.ctx
    await get_active_card_definition_or_404(ctx.db, card_id)
    runs = await repository.list_attempts_for_card(ctx.db, card_id, limit=limit)
    items: list[dict[str, Any]] = []
    for r in runs:
        sub = await repository.get_agent_submission(ctx.db, r["submission_id"])
        if sub:
            item = _eval_run_to_output(r, sub)
            # /attempts adds miner_hotkey so the site can attribute
            # failed attempts. Existing /feed renderers ignore unknown
            # fields, so adding it here is forward-compat.
            item["miner_hotkey"] = sub["miner_hotkey"]
            items.append(item)
    return {"items": items, "limit": limit}


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
      on the single timestamp, no tuple comparison. Matches what
      v1.0.7's stateless single-string cursor can actually express.
      Originally the code coerced ``since_id`` to ``""`` and ran the
      tuple comparison ``(ran_at, id) > (since, '')``; because every
      non-empty UUID is ``> ''``, this re-included every row at the
      boundary millisecond on every pull, and the v1.0.7 cursor
      (advanced to ``items[-1].ran_at``) never escaped. Strict `>`
      cleanly advances past the boundary timestamp once it's reached
      — at the cost of skipping any rows whose ``ran_at`` exactly
      equals the cursor, which is fine for normal traffic (UPSERT
      dedupe covered the v1.0.7 case where the boundary row was
      re-pulled; with strict `>` there's nothing to dedupe) but
      cannot drain a >limit ms-collision burst from a v1.0.7
      validator. That residual is unsolvable for a stateless
      single-string cursor and is the documented operational risk
      during the deploy-day window before the v1.0.7 fleet
      PM2-cycles to v1.1.0 — see Test 4 in
      ``tests/smoke/test_v107_v110_back_compat.py``.
    * ``since_id`` is a string (v1.1.0 tuple cursor, even ``""``):
      ``WHERE (ran_at, id) > (?, ?)``. v1.1.0 validators thread the
      ``next_since_ran_at`` + ``next_since_id`` pair so the boundary
      millisecond is drained without re-delivery.

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

    # Pass ``since_id`` through with its None-vs-string distinction
    # preserved. The repository branches on this to pick legacy
    # (``ran_at > ?``) vs tuple (``(ran_at, id) > (?, ?)``) semantics.
    # NOTE: if the client sent ``since_ran_at`` (v1.1.0 shape) but
    # omitted ``since_id``, we still default to tuple mode with an
    # empty-string id — the wire contract is that a v1.1.0 caller
    # using the new param names opts into the new semantics.
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

    # Emit both cursor shapes when the page is full so the cursor is
    # safe to thread; when the page is short, the cursor is None on
    # both shapes (means "you're caught up").
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
# 2.10 GET /v1/merkle/{epoch}
# --------------------------------------------------------------------------


@router.get("/v1/merkle/{epoch}")
async def get_merkle_anchor(epoch: int, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    anchor = await repository.get_merkle_anchor(ctx.db, epoch)
    if anchor is None:
        raise HTTPException(status_code=404, detail="epoch not anchored")
    return anchor


# --------------------------------------------------------------------------
# 2.11 GET /v1/miners/{hotkey}/agents
# --------------------------------------------------------------------------


@router.get("/v1/miners/{hotkey}/agents")
async def get_miner_agents(hotkey: str, request: Request) -> dict[str, Any]:
    ctx: PublisherContext = request.app.state.ctx
    subs = await repository.list_submissions_by_hotkey(ctx.db, hotkey)
    items: list[dict[str, Any]] = []
    for s in subs:
        runs = await repository.list_eval_runs_for_submission(ctx.db, s["id"], limit=20)
        items.append(
            {
                "id": s["id"],
                "display_name": s["display_name"],
                "bio": s["bio"],
                "logo_url": s["logo_url"],
                "miner_hotkey": s["miner_hotkey"],
                "card_id": s["card_id"],
                "bundle_hash": s["bundle_hash"],
                "bundle_size_bytes": s["bundle_size_bytes"],
                "status": s["status"],
                "current_score": s["current_score"],
                "current_rank": s["current_rank"],
                "submitted_at": s["submitted_at"],
                "attestation_mode": s.get("attestation_mode", "polaris"),
                "recent_evals": [_eval_run_to_output(r, s) for r in runs],
                "score_history": [
                    {"date": r["ran_at"], "score": r["weighted_score"]} for r in reversed(runs)
                ],
            }
        )
    return {"items": items}


# --------------------------------------------------------------------------
# 2.12 GET /health
# --------------------------------------------------------------------------


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
    # The HeadBucket variant gated deploys on bucket-list scope, which
    # tokens scoped to a single bucket (the common case for shared S3
    # tenants like Hippius's per-account buckets) lack — even though
    # PutObject works fine. Submission failures surface at write time
    # with a clear 5xx, not at deploy time as a silent crash.
    if ctx.hippius is not None:
        try:
            healthy = await ctx.hippius.healthcheck()
            checks["hippius"] = "ok" if healthy else "degraded"
        except Exception:
            # Healthcheck shouldn't itself fail the request; just mark degraded.
            checks["hippius"] = "degraded"
    else:
        checks["hippius"] = "skipped"

    checks["polaris"] = "ok"  # publisher does not poll Polaris directly

    # Only DB failure is fatal for /health. Storage degradation is
    # surfaced in the response body but does NOT 503 — the publisher can
    # still serve reads (cards, leaderboard, skill.md) without storage.
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

    Field order is the publication order — keys matter to clients but not
    to canonical-JSON signing (which sorts internally).

    `output_card_hash` is included per locked decision L8: frontend renders
    it as the visible trust-chain anchor, and validators verify the
    cathedral signature against the same byte-exact projection (CRIT-7).
    `merkle_epoch` is post-anchor metadata; it is NOT covered by the
    cathedral signature (see `cathedral.v1_types.canonical_json`).

    v1.1.0 schema split (cathedralai/cathedral#75 PR 4): when the row
    carries `eval_output_schema_version=2`, the wire-shape is the v2
    projection — drops `output_card` + `output_card_hash` +
    `polaris_verified` (these are still in the DB row for legacy
    reads during the dual-publish window but NOT on the wire for v2
    records). Adds `eval_card_excerpt` and `eval_artifact_manifest_hash`
    (signed). `eval_artifact_bundle_url` + `eval_artifact_manifest_url`
    live in the UNSIGNED envelope — they're addressable hints, not
    part of the signed bytes.
    """
    schema_version = int(run.get("eval_output_schema_version") or 1)
    if schema_version == 3:
        task_json = run.get("task_json") or {}
        output = run.get("output_card_json") or {}
        claim = output.get("claim")
        if isinstance(claim, dict):
            # Public v3 feeds expose the hashed challenge id only. The raw
            # challenge_id remains in the signed DB row for validator-private
            # verification, but unauthenticated miner/site reads must not let
            # miners correlate a live prompt with a solved public row by id.
            claim = dict(claim)
            claim.pop("challenge_id", None)
        return {
            "id": run["id"],
            "agent_id": sub["id"],
            "agent_display_name": sub["display_name"],
            "miner_hotkey": sub["miner_hotkey"],
            "task_type": "bug_isolation_v1",
            "challenge_id_public": task_json.get("challenge_id_public")
            or output.get("challenge_id_public"),
            # epoch_salt is part of the signed subset; surface it
            # back on the wire so validators can re-canonicalize.
            "epoch_salt": task_json.get("epoch_salt"),
            "weighted_score": run["weighted_score"],
            "score_parts": run["score_parts"],
            "claim": claim,
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
            # Unsigned envelope — URLs are hints, not trust anchors.
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
    card. The wall reads "one stone per mason" in its flow-hint; the
    leaderboard now matches that.

    Assumes ``submissions`` is already sorted by score descending — the
    first row seen for each hotkey is that miner's best. Drops rows
    that aren't currently ranked: ``current_score is None`` covers the
    never-evaluated case; ``status != 'ranked'`` covers in-flight
    transitions where the row still carries a prior score but no fresh
    commit has landed yet (queued first-eval, evaluating refresh).
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
    # `latest_eval_at` is the synthetic column added by
    # `list_submissions_*` (MAX(eval_runs.ran_at)). Falls back to
    # `submitted_at` only when no eval_runs row exists yet — that
    # window is brief in production but matters for tests + fixtures
    # that build the dict directly without the join. Falls back to
    # `_now_iso()` as last resort to satisfy the wire-shape contract.
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


def _submission_to_discovery_item(sub: dict[str, Any]) -> dict[str, Any]:
    """Project a discovery (unverified) submission to its public shape.

    Discovery items intentionally omit score/rank fields — there are none.
    `soul_md_preview` passes through whatever the row carries; today that
    is always NULL because the submit path leaves it unpopulated for
    unverified rows. The frontend renders this as a teaser when present
    and as nothing when null.
    """
    return {
        "agent_id": sub["id"],
        "display_name": sub["display_name"],
        "logo_url": sub.get("logo_url"),
        "bio": sub.get("bio"),
        "miner_hotkey": sub["miner_hotkey"],
        "card_id": sub["card_id"],
        "bundle_hash": sub["bundle_hash"],
        "bundle_size_bytes": sub["bundle_size_bytes"],
        "submitted_at": sub["submitted_at"],
        "soul_md_preview": sub.get("soul_md_preview"),
        "tags": ["unverified", "research"],
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
