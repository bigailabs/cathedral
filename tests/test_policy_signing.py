from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.policy.schemas import (
    MAX_VECTOR_HOTKEYS,
    SignedWeightVector,
    VectorVerificationError,
    canonical_bytes,
    sign_vector,
    verify_vector,
)


def _vector(**overrides: object) -> SignedWeightVector:
    base = {
        "schema_version": 1,
        "policy_version": 7,
        "vector_id": "vec-1",
        "issued_at": "2026-05-19T00:00:00.000Z",
        "expires_at": "2026-05-19T00:10:00.000Z",
        "network": "finney",
        "netuid": 39,
        "metagraph_block": 123,
        "burn_hotkey": "burn-hotkey",
        "burn_uid_snapshot": 204,
        "weights_by_hotkey": {"burn-hotkey": 0.95, "miner-hotkey": 0.05},
        "policy_hash": "hash-1",
        "key_id": "pinned",
    }
    base.update(overrides)
    return SignedWeightVector.model_validate(base)


def test_sign_and_verify_roundtrip() -> None:
    sk = Ed25519PrivateKey.generate()
    signed = sign_vector(_vector(), sk)
    verify_vector(signed, public_key=sk.public_key(), expected_key_id="pinned")
    assert signed.signature


def test_canonical_bytes_excludes_only_signature() -> None:
    sk = Ed25519PrivateKey.generate()
    unsigned = _vector()
    signed = sign_vector(unsigned, sk)
    assert canonical_bytes(unsigned) == canonical_bytes(signed)
    changed = signed.model_copy(update={"policy_hash": "different"})
    assert canonical_bytes(changed) != canonical_bytes(signed)


def test_verify_rejects_wrong_key_id_even_with_valid_signature() -> None:
    sk = Ed25519PrivateKey.generate()
    signed = sign_vector(_vector(), sk)
    with pytest.raises(VectorVerificationError, match="key_id mismatch"):
        verify_vector(signed, public_key=sk.public_key(), expected_key_id="other")


def test_verify_rejects_tampered_payload() -> None:
    sk = Ed25519PrivateKey.generate()
    signed = sign_vector(_vector(), sk)
    tampered = signed.model_copy(update={"policy_version": 8})
    with pytest.raises(VectorVerificationError):
        verify_vector(tampered, public_key=sk.public_key(), expected_key_id="pinned")


def test_vector_invariants_require_burn_hotkey_in_weights() -> None:
    vector = _vector(weights_by_hotkey={"miner-hotkey": 1.0})
    with pytest.raises(ValueError, match="burn_hotkey"):
        vector.invariant_check(
            network="finney",
            netuid=39,
            now=_vector_now(),
        )


def test_vector_invariants_require_signed_weights_sum_to_one() -> None:
    vector = _vector(weights_by_hotkey={"burn-hotkey": 0.2, "miner-hotkey": 0.2})
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        vector.invariant_check(network="finney", netuid=39, now=_vector_now())


def test_vector_invariants_reject_expired_and_wrong_network() -> None:
    expired = _vector(
        issued_at="2026-05-18T00:00:00.000Z",
        expires_at="2026-05-18T00:10:00.000Z",
    )
    with pytest.raises(ValueError, match="expired"):
        expired.invariant_check(network="finney", netuid=39, now=_vector_now())
    with pytest.raises(ValueError, match="network mismatch"):
        _vector().invariant_check(network="test", netuid=39, now=_vector_now())


def test_weights_reject_non_finite_negative_and_oversized() -> None:
    with pytest.raises(ValueError):
        _vector(weights_by_hotkey={"burn-hotkey": float("nan")})
    with pytest.raises(ValueError):
        _vector(weights_by_hotkey={"burn-hotkey": -0.1})
    too_many = {f"hk-{i}": 1.0 for i in range(MAX_VECTOR_HOTKEYS + 1)}
    too_many["burn-hotkey"] = 1.0
    with pytest.raises(ValueError):
        _vector(weights_by_hotkey=too_many)


def _vector_now():
    from datetime import UTC, datetime

    return datetime(2026, 5, 19, 0, 1, tzinfo=UTC)
