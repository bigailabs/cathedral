"""Signed rules document surface for the v1.3 decentralized SAT lane.

PR1 only establishes the durable schema and read surface. Validators do not
consume these rules until the later Reward Engine phase.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from fastapi import APIRouter, HTTPException, Request

RULES_PRIVATE_KEY_ENV = "CATHEDRAL_RULES_PRIVATE_KEY"
RULES_KEY_ID_ENV = "CATHEDRAL_RULES_KEY_ID"
RULES_NETWORK_ENV = "CATHEDRAL_RULES_NETWORK"
RULES_NETUID_ENV = "CATHEDRAL_RULES_NETUID"
RULES_BURN_PERCENTAGE_ENV = "CATHEDRAL_RULES_BURN_PERCENTAGE"
RULES_TTL_SECONDS_ENV = "CATHEDRAL_RULES_TTL_SECONDS"
RULES_MIN_VALIDATOR_VERSION_ENV = "CATHEDRAL_RULES_MIN_VALIDATOR_VERSION"

DEFAULT_RULES_KEY_ID = "cathedral-rules-v1"
CANONICALIZATION_VERSION = "sorted-json-v1"

router = APIRouter()


@dataclass(frozen=True)
class SignedRules:
    body: dict[str, Any]
    signature: str
    key_id: str
    body_sha256: str
    canonicalization_version: str = CANONICALIZATION_VERSION

    def to_wire(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "signature": self.signature,
            "key_id": self.key_id,
            "body_sha256": self.body_sha256,
            "canonicalization_version": self.canonicalization_version,
        }


def canonical_rules_bytes(body: Mapping[str, Any]) -> bytes:
    """Canonical signing bytes for rules bodies.

    This intentionally matches the existing signed-weight convention:
    sorted keys, compact separators, UTF-8 bytes. The
    canonicalization_version is carried outside the body envelope.
    """

    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


def _private_key_from_hex(seed_hex: str) -> Ed25519PrivateKey:
    try:
        raw = bytes.fromhex(seed_hex)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise RuntimeError(f"{RULES_PRIVATE_KEY_ENV} must be a 32-byte Ed25519 seed hex") from exc


def sign_rules_body(
    body: Mapping[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = DEFAULT_RULES_KEY_ID,
) -> SignedRules:
    canonical = canonical_rules_bytes(body)
    signature = base64.b64encode(private_key.sign(canonical)).decode("ascii")
    return SignedRules(
        body=dict(body),
        signature=signature,
        key_id=key_id,
        body_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def verify_rules(
    rules: SignedRules,
    *,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
) -> None:
    if rules.key_id != expected_key_id:
        raise ValueError(f"key_id mismatch: rules={rules.key_id!r}, pinned={expected_key_id!r}")
    if rules.canonicalization_version != CANONICALIZATION_VERSION:
        raise ValueError(
            "canonicalization_version mismatch: "
            f"{rules.canonicalization_version!r} != {CANONICALIZATION_VERSION!r}"
        )
    canonical = canonical_rules_bytes(rules.body)
    if hashlib.sha256(canonical).hexdigest() != rules.body_sha256:
        raise ValueError("body_sha256 does not match canonical body")
    try:
        sig = base64.b64decode(rules.signature.encode("ascii"), validate=True)
        public_key.verify(sig, canonical)
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise ValueError("rules signature verification failed") from exc


async def publish_new_rules(
    conn: aiosqlite.Connection,
    body: Mapping[str, Any],
    private_key: Ed25519PrivateKey,
    *,
    key_id: str = DEFAULT_RULES_KEY_ID,
    published_at: datetime | None = None,
) -> int:
    """Publish a new active signed rules row.

    version_id is allocated before signing inside the transaction, then
    inserted as part of the signed body to avoid autoincrement/signing
    circularity.
    """

    now = published_at or datetime.now(UTC)
    published_iso = _ms_iso(now)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cur = await conn.execute("SELECT COALESCE(MAX(version_id), 0) + 1 FROM rules_versions")
        row = await cur.fetchone()
        version_id = int(row[0])
        signed_body = dict(body)
        signed_body["version_id"] = version_id
        signed_body.setdefault("published_at", published_iso)
        signed = sign_rules_body(signed_body, private_key, key_id=key_id)
        await conn.execute("UPDATE rules_versions SET active = 0 WHERE active = 1")
        await conn.execute(
            "INSERT INTO rules_versions ("
            "version_id, body_json, body_sha256, cathedral_sig, key_id, "
            "canonicalization_version, published_at, active"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (
                version_id,
                json.dumps(signed.body, sort_keys=True, separators=(",", ":")),
                signed.body_sha256,
                signed.signature,
                signed.key_id,
                signed.canonicalization_version,
                published_iso,
            ),
        )
        await conn.commit()
        return version_id
    except BaseException:
        await conn.rollback()
        raise


async def load_active_rules(conn: aiosqlite.Connection) -> SignedRules | None:
    cur = await conn.execute(
        "SELECT body_json, body_sha256, cathedral_sig, key_id, canonicalization_version "
        "FROM rules_versions WHERE active = 1 ORDER BY published_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return SignedRules(
        body=json.loads(str(row[0])),
        body_sha256=str(row[1]),
        signature=str(row[2]),
        key_id=str(row[3]),
        canonicalization_version=str(row[4]),
    )


def _parse_expires_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _rules_current_for_env(
    signed: SignedRules,
    values: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether a persisted rules row is safe to serve now.

    Active rows are durable, but serving them is conditional on the live
    signing-key configuration and the signed body's TTL. This prevents a
    removed/rotated key or expired policy from lingering in the public API
    until an operator manually rewrites the database.
    """

    seed_hex = values.get(RULES_PRIVATE_KEY_ENV, "").strip()
    if not seed_hex:
        return False
    key_id = values.get(RULES_KEY_ID_ENV, DEFAULT_RULES_KEY_ID)
    private_key = _private_key_from_hex(seed_hex)
    try:
        verify_rules(
            signed,
            public_key=private_key.public_key(),
            expected_key_id=key_id,
        )
    except ValueError:
        return False
    expires_at = _parse_expires_at(signed.body.get("expires_at"))
    if expires_at is None:
        return False
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    return expires_at > current_time.astimezone(UTC)


def build_default_rules_body(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    values = os.environ if env is None else env
    now = datetime.now(UTC)
    ttl_seconds = int(values.get(RULES_TTL_SECONDS_ENV, "3600"))
    network = values.get(RULES_NETWORK_ENV, "finney")
    netuid = int(values.get(RULES_NETUID_ENV, "39"))
    return {
        "network": network,
        "netuid": netuid,
        "expires_at": _ms_iso(now + timedelta(seconds=ttl_seconds)),
        "burn_percentage": float(values.get(RULES_BURN_PERCENTAGE_ENV, "85.0")),
        "tier_budgets": {"1": 0.10, "2": 0.25, "3": 0.65},
        "eligibility": {},
        "eligible_challenges": [],
        "denylist": [],
        "kill_switches": {"emergency_use_publisher_vector": False},
        "min_validator_version": values.get(RULES_MIN_VALIDATOR_VERSION_ENV, "v1.3.0"),
        "bounty_rules": {},
    }


async def ensure_bootstrap_rules_from_env(
    conn: aiosqlite.Connection,
    *,
    env: Mapping[str, str] | None = None,
) -> int | None:
    """Publish v1 rules only when a rules signing key is configured."""

    values = os.environ if env is None else env
    signed = await load_active_rules(conn)
    if signed is not None and _rules_current_for_env(signed, values):
        return None
    seed_hex = values.get(RULES_PRIVATE_KEY_ENV, "").strip()
    if not seed_hex:
        return None
    key_id = values.get(RULES_KEY_ID_ENV, DEFAULT_RULES_KEY_ID)
    private_key = _private_key_from_hex(seed_hex)
    return await publish_new_rules(
        conn,
        build_default_rules_body(values),
        private_key,
        key_id=key_id,
    )


async def load_current_rules_from_env(
    conn: aiosqlite.Connection,
    *,
    env: Mapping[str, str] | None = None,
) -> SignedRules | None:
    """Load a current active rules document, refreshing stale rows if possible."""

    values = os.environ if env is None else env
    signed = await load_active_rules(conn)
    if signed is not None and _rules_current_for_env(signed, values):
        return signed
    await ensure_bootstrap_rules_from_env(conn, env=values)
    signed = await load_active_rules(conn)
    if signed is None or not _rules_current_for_env(signed, values):
        return None
    return signed


@router.get("/v1/rules/active")
async def get_active_rules(request: Request) -> dict[str, Any]:
    ctx = request.app.state.ctx
    signed = await load_current_rules_from_env(ctx.db)
    if signed is None:
        raise HTTPException(status_code=503, detail="no active rules document available")
    return signed.to_wire()
