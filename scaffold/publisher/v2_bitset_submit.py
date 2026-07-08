"""V2 native PM bitset submit admission.

This lane is intentionally isolated from V1 rewards. It accepts a tiny signed
assignment bitset for per-miner SAT challenges, performs cheap validity checks
(token, signature caller in app.py, assignment shape, SAT witness evaluation),
and records only verified shadow events.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import v2_pipeline

SCHEMA = "cathedral.v2.submit_bitset.v1"
TOKEN_SCHEMA = "cathedral.v2.submit_token.v1"
STATUS_VERIFIED = "verified"
STATUS_RECEIVED = "received"
ASSIGNMENT_ENCODING = "bitset/v1"
_TOKEN_VERSION = "v1"
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

# Forward-looking solver provenance metadata: miner-declared solver_id /
# solver_hash / image_url. SIGNED (part of canonical_submit_bytes, so tamper
# with any of it and the hotkey signature fails), stored on the v2_submit_events
# row, but never used for scoring/verification/eligibility today. Purely for
# later attestation/verification tooling.
_SOLVER_ID_RE = re.compile(r"^[A-Za-z0-9_.:+/ -]{1,64}$")
_SOLVER_HASH_RE = re.compile(r"^(sha256:)?[0-9a-fA-F]+$")
_IMAGE_URL_SCHEMES = ("https://", "oci://", "docker://", "ipfs://", "hippius://")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def require_solver_meta() -> bool:
    """CATHEDRAL_V2_REQUIRE_SOLVER_META — default false (accept-optional, no
    breakage). When true, submits missing solver_id/solver_hash are rejected."""
    return _env_bool("CATHEDRAL_V2_REQUIRE_SOLVER_META", False)


class BitsetSubmitError(ValueError):
    """Validation failure with a miner-safe reason string."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_iso(ts: str) -> float | None:
    try:
        value = str(ts).strip()
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp()
    except Exception:
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    value = str(text or "").strip()
    if not value or not _B64_RE.match(value):
        raise BitsetSubmitError("invalid_base64")
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _secret_bytes(secret: str) -> bytes:
    raw = str(secret or "").strip()
    if not raw:
        raise BitsetSubmitError("submit_token_secret_missing")
    return raw.encode("utf-8")


def _token_payload_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def mint_submit_token(
    *,
    secret: str,
    miner_hotkey: str,
    challenge_id: str,
    epoch: int,
    tier: int,
    seq: int,
    nvars: int,
    cnf_sha256: str,
    expires_at: str,
    not_before: str | None = None,
) -> str:
    """Return a compact HMAC token bound to a miner's exact PM challenge.

    not_before (optional): earliest ISO time a submit for this token is accepted.
    Used by the epoch publisher so a miner cannot pre-solve a NEXT-epoch manifest
    fetched early from public storage and submit before the epoch officially
    opens. Absent = no lower bound (back-compat with per-request minting).
    """
    payload = {
        "schema": TOKEN_SCHEMA,
        "miner_hotkey": str(miner_hotkey),
        "challenge_id": str(challenge_id),
        "epoch": int(epoch),
        "tier": int(tier),
        "seq": int(seq),
        "nvars": int(nvars),
        "cnf_sha256": str(cnf_sha256).lower(),
        "expires_at": str(expires_at),
    }
    if not_before:
        payload["not_before"] = str(not_before)
    body = _token_payload_bytes(payload)
    sig = hmac.new(_secret_bytes(secret), body, hashlib.sha256).digest()
    return f"{_TOKEN_VERSION}.{_b64url(body)}.{_b64url(sig)}"


def verify_submit_token(token: str, *, secret: str, miner_hotkey: str, challenge_id: str) -> dict[str, Any]:
    parts = str(token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        raise BitsetSubmitError("invalid_submit_token")
    try:
        body = _unb64url(parts[1])
        sig = _unb64url(parts[2])
    except BitsetSubmitError as exc:
        raise BitsetSubmitError("invalid_submit_token") from exc
    expected = hmac.new(_secret_bytes(secret), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise BitsetSubmitError("invalid_submit_token")
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise BitsetSubmitError("invalid_submit_token") from exc
    if not isinstance(payload, dict) or payload.get("schema") != TOKEN_SCHEMA:
        raise BitsetSubmitError("invalid_submit_token")
    if str(payload.get("miner_hotkey") or "") != str(miner_hotkey):
        raise BitsetSubmitError("submit_token_hotkey_mismatch")
    if str(payload.get("challenge_id") or "") != str(challenge_id):
        raise BitsetSubmitError("submit_token_challenge_mismatch")
    now_ts = datetime.now(timezone.utc).timestamp()
    exp_ts = parse_iso(str(payload.get("expires_at") or ""))
    if exp_ts is None or now_ts > exp_ts:
        raise BitsetSubmitError("submit_token_expired")
    # not_before is optional (per-request tokens omit it). When the epoch
    # publisher sets it, reject submits before the epoch officially opens so a
    # miner cannot pre-solve a next-epoch manifest fetched early from public
    # storage. A malformed not_before is treated as "not yet valid" (fail closed).
    nbf_raw = payload.get("not_before")
    if nbf_raw:
        nbf_ts = parse_iso(str(nbf_raw))
        if nbf_ts is None or now_ts < nbf_ts:
            raise BitsetSubmitError("submit_token_not_yet_valid")
    for key in ("epoch", "tier", "seq", "nvars"):
        try:
            payload[key] = int(payload[key])
        except Exception as exc:
            raise BitsetSubmitError("invalid_submit_token") from exc
    payload["cnf_sha256"] = str(payload.get("cnf_sha256") or "").lower()
    if not re.match(r"^[0-9a-f]{64}$", payload["cnf_sha256"]):
        raise BitsetSubmitError("invalid_submit_token")
    return payload


def normalize_submit_body(
    body: Any,
    *,
    miner_hotkey: str,
    submitted_at: str,
    card_id: str,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise BitsetSubmitError("invalid_json_submit_bitset")
    schema = str(body.get("schema") or SCHEMA).strip()
    if schema != SCHEMA:
        raise BitsetSubmitError("unsupported_submit_bitset_schema")
    body_card = str(body.get("card_id") or card_id).strip()
    if body_card != card_id:
        raise BitsetSubmitError("unsupported_card_id")
    body_hotkey = str(body.get("miner_hotkey") or miner_hotkey).strip()
    if body_hotkey != miner_hotkey:
        raise BitsetSubmitError("hotkey_mismatch")
    body_ts = str(body.get("submitted_at") or submitted_at).strip()
    if body_ts != submitted_at:
        raise BitsetSubmitError("submitted_at_mismatch")
    challenge_id = str(body.get("challenge_id") or "").strip()
    if not challenge_id or len(challenge_id) > 256:
        raise BitsetSubmitError("invalid_challenge_id")
    submit_token = str(body.get("submit_token") or "").strip()
    if not submit_token:
        raise BitsetSubmitError("missing_submit_token")
    encoding = str(body.get("assignment_encoding") or "").strip()
    if encoding != ASSIGNMENT_ENCODING:
        raise BitsetSubmitError("unsupported_assignment_encoding")
    assignment_b64 = str(body.get("assignment_b64") or "").strip()
    if not assignment_b64 or len(assignment_b64) > 100_000:
        raise BitsetSubmitError("invalid_assignment_b64")

    # Optional, forward-looking solver provenance. Miner-declared, SIGNED
    # (included in the normalized dict below so canonical_submit_bytes covers
    # them), stored for later verification/attestation — never used for
    # scoring today. Absent fields are simply omitted so the canonical form
    # stays deterministic and existing (no-metadata) callers are unaffected.
    solver_id = str(body.get("solver_id") or "").strip()
    if solver_id and not _SOLVER_ID_RE.match(solver_id):
        raise BitsetSubmitError("invalid_solver_id")
    solver_hash = str(body.get("solver_hash") or "").strip()
    if solver_hash:
        if len(solver_hash) > 80 or not _SOLVER_HASH_RE.match(solver_hash):
            raise BitsetSubmitError("invalid_solver_hash")
    image_url = str(body.get("image_url") or "").strip()
    if image_url:
        if len(image_url) > 512 or not image_url.startswith(_IMAGE_URL_SCHEMES):
            raise BitsetSubmitError("invalid_image_url")

    if require_solver_meta() and not (solver_id and solver_hash):
        raise BitsetSubmitError("solver_meta_required")

    out = {
        "schema": SCHEMA,
        "card_id": card_id,
        "miner_hotkey": miner_hotkey,
        "submitted_at": submitted_at,
        "challenge_id": challenge_id,
        "submit_token": submit_token,
        "assignment_encoding": ASSIGNMENT_ENCODING,
        "assignment_b64": assignment_b64,
    }
    if solver_id:
        out["solver_id"] = solver_id
    if solver_hash:
        out["solver_hash"] = solver_hash
    if image_url:
        out["image_url"] = image_url
    return out


def canonical_submit_bytes(submit: dict[str, Any]) -> bytes:
    body = {k: submit[k] for k in sorted(submit)}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def decode_assignment_b64(assignment_b64: str, *, nvars: int) -> tuple[bytes, list[int]]:
    try:
        raw = base64.b64decode(str(assignment_b64), validate=True)
    except Exception as exc:
        raise BitsetSubmitError("invalid_assignment_b64") from exc
    expected = (int(nvars) + 7) // 8
    if len(raw) != expected:
        raise BitsetSubmitError("bitset_size_mismatch")
    extra_bits = expected * 8 - int(nvars)
    if extra_bits > 0 and raw:
        mask = ((1 << extra_bits) - 1) << (8 - extra_bits)
        if raw[-1] & mask:
            raise BitsetSubmitError("bitset_trailing_bits_nonzero")
    try:
        return raw, v2_pipeline.decode_bitset_assignment(raw, int(nvars))
    except ValueError as exc:
        raise BitsetSubmitError(str(exc) or "invalid_assignment") from exc


def idempotency_key(*, miner_hotkey: str, challenge_id: str) -> str:
    body = json.dumps(
        {"miner_hotkey": miner_hotkey, "challenge_id": challenge_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"cathedral:v2:submit-bitset:\0" + body).hexdigest()


def admit_verified_event(
    store,
    *,
    submit: dict[str, Any],
    token_payload: dict[str, Any],
    signature: str,
    assignment_raw: bytes,
    received_at_iso: str,
    weighted_score: float,
    answer_hash: str,
    verifier_details_hash: str,
    eligibility_status: str = "unknown_beta",
    challenge_kind: str | None = None,
) -> tuple[dict[str, Any], bool]:
    idem = idempotency_key(
        miner_hotkey=submit["miner_hotkey"],
        challenge_id=submit["challenge_id"],
    )
    rid = str(uuid.uuid4())
    assignment_sha = hashlib.sha256(assignment_raw).hexdigest()
    submit_token_id = hashlib.sha256(str(submit["submit_token"]).encode("utf-8")).hexdigest()[:32]

    def _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO v2_submit_events("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, epoch, tier, seq, "
            "cnf_sha256, assignment_encoding, assignment_sha256, assignment_b64, status, "
            "eligibility_status, received_at_iso, submitted_at, verified_at_iso, signature, "
            "submit_token_id, weighted_score, answer_hash, verifier_details_hash, "
            "solver_id, solver_hash, image_url, challenge_kind"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                idem,
                submit["miner_hotkey"],
                submit["challenge_id"],
                submit["card_id"],
                int(token_payload["epoch"]),
                int(token_payload["tier"]),
                int(token_payload["seq"]),
                str(token_payload["cnf_sha256"]).lower(),
                submit["assignment_encoding"],
                assignment_sha,
                submit["assignment_b64"],
                STATUS_VERIFIED,
                eligibility_status,
                received_at_iso,
                submit["submitted_at"],
                received_at_iso,
                signature,
                submit_token_id,
                float(weighted_score),
                answer_hash,
                verifier_details_hash,
                submit.get("solver_id"),
                submit.get("solver_hash"),
                submit.get("image_url"),
                challenge_kind,
            ),
        )
        row = conn.execute(
            "SELECT * FROM v2_submit_events WHERE idempotency_key=? LIMIT 1",
            (idem,),
        ).fetchone()
        try:
            out = {k: row[k] for k in row.keys()}
        except Exception:
            out = dict(row)
        return out, bool(out.get("id") == rid)

    return store.write(_tx)


def admit_received_event(
    store,
    *,
    submit: dict[str, Any],
    token_payload: dict[str, Any],
    signature: str,
    assignment_raw: bytes,
    received_at_iso: str,
    challenge_kind: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """THIN admit: store the bitset submission as 'received' without regenerating
    the CNF or witness-checking. The submit_token (already HMAC-verified by the
    caller) cryptographically binds miner_hotkey + challenge_id + epoch/tier/seq +
    nvars + cnf_sha256, so ownership and challenge identity are proven here. The
    only thing NOT yet checked is whether the assignment satisfies the CNF — that
    witness check + scoring is done ASYNC by the verify worker, which claims
    'received' v2_submit_events rows (indexes idx_v2_submit_events_status_received
    exist for exactly this). This keeps submit ~sub-second instead of ~40s.

    weighted_score / answer_hash / verifier_details_hash / verified_at_iso are left
    NULL until the worker finalizes.
    """
    idem = idempotency_key(
        miner_hotkey=submit["miner_hotkey"],
        challenge_id=submit["challenge_id"],
    )
    rid = str(uuid.uuid4())
    assignment_sha = hashlib.sha256(assignment_raw).hexdigest()
    submit_token_id = hashlib.sha256(str(submit["submit_token"]).encode("utf-8")).hexdigest()[:32]

    def _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO v2_submit_events("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, epoch, tier, seq, "
            "cnf_sha256, assignment_encoding, assignment_sha256, assignment_b64, status, "
            "eligibility_status, received_at_iso, submitted_at, verified_at_iso, signature, "
            "submit_token_id, weighted_score, answer_hash, verifier_details_hash, "
            "solver_id, solver_hash, image_url, challenge_kind"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rid,
                idem,
                submit["miner_hotkey"],
                submit["challenge_id"],
                submit["card_id"],
                int(token_payload["epoch"]),
                int(token_payload["tier"]),
                int(token_payload["seq"]),
                str(token_payload["cnf_sha256"]).lower(),
                submit["assignment_encoding"],
                assignment_sha,
                submit["assignment_b64"],
                STATUS_RECEIVED,
                "unknown_beta",
                received_at_iso,
                submit["submitted_at"],
                None,  # verified_at_iso — set by the worker (nullable)
                signature,
                submit_token_id,
                0.0,   # weighted_score — placeholder; the worker sets the real score
                "",    # answer_hash — set by the worker
                "",    # verifier_details_hash — set by the worker
                submit.get("solver_id"),
                submit.get("solver_hash"),
                submit.get("image_url"),
                challenge_kind,
            ),
        )
        row = conn.execute(
            "SELECT * FROM v2_submit_events WHERE idempotency_key=? LIMIT 1",
            (idem,),
        ).fetchone()
        try:
            out = {k: row[k] for k in row.keys()}
        except Exception:
            out = dict(row)
        return out, bool(out.get("id") == rid)

    return store.write(_tx)


def get_receipt(store, receipt_id: str) -> dict[str, Any] | None:
    rows = store.query("SELECT * FROM v2_submit_events WHERE id=? LIMIT 1", (receipt_id,))
    if not rows:
        return None
    row = rows[0]
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        return dict(row)


def receipt_payload(row: dict[str, Any], *, inserted: bool | None = None) -> dict[str, Any]:
    status = str(row.get("status") or STATUS_VERIFIED)
    terminal = status in {STATUS_VERIFIED, "rejected"}
    payload = {
        "schema": "cathedral.v2.submit_bitset_receipt.v1",
        "shadow": True,
        "status": status,
        "open": not terminal,
        "terminal": terminal,
        "receipt_id": str(row["id"]),
        "receipt_url": f"/v2/agents/submit-bitset/receipts/{row['id']}",
        "miner_hotkey": str(row["miner_hotkey"]),
        "challenge_id": str(row["challenge_id"]),
        "card_id": str(row["card_id"]),
        "epoch": int(row["epoch"]),
        "tier": int(row["tier"]),
        "seq": int(row["seq"]),
        "assignment_encoding": str(row["assignment_encoding"]),
        "assignment_sha256": str(row["assignment_sha256"]),
        "cnf_sha256": str(row["cnf_sha256"]),
        "eligibility_status": str(row.get("eligibility_status") or "unknown_beta"),
        "submitted_at": str(row["submitted_at"]),
        "received_at": str(row["received_at_iso"]),
        "weighted_score": float(row.get("weighted_score") or 0.0),
    }
    if row.get("verified_at_iso"):
        payload["verified_at"] = str(row["verified_at_iso"])
    if row.get("answer_hash"):
        payload["answer_hash"] = str(row["answer_hash"])
    if row.get("rejection_reason"):
        payload["rejection_reason"] = str(row["rejection_reason"])
    if row.get("solver_id"):
        payload["solver_id"] = str(row["solver_id"])
    if row.get("solver_hash"):
        payload["solver_hash"] = str(row["solver_hash"])
    if row.get("image_url"):
        payload["image_url"] = str(row["image_url"])
    if row.get("challenge_kind"):
        payload["challenge_kind"] = str(row["challenge_kind"])
    if inserted is not None:
        payload["idempotent_replay"] = not inserted
    return payload


def event_counts(store) -> dict[str, int]:
    rows = store.query("SELECT status, COUNT(*) AS n FROM v2_submit_events GROUP BY status")
    return {str(r["status"]): int(r["n"] or 0) for r in rows}
