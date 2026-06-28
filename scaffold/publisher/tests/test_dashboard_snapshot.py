"""Tests for the prebuilt /v1/dashboard/state snapshot endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from scaffold.publisher import dashboard_snapshot as ds
from scaffold.publisher.app import build_app
from scaffold.publisher.keys import generate_test_key


def _build(monkeypatch, *, enabled: bool, service_role: str | None = None):
    if enabled:
        monkeypatch.setenv("CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED", "1")
    else:
        monkeypatch.delenv("CATHEDRAL_DASHBOARD_SNAPSHOT_ENABLED", raising=False)
    if service_role is None:
        monkeypatch.delenv("CATHEDRAL_SERVICE_ROLE", raising=False)
    else:
        monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", service_role)
    return build_app(database_path=":memory:", signing_key_hex=generate_test_key())


def test_dashboard_state_default_off_is_cheap_disabled(monkeypatch):
    app = _build(monkeypatch, enabled=False)
    with TestClient(app) as client:
        resp = client.get("/v1/dashboard/state")
    assert resp.status_code == 503
    assert resp.headers.get("cache-control") == "no-store"
    body = resp.json()
    assert body["schema"] == ds.SCHEMA
    assert body["data_status"] == "disabled"
    assert body["snapshot_id"] is None
    assert body["earnings_leaderboard"]["miners"] == []


def test_dashboard_state_enabled_but_cold_does_not_build_on_request(monkeypatch):
    app = _build(monkeypatch, enabled=True)
    with TestClient(app) as client:
        snap = app.state.dashboard_state_snapshot
        snap.stop()
        with snap._lock:  # type: ignore[attr-defined]
            snap._payload = None  # type: ignore[attr-defined]
        resp = client.get("/v1/dashboard/state")
    assert resp.status_code == 503
    body = resp.json()
    assert body["data_status"] == "warming"
    assert body["reason"] == "dashboard snapshot is cold or stale"


def test_dashboard_state_serves_prebuilt_payload(monkeypatch):
    app = _build(monkeypatch, enabled=True)
    with TestClient(app) as client:
        snap = app.state.dashboard_state_snapshot
        snap.stop()
        assert snap.refresh_once() is True
        resp = client.get("/v1/dashboard/state")
    assert resp.status_code == 200
    assert resp.headers.get("x-cathedral-snapshot") == ds.SNAPSHOT_NAME
    body = resp.json()
    assert body["schema"] == ds.SCHEMA
    assert body["snapshot_id"].startswith("dash_")
    assert body["built_at"]
    assert body["age_seconds"] >= 0
    assert "source_epoch" in body
    assert "source_block" in body
    assert "earnings_leaderboard" in body
    assert "miners" in body["earnings_leaderboard"]
    assert "pm_health" in body
    assert "queue_lag" in body
    assert body["queue_lag"]["data_status"] == "admin_only"
    assert "weights_freshness" in body
    assert "freshness" in body["weights_freshness"]
    assert "endpoint_pressure" in body
    assert body["endpoint_pressure"]["data_status"] == "admin_only"
    assert "rejection_reasons" in body
    assert "pm_attempt_reasons" in body["rejection_reasons"]


def test_dashboard_state_is_read_role_only(monkeypatch):
    read_app = _build(monkeypatch, enabled=False, service_role="read")
    with TestClient(read_app) as client:
        read_resp = client.get("/v1/dashboard/state")
    assert read_resp.status_code == 503
    assert read_resp.json()["data_status"] == "disabled"

    submit_app = _build(monkeypatch, enabled=False, service_role="submit")
    with TestClient(submit_app) as client:
        submit_resp = client.get("/v1/dashboard/state")
    assert submit_resp.status_code == 404
    assert submit_resp.headers.get("x-cathedral-service-role") == "submit"
