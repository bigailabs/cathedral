"""External score intake for orchestrator-side weight composition.

This module is intentionally publisher-side only. Validators still consume one
Cathedral-signed vector from /v1/validator/weights/next; external mechanisms
such as Violet can submit scored hotkeys here for Cathedral to verify, store,
blend, sign, and relay.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from .store import Store

INGEST_ENABLED_ENV = "CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED"
AUTH_TOKEN_ENV = "CATHEDRAL_EXTERNAL_SCORES_TOKEN"
HMAC_SECRET_ENV = "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET"
ALLOW_UNAUTH_ENV = "CATHEDRAL_EXTERNAL_SCORES_ALLOW_UNAUTHENTICATED"
MAX_SCORES_ENV = "CATHEDRAL_EXTERNAL_SCORES_MAX_SCORES"

_DEFAULT_SOURCE = "violet_audio"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class ExternalScoreError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ingest_enabled() -> bool:
    return _env_bool(INGEST_ENABLED_ENV, False)


def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    return _ms_iso(datetime.now(timezone.utc))


def _normalize_iso(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalScoreError(f"missing_{field}")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _ms_iso(dt)
    except Exception:
        raise ExternalScoreError(f"invalid_{field}")


def _float_01(value: Any, field: str, *, required: bool = False) -> float | None:
    if value is None:
        if required:
            raise ExternalScoreError(f"missing_{field}")
        return None
    if isinstance(value, bool):
        raise ExternalScoreError(f"invalid_{field}")
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ExternalScoreError(f"invalid_{field}")
    if not math.isfinite(out) or out < 0.0 or out > 1.0:
        raise ExternalScoreError(f"invalid_{field}")
    return out


def _int_nonnegative(value: Any, field: str, *, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ExternalScoreError(f"invalid_{field}")
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ExternalScoreError(f"invalid_{field}")
    if out < 0:
        raise ExternalScoreError(f"invalid_{field}")
    return out


def _source(value: Any, *, default: str = _DEFAULT_SOURCE) -> str:
    src = str(value or default).strip()
    if not _SOURCE_RE.match(src):
        raise ExternalScoreError("invalid_source")
    return src


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def token_configured() -> bool:
    """True iff a bearer token is set — i.e. ingest can be authenticated."""
    return bool((os.environ.get(AUTH_TOKEN_ENV) or "").strip())


def bearer_authorized(authorization: str | None, x_token: str | None = None) -> bool:
    expected = (os.environ.get(AUTH_TOKEN_ENV) or "").strip()
    if not expected:
        return _env_bool(ALLOW_UNAUTH_ENV, False)
    supplied = ""
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            supplied = authorization[len(prefix):].strip()
    if not supplied and x_token:
        supplied = x_token.strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def verify_hmac(body: bytes, signature: str | None) -> bool:
    """Verify optional raw-body HMAC when CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET is set."""
    secret = (os.environ.get(HMAC_SECRET_ENV) or "").strip()
    if not secret:
        return True
    if not signature:
        return False
    supplied = signature.strip()
    if supplied.startswith("sha256="):
        supplied = supplied[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)


def normalize_report(payload: Any, *, default_source: str = _DEFAULT_SOURCE) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExternalScoreError("invalid_report")

    source = _source(payload.get("source") or payload.get("mechanism"), default=default_source)
    generated_at = _normalize_iso(payload.get("generated_at"), "generated_at")
    max_scores = max(1, int(os.environ.get(MAX_SCORES_ENV, "4096") or "4096"))
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, list) or not raw_scores:
        raise ExternalScoreError("missing_scores")
    if len(raw_scores) > max_scores:
        raise ExternalScoreError("too_many_scores")

    seen: set[str] = set()
    scores: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_scores):
        if not isinstance(raw, dict):
            raise ExternalScoreError(f"invalid_score_{idx}")
        hotkey = str(raw.get("miner_hotkey") or raw.get("hotkey") or "").strip()
        if not hotkey or len(hotkey) > 128:
            raise ExternalScoreError(f"invalid_hotkey_{idx}")
        if hotkey in seen:
            raise ExternalScoreError(f"duplicate_hotkey_{idx}")
        seen.add(hotkey)
        uid_raw = raw.get("uid")
        uid = None if uid_raw is None else _int_nonnegative(uid_raw, f"uid_{idx}")
        score = _float_01(raw.get("score"), f"score_{idx}", required=True)
        assert score is not None
        quality = _float_01(raw.get("quality"), f"quality_{idx}")
        latency = _float_01(raw.get("latency"), f"latency_{idx}")
        validity = _float_01(raw.get("validity"), f"validity_{idx}")
        confidence = _float_01(raw.get("confidence"), f"confidence_{idx}")
        tasks_scored = _int_nonnegative(raw.get("tasks_scored"), f"tasks_scored_{idx}")
        scores.append({
            "miner_hotkey": hotkey,
            "uid": uid,
            "score": round(score, 9),
            "quality": None if quality is None else round(quality, 9),
            "latency": None if latency is None else round(latency, 9),
            "validity": None if validity is None else round(validity, 9),
            "tasks_scored": tasks_scored,
            "confidence": None if confidence is None else round(confidence, 9),
            "meta": raw.get("meta") if isinstance(raw.get("meta"), dict) else {},
        })

    try:
        epoch = int(payload.get("epoch", 0) or 0)
    except (TypeError, ValueError):
        raise ExternalScoreError("invalid_epoch")
    try:
        netuid = None if payload.get("netuid") is None else int(payload.get("netuid"))
    except (TypeError, ValueError):
        raise ExternalScoreError("invalid_netuid")

    normalized: dict[str, Any] = {
        "source": source,
        "mechanism": str(payload.get("mechanism") or source),
        "epoch": epoch,
        "netuid": netuid,
        "generated_at": generated_at,
        "scores": scores,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }
    digest = hashlib.sha256(_canonical(normalized)).hexdigest()
    normalized["report_sha256"] = digest
    normalized["report_id"] = str(payload.get("report_id") or f"ext-{digest[:32]}")
    return normalized


def store_report(store: Store, report: dict[str, Any]) -> dict[str, Any]:
    received_at = _now_iso()
    report_id = str(report["report_id"])
    report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
    scores = list(report.get("scores") or [])

    def _write(conn):
        conn.execute(
            "INSERT OR REPLACE INTO external_score_reports"
            "(id, source, epoch, generated_at_iso, received_at_iso, "
            "report_sha256, score_count, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                report["source"],
                int(report.get("epoch") or 0),
                report["generated_at"],
                received_at,
                report["report_sha256"],
                len(scores),
                report_json,
            ),
        )
        conn.execute("DELETE FROM external_score_entries WHERE report_id=?", (report_id,))
        for s in scores:
            conn.execute(
                "INSERT OR REPLACE INTO external_score_entries"
                "(report_id, source, epoch, miner_hotkey, uid, score, quality, "
                "latency, validity, tasks_scored, confidence, generated_at_iso, "
                "received_at_iso, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    report["source"],
                    int(report.get("epoch") or 0),
                    s["miner_hotkey"],
                    s.get("uid"),
                    float(s["score"]),
                    s.get("quality"),
                    s.get("latency"),
                    s.get("validity"),
                    int(s.get("tasks_scored") or 0),
                    s.get("confidence"),
                    report["generated_at"],
                    received_at,
                    json.dumps(s.get("meta") or {}, sort_keys=True, separators=(",", ":")),
                ),
            )

    store.write(_write)
    return {
        "status": "accepted",
        "report_id": report_id,
        "source": report["source"],
        "epoch": int(report.get("epoch") or 0),
        "score_count": len(scores),
        "received_at": received_at,
        "report_sha256": report["report_sha256"],
    }


def recent_scores(store: Store, *, source: str, since_iso: str) -> dict[str, float]:
    rows = store.query(
        "SELECT miner_hotkey, score, received_at_iso FROM external_score_entries "
        "WHERE source=? AND received_at_iso > ? AND score > 0 "
        "ORDER BY received_at_iso ASC",
        (source, since_iso),
    )
    latest: dict[str, float] = {}
    for r in rows:
        hk = str(r["miner_hotkey"])
        try:
            score = float(r["score"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(score) and score > 0.0:
            latest[hk] = min(1.0, max(0.0, score))
    return latest


def status(store: Store, *, source: str, since_iso: str) -> dict[str, Any]:
    reports = store.query(
        "SELECT id, epoch, generated_at_iso, received_at_iso, score_count, report_sha256 "
        "FROM external_score_reports WHERE source=? "
        "ORDER BY received_at_iso DESC LIMIT 1",
        (source,),
    )
    counts = store.query(
        "SELECT COUNT(*) AS n FROM external_score_entries "
        "WHERE source=? AND received_at_iso > ? AND score > 0",
        (source, since_iso),
    )
    latest = reports[0] if reports else None
    return {
        "latest_report_id": str(latest["id"]) if latest else None,
        "latest_epoch": int(latest["epoch"]) if latest else None,
        "latest_generated_at": str(latest["generated_at_iso"]) if latest else None,
        "latest_received_at": str(latest["received_at_iso"]) if latest else None,
        "latest_score_count": int(latest["score_count"]) if latest else 0,
        "latest_report_sha256": str(latest["report_sha256"]) if latest else None,
        "active_score_count": int(counts[0]["n"] if counts else 0),
    }
