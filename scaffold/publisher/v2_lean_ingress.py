"""Lean V2 bitset ingress service.

This module is a standalone submit-admission service for the V2 PM bitset path.
It is intentionally smaller than the full publisher app:

    miner -> lean ingress -> local SQLite WAL receipt
                         -> later batch flusher/verifier

Phase 1 does not write Postgres and does not verify the SAT witness inline. It
accepts only token/signature/shape-valid tiny bitset events and returns a durable
`received` receipt. This keeps the ACK path off Railway/Postgres while preserving
idempotency and auditability.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .auth import default_verifier
from . import v2_bitset_submit

DEFAULT_MAX_BODY_BYTES = 16_384
DEFAULT_SKEW_SECS = 5 * 60
DEFAULT_MAX_UNFLUSHED_EVENTS = 100_000
DEFAULT_MAX_STORAGE_BYTES = 1_000_000_000
DEFAULT_MIN_FREE_DISK_BYTES = 100_000_000
DEFAULT_MAX_UNFLUSHED_AGE_SECS = 0
DEFAULT_METRICS_TTL_SECS = 1.0
DEFAULT_IP_RPM = 6000
_FAMILY = "synthetic_boolean_v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _assert_single_worker_env() -> None:
    if _env_bool("CATHEDRAL_V2_INGRESS_ALLOW_MULTI_WORKER", False):
        return
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            workers = int(raw)
        except Exception:
            continue
        if workers > 1:
            raise RuntimeError(
                f"{name}={workers} is unsafe for sqlite-wal lean ingress; use one worker"
            )


def _now_iso_ms() -> str:
    return v2_bitset_submit.now_iso_ms()


def _receipt_id() -> str:
    return "v2in_" + uuid.uuid4().hex


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class LeanIngressPressureError(RuntimeError):
    """Local WAL/backlog/disk pressure should stop new unique admissions."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LeanIngressStore:
    """Tiny SQLite WAL-backed local durable event log."""

    def __init__(self, path: str | os.PathLike[str], *, enforce_single_process: bool = True):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._process_lock_handle = None
        if enforce_single_process:
            self._acquire_process_lock()
        self.init()

    def _acquire_process_lock(self) -> None:
        try:
            import fcntl  # POSIX-only; deployment target is Linux.
        except Exception:
            return
        lock_path = self.path + ".lock"
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("v2_lean_ingress_single_process_lock_failed") from exc
        self._process_lock_handle = handle

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS submit_events_local (
                  receipt_id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  miner_hotkey TEXT NOT NULL,
                  challenge_id TEXT NOT NULL,
                  card_id TEXT NOT NULL,
                  epoch INTEGER NOT NULL,
                  tier INTEGER NOT NULL,
                  seq INTEGER NOT NULL,
                  cnf_sha256 TEXT NOT NULL,
                  assignment_encoding TEXT NOT NULL,
                  assignment_sha256 TEXT NOT NULL,
                  assignment_b64 TEXT NOT NULL,
                  status TEXT NOT NULL,
                  eligibility_status TEXT NOT NULL,
                  received_at_iso TEXT NOT NULL,
                  submitted_at TEXT NOT NULL,
                  verified_at_iso TEXT,
                  signature TEXT NOT NULL,
                  submit_token_id TEXT NOT NULL,
                  weighted_score REAL NOT NULL DEFAULT 0,
                  event_json TEXT NOT NULL,
                  event_sha256 TEXT NOT NULL,
                  flushed_at_iso TEXT,
                  rejection_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_submit_events_local_status
                  ON submit_events_local(status, received_at_iso);
                CREATE INDEX IF NOT EXISTS idx_submit_events_local_miner_challenge
                  ON submit_events_local(miner_hotkey, challenge_id);
                CREATE INDEX IF NOT EXISTS idx_submit_events_local_unflushed
                  ON submit_events_local(flushed_at_iso, received_at_iso);
                CREATE TABLE IF NOT EXISTS reject_rollups_local (
                  bucket_iso TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  count INTEGER NOT NULL,
                  PRIMARY KEY(bucket_iso, reason)
                );
                """
            )

    def record_reject(self, reason: str) -> None:
        bucket = time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime())
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO reject_rollups_local(bucket_iso, reason, count) "
                    "VALUES (?, ?, 1) "
                    "ON CONFLICT(bucket_iso, reason) DO UPDATE SET count=count+1",
                    (bucket, str(reason)[:128]),
                )
                conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")  # type: ignore[name-defined]
            except Exception:
                pass

    def storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(self.path + suffix).stat().st_size
            except FileNotFoundError:
                pass
            except Exception:
                pass
        return int(total)

    def disk_free_bytes(self) -> int | None:
        try:
            return int(shutil.disk_usage(str(Path(self.path).parent)).free)
        except Exception:
            return None

    def _pressure_reason(
        self,
        conn: sqlite3.Connection,
        *,
        max_unflushed_events: int = 0,
        max_storage_bytes: int = 0,
        min_free_disk_bytes: int = 0,
        max_unflushed_age_secs: int = 0,
    ) -> str | None:
        if max_unflushed_events > 0:
            unflushed = int(conn.execute(
                "SELECT COUNT(*) AS n FROM submit_events_local WHERE flushed_at_iso IS NULL"
            ).fetchone()["n"] or 0)
            if unflushed >= int(max_unflushed_events):
                return "ingress_backlog_full"
        if max_storage_bytes > 0 and self.storage_bytes() >= int(max_storage_bytes):
            return "ingress_storage_limit"
        if min_free_disk_bytes > 0:
            free = self.disk_free_bytes()
            if free is not None and free < int(min_free_disk_bytes):
                return "ingress_disk_pressure"
        if max_unflushed_age_secs > 0:
            row = conn.execute(
                "SELECT MIN(received_at_iso) AS oldest FROM submit_events_local "
                "WHERE flushed_at_iso IS NULL"
            ).fetchone()
            oldest = str(row["oldest"] or "") if row else ""
            ts = v2_bitset_submit.parse_iso(oldest) if oldest else None
            if ts is not None and time.time() - ts > int(max_unflushed_age_secs):
                return "ingress_flush_lag"
        return None

    def get_replay_candidate(
        self,
        *,
        miner_hotkey: str,
        challenge_id: str,
        submit_token_id: str,
    ) -> dict[str, Any] | None:
        idem = v2_bitset_submit.idempotency_key(
            miner_hotkey=miner_hotkey,
            challenge_id=challenge_id,
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submit_events_local "
                "WHERE idempotency_key=? AND submit_token_id=? AND status != 'rejected' LIMIT 1",
                (idem, submit_token_id),
            ).fetchone()
            return dict(row) if row else None

    def admit_event(
        self,
        *,
        submit: dict[str, Any],
        token_payload: dict[str, Any],
        signature: str,
        assignment_raw: bytes,
        received_at_iso: str,
        max_unflushed_events: int = 0,
        max_storage_bytes: int = 0,
        min_free_disk_bytes: int = 0,
        max_unflushed_age_secs: int = 0,
    ) -> tuple[dict[str, Any], bool]:
        idem = v2_bitset_submit.idempotency_key(
            miner_hotkey=submit["miner_hotkey"],
            challenge_id=submit["challenge_id"],
        )
        rid = _receipt_id()
        assignment_sha = hashlib.sha256(assignment_raw).hexdigest()
        submit_token_id = hashlib.sha256(str(submit["submit_token"]).encode("utf-8")).hexdigest()[:32]
        event = {
            "schema": "cathedral.v2.lean_ingress_event.v1",
            "receipt_id": rid,
            "idempotency_key": idem,
            "miner_hotkey": submit["miner_hotkey"],
            "challenge_id": submit["challenge_id"],
            "card_id": submit["card_id"],
            "epoch": int(token_payload["epoch"]),
            "tier": int(token_payload["tier"]),
            "seq": int(token_payload["seq"]),
            "cnf_sha256": str(token_payload["cnf_sha256"]).lower(),
            "assignment_encoding": submit["assignment_encoding"],
            "assignment_sha256": assignment_sha,
            "status": "received",
            "received_at_iso": received_at_iso,
            "submitted_at": submit["submitted_at"],
            "submit_token_id": submit_token_id,
        }
        event_json = _json_dumps(event)
        event_sha = hashlib.sha256(event_json.encode("utf-8")).hexdigest()

        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM submit_events_local WHERE idempotency_key=? LIMIT 1",
                (idem,),
            ).fetchone()
            existing_row = dict(existing) if existing is not None else None
            if existing_row is not None and str(existing_row.get("status") or "") != "rejected":
                conn.execute("COMMIT")
                return existing_row, False
            if existing_row is not None:
                rid = str(existing_row["receipt_id"])
                event["receipt_id"] = rid
                event["replaces_rejected"] = True
                event_json = _json_dumps(event)
                event_sha = hashlib.sha256(event_json.encode("utf-8")).hexdigest()
            pressure_reason = self._pressure_reason(
                conn,
                max_unflushed_events=max_unflushed_events,
                max_storage_bytes=max_storage_bytes,
                min_free_disk_bytes=min_free_disk_bytes,
                max_unflushed_age_secs=max_unflushed_age_secs,
            )
            if pressure_reason:
                conn.execute("ROLLBACK")
                raise LeanIngressPressureError(pressure_reason)
            if existing_row is not None:
                cur = conn.execute(
                    "UPDATE submit_events_local SET "
                    "epoch=?, tier=?, seq=?, cnf_sha256=?, assignment_encoding=?, "
                    "assignment_sha256=?, assignment_b64=?, status='received', "
                    "eligibility_status='unknown_beta', received_at_iso=?, submitted_at=?, "
                    "verified_at_iso=NULL, signature=?, submit_token_id=?, weighted_score=0.0, "
                    "event_json=?, event_sha256=?, flushed_at_iso=NULL, rejection_reason=NULL "
                    "WHERE idempotency_key=? AND status='rejected'",
                    (
                        int(token_payload["epoch"]),
                        int(token_payload["tier"]),
                        int(token_payload["seq"]),
                        str(token_payload["cnf_sha256"]).lower(),
                        submit["assignment_encoding"],
                        assignment_sha,
                        submit["assignment_b64"],
                        received_at_iso,
                        submit["submitted_at"],
                        signature,
                        submit_token_id,
                        event_json,
                        event_sha,
                        idem,
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO submit_events_local("
                    "receipt_id, idempotency_key, miner_hotkey, challenge_id, card_id, "
                    "epoch, tier, seq, cnf_sha256, assignment_encoding, assignment_sha256, "
                    "assignment_b64, status, eligibility_status, received_at_iso, submitted_at, "
                    "verified_at_iso, signature, submit_token_id, weighted_score, event_json, event_sha256"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', 'unknown_beta', ?, ?, "
                    "NULL, ?, ?, 0.0, ?, ?)",
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
                        received_at_iso,
                        submit["submitted_at"],
                        signature,
                        submit_token_id,
                        event_json,
                        event_sha,
                    ),
                )
            inserted = cur.rowcount > 0
            row = conn.execute(
                "SELECT * FROM submit_events_local WHERE idempotency_key=? LIMIT 1",
                (idem,),
            ).fetchone()
            conn.execute("COMMIT")
            return dict(row), bool(inserted)
        except Exception:
            if conn is not None:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
            raise
        finally:
            if conn is not None:
                conn.close()

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM submit_events_local WHERE receipt_id=? LIMIT 1",
                (receipt_id,),
            ).fetchone()
            return dict(row) if row else None

    def metrics(self) -> dict[str, Any]:
        with self._connect() as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM submit_events_local GROUP BY status"
            ).fetchall()
            reject_rows = conn.execute(
                "SELECT reason, SUM(count) AS n FROM reject_rollups_local GROUP BY reason"
            ).fetchall()
            unflushed = int(conn.execute(
                "SELECT COUNT(*) AS n FROM submit_events_local WHERE flushed_at_iso IS NULL"
            ).fetchone()["n"] or 0)
            total = int(conn.execute("SELECT COUNT(*) AS n FROM submit_events_local").fetchone()["n"] or 0)
            oldest_row = conn.execute(
                "SELECT MIN(received_at_iso) AS oldest FROM submit_events_local WHERE flushed_at_iso IS NULL"
            ).fetchone()
        oldest = str(oldest_row["oldest"] or "") if oldest_row else ""
        oldest_ts = v2_bitset_submit.parse_iso(oldest) if oldest else None
        oldest_age = max(0.0, time.time() - oldest_ts) if oldest_ts is not None else None
        return {
            "schema": "cathedral.v2.lean_ingress_metrics.v1",
            "events": {str(r["status"]): int(r["n"] or 0) for r in status_rows},
            "rejects": {str(r["reason"]): int(r["n"] or 0) for r in reject_rows},
            "total_events": total,
            "unflushed_events": unflushed,
            "oldest_unflushed_at": oldest or None,
            "oldest_unflushed_age_secs": round(oldest_age, 3) if oldest_age is not None else None,
            "storage_bytes": self.storage_bytes(),
            "disk_free_bytes": self.disk_free_bytes(),
        }


def receipt_payload(row: dict[str, Any], *, inserted: bool | None = None) -> dict[str, Any]:
    payload = {
        "schema": "cathedral.v2.submit_bitset_receipt.v1",
        "shadow": True,
        "status": str(row.get("status") or "received"),
        "open": True,
        "terminal": False,
        "receipt_id": str(row["receipt_id"]),
        "receipt_url": f"/v2/agents/submit-bitset/receipts/{row['receipt_id']}",
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
    if row.get("rejection_reason"):
        payload["rejection_reason"] = str(row["rejection_reason"])
    if inserted is not None:
        payload["idempotent_replay"] = not inserted
    return payload


def build_ingress_app(
    *,
    store: LeanIngressStore | None = None,
    verifier: Any | None = None,
    submit_token_secret: str | None = None,
    max_body_bytes: int | None = None,
    timestamp_skew_secs: int | None = None,
    max_unflushed_events: int | None = None,
    max_storage_bytes: int | None = None,
    min_free_disk_bytes: int | None = None,
    max_unflushed_age_secs: int | None = None,
    metrics_token: str | None = None,
    metrics_ttl_secs: float | None = None,
    ip_rpm: int | None = None,
) -> FastAPI:
    _assert_single_worker_env()
    app = FastAPI(title="Cathedral V2 Lean Ingress", version="0.1.0")
    app.state.store = store or LeanIngressStore(
        os.environ.get("CATHEDRAL_V2_INGRESS_DB_PATH", "./data/v2-ingress.sqlite3"),
        enforce_single_process=not _env_bool("CATHEDRAL_V2_INGRESS_DISABLE_PROCESS_LOCK", False),
    )
    app.state.verifier = verifier or default_verifier()
    app.state.submit_token_secret = submit_token_secret or os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "")
    app.state.max_body_bytes = int(max_body_bytes or _env_int(
        "CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES
    ))
    app.state.timestamp_skew_secs = int(timestamp_skew_secs if timestamp_skew_secs is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_TIMESTAMP_SKEW_SECS", DEFAULT_SKEW_SECS
    ))
    app.state.max_unflushed_events = int(max_unflushed_events if max_unflushed_events is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_EVENTS", DEFAULT_MAX_UNFLUSHED_EVENTS
    ))
    app.state.max_storage_bytes = int(max_storage_bytes if max_storage_bytes is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_MAX_STORAGE_BYTES", DEFAULT_MAX_STORAGE_BYTES
    ))
    app.state.min_free_disk_bytes = int(min_free_disk_bytes if min_free_disk_bytes is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_MIN_FREE_DISK_BYTES", DEFAULT_MIN_FREE_DISK_BYTES
    ))
    app.state.max_unflushed_age_secs = int(max_unflushed_age_secs if max_unflushed_age_secs is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_MAX_UNFLUSHED_AGE_SECS", DEFAULT_MAX_UNFLUSHED_AGE_SECS
    ))
    app.state.metrics_token = (metrics_token if metrics_token is not None else os.environ.get(
        "CATHEDRAL_V2_INGRESS_METRICS_TOKEN", ""
    )).strip()
    app.state.metrics_ttl_secs = float(metrics_ttl_secs if metrics_ttl_secs is not None else _env_float(
        "CATHEDRAL_V2_INGRESS_METRICS_TTL_SECS", DEFAULT_METRICS_TTL_SECS
    ))
    app.state.ip_rpm = int(ip_rpm if ip_rpm is not None else _env_int(
        "CATHEDRAL_V2_INGRESS_IP_RPM", DEFAULT_IP_RPM
    ))
    app.state._metrics_cache = {"ts": 0.0, "payload": None}
    app.state._metrics_lock = threading.Lock()
    app.state._reject_counts: dict[str, int] = {}
    app.state._reject_lock = threading.Lock()
    app.state._ip_windows: dict[str, tuple[int, int]] = {}
    app.state._ip_lock = threading.Lock()

    def _limits() -> dict[str, Any]:
        return {
            "max_body_bytes": int(app.state.max_body_bytes),
            "timestamp_skew_secs": int(app.state.timestamp_skew_secs),
            "max_unflushed_events": int(app.state.max_unflushed_events),
            "max_storage_bytes": int(app.state.max_storage_bytes),
            "min_free_disk_bytes": int(app.state.min_free_disk_bytes),
            "max_unflushed_age_secs": int(app.state.max_unflushed_age_secs),
            "ip_rpm": int(app.state.ip_rpm),
            "metrics_ttl_secs": float(app.state.metrics_ttl_secs),
        }

    def _record_reject_memory(reason: str) -> None:
        with app.state._reject_lock:
            key = str(reason)[:128]
            app.state._reject_counts[key] = int(app.state._reject_counts.get(key, 0)) + 1

    def reject(reason: str, status_code: int = 400) -> None:
        # Public junk traffic must not contend on the SQLite write lock. Keep
        # reject accounting in memory; accepted events remain durable in SQLite.
        _record_reject_memory(reason)
        raise HTTPException(status_code, reason)

    def _metrics_payload() -> dict[str, Any]:
        now = time.time()
        with app.state._metrics_lock:
            cached = app.state._metrics_cache.get("payload")
            cached_ts = float(app.state._metrics_cache.get("ts") or 0.0)
            if cached is not None and now - cached_ts <= max(0.0, float(app.state.metrics_ttl_secs)):
                return dict(cached)
            payload = app.state.store.metrics()
            with app.state._reject_lock:
                rejects = dict(app.state._reject_counts)
            merged_rejects = dict(payload.get("rejects") or {})
            for key, value in rejects.items():
                merged_rejects[key] = int(merged_rejects.get(key, 0)) + int(value)
            payload["rejects"] = merged_rejects
            payload["limits"] = _limits()
            app.state._metrics_cache = {"ts": now, "payload": dict(payload)}
            return payload

    def _metrics_authorized(request: Request) -> bool:
        token = str(app.state.metrics_token or "").strip()
        if not token:
            return True
        auth = request.headers.get("authorization", "").strip()
        if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
            return True
        return request.headers.get("x-cathedral-admin-token", "").strip() == token

    def _client_ip(request: Request) -> str:
        # Route through the single hardened derivation (CATHEDRAL_CLIENT_IP_MODE)
        # so the ingress IP rate limit is not spoofable on the un-proxied
        # origin. request.scope carries the raw ASGI headers + client peer.
        # See ratelimit._client_ip_from_scope and issue #333.
        from . import ratelimit
        return ratelimit._client_ip_from_scope(request.scope)

    def _check_ip_rate(request: Request) -> None:
        rpm = int(app.state.ip_rpm or 0)
        if rpm <= 0:
            return
        ip = _client_ip(request)
        from . import ratelimit
        if ip == ratelimit.UNRESOLVED_IP:
            return  # fail open: no trustworthy IP to bucket on
        bucket = int(time.time() // 60)
        with app.state._ip_lock:
            old_bucket, count = app.state._ip_windows.get(ip, (bucket, 0))
            if old_bucket != bucket:
                old_bucket, count = bucket, 0
            count += 1
            app.state._ip_windows[ip] = (old_bucket, count)
            if count > rpm:
                reject("ingress_ip_rate_limited", 429)

    @app.get("/health/live")
    def health_live():
        return {
            "status": "ok",
            "kind": "live",
            "service_role": "v2-lean-ingress",
            "db": "sqlite-wal",
            "submit_token_secret": "set" if app.state.submit_token_secret else "missing",
            "limits": _limits(),
        }

    @app.get("/health/ready")
    def health_ready():
        metrics = _metrics_payload()
        reason = None
        if not app.state.submit_token_secret:
            reason = "v2_submit_token_secret_missing"
        elif app.state.max_unflushed_events > 0 and metrics["unflushed_events"] >= app.state.max_unflushed_events:
            reason = "ingress_backlog_full"
        elif app.state.max_storage_bytes > 0 and metrics["storage_bytes"] >= app.state.max_storage_bytes:
            reason = "ingress_storage_limit"
        elif app.state.min_free_disk_bytes > 0 and metrics.get("disk_free_bytes") is not None and metrics["disk_free_bytes"] < app.state.min_free_disk_bytes:
            reason = "ingress_disk_pressure"
        elif app.state.max_unflushed_age_secs > 0 and metrics.get("oldest_unflushed_age_secs") is not None and metrics["oldest_unflushed_age_secs"] > app.state.max_unflushed_age_secs:
            reason = "ingress_flush_lag"
        payload = {
            "status": "ok" if reason is None else "degraded",
            "kind": "ready",
            "service_role": "v2-lean-ingress",
            "reason": reason,
            "metrics": metrics,
            "limits": _limits(),
        }
        return JSONResponse(payload, status_code=200 if reason is None else 503)

    @app.get("/v2/ingress/metrics")
    def ingress_metrics(request: Request):
        if not _metrics_authorized(request):
            raise HTTPException(401, "metrics_unauthorized")
        return JSONResponse(
            _metrics_payload(),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v2/agents/submit-bitset")
    async def submit_bitset(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
        _check_ip_rate(request)
        if not app.state.submit_token_secret:
            reject("v2_submit_token_secret_missing", 503)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > app.state.max_body_bytes:
                    reject("submit_bitset_body_too_large", 413)
            except ValueError:
                reject("invalid_content_length", 400)
        raw_body = await request.body()
        if len(raw_body) > app.state.max_body_bytes:
            reject("submit_bitset_body_too_large", 413)
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            reject("invalid_json_submit_bitset", 400)
        try:
            submit = v2_bitset_submit.normalize_submit_body(
                body,
                miner_hotkey=x_cathedral_hotkey,
                submitted_at=x_cathedral_submitted_at,
                card_id=_FAMILY,
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            reject(exc.reason, 400)

        ts = v2_bitset_submit.parse_iso(x_cathedral_submitted_at)
        if ts is None or abs(time.time() - ts) > app.state.timestamp_skew_secs:
            reject("submitted_at outside acceptable clock-skew window", 400)

        msg = v2_bitset_submit.canonical_submit_bytes(submit)
        if not app.state.verifier.verify(x_cathedral_hotkey, msg, x_cathedral_signature):
            reject("invalid hotkey signature", 401)

        # Exact signed replays should remain cheap and available even if the
        # short-lived submit token has expired. This does not admit new work: it
        # only returns an existing non-rejected row for the same hotkey,
        # challenge, and submit-token hash.
        submit_token_id = hashlib.sha256(str(submit["submit_token"]).encode("utf-8")).hexdigest()[:32]
        replay_row = app.state.store.get_replay_candidate(
            miner_hotkey=x_cathedral_hotkey,
            challenge_id=submit["challenge_id"],
            submit_token_id=submit_token_id,
        )
        if replay_row is not None:
            return JSONResponse(
                receipt_payload(replay_row, inserted=False),
                status_code=200,
                headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
            )

        try:
            token_payload = v2_bitset_submit.verify_submit_token(
                submit["submit_token"],
                secret=app.state.submit_token_secret,
                miner_hotkey=x_cathedral_hotkey,
                challenge_id=submit["challenge_id"],
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            reject(exc.reason, 400)

        try:
            assignment_raw, _assignment = v2_bitset_submit.decode_assignment_b64(
                submit["assignment_b64"],
                nvars=int(token_payload["nvars"]),
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            reject(exc.reason, 400)

        try:
            row, inserted = app.state.store.admit_event(
                submit=submit,
                token_payload=token_payload,
                signature=x_cathedral_signature,
                assignment_raw=assignment_raw,
                received_at_iso=_now_iso_ms(),
                max_unflushed_events=app.state.max_unflushed_events,
                max_storage_bytes=app.state.max_storage_bytes,
                min_free_disk_bytes=app.state.min_free_disk_bytes,
                max_unflushed_age_secs=app.state.max_unflushed_age_secs,
            )
        except LeanIngressPressureError as exc:
            reject(exc.reason, 503)
        return JSONResponse(
            receipt_payload(row, inserted=inserted),
            status_code=202 if inserted else 200,
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.get("/v2/agents/submit-bitset/receipts/{receipt_id}")
    def get_receipt(receipt_id: str):
        row = app.state.store.get_receipt(receipt_id)
        if row is None:
            raise HTTPException(404, "receipt_not_found")
        return JSONResponse(
            receipt_payload(row),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    return app


app = build_ingress_app()
