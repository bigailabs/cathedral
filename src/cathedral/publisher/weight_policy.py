"""Publisher-side weight-policy surface (issue #155).

Owns:

* ``build_unsigned_vector``: deterministically build a
  ``SignedWeightVector`` (sans signature) from a snapshot of (miner
  hotkey, score) entries plus a policy snapshot
  (policy_version + vector_id + network + netuid + issued_at +
  expires_at + key_id).
* ``sign_vector``: thin wrapper for the policy signer.
* ``WeightPolicyStore``: in-memory holder for the latest signed
  vector. Held on the FastAPI ``app.state.weight_policy`` slot so the
  GET endpoint can return it (or 503 when empty).
* ``run_weight_policy_producer``: cadence loop that builds signed
  vectors from the publisher score snapshot.
* ``router``: FastAPI router exposing
  ``GET /v1/validator/weights/next``.

No admin UI: this surface is intentionally minimal. The publisher
producer owns writes to the store, and tests can still drive the store
directly for deterministic assertions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, HTTPException, Request

from cathedral.policy.signing import (
    BurnSnapshot,
    SignedWeightVector,
    WeightEntry,
    sign_vector,
)

logger = structlog.get_logger(__name__)


router = APIRouter()

_WEIGHT_POLICY_SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"


@dataclass(frozen=True)
class WeightPolicyProducerConfig:
    """Publisher-side settings for the signed weight producer."""

    network: str = "finney"
    netuid: int = 39
    key_id: str = "cathedral-weight-policy"
    burn_uid: int | None = 204
    forced_burn_percentage: float = 95.0
    interval_secs: float = 1500.0
    valid_for_secs: float = 1800.0
    limit: int = 1000
    task_family_weights: Mapping[str, float] | None = None
    task_family_since_days: int = 7


def build_unsigned_vector(
    scores_by_hotkey: dict[str, float] | Iterable[tuple[str, float]],
    *,
    vector_id: str,
    policy_version: int,
    network: str,
    netuid: int,
    key_id: str,
    policy_reason: str,
    burn_uid: int | None,
    forced_burn_percentage: float,
    generated_at: datetime | None = None,
    issued_at: datetime | None = None,
    valid_for: timedelta = timedelta(minutes=30),
    policy_metadata: dict[str, Any] | None = None,
    policy_hash: str | None = None,
) -> SignedWeightVector:
    """Build a deterministic unsigned ``SignedWeightVector``.

    Determinism: weights are sorted by ``miner_hotkey`` ascending and
    zero or negative scores are dropped. Non-finite scores raise
    ``ValueError`` - the caller should sanitize upstream.

    Caller passes ``policy_version`` (monotonic counter), ``vector_id``
    (opaque, unique per emission), ``network`` + ``netuid``, the pinned
    ``key_id``, the signed burn policy, audit context, and the
    wall-clock generation time. ``valid_for`` defaults to 30 minutes.
    """
    if generated_at is None and issued_at is None:
        raise ValueError("generated_at is required")
    generated = generated_at or issued_at
    assert generated is not None

    if isinstance(scores_by_hotkey, dict):
        items = list(scores_by_hotkey.items())
    else:
        items = list(scores_by_hotkey)

    cleaned: list[WeightEntry] = []
    for hotkey, score in items:
        import math

        if not math.isfinite(score):
            raise ValueError(f"non-finite score for hotkey {hotkey!r}: {score!r}")
        if score <= 0:
            continue
        cleaned.append(WeightEntry(miner_hotkey=hotkey, weight=float(score)))
    cleaned.sort(key=lambda e: e.miner_hotkey)

    generated_iso = _ms_iso(generated)
    expires_iso = _ms_iso(generated + valid_for)
    burn_snapshot = BurnSnapshot(
        burn_uid=burn_uid,
        forced_burn_percentage=forced_burn_percentage,
    )
    metadata = policy_metadata or {}
    resolved_policy_hash = policy_hash or compute_policy_hash(
        {
            "network": network,
            "netuid": netuid,
            "burn_snapshot": burn_snapshot.model_dump(mode="json"),
            "policy_reason": policy_reason,
            "policy_metadata": metadata,
            "weights": [entry.model_dump(mode="json") for entry in cleaned],
        }
    )
    return SignedWeightVector(
        vector_id=vector_id,
        policy_version=policy_version,
        network=network,
        netuid=netuid,
        generated_at=generated_iso,
        expires_at=expires_iso,
        burn_snapshot=burn_snapshot,
        policy_hash=resolved_policy_hash,
        key_id=key_id,
        policy_reason=policy_reason,
        policy_metadata=metadata,
        weights=cleaned,
    )


def build_and_sign(
    scores_by_hotkey: dict[str, float] | Iterable[tuple[str, float]],
    private_key: Ed25519PrivateKey,
    *,
    vector_id: str,
    policy_version: int,
    network: str,
    netuid: int,
    key_id: str,
    policy_reason: str,
    burn_uid: int | None,
    forced_burn_percentage: float,
    generated_at: datetime | None = None,
    issued_at: datetime | None = None,
    valid_for: timedelta = timedelta(minutes=30),
    policy_metadata: dict[str, Any] | None = None,
    policy_hash: str | None = None,
) -> SignedWeightVector:
    """Shortcut: ``build_unsigned_vector`` then ``sign_vector``."""
    unsigned = build_unsigned_vector(
        scores_by_hotkey,
        vector_id=vector_id,
        policy_version=policy_version,
        network=network,
        netuid=netuid,
        key_id=key_id,
        policy_reason=policy_reason,
        burn_uid=burn_uid,
        forced_burn_percentage=forced_burn_percentage,
        generated_at=generated_at,
        issued_at=issued_at,
        valid_for=valid_for,
        policy_metadata=policy_metadata,
        policy_hash=policy_hash,
    )
    return sign_vector(unsigned, private_key)


def compute_policy_hash(policy_input: dict[str, Any]) -> str:
    """Return the deterministic SHA-256 hash for policy audit input."""
    return hashlib.sha256(
        json.dumps(policy_input, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def load_producer_from_env() -> tuple[WeightPolicyProducerConfig, Ed25519PrivateKey] | None:
    """Return producer config + signer when the signing key env var is set."""
    signing_hex = os.environ.get(_WEIGHT_POLICY_SIGNING_KEY_ENV, "").strip()
    if not signing_hex:
        return None
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_hex))
    except ValueError as exc:
        raise RuntimeError(
            f"{_WEIGHT_POLICY_SIGNING_KEY_ENV} must be a 32-byte Ed25519 seed hex"
        ) from exc

    burn_uid_raw = os.environ.get("CATHEDRAL_WEIGHT_POLICY_BURN_UID", "204").strip()
    burn_uid = int(burn_uid_raw) if burn_uid_raw else None
    cfg = WeightPolicyProducerConfig(
        network=os.environ.get("CATHEDRAL_WEIGHT_POLICY_NETWORK", "finney"),
        netuid=int(os.environ.get("CATHEDRAL_WEIGHT_POLICY_NETUID", "39")),
        key_id=os.environ.get("CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"),
        burn_uid=burn_uid,
        forced_burn_percentage=float(
            os.environ.get("CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE", "95.0")
        ),
        interval_secs=float(os.environ.get("CATHEDRAL_WEIGHT_POLICY_INTERVAL_SECS", "1500.0")),
        valid_for_secs=float(os.environ.get("CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS", "1800.0")),
        limit=int(os.environ.get("CATHEDRAL_WEIGHT_POLICY_LIMIT", "1000")),
        task_family_weights=_resolve_task_family_weights(None),
        task_family_since_days=int(
            os.environ.get("CATHEDRAL_WEIGHT_POLICY_TASK_FAMILY_SINCE_DAYS", "7")
        ),
    )
    return cfg, private_key


async def latest_policy_scores_by_hotkey(
    conn: Any,
    *,
    limit: int = 1000,
    task_family_since_days: int = 7,
    task_family_weights: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Read the current publisher-scored surface as policy input.

    The producer starts from the best current ranked score per hotkey,
    then blends recent schema-5 Task Family rows only through explicitly
    configured signed-policy weights. This mirrors the validator local
    scoring rule: unknown or unweighted Task Family rows contribute zero.
    """
    lane_weights = _resolve_task_family_weights(task_family_weights)
    cur = await conn.execute(
        """
        SELECT miner_hotkey, MAX(current_score) AS score
        FROM agent_submissions
        WHERE status = 'ranked'
          AND current_score IS NOT NULL
          AND discovery_only = 0
          AND attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
        GROUP BY miner_hotkey
        ORDER BY score DESC, miner_hotkey ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    base_scores = {str(row[0]): float(row[1]) for row in rows if row[1] is not None}

    since = (datetime.now(UTC) - timedelta(days=task_family_since_days)).isoformat()
    cur = await conn.execute(
        """
        SELECT sub.miner_hotkey, er.weighted_score, er.task_json
        FROM eval_runs er
        JOIN agent_submissions sub ON sub.id = er.submission_id
        WHERE er.eval_output_schema_version = 5
          AND er.ran_at >= ?
          AND sub.status = 'ranked'
          AND sub.discovery_only = 0
          AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
        ORDER BY er.ran_at DESC
        """,
        (since,),
    )
    task_rows = await cur.fetchall()
    samples_by_hotkey: dict[str, dict[str, list[float]]] = {}
    for hotkey_raw, weighted_raw, task_json_raw in task_rows:
        try:
            task_json = json.loads(task_json_raw) if isinstance(task_json_raw, str) else {}
        except json.JSONDecodeError:
            task_json = {}
        task_type = str(task_json.get("task_type") or "")
        if task_type not in lane_weights:
            continue
        hotkey = str(hotkey_raw)
        samples_by_hotkey.setdefault(hotkey, {}).setdefault(task_type, []).append(
            float(weighted_raw)
        )

    scores = dict(base_scores)
    all_hotkeys = set(base_scores) | set(samples_by_hotkey)
    for hotkey in all_hotkeys:
        base_score = base_scores.get(hotkey)
        weighted_total = 0.0
        active_special_weight = 0.0
        task_buckets = samples_by_hotkey.get(hotkey, {})
        for task_type, lane_weight in lane_weights.items():
            samples = task_buckets.get(task_type)
            if samples and lane_weight > 0.0:
                lane_score = sum(samples) / len(samples)
                weighted_total += lane_score * lane_weight
                active_special_weight += lane_weight

        active_special_weight = min(active_special_weight, 1.0)
        weighted_total = min(weighted_total, 1.0)
        if base_score is not None:
            if active_special_weight == 0.0:
                scores[hotkey] = base_score
            else:
                scores[hotkey] = (base_score * (1.0 - active_special_weight)) + weighted_total
        elif weighted_total > 0.0:
            scores[hotkey] = weighted_total

    return scores


async def produce_weight_policy_once(
    conn: Any,
    store: WeightPolicyStore,
    private_key: Ed25519PrivateKey,
    *,
    config: WeightPolicyProducerConfig,
    issued_at: datetime | None = None,
) -> SignedWeightVector:
    """Build, sign, and publish one vector into ``store``."""
    issued = issued_at or datetime.now(UTC)
    task_family_weights = _resolve_task_family_weights(config.task_family_weights)
    scores = await latest_policy_scores_by_hotkey(
        conn,
        limit=config.limit,
        task_family_weights=task_family_weights,
        task_family_since_days=config.task_family_since_days,
    )
    policy_input = {
        "network": config.network,
        "netuid": config.netuid,
        "burn_uid": config.burn_uid,
        "forced_burn_percentage": config.forced_burn_percentage,
        "task_family_since_days": config.task_family_since_days,
        "task_family_weights": task_family_weights,
        "scores": scores,
    }
    digest = compute_policy_hash(policy_input)
    policy_version = int(issued.timestamp() * 1000)
    vector_id = f"{config.network}-{config.netuid}-{policy_version}-{digest[:12]}"
    vector = build_and_sign(
        scores,
        private_key,
        vector_id=vector_id,
        policy_version=policy_version,
        network=config.network,
        netuid=config.netuid,
        key_id=config.key_id,
        policy_reason="publisher_current_ranked_scores",
        burn_uid=config.burn_uid,
        forced_burn_percentage=config.forced_burn_percentage,
        generated_at=issued,
        valid_for=timedelta(seconds=config.valid_for_secs),
        policy_hash=digest,
        policy_metadata={
            "score_source": "agent_submissions.current_score+configured_task_family_rows",
            "ranked_hotkeys": len(scores),
            "task_family_since_days": config.task_family_since_days,
            "task_family_weights": task_family_weights,
            "input_sha256": digest,
        },
    )
    await store.set(vector)
    logger.info(
        "weight_policy_vector_produced",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        ranked_hotkeys=len(scores),
        burn_uid=config.burn_uid,
        forced_burn_percentage=config.forced_burn_percentage,
    )
    return vector


def _resolve_task_family_weights(configured: Mapping[str, float] | None) -> dict[str, float]:
    """Resolve signed-policy Task Family weights.

    SAT defaults to zero here as well as on validators. Remote signed
    vectors must not make a dark-launched lane weight-bearing unless the
    publisher policy explicitly opts it in.
    """
    out = {"synthetic_boolean_v1": 0.0}
    for key, value in (configured or {}).items():
        try:
            out[str(key)] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            continue

    raw_json = os.environ.get("CATHEDRAL_WEIGHT_POLICY_TASK_FAMILY_WEIGHTS_JSON")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("weight_policy_task_family_weights_json_invalid")
        else:
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    try:
                        out[str(key)] = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        logger.warning(
                            "weight_policy_task_family_weight_invalid",
                            task_family=str(key),
                        )
            else:
                logger.warning(
                    "weight_policy_task_family_weights_json_invalid",
                    reason="not_object",
                )

    raw_boolean = os.environ.get("CATHEDRAL_WEIGHT_POLICY_SYNTHETIC_BOOLEAN_V1_WEIGHT")
    if raw_boolean is None:
        raw_boolean = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT")
    if raw_boolean is not None:
        try:
            out["synthetic_boolean_v1"] = max(0.0, min(1.0, float(raw_boolean)))
        except ValueError:
            logger.warning("weight_policy_synthetic_boolean_v1_weight_invalid", value=raw_boolean)
    return out


async def run_weight_policy_producer(
    conn: Any,
    store: WeightPolicyStore,
    private_key: Ed25519PrivateKey,
    *,
    config: WeightPolicyProducerConfig,
    stop: asyncio.Event | None = None,
) -> None:
    """Continuously produce signed vectors on the safe chain cadence."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            await produce_weight_policy_once(conn, store, private_key, config=config)
        except Exception as exc:
            logger.warning("weight_policy_producer_error", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.interval_secs)
        except TimeoutError:
            pass


class WeightPolicyStore:
    """In-memory holder for the latest signed vector.

    The publisher mints new vectors on a cadence. The GET endpoint
    reads from here. Concurrent reads + a single writer are serialized
    with an asyncio lock so the read endpoint never returns a partially
    mutated vector.
    """

    def __init__(self) -> None:
        self._latest: SignedWeightVector | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> SignedWeightVector | None:
        async with self._lock:
            return self._latest

    async def set(self, vector: SignedWeightVector) -> None:
        async with self._lock:
            self._latest = vector

    async def clear(self) -> None:
        async with self._lock:
            self._latest = None


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


@router.get("/v1/validator/weights/next")
async def get_next_weight_vector(request: Request) -> dict[str, object]:
    """Serve the latest signed weight vector, or 503 if none yet.

    503 (not 404) signals "publisher is up but has no vector yet" so
    validators can distinguish a transient empty state from a
    misconfigured URL.
    """
    store: WeightPolicyStore | None = getattr(request.app.state, "weight_policy", None)
    if store is None:
        # Endpoint mounted but the app forgot to wire the store. Surface
        # as 503 - the validator's poll loop already treats 503 as
        # "publisher quiet". Operators see the warning in the log.
        logger.warning("weight_policy_store_not_wired")
        raise HTTPException(status_code=503, detail="weight policy not configured")
    vector = await store.get()
    if vector is None:
        raise HTTPException(status_code=503, detail="no weight vector available yet")
    return vector.to_payload()


def _ms_iso(dt: datetime) -> str:
    """ISO-8601 UTC, ms precision, trailing Z."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


__all__ = [
    "WeightPolicyProducerConfig",
    "WeightPolicyStore",
    "build_and_sign",
    "build_unsigned_vector",
    "compute_policy_hash",
    "latest_policy_scores_by_hotkey",
    "load_producer_from_env",
    "produce_weight_policy_once",
    "router",
    "run_weight_policy_producer",
]
