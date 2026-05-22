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
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

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
_FILE_CHUNK_BYTES = 1024 * 1024


class _CnfFileHashMismatchError(Exception):
    pass


class _CnfSnapshotCache:
    """Process-local content-addressed cache for file-backed CNF snapshots.

    The first authorized fetch for a digest pays the copy/hash cost. Later
    fetches stream the same private immutable snapshot, avoiding
    O(file_size * request_count) temp-space and disk-I/O pressure from shared
    miner fetch tokens.
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cathedral-cnf-snapshots-")
        self._root = Path(self._tmp.name)
        self._lock = asyncio.Lock()
        self._by_digest: dict[str, Path] = {}

    async def get(self, source_path: Path, *, expected_sha256: str) -> Path:
        cached = self._by_digest.get(expected_sha256)
        if cached is not None and cached.is_file():
            return cached

        async with self._lock:
            cached = self._by_digest.get(expected_sha256)
            if cached is not None and cached.is_file():
                return cached
            snapshot = await asyncio.to_thread(
                _materialize_verified_cnf_snapshot,
                source_path,
                expected_sha256=expected_sha256,
                cache_root=self._root,
            )
            self._by_digest[expected_sha256] = snapshot
            return snapshot


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


def _materialize_verified_cnf_snapshot(
    path: Path,
    *,
    expected_sha256: str,
    cache_root: Path,
) -> Path:
    """Create or return one immutable snapshot for an announced CNF digest.

    ``FileResponse`` opens the path later, after route code returns. For
    operator-managed file-backed CNFs that leaves a check/use gap. Even an
    already-open descriptor can see in-place writes after the digest check, so
    the first fetch copies bytes into a private content-addressed snapshot and
    later fetches reuse it.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / f"{expected_sha256}.cnf"
    if target.is_file():
        return target

    tmp_path: str | None = None
    try:
        with path.open("rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("challenge CNF path is not a regular file")
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(
                "w+b",
                dir=cache_root,
                prefix=f".{expected_sha256[:12]}.",
                suffix=".tmp",
                delete=False,
            ) as snapshot:
                tmp_path = snapshot.name
                while chunk := source.read(_FILE_CHUNK_BYTES):
                    digest.update(chunk)
                    snapshot.write(chunk)
                snapshot.flush()
                os.fsync(snapshot.fileno())
        if digest.hexdigest() != expected_sha256:
            raise _CnfFileHashMismatchError
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.replace(tmp_path, target)
        tmp_path = None
        return target
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _cnf_snapshot_cache(request: Request) -> _CnfSnapshotCache:
    cache: _CnfSnapshotCache | None = getattr(request.app.state, "cnf_snapshot_cache", None)
    if cache is None:
        cache = _CnfSnapshotCache()
        request.app.state.cnf_snapshot_cache = cache
    return cache


def _iter_open_file(handle: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := handle.read(_FILE_CHUNK_BYTES):
            yield chunk
    finally:
        handle.close()


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
            # The cache materializes one verified immutable snapshot per digest
            # off the event loop; later authorized fetches stream that snapshot
            # directly instead of copying the full launch CNF again.
            snapshot_path = await _cnf_snapshot_cache(request).get(
                path,
                expected_sha256=expected_sha256,
            )
        except FileNotFoundError:
            logger.warning("challenge_cnf_file_missing", challenge_id=challenge_id)
            raise _not_found() from None
        except _CnfFileHashMismatchError:
            logger.warning("challenge_cnf_file_hash_mismatch", challenge_id=challenge_id)
            raise _not_found() from None
        except OSError:
            logger.warning("challenge_cnf_file_unreadable", challenge_id=challenge_id)
            raise _not_found() from None
        return StreamingResponse(
            _iter_open_file(snapshot_path.open("rb")),
            media_type="text/plain; charset=utf-8",
        )
    if not lookup.cnf_text:
        raise _not_found()
    return PlainTextResponse(
        lookup.cnf_text,
        media_type="text/plain; charset=utf-8",
    )
