#!/usr/bin/env python3
"""Publish a bounded, sanitized SN39 validator event stream and status card."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SOURCE = Path("/var/log/cathedral-validator/validator-events.jsonl")
PUBLIC_ROOT = Path("/var/lib/cathedral-public-evidence")
LOG_ROOT = PUBLIC_ROOT / "logs"
INDEX = PUBLIC_ROOT / "index.json"
RELEASE = PUBLIC_ROOT / "release.json"
RELEASE_SIGNATURE = PUBLIC_ROOT / "release.json.sig"
RELEASE_KEYS = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "provenance"
    / "release-attestation-keys.json"
)
RELEASE_KEY_ID = "cathedral-release-attestation-sn39-20260724"
MAX_EVENTS = 200
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_EVENT_AGE_SECONDS = 2100
MAX_FUTURE_SKEW_SECONDS = 300
STATUS_VALID_SECONDS = 125
EVENT_TIMESTAMP = re.compile(
    r"^(?P<year>[0-9]{4})-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
MIN_EVENT_YEAR = 2024
MAX_EVENT_YEAR = 2100

ALLOWED_EVENTS = frozenset(
    {
        "STARTUP",
        "PROVENANCE_AUDIT_FAIL",
        "PROVENANCE_AUDIT_NOT_PROVEN",
        "PROVENANCE_AUDIT_PASS",
        "PROVENANCE_AUDIT_SKIPPED",
        "PROVENANCE_AUDIT_UNRESOLVED",
        "PROVENANCE_HEALTH_GATE_FAILED",
        "PROVENANCE_RESERVATION_REFUSED",
        "PROVENANCE_STATE_STALE_SKIPPED",
        "PROVENANCE_STATE_WRITE_FAILED",
        "PROVENANCE_VECTOR_MISMATCH",
        "LAUNCH_REWARDED_SET_GATE_PASS",
        "PENDING_RECEIPT_CONTRADICTION",
        "PENDING_RECEIPT_NOT_PROVEN",
        "PENDING_RECEIPT_RECOVERED",
        "TICK_FAILED",
        "VECTOR_ACCEPTED",
        "VECTOR_REJECTED",
        "WEIGHTS_DRY_RUN",
        "WEIGHTS_SUBMITTED",
    }
)
EVENT_STATUS = {
    "STARTUP": "INFO",
    "PROVENANCE_AUDIT_FAIL": "FAIL",
    "PROVENANCE_AUDIT_NOT_PROVEN": "NOT_PROVEN",
    "PROVENANCE_AUDIT_PASS": "PASS",
    "PROVENANCE_AUDIT_SKIPPED": "INFO",
    "PROVENANCE_AUDIT_UNRESOLVED": "NOT_PROVEN",
    "PROVENANCE_HEALTH_GATE_FAILED": "FAIL",
    "PROVENANCE_RESERVATION_REFUSED": "FAIL",
    "PROVENANCE_STATE_STALE_SKIPPED": "NOT_PROVEN",
    "PROVENANCE_STATE_WRITE_FAILED": "NOT_PROVEN",
    "PROVENANCE_VECTOR_MISMATCH": "FAIL",
    "LAUNCH_REWARDED_SET_GATE_PASS": "PASS",
    "PENDING_RECEIPT_CONTRADICTION": "FAIL",
    "PENDING_RECEIPT_NOT_PROVEN": "NOT_PROVEN",
    "PENDING_RECEIPT_RECOVERED": "PASS",
    "TICK_FAILED": "FAIL",
    "VECTOR_ACCEPTED": "PASS",
    "VECTOR_REJECTED": "FAIL",
    "WEIGHTS_DRY_RUN": "PASS",
    "WEIGHTS_SUBMITTED": "PASS",
}
EVENT_REMEDIATION = {
    "PROVENANCE_AUDIT_FAIL": "inspect the validator-local audit log; thin authority is unaffected",
    "PROVENANCE_AUDIT_NOT_PROVEN": "keep thin authority until every anchored outcome has replayable evidence",
    "PROVENANCE_AUDIT_UNRESOLVED": "inspect the validator-local audit log and evidence endpoint",
    "PROVENANCE_RESERVATION_REFUSED": "inspect the validator-local state fence; nothing was submitted",
    "PROVENANCE_STATE_WRITE_FAILED": "repair the validator-local state path; thin authority is unaffected",
    "PROVENANCE_VECTOR_MISMATCH": "keep thin authority and inspect the validator-local discrepancy log",
    "PROVENANCE_HEALTH_GATE_FAILED": "keep thin authority and inspect the validator-local provenance verdict",
    "PENDING_RECEIPT_RECOVERED": (
        "verify the published exact transaction proof; never retry the recovered attempt"
    ),
    "PENDING_RECEIPT_CONTRADICTION": (
        "stop every writer and inspect the durable journal and named transaction; "
        "never submit a replacement"
    ),
    "PENDING_RECEIPT_NOT_PROVEN": (
        "wait for archive proof and restart to re-prove the exact fenced "
        "transaction; never submit a replacement"
    ),
    "TICK_FAILED": (
        "inspect the validator-local log and durable attempt journal; a named "
        "extrinsic may have finalized and automatic retry remains blocked"
    ),
    "VECTOR_REJECTED": "inspect the validator-local verification log; nothing was submitted",
}
ALLOWED_FIELDS = (
    "ts",
    "event",
    "stage",
    "mode",
    "status",
    "duration_ms",
    "artifact",
    "detail",
    "remediation",
)
TEXT_LIMITS = {
    "ts": 64,
    "event": 96,
    "stage": 48,
    "mode": 48,
    "status": 32,
    "artifact": 160,
    "detail": 512,
    "remediation": 512,
}
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9:])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")
BASE58_ID = re.compile(r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{40,64}(?![A-Za-z0-9])")
BRACKETED_IDENTIFIERS = re.compile(r"\[[^\]]*(?:\]|$)")
SECRETISH = re.compile(
    r"""(?ix)
    (["']?)
    (?:bearer|basic|token|secret|hmac|api[_-]?key|authorization|
       password|private[_-]?key)
    \1
    (?:(?:\s*[=:]\s*)|\s+)
    (?:(?:bearer|basic)\s+)?
    (?:"[^"]*"|'[^']*'|\S+)
    """
)
PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]{1,64}-----.*?(?:-----END [A-Z0-9 ]{1,64}-----|$)",
    re.IGNORECASE | re.DOTALL,
)
JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\."
    r"[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"
)
URL_CREDENTIAL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s]+@")
URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s]+")
HEX_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])")
HIGH_ENTROPY_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_+/=-])[A-Za-z0-9_+/=-]{24,}(?![A-Za-z0-9_+/=-])"
)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
WEIGHT_DETAIL = re.compile(
    r"authority=(thin|full_provenance) uids=(\d{1,5}) "
    r"(?:block=\d{1,12} )?"
    r"(?:burn_uid=(\d{1,5}) burn_share=(0(?:\.\d{1,12})?|1(?:\.0{1,12})?) )?"
    r"vector=([0-9:,.\-]{1,256})"
)
STARTUP_DETAIL = re.compile(
    r"submission_authority=(thin|full_provenance) "
    r"provenance=(off|shadow|authority) "
    r"policy_pin=[^\s\x00-\x1f\x7f]{1,128} network=finney netuid=39"
)
STARTUP_MODE_PAIRS = frozenset(
    {
        ("thin", "off"),
        ("thin", "shadow"),
        ("full_provenance", "authority"),
    }
)
SAFE_STAGE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SAFE_MODES = frozenset({"thin", "full_provenance"})
ALLOWED_RAW_STATUSES = {
    event: frozenset({status}) for event, status in EVENT_STATUS.items()
}
ALLOWED_RAW_STATUSES["WEIGHTS_DRY_RUN"] = frozenset({"PASS", "FAIL"})


def scrub(value: str, limit: int) -> str:
    text = PEM_BLOCK.sub("<redacted-secret>", value)
    text = CONTROL.sub(" ", text)
    text = URL.sub(
        lambda match: URL_CREDENTIAL.sub(
            r"\1<redacted-secret>@",
            match.group(0).split("#", 1)[0].split("?", 1)[0],
        ),
        text,
    )
    text = SECRETISH.sub("<redacted-secret>", text)
    text = URL_CREDENTIAL.sub(r"\1<redacted-secret>@", text)
    text = JWT.sub("<redacted-secret>", text)
    text = HEX_TOKEN.sub("<redacted-secret>", text)
    text = HIGH_ENTROPY_TOKEN.sub("<redacted-secret>", text)
    text = ABSOLUTE_PATH.sub("<redacted-path>", text)
    text = BRACKETED_IDENTIFIERS.sub("<redacted-identifiers>", text)
    text = BASE58_ID.sub("<redacted-id>", text)
    text = text.replace("|", "/")
    return text[:limit]


def public_detail(event: str, raw: Any) -> str | None:
    """Convert private diagnostics into fixed public templates."""
    detail = raw if isinstance(raw, str) else ""
    if event == "STARTUP":
        match = STARTUP_DETAIL.fullmatch(detail)
        if match is None:
            return None
        modes = match.groups()
        return {
            ("thin", "off"): "thin authority started; provenance audit is off",
            (
                "thin",
                "shadow",
            ): "thin authority and concurrent provenance shadow started",
            (
                "full_provenance",
                "authority",
            ): "FULL provenance authority started",
        }.get(modes)
    if event == "VECTOR_ACCEPTED":
        return "signed vector passed signature, audience, policy, freshness, and rollback gates"
    if event == "VECTOR_REJECTED":
        return "signed vector was rejected; nothing was submitted"
    if event in ("WEIGHTS_DRY_RUN", "WEIGHTS_SUBMITTED"):
        match = WEIGHT_DETAIL.fullmatch(detail)
        if match is None:
            return (
                "weight result recorded; detailed values remain in validator-local logs"
            )
        authority, count, burn_uid, burn_share, vector = match.groups()
        action = (
            "submitted"
            if event == "WEIGHTS_SUBMITTED"
            else "verified without a chain write"
        )
        burn = (
            f" burn_uid={burn_uid} burn_share={burn_share}"
            if burn_uid and burn_share
            else ""
        )
        return (
            f"authority={authority} uids={count}{burn} vector={vector} action={action}"
        )
    if event == "PROVENANCE_AUDIT_PASS":
        return "whole-epoch FULL provenance audit passed"
    if event == "PROVENANCE_AUDIT_NOT_PROVEN":
        replay = (
            "positive raw Intel TDX evidence replayed; "
            if "positive raw evidence replayed for " in detail
            else ""
        )
        return replay + "whole-epoch FULL assurance is not established"
    if event == "PROVENANCE_AUDIT_SKIPPED":
        return "the prior bounded provenance audit was still in flight"
    if event == "PROVENANCE_AUDIT_UNRESOLVED":
        return "the bounded provenance audit outcome was not captured"
    if event == "PROVENANCE_RESERVATION_REFUSED":
        return "the provenance state fence refused the reservation"
    if event == "PROVENANCE_STATE_STALE_SKIPPED":
        return "a newer provenance state already exists"
    if event == "PROVENANCE_STATE_WRITE_FAILED":
        return "the observational provenance state could not be persisted"
    if event == "PROVENANCE_VECTOR_MISMATCH":
        return "independent recomputation disagreed with the signed vector"
    if event == "PROVENANCE_AUDIT_FAIL":
        return "the provenance audit failed"
    if event == "PROVENANCE_HEALTH_GATE_FAILED":
        return "the current provenance health gate failed"
    if event == "LAUNCH_REWARDED_SET_GATE_PASS":
        return "every rewarded miner independently replayed with exact vector and UID agreement"
    if event == "PENDING_RECEIPT_RECOVERED":
        return "exact journaled transaction re-proven; no second chain write"
    if event == "PENDING_RECEIPT_CONTRADICTION":
        return (
            "the exact signed attempt has a positive durable or historical "
            "contradiction; no replacement was submitted"
        )
    if event == "PENDING_RECEIPT_NOT_PROVEN":
        return (
            "the exact signed attempt remains fenced while finalized archive "
            "proof is unavailable; no replacement was submitted"
        )
    if event == "TICK_FAILED":
        return (
            "the validator tick failed; a write may have finalized, so inspect "
            "the named extrinsic and durable attempt journal before recovery"
        )
    return None


def parse_weight_boundary(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    match = WEIGHT_DETAIL.fullmatch(raw)
    if match is None:
        return None
    authority, count_text, burn_uid_text, burn_share_text, vector_text = match.groups()
    try:
        count = int(count_text)
        burn_uid = int(burn_uid_text) if burn_uid_text is not None else None
        burn_share = float(burn_share_text) if burn_share_text is not None else None
        uid_weights: dict[str, float] = {}
        for item in vector_text.split(","):
            uid_text, weight_text = item.split(":", 1)
            uid = str(int(uid_text))
            weight = float(weight_text)
            if (
                uid in uid_weights
                or not math.isfinite(weight)
                or not 0.0 <= weight <= 1.0
            ):
                return None
            uid_weights[uid] = weight
    except (TypeError, ValueError):
        return None
    if count != len(uid_weights):
        return None
    return {
        "authority": authority,
        "uid_count": count,
        "burn_uid": burn_uid,
        "burn_share": burn_share,
        "uid_weights": uid_weights,
    }


def is_launch_weight_boundary(event: dict[str, Any] | None) -> bool:
    if event is None:
        return False
    weights = event.get("uid_weights")
    burn_uid = event.get("burn_uid")
    if (
        not isinstance(weights, dict)
        or isinstance(burn_uid, bool)
        or not isinstance(burn_uid, int)
        or len(weights) != 2
        or str(burn_uid) not in weights
    ):
        return False
    rewarded = [value for uid, value in weights.items() if uid != str(burn_uid)]
    if len(rewarded) != 1:
        return False
    worker_weight = rewarded[0]
    burn_weight = weights[str(burn_uid)]
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (worker_weight, burn_weight)
    ):
        return False
    return (
        event.get("event") in ("WEIGHTS_SUBMITTED", "PENDING_RECEIPT_RECOVERED")
        and event.get("status") == "PASS"
        and event.get("authority") == "thin"
        and event.get("uid_count") == 2
        and isinstance(event.get("burn_share"), (int, float))
        and not isinstance(event.get("burn_share"), bool)
        and math.isclose(float(event["burn_share"]), 0.1, rel_tol=0.0, abs_tol=1e-12)
        and math.isclose(float(worker_weight), 0.9, rel_tol=0.0, abs_tol=1e-12)
        and math.isclose(float(burn_weight), 0.1, rel_tol=0.0, abs_tol=1e-12)
    )


def parse_event_time(raw: str) -> datetime | None:
    match = EVENT_TIMESTAMP.fullmatch(raw)
    if match is None:
        return None
    year = int(match.group("year"))
    if not MIN_EVENT_YEAR <= year <= MAX_EVENT_YEAR:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except (OverflowError, ValueError):
        return None


def clean_event(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    event = document.get("event")
    timestamp = document.get("ts")
    if not isinstance(event, str) or not isinstance(timestamp, str):
        return None
    if event not in ALLOWED_EVENTS:
        return None
    startup_modes: tuple[str, str] | None = None
    if event == "STARTUP":
        detail = document.get("detail")
        match = STARTUP_DETAIL.fullmatch(detail) if isinstance(detail, str) else None
        if match is None:
            return None
        startup_modes = match.groups()
        if (
            startup_modes not in STARTUP_MODE_PAIRS
            or document.get("mode") != startup_modes[0]
            or document.get("authority") != startup_modes[0]
            or document.get("provenance_mode") != startup_modes[1]
        ):
            return None
    raw_status = document.get("status")
    if raw_status not in ALLOWED_RAW_STATUSES[event]:
        return None
    if parse_event_time(timestamp) is None:
        return None
    clean: dict[str, Any] = {}
    for key in ALLOWED_FIELDS:
        if key in ("detail", "remediation", "status"):
            continue
        value = document.get(key)
        if value is None:
            continue
        if key == "duration_ms":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                continue
            clean[key] = min(86_400_000.0, max(0, round(float(value), 3)))
            continue
        if not isinstance(value, str):
            continue
        if key == "event":
            clean[key] = event
            continue
        if key == "ts":
            clean[key] = timestamp
            continue
        if key == "stage":
            clean[key] = value if SAFE_STAGE.fullmatch(value) else "unknown"
            continue
        if key == "mode":
            clean[key] = value if value in SAFE_MODES else "unknown"
            continue
        if key == "artifact":
            # Public artifacts are immutable content digests. Drop any
            # unexpected free-form path or identifier instead of redacting it.
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                continue
            clean[key] = value
            continue
        clean[key] = scrub(value, TEXT_LIMITS[key])
    clean["status"] = raw_status
    if startup_modes is not None:
        clean["authority"] = startup_modes[0]
        clean["provenance_mode"] = startup_modes[1]
    detail = public_detail(event, document.get("detail"))
    if detail:
        clean["detail"] = detail[: TEXT_LIMITS["detail"]]
    if event in (
        "WEIGHTS_DRY_RUN",
        "WEIGHTS_SUBMITTED",
        "PENDING_RECEIPT_RECOVERED",
    ):
        boundary = parse_weight_boundary(document.get("detail"))
        if boundary is not None:
            clean.update(boundary)
    remediation = EVENT_REMEDIATION.get(event)
    if remediation:
        clean["remediation"] = remediation[: TEXT_LIMITS["remediation"]]
    if not clean.get("event") or not clean.get("ts"):
        return None
    return clean


def tail_events() -> list[dict[str, Any]]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(SOURCE, flags)
    except OSError:
        return []
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o002
            or info.st_size < 0
        ):
            return []
        offset = max(0, info.st_size - MAX_SOURCE_BYTES)
        os.lseek(descriptor, offset, os.SEEK_SET)
        payload = os.read(descriptor, MAX_SOURCE_BYTES)
    except OSError:
        return []
    finally:
        os.close(descriptor)
    if offset:
        _discarded, _separator, payload = payload.partition(b"\n")
    lines = payload.splitlines()
    events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
    for raw in lines:
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        event = clean_event(document)
        if event is not None:
            events.append(event)
    return list(events)


def read_public_bytes(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 1024 * 1024
            or stat.S_IMODE(info.st_mode) & 0o002
        ):
            os.close(descriptor)
            return None
        try:
            payload = os.read(descriptor, info.st_size + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return None
    return payload


def read_public_json(path: Path) -> dict[str, Any]:
    payload = read_public_bytes(path)
    if payload is None:
        return {}
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def read_signed_release() -> dict[str, Any]:
    """Return release.json only when its detached root signature verifies."""
    release_bytes = read_public_bytes(RELEASE)
    signature_bytes = read_public_bytes(RELEASE_SIGNATURE)
    keys = read_public_json(RELEASE_KEYS)
    if release_bytes is None or signature_bytes is None:
        return {}
    try:
        release = json.loads(release_bytes)
        signature = json.loads(signature_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    release_attestation = (
        release.get("release_attestation") if isinstance(release, dict) else None
    )
    if (
        not isinstance(release, dict)
        or not isinstance(signature, dict)
        or not isinstance(release_attestation, dict)
        or set(signature)
        != {
            "algorithm",
            "key_id",
            "payload",
            "payload_sha256",
            "signature",
        }
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != RELEASE_KEY_ID
        or signature.get("payload") != "release.json exact bytes"
        or signature.get("payload_sha256")
        != "sha256:" + hashlib.sha256(release_bytes).hexdigest()
        or release_attestation.get("key_id") != RELEASE_KEY_ID
    ):
        return {}
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return {}

    try:
        public = base64.b64decode(keys[RELEASE_KEY_ID], validate=True)
        detached = base64.b64decode(signature["signature"], validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(detached, release_bytes)
    except (InvalidSignature, KeyError, TypeError, ValueError):
        return {}
    return release


def latest_matching(
    events: list[dict[str, Any]], prefixes: tuple[str, ...]
) -> dict[str, Any] | None:
    for event in reversed(events):
        name = str(event.get("event", ""))
        if name.startswith(prefixes):
            return event
    return None


def event_age_seconds(event: dict[str, Any] | None, now: datetime) -> float | None:
    if event is None:
        return None
    raw = event.get("ts")
    if not isinstance(raw, str):
        return None
    parsed = parse_event_time(raw)
    if parsed is None:
        return None
    try:
        age = (now - parsed).total_seconds()
    except OverflowError:
        return None
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return None
    return max(0.0, age)


def build_status(events: list[dict[str, Any]]) -> dict[str, Any]:
    index = read_public_json(INDEX)
    release = read_signed_release()
    provenance = latest_matching(events, ("PROVENANCE_",))
    rewarded_set = latest_matching(events, ("LAUNCH_REWARDED_SET_GATE_",))
    startup = latest_matching(events, ("STARTUP",))
    # Authority status is about an observed live submission, not merely a
    # signed vector passing preflight or a no-write canary. Keep the last
    # successful live submission until a later tick records a real failure.
    authority = latest_matching(
        events,
        (
            "WEIGHTS_SUBMITTED",
            "PENDING_RECEIPT_CONTRADICTION",
            "PENDING_RECEIPT_NOT_PROVEN",
            "PENDING_RECEIPT_RECOVERED",
            "TICK_FAILED",
            "VECTOR_REJECTED",
        ),
    )
    now = datetime.now(UTC)
    authority_age = event_age_seconds(authority, now)
    provenance_age = event_age_seconds(provenance, now)
    rewarded_set_age = event_age_seconds(rewarded_set, now)
    authority_fresh = (
        authority_age is not None and authority_age <= MAX_EVENT_AGE_SECONDS
    )
    provenance_fresh = (
        provenance_age is not None and provenance_age <= MAX_EVENT_AGE_SECONDS
    )
    rewarded_set_fresh = (
        rewarded_set_age is not None and rewarded_set_age <= MAX_EVENT_AGE_SECONDS
    )
    authority_event = str((authority or {}).get("event", ""))
    provenance_event = str((provenance or {}).get("event", ""))
    mode_event = latest_matching(
        events,
        ("WEIGHTS_SUBMITTED", "WEIGHTS_DRY_RUN", "PENDING_RECEIPT_RECOVERED"),
    )
    if startup is not None:
        current_authority_mode = str(startup.get("authority", "NOT_PROVEN"))
        current_provenance_mode = str(startup.get("provenance_mode", "NOT_PROVEN"))
    elif (mode_event or {}).get("authority") in SAFE_MODES:
        current_authority_mode = str(mode_event["authority"])
        current_provenance_mode = (
            "authority" if current_authority_mode == "full_provenance" else "NOT_PROVEN"
        )
    else:
        current_authority_mode = "NOT_PROVEN"
        current_provenance_mode = "NOT_PROVEN"
    if authority_event in (
        "PENDING_RECEIPT_CONTRADICTION",
        "TICK_FAILED",
        "VECTOR_REJECTED",
    ):
        authority_status = "FAIL"
    elif authority_event == "PENDING_RECEIPT_NOT_PROVEN":
        authority_status = "NOT_PROVEN"
    elif current_authority_mode == "thin" and is_launch_weight_boundary(authority):
        authority_status = "PASS"
    else:
        authority_status = "NOT_PROVEN"
    provenance_status = EVENT_STATUS.get(provenance_event, "NOT_PROVEN")
    if provenance_status not in ("PASS", "FAIL"):
        provenance_status = "NOT_PROVEN"
    detail = str((provenance or {}).get("detail", ""))
    positive_replay = (
        "PASS"
        if provenance_fresh
        and (
            provenance_event == "PROVENANCE_AUDIT_PASS"
            or (
                provenance_event == "PROVENANCE_AUDIT_NOT_PROVEN"
                and "positive raw Intel TDX evidence replayed" in detail
            )
        )
        else "NOT_PROVEN"
    )
    launch_checkpoint = (
        ((release.get("launch_submission") or {}).get("evidence_checkpoint") or {})
        if isinstance(release.get("launch_submission"), dict)
        else {}
    )
    launch_public_assurance = (
        launch_checkpoint.get("public_assurance")
        if isinstance(launch_checkpoint, dict)
        else None
    )
    launch_whole_epoch_full = (
        "PASS" if launch_public_assurance == "full" else "NOT_PROVEN"
    )
    return {
        "schema": "cathedral.sn39.validator-status.v1",
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "valid_until": (now + timedelta(seconds=STATUS_VALID_SECONDS))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "validity": (
            "PASS and FAIL observations are current only through valid_until; "
            "after that instant every gate is NOT_PROVEN."
        ),
        "network": "finney",
        "netuid": 39,
        "authority": {
            "mode": current_authority_mode,
            "status": authority_status if authority_fresh else "NOT_PROVEN",
            "burn_share": "0.10"
            if authority_fresh
            and current_authority_mode == "thin"
            and is_launch_weight_boundary(authority)
            else None,
            "fresh": authority_fresh,
            "age_seconds": round(authority_age, 3)
            if authority_age is not None
            else None,
            "latest_event": (authority or {}).get("event")
            if authority_fresh
            else "STALE",
            "detail": (
                (authority or {}).get("detail")
                if authority_fresh
                else f"no live submission observed within {MAX_EVENT_AGE_SECONDS} seconds"
            ),
        },
        "provenance": {
            "mode": current_provenance_mode,
            "rewarded_set_full": (
                "PASS"
                if rewarded_set_fresh
                and rewarded_set
                and rewarded_set.get("event") == "LAUNCH_REWARDED_SET_GATE_PASS"
                else "NOT_PROVEN"
            ),
            "positive_tdx_raw_replay": positive_replay,
            "whole_epoch_full": launch_whole_epoch_full,
            "launch_public_assurance": (
                launch_public_assurance
                if isinstance(launch_public_assurance, str)
                else "NOT_PROVEN"
            ),
            "current_whole_epoch_full": (
                provenance_status if provenance_fresh else "NOT_PROVEN"
            ),
            "fresh": provenance_fresh,
            "age_seconds": round(provenance_age, 3)
            if provenance_age is not None
            else None,
            "latest_event": (provenance or {}).get("event")
            if provenance_fresh
            else "STALE",
            "detail": (
                (provenance or {}).get("detail")
                if provenance_fresh
                else f"no provenance result observed within {MAX_EVENT_AGE_SECONDS} seconds"
            ),
        },
        "public_evidence": {
            "latest": index.get("latest"),
            "history_entries": len(index.get("recent", []))
            if isinstance(index.get("recent"), list)
            else None,
        },
        "release": {
            "claim": release.get("claim"),
            "producer_revision": (release.get("source_revisions") or {}).get(
                "producer"
            ),
            "subnet_revision": (release.get("source_revisions") or {}).get("validator"),
            "verifier_digest": (release.get("pins") or {}).get(
                "verifier_implementation"
            ),
        },
        "events_published": len(events),
        "disclosure": (
            "Public logs are allowlisted and redacted. Raw TDX evidence and "
            "machine endpoints remain controlled-disclosure artifacts."
        ),
    }


def atomic_write(path: Path, data: bytes) -> None:
    try:
        info = path.parent.lstat()
    except OSError as exc:
        raise RuntimeError("public log directory is unavailable") from exc
    if (
        path.parent.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise RuntimeError("public log directory is not owner-controlled")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    events = tail_events()
    jsonl = b"".join(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        for event in events
    )
    human_lines: list[str] = []
    for event in events:
        fields = [
            str(event.get("ts", "-")),
            str(event.get("stage", "-")).upper(),
            str(event.get("mode", "-")).upper(),
            str(event.get("status", "-")).upper(),
            str(event.get("event", "-")),
        ]
        if event.get("detail"):
            fields.append(str(event["detail"]))
        if event.get("remediation"):
            fields.append("next: " + str(event["remediation"]))
        human_lines.append(" | ".join(fields))
    status = build_status(events)
    atomic_write(LOG_ROOT / "validator-events.jsonl", jsonl)
    atomic_write(
        LOG_ROOT / "validator-events.log",
        ("\n".join(human_lines) + ("\n" if human_lines else "")).encode("utf-8"),
    )
    atomic_write(
        LOG_ROOT / "status.json",
        json.dumps(status, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    print(
        json.dumps(
            {
                "events_published": len(events),
                "status": "sanitized_validator_stream_published",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
