"""HTTP 410 Gone stubs for the card-era endpoints removed in PR2.

Cathedral SN39 moved from card-shaped registrations to the SAT
(synthetic_boolean_v1) task family lane. The publisher no longer reads
or writes the card surfaces; every old read endpoint returns HTTP 410
with a stable JSON body pointing miners at the live skill manifest.

The routes are mounted with ``include_in_schema=False`` so they do NOT
appear in ``/openapi.json``. Tests in
``tests/publisher/test_dead_routes.py`` pin both the response body and
the OpenAPI absence.

Removed endpoints (12):

  GET /v1/agents
  GET /v1/agents/{agent_id}
  GET /v1/cards/{card_id}
  GET /v1/cards/{card_id}/history
  GET /v1/cards/{card_id}/eval-spec
  GET /v1/cards/{card_id}/discovery
  GET /v1/cards/{card_id}/discovery/count
  GET /v1/cards/{card_id}/feed
  GET /v1/cards/{card_id}/attempts
  GET /v1/discovery/recent
  GET /v1/merkle/{epoch}
  GET /v1/miners/{hotkey}/agents
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


DEAD_ROUTE_BODY: dict[str, object] = {
    "deprecated": True,
    "see": "https://cathedral.computer/skill.md",
    "reason": "Card-era endpoint removed; SAT is the live lane.",
}


# Routes whose path contains a templated segment (``{card_id}`` /
# ``{agent_id}`` / ``{hotkey}`` / ``{epoch}``) keep that template — FastAPI
# matches any value, the handler ignores it and returns 410. Order: list the
# more specific paths first so FastAPI's longest-match routing finds them
# before the parent ``/v1/cards/{card_id}`` template would shadow them.
_DEAD_PATHS: tuple[str, ...] = (
    "/v1/agents",
    "/v1/agents/{agent_id}",
    "/v1/cards/{card_id}/history",
    "/v1/cards/{card_id}/eval-spec",
    "/v1/cards/{card_id}/discovery/count",
    "/v1/cards/{card_id}/discovery",
    "/v1/cards/{card_id}/feed",
    "/v1/cards/{card_id}/attempts",
    "/v1/cards/{card_id}",
    "/v1/discovery/recent",
    "/v1/merkle/{epoch}",
    "/v1/miners/{hotkey}/agents",
)


def _gone_response() -> JSONResponse:
    return JSONResponse(status_code=410, content=DEAD_ROUTE_BODY)


def _make_handler(path: str):  # type: ignore[no-untyped-def]
    """Build a closure whose signature exposes the path parameters FastAPI
    extracts from the URL template, all typed as ``str``. The handler
    discards them and returns the canonical 410 body.

    Without this, FastAPI's dependency resolver treats path parameters as
    REQUIRED query parameters when the handler's signature doesn't list
    them, which turns the routes into 400 ``Field required`` responses
    instead of 410 — the failure mode the contract test pins.
    """
    import re

    params = re.findall(r"\{([^}]+)\}", path)

    if not params:

        async def _no_params() -> JSONResponse:
            return _gone_response()

        return _no_params

    # Build the function dynamically so each path param appears in the
    # signature with a typed annotation.
    arg_sig = ", ".join(f"{name}: str" for name in params)
    src = (
        f"async def _handler({arg_sig}) -> JSONResponse:\n"
        f"    return _gone_response()\n"
    )
    namespace: dict[str, object] = {"JSONResponse": JSONResponse, "_gone_response": _gone_response}
    exec(src, namespace)  # noqa: S102 — closed-set of known param names
    return namespace["_handler"]


def _register(path: str) -> None:
    handler = _make_handler(path)
    router.add_api_route(
        path,
        handler,  # type: ignore[arg-type]
        methods=["GET"],
        include_in_schema=False,
        status_code=410,
    )


for _path in _DEAD_PATHS:
    _register(_path)


# Exported so tests can iterate the dead-route set without re-typing the list.
DEAD_PATHS: tuple[str, ...] = _DEAD_PATHS
