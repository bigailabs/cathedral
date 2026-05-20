"""Publisher-side signed weight-vector policy surface."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, HTTPException, Request, Response

from cathedral.policy.schemas import (
    SignedWeightVector,
    ms_iso,
    sign_vector,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

_SIGNING_KEY_ENV = "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY"
_ENABLED_ENV = "CATHEDRAL_WEIGHT_POLICY_ENABLED"


@dataclass(frozen=True)
class WeightPolicyProducerConfig:
    network: str = "finney"
    netuid: int = 39
    key_id: str = "cathedral-weight-policy"
    burn_hotkey: str = ""
    burn_uid_snapshot: int | None = None
    burn_share: float = 0.95
    metagraph_block: int = 0
    interval_secs: float = 60.0
    valid_for_secs: float = 600.0
    limit: int = 1000
    task_family_weights: Mapping[str, float] | None = None
    task_family_since_days: int = 7
    miner_overrides: Mapping[str, float] | None = None


def build_unsigned_vector(
    scores_by_hotkey: dict[str, float] | Iterable[tuple[str, float]],
    *,
    vector_id: str,
    policy_version: int,
    network: str,
    netuid: int,
    metagraph_block: int,
    key_id: str,
    burn_hotkey: str,
    burn_uid_snapshot: int | None,
    burn_share: float,
    issued_at: datetime,
    valid_for: timedelta = timedelta(minutes=10),
    policy_hash: str | None = None,
) -> SignedWeightVector:
    """Build a deterministic unsigned vector from scored hotkeys."""
    if not burn_hotkey.strip():
        raise ValueError("burn_hotkey is required")
    if not 0.0 <= burn_share <= 1.0:
        raise ValueError(f"burn_share must be in [0.0, 1.0], got {burn_share}")

    items = (
        list(scores_by_hotkey.items())
        if isinstance(scores_by_hotkey, dict)
        else list(scores_by_hotkey)
    )
    miner_scores: dict[str, float] = {}
    for hotkey, score in items:
        if not math.isfinite(score):
            raise ValueError(f"non-finite score for hotkey {hotkey!r}: {score!r}")
        if score <= 0.0 or hotkey == burn_hotkey:
            continue
        miner_scores[str(hotkey)] = miner_scores.get(str(hotkey), 0.0) + float(score)

    miner_total = sum(miner_scores.values())
    weights: dict[str, float] = {}
    miner_share = 1.0 - burn_share
    if miner_total > 0.0 and miner_share > 0.0:
        for hotkey in sorted(miner_scores):
            weights[hotkey] = (miner_scores[hotkey] / miner_total) * miner_share
    weights[burn_hotkey] = weights.get(burn_hotkey, 0.0) + burn_share
    if sum(weights.values()) <= 0.0:
        weights[burn_hotkey] = 1.0

    issued = ms_iso(issued_at)
    expires = ms_iso(issued_at + valid_for)
    resolved_hash = policy_hash or compute_policy_hash(
        {
            "network": network,
            "netuid": netuid,
            "metagraph_block": metagraph_block,
            "burn_hotkey": burn_hotkey,
            "burn_uid_snapshot": burn_uid_snapshot,
            "burn_share": burn_share,
            "weights_by_hotkey": weights,
        }
    )
    return SignedWeightVector(
        schema_version=1,
        policy_version=policy_version,
        vector_id=vector_id,
        issued_at=issued,
        expires_at=expires,
        network=network,
        netuid=netuid,
        metagraph_block=metagraph_block,
        burn_hotkey=burn_hotkey,
        burn_uid_snapshot=burn_uid_snapshot,
        weights_by_hotkey=weights,
        policy_hash=resolved_hash,
        key_id=key_id,
    )


def build_and_sign(
    scores_by_hotkey: dict[str, float] | Iterable[tuple[str, float]],
    private_key: Ed25519PrivateKey,
    *,
    vector_id: str,
    policy_version: int,
    network: str,
    netuid: int,
    metagraph_block: int,
    key_id: str,
    burn_hotkey: str,
    burn_uid_snapshot: int | None,
    burn_share: float,
    issued_at: datetime,
    valid_for: timedelta = timedelta(minutes=10),
    policy_hash: str | None = None,
) -> SignedWeightVector:
    return sign_vector(
        build_unsigned_vector(
            scores_by_hotkey,
            vector_id=vector_id,
            policy_version=policy_version,
            network=network,
            netuid=netuid,
            metagraph_block=metagraph_block,
            key_id=key_id,
            burn_hotkey=burn_hotkey,
            burn_uid_snapshot=burn_uid_snapshot,
            burn_share=burn_share,
            issued_at=issued_at,
            valid_for=valid_for,
            policy_hash=policy_hash,
        ),
        private_key,
    )


def compute_policy_hash(policy_input: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(policy_input, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class FileBackedWeightPolicyStore:
    """Atomic JSON store for the latest signed vector."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._latest: SignedWeightVector | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> SignedWeightVector | None:
        async with self._lock:
            if not self.path.exists():
                return None
            self._latest = SignedWeightVector.model_validate(json.loads(self.path.read_text()))
            return self._latest

    async def get(self) -> SignedWeightVector | None:
        async with self._lock:
            return self._latest

    async def set(self, vector: SignedWeightVector) -> None:
        payload = json.dumps(vector.to_payload(), sort_keys=True, separators=(",", ":"))
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        async with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload + "\n")
            os.replace(tmp, self.path)
            self._latest = vector

    async def clear(self) -> None:
        async with self._lock:
            self._latest = None


def load_producer_from_env() -> tuple[WeightPolicyProducerConfig, Ed25519PrivateKey] | None:
    enabled = os.environ.get(_ENABLED_ENV, "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None

    signing_hex = os.environ.get(_SIGNING_KEY_ENV, "").strip()
    if not signing_hex:
        raise RuntimeError(f"{_SIGNING_KEY_ENV} is required when {_ENABLED_ENV}=true")
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(signing_hex))
    except ValueError as exc:
        raise RuntimeError(f"{_SIGNING_KEY_ENV} must be a 32-byte Ed25519 seed hex") from exc

    burn_hotkey = os.environ.get("CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY", "").strip()
    if not burn_hotkey:
        raise RuntimeError("CATHEDRAL_WEIGHT_POLICY_BURN_HOTKEY is required")
    burn_uid_raw = os.environ.get("CATHEDRAL_WEIGHT_POLICY_BURN_UID_SNAPSHOT", "").strip()
    burn_uid_snapshot = int(burn_uid_raw) if burn_uid_raw else None
    cfg = WeightPolicyProducerConfig(
        network=os.environ.get("CATHEDRAL_WEIGHT_POLICY_NETWORK", "finney"),
        netuid=int(os.environ.get("CATHEDRAL_WEIGHT_POLICY_NETUID", "39")),
        key_id=os.environ.get("CATHEDRAL_WEIGHT_POLICY_KEY_ID", "cathedral-weight-policy"),
        burn_hotkey=burn_hotkey,
        burn_uid_snapshot=burn_uid_snapshot,
        burn_share=float(os.environ.get("CATHEDRAL_WEIGHT_POLICY_BURN_SHARE", "0.95")),
        metagraph_block=int(os.environ.get("CATHEDRAL_WEIGHT_POLICY_METAGRAPH_BLOCK", "0")),
        interval_secs=float(os.environ.get("CATHEDRAL_WEIGHT_POLICY_INTERVAL_SECS", "60.0")),
        valid_for_secs=float(os.environ.get("CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS", "600.0")),
        limit=int(os.environ.get("CATHEDRAL_WEIGHT_POLICY_LIMIT", "1000")),
        task_family_weights=_resolve_task_family_weights(None),
        task_family_since_days=int(
            os.environ.get("CATHEDRAL_WEIGHT_POLICY_TASK_FAMILY_SINCE_DAYS", "7")
        ),
        miner_overrides=_resolve_miner_overrides(None),
    )
    return cfg, private_key


async def latest_policy_scores_by_hotkey(
    conn: Any,
    *,
    limit: int = 1000,
    task_family_since_days: int = 7,
    task_family_weights: Mapping[str, float] | None = None,
    miner_overrides: Mapping[str, float] | None = None,
) -> dict[str, float]:
    """Read publisher-scored hotkeys as signed-policy input."""
    lane_weights = _resolve_task_family_weights(task_family_weights)
    overrides = _resolve_miner_overrides(miner_overrides)
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
    rows = await cur.fetchall()
    samples: dict[str, dict[str, list[float]]] = {}
    for hotkey_raw, weighted_raw, task_json_raw in rows:
        try:
            task_json = json.loads(task_json_raw) if isinstance(task_json_raw, str) else {}
        except json.JSONDecodeError:
            task_json = {}
        task_type = str(task_json.get("task_type") or "")
        if task_type not in lane_weights:
            continue
        samples.setdefault(str(hotkey_raw), {}).setdefault(task_type, []).append(
            float(weighted_raw)
        )

    scores = dict(base_scores)
    for hotkey in set(base_scores) | set(samples):
        base = base_scores.get(hotkey)
        weighted_total = 0.0
        active_weight = 0.0
        for task_type, lane_weight in lane_weights.items():
            bucket = samples.get(hotkey, {}).get(task_type)
            if bucket and lane_weight > 0.0:
                weighted_total += (sum(bucket) / len(bucket)) * lane_weight
                active_weight += lane_weight
        active_weight = min(active_weight, 1.0)
        weighted_total = min(weighted_total, 1.0)
        if base is not None:
            scores[hotkey] = (base * (1.0 - active_weight)) + weighted_total
        elif weighted_total > 0.0:
            scores[hotkey] = weighted_total
    for hotkey, score in overrides.items():
        if score > 0.0:
            scores[hotkey] = score
        else:
            scores.pop(hotkey, None)
    return scores


async def produce_weight_policy_once(
    conn: Any,
    store: FileBackedWeightPolicyStore,
    private_key: Ed25519PrivateKey,
    *,
    config: WeightPolicyProducerConfig,
    issued_at: datetime | None = None,
) -> SignedWeightVector:
    issued = issued_at or datetime.now(UTC)
    task_family_weights = _resolve_task_family_weights(config.task_family_weights)
    scores = await latest_policy_scores_by_hotkey(
        conn,
        limit=config.limit,
        task_family_weights=task_family_weights,
        task_family_since_days=config.task_family_since_days,
        miner_overrides=config.miner_overrides,
    )
    policy_input = {
        "network": config.network,
        "netuid": config.netuid,
        "metagraph_block": config.metagraph_block,
        "burn_hotkey": config.burn_hotkey,
        "burn_uid_snapshot": config.burn_uid_snapshot,
        "burn_share": config.burn_share,
        "task_family_since_days": config.task_family_since_days,
        "task_family_weights": task_family_weights,
        "miner_overrides": _resolve_miner_overrides(config.miner_overrides),
        "scores": scores,
    }
    digest = compute_policy_hash(policy_input)
    latest = await store.get()
    floor = latest.policy_version + 1 if latest is not None else 0
    policy_version = max(floor, int(issued.timestamp() * 1000))
    vector_id = f"{config.network}-{config.netuid}-{policy_version}-{digest[:12]}"
    vector = build_and_sign(
        scores,
        private_key,
        vector_id=vector_id,
        policy_version=policy_version,
        network=config.network,
        netuid=config.netuid,
        metagraph_block=config.metagraph_block,
        key_id=config.key_id,
        burn_hotkey=config.burn_hotkey,
        burn_uid_snapshot=config.burn_uid_snapshot,
        burn_share=config.burn_share,
        issued_at=issued,
        valid_for=timedelta(seconds=config.valid_for_secs),
        policy_hash=digest,
    )
    await store.set(vector)
    logger.info(
        "weight_policy_vector_produced",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        hotkeys=len(vector.weights_by_hotkey),
        burn_hotkey_prefix=config.burn_hotkey[:8],
        burn_uid_snapshot=config.burn_uid_snapshot,
    )
    return vector


async def run_weight_policy_producer(
    conn: Any,
    store: FileBackedWeightPolicyStore,
    private_key: Ed25519PrivateKey,
    *,
    config: WeightPolicyProducerConfig,
    stop: asyncio.Event | None = None,
) -> None:
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


def _resolve_task_family_weights(configured: Mapping[str, float] | None) -> dict[str, float]:
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
                logger.warning("weight_policy_task_family_weights_json_invalid")

    raw_boolean = os.environ.get("CATHEDRAL_WEIGHT_POLICY_SYNTHETIC_BOOLEAN_V1_WEIGHT")
    if raw_boolean is None:
        raw_boolean = os.environ.get("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT")
    if raw_boolean is not None:
        try:
            out["synthetic_boolean_v1"] = max(0.0, min(1.0, float(raw_boolean)))
        except ValueError:
            logger.warning("weight_policy_synthetic_boolean_v1_weight_invalid", value=raw_boolean)
    return out


def _resolve_miner_overrides(configured: Mapping[str, float] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for hotkey, value in (configured or {}).items():
        try:
            out[str(hotkey)] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            logger.warning("weight_policy_miner_override_invalid", hotkey=str(hotkey))

    raw_json = os.environ.get("CATHEDRAL_WEIGHT_POLICY_MINER_OVERRIDES_JSON")
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError:
            logger.warning("weight_policy_miner_overrides_json_invalid")
        else:
            if isinstance(parsed, dict):
                for hotkey, value in parsed.items():
                    try:
                        out[str(hotkey)] = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        logger.warning(
                            "weight_policy_miner_override_invalid",
                            hotkey=str(hotkey),
                        )
            else:
                logger.warning("weight_policy_miner_overrides_json_invalid")
    return out


@router.get("/v1/validator/weights/next")
async def get_next_weight_vector(request: Request, response: Response) -> dict[str, object]:
    store: FileBackedWeightPolicyStore | None = getattr(request.app.state, "weight_policy", None)
    if store is None:
        raise HTTPException(status_code=503, detail="weight policy not configured")
    vector = await store.get()
    if vector is None:
        raise HTTPException(status_code=503, detail="no weight vector available yet")
    response.headers["Cache-Control"] = "no-store"
    return vector.to_payload()


__all__ = [
    "FileBackedWeightPolicyStore",
    "WeightPolicyProducerConfig",
    "build_and_sign",
    "build_unsigned_vector",
    "compute_policy_hash",
    "latest_policy_scores_by_hotkey",
    "load_producer_from_env",
    "produce_weight_policy_once",
    "router",
    "run_weight_policy_producer",
]
