"""The CyberGym score document contract, shared by the writer and the reader.

``cybergym_ingest`` authenticates and persists producer documents;
``mechanism_cybergym_adapter`` reads them back and must be able to re-derive the
same digests to decide whether a stored row is genuinely the document that was
authenticated. Both need the same canonicalization, the same key list, and the
same HMAC verification, and those are security-critical constants: two copies
would eventually disagree, and the disagreement would look like a passing test
suite.

This module holds exactly those pieces and imports NOTHING beyond the standard
library. That is deliberate: the adapter sits on the weight-composition path and
must not pull in FastAPI, which ``cybergym_ingest`` does.

What a verified read proves, and what it does not
------------------------------------------------
``verify_stored_report`` proves that the stored authenticated body is
byte-identical to what some holder of the configured HMAC secret sent, and that
every derived column and every score row agrees with the separately stored
canonical semantic body. It therefore detects a row hand-inserted by anything
without the secret, and any later tampering with either body, the derived
columns, or the score rows.

It does NOT prove the producer's own identity beyond possession of a shared
secret: the transport is symmetric HMAC, so anyone holding the secret can mint a
verifiable document. Upgrading that to a producer public key is an owner
decision, recorded as such. Rotating the secret makes previously stored rows
unverifiable, which fails closed: the lane contributes nothing and its share
burns.
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

# The exact keys a producer document carries. The semantic digest is taken over
# these and only these, so adding a derived column never changes the digest.
SEMANTIC_KEYS = (
    "producer_hotkey",
    "network",
    "netuid",
    "source_epoch",
    "generated_at",
    "complete",
    "score_units",
    "scores",
    "evidence_sha256",
)

HMAC_SECRET_ENV = "CATHEDRAL_CYBERGYM_SCORES_HMAC_SECRET"

SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_RECEIPT_NS = "cathedral:cybergym-scores:v1"


def canonical_report_bytes(body: dict[str, Any]) -> bytes:
    """sort_keys + compact separators, UTF-8: the one canonicalization.

    Same style as ``external_scores._canonical`` and
    ``mechanism_intake.canonical_score_post_bytes``, so a producer reuses one
    serializer across every Cathedral score surface.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def semantic_view(document: dict[str, Any]) -> dict[str, Any]:
    """Just the contract keys, so derived fields cannot enter the digest."""
    return {key: document[key] for key in SEMANTIC_KEYS if key in document}


def normalize_semantic_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return the one normalized semantic form for an accepted wire document.

    This intentionally lives in the dependency-free shared contract. The writer
    uses it before persistence and the reader applies it again to the exact
    HMAC-authenticated bytes. That second derivation is what binds the stored
    canonical body to the signed wire body rather than trusting two independent
    database columns.
    """
    keys = set(document)
    expected_keys = set(SEMANTIC_KEYS)
    missing = [key for key in SEMANTIC_KEYS if key not in keys]
    if missing:
        raise ValueError("missing " + ",".join(missing))
    if keys != expected_keys:
        raise ValueError("unexpected semantic fields")

    producer = document["producer_hotkey"]
    network = document["network"]
    netuid = document["netuid"]
    source_epoch = document["source_epoch"]
    generated_at = document["generated_at"]
    complete = document["complete"]
    score_units = document["score_units"]
    scores = document["scores"]
    evidence = document["evidence_sha256"]
    if (
        not isinstance(producer, str)
        or not isinstance(network, str)
        or isinstance(netuid, bool)
        or not isinstance(netuid, int)
        or isinstance(source_epoch, bool)
        or not isinstance(source_epoch, int)
        or not isinstance(generated_at, str)
        or complete is not True
        or not isinstance(score_units, str)
        or not isinstance(scores, dict)
        or not isinstance(evidence, str)
    ):
        raise ValueError("invalid semantic document")

    parsed_at = datetime.fromisoformat(generated_at.strip().replace("Z", "+00:00"))
    if parsed_at.tzinfo is None:
        parsed_at = parsed_at.replace(tzinfo=timezone.utc)
    parsed_at = parsed_at.astimezone(timezone.utc)
    canonical_at = parsed_at.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{parsed_at.microsecond // 1000:03d}Z"
    )

    normalized_scores: dict[str, float] = {}
    for hotkey, value in scores.items():
        if (
            not isinstance(hotkey, str)
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
        ):
            raise ValueError("invalid score")
        score = float(value)
        if not math.isfinite(score) or score < 0.0:
            raise ValueError("invalid score")
        normalized_hotkey = hotkey.strip()
        if not normalized_hotkey or normalized_hotkey in normalized_scores:
            raise ValueError("invalid score hotkey")
        normalized_scores[normalized_hotkey] = score

    return {
        "producer_hotkey": producer.strip(),
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "generated_at": canonical_at,
        "complete": True,
        "score_units": score_units,
        "scores": normalized_scores,
        "evidence_sha256": evidence,
    }


def report_digest(document: dict[str, Any]) -> str:
    """The semantic canonical digest: the epoch fence's identity for a report."""
    return hashlib.sha256(canonical_report_bytes(semantic_view(document))).hexdigest()


def receipt_id(digest: str) -> str:
    return "cyg-" + hashlib.sha256(
        f"{_RECEIPT_NS}\0{digest}".encode("utf-8")
    ).hexdigest()[:32]


def hmac_secret() -> str:
    return (os.environ.get(HMAC_SECRET_ENV) or "").strip()


def hmac_secret_configured() -> bool:
    return bool(hmac_secret())


def constant_time_equal(supplied: str, expected: str) -> bool:
    """Constant-time compare that tolerates non-ASCII input.

    ``hmac.compare_digest`` raises TypeError on ``str`` arguments containing
    non-ASCII characters, and Starlette decodes header bytes as latin-1, so a
    single non-ASCII credential byte would otherwise escape as an unhandled 500
    instead of a 401. Comparing UTF-8 bytes keeps the comparison constant time
    and simply does not match.
    """
    try:
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except Exception:
        return False


def body_hmac_hex(body: bytes, secret: str | None = None) -> str:
    key = (secret if secret is not None else hmac_secret()).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def strip_sha256_prefix(signature: str) -> str:
    supplied = (signature or "").strip()
    if supplied.startswith("sha256="):
        supplied = supplied[len("sha256="):]
    return supplied


def verify_body_hmac(body: bytes, signature: str | None) -> bool:
    """Verify the raw-body HMAC. An unset secret never passes."""
    secret = hmac_secret()
    if not secret or not signature:
        return False
    return constant_time_equal(strip_sha256_prefix(signature), body_hmac_hex(body, secret))


# --- read-side verification ------------------------------------------------

# Columns of cybergym_score_reports that MUST agree with the stored body. Each
# maps a column name to the document key it was derived from at intake.
_HEADER_TO_DOCUMENT = {
    "producer_hotkey": "producer_hotkey",
    "network": "network",
    "netuid": "netuid",
    "source_epoch": "source_epoch",
    "generated_at_iso": "generated_at",
}


class ReportVerificationError(ValueError):
    """Verification failed; ``reason`` is the machine-readable cause."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def verify_stored_report(
    header: dict[str, Any],
    *,
    body: str | bytes | None,
    authenticated_body: str | bytes | None = None,
    signature: str | None,
    rows: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Re-derive every digest for a stored report and return the verified document.

    ``header`` is the ``cybergym_score_reports`` row as a mapping. ``body`` is
    the canonical normalized semantic body. ``authenticated_body`` is the exact
    wire body covered by ``signature`` and ``body_sha256``. It defaults to
    ``body`` for rows written before the representations were split; those
    legacy bytes are normalized directly after their HMAC verifies, without
    requiring the old JSON serialization itself to be canonical. ``rows``
    contains the ``cybergym_scores`` values keyed by miner hotkey (omit to skip
    the row cross-check).

    Raises ``ReportVerificationError`` with one of these reasons:
      ``body_missing``, ``body_digest_mismatch``, ``signature_unverifiable``
      (no secret configured), ``signature_invalid``, ``malformed_report``,
      ``authenticated_semantics_mismatch``, ``report_digest_mismatch``,
      ``header_mismatch``, ``rows_tampered``.
    """
    if body is None or body == "":
        raise ReportVerificationError("body_missing")
    semantic_body_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)
    legacy_combined_body = authenticated_body in (None, "")
    raw_body = body if legacy_combined_body else authenticated_body
    raw_body_bytes = (
        raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
    )

    stored_body_digest = str(header.get("body_sha256") or "")
    if SHA256_HEX_RE.fullmatch(stored_body_digest) is None:
        raise ReportVerificationError("body_digest_mismatch", "no stored body digest")
    if not constant_time_equal(
        hashlib.sha256(raw_body_bytes).hexdigest(), stored_body_digest
    ):
        raise ReportVerificationError("body_digest_mismatch")

    if not hmac_secret_configured():
        # Fail closed rather than downgrade to a digest-only check: without the
        # secret nothing distinguishes an authenticated body from a forged one.
        raise ReportVerificationError("signature_unverifiable")
    if not verify_body_hmac(raw_body_bytes, signature):
        raise ReportVerificationError("signature_invalid")

    try:
        authenticated_document = json.loads(raw_body_bytes.decode("utf-8"))
    except Exception:
        raise ReportVerificationError("malformed_report", "body is not JSON")
    if not isinstance(authenticated_document, dict):
        raise ReportVerificationError("malformed_report", "body is not an object")
    try:
        document = normalize_semantic_document(authenticated_document)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReportVerificationError("malformed_report", str(exc)) from exc
    if (
        not legacy_combined_body
        and canonical_report_bytes(document) != semantic_body_bytes
    ):
        raise ReportVerificationError("authenticated_semantics_mismatch")

    if not constant_time_equal(report_digest(document), str(header.get("report_sha256") or "")):
        raise ReportVerificationError("report_digest_mismatch")

    for column, key in _HEADER_TO_DOCUMENT.items():
        stored = header.get(column)
        derived = document.get(key)
        if isinstance(stored, str) or isinstance(derived, str):
            if str(stored) != str(derived):
                raise ReportVerificationError("header_mismatch", column)
        elif int(stored) != int(derived):
            raise ReportVerificationError("header_mismatch", column)
    if bool(header.get("complete")) is not bool(document.get("complete")):
        raise ReportVerificationError("header_mismatch", "complete")
    if document.get("complete") is not True:
        raise ReportVerificationError("header_mismatch", "not_complete")

    scores = document.get("scores")
    if not isinstance(scores, dict):
        raise ReportVerificationError("malformed_report", "scores is not an object")
    verified: dict[str, float] = {}
    for hotkey, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReportVerificationError("malformed_report", "non-numeric score")
        verified[str(hotkey)] = float(value)
    if int(header.get("score_count") or 0) != len(verified):
        raise ReportVerificationError("header_mismatch", "score_count")

    if rows is not None:
        # The projection must match the authenticated document exactly: an added,
        # removed, or edited row is tampering, not a rounding difference.
        if set(rows) != set(verified):
            raise ReportVerificationError("rows_tampered", "hotkey set")
        for hotkey, value in verified.items():
            if float(rows[hotkey]) != value:
                raise ReportVerificationError("rows_tampered", hotkey)

    return {"document": document, "scores": verified}
