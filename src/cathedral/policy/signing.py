"""SignedWeightVector wire schema, canonical bytes, sign + verify.

The signed payload is what the publisher emits and what the validator
verifies. ``policy_version`` is a monotonically increasing publisher
counter used for durable rollback protection on the validator side. It
is NOT used as the chain ``version_key`` (which stays pinned to
``cathedral.SPEC_VERSION``).

Canonical bytes: ``signature`` is excluded, keys are sorted, no
whitespace, UTF-8. ``key_id`` is included in the signed bytes so a
validator that pinned key_id ``A`` will reject a vector signed with key
``B`` even if both keys are technically known.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sentinel max vector length. Bittensor metagraph caps subnet uid count
# in the low thousands; allow comfortably more so the schema does not
# need a bump when the subnet grows. Validators apply their own per-call
# bounds against the live metagraph.
MAX_VECTOR_ENTRIES = 8192

# Excluded from canonical_bytes. Anything appended after signing must be
# listed here or signatures will not round-trip.
_SIGNATURE_EXCLUDED_KEYS = frozenset({"signature"})


class WeightEntry(BaseModel):
    """One (miner_hotkey, weight) row inside a SignedWeightVector.

    Hotkey, not uid, so the publisher does not need to know the
    validator's local metagraph snapshot. The validator maps hotkey to
    uid against its own metagraph at apply time.
    """

    model_config = ConfigDict(extra="forbid")

    miner_hotkey: str = Field(min_length=1)
    weight: float

    @field_validator("weight")
    @classmethod
    def _weight_finite_nonneg(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"weight must be finite, got {v!r}")
        if v < 0:
            raise ValueError(f"weight must be nonnegative, got {v!r}")
        return v


class BurnSnapshot(BaseModel):
    """Signed burn policy applied by the validator at weight-set time."""

    model_config = ConfigDict(extra="forbid")

    burn_uid: int | None = Field(default=None, ge=0)
    forced_burn_percentage: float = Field(ge=0.0, le=100.0)


class SignedWeightVector(BaseModel):
    """A signed weight policy emission from the publisher.

    Wire shape served by ``GET /v1/validator/weights/next`` and consumed
    by the validator's remote weight loop. The fields below are all
    inputs to ``canonical_bytes`` (everything except ``signature``).

    * ``vector_id``: opaque unique id for this emission. The validator
      uses this for last-known-good / "did I already apply this?"
      bookkeeping so a publisher that re-serves the same vector does
      not cause repeated chain submissions.
    * ``policy_version``: monotonically increasing counter owned by the
      publisher. Used by the validator for durable rollback protection
      (refuse a vector whose policy_version is older than the last
      accepted one). NOT the chain ``version_key``.
    * ``generated_at``: emission timestamp, ms-precision ISO-8601 UTC.
    * ``expires_at``: must be strictly after generated_at. The validator
      refuses vectors whose expires_at is in the past at verification
      time.
    * ``network`` + ``netuid``: who this vector targets. The validator
      refuses a vector that does not match its local chain config.
    * ``key_id``: which signing key produced this vector. The validator
      pins ``key_id`` and refuses any other.
    * ``burn_snapshot``: signed burn policy applied after mapping hotkeys
      to uids. This keeps remote mode from silently bypassing the
      protective burn policy.
    * ``policy_hash``: deterministic hash of the publisher policy input.
    * ``policy_reason`` + ``policy_metadata``: operator-readable audit
      context for why this vector exists and what inputs produced it.
    * ``weights``: the (hotkey, weight) entries. Per
      ``invariant_check``: every weight finite + nonnegative, vector
      size <= ``MAX_VECTOR_ENTRIES``. A zero-sum or empty miner vector
      is only valid when ``burn_uid`` is present, because that is the
      explicit signed fallback target.
    """

    model_config = ConfigDict(extra="forbid")

    vector_id: str = Field(min_length=1)
    policy_version: int = Field(ge=0)
    network: str = Field(min_length=1)
    netuid: int = Field(ge=0)
    generated_at: str = Field(min_length=1)
    expires_at: str = Field(min_length=1)
    burn_snapshot: BurnSnapshot
    policy_hash: str = Field(min_length=1)
    key_id: str = Field(min_length=1)
    policy_reason: str = Field(default="publisher_current_ranked_scores", min_length=1)
    policy_metadata: dict[str, Any] = Field(default_factory=dict)
    weights: list[WeightEntry] = Field(default_factory=list)
    signature: str | None = None

    @field_validator("weights")
    @classmethod
    def _weights_bounded(cls, v: list[WeightEntry]) -> list[WeightEntry]:
        if len(v) > MAX_VECTOR_ENTRIES:
            raise ValueError(
                f"weights vector exceeds MAX_VECTOR_ENTRIES={MAX_VECTOR_ENTRIES} (got {len(v)})"
            )
        return v

    def to_payload(self) -> dict[str, Any]:
        """Return the wire-shaped dict (includes ``signature`` if set)."""
        return self.model_dump(mode="json")

    def invariant_check(self, *, network: str, netuid: int, now_iso: str) -> None:
        """Raise ``ValueError`` if any structural invariant is violated.

        Structural invariants checked here:

        * vector size <= MAX_VECTOR_ENTRIES
        * every weight finite and nonnegative (field-level already, this
          re-checks defensively)
        * sum of weights > 0 unless burn_snapshot.burn_uid is set as the explicit
          fallback target
        * burn_snapshot.forced_burn_percentage > 0 requires burn_snapshot.burn_uid
        * ``network`` matches the validator's expected network
        * ``netuid`` matches the validator's expected netuid
        * ``expires_at`` > ``now_iso`` (string compare is correct for
          ISO-8601 UTC with the same precision)

        Signature verification is a separate step (``verify_vector``)
        because the validator needs the pinned public key for that;
        invariants are local-only.
        """
        if len(self.weights) > MAX_VECTOR_ENTRIES:
            raise ValueError(
                f"weights vector exceeds MAX_VECTOR_ENTRIES={MAX_VECTOR_ENTRIES} "
                f"(got {len(self.weights)})"
            )
        if not self.weights and self.burn_snapshot.burn_uid is None:
            raise ValueError("weights vector is empty and burn_uid is unset")
        if self.burn_snapshot.forced_burn_percentage > 0.0 and self.burn_snapshot.burn_uid is None:
            raise ValueError("forced_burn_percentage requires burn_uid")
        total = 0.0
        for entry in self.weights:
            if not math.isfinite(entry.weight):
                raise ValueError(
                    f"non-finite weight for hotkey {entry.miner_hotkey!r}: {entry.weight!r}"
                )
            if entry.weight < 0:
                raise ValueError(
                    f"negative weight for hotkey {entry.miner_hotkey!r}: {entry.weight!r}"
                )
            total += entry.weight
        if total <= 0 and self.burn_snapshot.burn_uid is None:
            raise ValueError(f"weights vector sums to {total!r}, must be > 0")
        if self.network != network:
            raise ValueError(f"network mismatch: vector={self.network!r}, validator={network!r}")
        if self.netuid != netuid:
            raise ValueError(f"netuid mismatch: vector={self.netuid!r}, validator={netuid!r}")
        if self.expires_at <= self.generated_at:
            raise ValueError(
                f"expires_at {self.expires_at!r} must be after generated_at {self.generated_at!r}"
            )
        if self.expires_at <= now_iso:
            raise ValueError(f"vector expired: expires_at={self.expires_at!r}, now={now_iso!r}")


def canonical_bytes(vector: SignedWeightVector | dict[str, Any]) -> bytes:
    """Canonical signing bytes.

    Drops ``signature``, sorts keys, no whitespace, UTF-8. Matches the
    canonical-json conventions used elsewhere in the codebase
    (cathedral.v1_types.canonical_json).
    """
    if isinstance(vector, SignedWeightVector):
        payload = vector.model_dump(mode="json")
    else:
        payload = dict(vector)
    body = {k: v for k, v in payload.items() if k not in _SIGNATURE_EXCLUDED_KEYS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sign_vector(vector: SignedWeightVector, private_key: Ed25519PrivateKey) -> SignedWeightVector:
    """Return a copy of ``vector`` with ``signature`` populated.

    Does NOT mutate the input. The caller is responsible for setting
    ``vector_id``, ``policy_version``, ``generated_at``, ``expires_at``,
    ``network``, ``netuid``, ``burn_snapshot``, ``policy_hash``,
    ``key_id``, and ``weights`` first; this helper signs the canonical
    bytes as-is.
    """
    payload = vector.model_dump(mode="json")
    payload.pop("signature", None)
    canonical = canonical_bytes(payload)
    sig = private_key.sign(canonical)
    out = SignedWeightVector.model_validate(
        {**payload, "signature": base64.b64encode(sig).decode()}
    )
    return out


class VectorVerificationError(Exception):
    """Signature or key-id check failed."""


def verify_vector(
    vector: SignedWeightVector,
    *,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
) -> None:
    """Verify the signature on ``vector`` against ``public_key``.

    Pinned ``expected_key_id`` semantics: if ``vector.key_id`` does not
    match ``expected_key_id`` the call raises
    ``VectorVerificationError`` even when the signature would otherwise
    have verified. This is the "validator pinned key A, do not trust a
    rotation to key B until the operator updates config" guarantee.

    Raises ``VectorVerificationError`` on any failure mode.
    """
    if vector.signature is None or not vector.signature.strip():
        raise VectorVerificationError("vector is missing signature")
    if vector.key_id != expected_key_id:
        raise VectorVerificationError(
            f"key_id mismatch: vector={vector.key_id!r}, pinned={expected_key_id!r}"
        )
    try:
        sig = base64.b64decode(vector.signature.encode("ascii"), validate=True)
    except (ValueError, base64.binascii.Error) as e:  # type: ignore[attr-defined]
        raise VectorVerificationError(f"signature is not valid base64: {e}") from e
    canonical = canonical_bytes(vector)
    try:
        public_key.verify(sig, canonical)
    except InvalidSignature as e:
        raise VectorVerificationError("ed25519 signature verify failed") from e
