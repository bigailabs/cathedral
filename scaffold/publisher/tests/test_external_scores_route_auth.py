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
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-secret",
            "X-Cathedral-External-Signature": f"sha256={sig}",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_rejects_shared_token_when_dedicated_exists(client, monkeypatch):
    """Shared token does NOT authorize a source with a dedicated token."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-secret")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-secret",
            "X-Cathedral-External-Signature": f"sha256={sig}",
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
    # Token check happens before HMAC check, so fails with token_required error.
    assert resp.status_code == 503
    assert resp.json()["detail"] == "external_scores_token_required_while_blending"


def test_route_accepts_dedicated_token_when_blending_enabled(client, monkeypatch):
    """When blending is enabled, dedicated token + dedicated HMAC alone (no shared) authorizes."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-secret")
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", raising=False)
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-secret",
            "X-Cathedral-External-Signature": f"sha256={sig}",
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


# ---- Source-specific HMAC tests (cathedral_confidential_tdx) -----

def test_route_fails_503_when_cathedral_confidential_tdx_missing_dedicated_hmac_secret(client, monkeypatch):
    """For cathedral_confidential_tdx, 503 if the dedicated HMAC secret is not configured."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    # No dedicated HMAC secret configured.
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", raising=False)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": "sha256=abc123",
        }
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "external_scores_hmac_secret_required"


def test_route_fails_401_when_cathedral_confidential_tdx_missing_signature(client, monkeypatch):
    """For cathedral_confidential_tdx, 401 if signature is missing but secret is configured."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-hmac-secret")
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_signature"


def test_route_fails_401_when_cathedral_confidential_tdx_bad_signature(client, monkeypatch):
    """For cathedral_confidential_tdx, 401 if signature doesn't match the HMAC."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-hmac-secret")
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_signature"


def test_route_accepts_cathedral_confidential_tdx_with_correct_signature(client, monkeypatch):
    """For cathedral_confidential_tdx, 202 when signature is valid."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": f"sha256={expected_sig}",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_global_hmac_does_not_substitute_for_cathedral_confidential_tdx(client, monkeypatch):
    """For cathedral_confidential_tdx, global HMAC secret cannot substitute for dedicated one."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    global_secret = "global-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", global_secret)
    # No dedicated HMAC secret.
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", raising=False)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    # Sign with global secret (should fail since dedicated is missing).
    global_sig = hmac.new(global_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": f"sha256={global_sig}",
        }
    )
    # Expect 503 (missing dedicated secret) regardless of valid global signature.
    assert resp.status_code == 503
    assert resp.json()["detail"] == "external_scores_hmac_secret_required"


def test_route_cathedral_confidential_tdx_accepts_hex_without_sha256_prefix(client, monkeypatch):
    """For cathedral_confidential_tdx, signature can be bare hex without sha256= prefix."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": expected_sig,  # Bare hex
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_body_tampering_fails_for_cathedral_confidential_tdx(client, monkeypatch):
    """For cathedral_confidential_tdx, 401 when request body is tampered with."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    report = _sample_report("cathedral_confidential_tdx")
    original_body = json.dumps(report).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), original_body, hashlib.sha256).hexdigest()
    # Tamper with the body by changing a score value.
    tampered_report = report.copy()
    tampered_report["scores"][0]["score"] = 0.9
    tampered_body = json.dumps(tampered_report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=tampered_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": f"sha256={sig}",
        }
    )
    # Signature was computed over original body, tampering should fail.
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_signature"


def test_route_other_sources_remain_backward_compatible_with_optional_hmac(client, monkeypatch):
    """For non-mandatory sources (e.g., violet_audio), global HMAC remains optional."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    global_secret = "global-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", global_secret)
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    sig = hmac.new(global_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # With valid signature.
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "X-Cathedral-External-Signature": f"sha256={sig}",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_other_sources_fail_401_with_bad_global_hmac(client, monkeypatch):
    """For non-mandatory sources, 401 when global HMAC is required but bad."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", "global-secret")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "X-Cathedral-External-Signature": "sha256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_external_scores_signature"


def test_route_other_sources_pass_when_no_global_hmac_configured(client, monkeypatch):
    """For non-mandatory sources, 202 when no global HMAC is configured."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    # No global HMAC configured.
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", raising=False)
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


# ---- Bounded body consumption tests (CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES) -----

def test_route_rejects_declared_oversize_with_413(client, monkeypatch):
    """Declared Content-Length over cap is rejected with 413 before reading."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    # Cap at 512 bytes for this test
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", "512")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    # Claim a larger size than actual (2 KiB declared, small actual payload)
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "Content-Length": str(2048),  # Over cap
        }
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "external_scores_body_too_large"


def test_route_accepts_exact_cap_boundary(client, monkeypatch):
    """Payload exactly at cap is accepted."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    # Create a small report
    report = {"source": "violet_audio", "generated_at": _now_iso(), "scores": [], "complete": True, "epoch": 1}
    body = json.dumps(report).encode("utf-8")
    # Set cap to exactly the body size
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", str(len(body)))
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "Content-Length": str(len(body)),
        }
    )
    assert resp.status_code == 202, f"Expected 202 at exact cap, got {resp.status_code}: {resp.json()}"


def test_route_rejects_one_byte_over_cap(client, monkeypatch):
    """Payload one byte over cap is rejected with 413."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    # Create a small report
    report = {"source": "violet_audio", "generated_at": _now_iso(), "scores": [], "complete": True, "epoch": 1}
    body = json.dumps(report).encode("utf-8")
    # Set cap to one byte less than the body
    cap = len(body) - 1
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", str(cap))
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
        }
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "external_scores_body_too_large"


def test_route_rejects_stream_oversize_attempt(client, monkeypatch):
    """Chunked/streaming payload exceeding cap is rejected with 413."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    # Cap at 512 bytes
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", "512")
    # Create a large payload (1 KiB), no Content-Length declared
    large_report = {
        "source": "violet_audio",
        "generated_at": _now_iso(),
        "scores": [{"miner_hotkey": f"hotkey_{i}", "score": 0.5} for i in range(20)],
        "complete": True,
        "epoch": 1,
    }
    body = json.dumps(large_report).encode("utf-8")
    # Verify the body is indeed larger than cap
    assert len(body) > 512, f"Test setup error: body size {len(body)} is not > 512"
    # Send without Content-Length to trigger streaming consumption
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
        }
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "external_scores_body_too_large"


def test_route_rejects_negative_content_length_with_400(client, monkeypatch):
    """Malformed negative Content-Length is rejected with 400."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "Content-Length": "-123",  # Malformed
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_content_length_negative"


def test_route_preserves_exact_bytes_for_json_and_hmac(client, monkeypatch):
    """Bounded body consumption preserves exact bytes for JSON parse and HMAC verification."""
    import hmac
    import hashlib
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_SAT_FAST", "sat-token")
    secret = "sat-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_SAT_FAST", secret)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", "10M")  # Large cap
    report = _sample_report("cathedral_sat_fast")
    body = json.dumps(report).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer sat-token",
            "X-Cathedral-External-Signature": f"sha256={sig}",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_env_bytes_parsing_with_suffix(monkeypatch):
    """Test that CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES parses suffixes correctly."""
    from scaffold.publisher.app import _env_bytes
    
    # Test 1M suffix
    monkeypatch.setenv("TEST_BYTES", "1M")
    assert _env_bytes("TEST_BYTES", 0) == 1024 * 1024
    
    # Test 1Mi suffix
    monkeypatch.setenv("TEST_BYTES", "1Mi")
    assert _env_bytes("TEST_BYTES", 0) == 1024 * 1024
    
    # Test 1MiB suffix
    monkeypatch.setenv("TEST_BYTES", "1MiB")
    assert _env_bytes("TEST_BYTES", 0) == 1024 * 1024
    
    # Test plain number
    monkeypatch.setenv("TEST_BYTES", "2048")
    assert _env_bytes("TEST_BYTES", 0) == 2048
    
    # Test missing/empty falls back to default
    monkeypatch.delenv("TEST_BYTES", raising=False)
    assert _env_bytes("TEST_BYTES", 999) == 999
    
    # Test default 1 MiB when env var is not set
    assert _env_bytes("TEST_BYTES_NONEXISTENT", 1024 * 1024) == 1024 * 1024
