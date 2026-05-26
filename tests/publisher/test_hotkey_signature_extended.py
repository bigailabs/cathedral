"""PR5: round-trip tests for the extended canonical_claim_bytes shape.

Covers:

- Legacy 4-field shape signs/verifies unchanged.
- 6-field shape with empty-string challenge_id + solution_sha256 is a
  distinct canonical payload from the 4-field shape (signatures NOT
  interchangeable across shapes).
- 6-field shape with populated solve fields signs/verifies as expected.
- A signature over the 4-field shape MUST fail verification when the
  verifier expects the 6-field shape (replay-protection guard).
"""

from __future__ import annotations

import base64

from bittensor_wallet import Keypair

from cathedral.auth.hotkey_signature import (
    InvalidSignatureError,
    canonical_claim_bytes,
    verify_hotkey_signature,
)


def test_legacy_4field_shape_signs_and_verifies() -> None:
    kp = Keypair.create_from_uri("//Alice")
    bundle_hash = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    sig = kp.sign(
        canonical_claim_bytes(
            bundle_hash=bundle_hash,
            card_id="synthetic_boolean_v1",
            miner_hotkey=kp.ss58_address,
            submitted_at="2026-05-26T12:00:00.000Z",
        )
    )
    sig_b64 = base64.b64encode(sig).decode("ascii")
    # Default (legacy) shape verifies.
    verify_hotkey_signature(
        hotkey_ss58=kp.ss58_address,
        signature_b64=sig_b64,
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        submitted_at="2026-05-26T12:00:00.000Z",
    )


def test_6field_shape_with_empty_solve_fields_differs_from_4field() -> None:
    """The 4-field and 6-field-with-empty-strings shapes must be DIFFERENT bytes.

    Otherwise a miner could sign under one shape and replay under the
    other, which would let an attacker swap an empty-string registration
    signature into a solve-POST flow.
    """
    bundle_hash = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    legacy = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        miner_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        submitted_at="2026-05-26T12:00:00.000Z",
    )
    extended = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        miner_hotkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        submitted_at="2026-05-26T12:00:00.000Z",
        challenge_id="",
        dimacs_solution_sha256="",
    )
    assert legacy != extended
    # And the 6-field shape contains both keys.
    assert b'"challenge_id":""' in extended
    assert b'"dimacs_solution_sha256":""' in extended


def test_6field_solve_post_roundtrip() -> None:
    kp = Keypair.create_from_uri("//Bob")
    bundle_hash = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    challenge_id = "bsat-uf20-04"
    solution_sha256 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        miner_hotkey=kp.ss58_address,
        submitted_at="2026-05-26T12:01:00.000Z",
        challenge_id=challenge_id,
        dimacs_solution_sha256=solution_sha256,
    )
    sig_b64 = base64.b64encode(kp.sign(payload)).decode("ascii")
    # Verifies under the 6-field shape.
    verify_hotkey_signature(
        hotkey_ss58=kp.ss58_address,
        signature_b64=sig_b64,
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        submitted_at="2026-05-26T12:01:00.000Z",
        challenge_id=challenge_id,
        dimacs_solution_sha256=solution_sha256,
    )


def test_legacy_signature_fails_against_extended_verifier() -> None:
    """Signing under 4-field shape must NOT verify under the 6-field shape.

    This is the anti-replay guarantee: an attacker can't promote a
    registration-only signature into a solve-POST claim.
    """
    kp = Keypair.create_from_uri("//Charlie")
    bundle_hash = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    legacy_payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        miner_hotkey=kp.ss58_address,
        submitted_at="2026-05-26T12:02:00.000Z",
    )
    sig_b64 = base64.b64encode(kp.sign(legacy_payload)).decode("ascii")
    try:
        verify_hotkey_signature(
            hotkey_ss58=kp.ss58_address,
            signature_b64=sig_b64,
            bundle_hash=bundle_hash,
            card_id="synthetic_boolean_v1",
            submitted_at="2026-05-26T12:02:00.000Z",
            challenge_id="bsat-uf20-04",
            dimacs_solution_sha256="ab" * 32,
        )
    except InvalidSignatureError:
        return
    raise AssertionError(
        "4-field signature unexpectedly verified under 6-field verifier"
    )


def test_swapped_solution_sha256_fails() -> None:
    """A miner can't swap in a different valid solution to claim the win."""
    kp = Keypair.create_from_uri("//Dave")
    bundle_hash = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    real_sha = "00" * 32
    other_sha = "ff" * 32
    payload = canonical_claim_bytes(
        bundle_hash=bundle_hash,
        card_id="synthetic_boolean_v1",
        miner_hotkey=kp.ss58_address,
        submitted_at="2026-05-26T12:03:00.000Z",
        challenge_id="bsat-uf20-04",
        dimacs_solution_sha256=real_sha,
    )
    sig_b64 = base64.b64encode(kp.sign(payload)).decode("ascii")
    try:
        verify_hotkey_signature(
            hotkey_ss58=kp.ss58_address,
            signature_b64=sig_b64,
            bundle_hash=bundle_hash,
            card_id="synthetic_boolean_v1",
            submitted_at="2026-05-26T12:03:00.000Z",
            challenge_id="bsat-uf20-04",
            dimacs_solution_sha256=other_sha,
        )
    except InvalidSignatureError:
        return
    raise AssertionError("signature unexpectedly verified after solution swap")
