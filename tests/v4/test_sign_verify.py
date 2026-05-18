"""v4 sign + verify round-trip.

The publisher signs a row with the existing EvalSigner-shape
interface; the validator verifies it with a NaCl VerifyKey. Both
sides re-canonicalize the same locked signed subset.

This test also pins the key contract: the validator-side signed-key
mirror in ``cathedral.v4.verify`` MUST stay byte-equal to the
publisher-side keyset in ``cathedral.v4.sign``. Any drift breaks
verification at runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nacl.signing import SigningKey, VerifyKey

from cathedral.v4 import ValidationPayload, VerifyError, verify_v4_row
from cathedral.v4.sign import build_signed_v4_row


class _MockEvalSigner:
    """Minimal stand-in for the publisher's EvalSigner.

    The real ``cathedral.eval.scoring_pipeline.EvalSigner`` holds a
    ``_sk`` (NaCl SigningKey) attribute that v3/v4 sign modules
    access. We replicate the attribute so the signing path is
    byte-identical without pulling the publisher import chain.
    """

    def __init__(self, signing_key: SigningKey) -> None:
        self._sk = signing_key


@pytest.fixture
def signing_pair() -> tuple[_MockEvalSigner, VerifyKey]:
    sk = SigningKey.generate()
    return _MockEvalSigner(sk), sk.verify_key


def _make_payload() -> ValidationPayload:
    return ValidationPayload(
        task_id="v4t_sign_001",
        difficulty_tier="bronze",
        language="python",
        injected_fault_type="sign_error_off_by_operator",
        winning_patch="--- a/x\n+++ b/x\n",
        trajectories=[],
        deterministic_hash="abc" * 10,
    )


def test_sign_then_verify_round_trip(
    signing_pair: tuple[_MockEvalSigner, VerifyKey],
) -> None:
    signer, pubkey = signing_pair
    payload = _make_payload()

    row = build_signed_v4_row(
        eval_run_id="run_abc",
        miner_hotkey="5DfHt...miner_x",
        payload=payload,
        weighted_score=0.87,
        outcome="SUCCESS",
        total_turns=4,
        ran_at_iso=datetime.now(UTC).isoformat(),
        signer=signer,
    )

    assert row["eval_output_schema_version"] == 4
    assert "cathedral_signature" in row

    verified, score = verify_v4_row(row, publisher_pubkey=pubkey)
    assert verified is True
    assert score == pytest.approx(0.87)


def test_tampered_score_fails_verification(
    signing_pair: tuple[_MockEvalSigner, VerifyKey],
) -> None:
    signer, pubkey = signing_pair
    payload = _make_payload()

    row = build_signed_v4_row(
        eval_run_id="run_abc",
        miner_hotkey="5DfHt...miner_x",
        payload=payload,
        weighted_score=0.10,
        outcome="FAILURE",
        total_turns=2,
        ran_at_iso=datetime.now(UTC).isoformat(),
        signer=signer,
    )

    # Tamper post-sign.
    row["weighted_score"] = 0.99
    with pytest.raises(VerifyError):
        verify_v4_row(row, publisher_pubkey=pubkey)


def test_missing_signed_key_fails_verification(
    signing_pair: tuple[_MockEvalSigner, VerifyKey],
) -> None:
    signer, pubkey = signing_pair
    payload = _make_payload()

    row = build_signed_v4_row(
        eval_run_id="run_abc",
        miner_hotkey="5DfHt...miner_x",
        payload=payload,
        weighted_score=0.5,
        outcome="SUCCESS",
        total_turns=3,
        ran_at_iso=datetime.now(UTC).isoformat(),
        signer=signer,
    )
    del row["task_id"]
    with pytest.raises(VerifyError):
        verify_v4_row(row, publisher_pubkey=pubkey)


def test_wrong_schema_version_fails() -> None:
    sk = SigningKey.generate()
    row = {"eval_output_schema_version": 3, "cathedral_signature": "x"}
    with pytest.raises(VerifyError):
        verify_v4_row(row, publisher_pubkey=sk.verify_key)


def test_signed_keysets_byte_equal() -> None:
    """Publisher signer and validator verifier MUST share the keyset."""
    from cathedral.v4.sign import _V4_SIGNED_KEYS
    from cathedral.v4.verify import _V4_SIGNED_KEYS_VALIDATOR_MIRROR

    assert _V4_SIGNED_KEYS == _V4_SIGNED_KEYS_VALIDATOR_MIRROR
