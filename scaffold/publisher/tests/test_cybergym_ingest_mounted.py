"""The CyberGym intake must be reachable on the real publisher app.

An endpoint that exists only as an unmounted APIRouter is a component, not a
default-off endpoint: no amount of configuration can make it answer. These tests
build the actual app through ``build_app`` and pin both halves of the claim:

  * the route is mounted and bound to that app's own publisher ``Store``, so
    configuration alone turns it on and an accepted report lands in the same
    database the adapter reads;
  * the default-off gate is still at the route, so an unconfigured deployment
    answers 404 and mounting changed nothing for it.

Non-writing: an in-memory SQLite store, no chain, no network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from scaffold.publisher import app as app_mod, cybergym_ingest as ingest

TOKEN = "mounted-cybergym-token"
SECRET = "mounted-cybergym-secret"
PRODUCER = "5MountedProducer"
NETWORK = "finney"
NETUID = 39
PATH = "/v1/cybergym/scores"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _doc(*, epoch: int = 1) -> dict:
    return {
        "producer_hotkey": PRODUCER,
        "network": NETWORK,
        "netuid": NETUID,
        "source_epoch": epoch,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "complete": True,
        "score_units": "level_weighted_verified_solves",
        "scores": {"5Alice": 3.0},
        "evidence_sha256": "e" * 64,
    }


def _headers(raw: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "X-Cathedral-Cybergym-Signature": "sha256=" + hmac.new(
            SECRET.encode(), raw, hashlib.sha256
        ).hexdigest(),
    }


def _configure(monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETWORK", NETWORK)
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETUID", str(NETUID))
    monkeypatch.setenv(ingest.AUTH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ingest.HMAC_SECRET_ENV, SECRET)
    monkeypatch.setenv(ingest.PRODUCER_HOTKEY_ENV, PRODUCER)
    if enabled:
        monkeypatch.setenv(ingest.INGEST_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(ingest.INGEST_ENABLED_ENV, raising=False)


def _mounted_paths(app) -> set[str]:
    """Every routable path, following FastAPI 0.141's included-router wrappers.

    ``include_router`` no longer flattens routes into ``app.routes``: an included
    router shows up as a single wrapper object exposing ``original_router``. A
    test that only reads ``app.routes[*].path`` would conclude the route is
    absent while it is in fact served.
    """
    found: set[str] = set()
    pending = [app.router]
    while pending:
        router = pending.pop()
        for route in getattr(router, "routes", []):
            path = getattr(route, "path", None)
            if path:
                found.add(path)
            nested = getattr(route, "original_router", None) or getattr(
                route, "router", None
            )
            if nested is not None:
                pending.append(nested)
    return found


def test_route_is_mounted_on_the_real_app():
    assert PATH in _mounted_paths(app_mod.build_app())


def test_route_is_actually_served_not_just_registered(monkeypatch):
    """Reachability, not registration: an unmatched path returns FastAPI's own
    "Not Found", so seeing this endpoint's own gate detail proves the handler ran."""
    _configure(monkeypatch, enabled=False)
    client = TestClient(app_mod.build_app())
    served = client.post(PATH, json={})
    unmatched = client.post("/v1/cybergym/not-a-route", json={})
    assert served.status_code == 404 and unmatched.status_code == 404
    assert served.json()["detail"] == "cybergym_ingest_not_enabled"
    assert unmatched.json()["detail"] == "Not Found"


def test_unconfigured_app_still_answers_404(monkeypatch):
    """Mounting must not change behavior for a deployment that has not enabled
    the lane."""
    for name in (
        ingest.INGEST_ENABLED_ENV,
        ingest.AUTH_TOKEN_ENV,
        ingest.HMAC_SECRET_ENV,
        ingest.PRODUCER_HOTKEY_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    client = TestClient(app_mod.build_app())
    raw = json.dumps(_doc(), sort_keys=True, separators=(",", ":")).encode()
    r = client.post(PATH, content=raw, headers=_headers(raw))
    assert r.status_code == 404
    assert r.json()["detail"] == "cybergym_ingest_not_enabled"


@pytest.mark.parametrize("missing", [
    ingest.AUTH_TOKEN_ENV,
    ingest.HMAC_SECRET_ENV,
    ingest.PRODUCER_HOTKEY_ENV,
])
def test_enabled_but_incompletely_configured_fails_closed(monkeypatch, missing):
    _configure(monkeypatch, enabled=True)
    monkeypatch.delenv(missing, raising=False)
    client = TestClient(app_mod.build_app())
    raw = json.dumps(_doc(), sort_keys=True, separators=(",", ":")).encode()
    r = client.post(PATH, content=raw, headers=_headers(raw))
    assert r.status_code == 503


def test_configuration_alone_makes_the_endpoint_work(monkeypatch):
    """The whole point of mounting: no code change, no separate app, and the
    accepted report lands in the store the adapter reads."""
    _configure(monkeypatch, enabled=True)
    app = app_mod.build_app()
    client = TestClient(app)
    raw = json.dumps(_doc(), sort_keys=True, separators=(",", ":")).encode()
    r = client.post(PATH, content=raw, headers=_headers(raw))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True
    assert body["source_epoch"] == 1

    rows = app.state.store.query(
        "SELECT id, producer_hotkey, source_epoch, score_count "
        "FROM cybergym_score_reports"
    )
    assert len(rows) == 1
    assert rows[0]["producer_hotkey"] == PRODUCER
    assert rows[0]["score_count"] == 1
    scores = app.state.store.query(
        "SELECT miner_hotkey, score FROM cybergym_scores"
    )
    assert [(s["miner_hotkey"], s["score"]) for s in scores] == [("5Alice", 3.0)]


def test_each_app_uses_its_own_store(monkeypatch):
    """Injected per app rather than through the module-level setter, so building
    several apps in one process cannot cross-wire their databases."""
    _configure(monkeypatch, enabled=True)
    first = app_mod.build_app()
    second = app_mod.build_app()
    raw = json.dumps(_doc(), sort_keys=True, separators=(",", ":")).encode()
    assert TestClient(first).post(PATH, content=raw, headers=_headers(raw)).status_code == 200
    assert len(first.state.store.query("SELECT * FROM cybergym_score_reports")) == 1
    assert second.state.store.query("SELECT * FROM cybergym_score_reports") == []


@pytest.mark.parametrize("role", ["read", "submit", "worker"])
def test_narrow_service_roles_refuse_the_route(monkeypatch, role):
    """The path is deliberately absent from the role allowlists, so only a
    full-role process can serve producer intake."""
    _configure(monkeypatch, enabled=True)
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", role)
    client = TestClient(app_mod.build_app())
    raw = json.dumps(_doc(), sort_keys=True, separators=(",", ":")).encode()
    r = client.post(PATH, content=raw, headers=_headers(raw))
    assert r.status_code == 404
    assert r.headers.get("x-cathedral-rejection-reason") == (
        f"route_not_served_by_{role}_role"
    )
