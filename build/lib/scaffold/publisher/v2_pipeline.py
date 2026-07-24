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
from . import v2_cnf_store
from .auth import sha256_hex
from .sat_solution import verify_dimacs_solution
from .store import Store

STATUS_RECEIVED = solution_manifest.STATUS_RECEIVED
STATUS_RETRY = "retry"
STATUS_VERIFYING = "verifying"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"

# ---- CNF store hit/miss counters (verify-path metrics) --------------------
# Cumulative, process-lifetime counters for how often verify_one found the CNF
# already baked in v2_cnf_store vs had to regenerate it from seed. Read by the
# /v2/verify/metrics endpoint. Not persisted — a restart resets them, which is
# fine for an operational hit-rate gauge.
_CNF_STORE_METRICS_LOCK = threading.Lock()
_cnf_store_hits = 0
_cnf_store_misses = 0


def _record_cnf_store_hit() -> None:
    global _cnf_store_hits
    with _CNF_STORE_METRICS_LOCK:
        _cnf_store_hits += 1


def _record_cnf_store_miss() -> None:
    global _cnf_store_misses
    with _CNF_STORE_METRICS_LOCK:
        _cnf_store_misses += 1


def cnf_store_metrics() -> dict[str, int]:
    with _CNF_STORE_METRICS_LOCK:
        return {"cnf_store_hits": _cnf_store_hits, "cnf_store_misses": _cnf_store_misses}


_PM_ENV_LOCK = threading.RLock()
_PM_ENV_PINNED = False
# Same truthy set as per_miner.perminer_enabled() so both gates agree.
_ENV_TRUTHY = {"1", "true", "yes", "on"}
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


def _scoring_identity(store: Store, hotkey: str) -> str:
    """V1-parity assignment identity (coldkey collapse aware).

    Same semantics as app._assignment_identity_for_hotkey: when collapse is on
    and a coldkey_map row exists, instances derive from the coldkey; otherwise
    the raw hotkey. Signing and receipts always stay on the raw hotkey."""
    from . import weights as weights_mod
    return weights_mod.scoring_identity_for_hotkey(
        store, hotkey, require_mapped=False) or hotkey


def pm_payout_bridge_enabled() -> bool:
    """When on, a VERIFIED bitset event also records a per_miner_solves row so
    the existing pm_primary scoring path pays V2 submits. This is the V1->V2
    convergence bridge: validators keep consuming the identical signed vector;
    only the miner-facing protocol changes. Default off; implied on by the
    v2-converged launch profile."""
    from . import launch_profile
    if launch_profile.converged():
        return True
    return os.environ.get(
        "CATHEDRAL_V2_PM_PAYOUT_BRIDGE", "").strip().lower() in _ENV_TRUTHY


def v2_perminer_enabled() -> bool:
    """Enabled-gate for the V2 per-miner surface, read from env directly.

    Mirrors what per_miner.perminer_enabled() sees inside v2_pm_env(): the
    V2-prefixed name when present (even present-but-empty, matching the
    bridge's presence check), else the unprefixed legacy name. Reading it here
    lets the V2 handlers answer "is V2 on?" without the env bridge -- required
    once pin_v2_pm_env() runs, because the pin deliberately never sets the
    legacy CATHEDRAL_PERMINER_ENABLED.
    """
    from . import launch_profile
    if launch_profile.converged() and "CATHEDRAL_V2_PERMINER_ENABLED" not in os.environ:
        # The converged launch profile implies the V2 per-miner surface;
        # an explicit env still wins (contradictions fail closed at boot).
        return True
    if "CATHEDRAL_V2_PERMINER_ENABLED" in os.environ:
        raw = os.environ["CATHEDRAL_V2_PERMINER_ENABLED"]
    else:
        raw = os.environ.get("CATHEDRAL_PERMINER_ENABLED", "")
    return raw.strip().lower() in _ENV_TRUTHY


def pin_v2_pm_env() -> bool:
    """Pin the V2 per-miner env onto the legacy names once, at startup.

    Called from build_app before the app serves traffic (single-threaded), so
    the per-miner handlers and the verify worker no longer need v2_pm_env()'s
    process-global lock -- the lock serialized every V2 per-miner request
    (challenges fetch, CNF fetch, submit) and the verify worker to
    one-at-a-time per process.

    Enabled automatically by the v2-converged launch profile, which is the
    deployment path that promises one coherent env surface. Legacy/surgical
    rollouts can still opt in via CATHEDRAL_V2_PERMINER_ENV_PIN. Refuses,
    leaving the bridge in place:
      - when the unprefixed CATHEDRAL_PERMINER_ENABLED is truthy -- the V1
        per-miner surface is live in this process and overwriting its config
        would corrupt it;
      - when a mapped legacy name is already set to a value differing from its
        V2 name -- an operator set legacy config on purpose.

    CATHEDRAL_PERMINER_ENABLED itself is deliberately never pinned: setting it
    would enable the V1 per-miner routes and could flip live scoring (the
    pm_primary /leaderboard/recent compatibility path keys off it). The V2
    handlers gate on v2_perminer_enabled() instead.
    """
    from . import launch_profile

    global _PM_ENV_PINNED
    explicit_pin = (
        os.environ.get("CATHEDRAL_V2_PERMINER_ENV_PIN", "").strip().lower()
        in _ENV_TRUTHY
    )
    if not (explicit_pin or launch_profile.converged()):
        return False
    if os.environ.get("CATHEDRAL_PERMINER_ENABLED", "").strip().lower() in _ENV_TRUTHY:
        print(
            "[v2_pm_env] pin refused: CATHEDRAL_PERMINER_ENABLED is truthy "
            "(V1 per-miner active in-process)"
        )
        return False
    with _PM_ENV_LOCK:
        for legacy, v2_name in _V2_PM_ENV_MAP.items():
            if legacy == "CATHEDRAL_PERMINER_ENABLED":
                continue
            if (
                v2_name in os.environ
                and legacy in os.environ
                and os.environ[legacy] != os.environ[v2_name]
            ):
                print(
                    f"[v2_pm_env] pin refused: {legacy} already set and "
                    f"differs from {v2_name}"
                )
                return False
        for legacy, v2_name in _V2_PM_ENV_MAP.items():
            if legacy == "CATHEDRAL_PERMINER_ENABLED":
                continue
            if v2_name in os.environ:
                os.environ[legacy] = os.environ[v2_name]
        _PM_ENV_PINNED = True
    return True


@contextmanager
def v2_pm_env():
    """Temporarily map V2-prefixed PM config onto the existing PM generator.

    The current PM generator reads process env directly. V2 is isolated and uses
    prefixed env vars, so this bridge lets the beta stack avoid setting live-ish
    CATHEDRAL_PERMINER_* names. A lock keeps the temporary mapping serialized.

    When pin_v2_pm_env() ran at startup the mapping is already baked into
    os.environ, so this becomes a lock-free no-op.
    """
    if _PM_ENV_PINNED:
        yield
        return
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
    max_attempts: int = 20,
) -> list[dict[str, Any]]:
    """Claim V2 rows for verification.

    This is sufficient for one/few workers in beta. A future high-scale queue can
    replace this with SKIP LOCKED/NATS/etc. without changing manifest format.

    max_attempts caps how many times a single row is claimed. Without it, a row
    that keeps failing non-terminally (e.g. an unreachable blob) re-enters the
    oldest-first queue every retry interval and, in bulk, starves every healthy
    row so nothing verifies. Rows past the cap are skipped here and swept to
    'rejected' as exhausted, keeping the queue live. 0 disables the cap.
    Expired-lock 'verifying' rows are also reclaimed so a worker that died
    mid-verify (or a lock that lapsed) cannot strand rows forever.
    """
    now_iso = _now_iso_ms()
    lock_until = _iso_plus(lock_secs)
    cap_clause = "" if not max_attempts else "AND attempt_count < ? "

    def _tx(conn):
        params: list[Any] = [
            STATUS_RECEIVED, STATUS_RETRY, STATUS_VERIFYING, now_iso, now_iso,
        ]
        if max_attempts:
            params.append(int(max_attempts))
        params.append(int(batch_size))
        if getattr(store, "backend", "") == "postgres":
            rows = conn.execute(
                "WITH picked AS ("
                "SELECT id FROM solution_manifests "
                "WHERE status IN (?, ?, ?) "
                "AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= ?) "
                "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
                + cap_clause +
                "ORDER BY received_at_iso ASC LIMIT ? FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE solution_manifests AS s SET status=?, locked_by=?, "
                "locked_until_iso=?, attempt_count=attempt_count+1, last_error=NULL "
                "FROM picked WHERE s.id=picked.id RETURNING s.*",
                tuple(params + [STATUS_VERIFYING, worker_id, lock_until]),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        rows = conn.execute(
            "SELECT * FROM solution_manifests "
            "WHERE status IN (?, ?, ?) "
            "AND (next_attempt_at_iso IS NULL OR next_attempt_at_iso <= ?) "
            "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
            + cap_clause +
            "ORDER BY received_at_iso ASC LIMIT ?",
            tuple(params),
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
    # Prefer the durable inline copy captured at admit time (solution_inline
    # column): the local blob dir is ephemeral container disk, so blobs die on
    # redeploy while queued. The sha check below gates BOTH sources — a wrong
    # inline copy can never be scored. Falls back to the blob store unchanged.
    blob: bytes | None = None
    try:
        raw = row.get("solution_inline")
        if raw:
            blob = raw.tobytes() if isinstance(raw, memoryview) else bytes(raw)
            if max_blob_bytes and len(blob) > int(max_blob_bytes):
                blob = None
    except Exception:
        blob = None
    if blob is None:
        try:
            blob = blob_store.fetch(cid, max_bytes=max_blob_bytes)
        except ValueError as exc:
            reason = str(exc) or "blob_fetch_failed"
            terminal = reason in {"blob_too_large", "unsupported_cid_scheme", "cid_fetch_backend_not_configured"}
            _finish_rejected(store, rid, reason, terminal=terminal)
            return {"id": rid, "status": STATUS_REJECTED if terminal else STATUS_RETRY, "reason": reason}
        except Exception as exc:
            reason = f"blob_fetch_failed:{type(exc).__name__}"
            # A local:// blob with no durable inline copy can NEVER be fetched by
            # this worker: local:// blobs live on the web container's ephemeral
            # /tmp, which is not shared cross-container and is wiped on redeploy.
            # Retrying is futile and lets one such row poison-loop forever,
            # starving the batch (claim orders oldest-first) so no healthy row
            # ever verifies. Make it terminal. Solutions admitted after the
            # inline-threshold fix always carry solution_inline, so this only
            # ever fires for pre-fix stranded rows.
            no_inline = not row.get("solution_inline")
            local_blob = cid.startswith("local://")
            terminal = no_inline and local_blob
            _finish_rejected(store, rid, reason, terminal=terminal)
            return {
                "id": rid,
                "status": STATUS_REJECTED if terminal else STATUS_RETRY,
                "reason": reason,
            }

    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        _finish_rejected(store, rid, "solution_sha256_mismatch")
        return {"id": rid, "status": STATUS_REJECTED, "reason": "solution_sha256_mismatch"}

    challenge_id = str(row["challenge_id"])
    miner_hotkey = str(row["miner_hotkey"])
    event_cnf_sha = str(row.get("cnf_sha256") or "") or None
    with v2_pm_env():
        parsed = pm.parse_challenge_id(challenge_id)
        if not parsed:
            _finish_rejected(store, rid, "unsupported_challenge_kind")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "unsupported_challenge_kind"}
        epoch = int(parsed["epoch"])
        # V1 parity: ownership resolves against the scoring identity (coldkey
        # collapse aware), matching the mint/admit sites. Raw hotkey stays the
        # signing/receipt identity.
        _manifest_identity = _scoring_identity(store, miner_hotkey)
        tier_seq = pm.resolve_tier_seq_for(_manifest_identity, epoch, challenge_id)
        if tier_seq is None:
            _finish_rejected(store, rid, "assignment_required_fetch_challenges_first")
            return {"id": rid, "status": STATUS_REJECTED, "reason": "assignment_required_fetch_challenges_first"}
        tier, seq = tier_seq

        # CNF store read: the CNF for this challenge_id may already be baked
        # (mint-time / submit-time write in app.py) — reading it back skips
        # the expensive from-seed regeneration (generate_instance), which for
        # REAL/unplanted instances re-runs combinatorial coloring/latin-square
        # generation with internal retries. sha-gated against the event's own
        # cnf_sha256 (when present) so a stale/corrupt store entry can never
        # be used; any error here is swallowed and treated as a plain miss —
        # this call must never be able to break verification.
        try:
            cnf_text = v2_cnf_store.get(store, challenge_id, expected_sha256=event_cnf_sha)
        except Exception:
            cnf_text = None
        if cnf_text is not None:
            _record_cnf_store_hit()
        else:
            _record_cnf_store_miss()
            # Fallback: EXACT existing regeneration path, unchanged.
            generated = pm.get_miner_cnf(_manifest_identity, epoch, tier, seq)
            if generated is None:
                _finish_rejected(store, rid, "challenge_id_not_in_miner_set")
                return {"id": rid, "status": STATUS_REJECTED, "reason": "challenge_id_not_in_miner_set"}
            _cid, cnf_text = generated
            # Backfill so the next event for this challenge_id hits.
            try:
                v2_cnf_store.put(store, challenge_id, cnf_text)
            except Exception:
                pass

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
        ok, reason = pm.verify_miner_submission_for(_manifest_identity, epoch, tier, seq, challenge_id, assignment)
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


def sweep_exhausted(store: Store, *, max_attempts: int = 20, limit: int = 500) -> int:
    """Reject rows that have exhausted the claim attempt cap.

    claim_batch skips rows at/over max_attempts so they can no longer starve the
    queue, but they'd otherwise sit in 'retry' indefinitely. This sweeps them to
    'rejected' (verify_attempts_exhausted) so the backlog stays clean. Best-effort,
    called once per tick. Disabled when max_attempts is 0.
    """
    if not max_attempts:
        return 0
    now_iso = _now_iso_ms()

    def _tx(conn):
        rows = conn.execute(
            "SELECT id FROM solution_manifests "
            "WHERE status IN (?, ?) AND attempt_count >= ? "
            "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) LIMIT ?",
            (STATUS_RECEIVED, STATUS_RETRY, int(max_attempts), now_iso, int(limit)),
        ).fetchall()
        ids = [str(r["id"]) for r in rows]
        for rid in ids:
            conn.execute(
                "UPDATE solution_manifests SET status=?, rejection_reason=?, "
                "locked_by=NULL, locked_until_iso=NULL, next_attempt_at_iso=NULL, "
                "last_error=? WHERE id=?",
                (STATUS_REJECTED, "verify_attempts_exhausted",
                 "verify_attempts_exhausted", rid),
            )
        return len(ids)

    try:
        return store.write(_tx)
    except Exception:
        return 0


def process_batch(
    store: Store,
    blob_store,
    *,
    worker_id: str | None = None,
    batch_size: int = 8,
    lock_secs: float = 120.0,
    max_blob_bytes: int = 0,
    max_attempts: int = 20,
) -> list[dict[str, Any]]:
    worker_id = worker_id or f"v2-{uuid.uuid4().hex[:12]}"
    rows = claim_batch(
        store, worker_id=worker_id, batch_size=batch_size,
        lock_secs=lock_secs, max_attempts=max_attempts)
    results = [verify_one(store, row, blob_store, max_blob_bytes=max_blob_bytes) for row in rows]
    # Sweep attempt-cap-exhausted rows to 'rejected' so they leave the queue
    # instead of lingering in 'retry'. Cheap point-update, best-effort.
    try:
        sweep_exhausted(store, max_attempts=max_attempts)
    except Exception:
        pass
    # Opportunistic purge of old baked CNFs — self-throttled to at most once
    # per ~10 minutes inside v2_cnf_store, so it is cheap to call every tick.
    # Best-effort: maybe_purge_older_than swallows all errors internally.
    # Window is env-tunable (CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS, default
    # 4h, clamped >=2h) — the old hardcoded 24h accumulated ~8.7GB/day under
    # all-miner load and filled the disk (2026-07-09 incident).
    try:
        v2_cnf_store.maybe_purge_older_than(
            store, hours=v2_cnf_store.retention_hours(), min_interval_secs=600.0)
    except Exception:
        pass
    return results


def claim_bitset_batch(store: Store, *, worker_id: str, batch_size: int = 8,
                       lock_secs: float = 120.0) -> list[dict[str, Any]]:
    """Claim 'received' v2_submit_events for async witness-check + scoring.
    Mirrors claim_batch but for the bitset table. Reclaims expired-lock rows so a
    dead worker cannot strand them."""
    now_iso = _now_iso_ms()
    lock_until = _iso_plus(lock_secs)

    def _tx(conn):
        if getattr(store, "backend", "") == "postgres":
            rows = conn.execute(
                "WITH picked AS ("
                "SELECT id FROM v2_submit_events "
                "WHERE status=? "
                "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
                "ORDER BY received_at_iso ASC LIMIT ? FOR UPDATE SKIP LOCKED"
                ") "
                "UPDATE v2_submit_events AS e SET locked_by=?, locked_until_iso=? "
                "FROM picked WHERE e.id=picked.id RETURNING e.*",
                (STATUS_RECEIVED, now_iso, int(batch_size), worker_id, lock_until),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
        rows = conn.execute(
            "SELECT * FROM v2_submit_events "
            "WHERE status=? "
            "AND (locked_until_iso IS NULL OR locked_until_iso <= ?) "
            "ORDER BY received_at_iso ASC LIMIT ?",
            (STATUS_RECEIVED, now_iso, int(batch_size)),
        ).fetchall()
        ids = [str(r["id"]) for r in rows]
        for rid in ids:
            conn.execute(
                "UPDATE v2_submit_events SET locked_by=?, locked_until_iso=? WHERE id=?",
                (worker_id, lock_until, rid),
            )
        return [_row_to_dict(r) for r in rows]

    return store.write(_tx)


def verify_bitset_one(store: Store, row: dict[str, Any]) -> dict[str, Any]:
    """Witness-check + score ONE received bitset event, then mark it verified (or
    rejected).

    The token already bound the exact cnf_sha256, so the verifier first tries the
    baked CNF store with that hash. On a miss it falls back to deterministic
    generation and keeps the old drift checks. This is the async half of the
    thin-submit design: the heavy work the submit handler used to do inline now
    runs here, and warm baked rows avoid doing it again.
    """
    import hashlib as _h
    from . import per_miner as pm
    from ..dimacs import parse_cnf
    from . import real_corpus
    from . import v2_cnf_store

    rid = str(row["id"])
    hk = str(row["miner_hotkey"])
    epoch_i, tier_i, seq_i = int(row["epoch"]), int(row["tier"]), int(row["seq"])
    now_iso = _now_iso_ms()

    def _finish(status, *, score=None, ah=None, vdh=None, reason=None, payout=None):
        # weighted_score/answer_hash/verifier_details_hash are NOT NULL, so a
        # reject path MUST write the zero/empty defaults (not None) or the UPDATE
        # is rejected and the row stays 'received' -> unlocks -> retries forever
        # (poison queue). On verify we write the real values.
        #
        # payout (bridge): the per_miner_solves insert rides the SAME transaction
        # as the terminal status update, so a paid row and a verified receipt are
        # atomic -- no pay-without-terminal-receipt dispute window. The insert is
        # INSERT OR IGNORE on (challenge_id, miner_hotkey) with the RAW signing
        # hotkey (V1 accept parity), so the same identity-derived challenge can
        # never double-pay across protocols. difficulty_weight is the EXACT
        # weight the verifier scored under v2_pm_env, never a recompute.
        def _tx(conn):
            if payout is not None:
                inserted = pm.record_perminer_solve_tx(
                    conn, hk, payout["epoch"], payout["challenge_id"],
                    tier_i, seq_i, True,
                    solved_at_iso=now_iso,
                    difficulty_weight=payout["weight"])
                if not inserted:
                    # Existing ledger row (e.g. written earlier by the V1 accept
                    # path) wins; audit-log so a weight mismatch between the V2
                    # receipt and the paid row is visible in a dispute.
                    print(f"[v2_pm_payout_bridge] duplicate_ledger_row "
                          f"challenge_id={payout['challenge_id']} hotkey={hk} "
                          f"v2_weight={payout['weight']}")
            conn.execute(
                "UPDATE v2_submit_events SET status=?, weighted_score=?, "
                "answer_hash=?, verifier_details_hash=?, verified_at_iso=?, "
                "eligibility_status=?, last_error=?, locked_by=NULL, "
                "locked_until_iso=NULL WHERE id=?",
                (status,
                 float(score) if score is not None else 0.0,
                 ah if ah is not None else "",
                 vdh if vdh is not None else "",
                 now_iso if status == STATUS_VERIFIED else None,
                 "eligible_beta" if status == STATUS_VERIFIED else (reason or "rejected"),
                 None if status == STATUS_VERIFIED else (reason or "rejected"),
                 rid),
            )
        store.write(_tx)

    try:
        with v2_pm_env():
            current_epoch = pm.current_epoch()
            if epoch_i not in (current_epoch, current_epoch - 1):
                # Same gate as V1's _perminer_epoch_for. Stale epochs are 410 at
                # mint and admit; an event that slipped through anyway must
                # terminal-reject here BEFORE the payout bridge can see it.
                _finish(STATUS_REJECTED, reason="per_miner_challenge_expired")
                return {"id": rid, "status": STATUS_REJECTED,
                        "reason": "per_miner_challenge_expired"}
            # V1 parity: instances derive from the scoring identity (coldkey
            # collapse aware); receipts and the payout row keep the RAW hotkey.
            identity = _scoring_identity(store, hk)
            cid = pm.instance_id(identity, epoch_i, tier_i, seq_i)
            if cid != str(row["challenge_id"]):
                _finish(STATUS_REJECTED, reason="challenge_id_mismatch")
                return {"id": rid, "status": STATUS_REJECTED, "reason": "challenge_id_mismatch"}
            expected_cnf_sha = str(row["cnf_sha256"]).lower()
            cnf_text = v2_cnf_store.get(store, cid, expected_sha256=expected_cnf_sha)
            if cnf_text is not None:
                _record_cnf_store_hit()
                cnf_sha = expected_cnf_sha
            else:
                _record_cnf_store_miss()
                generated_cid, cnf_text, _planted = pm.generate_instance(
                    identity, epoch_i, tier_i, seq_i)
                if generated_cid != cid:
                    _finish(STATUS_REJECTED, reason="challenge_id_mismatch")
                    return {"id": rid, "status": STATUS_REJECTED, "reason": "challenge_id_mismatch"}
                cnf_sha = _h.sha256(cnf_text.encode("utf-8")).hexdigest()
                if cnf_sha != expected_cnf_sha:
                    # config drift since mint — cannot fairly score. Reject, never credit.
                    _finish(STATUS_REJECTED, reason="cnf_sha_drift")
                    return {"id": rid, "status": STATUS_REJECTED, "reason": "cnf_sha_drift"}
                try:
                    v2_cnf_store.put(store, cid, cnf_text)
                except Exception:
                    pass
            nvars, _clauses = parse_cnf(cnf_text)
            from . import v2_bitset_submit as _bs
            assignment_raw, assignment = _bs.decode_assignment_b64(
                str(row["assignment_b64"]), nvars=nvars)
            ok, reason = verify_assignment_literals(cnf_text, assignment)
            weight = float(pm.weight_for(tier_i))
        if not ok:
            _finish(STATUS_REJECTED, reason=reason or "witness_check_failed")
            return {"id": rid, "status": STATUS_REJECTED, "reason": reason or "witness_check_failed"}
        ah = _h.sha256(assignment_raw).hexdigest()
        details = {
            "schema": "cathedral.v2.submit_bitset_verifier_details.v1",
            "challenge_id": cid, "miner_hotkey": hk, "epoch": epoch_i,
            "tier": tier_i, "seq": seq_i, "cnf_sha256": cnf_sha,
            "assignment_sha256": ah, "verified_at": now_iso,
        }
        vdh = _h.sha256(json.dumps(details, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        payout = (
            {"epoch": epoch_i, "challenge_id": cid, "weight": weight}
            if pm_payout_bridge_enabled() else None
        )
        _finish(STATUS_VERIFIED, score=weight, ah=ah, vdh=vdh, payout=payout)
        return {"id": rid, "status": STATUS_VERIFIED, "weighted_score": weight,
                "miner_hotkey": hk, "challenge_id": cid,
                "pm_payout_bridged": pm_payout_bridge_enabled()}
    except Exception as exc:  # keep it claimable next tick rather than lost
        def _unlock(conn):
            conn.execute(
                "UPDATE v2_submit_events SET locked_by=NULL, locked_until_iso=NULL, "
                "last_error=? WHERE id=?", (f"bitset_verify_error:{type(exc).__name__}", rid))
        try:
            store.write(_unlock)
        except Exception:
            pass
        return {"id": rid, "status": "error", "reason": str(exc)[:120]}


def process_bitset_batch(store: Store, *, worker_id: str | None = None,
                         batch_size: int = 8, lock_secs: float = 120.0) -> list[dict[str, Any]]:
    """Claim + verify + score a batch of received bitset events.

    The verification work is per-row independent after claim: read the baked CNF
    or regenerate on a cache miss, sha-gate the token, decode/verify the bitset,
    then write that row's terminal status. Run those row checks in a bounded local
    pool so a burst of thin submits drains faster than one serial CNF verification
    loop.
    """
    worker_id = worker_id or f"v2b-{uuid.uuid4().hex[:12]}"
    rows = claim_bitset_batch(store, worker_id=worker_id, batch_size=batch_size, lock_secs=lock_secs)
    if not rows:
        return []
    try:
        workers = max(1, int(os.environ.get("CATHEDRAL_V2_BITSET_VERIFY_THREADS", "8") or "1"))
    except ValueError:
        workers = 8
    workers = min(workers, len(rows))
    if workers <= 1:
        return [verify_bitset_one(store, r) for r in rows]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v2-bitset-verify") as pool:
        return list(pool.map(lambda r: verify_bitset_one(store, r), rows))


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
