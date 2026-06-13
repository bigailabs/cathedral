"""Signed weight-vector wire helpers shared by the orchestrator and the
validator — canonical bytes, signature verify, structural invariants.

Dependency-light by design: stdlib + cryptography only, no FastAPI, no store,
no bittensor. A validator install imports this; it must not drag in the
publisher's server dependencies. The orchestrator's
``scaffold.publisher.weights`` re-exports these so its callers and the gates
keep one import surface.
"""
from __future__ import annotations

import base64
import json
import math
from typing import Any

MAX_VECTOR_ENTRIES = 8192


class VectorError(Exception):
    """Signature, key-id, or structural-invariant check failed."""


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Drop ``signature``, sort keys, no whitespace, UTF-8 — must stay
    byte-identical to ``cathedral.policy.signing.canonical_bytes`` so the
    deployed validator and this one verify the same emission."""
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def verify_signature(payload: dict[str, Any], *, public_key_hex: str,
                     expected_key_id: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig_b64 = payload.get("signature") or ""
    if not str(sig_b64).strip():
        raise VectorError("vector is missing signature")
    if payload.get("key_id") != expected_key_id:
        raise VectorError(
            f"key_id mismatch: vector={payload.get('key_id')!r}, pinned={expected_key_id!r}")
    try:
        sig = base64.b64decode(str(sig_b64).encode("ascii"), validate=True)
    except Exception as e:
        raise VectorError(f"signature is not valid base64: {e}") from e
    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
    try:
        pk.verify(sig, canonical_bytes(payload))
    except InvalidSignature as e:
        raise VectorError("ed25519 signature verify failed") from e


def invariant_check(payload: dict[str, Any], *, network: str, netuid: int,
                    now_iso: str) -> None:
    """Structural sanity — mirrors the deployed validator's checks."""
    weights = payload.get("weights") or []
    snap = payload.get("burn_snapshot") or {}
    b_uid, b_pct = snap.get("burn_uid"), float(snap.get("forced_burn_percentage", -1))
    if len(weights) > MAX_VECTOR_ENTRIES:
        raise VectorError(f"weights vector exceeds {MAX_VECTOR_ENTRIES}")
    if not 0.0 <= b_pct <= 100.0:
        raise VectorError(f"forced_burn_percentage out of range: {b_pct!r}")
    if b_pct > 0.0 and b_uid is None:
        raise VectorError("forced_burn_percentage requires burn_uid")
    total = 0.0
    for w in weights:
        v = float(w["weight"])
        if not math.isfinite(v) or v < 0:
            raise VectorError(f"bad weight for {w.get('miner_hotkey')!r}: {v!r}")
        total += v
    if total <= 0 and b_uid is None:
        raise VectorError("empty/zero-sum weights without burn_uid fallback")
    if payload.get("network") != network:
        raise VectorError(f"network mismatch: {payload.get('network')!r} != {network!r}")
    if int(payload.get("netuid", -1)) != netuid:
        raise VectorError(f"netuid mismatch: {payload.get('netuid')!r} != {netuid!r}")
    if str(payload.get("expires_at", "")) <= str(payload.get("generated_at", "")):
        raise VectorError("expires_at must be after generated_at")
    if str(payload.get("expires_at", "")) <= now_iso:
        raise VectorError(f"vector expired at {payload.get('expires_at')!r}")
