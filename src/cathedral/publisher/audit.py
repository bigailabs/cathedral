"""Bearer-gated audit surface for raw DIMACS solution bodies (issue #242).

The publisher writes the raw DIMACS body submitted by a miner into the
``eval_run_solutions`` sidecar table inside the same transactional
scope as the parent ``eval_runs`` INSERT (see
``cathedral.publisher.submit._handle_solve_post``). That body is NEVER
exposed via the public read surface (``/v1/leaderboard/recent``,
``/v1/agents/*``, dashboards) — those continue to expose only the
hash, verdict, score, and signature.

This module mounts the one private read path for the body:

    GET /v1/audit/eval-runs/{eval_run_id}/solution

Authentication: shared-secret bearer in the ``Authorization`` header.
The token comes from the ``CATHEDRAL_AUDIT_TOKEN`` env var, which is
NOT defaulted — if the env is unset, every call returns 503 ("audit
not configured"). This is deliberate: open access to raw solution
bodies would defeat the public/private split the issue defines. The
token comparison uses ``hmac.compare_digest`` so a network-timing
attacker cannot byte-walk the secret.

TODO(issue #242): retention pruner. The issue's acceptance criteria
include a nightly job that deletes ``eval_run_solutions`` rows older
than `(now - 90d)` UNLESS the row is referenced by a
``validator_sat_results`` record with ``verdict_matches = 0`` (the
disagreement-bypass). That table does not exist in the codebase yet
(slice-1 work, not merged), so the pruner cannot be wired here without
referencing a missing dependency. When ``validator_sat_results`` lands,
add a ``cathedral.publisher.audit_retention`` module + an asyncio task
in ``app.py`` lifespan that runs the cutoff query nightly. Until then,
bodies accumulate indefinitely — same as if this PR had never been
written, because the alternative is data loss with no audit trail.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from cathedral.publisher import repository

# Env var name centralised here so tests + ops runbooks can grep for it.
# Empty/unset → 503 on every call. The value is treated as opaque bytes
# for the constant-time compare; we do NOT trim/strip it because that
# would silently mask operator-side trailing-newline mistakes.
AUDIT_TOKEN_ENV = "CATHEDRAL_AUDIT_TOKEN"

# "Authorization: Bearer <token>" prefix. Case-insensitive per RFC 7235
# but FastAPI/Starlette gives us the header verbatim; we normalise here.
_BEARER_PREFIX = "bearer "

router = APIRouter()


def _extract_bearer(authorization: str | None) -> str | None:
    """Pull the token off an ``Authorization: Bearer <token>`` header.

    Returns ``None`` for missing/malformed headers; the caller maps that
    to 401. We intentionally do NOT distinguish "no header" from "wrong
    scheme" from "empty token" in the response body — leaking which one
    failed gives an attacker a free oracle.

    Whitespace handling: we deliberately do NOT ``.strip()`` the token
    body, because a stripped token compares equal to one with trailing
    whitespace and that would (a) mask an operator-side trailing-newline
    mistake in the env var (which silently lets the wrong bytes
    authenticate) and (b) break the raw-bytes constant-time compare
    contract — two byte strings that differ in trailing whitespace must
    NOT collide. The empty-token check uses a length test on the raw
    suffix instead.
    """
    if not authorization:
        return None
    if len(authorization) <= len(_BEARER_PREFIX):
        return None
    if authorization[: len(_BEARER_PREFIX)].lower() != _BEARER_PREFIX:
        return None
    token = authorization[len(_BEARER_PREFIX) :]
    if not token:
        return None
    return token


def _require_audit_token(request: Request) -> None:
    """Authenticate an audit request or raise the right HTTPException.

    503 if the publisher has no ``CATHEDRAL_AUDIT_TOKEN`` configured —
    we do NOT allow open access in any environment, including tests
    that forgot to set the env var. The endpoint is OFF by default.

    401 if the header is missing or the token does not match. The
    response detail is intentionally generic so an attacker cannot tell
    whether the header was missing or just wrong.
    """
    expected = os.environ.get(AUDIT_TOKEN_ENV)
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="audit not configured",
        )
    presented = _extract_bearer(request.headers.get("authorization"))
    if presented is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    # Constant-time compare on raw UTF-8 bytes — guards against
    # remote-timing side channels. compare_digest is the standard library
    # primitive for exactly this case.
    if not hmac.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.get("/v1/audit/eval-runs/{eval_run_id}/solution")
async def get_eval_run_solution(
    eval_run_id: str,
    request: Request,
) -> dict[str, Any]:
    """Return the raw DIMACS body for an eval_run, bearer-gated.

    Response shape (200):

    .. code-block:: json

        {
          "eval_run_id":      "<uuid>",
          "dimacs_solution":  "<raw bytes the miner sent, UTF-8>",
          "body_sha256":      "<hex sha256, mirrors miner_solution_sha256>",
          "stored_at":        "<ISO-8601 millisecond timestamp>"
        }

    Errors:

    - 503 ``audit not configured`` — ``CATHEDRAL_AUDIT_TOKEN`` unset.
    - 401 ``unauthorized`` — bearer missing or mismatched.
    - 404 ``not found`` — no sidecar row for that ``eval_run_id``
      (either never written, or pruned per future retention policy).
    """
    _require_audit_token(request)
    ctx = request.app.state.ctx
    row = await repository.get_eval_run_solution(
        ctx.db, eval_run_id=eval_run_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return row
