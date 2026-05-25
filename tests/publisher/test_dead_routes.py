"""Contract tests for the 12 HTTP 410 stubs introduced in PR2.

The card-era publisher exposed a per-card read catalogue
(``/v1/agents``, ``/v1/cards/{id}/*``, ``/v1/discovery/recent``,
``/v1/merkle/{epoch}``, ``/v1/miners/{hotkey}/agents``). PR2 removes
those surfaces and replaces them with HTTP 410 stubs that point miners
at the live SAT skill manifest.

These tests pin:
  - every dead route returns HTTP 410
  - the JSON body is the documented migration pointer (stable shape)
  - every dead route mounts with ``include_in_schema=False`` so
    ``/openapi.json`` stays clean
  - both URL prefixes (``/v1/...`` back-compat and
    ``/api/cathedral/v1/...`` canonical) are covered
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from cathedral.publisher.app import build_app
from cathedral.publisher.dead_routes import DEAD_PATHS, DEAD_ROUTE_BODY


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "publisher.db"
    app = build_app(database_path=str(db_path))
    with TestClient(app) as c:
        yield c


def _materialize_path(template: str) -> str:
    """Substitute ``{name}`` placeholders with an arbitrary URL-safe value
    so the route can be hit. The handler doesn't inspect path params; we
    just need something that satisfies FastAPI's path-matching."""
    return re.sub(r"\{[^}]+\}", "placeholder", template)


@pytest.mark.parametrize("path_template", DEAD_PATHS)
def test_dead_route_returns_410_on_v1_prefix(
    client: TestClient, path_template: str
) -> None:
    """``/v1/...`` (back-compat root) must 410 on every dead path."""
    path = _materialize_path(path_template)
    r = client.get(path)
    assert r.status_code == 410, (
        f"{path} should be 410 Gone; got {r.status_code} {r.text!r}"
    )
    assert r.json() == DEAD_ROUTE_BODY, (
        f"{path} body must match the documented migration pointer"
    )


@pytest.mark.parametrize("path_template", DEAD_PATHS)
def test_dead_route_returns_410_on_api_cathedral_prefix(
    client: TestClient, path_template: str
) -> None:
    """``/api/cathedral/v1/...`` (canonical surface) must 410 too."""
    path = "/api/cathedral" + _materialize_path(path_template)
    r = client.get(path)
    assert r.status_code == 410, (
        f"{path} should be 410 Gone; got {r.status_code} {r.text!r}"
    )
    assert r.json() == DEAD_ROUTE_BODY


def test_dead_route_body_is_stable(client: TestClient) -> None:
    """The migration body shape is the public contract for old miners; lock it."""
    assert DEAD_ROUTE_BODY == {
        "deprecated": True,
        "see": "https://cathedral.computer/skill.md",
        "reason": "Card-era endpoint removed; SAT is the live lane.",
    }


def test_dead_routes_absent_from_openapi(client: TestClient) -> None:
    """``include_in_schema=False`` keeps the 12 dead routes off the public API."""
    schema = client.get("/openapi.json").json()
    schema_paths = set(schema.get("paths", {}).keys())
    for template in DEAD_PATHS:
        # FastAPI emits OpenAPI paths with the ``{name}`` placeholder intact.
        for prefix in ("", "/api/cathedral"):
            full = prefix + template
            assert full not in schema_paths, (
                f"dead route {full!r} leaked into /openapi.json — "
                f"include_in_schema=False is the contract"
            )


def test_surviving_routes_still_in_openapi(client: TestClient) -> None:
    """Sanity check: the SAT-lane endpoints are still advertised."""
    schema = client.get("/openapi.json").json()
    schema_paths = set(schema.get("paths", {}).keys())
    expected_survivors = {
        "/api/cathedral/v1/agents/submit",
        "/api/cathedral/v1/leaderboard",
        "/api/cathedral/v1/leaderboard/recent",
        "/api/cathedral/health",
    }
    missing = expected_survivors - schema_paths
    assert not missing, f"SAT-lane surface missing from OpenAPI: {missing}"


def test_dead_route_count_matches_recovery_plan() -> None:
    """The recovery plan PR2 section enumerates exactly 12 dead routes."""
    assert len(DEAD_PATHS) == 12, (
        f"expected 12 dead routes per recovery plan PR2; got {len(DEAD_PATHS)}: "
        f"{DEAD_PATHS}"
    )
