"""V2 public receipts feed.

A scrubbed, signed, per-epoch view of VERIFIED solution_manifests /
v2_submit_events rows, published so anyone can independently re-verify:
"this miner (hotkey + coldkey when resolvable) solved this specific
challenge with this solver at this time".

SN39's per-miner challenges are hotkey-salted (unique per miner), so
publishing the miner's own answer post-verification does not leak anything
another miner could replay.

This module deliberately reuses the SAME crypto/hashing building blocks as
`v2_pipeline.audit_bundle` (`_leaf_hash`, the sorted-leaf sha256 "merkle"
root, and the Ed25519-over-canonical-bytes signing pattern) so a receipt's
`leaf_hash` cross-checks byte-for-byte against the audit bundle for the same
underlying row. It does not modify v2_pipeline.py — this is a read-only,
additive, public *view* of already-verified rows; it must never be able to
affect scoring/verification/weights.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..wire_vector import canonical_bytes
from . import v2_pipeline
from .store import Store

SCHEMA = "cathedral.v2.receipts.v1"

ColdkeyResolver = Callable[[str], "str | None"]


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        keys = row.keys()
    except Exception:
        return dict(row)
    return {k: row[k] for k in keys}


def _answer_b64(row: dict[str, Any]) -> str | None:
    """The miner's answer, base64-encoded, from whichever source table this
    row came from. bitset rows already carry a base64 assignment column;
    manifest rows only have the durable inline blob copy (when the answer
    was small enough to have been captured at admit time)."""
    if row.get("source") == "bitset":
        val = row.get("assignment_b64")
        return str(val) if val else None
    raw = row.get("solution_inline")
    if raw is None:
        return None
    try:
        data = raw.tobytes() if isinstance(raw, memoryview) else bytes(raw)
    except Exception:
        return None
    if not data:
        return None
    return base64.b64encode(data).decode("ascii")


def _int_or_none(value: Any) -> int | None:
    return int(value) if value is not None else None


def _public_receipt(row: dict[str, Any], *, coldkey: str | None) -> dict[str, Any]:
    """Scrub a verified solution_manifests/v2_submit_events row down to the
    public receipt fields. Everything not listed here (submit tokens,
    idempotency keys, lock bookkeeping, internal error strings, the raw
    manifest_json blob, etc) is intentionally dropped."""
    return {
        "receipt_id": row.get("id"),
        "source": row.get("source"),
        "challenge_id": row.get("challenge_id"),
        "epoch": _int_or_none(row.get("epoch")),
        "tier": _int_or_none(row.get("tier")),
        "seq": _int_or_none(row.get("seq")),
        "challenge_kind": row.get("challenge_kind") or "unknown",
        "cnf_sha256": row.get("cnf_sha256") or None,
        "assignment_encoding": row.get("assignment_encoding"),
        "answer_b64": _answer_b64(row),
        "answer_sha256": row.get("answer_hash") or row.get("solution_sha256"),
        "miner_hotkey": row.get("miner_hotkey"),
        "miner_coldkey": coldkey,
        "solver_id": row.get("solver_id"),
        "solver_hash": row.get("solver_hash"),
        "image_url": row.get("image_url"),
        "submitted_at": row.get("submitted_at"),
        "verified_at": row.get("verified_at_iso"),
        "weighted_score": (
            float(row["weighted_score"]) if row.get("weighted_score") is not None else None
        ),
        "miner_signature": row.get("signature"),
        # SAME function/inputs as audit_bundle's leaves, so a receipt's
        # leaf_hash matches the corresponding audit_bundle leaf exactly.
        "leaf_hash": v2_pipeline._leaf_hash(row),
    }


def _merkle_root(leaves: list[str]) -> str:
    """Identical construction to v2_pipeline.audit_bundle's merkle_root: a
    sha256 over the sorted, concatenated leaf hashes (hex, prefixed)."""
    if not leaves:
        return "sha256:" + hashlib.sha256(b"").hexdigest()
    return "sha256:" + hashlib.sha256("".join(sorted(leaves)).encode()).hexdigest()


def build_receipts_bundle(
    store: Store,
    *,
    epoch: int,
    signing_key_hex: str,
    coldkey_resolver: ColdkeyResolver | None = None,
    limit: int = 10_000,
) -> dict[str, Any]:
    """Build the signed, public receipts bundle for one epoch.

    Pulls VERIFIED-only rows from both v2 tables (mirrors audit_bundle's row
    selection), scrubs each down to public fields, and signs the envelope the
    same way audit_bundle does. `coldkey_resolver`, when given, is called at
    most once per distinct miner_hotkey in this bundle and is never allowed
    to raise out of this function (a failing/absent resolver degrades to
    `miner_coldkey: null`, never blocks the feed).
    """
    manifest_rows = [
        _row_to_dict(r) | {"source": "manifest"}
        for r in store.query(
            "SELECT * FROM solution_manifests WHERE status=? "
            "AND (epoch=? OR (epoch IS NULL AND challenge_id LIKE ?)) "
            "ORDER BY received_at_iso ASC LIMIT ?",
            (v2_pipeline.STATUS_VERIFIED, int(epoch), f"pm-%-e{int(epoch)}-%", int(limit)),
        )
    ]
    bitset_rows = [
        _row_to_dict(r) | {"source": "bitset"}
        for r in store.query(
            "SELECT * FROM v2_submit_events WHERE status=? AND epoch=? "
            "ORDER BY received_at_iso ASC LIMIT ?",
            (v2_pipeline.STATUS_VERIFIED, int(epoch), int(limit)),
        )
    ]
    rows = sorted(
        (manifest_rows + bitset_rows),
        key=lambda r: str(r.get("received_at_iso") or ""),
    )[: int(limit)]

    resolved: dict[str, str | None] = {}

    def _resolve(hotkey: str) -> str | None:
        if coldkey_resolver is None or not hotkey:
            return None
        if hotkey not in resolved:
            try:
                resolved[hotkey] = coldkey_resolver(hotkey)
            except Exception:
                resolved[hotkey] = None
        return resolved[hotkey]

    receipts = [
        _public_receipt(r, coldkey=_resolve(str(r.get("miner_hotkey") or "")))
        for r in rows
    ]
    merkle_root = _merkle_root([r["leaf_hash"] for r in receipts])

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "epoch": int(epoch),
        "count": len(receipts),
        "merkle_root": merkle_root,
        "generated_at": _now_iso_ms(),
        "coldkey_resolution": "live" if coldkey_resolver is not None else "unavailable",
        "receipts": receipts,
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["publisher_signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def latest_verified_epoch(store: Store) -> int | None:
    """Cheapest possible "what epoch should /v2/receipts/latest serve" query:
    a single MAX(epoch) over verified rows in both tables. Callers are
    expected to TTL-cache this (it is cheap but still a table scan)."""
    best: int | None = None
    for table in ("solution_manifests", "v2_submit_events"):
        try:
            rows = store.query(
                f"SELECT MAX(epoch) AS m FROM {table} WHERE status=?",
                (v2_pipeline.STATUS_VERIFIED,),
            )
        except Exception:
            rows = []
        if rows and rows[0]["m"] is not None:
            m = int(rows[0]["m"])
            if best is None or m > best:
                best = m
    return best


# ---- Best-effort hotkey -> coldkey resolver --------------------------------
# Reuses the SAME out-of-band coldkey_map table that weights.py's scoring
# path reads (populated by a metagraph poller outside this process). We do
# NOT touch weights.py; we just call its existing loader. Cached here
# (module-level, ~10 min TTL) so the public receipts endpoint never pays a
# live DB round trip per request, and any failure resolves to an empty map
# rather than raising.
_COLDKEY_CACHE_LOCK = threading.Lock()
_coldkey_cache: dict[int, tuple[float, dict[str, str]]] = {}


def _cached_coldkey_map(store: Store, *, ttl_secs: float) -> dict[str, str]:
    key = id(store)
    now_ts = time.time()
    with _COLDKEY_CACHE_LOCK:
        entry = _coldkey_cache.get(key)
        if entry and (now_ts - entry[0]) < ttl_secs:
            return entry[1]
    try:
        from . import weights as weights_mod

        loaded = weights_mod._load_coldkey_map(store) or {}
    except Exception:
        loaded = {}
    with _COLDKEY_CACHE_LOCK:
        _coldkey_cache[key] = (time.time(), loaded)
    return loaded


def make_coldkey_resolver(store: Store, *, ttl_secs: float = 600.0) -> ColdkeyResolver:
    """Best-effort hotkey->coldkey resolver built from the existing
    metagraph-backed `coldkey_map` table (see weights._load_coldkey_map).
    Never raises: any lookup/refresh failure resolves to None so coldkey
    resolution can never block or slow the public receipts feed."""

    def _resolve(hotkey: str) -> str | None:
        try:
            mapping = _cached_coldkey_map(store, ttl_secs=ttl_secs)
            return mapping.get(hotkey)
        except Exception:
            return None

    return _resolve
