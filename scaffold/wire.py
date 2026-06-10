"""The live wire contract — the surface v4 must keep byte-identical.

This is the ONLY coupling between the thin publisher and the released
validators (Path A / local weights): validators pull eval rows from
/v1/leaderboard/recent, verify `cathedral_signature` (Ed25519, base64) over
the canonical JSON of a version-keyed signed subset, aggregate locally, and
set weights. Break any byte of this and 11 live validators stop scoring;
keep it and the publisher can be swapped wholesale underneath them.

Ported faithfully from cathedralai/cathedral:
  - canonical_json:  src/cathedral/v1_types.py:222  (sort_keys, compact
    separators, default=str, UTF-8; signature/cathedral_signature/
    merkle_epoch excluded post-signing — CRIT-7)
  - signed key sets: src/cathedral/validator/pull_loop.py:907 (v5) / :928 (v6)
  - signer:          src/cathedral/eval/eval_signer.py (Ed25519, 32-byte hex
    key from CATHEDRAL_EVAL_SIGNING_KEY, base64 signature)

Compatibility is PROVEN, not assumed: wire_compat.py verifies real rows
sampled from the live feed against the live jwks public key using this
module's canonicalization. If that test passes, this port is byte-correct.
"""
from __future__ import annotations

import base64
import json
from typing import Any

_SIGNATURE_EXCLUDED_KEYS = frozenset({"signature", "cathedral_signature", "merkle_epoch"})

SIGNED_KEYS_V5 = frozenset({
    "id", "agent_id", "agent_display_name", "miner_hotkey", "task_type",
    "task_id_public", "epoch_salt", "difficulty_tier", "weighted_score",
    "score_parts", "answer_hash", "verifier_details_hash",
    "rejection_reason", "ran_at",
})
SIGNED_KEYS_V6 = SIGNED_KEYS_V5 | frozenset({
    "challenge_value", "solve_rank", "solved", "operator",
})
SIGNED_KEYS_BY_VERSION: dict[int, frozenset[str]] = {5: SIGNED_KEYS_V5, 6: SIGNED_KEYS_V6}


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Byte-identical port of cathedral.v1_types.canonical_json."""
    body = {k: v for k, v in payload.items() if k not in _SIGNATURE_EXCLUDED_KEYS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def signed_payload(row: dict[str, Any]) -> dict[str, Any]:
    """The version-keyed signed subset of an eval row (validator-side filter)."""
    version = int(row.get("eval_output_schema_version", 1))
    keys = SIGNED_KEYS_BY_VERSION.get(version)
    if keys is None:
        raise ValueError(f"unsupported eval_output_schema_version: {version}")
    return {k: row[k] for k in keys if k in row}


def verify_row(row: dict[str, Any], public_key_hex: str) -> bool:
    """Verify a row's cathedral_signature exactly as released validators do."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
    sig = base64.b64decode(row["cathedral_signature"])
    try:
        pk.verify(sig, canonical_json(signed_payload(row)))
        return True
    except Exception:
        return False


def sign_row(row: dict[str, Any], private_key_hex: str) -> str:
    """Sign a row the way the publisher does (EvalSigner port). The thin
    publisher uses this with the SAME production key so validators cannot
    tell the publisher changed."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    raw = bytes.fromhex(private_key_hex.strip())
    if len(raw) != 32:
        raise ValueError(f"signing key must be 32 bytes, got {len(raw)}")
    sk = Ed25519PrivateKey.from_private_bytes(raw)
    return base64.b64encode(sk.sign(canonical_json(signed_payload(row)))).decode("ascii")
