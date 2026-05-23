"""Validator-side verification of a signed v4 row.

**This is the ONLY v4 module a validator imports.** The validator
pulls a signed row from the publisher feed, hands it here, and gets
back ``(verified: bool, score: float)``. No subprocess, no
patch-runner, no oracle import. Validators MUST NOT execute miner
code under any circumstance.

This module:

  * Re-canonicalizes the signed subset using the same canonical-JSON
    rules as ``cathedral.v4.sign``.
  * Verifies the Ed25519 signature against the publisher's pinned
    pubkey.
  * Extracts ``weighted_score`` for the validator weight loop.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# The set of keys the validator re-canonicalizes for signature check.
# MUST stay byte-equal to ``cathedral.v4.sign._V4_SIGNED_KEYS``; a
# test pins this.
_V4_SIGNED_KEYS_VALIDATOR_MIRROR: frozenset[str] = frozenset(
    {
        "id",
        "miner_hotkey",
        "task_type",
        "task_id",
        "difficulty_tier",
        "language",
        "injected_fault_type",
        "weighted_score",
        "outcome",
        "total_turns",
        "deterministic_hash",
        "ran_at",
    }
)


class VerifyError(Exception):
    """Raised when a v4 row fails verification (bad shape, missing
    key, signature mismatch). NEVER raised for low-score rows;
    those verify cleanly and just carry a small weighted_score.
    """


def _canonical_json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_detached_ed25519(
    publisher_pubkey: Any,
    *,
    payload_bytes: bytes,
    signature: bytes,
) -> None:
    if isinstance(publisher_pubkey, Ed25519PublicKey):
        # Production validator auth uses cryptography keys. Its API takes
        # (signature, data), the reverse of PyNaCl's detached verify order.
        publisher_pubkey.verify(signature, payload_bytes)
        return

    # Unit tests and older mock integrations use PyNaCl VerifyKey, whose
    # detached-signature API is verify(data, signature). Keep this fallback
    # explicit so a future key-type swap cannot silently flip the arguments.
    publisher_pubkey.verify(payload_bytes, signature)


def verify_v4_row(
    row: dict[str, Any],
    publisher_pubkey: Any,
) -> tuple[bool, float]:
    """Verify a v4 signed row.

    Args:
      row: the wire dict pulled from the publisher feed. Must
        contain every key in ``_V4_SIGNED_KEYS_VALIDATOR_MIRROR``
        plus ``cathedral_signature`` and ``eval_output_schema_version``.
      publisher_pubkey: a production ``cryptography``
        ``Ed25519PublicKey`` or a legacy/test ``nacl.signing.VerifyKey``.
        Typed as ``Any`` so this module stays decoupled from the
        validator's auth wiring.

    Returns ``(verified, weighted_score)``. ``verified=False`` on
    any structural or signature failure; verified=True and a small
    weighted_score on a legitimate low-score row.

    **This function performs ZERO subprocess work, ZERO file I/O,
    ZERO network calls. It is pure CPU.** That is the contract: the
    validator may call it freely inside its hot loop without any
    sandbox.
    """
    schema = row.get("eval_output_schema_version")
    if schema != 4:
        raise VerifyError(f"not a v4 row: schema={schema}")

    signature_b64 = row.get("cathedral_signature")
    if not isinstance(signature_b64, str):
        raise VerifyError("missing cathedral_signature")

    signed_subset: dict[str, Any] = {}
    for key in _V4_SIGNED_KEYS_VALIDATOR_MIRROR:
        if key not in row:
            raise VerifyError(f"missing signed key: {key!r}")
        signed_subset[key] = row[key]

    payload_bytes = _canonical_json(signed_subset)
    try:
        signature = base64.b64decode(signature_b64)
    except (ValueError, TypeError) as e:
        raise VerifyError(f"signature is not valid base64: {e}") from e

    try:
        _verify_detached_ed25519(
            publisher_pubkey,
            payload_bytes=payload_bytes,
            signature=signature,
        )
    except Exception as e:  # nacl raises BadSignatureError
        raise VerifyError(f"signature verification failed: {e}") from e

    weighted_score = float(signed_subset["weighted_score"])
    return True, weighted_score


__all__ = [
    "VerifyError",
    "verify_v4_row",
]
