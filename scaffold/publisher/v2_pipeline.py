"""V2 blob-backed manifest verification, shadow scoring, and audit bundles.

This module is intentionally isolated from the current subnet payout ledgers. It
reads/writes only `solution_manifests`, so it can run in beta/staging/live-adjacent
without changing current miner rewards or validator weights.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import uuid
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..dimacs import parse_cnf
from ..wire_vector import canonical_bytes
from . import per_miner as pm
from . import solution_manifest
from .auth import sha256_hex
from .sat_solution import verify_dimacs_solution
from .store import Store

STATUS_RECEIVED = solution_manifest.STATUS_RECEIVED
STATUS_RETRY = "retry"
STATUS_VERIFYING = "verifying"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"

_PM_ENV_LOCK = threading.RLock()
_V2_PM_ENV_MAP = {
    "CATHEDRAL_PERMINER_ENABLED": "CATHEDRAL_V2_PERMINER_ENABLED",
    "CATHEDRAL_PERMINER_SEED_SECRET": "CATHEDRAL_V2_PERMINER_SEED_SECRET",
    "CATHEDRAL_PERMINER_EPOCH_HOURS": "CATHEDRAL_V2_PERMINER_EPOCH_HOURS",
    "CATHEDRAL_PERMINER_MAX_PAGE_LIMIT": "CATHEDRAL_V2_PERMINER_MAX_PAGE_LIMIT",
    "CATHEDRAL_PERMINER_ALLOTMENT_T1": "CATHEDRAL_V2_PERMINER_ALLOTMENT_T1",
    "CATHEDRAL_PERMINER_ALLOTMENT_T2": "CATHEDRAL_V2_PERMINER_ALLOTMENT_T2",
    "CATHEDRAL_PERMINER_WEIGHT_T1": "CATHEDRAL_V2_PERMINER_WEIGHT_T1",
    "CATHEDRAL_PERMINER_WEIGHT_T2": "CATHEDRAL_V2_PERMINER_WEIGHT_T2",
    "CATHEDRAL_PERMINER_METHOD_T1": "CATHEDRAL_V2_PERMINER_METHOD_T1",
    "CATHEDRAL_PERMINER_METHOD_T2": "CATHEDRAL_V2_PERMINER_METHOD_T2",
    "CATHEDRAL_PERMINER_NVARS_T1": "CATHEDRAL_V2_PERMINER_NVARS_T1",
    "CATHEDRAL_PERMINER_NVARS_T2": "CATHEDRAL_V2_PERMINER_NVARS_T2",
    "CATHEDRAL_PERMINER_NCLAUSES_T1": "CATHEDRAL_V2_PERMINER_NCLAUSES_T1",
    "CATHEDRAL_PERMINER_NCLAUSES_T2": "CATHEDRAL_V2_PERMINER_NCLAUSES_T2",
}


@contextmanager
def v2_pm_env():
    """Temporarily map V2-prefixed PM config onto the existing PM generator.

    The current PM generator reads process env directly. V2 is isolated and uses
    prefixed env vars, so this bridge lets the beta stack avoid setting live-ish
    CATHEDRAL_PERMINER_* names. A lock keeps the temporary mapping serialized.
    """
    with _PM_ENV_LOCK:
        old: dict[str, str | None] = {}
        try:
            for legacy, v2_name in _V2_PM_ENV_MAP.items():
                if v2_name in os.environ:
                    old[legacy] = os.environ.get(legacy)
                    os.environ[legacy] = os.environ[v2_name]
            yield
        finally:
            for legacy, value in old.items():
                if value is None:
                    os.environ.pop(legacy, None)
                else:
                    os.environ[legacy] = value


def _now_iso_ms() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_plus(secs: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=float(secs))
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        keys = row.keys()
    except Exception:
        return dict(row)
    return {k: row[k] for k in keys}


def encode_bitset_assignment(assignment: list[int]) -> bytes:
    """Pack a complete SAT assignment as little-endian truth bits.

    Variable i is stored at bit i-1; 1 = positive/true, 0 = negative/false.
    The verifier reconstructs signed literals [1|-1, 2|-2, ...].
    """
    n_vars = len(assignment)
    out = bytearray((n_vars + 7) // 8)
    seen = set()
    for lit in assignment:
        v = abs(int(lit))
        if v < 1 or v > n_vars or v in seen:
            raise ValueError("invalid_assignment")
        seen.add(v)
        if lit > 0:
            out[(v - 1) // 8] |= 1 << ((v - 1) % 8)
    if len(seen) != n_vars:
        raise ValueError("incomplete_assignment")
    return bytes(out)


def decode_bitset_assignment(data: bytes, n_vars: int) -> list[int]:
    expected = (int(n_vars) + 7) // 8
    if len(data) != expected:
        raise ValueError("bitset_size_mismatch")
    return [
        i if (data[(i - 1) // 8] >> ((i - 1) % 8)) & 1 else -i
        for i in range(1, int(n_vars) + 1)
    ]


def verify_assignment_literals(cnf_text: str, assignment: list[int]) -> tuple[bool, str | None]:
    n_vars, clauses = parse_cnf(cnf_text)
    if len(assignment) != n_vars:
        return False, "solution_incomplete_assignment"
    vals: dict[int, bool] = {}
    for lit in assignment:
        try:
            lit_i = int(lit)
        except Exception:
            return False, "solution_non_integer_literal"
        v = abs(lit_i)
        if v < 1 or v > n_vars:
            return False, "solution_variable_out_of_range"
        val = lit_i > 0
        if v in vals and vals[v] != val:
            return False, "solution_contradictory_assignment"
        vals[v] = val
    if len(vals) != n_vars:
        return False, "solution_incomplete_assignment"
    for clause in clauses:
        if not any(vals.get(abs(lit)) == (lit > 0) for lit in clause):
            return False, "solution_unsatisfied"
    return True, None


def claim_batch(
    store: Store,
    *,
    worker_id: str,
    batch_size: int = 8,
    lock_secs: float = 120.0,
) -> list[dict[str, Any]]:
    """Claim V2 rows for verification.

    This is sufficient for one/few workers in beta. A future high-scale queue can
    replace this with SKIP LOCKED/NATS/etc. without changing manifest format.
    """
    now_iso = _now_iso_ms()
    lock_until = _iso_plus(lock_secs)

    def _tx(conn):
        rows = conn.execute(
            "SELECT * FROM solution_manifests "
            "WHERE status IN (?, ?) "
            "AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= ?) "
            "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
            "ORDER BY received_at_iso ASC LIMIT ?",
            (STATUS_RECEIVED, STATUS_RETRY, now_iso, now_iso, int(batch_size)),
        ).fetchall()
        ids = [str(r["id"]) for r in rows]
        for rid in ids:
            conn.execute(
                "UPDATE solution_manifests SET status=?, locked_by=?, "
                "locked_until_iso=?, attempt_count=attempt_count+1, last_error=NULL "
                "WHERE id=?",
                (STATUS_VERIFYING, worker_id, lock_until, rid),
            )
        return [_row_to_dict(r) for r in rows]

    return store.write(_tx)


def _finish_verified(store: Store, rid: str, result: dict[str, Any]) -> None:
    now_iso = _now_iso_ms()

    def _tx(conn):
        conn.execute(
            "UPDATE solution_manifests SET status=?, rejection_reason=NULL, "
            "verified_at_iso=?, locked_by=NULL, locked_until_iso=NULL, "
            "next_attempt_at_iso=NULL, epoch=?, tier=?, seq=?, assignment_identity=?, "
            "weighted_score=?, answer_hash=?, verifier_details_hash=?, last_error=NULL "
            "WHERE id=?",
            (
                STATUS_VERIFIED,
                now_iso,
                int(result["epoch"]),
                int(result["tier"]),
                int(result["seq"]),
                str(result["assignment_identity"]),
                float(result["weighted_score"]),
                str(result["answer_hash"]),
                str(result["verifier_details_hash"]),
                rid,
            ),
        )

    store.write(_tx)


def _finish_rejected(store: Store, rid: str, reason: str, *, terminal: bool = True) -> None:
    now_iso = _now_iso_ms()
    status = STATUS_REJECTED if terminal else STATUS_RETRY
    next_attempt = None if terminal else _iso_plus(15.0)

    def _tx(conn):
        conn.execute(
            "UPDATE solution_manifests SET status=?, rejection_reason=?, "
            "verified_at_iso=?, locked_by=NULL, locked_until_iso=NULL, "
            "next_attempt_at_iso=?, last_error=? WHERE id=?",
            (status, reason, now_iso if terminal else None, next_attempt, reason, rid),
        )

    store.write(_tx)


def verify_one(store: Store, row: dict[str, Any], blob_store, *, max_blob_bytes: int = 0) -> dict[str, Any]:
    """Verify one V2 manifest row and finalize it."""
    rid = str(row["id"])
    try:
        manifest = json.loads(row["manifest_json"])
    except Exception:
        _finish_rejected(store, rid, "invalid_manifest_json")
        return {"id": rid, "status": STATUS_REJECTED, "reason": "invalid_manifest_json"}

    cid = str(row["solution_cid"])
    expected_sha = str(row["solution_sha256"]).lower()
    try:
        blob = blob_store.fetch(cid, max_bytes=max_blob_bytes)
    except ValueError as exc:
        reason = str(exc) or "blob_fetch_failed"
        terminal = reason in {"blob_too_large", "unsupported_cid_scheme", "cid_fetch_backend_not_configured"}
        _finish_rejected(store, rid, reason, terminal=terminal)
        return {"id": rid, "status": STATUS_REJECTED if terminal else STATUS_RETRY, "reason": reason}
    except Exception as exc:
        reason = f"blob_fetch_failed:{type(exc).__name__}"
        _finish_rejected(store, rid, reason, terminal=False)
        return {"id": rid, "status": STATUS_RETRY, "reason": reason}

    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        _finish_rejected(store, rid, "solution_sha256_mismatch")
        return {"id": rid, "status": STATUS_REJECTED, "reason": "solution_sha256_mismatch"}

    challenge_id = str(row["challenge_id"])
    miner_hotkey = str(row["miner_hotkey"])
    with v2_pm_env():
        parsed = pm.parse_challenge_id(challenge_id)
        if not parsed:
            _finish_rejected(store, rid, "unsupported_challenge_kind")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "unsupported_challenge_kind"}
        epoch = int(parsed["epoch"])
        tier_seq = pm.resolve_tier_seq_for(miner_hotkey, epoch, challenge_id)
        if tier_seq is None:
            _finish_rejected(store, rid, "assignment_required_fetch_challenges_first")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "assignment_required_fetch_challenges_first"}
        tier, seq = tier_seq
        generated = pm.get_miner_cnf(miner_hotkey, epoch, tier, seq)
        if generated is None:
            _finish_rejected(store, rid, "challenge_id_not_in_miner_set")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "challenge_id_not_in_miner_set"}
        _cid, cnf_text = generated

    encoding = str(row["assignment_encoding"])
    if encoding == "dimacs/v1":
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError:
            _finish_rejected(store, rid, "solution_non_utf8")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "solution_non_utf8"}
        check = verify_dimacs_solution(cnf_text, text)
        if not check.ok:
            reason = check.rejection_reason or "witness_check_failed"
            _finish_rejected(store, rid, reason)
            return {"id": rid, "status": STATUS_REJECTED, "reason": reason}
        assignment = check.assignment
    elif encoding == "bitset/v1":
        n_vars, _clauses = parse_cnf(cnf_text)
        try:
            assignment = decode_bitset_assignment(blob, n_vars)
        except ValueError as exc:
            reason = str(exc) or "invalid_bitset"
            _finish_rejected(store, rid, reason)
            return {"id": rid, "status": STATUS_REJECTED, "reason": reason}
        ok, reason = verify_assignment_literals(cnf_text, assignment)
        if not ok:
            _finish_rejected(store, rid, reason or "witness_check_failed")
            return {"id": rid, "status": STATUS_REJECTED, "reason": reason}
    else:
        _finish_rejected(store, rid, "unsupported_assignment_encoding")
        return {"id": rid, "status": STATUS_REJECTED, "reason": "unsupported_assignment_encoding"}

    with v2_pm_env():
        ok, reason = pm.verify_miner_submission_for(miner_hotkey, epoch, tier, seq, challenge_id, assignment)
        weight = pm.weight_for(tier)
    if not ok:
        _finish_rejected(store, rid, reason or "witness_check_failed")
        return {"id": rid, "status": STATUS_REJECTED, "reason": reason}

    result = {
        "epoch": epoch,
        "tier": tier,
        "seq": seq,
        "assignment_identity": miner_hotkey,
        "weighted_score": weight,
        "answer_hash": sha256_hex(",".join(str(x) for x in assignment)),
        "verifier_details_hash": sha256_hex(f"{challenge_id}:{expected_sha}"),
    }
    _finish_verified(store, rid, result)
    return {"id": rid, "status": STATUS_VERIFIED, **result}


def process_batch(
    store: Store,
    blob_store,
    *,
    worker_id: str | None = None,
    batch_size: int = 8,
    lock_secs: float = 120.0,
    max_blob_bytes: int = 0,
) -> list[dict[str, Any]]:
    worker_id = worker_id or f"v2-{uuid.uuid4().hex[:12]}"
    rows = claim_batch(store, worker_id=worker_id, batch_size=batch_size, lock_secs=lock_secs)
    return [verify_one(store, row, blob_store, max_blob_bytes=max_blob_bytes) for row in rows]


def _score_rows(store: Store, table: str, *, since_iso: str | None = None,
                epoch: int | None = None) -> list[dict[str, Any]]:
    clauses = ["status=?"]
    params: list[Any] = [STATUS_VERIFIED]
    if since_iso:
        clauses.append("verified_at_iso > ?")
        params.append(since_iso)
    if epoch is not None:
        clauses.append("epoch=?")
        params.append(int(epoch))
    rows = store.query(
        "SELECT id, miner_hotkey, challenge_id, weighted_score, verified_at_iso "
        f"FROM {table} WHERE " + " AND ".join(clauses),
        tuple(params),
    )
    return [_row_to_dict(r) for r in rows]


def score_totals(store: Store, *, since_iso: str | None = None, epoch: int | None = None) -> dict[str, float]:
    """Return V2 shadow scores with one score per miner/challenge.

    During the beta transition the same PM challenge can be submitted via the
    older manifest path and the new bitset path. Count it once, preferring the
    bitset event because that is the PM-native scoring path under test.
    """
    by_key: dict[tuple[str, str], tuple[int, float]] = {}
    for priority, table in ((0, "solution_manifests"), (1, "v2_submit_events")):
        for row in _score_rows(store, table, since_iso=since_iso, epoch=epoch):
            hk = str(row.get("miner_hotkey") or "")
            cid = str(row.get("challenge_id") or "")
            if not hk or not cid:
                continue
            score = float(row.get("weighted_score") or 0.0)
            current = by_key.get((hk, cid))
            if current is None or priority >= current[0]:
                by_key[(hk, cid)] = (priority, score)
    totals: dict[str, float] = {}
    for (hk, _cid), (_priority, score) in by_key.items():
        totals[hk] = totals.get(hk, 0.0) + score
    return totals


def receipt_counts(store: Store) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table, prefix in (("solution_manifests", "manifest"), ("v2_submit_events", "bitset")):
        rows = store.query(f"SELECT status, COUNT(*) AS n FROM {table} GROUP BY status")
        for r in rows:
            key = f"{prefix}:{r['status']}"
            counts[key] = int(r["n"] or 0)
    return counts


def build_shadow_weight_vector(
    store: Store,
    *,
    signing_key_hex: str,
    window_hours: float = 24.0,
    valid_for_secs: float = 1800.0,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=float(window_hours))
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S.") + f"{since.microsecond // 1000:03d}Z"
    totals = score_totals(store, since_iso=since_iso)
    top = max(totals.values()) if totals else 0.0
    weights = {
        hk: round(score / top, 6)
        for hk, score in totals.items()
        if top > 0 and math.isfinite(score) and score > 0
    }
    policy_inputs = {
        "schema": "cathedral.v2.shadow_weights.v1",
        "window_hours": float(window_hours),
        "scores": {hk: totals[hk] for hk in sorted(totals)},
        "counts": receipt_counts(store),
    }
    payload: dict[str, Any] = {
        "schema": "cathedral.v2.shadow_weights.v1",
        "vector_id": str(uuid.uuid4()),
        "policy_version": int(datetime.now(timezone.utc).timestamp() * 1000),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
        "expires_at": (now + timedelta(seconds=float(valid_for_secs))).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(now + timedelta(seconds=float(valid_for_secs))).microsecond // 1000:03d}Z",
        "policy_hash": "sha256:" + hashlib.sha256(json.dumps(policy_inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "key_id": os.environ.get("CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"),
        "policy_reason": f"v2_shadow_verified_receipts_{window_hours:g}h",
        "policy_metadata": {
            "shadow": True,
            "composer": "scaffold.publisher.v2_pipeline",
            "window_hours": float(window_hours),
            "receipt_counts": receipt_counts(store),
            "score_source": "v2_verified_receipts_shadow",
        },
        "weights": [
            {"miner_hotkey": hk, "weight": weights[hk], "raw_score": totals[hk]}
            for hk in sorted(weights)
        ],
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def _leaf_hash(row: dict[str, Any]) -> str:
    body = {
        "source": row.get("source"),
        "id": row.get("id"),
        "miner_hotkey": row.get("miner_hotkey"),
        "challenge_id": row.get("challenge_id"),
        "solution_cid": row.get("solution_cid"),
        "solution_sha256": row.get("solution_sha256"),
        "assignment_sha256": row.get("assignment_sha256"),
        "status": row.get("status"),
        "rejection_reason": row.get("rejection_reason"),
        "weighted_score": row.get("weighted_score"),
        "answer_hash": row.get("answer_hash"),
        "verified_at_iso": row.get("verified_at_iso"),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _audit_receipt(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": row.get("source"),
        "id": row.get("id"),
        "miner_hotkey": row.get("miner_hotkey"),
        "challenge_id": row.get("challenge_id"),
        "solution_cid": row.get("solution_cid"),
        "solution_sha256": row.get("solution_sha256"),
        "assignment_sha256": row.get("assignment_sha256"),
        "assignment_encoding": row.get("assignment_encoding"),
        "status": row.get("status"),
        "rejection_reason": row.get("rejection_reason"),
        "weighted_score": row.get("weighted_score"),
        "answer_hash": row.get("answer_hash"),
        "received_at_iso": row.get("received_at_iso"),
        "verified_at_iso": row.get("verified_at_iso"),
    }


def audit_bundle(store: Store, *, epoch: int, signing_key_hex: str, limit: int = 10_000) -> dict[str, Any]:
    manifest_rows = [_row_to_dict(r) | {"source": "manifest"} for r in store.query(
        "SELECT * FROM solution_manifests WHERE epoch=? OR (epoch IS NULL AND challenge_id LIKE ?) "
        "ORDER BY received_at_iso ASC LIMIT ?",
        (int(epoch), f"pm-%-e{int(epoch)}-%", int(limit)),
    )]
    bitset_rows = [_row_to_dict(r) | {"source": "bitset"} for r in store.query(
        "SELECT * FROM v2_submit_events WHERE epoch=? ORDER BY received_at_iso ASC LIMIT ?",
        (int(epoch), int(limit)),
    )]
    rows = sorted(
        (manifest_rows + bitset_rows),
        key=lambda r: str(r.get("received_at_iso") or ""),
    )[: int(limit)]
    leaves = [_leaf_hash(r) for r in rows]
    root = hashlib.sha256("".join(sorted(leaves)).encode()).hexdigest() if leaves else hashlib.sha256(b"").hexdigest()
    counts: dict[str, int] = {}
    for r in rows:
        key = f"{r.get('source') or 'unknown'}:{r.get('status') or 'unknown'}"
        counts[key] = counts.get(key, 0) + 1
    now = _now_iso_ms()
    payload: dict[str, Any] = {
        "schema": "cathedral.v2.audit_bundle.v1",
        "epoch": int(epoch),
        "generated_at": now,
        "count": len(rows),
        "status_counts": counts,
        "merkle_root": "sha256:" + root,
        "leaves": leaves,
        "receipts": [_audit_receipt(r) for r in rows],
    }
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_key_hex.strip()))
    payload["signature"] = base64.b64encode(sk.sign(canonical_bytes(payload))).decode()
    return payload


def publish_audit_bundle(store: Store, blob_store, *, epoch: int, signing_key_hex: str) -> dict[str, Any]:
    bundle = audit_bundle(store, epoch=epoch, signing_key_hex=signing_key_hex)
    data = json.dumps(bundle, sort_keys=True, separators=(",", ":"), default=str).encode()
    put = blob_store.put(data, kind="audit")
    return {"cid": put.cid, "sha256": put.sha256, "bytes": put.size, "bundle": bundle}
