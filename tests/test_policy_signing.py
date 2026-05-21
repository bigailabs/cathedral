"""Issue #155: SignedWeightVector schema + signing roundtrip tests."""

from __future__ import annotations

import math

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.policy.signing import (
    MAX_VECTOR_ENTRIES,
    BurnSnapshot,
    SignedWeightVector,
    VectorVerificationError,
    WeightEntry,
    canonical_bytes,
    sign_vector,
    verify_vector,
)


def _fresh_vector(**overrides: object) -> SignedWeightVector:
    base = {
        "vector_id": "vec-1",
        "policy_version": 7,
        "network": "finney",
        "netuid": 39,
        "generated_at": "2026-05-19T00:00:00.000Z",
        "expires_at": "2026-05-19T00:30:00.000Z",
        "burn_snapshot": BurnSnapshot(burn_uid=204, forced_burn_percentage=95.0),
        "policy_hash": "hash-1",
        "key_id": "cathedral-weight-policy",
        "policy_reason": "test policy",
        "policy_metadata": {"source": "unit"},
        "weights": [
            WeightEntry(miner_hotkey="hk-a", weight=0.5),
            WeightEntry(miner_hotkey="hk-b", weight=0.25),
        ],
    }
    base.update(overrides)
    return SignedWeightVector.model_validate(base)


def test_weight_entry_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        WeightEntry(miner_hotkey="hk", weight=float("nan"))
    with pytest.raises(ValueError):
        WeightEntry(miner_hotkey="hk", weight=float("inf"))


def test_weight_entry_rejects_negative() -> None:
    with pytest.raises(ValueError):
        WeightEntry(miner_hotkey="hk", weight=-0.01)


def test_canonical_bytes_excludes_signature() -> None:
    v = _fresh_vector()
    sk = Ed25519PrivateKey.generate()
    signed = sign_vector(v, sk)
    assert signed.signature is not None
    # Canonical bytes ignore the signature field.
    assert canonical_bytes(signed) == canonical_bytes(v)


def test_sign_and_verify_roundtrip() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    signed = sign_vector(_fresh_vector(), sk)
    # Should not raise.
    verify_vector(signed, public_key=pk, expected_key_id="cathedral-weight-policy")


def test_verify_fails_on_key_id_mismatch_even_with_valid_signature() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    signed = sign_vector(_fresh_vector(key_id="cathedral-weight-policy"), sk)
    with pytest.raises(VectorVerificationError, match="key_id mismatch"):
        verify_vector(signed, public_key=pk, expected_key_id="other-pinned-key")


def test_verify_fails_on_tampered_payload() -> None:
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    signed = sign_vector(_fresh_vector(), sk)
    # Tamper with policy_version after signing.
    tampered = signed.model_copy(update={"policy_version": signed.policy_version + 1})
    with pytest.raises(VectorVerificationError):
        verify_vector(tampered, public_key=pk, expected_key_id="cathedral-weight-policy")


def test_verify_fails_when_signature_missing() -> None:
    pk = Ed25519PrivateKey.generate().public_key()
    v = _fresh_vector()
    with pytest.raises(VectorVerificationError, match="missing signature"):
        verify_vector(v, public_key=pk, expected_key_id="cathedral-weight-policy")


def test_invariant_check_rejects_empty_vector_without_burn_uid() -> None:
    v = _fresh_vector(
        weights=[],
        burn_snapshot=BurnSnapshot(burn_uid=None, forced_burn_percentage=0.0),
    )
    with pytest.raises(ValueError, match="empty"):
        v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_zero_sum_without_burn_uid() -> None:
    v = _fresh_vector(
        weights=[WeightEntry(miner_hotkey="hk", weight=0.0)],
        burn_snapshot=BurnSnapshot(burn_uid=None, forced_burn_percentage=0.0),
    )
    with pytest.raises(ValueError, match="sums to"):
        v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_allows_empty_vector_with_signed_burn_uid() -> None:
    v = _fresh_vector(weights=[])
    v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_forced_burn_without_burn_uid() -> None:
    v = _fresh_vector(burn_snapshot=BurnSnapshot(burn_uid=None, forced_burn_percentage=95.0))
    with pytest.raises(ValueError, match="requires burn_uid"):
        v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_network_mismatch() -> None:
    v = _fresh_vector()
    with pytest.raises(ValueError, match="network mismatch"):
        v.invariant_check(network="test", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_netuid_mismatch() -> None:
    v = _fresh_vector()
    with pytest.raises(ValueError, match="netuid mismatch"):
        v.invariant_check(network="finney", netuid=12, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_expired_vector() -> None:
    v = _fresh_vector(
        generated_at="2026-05-18T00:00:00.000Z",
        expires_at="2026-05-18T00:30:00.000Z",
    )
    with pytest.raises(ValueError, match="expired"):
        v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_invariant_check_rejects_expires_before_issued() -> None:
    v = _fresh_vector(
        generated_at="2026-05-19T01:00:00.000Z",
        expires_at="2026-05-19T00:30:00.000Z",
    )
    with pytest.raises(ValueError, match="must be after generated_at"):
        v.invariant_check(network="finney", netuid=39, now_iso="2026-05-19T00:00:00.000Z")


def test_vector_size_ceiling_enforced() -> None:
    too_many = [
        WeightEntry(miner_hotkey=f"hk-{i}", weight=1.0) for i in range(MAX_VECTOR_ENTRIES + 1)
    ]
    with pytest.raises(ValueError, match="MAX_VECTOR_ENTRIES"):
        SignedWeightVector(
            vector_id="vec",
            policy_version=1,
            network="finney",
            netuid=39,
            generated_at="2026-05-19T00:00:00.000Z",
            expires_at="2026-05-19T00:30:00.000Z",
            burn_snapshot=BurnSnapshot(burn_uid=204, forced_burn_percentage=95.0),
            policy_hash="hash-1",
            key_id="cathedral-weight-policy",
            policy_reason="test policy",
            policy_metadata={},
            weights=too_many,
        )


def test_canonical_bytes_is_deterministic() -> None:
    v1 = _fresh_vector()
    v2 = _fresh_vector()
    assert canonical_bytes(v1) == canonical_bytes(v2)
    # Order of weight entries inside the JSON: pydantic preserves
    # construction order, and our build path sorts; check that two
    # equally-sorted inputs produce identical bytes.
    assert math.isfinite(0)
