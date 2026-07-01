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
import sqlite3
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
_FAMILY = "synthetic_boolean_v1"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _now_iso_ms() -> str:
    return v2_bitset_submit.now_iso_ms()


def _receipt_id() -> str:
    return "v2in_" + uuid.uuid4().hex


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class LeanIngressStore:
    """Tiny SQLite WAL-backed local durable event log."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

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

    def admit_event(
        self,
        *,
        submit: dict[str, Any],
        token_payload: dict[str, Any],
        signature: str,
        assignment_raw: bytes,
        received_at_iso: str,
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
            cur = conn.execute(
                "INSERT OR IGNORE INTO submit_events_local("
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
            unflushed = conn.execute(
                "SELECT COUNT(*) AS n FROM submit_events_local WHERE flushed_at_iso IS NULL"
            ).fetchone()["n"]
            total = conn.execute("SELECT COUNT(*) AS n FROM submit_events_local").fetchone()["n"]
        return {
            "schema": "cathedral.v2.lean_ingress_metrics.v1",
            "events": {str(r["status"]): int(r["n"] or 0) for r in status_rows},
            "rejects": {str(r["reason"]): int(r["n"] or 0) for r in reject_rows},
            "total_events": int(total or 0),
            "unflushed_events": int(unflushed or 0),
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
) -> FastAPI:
    app = FastAPI(title="Cathedral V2 Lean Ingress", version="0.1.0")
    app.state.store = store or LeanIngressStore(
        os.environ.get("CATHEDRAL_V2_INGRESS_DB_PATH", "./data/v2-ingress.sqlite3")
    )
    app.state.verifier = verifier or default_verifier()
    app.state.submit_token_secret = submit_token_secret or os.environ.get("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "")
    app.state.max_body_bytes = int(max_body_bytes or _env_int(
        "CATHEDRAL_V2_SUBMIT_BITSET_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES
    ))
    app.state.timestamp_skew_secs = int(timestamp_skew_secs or _env_int(
        "CATHEDRAL_V2_INGRESS_TIMESTAMP_SKEW_SECS", DEFAULT_SKEW_SECS
    ))

    def reject(reason: str, status_code: int = 400) -> None:
        app.state.store.record_reject(reason)
        raise HTTPException(status_code, reason)

    @app.get("/health/live")
    def health_live():
        return {
            "status": "ok",
            "kind": "live",
            "service_role": "v2-lean-ingress",
            "db": "sqlite-wal",
            "submit_token_secret": "set" if app.state.submit_token_secret else "missing",
            "max_body_bytes": app.state.max_body_bytes,
        }

    @app.get("/v2/ingress/metrics")
    def ingress_metrics():
        return JSONResponse(
            app.state.store.metrics(),
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
        )

    @app.post("/v2/agents/submit-bitset")
    async def submit_bitset(
        request: Request,
        x_cathedral_hotkey: str = Header(...),
        x_cathedral_signature: str = Header(...),
        x_cathedral_submitted_at: str = Header(...),
    ):
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

        try:
            token_payload = v2_bitset_submit.verify_submit_token(
                submit["submit_token"],
                secret=app.state.submit_token_secret,
                miner_hotkey=x_cathedral_hotkey,
                challenge_id=submit["challenge_id"],
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            reject(exc.reason, 400)

        msg = v2_bitset_submit.canonical_submit_bytes(submit)
        if not app.state.verifier.verify(x_cathedral_hotkey, msg, x_cathedral_signature):
            reject("invalid hotkey signature", 401)

        try:
            assignment_raw, _assignment = v2_bitset_submit.decode_assignment_b64(
                submit["assignment_b64"],
                nvars=int(token_payload["nvars"]),
            )
        except v2_bitset_submit.BitsetSubmitError as exc:
            reject(exc.reason, 400)

        row, inserted = app.state.store.admit_event(
            submit=submit,
            token_payload=token_payload,
            signature=x_cathedral_signature,
            assignment_raw=assignment_raw,
            received_at_iso=_now_iso_ms(),
        )
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
