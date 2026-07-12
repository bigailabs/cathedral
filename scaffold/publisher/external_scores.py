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
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store

INGEST_ENABLED_ENV = "CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED"
AUTH_TOKEN_ENV = "CATHEDRAL_EXTERNAL_SCORES_TOKEN"
HMAC_SECRET_ENV = "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET"
ALLOW_UNAUTH_ENV = "CATHEDRAL_EXTERNAL_SCORES_ALLOW_UNAUTHENTICATED"
# Sources requiring their own mandatory HMAC secret (fail 503 if absent)
MANDATORY_HMAC_SOURCES = {"cathedral_confidential_tdx"}
MAX_SCORES_ENV = "CATHEDRAL_EXTERNAL_SCORES_MAX_SCORES"
# Maximum age (seconds) a report's generated_at may lag behind now.
# Default 1 hour.  Reports older than this are rejected as stale.
MAX_REPORT_AGE_SECS_ENV = "CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_AGE_SECS"
# Maximum seconds a report's generated_at may be ahead of now.
# Default 120 s to absorb clock skew.  Reports further in the future are
# rejected.
MAX_REPORT_FUTURE_SECS_ENV = "CATHEDRAL_EXTERNAL_SCORES_MAX_REPORT_FUTURE_SECS"

_DEFAULT_SOURCE = "violet_audio"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Known/allowed `source` labels for POST /v1/external-scores/violet. The route
# name predates any source beyond Violet's own audio scorer; widen this
# allowlist (not the auth/registration/fraction-cap gates) when a new external
# mechanism is wired to post here.  Anything not listed is rejected with
# invalid_source_for_violet_endpoint.
ALLOWED_ENDPOINT_SOURCES = {
    "violet_audio",
    "cathedral_sat_fast",
    "cathedral_confidential_tdx",
}

# Sources that must never be accepted as an incomplete snapshot, regardless of
# whether external blending happens to be enabled for them right now. A
# confidential/attested source's whole trust model is "the report is the full
# truth at its epoch" — a partial report from it must never be stored as if it
# were live-composable.
COMPLETE_REQUIRED_SOURCES = {"cathedral_confidential_tdx"}


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
    """True iff the shared bearer token is set — i.e. ingest can be authenticated."""
    return bool((os.environ.get(AUTH_TOKEN_ENV) or "").strip())


def _source_token_env(source: str) -> str:
    """Env var name for a per-source credential, e.g.
    CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_CONFIDENTIAL_TDX for source
    'cathedral_confidential_tdx'. Minimally compatible with the shared-token
    scheme: sources without a dedicated var keep using the shared token."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", source.strip()).strip("_").upper()
    return f"{AUTH_TOKEN_ENV}_{slug}" if slug else AUTH_TOKEN_ENV


def _source_hmac_secret_env(source: str) -> str:
    """Env var name for a per-source HMAC secret, e.g.
    CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_CONFIDENTIAL_TDX for source
    'cathedral_confidential_tdx'. Derived deterministically from source name."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", source.strip()).strip("_").upper()
    return f"{HMAC_SECRET_ENV}_{slug}" if slug else HMAC_SECRET_ENV


def source_token_configured(source: str) -> bool:
    """True iff *source* has its own dedicated bearer token configured."""
    return bool((os.environ.get(_source_token_env(source)) or "").strip())


def _bearer_supplied(authorization: str | None, x_token: str | None) -> str:
    supplied = ""
    if authorization:
        prefix = "Bearer "
        if authorization.startswith(prefix):
            supplied = authorization[len(prefix):].strip()
    if not supplied and x_token:
        supplied = x_token.strip()
    return supplied


def bearer_authorized(authorization: str | None, x_token: str | None = None) -> bool:
    expected = (os.environ.get(AUTH_TOKEN_ENV) or "").strip()
    if not expected:
        return _env_bool(ALLOW_UNAUTH_ENV, False)
    supplied = _bearer_supplied(authorization, x_token)
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def bearer_authorized_for_source(
    source: str, authorization: str | None, x_token: str | None = None
) -> bool:
    """Source-scoped auth (#7): when *source* has a dedicated token configured,
    ONLY that token authorizes a report claiming this source label — the shared
    token no longer authorizes it, so one source's credential cannot submit
    another source's label. Sources without a dedicated token keep using the
    shared bearer_authorized() check, so this is backward compatible."""
    env_name = _source_token_env(source)
    expected = (os.environ.get(env_name) or "").strip()
    if not expected:
        return bearer_authorized(authorization, x_token)
    supplied = _bearer_supplied(authorization, x_token)
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


def verify_hmac_for_source(source: str, body: bytes, signature: str | None) -> tuple[bool, bool]:
    """Verify raw-body HMAC with source-specific enforcement.

    Returns (is_valid: bool, should_fail_503: bool) where:
    - is_valid: HMAC signature verified successfully
    - should_fail_503: dedicated secret required but not configured (fail-closed)

    For sources in MANDATORY_HMAC_SOURCES (e.g., cathedral_confidential_tdx):
    - Dedicated source-scoped HMAC secret is required
    - Global secret does NOT substitute
    - Missing secret returns (False, True) -> 503
    - Bad/missing signature returns (False, False) -> 401

    For other sources:
    - Optional global HMAC secret (backward compatible)
    - Missing/invalid signature returns (False, False) -> 401
    - No secret configured returns (True, False) -> pass (signature optional)
    """
    if source in MANDATORY_HMAC_SOURCES:
        # Mandatory source-scoped HMAC: fail closed if secret missing
        env_name = _source_hmac_secret_env(source)
        secret = (os.environ.get(env_name) or "").strip()
        if not secret:
            return False, True  # 503: secret required but absent
        if not signature:
            return False, False  # 401: signature required but missing
        supplied = signature.strip()
        if supplied.startswith("sha256="):
            supplied = supplied[len("sha256="):]
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        is_valid = hmac.compare_digest(supplied, expected)
        return is_valid, False
    else:
        # Optional global HMAC (backward compatible)
        secret = (os.environ.get(HMAC_SECRET_ENV) or "").strip()
        if not secret:
            return True, False  # No secret configured, pass
        if not signature:
            return False, False  # 401: signature required but missing
        supplied = signature.strip()
        if supplied.startswith("sha256="):
            supplied = supplied[len("sha256="):]
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        is_valid = hmac.compare_digest(supplied, expected)
        return is_valid, False


def max_report_age_secs() -> float:
    """Maximum allowed age (in seconds) of a report's generated_at."""
    try:
        return max(0.0, float(os.environ.get(MAX_REPORT_AGE_SECS_ENV, "3600") or "3600"))
    except ValueError:
        return 3600.0


def max_report_future_secs() -> float:
    """Maximum seconds a report's generated_at may be ahead of now."""
    try:
        return max(0.0, float(os.environ.get(MAX_REPORT_FUTURE_SECS_ENV, "120") or "120"))
    except ValueError:
        return 120.0


def _parse_iso_dt(value: str) -> datetime:
    """Parse an ISO-8601 string to a tz-aware UTC datetime."""
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_report(
    payload: Any,
    *,
    default_source: str = _DEFAULT_SOURCE,
    now: datetime | None = None,
    enforce_max_age: bool,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExternalScoreError("invalid_report")

    source = _source(payload.get("source") or payload.get("mechanism"), default=default_source)
    generated_at = _normalize_iso(payload.get("generated_at"), "generated_at")

    # -- freshness gate (generated_at) ------------------------------------
    ref = now or datetime.now(timezone.utc)
    try:
        gen_dt = _parse_iso_dt(generated_at)
    except Exception:
        raise ExternalScoreError("invalid_generated_at")
    age = (ref - gen_dt).total_seconds()
    if enforce_max_age and age > max_report_age_secs():
        raise ExternalScoreError("report_too_old")
    if age < -max_report_future_secs():
        raise ExternalScoreError("report_in_future")

    # -- complete flag (required for live external sources) ----------------
    complete = payload.get("complete")
    if complete is not None:
        if not isinstance(complete, bool):
            raise ExternalScoreError("invalid_complete")

    # (#1) An empty scores list is only meaningful for a COMPLETE snapshot --
    # it is the source asserting "zero live miners, revoke everyone". An
    # incomplete or legacy (complete omitted) report with no scores carries no
    # information and stays rejected, exactly as before.
    if complete is not True and source in COMPLETE_REQUIRED_SOURCES:
        raise ExternalScoreError("complete_required_for_source")
    max_scores = max(1, int(os.environ.get(MAX_SCORES_ENV, "4096") or "4096"))
    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, list):
        raise ExternalScoreError("missing_scores")
    if not raw_scores and complete is not True:
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
        "complete": bool(complete) if complete is not None else None,
        "netuid": netuid,
        "generated_at": generated_at,
        "scores": scores,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
    }
    digest = hashlib.sha256(_canonical(normalized)).hexdigest()
    normalized["report_sha256"] = digest
    normalized["report_id"] = str(payload.get("report_id") or f"ext-{digest[:32]}")
    return normalized


def normalize_report(
    payload: Any,
    *,
    default_source: str = _DEFAULT_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    return _normalize_report(
        payload,
        default_source=default_source,
        now=now,
        enforce_max_age=True,
    )


def normalize_stale_idempotent_retry(
    store: Store,
    payload: Any,
    *,
    default_source: str = _DEFAULT_SOURCE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize an over-age report only when its exact digest is persisted.

    This disables only the maximum-age check. Future-time validation and the
    full report contract remain enforced by the shared normalization path.
    """
    report = _normalize_report(
        payload,
        default_source=default_source,
        now=now,
        enforce_max_age=False,
    )
    rows = store.query(
        "SELECT report_sha256 FROM external_score_reports "
        "WHERE source=? AND epoch=? AND report_sha256=? LIMIT 1",
        (report["source"], report["epoch"], report["report_sha256"]),
    )
    if not rows:
        raise ExternalScoreError("report_too_old")
    return report


def store_report(store: Store, report: dict[str, Any]) -> dict[str, Any]:
    """Persist a normalized report, enforcing epoch monotonicity.

    Epoch must be strictly increasing per source.  A byte-identical retry
    (same report_sha256) at the same epoch is idempotent.  An older or
    conflicting epoch is rejected.
    """
    received_at = _now_iso()
    report_id = str(report["report_id"])
    report_json = json.dumps(report, sort_keys=True, separators=(",", ":"))
    scores = list(report.get("scores") or [])
    epoch = int(report.get("epoch") or 0)
    source = report["source"]
    digest = report["report_sha256"]

    def _write(conn):
        # -- epoch monotonicity gate --------------------------------------
        cur = conn.execute(
            "SELECT epoch, report_sha256 FROM external_score_reports "
            "WHERE source=? ORDER BY epoch DESC LIMIT 1",
            (source,),
        )
        row = cur.fetchone()
        if row is not None:
            stored_epoch = int(row[0])
            stored_digest = str(row[1])
            if epoch < stored_epoch:
                raise ExternalScoreError("epoch_too_old")
            if epoch == stored_epoch and stored_digest != digest:
                raise ExternalScoreError("epoch_conflict")
            if epoch == stored_epoch and stored_digest == digest:
                # Byte-identical retry at the same epoch: idempotent accept.
                return None  # sentinel: already stored

        conn.execute(
            "INSERT OR REPLACE INTO external_score_reports"
            "(id, source, epoch, generated_at_iso, received_at_iso, "
            "report_sha256, score_count, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                source,
                epoch,
                report["generated_at"],
                received_at,
                digest,
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
                    source,
                    epoch,
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
        return True  # newly stored

    result = store.write(_write)
    return {
        "status": "accepted",
        "report_id": report_id,
        "source": source,
        "epoch": epoch,
        "score_count": len(scores),
        "received_at": received_at,
        "report_sha256": digest,
        "idempotent": result is None,
    }


def recent_scores(store: Store, *, source: str, since_iso: str) -> dict[str, float]:
    """Legacy non-snapshot query: latest positive scores received after since_iso.

    Kept for backward compatibility with callers that do not use snapshot
    semantics.  New blending code should prefer :func:`latest_snapshot_scores`.
    """
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


def latest_snapshot_scores(
    store: Store,
    *,
    source: str,
    max_age_secs: float = 3600.0,
    now: datetime | None = None,
) -> dict[str, float] | None:
    """Return the score vector from the latest complete report for *source*.

    Complete-snapshot semantics: a complete report is the full truth at its
    epoch.  An omitted hotkey has an implicit score of zero (revoked).  An
    explicit zero also revokes.

    Returns ``None`` when no valid (complete, fresh) snapshot exists, meaning
    the caller should treat external scores as unavailable and fail closed.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = _ms_iso(now - timedelta(seconds=max_age_secs))
    # Find the latest report for this source.
    reports = store.query(
        "SELECT id, epoch, generated_at_iso, received_at_iso, report_json "
        "FROM external_score_reports WHERE source=? "
        "ORDER BY epoch DESC LIMIT 1",
        (source,),
    )
    if not reports:
        return None
    report_row = reports[0]
    generated_at_iso = str(report_row["generated_at_iso"])
    # Freshness: reject if generated_at is older than max_age_secs.
    if generated_at_iso <= cutoff:
        return None
    # Parse the stored report JSON to check the complete flag.
    try:
        report_obj = json.loads(report_row["report_json"])
    except Exception:
        return None
    if not report_obj.get("complete"):
        # For live external sources, require complete=true.
        return None
    # Pull all entries for this specific report (the full snapshot).
    report_id = str(report_row["id"])
    rows = store.query(
        "SELECT miner_hotkey, score FROM external_score_entries "
        "WHERE report_id=? AND source=?",
        (report_id, source),
    )
    scores: dict[str, float] = {}
    for r in rows:
        hk = str(r["miner_hotkey"])
        try:
            score = float(r["score"])
        except (TypeError, ValueError):
            continue
        if math.isfinite(score) and score > 0.0:
            scores[hk] = min(1.0, max(0.0, score))
        # Explicit zero or omitted: not included -> revoked.
    return scores


def status(store: Store, *, source: str, since_iso: str) -> dict[str, Any]:
    """Status derived from the LATEST epoch report only (#3).

    Prior behavior counted every positive entry received since ``since_iso``
    across ALL stored reports for the source, so a fresh zero/omission
    snapshot could report ``active_score_count == 0`` while stale positive
    rows from an older report were still summed in as "active". A newer
    complete snapshot is the full truth at its epoch (see
    :func:`latest_snapshot_scores`), so status must agree with it: only the
    latest report counts, and only when it is both COMPLETE and fresher than
    ``since_iso`` (generated_at cutoff, same convention callers already use
    for the blend window).
    """
    reports = store.query(
        "SELECT id, epoch, generated_at_iso, received_at_iso, score_count, "
        "report_sha256, report_json "
        "FROM external_score_reports WHERE source=? "
        "ORDER BY epoch DESC LIMIT 1",
        (source,),
    )
    latest = reports[0] if reports else None
    is_complete = False
    is_fresh = False
    active_score_count = 0
    if latest is not None:
        try:
            report_obj = json.loads(latest["report_json"])
            is_complete = bool(report_obj.get("complete"))
        except Exception:
            is_complete = False
        is_fresh = str(latest["generated_at_iso"]) > str(since_iso)
        if is_complete and is_fresh:
            counts = store.query(
                "SELECT COUNT(*) AS n FROM external_score_entries "
                "WHERE report_id=? AND score > 0",
                (str(latest["id"]),),
            )
            active_score_count = int(counts[0]["n"] if counts else 0)
    return {
        "latest_report_id": str(latest["id"]) if latest else None,
        "latest_epoch": int(latest["epoch"]) if latest else None,
        "latest_generated_at": str(latest["generated_at_iso"]) if latest else None,
        "latest_received_at": str(latest["received_at_iso"]) if latest else None,
        "latest_score_count": int(latest["score_count"]) if latest else 0,
        "latest_report_sha256": str(latest["report_sha256"]) if latest else None,
        "latest_complete": is_complete,
        "latest_fresh": is_fresh,
        "active_score_count": active_score_count,
    }
