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

import asyncio
import hashlib
import hmac
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    EndpointLookup,
    FetchTokenRecord,
    SqliteChallengeSource,
    SqliteFetchTokenStore,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


_CHALLENGE_NOT_FOUND_DETAIL = "challenge_not_found"
_POST_LOCK_GRACE_SECS = 30


class _CnfFileHashMismatchError(Exception):
    pass


class _CnfFileOversizedError(Exception):
    pass


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


def _normalized_sha256(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    if len(stripped) == 64 and all(ch in "0123456789abcdef" for ch in stripped):
        return stripped
    return None


def _read_verified_cnf_file(
    path: Path,
    *,
    expected_sha256: str,
    max_bytes: int | None,
) -> bytes:
    """Read a file-backed CNF fully, enforcing the size cap and digest.

    Lock-free and safe to run in a worker thread. There is NO check/use gap:
    we hash the exact bytes we return, so an in-place mutation can only produce
    a digest mismatch (-> 404), never a served-but-wrong body. Reading the whole
    (immutable, <= a couple MB) body into memory in-process also makes an
    in-flight retirement ``unlink`` harmless — the response no longer depends on
    the path after this returns. This deliberately replaces the global-locked
    snapshot cache on the serve path, whose prune-on-every-fetch serialized all
    miner CNF fetches.
    """
    with path.open("rb") as handle:
        file_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("challenge CNF path is not a regular file")
        if max_bytes is not None and file_stat.st_size > max_bytes:
            raise _CnfFileOversizedError
        # Read one byte past the cap (when set) so a path swapped to a larger
        # file between stat and read is still rejected rather than truncated.
        limit = -1 if max_bytes is None else max_bytes + 1
        data = handle.read(limit)
    if max_bytes is not None and len(data) > max_bytes:
        raise _CnfFileOversizedError
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise _CnfFileHashMismatchError
    return data


def _effective_cnf_max_bytes(lookup: EndpointLookup) -> int | None:
    candidates = [
        value for value in (lookup.cnf_bytes, lookup.max_cnf_bytes) if value is not None
    ]
    if not candidates:
        return None
    # File-backed rows record the seeded file size. Use the tighter of that
    # immutable size and any configured launch cap so a later oversized path
    # replacement fails on stat instead of after a full copy/hash pass.
    return min(candidates)


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
        expected_sha256 = _normalized_sha256(lookup.cnf_sha256)
        if expected_sha256 is None:
            logger.warning("challenge_cnf_file_hash_missing", challenge_id=challenge_id)
            raise _not_found()
        try:
            # Lock-free, per-request read+verify off the event loop. We hash the
            # exact bytes returned (no check/use gap) and hold them in memory, so
            # there is no shared cache lock to serialize concurrent miner fetches
            # and an in-flight retirement unlink cannot corrupt the response.
            data = await asyncio.to_thread(
                _read_verified_cnf_file,
                path,
                expected_sha256=expected_sha256,
                max_bytes=_effective_cnf_max_bytes(lookup),
            )
        except FileNotFoundError:
            logger.warning("challenge_cnf_file_missing", challenge_id=challenge_id)
            raise _not_found() from None
        except _CnfFileHashMismatchError:
            logger.warning("challenge_cnf_file_hash_mismatch", challenge_id=challenge_id)
            raise _not_found() from None
        except _CnfFileOversizedError:
            logger.warning("challenge_cnf_file_oversized", challenge_id=challenge_id)
            raise _not_found() from None
        except OSError:
            logger.warning("challenge_cnf_file_unreadable", challenge_id=challenge_id)
            raise _not_found() from None
        return Response(
            content=data,
            media_type="text/plain; charset=utf-8",
        )
    if not lookup.cnf_text:
        raise _not_found()
    return PlainTextResponse(
        lookup.cnf_text,
        media_type="text/plain; charset=utf-8",
    )
