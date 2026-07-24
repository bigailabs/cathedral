"""Route-level tests for POST /v1/external-scores/violet source-scoped auth.

These tests verify the actual HTTP route behavior: parsing source, enforcing
dedicated vs. shared token boundaries, and proper 401/503 error codes.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from scaffold.publisher import app as app_mod, external_scores, weights


def _now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _sample_report(source: str = "cathedral_sat_fast") -> dict:
    report = {
        "source": source,
        "generated_at": _now_iso(),
        "scores": [
            {"miner_hotkey": "5Alice", "score": 0.5},
        ],
        "complete": True,
        "epoch": 1,
    }
    if source == "cathedral_confidential_tdx":
        report["network"] = "finney"
        report["netuid"] = 39
    return report


def _tdx_auth(monkeypatch):
    token = "tdx-token"
    secret = "tdx-hmac-secret"
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX",
        token,
    )
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX",
        secret,
    )
    return token, secret


def _tdx_headers(token: str, secret: str, body: bytes) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "X-Cathedral-External-Signature": f"sha256={signature}",
    }


def _freeze_external_scores_now(monkeypatch, now: datetime):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls.current.replace(tzinfo=None)
            return cls.current.astimezone(tz)

    FrozenDateTime.current = now
    monkeypatch.setattr(external_scores, "datetime", FrozenDateTime)
    return FrozenDateTime


@pytest.fixture
def client(monkeypatch):
    """Build the app with external scores enabled but no weights blending yet."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETWORK", "finney")
    monkeypatch.setenv("CATHEDRAL_WEIGHT_POLICY_NETUID", "39")
    # Weights blending OFF by default in these tests (we test it separately).
    monkeypatch.delenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", raising=False)
    return TestClient(app_mod.build_app())


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("network", "test", "score_audience_mismatch"),
        ("netuid", 40, "score_audience_mismatch"),
        ("netuid", True, "invalid_score_audience"),
    ],
)
def test_confidential_route_rejects_wrong_audience_without_advancing_epoch(
    client, monkeypatch, field, value, detail
):
    token, secret = _tdx_auth(monkeypatch)
    wrong = _sample_report("cathedral_confidential_tdx")
    wrong["epoch"] = 100
    wrong[field] = value
    wrong_body = json.dumps(wrong).encode("utf-8")
    rejected = client.post(
        "/v1/external-scores/violet",
        content=wrong_body,
        headers=_tdx_headers(token, secret, wrong_body),
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"] == detail

    local = _sample_report("cathedral_confidential_tdx")
    local_body = json.dumps(local).encode("utf-8")
    accepted = client.post(
        "/v1/external-scores/violet",
        content=local_body,
        headers=_tdx_headers(token, secret, local_body),
    )
    assert accepted.status_code == 202
    assert accepted.json()["epoch"] == 1


def test_confidential_route_fails_closed_when_local_audience_is_unconfigured(
    client, monkeypatch
):
    token, secret = _tdx_auth(monkeypatch)
    monkeypatch.delenv("CATHEDRAL_WEIGHT_POLICY_NETWORK")
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report).encode("utf-8")
    response = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(token, secret, body),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "score_audience_not_configured"


def test_legacy_unscoped_high_epoch_cannot_block_or_feed_local_audience(
    client, monkeypatch
):
    token, secret = _tdx_auth(monkeypatch)
    store = client.app.state.store
    legacy_json = json.dumps(
        {
            "complete": True,
            "epoch": 999,
            "generated_at": _now_iso(),
            "scores": [{"miner_hotkey": "legacy", "score": 1.0}],
            "source": "cathedral_confidential_tdx",
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    def insert_legacy(conn):
        conn.execute(
            "INSERT INTO external_score_reports"
            "(id, source, epoch, generated_at_iso, received_at_iso, "
            "report_sha256, score_count, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-unscoped",
                "cathedral_confidential_tdx",
                999,
                _now_iso(),
                _now_iso(),
                "0" * 64,
                1,
                legacy_json,
            ),
        )

    store.write(insert_legacy)
    local = _sample_report("cathedral_confidential_tdx")
    local_body = json.dumps(local).encode("utf-8")
    accepted = client.post(
        "/v1/external-scores/violet",
        content=local_body,
        headers=_tdx_headers(token, secret, local_body),
    )
    assert accepted.status_code == 202
    assert external_scores.latest_snapshot_scores(
        store,
        source="cathedral_confidential_tdx",
    ) == {"5Alice": 0.5}


@pytest.fixture
def accepted_tdx_report(client, monkeypatch):
    now = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)
    clock = _freeze_external_scores_now(monkeypatch, now)
    token, secret = _tdx_auth(monkeypatch)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_AGE_SECS", "3600")
    report = _sample_report("cathedral_confidential_tdx")
    report["generated_at"] = _iso(now)
    body = json.dumps(report).encode("utf-8")

    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(token, secret, body),
    )
    assert resp.status_code == 202
    assert resp.json()["idempotent"] is False

    return {
        "body": body,
        "clock": clock,
        "report": report,
        "response": resp.json(),
        "secret": secret,
        "token": token,
    }


def test_confidential_store_rejects_unbound_normalized_report(
    client,
):
    report = external_scores.normalize_report(
        _sample_report("cathedral_confidential_tdx")
    )

    with pytest.raises(
        external_scores.ExternalScoreError,
        match="authenticated_body_digest_required",
    ):
        external_scores.store_report(client.app.state.store, report)


def test_confidential_route_persists_exact_body_digest_and_signs_it(
    client,
    monkeypatch,
):
    token, secret = _tdx_auth(monkeypatch)
    report = _sample_report("cathedral_confidential_tdx")
    body = json.dumps(report, indent=2, sort_keys=False).encode("utf-8")
    expected = hashlib.sha256(body).hexdigest()

    response = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(token, secret, body),
    )

    assert response.status_code == 202
    store = client.app.state.store
    rows = store.query(
        "SELECT report_json FROM external_score_reports "
        "WHERE source=? AND network=? AND netuid=? AND epoch=?",
        ("cathedral_confidential_tdx", "finney", 39, report["epoch"]),
    )
    assert json.loads(rows[0]["report_json"])["body_sha256"] == expected
    status = external_scores.status(
        store,
        source="cathedral_confidential_tdx",
        since_iso=_iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
    )
    assert status["latest_body_sha256"] == expected

    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_SOURCE",
        "cathedral_confidential_tdx",
    )
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MODE", "confidential_primary")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_PRIMARY_CONFIRM", "true")
    vector = weights.build_signed_vector(
        store,
        signing_key_hex="11" * 32,
        now=datetime.now(timezone.utc),
    )
    assert (
        vector["policy_metadata"]["external_scores"]["latest_body_sha256"]
        == expected
    )


def test_confidential_idempotent_reencoding_keeps_original_body_digest(
    client,
    accepted_tdx_report,
):
    original_digest = hashlib.sha256(accepted_tdx_report["body"]).hexdigest()
    reencoded = json.dumps(
        json.loads(accepted_tdx_report["body"]),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert reencoded != accepted_tdx_report["body"]

    response = client.post(
        "/v1/external-scores/violet",
        content=reencoded,
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            reencoded,
        ),
    )

    assert response.status_code == 202
    assert response.json()["idempotent"] is True
    stored = client.app.state.store.query(
        "SELECT report_json FROM external_score_reports "
        "WHERE source=? AND network=? AND netuid=? AND epoch=?",
        ("cathedral_confidential_tdx", "finney", 39, 1),
    )
    assert json.loads(stored[0]["report_json"])["body_sha256"] == original_digest


def test_confidential_same_epoch_semantic_change_is_conflict(
    client,
    accepted_tdx_report,
):
    changed = json.loads(accepted_tdx_report["body"])
    changed["scores"][0]["score"] = 0.75
    body = json.dumps(changed).encode("utf-8")

    response = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            body,
        ),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "epoch_conflict"


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
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", "512")
    report = _sample_report("violet_audio")
    body = json.dumps(report).encode("utf-8")
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer shared-token",
            "Content-Length": str(2048),
        }
    )
    assert resp.status_code == 413
    assert resp.json()["detail"] == "external_scores_body_too_large"


def test_route_accepts_exact_cap_boundary(client, monkeypatch):
    """Payload exactly at cap is accepted."""
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")
    report = {"source": "violet_audio", "generated_at": _now_iso(), "scores": [], "complete": True, "epoch": 1}
    body = json.dumps(report).encode("utf-8")
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
    report = {"source": "violet_audio", "generated_at": _now_iso(), "scores": [], "complete": True, "epoch": 1}
    body = json.dumps(report).encode("utf-8")
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
            "Content-Length": "-123",
        }
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_content_length_negative"


def test_bounded_body_counts_asgi_chunks_despite_small_content_length():
    """Actual ASGI chunks, not a misleading Content-Length, enforce the cap."""
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"1"),
        ],
    }
    chunks = (b"x" * 128, b"y" * 129)
    chunk_index = 0

    async def receive():
        nonlocal chunk_index
        chunk = chunks[chunk_index]
        chunk_index += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": chunk_index < len(chunks),
        }

    async def read_body():
        request = Request(scope, receive=receive)
        return await app_mod._read_bounded_body(request, 256)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(read_body())

    assert chunk_index == 2
    assert exc_info.value.status_code == 413
    assert exc_info.value.detail == "external_scores_body_too_large"


@pytest.mark.parametrize("body", [b"", b'{"source":'])
def test_route_rejects_missing_or_malformed_body(client, monkeypatch, body):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared-token")

    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={"Authorization": "Bearer shared-token"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_json_report"


def test_route_preserves_exact_bytes_for_json_and_hmac(client, monkeypatch):
    """Bounded body consumption preserves exact bytes for HMAC with cathedral_confidential_tdx.

    Uses cathedral_confidential_tdx with dedicated bearer and HMAC secret.
    A modified body or signature will fail verification, proving HMAC is active.
    """
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX", "tdx-token")
    secret = "tdx-hmac-secret"
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX", secret)
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES", "10M")
    generated_at = _now_iso()
    body = (
        b'{\n  "scores": [{"score": 0.5, "miner_hotkey": "5Alice"}],\n'
        b'  "epoch": 1,\n  "source": "cathedral_confidential_tdx",\n'
        b'  "network": "finney",\n  "netuid": 39,\n  "complete": true,\n'
        b'  "generated_at": "'
        + generated_at.encode("utf-8")
        + b'"\n}'
    )
    assert json.dumps(json.loads(body)).encode("utf-8") != body
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer tdx-token",
            "X-Cathedral-External-Signature": f"sha256={sig}",
        }
    )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.json()}"


def test_route_accepts_exact_authenticated_tdx_retry_after_report_ages_out(
    client,
    accepted_tdx_report,
):
    accepted_tdx_report["clock"].current += timedelta(seconds=3601)

    resp = client.post(
        "/v1/external-scores/violet",
        content=accepted_tdx_report["body"],
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            accepted_tdx_report["body"],
        ),
    )

    assert resp.status_code == 202
    assert resp.json()["idempotent"] is True
    assert resp.json().keys() == accepted_tdx_report["response"].keys()
    for key in ("status", "report_id", "source", "epoch", "score_count", "report_sha256"):
        assert resp.json()[key] == accepted_tdx_report["response"][key]


def test_route_preserves_exact_monotonic_epoch_rejection(client, accepted_tdx_report):
    newer = dict(accepted_tdx_report["report"])
    newer["epoch"] = int(newer["epoch"]) + 1
    newer_body = json.dumps(newer).encode("utf-8")
    newer_response = client.post(
        "/v1/external-scores/violet",
        content=newer_body,
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            newer_body,
        ),
    )
    assert newer_response.status_code == 202

    replay = client.post(
        "/v1/external-scores/violet",
        content=accepted_tdx_report["body"],
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            accepted_tdx_report["body"],
        ),
    )

    assert replay.status_code == 409
    assert replay.json()["detail"] == "epoch_too_old"
    latest = client.app.state.store.query(
        "SELECT epoch, report_sha256 FROM external_score_reports "
        "WHERE source=? ORDER BY epoch DESC LIMIT 1",
        ("cathedral_confidential_tdx",),
    )
    assert int(latest[0]["epoch"]) == newer["epoch"]
    assert latest[0]["report_sha256"] == newer_response.json()["report_sha256"]


def test_route_rejects_stale_authenticated_tdx_new_epoch(client, accepted_tdx_report):
    accepted_tdx_report["clock"].current += timedelta(seconds=3601)
    report = dict(accepted_tdx_report["report"])
    report["epoch"] += 1
    body = json.dumps(report).encode("utf-8")

    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            body,
        ),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "report_too_old"


def test_route_rejects_stale_authenticated_tdx_same_epoch_different_digest(
    client,
    accepted_tdx_report,
):
    accepted_tdx_report["clock"].current += timedelta(seconds=3601)
    report = json.loads(accepted_tdx_report["body"])
    report["scores"][0]["score"] = 0.75
    body = json.dumps(report).encode("utf-8")

    resp = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers=_tdx_headers(
            accepted_tdx_report["token"],
            accepted_tdx_report["secret"],
            body,
        ),
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "report_too_old"


def test_route_authenticates_stale_tdx_retry_before_idempotency(
    client,
    accepted_tdx_report,
):
    accepted_tdx_report["clock"].current += timedelta(seconds=3601)
    body = accepted_tdx_report["body"]
    signature = hmac.new(
        accepted_tdx_report["secret"].encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()

    missing_bearer = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={"X-Cathedral-External-Signature": f"sha256={signature}"},
    )
    bad_hmac = client.post(
        "/v1/external-scores/violet",
        content=body,
        headers={
            "Authorization": f"Bearer {accepted_tdx_report['token']}",
            "X-Cathedral-External-Signature": f"sha256={'0' * 64}",
        },
    )

    assert missing_bearer.status_code == 401
    assert missing_bearer.json()["detail"] == "invalid_external_scores_token"
    assert bad_hmac.status_code == 401
    assert bad_hmac.json()["detail"] == "invalid_external_scores_signature"


def test_route_env_bytes_parsing_with_suffix(monkeypatch):
    """Test that CATHEDRAL_EXTERNAL_SCORES_MAX_BODY_BYTES parses suffixes correctly."""
    from scaffold.publisher.app import _env_bytes

    monkeypatch.setenv("TEST_BYTES", "1M")
    assert _env_bytes("TEST_BYTES", 999) == 1024 * 1024

    monkeypatch.setenv("TEST_BYTES", "1Mi")
    assert _env_bytes("TEST_BYTES", 999) == 1024 * 1024

    monkeypatch.setenv("TEST_BYTES", "1MiB")
    assert _env_bytes("TEST_BYTES", 999) == 1024 * 1024

    monkeypatch.setenv("TEST_BYTES", "2048")
    assert _env_bytes("TEST_BYTES", 999) == 2048

    for invalid in ("", "bogus", "0", "-1", "0M", "-2MiB"):
        monkeypatch.setenv("TEST_BYTES", invalid)
        assert _env_bytes("TEST_BYTES", 999) == 999

    monkeypatch.delenv("TEST_BYTES", raising=False)
    assert _env_bytes("TEST_BYTES", 999) == 999
    assert _env_bytes("TEST_BYTES_NONEXISTENT", 1024 * 1024) == 1024 * 1024
    assert _env_bytes("TEST_BYTES_NONEXISTENT", 0) == 1
