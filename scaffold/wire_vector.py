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
import re
from datetime import UTC, datetime
from typing import Any

MAX_VECTOR_ENTRIES = 8192
MAX_VECTOR_LIFETIME_SECONDS = 3600.0
MAX_VECTOR_FUTURE_SKEW_SECONDS = 120.0
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3}|\.\d{6})Z$"
)


class VectorError(Exception):
    """Signature, key-id, or structural-invariant check failed."""


def _parse_canonical_utc(value: Any, *, field: str) -> datetime:
    """Parse the feed's bounded canonical UTC timestamp representation."""
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise VectorError(f"{field} must be canonical UTC (YYYY-MM-DDTHH:MM:SS.sssZ)")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise VectorError(f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo != UTC:
        raise VectorError(f"{field} must be UTC")
    return parsed


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Drop ``signature``, sort keys, no whitespace, UTF-8 — must stay
    byte-identical to ``cathedral.policy.signing.canonical_bytes`` so the
    deployed validator and this one verify the same emission."""
    body = {k: v for k, v in payload.items() if k != "signature"}
    try:
        return json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VectorError("vector cannot be encoded as finite canonical JSON") from exc


def verify_signature(
    payload: dict[str, Any], *, public_key_hex: str, expected_key_id: str
) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig_b64 = payload.get("signature") or ""
    if not str(sig_b64).strip():
        raise VectorError("vector is missing signature")
    if payload.get("key_id") != expected_key_id:
        raise VectorError(
            f"key_id mismatch: vector={payload.get('key_id')!r}, pinned={expected_key_id!r}"
        )
    try:
        sig = base64.b64decode(str(sig_b64).encode("ascii"), validate=True)
    except Exception as e:
        raise VectorError(f"signature is not valid base64: {e}") from e
    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
    try:
        pk.verify(sig, canonical_bytes(payload))
    except InvalidSignature as e:
        raise VectorError("ed25519 signature verify failed") from e


def invariant_check(
    payload: dict[str, Any], *, network: str, netuid: int, now_iso: str
) -> None:
    """Structural sanity — mirrors the deployed validator's checks."""
    weights = payload.get("weights") or []
    snap = payload.get("burn_snapshot") or {}
    b_uid = snap.get("burn_uid")
    b_hotkey = snap.get("burn_hotkey")
    b_pct = float(snap.get("forced_burn_percentage", -1))
    if len(weights) > MAX_VECTOR_ENTRIES:
        raise VectorError(f"weights vector exceeds {MAX_VECTOR_ENTRIES}")
    if not 0.0 <= b_pct <= 100.0:
        raise VectorError(f"forced_burn_percentage out of range: {b_pct!r}")
    if b_hotkey is not None and (
        not isinstance(b_hotkey, str) or not 1 <= len(b_hotkey) <= 128
    ):
        raise VectorError("burn_hotkey must be a non-empty bounded string")
    if b_pct > 0.0 and b_uid is None and b_hotkey is None:
        raise VectorError("forced_burn_percentage requires a burn destination")
    total = 0.0
    for w in weights:
        v = float(w["weight"])
        if not math.isfinite(v) or v < 0:
            raise VectorError(f"bad weight for {w.get('miner_hotkey')!r}: {v!r}")
        total += v
    if total <= 0 and b_uid is None and b_hotkey is None:
        raise VectorError("empty/zero-sum weights without burn fallback")
    if payload.get("network") != network:
        raise VectorError(
            f"network mismatch: {payload.get('network')!r} != {network!r}"
        )
    if int(payload.get("netuid", -1)) != netuid:
        raise VectorError(f"netuid mismatch: {payload.get('netuid')!r} != {netuid!r}")
    generated_at = _parse_canonical_utc(
        payload.get("generated_at"), field="generated_at"
    )
    expires_at = _parse_canonical_utc(payload.get("expires_at"), field="expires_at")
    now = _parse_canonical_utc(now_iso, field="validator now")
    if expires_at <= generated_at:
        raise VectorError("expires_at must be after generated_at")
    lifetime = (expires_at - generated_at).total_seconds()
    if lifetime > MAX_VECTOR_LIFETIME_SECONDS:
        raise VectorError(
            f"vector lifetime {lifetime:.0f}s exceeds "
            f"{MAX_VECTOR_LIFETIME_SECONDS:.0f}s"
        )
    future_skew = (generated_at - now).total_seconds()
    if future_skew > MAX_VECTOR_FUTURE_SKEW_SECONDS:
        raise VectorError(
            f"generated_at is {future_skew:.0f}s in the future; "
            f"maximum skew is {MAX_VECTOR_FUTURE_SKEW_SECONDS:.0f}s"
        )
    if expires_at <= now:
        raise VectorError(f"vector expired at {payload.get('expires_at')!r}")
