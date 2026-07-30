"""CyberGym producer-to-publisher score transport.

The CyberGym validator (the producer) derives verified per-miner scores on its
own host. Nothing previously carried them into the publisher's database, so the
``cybergym_scores`` table the mechanism adapter reads could never be populated
in production: there was no endpoint, no migration, and no wire contract. This
module is that missing transport.

It is deliberately a faithful copy of the authentication and epoch-fence shape
``external_scores.py`` already uses for producer-posted score documents (see
``external_scores.store_report`` and the ``POST /v1/external-scores/violet``
handler in ``app.py``), with its own configuration keys so no existing route or
blend path changes:

  * source-scoped bearer credential, constant-time compared;
  * mandatory raw-body HMAC over the exact authenticated bytes, fail-closed
    (an unconfigured secret means 503, never "accept unsigned");
  * audience binding: the document must declare the network/netuid this
    publisher is configured for;
  * strictly increasing epochs per audience, byte-identical retries accepted
    idempotently, conflicting documents refused.

Exactly ONE producer per audience
---------------------------------
``CATHEDRAL_CYBERGYM_PRODUCER_HOTKEY`` is REQUIRED, and the epoch fence is
audience-scoped rather than producer-scoped. Those two facts belong together: the
adapter composes the single newest complete report for an audience, so admitting a
second signer would let it outbid the real producer simply by posting a higher
epoch. One configured signer plus one audience fence removes that entirely.

The cost is deliberate and worth stating: rotating the producer key does NOT reset
the epoch counter. A replacement key must continue above the highest epoch already
stored for that audience, or its first post is refused with ``epoch_too_old``.

Wire contract
-------------
``POST /v1/cybergym/scores``

Headers:
  * ``Authorization: Bearer <token>`` (or ``X-Cathedral-Cybergym-Token``)
  * ``X-Cathedral-Cybergym-Signature: sha256=<hex>``, HMAC-SHA256 over the
    exact request body bytes.

Body (exactly these keys; extra or missing keys are rejected)::

    {
      "producer_hotkey": "<ss58 identity of the producing validator>",
      "network": "<must equal the configured weight-policy network>",
      "netuid": <must equal the configured weight-policy netuid>,
      "source_epoch": <int >= 0, the producer's own epoch counter>,
      "generated_at": "<ISO-8601 UTC, the authenticated freshness clock>",
      "complete": true,
      "score_units": "<canonical unit label for the score values>",
      "scores": {"<miner_hotkey>": <nonneg finite number>, ...},
      "evidence_sha256": "<64 hex chars, producer evidence bundle digest>"
    }

``complete`` must be ``true``: a CyberGym report is the full truth at its epoch,
so an omitted miner scores zero (revoked) rather than keeping an older value.
That is the posture ``external_scores.COMPLETE_REQUIRED_SOURCES`` already takes
for attested sources. An empty ``scores`` object is therefore meaningful and
legal: "nobody scored this epoch", which makes the mechanism non-contributing so
its share burns.

``generated_at`` is the ONLY freshness clock. The adapter derives
``ScoreVectorMeta.signed_at_ms`` from it, never from read time, so a stale
document can never look freshly signed. It is bounded in BOTH directions: a
timestamp later than receipt time plus
``CATHEDRAL_CYBERGYM_MAX_FUTURE_SKEW_SECS`` (default 120s, matching
``external_scores.max_report_future_secs``) is refused, because a future-dated
report would compute a negative age downstream and stay inside every freshness
window for as long as it sat in the table.

Failure modes: 404 (ingest disabled, the default), 503 (bearer, HMAC secret,
producer, or audience not configured), 401 (bad bearer or bad HMAC), 403 (a
document declaring a signer other than the configured producer), 400 (malformed
body, wrong audience, non-complete report, future-dated report), 409 (epoch
rollback, or a conflicting document at a stored epoch), 413 (body over the cap,
enforced before parsing AND before buffering).

Mounting: ``build_app`` mounts this router and injects the publisher's own
``Store`` through ``app.dependency_overrides``, so the endpoint EXISTS on a
full-role process and configuration alone turns it on. That is the point of
mounting it: an intake nobody can reach is a component, not a default-off
endpoint. The default-off gate stays at the route, so an unconfigured deployment
answers 404 and mounting changes nothing for it. The path is deliberately not in
app.py's narrow service-role allowlists, so read, submit, and worker roles keep
refusing it.

Another integrator can still mount it standalone::

    from scaffold.publisher import cybergym_ingest
    cybergym_ingest.set_publisher_store(store)
    app.include_router(cybergym_ingest.router)

Guardrails: default OFF, no chain calls, no secrets in code or logs, body size
capped before any JSON parsing and before buffering, writes only the two 0048
tables.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

# Re-exported so callers and tests keep one import site for this endpoint, and
# so the writer and the reader can never drift apart on canonicalization.
from .cybergym_contract import (  # noqa: F401
    HMAC_SECRET_ENV,
    SEMANTIC_KEYS,
    SHA256_HEX_RE as _SHA256_HEX_RE,
    canonical_report_bytes,
    constant_time_equal,
    constant_time_equal as _constant_time_equal,
    hmac_secret_configured,
    normalize_semantic_document,
    receipt_id,
    report_digest,
    semantic_view,
    verify_body_hmac,
)
from .store import Store

INGEST_ENABLED_ENV = "CATHEDRAL_CYBERGYM_INGEST_ENABLED"
AUTH_TOKEN_ENV = "CATHEDRAL_CYBERGYM_SCORES_TOKEN"
PRODUCER_HOTKEY_ENV = "CATHEDRAL_CYBERGYM_PRODUCER_HOTKEY"
MAX_BODY_BYTES_ENV = "CATHEDRAL_CYBERGYM_INGEST_MAX_BODY_BYTES"
MAX_SCORES_ENV = "CATHEDRAL_CYBERGYM_INGEST_MAX_SCORES"
MAX_FUTURE_SKEW_SECS_ENV = "CATHEDRAL_CYBERGYM_MAX_FUTURE_SKEW_SECS"
# Audience comes from the SAME weight-policy settings the signed vector uses, so
# an ingested report can only ever belong to this publisher's own audience.
NETWORK_ENV = "CATHEDRAL_WEIGHT_POLICY_NETWORK"
NETUID_ENV = "CATHEDRAL_WEIGHT_POLICY_NETUID"

DEFAULT_MAX_BODY_BYTES = 65_536
DEFAULT_MAX_SCORES = 8_192
# Clock-skew allowance for the producer's generated_at, matching
# external_scores.max_report_future_secs (120s).
DEFAULT_MAX_FUTURE_SKEW_SECS = 120.0
MAX_NETUID = 2**16 - 1

SOURCE = "cathedral_cybergym"

# The document contract itself lives in cybergym_contract so the read side can
# re-derive the identical digests without importing FastAPI through this module.
_ALLOWED_KEYS = set(SEMANTIC_KEYS)
_UNITS_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
# An audience-scoped monotonic-max fence keys on (network, netuid, source_epoch)
# and refuses any source_epoch below the highest stored, so a single accepted
# absurd value would permanently refuse every later legitimate report as
# ``epoch_too_old``. Bound it well above any real producer counter (2**31-1 epochs
# is hundreds of millennia at any realistic tempo) so the fence cannot be poisoned
# by one oversized post. A legitimate producer downgrade is still refused by
# design (anti-rollback); recovering from that is an owner/DB decision.
_MAX_SOURCE_EPOCH = 2**31 - 1

router = APIRouter()


class CybergymIngestError(ValueError):
    """Domain error; ``reason`` is the wire detail string."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- configuration -------------------------------------------------------

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def ingest_enabled() -> bool:
    return _env_bool(INGEST_ENABLED_ENV, False)


def token_configured() -> bool:
    return bool((os.environ.get(AUTH_TOKEN_ENV) or "").strip())


def configured_producer_hotkey() -> str:
    """The ONE producer identity allowed to post for this audience; '' = unset.

    Required, not optional. The epoch fence is audience-scoped (see
    ``store_report``), so admitting a second signer for the same audience would
    let it outbid the real producer by posting a higher epoch. An unset value
    means the route answers 503 and the lane burns.
    """
    return (os.environ.get(PRODUCER_HOTKEY_ENV) or "").strip()


def producer_configured() -> bool:
    return bool(configured_producer_hotkey())


def configured_audience() -> tuple[str, int]:
    """The exact (network, netuid) an ingested report must declare.

    Fails closed exactly like ``external_scores.configured_score_audience``: an
    unset or malformed audience raises rather than defaulting to finney/39, so a
    misconfigured deployment cannot silently ingest for the wrong subnet.
    """
    network = os.environ.get(NETWORK_ENV)
    netuid_raw = os.environ.get(NETUID_ENV)
    if (
        not isinstance(network, str)
        or not network
        or len(network) > 128
        or network.strip() != network
        or not isinstance(netuid_raw, str)
        or not re.fullmatch(r"[0-9]{1,5}", netuid_raw)
    ):
        raise CybergymIngestError("cybergym_audience_not_configured")
    netuid = int(netuid_raw)
    if netuid > MAX_NETUID:
        raise CybergymIngestError("cybergym_audience_not_configured")
    return network, netuid


def _max_body_bytes() -> int:
    return max(1024, _env_int(MAX_BODY_BYTES_ENV, DEFAULT_MAX_BODY_BYTES))


def _max_scores() -> int:
    return max(1, _env_int(MAX_SCORES_ENV, DEFAULT_MAX_SCORES))


def max_future_skew_secs() -> float:
    """How far ahead of receipt time a producer timestamp may be."""
    raw = (os.environ.get(MAX_FUTURE_SKEW_SECS_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_FUTURE_SKEW_SECS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_MAX_FUTURE_SKEW_SECS


# --- authentication ------------------------------------------------------

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
    """Constant-time bearer check. No token configured means NOT authorized.

    Unlike the legacy external-scores helper there is no
    allow-unauthenticated escape hatch: this lane is new, so it starts
    fail-closed and stays that way.
    """
    expected = (os.environ.get(AUTH_TOKEN_ENV) or "").strip()
    if not expected:
        return False
    supplied = _bearer_supplied(authorization, x_token)
    return bool(supplied) and _constant_time_equal(supplied, expected)


# --- store wiring --------------------------------------------------------

_store_override: Store | None = None


def set_publisher_store(store: Store | None) -> None:
    global _store_override
    _store_override = store


def get_publisher_store() -> Store | None:
    return _store_override


# --- canonicalization + validation --------------------------------------

def _ms_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now_iso() -> str:
    return _ms_iso(datetime.now(timezone.utc))


def _normalize_generated_at(value: Any) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip():
        raise CybergymIngestError("invalid_generated_at")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except Exception:
        raise CybergymIngestError("invalid_generated_at")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _ms_iso(dt), dt.astimezone(timezone.utc)


def validate_report(
    payload: Any,
    *,
    audience: tuple[str, int],
    producer: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Shape, value, audience, producer, and freshness validation.

    Returns the normalized report. ``report_sha256`` is the semantic canonical
    digest (the epoch-fence identity); the caller separately binds
    ``body_sha256`` to the exact authenticated bytes, mirroring
    ``external_scores.bind_authenticated_body``.

    ``producer`` is the ONE configured producer ss58 and is required: a document
    declaring any other signer is refused. ``generated_at`` may not be later
    than ``now`` plus ``max_future_skew_secs()``, because a future-dated
    timestamp would otherwise stay inside every freshness window forever.
    """
    now = now or datetime.now(timezone.utc)
    if not producer:
        raise CybergymIngestError("cybergym_producer_not_configured")
    if not isinstance(payload, dict):
        raise CybergymIngestError("invalid_body")
    extra = set(payload.keys()) - _ALLOWED_KEYS
    if extra:
        raise CybergymIngestError("unexpected_fields")
    missing = _ALLOWED_KEYS - set(payload.keys())
    if missing:
        raise CybergymIngestError("missing_fields")

    declared = payload.get("producer_hotkey")
    if not isinstance(declared, str) or not declared.strip() or len(declared) > 128:
        raise CybergymIngestError("invalid_producer_hotkey")
    declared = declared.strip()
    if declared != producer:
        raise CybergymIngestError("producer_hotkey_mismatch")

    network = payload.get("network")
    netuid = payload.get("netuid")
    if not isinstance(network, str) or type(netuid) is not int:
        raise CybergymIngestError("invalid_cybergym_audience")
    if (network, netuid) != audience:
        raise CybergymIngestError("cybergym_audience_mismatch")

    source_epoch = payload.get("source_epoch")
    if (
        isinstance(source_epoch, bool)
        or not isinstance(source_epoch, int)
        or source_epoch < 0
        or source_epoch > _MAX_SOURCE_EPOCH
    ):
        raise CybergymIngestError("invalid_source_epoch")

    generated_at, generated_dt = _normalize_generated_at(payload.get("generated_at"))
    skew = max_future_skew_secs()
    if (generated_dt - now).total_seconds() > skew:
        # A future-dated report would compute a negative age downstream and
        # never age out of any freshness window.
        raise CybergymIngestError("report_in_future")

    if payload.get("complete") is not True:
        # A CyberGym report is the full truth at its epoch; a partial document
        # must never be stored as if it were live-composable.
        raise CybergymIngestError("complete_required")

    score_units = payload.get("score_units")
    if not isinstance(score_units, str) or not _UNITS_RE.match(score_units):
        raise CybergymIngestError("invalid_score_units")

    evidence = payload.get("evidence_sha256")
    if not isinstance(evidence, str) or _SHA256_HEX_RE.fullmatch(evidence) is None:
        raise CybergymIngestError("invalid_evidence_sha256")

    raw_scores = payload.get("scores")
    if not isinstance(raw_scores, dict):
        raise CybergymIngestError("invalid_scores")
    if len(raw_scores) > _max_scores():
        raise CybergymIngestError("too_many_scores")
    scores: dict[str, float] = {}
    for hotkey, value in raw_scores.items():
        if not isinstance(hotkey, str) or not hotkey.strip() or len(hotkey) > 128:
            raise CybergymIngestError("invalid_miner_hotkey")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CybergymIngestError("invalid_score")
        score = float(value)
        if not math.isfinite(score) or score < 0.0:
            raise CybergymIngestError("invalid_score")
        key = hotkey.strip()
        if key in scores:
            raise CybergymIngestError("duplicate_miner_hotkey")
        scores[key] = score

    semantic = normalize_semantic_document({
        "producer_hotkey": declared,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "generated_at": generated_at,
        "complete": True,
        "score_units": score_units,
        "scores": scores,
        "evidence_sha256": evidence,
    })
    digest = report_digest(semantic)
    report = dict(semantic)
    report["report_sha256"] = digest
    report["report_id"] = receipt_id(digest)
    return report


def bind_authenticated_body(report: dict[str, Any], body: bytes) -> dict[str, Any]:
    """Bind a normalized report to the exact authenticated HTTP body.

    ``report_sha256`` is the semantic identity the epoch fence uses;
    ``body_sha256`` commits to the exact bytes that passed bearer and HMAC
    authentication, so an auditor can compare the stored row against the wire
    evidence. Same split as ``external_scores.bind_authenticated_body``.
    """
    if not isinstance(body, bytes):
        raise CybergymIngestError("invalid_authenticated_report_body")
    try:
        body_text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise CybergymIngestError("invalid_authenticated_report_body")
    bound = dict(report)
    bound["body_sha256"] = hashlib.sha256(body).hexdigest()
    # The exact authenticated bytes are persisted, not a re-serialization of the
    # parsed document. That is what lets the adapter re-verify the stored HMAC
    # rather than trusting a column, so a hand-inserted row cannot read as
    # authenticated. See cybergym_contract.verify_stored_report.
    bound["authenticated_body"] = body_text
    return bound


# --- persistence: idempotent audience-fenced write ------------------------

def _fence_lock_key(network: str, netuid: int) -> int:
    material = f"cybergym-score-epoch\0{network}\0{netuid}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)


def store_report(
    store: Store,
    report: dict[str, Any],
    *,
    signature: str = "",
) -> dict[str, Any]:
    """Persist an authenticated report behind the audience's epoch fence.

    Epochs are strictly increasing per (network, netuid). A byte-identical retry
    at the newest epoch is idempotent. A different document at a stored epoch is
    a conflict. An older epoch is a rollback and is refused, so a replayed
    document can never resurrect a stale epoch.
    """
    received_at = _now_iso()
    report_id = str(report["report_id"])
    producer = str(report["producer_hotkey"])
    network = str(report["network"])
    netuid = int(report["netuid"])
    source_epoch = int(report["source_epoch"])
    digest = str(report["report_sha256"])
    body_digest = report.get("body_sha256")
    if not isinstance(body_digest, str) or _SHA256_HEX_RE.fullmatch(body_digest) is None:
        raise CybergymIngestError("authenticated_body_digest_required")
    scores: dict[str, float] = dict(report["scores"])
    # Keep the exact authenticated bytes for HMAC verification, but do not use
    # their representation as the semantic document. Valid wire values such as
    # ``+00:00`` timestamps and integer scores normalize to the canonical
    # ``.000Z`` / float form above. Persist that one normalized form in
    # report_json so the digest, header projection, rows, and reader all derive
    # from identical semantics.
    body_text = report.get("authenticated_body")
    if not isinstance(body_text, str) or not body_text:
        raise CybergymIngestError("authenticated_body_required")
    if not constant_time_equal(
        hashlib.sha256(body_text.encode("utf-8")).hexdigest(), str(body_digest)
    ):
        raise CybergymIngestError("authenticated_body_digest_mismatch")
    report_json = canonical_report_bytes(semantic_view(report)).decode("utf-8")

    def _write(conn):
        if store.backend == "postgres":
            # Serialize this audience's fence only. MVCC alone would let two
            # same-epoch writers observe the same prior row (the same fix
            # external_scores.store_report applies).
            conn.execute(
                "SELECT pg_advisory_xact_lock(?)", (_fence_lock_key(network, netuid),)
            )
        cur = conn.execute(
            "SELECT source_epoch, report_sha256, body_sha256, signature, "
            "authenticated_body, report_json FROM cybergym_score_reports "
            "WHERE network=? AND netuid=? ORDER BY source_epoch DESC LIMIT 1",
            (network, netuid),
        )
        row = cur.fetchone()
        if row is not None:
            stored_epoch = int(row[0])
            stored_digest = str(row[1])
            if source_epoch < stored_epoch:
                raise CybergymIngestError("epoch_too_old")
            if source_epoch == stored_epoch:
                if stored_digest != digest:
                    raise CybergymIngestError("epoch_conflict")
                # Migration 0049 deliberately leaves authenticated_body empty
                # on pre-split rows: report_json is their exact HMAC-covered
                # wire body, not necessarily canonical JSON. An exact retry is
                # a safe opportunity to finish the split without changing the
                # epoch or semantic identity. Verify the legacy bytes against
                # their stored digest and HMAC before replacing report_json
                # with the canonical normalized document. A semantically equal
                # but differently serialized retry remains idempotent and is
                # served by the read-side legacy compatibility path instead.
                stored_authenticated_body = row[4]
                if stored_authenticated_body in (None, ""):
                    legacy_body = str(row[5] or "")
                    legacy_bytes = legacy_body.encode("utf-8")
                    legacy_body_digest = str(row[2] or "")
                    legacy_signature = str(row[3] or "")
                    if (
                        legacy_body == body_text
                        and constant_time_equal(legacy_body_digest, str(body_digest))
                        and constant_time_equal(
                            hashlib.sha256(legacy_bytes).hexdigest(),
                            legacy_body_digest,
                        )
                        and verify_body_hmac(legacy_bytes, legacy_signature)
                    ):
                        conn.execute(
                            "UPDATE cybergym_score_reports "
                            "SET authenticated_body=?, report_json=? "
                            "WHERE network=? AND netuid=? AND source_epoch=? "
                            "AND authenticated_body=''",
                            (
                                legacy_body,
                                report_json,
                                network,
                                netuid,
                                source_epoch,
                            ),
                        )
                return None  # sentinel: byte-identical retry, already stored

        conn.execute(
            "INSERT OR REPLACE INTO cybergym_score_reports"
            "(id, network, netuid, source_epoch, producer_hotkey, complete, "
            "score_units, score_count, generated_at_iso, received_at_iso, "
            "report_sha256, body_sha256, evidence_sha256, signature, "
            "authenticated_body, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                network,
                netuid,
                source_epoch,
                producer,
                1,
                str(report["score_units"]),
                len(scores),
                report["generated_at"],
                received_at,
                digest,
                body_digest,
                str(report["evidence_sha256"]),
                signature,
                body_text,
                report_json,
            ),
        )
        conn.execute("DELETE FROM cybergym_scores WHERE report_id=?", (report_id,))
        for hotkey in sorted(scores):
            conn.execute(
                "INSERT OR REPLACE INTO cybergym_scores"
                "(report_id, miner_hotkey, epoch, score, network, netuid, "
                "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    hotkey,
                    source_epoch,
                    float(scores[hotkey]),
                    network,
                    netuid,
                    producer,
                    digest,
                    report["generated_at"],
                    received_at,
                ),
            )
        return True  # newly stored

    result = store.write(_write)
    return {
        "accepted": True,
        "report_id": report_id,
        "producer_hotkey": producer,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "score_count": len(scores),
        "report_sha256": digest,
        "body_sha256": body_digest,
        "received_at": received_at,
        "idempotent": result is None,
    }


def latest_complete_report(
    store: Store, *, audience: tuple[str, int]
) -> dict[str, Any] | None:
    """The newest complete report header for *audience*, or None.

    Read-only, and exactly ONE report: a reader must never mix epochs.
    """
    rows = store.query(
        "SELECT id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256 "
        "FROM cybergym_score_reports "
        "WHERE network=? AND netuid=? AND complete=1 "
        "ORDER BY source_epoch DESC LIMIT 1",
        (audience[0], audience[1]),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "report_id": str(row["id"]),
        "network": str(row["network"]),
        "netuid": int(row["netuid"]),
        "source_epoch": int(row["source_epoch"]),
        "producer_hotkey": str(row["producer_hotkey"]),
        "complete": bool(row["complete"]),
        "score_units": str(row["score_units"]),
        "score_count": int(row["score_count"]),
        "generated_at_iso": str(row["generated_at_iso"]),
        "received_at_iso": str(row["received_at_iso"]),
        "report_sha256": str(row["report_sha256"]),
        "body_sha256": str(row["body_sha256"]),
        "evidence_sha256": str(row["evidence_sha256"]),
    }


# --- route ----------------------------------------------------------------

_REASON_HTTP_STATUS = {
    "epoch_too_old": 409,
    "epoch_conflict": 409,
    "producer_hotkey_mismatch": 403,
}


async def read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the request body, aborting as soon as it exceeds ``max_bytes``.

    ``request.body()`` buffers the WHOLE request before any size check, so an
    absent or dishonest Content-Length let an authenticated client push
    arbitrary bytes into worker memory before the bound was applied. Consuming
    the stream incrementally makes the bound real: at most ``max_bytes`` plus one
    chunk is ever held.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="cybergym_report_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/v1/cybergym/scores")
async def post_cybergym_scores(
    request: Request,
    authorization: str | None = Header(None),
    x_cathedral_cybergym_token: str | None = Header(None),
    x_cathedral_cybergym_signature: str | None = Header(None),
    store: Store | None = Depends(get_publisher_store),
) -> dict[str, Any]:
    if not ingest_enabled():
        raise HTTPException(status_code=404, detail="cybergym_ingest_not_enabled")
    if store is None:
        raise HTTPException(status_code=503, detail="cybergym_store_not_configured")

    # Fail closed on every missing credential: with no way to authenticate a
    # producer the lane never receives data, and its share burns.
    if not token_configured():
        raise HTTPException(status_code=503, detail="cybergym_token_required")
    if not hmac_secret_configured():
        raise HTTPException(status_code=503, detail="cybergym_hmac_secret_required")
    producer = configured_producer_hotkey()
    if not producer:
        raise HTTPException(
            status_code=503, detail="cybergym_producer_not_configured"
        )
    try:
        audience = configured_audience()
    except CybergymIngestError as exc:
        raise HTTPException(status_code=503, detail=exc.reason)

    if not bearer_authorized(authorization, x_cathedral_cybergym_token):
        raise HTTPException(status_code=401, detail="invalid_cybergym_token")

    # Cap body size BEFORE any parsing AND before buffering: the declared
    # Content-Length first (fast reject), then the stream itself, so a lying or
    # absent header cannot get more than the bound into memory.
    max_body = _max_body_bytes()
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_body:
                raise HTTPException(status_code=413, detail="cybergym_report_too_large")
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
    raw_body = await read_bounded_body(request, max_body)

    if not verify_body_hmac(raw_body, x_cathedral_cybergym_signature):
        raise HTTPException(status_code=401, detail="invalid_cybergym_signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_json")

    try:
        report = validate_report(payload, audience=audience, producer=producer)
        bound = bind_authenticated_body(report, raw_body)
    except CybergymIngestError as exc:
        raise HTTPException(
            status_code=_REASON_HTTP_STATUS.get(exc.reason, 400), detail=exc.reason
        )

    try:
        return store_report(
            store, bound, signature=(x_cathedral_cybergym_signature or "")
        )
    except CybergymIngestError as exc:
        raise HTTPException(
            status_code=_REASON_HTTP_STATUS.get(exc.reason, 400), detail=exc.reason
        )
    except Exception:
        raise HTTPException(status_code=500, detail="cybergym_store_failed")
