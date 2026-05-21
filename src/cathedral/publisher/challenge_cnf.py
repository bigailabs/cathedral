"""Public CNF endpoint for the synthetic boolean SAT lane.

Cardinal sin: the satisfying assignment must never leave the publisher
process. This route serves only the active challenge's CNF body, either
from ``lane_challenges.cnf_text`` or from its publisher-local
``cnf_path``, and only when the publisher has explicitly announced the
challenge by minting a ``lane_challenge_fetch_tokens`` row.

The endpoint deliberately has no enumerate / list surface and returns
the same opaque 404 body for every miss path -- unknown id, missing
token, wrong token, status that's not active-or-locked-in-grace -- so
the route cannot be used as an existence oracle on private challenge
material.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse, Response

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    EndpointLookup,
    FetchTokenRecord,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)

router = APIRouter()


_CHALLENGE_NOT_FOUND_DETAIL = "challenge_not_found"
_POST_LOCK_GRACE_SECS = 30


def _parse_iso(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse. Returns ``None`` on malformed input.

    The route never raises on a bad timestamp; it treats the row as if
    the grace window has elapsed (404). This keeps the endpoint total:
    a single bad column never produces a 5xx that a probe could use to
    distinguish a real row from a fake one.
    """
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _within_post_lock_grace(
    lookup: EndpointLookup,
    token_row: FetchTokenRecord,
    now: datetime,
) -> bool:
    """Locked rows stay fetchable for announced_time_limit_secs + 30s.

    The grace anchor is the source row's ``updated_at_iso`` (set by
    ``mark_locked_and_promote_next``), not the token's mint time, so
    miners in the same batch that the winner just locked out have the
    full time-limit window plus a small cushion to finish their fetch
    before the URL goes dark.
    """
    locked_at = _parse_iso(lookup.updated_at_iso)
    if locked_at is None:
        return False
    grace_secs = int(token_row.announced_time_limit_secs) + _POST_LOCK_GRACE_SECS
    elapsed = (now - locked_at).total_seconds()
    return 0 <= elapsed <= grace_secs


def _not_found() -> HTTPException:
    """Same 404 shape for every miss path. No state-distinguishing detail."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=_CHALLENGE_NOT_FOUND_DETAIL,
    )


@router.get("/v1/challenges/{challenge_id}/cnf", include_in_schema=False)
async def get_challenge_cnf(
    challenge_id: str,
    request: Request,
    t: str = Query(default="", description="Announcement token"),
) -> Response:
    """Serve the active (or locked-in-grace) CNF body for a challenge.

    Returns 200 ``text/plain`` with the DIMACS body when:
    - a fetch-token row exists for ``challenge_id``
    - the ``?t=`` query value matches that row's token (constant-time
      compare)
    - the source row's status is ``active``, OR ``locked`` and within
      ``announced_time_limit_secs + 30s`` of ``updated_at_iso``

    Returns 404 with ``{"detail": "challenge_not_found"}`` in every
    other case. The route never logs the token.
    """
    source: SqliteChallengeSource | None = getattr(
        request.app.state, "task_family_challenge_source", None
    )
    tokens: SqliteFetchTokenStore | None = getattr(
        request.app.state, "task_family_fetch_token_store", None
    )
    if source is None or tokens is None:
        # Feed not wired. Same 404 as a real miss so the wire shape
        # doesn't differ between "publisher under-configured" and
        # "challenge does not exist".
        raise _not_found()

    if not t:
        raise _not_found()

    token_row = await tokens.get(challenge_id)
    if token_row is None:
        raise _not_found()

    # Constant-time compare so a probe can't measure prefix matches
    # against the unguessable token.
    if not hmac.compare_digest(token_row.fetch_token, t):
        raise _not_found()

    lookup = await source.get_for_endpoint(challenge_id)
    if lookup is None:
        raise _not_found()

    now = datetime.now(UTC)
    if lookup.status == CHALLENGE_STATUS_ACTIVE:
        servable = True
    elif lookup.status == CHALLENGE_STATUS_LOCKED:
        servable = _within_post_lock_grace(lookup, token_row, now)
    else:
        servable = False
    if not servable:
        raise _not_found()

    if lookup.cnf_path:
        path = Path(lookup.cnf_path)
        if not path.is_file():
            logger.warning("challenge_cnf_file_missing", challenge_id=challenge_id)
            raise _not_found()
        return FileResponse(
            path,
            media_type="text/plain; charset=utf-8",
        )
    if not lookup.cnf_text:
        raise _not_found()
    return PlainTextResponse(
        lookup.cnf_text,
        media_type="text/plain; charset=utf-8",
    )
