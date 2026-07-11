"""Route-level tests for POST /v1/external-scores/violet source-scoped auth.

These tests verify the actual HTTP route behavior: parsing source, enforcing
dedicated vs. shared token boundaries, and proper 401/503 error codes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from scaffold.publisher import app as app_mod


def _now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sample_report(source: str = "cathedral_sat_fast") -> dict:
    return {
        "source": source,
        "generated_at": _now_iso(),
        "scores": [
            {"miner_hotkey": "5Alice", "score": 0.5},
        ],
        "complete": True,
        "epoch": 1,
    }


@pytest.fixture
def client(monkeypatch):
    """Build the app with external scores enabled but no weights blending yet."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED", "1")
    # Weights blending OFF by default in these tests (we test it separately).
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", raising=False)
    return TestClient(app_mod.build_app())


def test_route_rejects_missing_bearer_when_shared_token_required(client, monkeypatch):
    """Missing bearer token fails 401 when shared token is configured."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_token"


def test_route_accepts_shared_token_for_source_without_dedicated(client, monkeypatch):
    """Shared token authorizes a source without a dedicated token."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_accepts_dedicated_token_for_source(client, monkeypatch):
    """Dedicated token for a source authorizes only that source."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-secret",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_rejects_shared_token_when_dedicated_exists(client, monkeypatch):
    """Shared token does NOT authorize a source with a dedicated token."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_token"


def test_route_rejects_wrong_dedicated_token(client, monkeypatch):
    """A source's dedicated token does not authorize a different source."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_SAT_FAST", "sat-secret")
    report = _sample_report("cathedral_sat_fast")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-secret",  # Wrong token for this source
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_token"


def test_route_fails_503_when_blending_enabled_but_no_credential(client, monkeypatch):
    """When blending is enabled, 503 if neither shared nor dedicated credential exists."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    # No shared token and no dedicated token for this source.
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", raising=False)
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", raising=False)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "external_scores_token_required_while_blending"


def test_route_accepts_dedicated_token_when_blending_enabled(client, monkeypatch):
    """When blending is enabled, dedicated token alone (no shared) authorizes."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", raising=False)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-secret",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_accepts_x_token_header_for_bearer(client, monkeypatch):
    """X-Cathedral-External-Token header works as alternative to Authorization bearer."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_SAT_FAST", "sat-secret")
    report = _sample_report("cathedral_sat_fast")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Cathedral-External-Token": "sat-secret",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_rejects_invalid_source_before_auth(client, monkeypatch):
    """Invalid source is rejected before auth check (400, not 401)."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    report = _sample_report("cathedral_invalid_source")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert "source" in resp.json()["detail"].lower()


def test_route_rejects_json_array(client, monkeypatch):
    """JSON array payload is rejected with 400 invalid_report_contract."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    body = json.dumps([{"miner_hotkey": "5Alice"}]).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_report_contract"


def test_route_rejects_json_null(client, monkeypatch):
    """JSON null payload is rejected with 400 invalid_report_contract."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    body = json.dumps(None).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_report_contract"


def test_route_rejects_json_scalar_string(client, monkeypatch):
    """JSON scalar string payload is rejected with 400 invalid_report_contract."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    body = json.dumps("just a string").encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_report_contract"


def test_route_rejects_json_scalar_number(client, monkeypatch):
    """JSON scalar number payload is rejected with 400 invalid_report_contract."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    body = json.dumps(42).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_report_contract"


def test_route_rejects_json_scalar_boolean(client, monkeypatch):
    """JSON scalar boolean payload is rejected with 400 invalid_report_contract."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    body = json.dumps(True).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_report_contract"
