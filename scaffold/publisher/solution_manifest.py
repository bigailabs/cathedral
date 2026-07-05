"""V2 off-chain solution manifest admission.

The V2 submit lane keeps large solution bytes out of the synchronous FastAPI
request. Miners publish the solution blob elsewhere (Hippius/IPFS/S3/etc.) and
submit a tiny hotkey-signed manifest that Cathedral can persist immediately and
verify asynchronously later.

This module is intentionally narrow for phase 1/2:
- validate and canonicalize the manifest
- verify identity in app.py using canonical bytes
- durably store an idempotent receipt row
- no blob fetch / no scoring yet
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

SCHEMA = "cathedral.solution_manifest.v1"
STATUS_RECEIVED = "received"
SUPPORTED_ENCODINGS = {
    "bitset/v1",
    "dimacs/v1",
}
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_CHALLENGE_ID_LEN = 256
_MAX_CID_LEN = 1024
_MAX_ENCODING_LEN = 64


class ManifestError(ValueError):
    """Validation failure with a miner-safe reason string."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _clean_str(body: dict[str, Any], key: str, *, required: bool = True,
               max_len: int = 1024, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ManifestError(f"invalid_{key}")
    value = value.strip()
    if required and not value:
        raise ManifestError(f"missing_{key}")
    if len(value) > max_len:
        raise ManifestError(f"{key}_too_long")
    return value


def _clean_int(body: dict[str, Any], key: str, *, default: int = 0) -> int:
    value = body.get(key, default)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ManifestError(f"invalid_{key}")
    try:
        value_i = int(value)
    except (TypeError, ValueError):
        raise ManifestError(f"invalid_{key}")
    if value_i < 0:
        raise ManifestError(f"invalid_{key}")
    return value_i


def normalize_manifest(
    body: Any,
    *,
    miner_hotkey: str,
    submitted_at: str,
    card_id: str,
    max_solution_bytes: int = 0,
) -> dict[str, Any]:
    """Return the exact manifest object covered by the miner signature.

    `max_solution_bytes <= 0` means no declared-size cap. The endpoint never
    accepts solution bytes inline; this cap is only a guard on the declared blob
    size that future workers may choose to enforce.
    """
    if not isinstance(body, dict):
        raise ManifestError("invalid_json_manifest")

    body_schema = _clean_str(body, "schema", required=False, max_len=128, default=SCHEMA)
    if body_schema != SCHEMA:
        raise ManifestError("unsupported_manifest_schema")

    body_card = _clean_str(body, "card_id", required=False, max_len=128, default=card_id)
    if body_card != card_id:
        raise ManifestError("unsupported_card_id")

    body_hotkey = _clean_str(body, "miner_hotkey", required=False, max_len=128, default=miner_hotkey)
    if body_hotkey != miner_hotkey:
        raise ManifestError("hotkey_mismatch")

    body_ts = _clean_str(body, "submitted_at", required=False, max_len=64, default=submitted_at)
    if body_ts != submitted_at:
        raise ManifestError("submitted_at_mismatch")

    challenge_id = _clean_str(
        body, "challenge_id", max_len=_MAX_CHALLENGE_ID_LEN)
    solution_cid = _clean_str(
        body, "solution_cid", max_len=_MAX_CID_LEN)
    solution_sha256 = _clean_str(body, "solution_sha256", max_len=64)
    if not _HEX64_RE.match(solution_sha256):
        raise ManifestError("invalid_solution_sha256")

    assignment_encoding = _clean_str(
        body, "assignment_encoding", max_len=_MAX_ENCODING_LEN)
    if assignment_encoding not in SUPPORTED_ENCODINGS:
        raise ManifestError("unsupported_assignment_encoding")

    cnf_sha256 = _clean_str(
        body, "cnf_sha256", required=False, max_len=64, default="")
    if cnf_sha256 and not _HEX64_RE.match(cnf_sha256):
        raise ManifestError("invalid_cnf_sha256")

    solution_bytes = _clean_int(body, "solution_bytes", default=0)
    if max_solution_bytes > 0 and solution_bytes > max_solution_bytes:
        raise ManifestError("solution_blob_too_large")

    return {
        "schema": SCHEMA,
        "card_id": card_id,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "challenge_id": challenge_id,
        "assignment_encoding": assignment_encoding,
        "solution_cid": solution_cid,
        "solution_sha256": solution_sha256.lower(),
        "solution_bytes": solution_bytes,
        "cnf_sha256": cnf_sha256.lower(),
    }


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Canonical bytes signed by miner hotkeys for V2 manifest admission."""
    body = {k: manifest[k] for k in sorted(manifest)}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_blob_upload_bytes(*, miner_hotkey: str, submitted_at: str,
                                blob_sha256: str, blob_bytes: int,
                                kind: str = "solution") -> bytes:
    """Canonical bytes for optional Cathedral-hosted/local beta blob uploads."""
    body = {
        "schema": "cathedral.blob_upload.v1",
        "miner_hotkey": str(miner_hotkey),
        "submitted_at": str(submitted_at),
        "kind": str(kind),
        "blob_sha256": str(blob_sha256).lower(),
        "blob_bytes": int(blob_bytes),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def idempotency_key(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(b"cathedral:v2:manifest:\0" + canonical_manifest_bytes(manifest)).hexdigest()


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        keys = row.keys()
    except Exception:
        return dict(row)
    return {k: row[k] for k in keys}


def admit_manifest(
    store,
    manifest: dict[str, Any],
    *,
    signature: str,
    inline_solution: bytes | None = None,
) -> tuple[dict[str, Any], bool]:
    """Insert manifest if new, returning (row, inserted). Idempotent on replay.

    inline_solution: optional copy of the (small) solution blob bytes, stored in
    the solution_inline column so verification survives blob-store loss (the
    local blob dir is ephemeral container disk — a redeploy wipes it). Kept
    OUTSIDE manifest_json so the miner-signed manifest bytes are untouched.
    The verify path sha-checks whatever bytes it uses against the manifest's
    solution_sha256, so a wrong inline copy can never be scored.
    """
    idem = idempotency_key(manifest)
    rid = str(uuid.uuid4())
    received_at = _now_iso_ms()
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))

    def _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO solution_manifests("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
            "assignment_encoding, solution_cid, solution_sha256, solution_bytes, "
            "cnf_sha256, status, submitted_at, received_at_iso, signature, manifest_json, "
            "solution_inline"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                idem,
                manifest["miner_hotkey"],
                manifest["challenge_id"],
                manifest["card_id"],
                manifest["assignment_encoding"],
                manifest["solution_cid"],
                manifest["solution_sha256"],
                int(manifest.get("solution_bytes") or 0),
                manifest.get("cnf_sha256") or "",
                STATUS_RECEIVED,
                manifest["submitted_at"],
                received_at,
                signature,
                manifest_json,
                inline_solution,
            ),
        )
        row = conn.execute(
            "SELECT * FROM solution_manifests WHERE idempotency_key=? LIMIT 1",
            (idem,),
        ).fetchone()
        inserted = bool(row and row["id"] == rid)
        return _row_to_dict(row), inserted

    return store.write(_tx)


def get_manifest_receipt(store, receipt_id: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM solution_manifests WHERE id=? LIMIT 1", (receipt_id,))
    if not rows:
        return None
    return _row_to_dict(rows[0])


def receipt_payload(row: dict[str, Any], *, inserted: bool | None = None) -> dict[str, Any]:
    status = str(row.get("status") or STATUS_RECEIVED)
    terminal = status in {"verified", "rejected"}
    payload = {
        "schema": "cathedral.solution_manifest_receipt.v1",
        "status": status,
        "open": not terminal,
        "terminal": terminal,
        "receipt_id": str(row["id"]),
        "receipt_url": f"/v2/agents/submit-manifest/receipts/{row['id']}",
        "challenge_id": str(row["challenge_id"]),
        "miner_hotkey": str(row["miner_hotkey"]),
        "assignment_encoding": str(row["assignment_encoding"]),
        "solution_cid": str(row["solution_cid"]),
        "solution_sha256": str(row["solution_sha256"]),
        "solution_bytes": int(row.get("solution_bytes") or 0),
        "submitted_at": str(row["submitted_at"]),
        "received_at": str(row["received_at_iso"]),
    }
    if row.get("cnf_sha256"):
        payload["cnf_sha256"] = str(row["cnf_sha256"])
    if row.get("verified_at_iso"):
        payload["verified_at"] = str(row["verified_at_iso"])
    if row.get("rejection_reason"):
        payload["rejection_reason"] = str(row["rejection_reason"])
    if row.get("weighted_score") is not None:
        payload["weighted_score"] = float(row["weighted_score"] or 0.0)
    if row.get("answer_hash"):
        payload["answer_hash"] = str(row["answer_hash"])
    if row.get("epoch") is not None:
        payload["epoch"] = int(row["epoch"])
    if row.get("tier") is not None:
        payload["tier"] = int(row["tier"])
    if row.get("seq") is not None:
        payload["seq"] = int(row["seq"])
    if inserted is not None:
        payload["idempotent_replay"] = not inserted
    return payload
