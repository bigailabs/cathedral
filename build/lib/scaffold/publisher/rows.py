"""Eval-row construction + signing — the frozen wire surface.

The thin publisher emits exactly the v5/v6 row shape the live publisher does
(verified byte-compatible by wire_compat.py against 50 live rows). Each scored
solve becomes a v6 row PLUS a `-v5compat` mirror row (the live convention: the
v5 row id is the v6 id with the `-v5compat` suffix — see
fixtures/live-20260609/rows.json). Validators on schema_version IN (5,6) consume
both; the v5 mirror keeps pre-v6 validators scoring.

Signing uses scaffold.wire.sign_row with the SAME key the live publisher uses
(CATHEDRAL_EVAL_SIGNING_KEY); the signed subset is wire's SIGNED_KEYS_V5/V6.
Post-signing fields (output_card, merkle_epoch) are added AFTER signing and are
excluded from the canonical payload, so they don't break verification.
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

from .. import wire

TASK_TYPE = "synthetic_boolean_v1"  # the frozen vocabulary (COMPAT.md item 5)


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _jwk_from_key(private_key_hex: str, *, kid: str, purpose: str) -> dict[str, Any]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = bytes.fromhex(private_key_hex.strip())
    sk = Ed25519PrivateKey.from_private_bytes(raw)
    from cryptography.hazmat.primitives import serialization

    pub_raw = sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    x_b64url = base64.urlsafe_b64encode(pub_raw).rstrip(b"=").decode("ascii")
    return {
        "kid": kid, "use": "sig", "alg": "EdDSA", "kty": "OKP", "crv": "Ed25519",
        "x": x_b64url, "public_key_hex": pub_raw.hex(), "purpose": purpose,
    }


def jwks_from_key(
    private_key_hex: str,
    kid: str = "cathedral-eval-signing",
    *,
    weight_policy_private_key_hex: str | None = None,
    weight_policy_kid: str = "cathedral-weight-policy",
) -> dict[str, Any]:
    """Derive the publisher JWKS from its configured private signing keys.

    The eval key retains the exact shape published in
    fixtures/live-20260609/jwks.json. The weight-policy key defaults to the eval
    key, matching the vector signer's fallback behavior. Exact duplicate key
    entries are omitted, while the same public key under distinct key IDs
    remains discoverable by either ID.
    """
    eval_key = _jwk_from_key(
        private_key_hex,
        kid=kid,
        purpose="Cathedral signs every EvalRun projection served from "
                "/v1/leaderboard/recent. Pin this key in your validator config.",
    )
    keys = [eval_key]
    weight_policy_key = _jwk_from_key(
        weight_policy_private_key_hex or private_key_hex,
        kid=weight_policy_kid,
        purpose="Cathedral signs weight-policy vectors served from "
                "/v1/validator/weights/next. Pin this key in your validator config.",
    )
    identity = (weight_policy_key["kid"], weight_policy_key["public_key_hex"])
    if identity != (eval_key["kid"], eval_key["public_key_hex"]):
        keys.append(weight_policy_key)
    return {"issuer": "cathedral.computer", "keys": keys}


def public_key_hex(private_key_hex: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_key_hex.strip()))
    return sk.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ).hex()


def build_solve_rows(
    *,
    row_uuid: str,
    miner_hotkey: str,
    agent_id: str,
    challenge_id: str,
    tier: int,
    weighted_score: float,
    answer_hash: str,
    verifier_details_hash: str,
    ran_at: str,
    epoch_salt: str,
    solve_rank: int,
    solved: bool,
    private_key_hex: str,
    rejection_reason: str | None = None,
) -> list[dict[str, Any]]:
    """Build a signed v6 row + its `-v5compat` mirror for one scored solve.

    Returns [v6_row, v5_row]. Both carry identical scores/hashes; the v6 row adds
    challenge_value/solve_rank/solved/operator (the v6 signed extension) plus a
    post-signing output_card. The v5 id is the v6 id + '-v5compat' (live shape).
    """
    display = f"AL-SAT-{miner_hotkey[:8]}"
    task_id_public = _hash16(f"{challenge_id}:{tier}")
    score_parts = {"binary_correct": float(weighted_score)}

    common = {
        "agent_id": agent_id,
        "agent_display_name": display,
        "miner_hotkey": miner_hotkey,
        "task_type": TASK_TYPE,
        "task_id_public": task_id_public,
        "epoch_salt": epoch_salt,
        "difficulty_tier": int(tier),
        "weighted_score": float(weighted_score),
        "score_parts": score_parts,
        "answer_hash": answer_hash,
        "verifier_details_hash": verifier_details_hash,
        "rejection_reason": rejection_reason,
        "ran_at": ran_at,
    }

    v6 = dict(common)
    v6.update({
        "id": row_uuid,
        "eval_output_schema_version": 6,
        "challenge_value": float(weighted_score),
        "solve_rank": int(solve_rank),
        "solved": bool(solved),
        "operator": miner_hotkey,
    })
    v6["cathedral_signature"] = wire.sign_row(v6, private_key_hex)
    # post-signing fields (excluded from canonical payload, mirror live shape)
    v6["output_card"] = {
        "difficulty_tier": int(tier),
        "rejection_reason": rejection_reason,
        "task_id_public": task_id_public,
        "task_type": TASK_TYPE,
        "weighted_score": float(weighted_score),
        "worker_owner_hotkey": miner_hotkey,
    }
    v6["merkle_epoch"] = None

    v5 = dict(common)
    v5.update({"id": f"{row_uuid}-v5compat", "eval_output_schema_version": 5})
    v5["cathedral_signature"] = wire.sign_row(v5, private_key_hex)
    v5["merkle_epoch"] = None

    return [v6, v5]
