"""One-shot signed remote weight vector verification for staging."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from cathedral.config import ValidatorSettings
from cathedral.policy.signing import (
    SignedWeightVector,
    VectorVerificationError,
    verify_vector,
)

WEIGHTS_NEXT_PATH = "/v1/validator/weights/next"


class RemoteWeightVectorFetchError(Exception):
    """Could not fetch or parse a staging remote weight vector."""


@dataclass(frozen=True)
class RemoteWeightVectorVerification:
    errors: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_remote_weight_public_key(
    settings: ValidatorSettings,
    env: Mapping[str, str] | None = None,
) -> tuple[Ed25519PublicKey | None, str | None]:
    values = os.environ if env is None else env
    raw = values.get(settings.remote_weight_source.public_key_env, "").strip()
    if not raw:
        return None, f"{settings.remote_weight_source.public_key_env} is required"
    try:
        key_bytes = bytes.fromhex(raw)
    except ValueError:
        return None, f"{settings.remote_weight_source.public_key_env} must be hex"
    if len(key_bytes) != 32:
        return None, f"{settings.remote_weight_source.public_key_env} must be 32 bytes"
    try:
        return Ed25519PublicKey.from_public_bytes(key_bytes), None
    except ValueError as exc:
        return None, f"{settings.remote_weight_source.public_key_env} is invalid: {exc}"


def verify_remote_weight_vector_for_settings(
    vector: SignedWeightVector,
    settings: ValidatorSettings,
    public_key: Ed25519PublicKey,
    *,
    now_iso: str | None = None,
) -> RemoteWeightVectorVerification:
    """Verify a fetched vector against validator config without touching chain state."""

    errors: list[str] = []
    now = now_iso or datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    try:
        verify_vector(
            vector,
            public_key=public_key,
            expected_key_id=settings.remote_weight_source.key_id,
        )
    except VectorVerificationError as exc:
        errors.append(str(exc))

    try:
        vector.invariant_check(
            network=settings.network.name,
            netuid=settings.network.netuid,
            now_iso=now,
        )
    except ValueError as exc:
        errors.append(str(exc))

    return RemoteWeightVectorVerification(
        errors=tuple(errors),
        details={
            "vector_id": vector.vector_id,
            "policy_version": vector.policy_version,
            "network": vector.network,
            "netuid": vector.netuid,
            "key_id": vector.key_id,
            "generated_at": vector.generated_at,
            "expires_at": vector.expires_at,
            "weight_entries": len(vector.weights),
            "burn_uid": vector.burn_snapshot.burn_uid,
            "forced_burn_percentage": vector.burn_snapshot.forced_burn_percentage,
            "policy_hash": vector.policy_hash,
            "policy_metadata": vector.policy_metadata,
        },
    )


async def fetch_remote_weight_vector_for_verification(
    client: httpx.AsyncClient,
    *,
    publisher_url: str,
) -> SignedWeightVector | None:
    """Fetch one signed vector from the publisher without importing chain code."""

    url = publisher_url.rstrip("/") + WEIGHTS_NEXT_PATH
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise RemoteWeightVectorFetchError(f"network error fetching {url}: {exc}") from exc
    if response.status_code == 503:
        return None
    if response.status_code != 200:
        raise RemoteWeightVectorFetchError(
            f"publisher returned {response.status_code} for {url}: {response.text[:200]!r}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RemoteWeightVectorFetchError(f"publisher response is not JSON: {exc}") from exc
    try:
        return SignedWeightVector.model_validate(body)
    except ValidationError as exc:
        raise RemoteWeightVectorFetchError(
            f"publisher response failed schema validation: {exc}"
        ) from exc


__all__ = [
    "RemoteWeightVectorFetchError",
    "RemoteWeightVectorVerification",
    "fetch_remote_weight_vector_for_verification",
    "load_remote_weight_public_key",
    "verify_remote_weight_vector_for_settings",
]
