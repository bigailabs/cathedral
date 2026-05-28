from __future__ import annotations

import hashlib
import json
import sqlite3

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cathedral.publisher import repository
from cathedral.publisher.app import build_app, latest_ctx
from cathedral.publisher.rules import (
    RULES_KEY_ID_ENV,
    RULES_PRIVATE_KEY_ENV,
    SignedRules,
    build_default_rules_body,
    canonical_rules_bytes,
    load_active_rules,
    publish_new_rules,
    sign_rules_body,
    verify_rules,
)


def _insert_signed_rules(
    conn: sqlite3.Connection,
    body: dict[str, object],
    key: Ed25519PrivateKey,
    *,
    key_id: str,
) -> None:
    signed = sign_rules_body(body, key, key_id=key_id)
    conn.executescript(repository.RULES_VERSIONS_SCHEMA)
    conn.execute(
        "INSERT INTO rules_versions ("
        "version_id, body_json, body_sha256, cathedral_sig, key_id, "
        "canonicalization_version, published_at, active"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            int(body["version_id"]),
            json_dumps(signed.body),
            signed.body_sha256,
            signed.signature,
            signed.key_id,
            signed.canonicalization_version,
            str(body["published_at"]),
        ),
    )
    conn.commit()


def json_dumps(body: dict[str, object]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


class _TrackingAsyncLock:
    def __init__(self) -> None:
        self.enter_count = 0

    async def __aenter__(self) -> _TrackingAsyncLock:
        self.enter_count += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_publish_rules_allocates_version_before_signing(tmp_path) -> None:
    db = await aiosqlite.connect(tmp_path / "publisher.db")
    try:
        await db.executescript(repository.RULES_VERSIONS_SCHEMA)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("11" * 32))

        first = await publish_new_rules(
            db,
            {
                "network": "finney",
                "netuid": 39,
                "expires_at": "2026-05-28T12:30:00.000Z",
                "burn_percentage": 85.0,
                "tier_budgets": {"1": 1.0},
                "eligibility": {},
                "eligible_challenges": [],
                "denylist": [],
                "kill_switches": {"emergency_use_publisher_vector": False},
                "min_validator_version": "v1.3.0",
                "bounty_rules": {},
            },
            key,
            key_id="rules-test",
        )
        second = await publish_new_rules(
            db,
            build_default_rules_body({"CATHEDRAL_RULES_TTL_SECONDS": "60"}),
            key,
            key_id="rules-test",
        )
        active = await load_active_rules(db)

        assert first == 1
        assert second == 2
        assert active is not None
        assert active.body["version_id"] == 2
        assert active.key_id == "rules-test"
        assert active.body_sha256 == hashlib.sha256(canonical_rules_bytes(active.body)).hexdigest()
        verify_rules(active, public_key=key.public_key(), expected_key_id="rules-test")
    finally:
        await db.close()


def test_verify_rules_rejects_malformed_base64_signature() -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("55" * 32))
    body = build_default_rules_body({"CATHEDRAL_RULES_TTL_SECONDS": "60"})
    body["version_id"] = 1
    signed = sign_rules_body(body, key, key_id="rules-test")
    malformed = SignedRules(
        body=signed.body,
        signature="not base64!!!",
        key_id=signed.key_id,
        body_sha256=signed.body_sha256,
        canonicalization_version=signed.canonicalization_version,
    )

    with pytest.raises(ValueError, match="rules signature verification failed"):
        verify_rules(malformed, public_key=key.public_key(), expected_key_id="rules-test")


def test_rules_schema_allows_only_one_active_row(tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "publisher.db")
    try:
        conn.executescript(repository.RULES_VERSIONS_SCHEMA)
        conn.execute(
            "INSERT INTO rules_versions ("
            "version_id, body_json, body_sha256, cathedral_sig, key_id, "
            "canonicalization_version, published_at, active"
            ") VALUES (1, '{}', 'a', 'sig', 'kid', 'sorted-json-v1', 'now', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rules_versions ("
                "version_id, body_json, body_sha256, cathedral_sig, key_id, "
                "canonicalization_version, published_at, active"
                ") VALUES (2, '{}', 'b', 'sig', 'kid', 'sorted-json-v1', 'now', 1)"
            )
    finally:
        conn.close()


def test_rules_route_503_without_signing_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(RULES_PRIVATE_KEY_ENV, raising=False)

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        for path in ("/v1/rules/active", "/api/cathedral/v1/rules/active"):
            response = client.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "no active rules document available"}


def test_rules_route_503_when_persisted_rules_key_removed(tmp_path, monkeypatch) -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("33" * 32))
    db_path = tmp_path / "publisher.db"
    conn = sqlite3.connect(db_path)
    try:
        body = build_default_rules_body({"CATHEDRAL_RULES_TTL_SECONDS": "3600"})
        body["version_id"] = 1
        body["published_at"] = "2026-05-28T12:00:00.000Z"
        _insert_signed_rules(conn, body, key, key_id="removed-key")
    finally:
        conn.close()

    monkeypatch.delenv(RULES_PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(RULES_KEY_ID_ENV, raising=False)

    app = build_app(str(db_path))
    with TestClient(app) as client:
        response = client.get("/v1/rules/active")

    assert response.status_code == 503
    assert response.json() == {"detail": "no active rules document available"}


def test_rules_route_refreshes_expired_persisted_rules(tmp_path, monkeypatch) -> None:
    key_hex = "44" * 32
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_hex))
    db_path = tmp_path / "publisher.db"
    conn = sqlite3.connect(db_path)
    try:
        body = build_default_rules_body({"CATHEDRAL_RULES_TTL_SECONDS": "3600"})
        body["version_id"] = 1
        body["published_at"] = "2026-05-28T12:00:00.000Z"
        body["expires_at"] = "2000-01-01T00:00:00.000Z"
        _insert_signed_rules(conn, body, key, key_id="route-rules")
    finally:
        conn.close()

    monkeypatch.setenv(RULES_PRIVATE_KEY_ENV, key_hex)
    monkeypatch.setenv(RULES_KEY_ID_ENV, "route-rules")
    monkeypatch.setenv("CATHEDRAL_RULES_TTL_SECONDS", "120")

    app = build_app(str(db_path))
    with TestClient(app) as client:
        response = client.get("/v1/rules/active")

    assert response.status_code == 200
    payload = response.json()
    assert payload["key_id"] == "route-rules"
    assert payload["body"]["version_id"] == 2
    assert payload["body"]["expires_at"] != "2000-01-01T00:00:00.000Z"


def test_rules_route_refresh_uses_publisher_write_lock(tmp_path, monkeypatch) -> None:
    key_hex = "66" * 32
    monkeypatch.delenv(RULES_PRIVATE_KEY_ENV, raising=False)
    monkeypatch.delenv(RULES_KEY_ID_ENV, raising=False)

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        ctx = latest_ctx()
        assert ctx is not None
        tracking_lock = _TrackingAsyncLock()
        ctx.db_write_lock = tracking_lock
        monkeypatch.setenv(RULES_PRIVATE_KEY_ENV, key_hex)
        monkeypatch.setenv(RULES_KEY_ID_ENV, "route-rules")

        response = client.get("/v1/rules/active")

    assert response.status_code == 200
    assert tracking_lock.enter_count == 1


def test_rules_route_bootstraps_when_signing_key_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(RULES_PRIVATE_KEY_ENV, "22" * 32)
    monkeypatch.setenv("CATHEDRAL_RULES_KEY_ID", "route-rules")
    monkeypatch.setenv("CATHEDRAL_RULES_TTL_SECONDS", "120")

    app = build_app(str(tmp_path / "publisher.db"))
    with TestClient(app) as client:
        response = client.get("/v1/rules/active")

        assert response.status_code == 200
        payload = response.json()
        assert payload["key_id"] == "route-rules"
        assert payload["canonicalization_version"] == "sorted-json-v1"
        assert payload["signature"]
        assert payload["body"]["version_id"] == 1
        assert payload["body"]["network"] == "finney"
        assert payload["body"]["netuid"] == 39
        assert payload["body"]["kill_switches"] == {
            "emergency_use_publisher_vector": False
        }
        assert "bounty_rules" in payload["body"]
