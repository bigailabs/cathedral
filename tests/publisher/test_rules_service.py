from __future__ import annotations

import hashlib
import sqlite3

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cathedral.publisher import repository
from cathedral.publisher.app import build_app
from cathedral.publisher.rules import (
    RULES_PRIVATE_KEY_ENV,
    build_default_rules_body,
    canonical_rules_bytes,
    load_active_rules,
    publish_new_rules,
    verify_rules,
)


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
