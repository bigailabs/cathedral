"""Signed weight-vector schema and Ed25519 helpers.

This is the corrected #155 contract. The publisher signs hotkey weights.
Validators verify the envelope, map hotkeys to live UIDs locally, drop
missing hotkeys, renormalize, and call set_weights on their configured
chain cadence. The burn target is a hotkey in weights_by_hotkey. The
optional burn_uid_snapshot is only audit context from the publisher's
metagraph view.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_VECTOR_HOTKEYS = 8192


class SignedWeightVector(BaseModel):
    """Signed publisher policy vector served to remote-mode validators."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    policy_version: int = Field(ge=0)
    vector_id: str = Field(min_length=1)
    issued_at: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    network: str = Field(min_length=1)
    netuid: int = Field(ge=0)
    metagraph_block: int = Field(ge=0)
    burn_hotkey: str = Field(min_length=1)
    burn_uid_snapshot: int | None = Field(default=None, ge=0)
    weights_by_hotkey: dict[str, float] = Field(default_factory=dict)
    policy_hash: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    signature: str | None = None

    @field_validator("weights_by_hotkey")
    @classmethod
    def _weights_valid(cls, v: dict[str, float]) -> dict[str, float]:
        if len(v) > MAX_VECTOR_HOTKEYS:
            raise ValueError(f"weights_by_hotkey exceeds MAX_VECTOR_HOTKEYS={MAX_VECTOR_HOTKEYS}")
        cleaned: dict[str, float] = {}
        for hotkey, weight in v.items():
            if not str(hotkey).strip():
                raise ValueError("weights_by_hotkey contains a blank hotkey")
            if not math.isfinite(weight):
                raise ValueError(f"weight for hotkey {hotkey!r} must be finite")
            if weight < 0.0:
                raise ValueError(f"weight for hotkey {hotkey!r} must be nonnegative")
            cleaned[str(hotkey)] = float(weight)
        return cleaned

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def invariant_check(
        self,
        *,
        network: str,
        netuid: int,
        now: datetime | None = None,
        require_unexpired: bool = True,
    ) -> None:
        """Raise ValueError when local validator invariants fail."""
        now = now or datetime.now(UTC)
        issued = parse_iso_utc(self.issued_at)
        expires = parse_iso_utc(self.expires_at)
        if expires <= issued:
            raise ValueError("expires_at must be after issued_at")
        if require_unexpired and expires <= now:
            raise ValueError(f"vector expired: expires_at={self.expires_at!r}")
        if self.network != network:
            raise ValueError(f"network mismatch: vector={self.network!r}, validator={network!r}")
        if self.netuid != netuid:
            raise ValueError(f"netuid mismatch: vector={self.netuid!r}, validator={netuid!r}")
        if self.burn_hotkey not in self.weights_by_hotkey:
            raise ValueError("burn_hotkey must appear in weights_by_hotkey")
        total = sum(self.weights_by_hotkey.values())
        if total <= 0.0:
            raise ValueError("weights_by_hotkey must sum to a positive value")
        if not math.isclose(total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError(f"weights_by_hotkey must sum to 1.0, got {total!r}")


def parse_iso_utc(value: str) -> datetime:
    """Parse ISO-8601 UTC with trailing Z or an explicit UTC offset."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, millisecond precision, trailing Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def canonical_bytes(vector: SignedWeightVector | dict[str, Any]) -> bytes:
    """Canonical bytes for Ed25519 signing.

    Only signature is excluded. All other fields are signed using sorted
    keys and compact separators.
    """
    if isinstance(vector, SignedWeightVector):
        payload = vector.model_dump(mode="json")
    else:
        payload = dict(vector)
    payload.pop("signature", None)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def sign_vector(vector: SignedWeightVector, private_key: Ed25519PrivateKey) -> SignedWeightVector:
    payload = vector.model_dump(mode="json")
    payload.pop("signature", None)
    signature = private_key.sign(canonical_bytes(payload))
    payload["signature"] = base64.b64encode(signature).decode("ascii")
    return SignedWeightVector.model_validate(payload)


class VectorVerificationError(Exception):
    """The vector failed key-id or Ed25519 signature verification."""


def verify_vector(
    vector: SignedWeightVector,
    *,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
) -> None:
    if vector.key_id != expected_key_id:
        raise VectorVerificationError(
            f"key_id mismatch: vector={vector.key_id!r}, pinned={expected_key_id!r}"
        )
    if not vector.signature:
        raise VectorVerificationError("vector is missing signature")
    try:
        signature = base64.b64decode(vector.signature.encode("ascii"), validate=True)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise VectorVerificationError(f"signature is not valid base64: {exc}") from exc
    try:
        public_key.verify(signature, canonical_bytes(vector))
    except InvalidSignature as exc:
        raise VectorVerificationError("ed25519 signature verify failed") from exc
