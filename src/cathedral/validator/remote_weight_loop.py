"""Remote signed-weight vector polling and apply helpers."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import aiosqlite
import httpx
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from cathedral.chain import Chain, normalize
from cathedral.chain.client import WeightStatus
from cathedral.policy.schemas import (
    SignedWeightVector,
    VectorVerificationError,
    parse_iso_utc,
    verify_vector,
)
from cathedral.validator import remote_state
from cathedral.validator.health import Health

logger = structlog.get_logger(__name__)

WEIGHTS_NEXT_PATH = "/v1/validator/weights/next"


class RemoteWeightFetchError(Exception):
    """The publisher did not return a usable signed vector."""


async def fetch_signed_vector(
    client: httpx.AsyncClient,
    *,
    publisher_weights_url: str,
) -> SignedWeightVector | None:
    try:
        resp = await client.get(publisher_weights_url)
    except httpx.HTTPError as exc:
        raise RemoteWeightFetchError(f"network error fetching remote vector: {exc}") from exc
    if resp.status_code == 503:
        return None
    if resp.status_code != 200:
        raise RemoteWeightFetchError(f"publisher returned {resp.status_code}: {resp.text[:200]!r}")
    try:
        body = _loads_no_duplicate_keys(resp.text)
        return SignedWeightVector.model_validate(body)
    except (ValueError, ValidationError) as exc:
        raise RemoteWeightFetchError(f"publisher vector schema invalid: {exc}") from exc


def map_weights_to_uids(
    vector: SignedWeightVector,
    *,
    uid_by_hotkey: dict[str, int],
) -> tuple[list[tuple[int, float]], list[str]]:
    raw: list[tuple[int, float]] = []
    dropped: list[str] = []
    for hotkey, weight in vector.weights_by_hotkey.items():
        uid = uid_by_hotkey.get(hotkey)
        if uid is None:
            dropped.append(hotkey)
            continue
        raw.append((uid, weight))
    return raw, dropped


def map_and_renormalize(
    vector: SignedWeightVector,
    *,
    uid_by_hotkey: dict[str, int],
) -> tuple[list[tuple[int, float]], list[str]]:
    raw, dropped = map_weights_to_uids(vector, uid_by_hotkey=uid_by_hotkey)
    return normalize(raw), dropped


async def run_remote_weight_loop(
    conn: aiosqlite.Connection,
    health: Health,
    *,
    publisher_weights_url: str,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
    poll_interval_secs: float = 60.0,
    request_timeout_secs: float = 10.0,
    stop: asyncio.Event | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    stop = stop or asyncio.Event()
    await remote_state.ensure_schema(conn)
    close_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=request_timeout_secs)
        close_client = True
    try:
        while not stop.is_set():
            await poll_once(
                conn,
                health,
                http_client=http_client,
                publisher_weights_url=publisher_weights_url,
                public_key=public_key,
                expected_key_id=expected_key_id,
                network=network,
                netuid=netuid,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval_secs)
            except TimeoutError:
                pass
    finally:
        if close_client:
            await http_client.aclose()


async def poll_once(
    conn: aiosqlite.Connection,
    health: Health,
    *,
    http_client: httpx.AsyncClient,
    publisher_weights_url: str,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
) -> None:
    try:
        vector = await fetch_signed_vector(http_client, publisher_weights_url=publisher_weights_url)
    except RemoteWeightFetchError as exc:
        logger.warning("remote_weight_fetch_error", error=str(exc))
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    state = await remote_state.load_state(conn)
    if vector is None:
        if state.cached_vector is None:
            logger.warning("remote_weight_no_vector_yet")
            await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        else:
            logger.info(
                "remote_weight_publisher_quiet",
                last_accepted_vector_id=state.last_accepted_vector_id,
                last_accepted_policy_version=state.last_accepted_policy_version,
            )
        return

    try:
        verify_vector(vector, public_key=public_key, expected_key_id=expected_key_id)
        vector.invariant_check(network=network, netuid=netuid, require_unexpired=True)
    except (VectorVerificationError, ValueError) as exc:
        logger.warning(
            "remote_weight_vector_rejected",
            error=str(exc),
            vector_id=vector.vector_id,
            policy_version=vector.policy_version,
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    if remote_state.is_replay_or_rollback(
        state,
        candidate_policy_version=vector.policy_version,
        candidate_vector_id=vector.vector_id,
    ):
        logger.warning(
            "remote_weight_replay_or_rollback_rejected",
            vector_id=vector.vector_id,
            policy_version=vector.policy_version,
            last_accepted_policy_version=state.last_accepted_policy_version,
            last_accepted_vector_id=state.last_accepted_vector_id,
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    await remote_state.record_accepted(
        conn,
        policy_version=vector.policy_version,
        vector_id=vector.vector_id,
        vector_payload=vector.to_payload(),
        sidecar={
            "issued_at": vector.issued_at,
            "expires_at": vector.expires_at,
            "key_id": vector.key_id,
            "policy_hash": vector.policy_hash,
            "metagraph_block": vector.metagraph_block,
            "burn_hotkey_prefix": vector.burn_hotkey[:8],
            "burn_uid_snapshot": vector.burn_uid_snapshot,
            "weight_hotkeys": len(vector.weights_by_hotkey),
        },
    )
    logger.info(
        "remote_weight_vector_cached",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        expires_at=vector.expires_at,
    )


async def apply_cached_remote_vector_once(
    conn: aiosqlite.Connection,
    chain: Chain,
    health: Health,
    *,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
    disabled: bool,
    fallback_after_stale_minutes: float,
    refuse_after_stale_minutes: float,
    now: datetime | None = None,
) -> None:
    state = await remote_state.load_state(conn)
    if state.cached_vector is None:
        logger.warning("remote_weight_no_cached_vector")
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    now = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        vector = SignedWeightVector.model_validate(state.cached_vector)
        verify_vector(vector, public_key=public_key, expected_key_id=expected_key_id)
        vector.invariant_check(
            network=network,
            netuid=netuid,
            now=now,
            require_unexpired=False,
        )
    except (ValidationError, VectorVerificationError, ValueError) as exc:
        logger.warning("remote_weight_cached_vector_rejected", error=str(exc))
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    stale = _stale_state(
        vector,
        now=now,
        fallback_after_stale_minutes=fallback_after_stale_minutes,
        refuse_after_stale_minutes=refuse_after_stale_minutes,
    )
    if stale == "refuse":
        logger.warning(
            "remote_weight_cached_vector_refused_stale",
            vector_id=vector.vector_id,
            expires_at=vector.expires_at,
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return
    if stale == "fallback":
        logger.warning(
            "remote_weight_using_stale_fallback",
            vector_id=vector.vector_id,
            expires_at=vector.expires_at,
        )

    metagraph = await chain.metagraph()
    registered = await chain.is_registered()
    await health.update(current_block=metagraph.block, registered=registered)
    await health.heartbeat("last_metagraph_at")

    normalized, dropped = map_and_renormalize(vector, uid_by_hotkey=metagraph.hotkey_to_uid())
    burn_hotkey_mapped = vector.burn_hotkey not in dropped
    logger.info(
        "remote_weight_mapped",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        signed_hotkeys=len(vector.weights_by_hotkey),
        mapped_uids=len(normalized),
        dropped_hotkeys=len(dropped),
        dropped_sample=[hotkey[:8] for hotkey in dropped[:5]],
        burn_hotkey_mapped=burn_hotkey_mapped,
    )
    if not burn_hotkey_mapped:
        logger.warning(
            "remote_weight_burn_hotkey_unmapped",
            vector_id=vector.vector_id,
            burn_hotkey_prefix=vector.burn_hotkey[:8],
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return
    if not normalized:
        logger.warning("remote_weight_no_mapped_entries", vector_id=vector.vector_id)
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    status = WeightStatus.DISABLED if disabled else await chain.set_weights(normalized)
    await health.update(weight_status=status)
    await health.heartbeat("last_weight_set_at")
    if status in {WeightStatus.HEALTHY, WeightStatus.DISABLED}:
        await remote_state.record_applied(
            conn,
            policy_version=vector.policy_version,
            vector_id=vector.vector_id,
        )
    logger.info(
        "remote_weight_relayed",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        status=status.value,
        count=len(normalized),
        uids=[uid for uid, _ in normalized][:20],
    )


async def _run_one_tick(
    conn: aiosqlite.Connection,
    chain: Chain,
    health: Health,
    *,
    http_client: httpx.AsyncClient,
    publisher_url: str,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
    disabled: bool,
) -> None:
    del chain, disabled
    await poll_once(
        conn,
        health,
        http_client=http_client,
        publisher_weights_url=publisher_url.rstrip("/") + WEIGHTS_NEXT_PATH,
        public_key=public_key,
        expected_key_id=expected_key_id,
        network=network,
        netuid=netuid,
    )


def _stale_state(
    vector: SignedWeightVector,
    *,
    now: datetime,
    fallback_after_stale_minutes: float,
    refuse_after_stale_minutes: float,
) -> str:
    expires = parse_iso_utc(vector.expires_at)
    if now <= expires:
        return "fresh"
    stale_minutes = (now - expires).total_seconds() / 60.0
    if stale_minutes >= refuse_after_stale_minutes:
        return "refuse"
    if stale_minutes >= fallback_after_stale_minutes:
        return "fallback"
    return "fresh"


def _loads_no_duplicate_keys(body: str) -> object:
    def hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON key {key!r}")
            out[key] = value
        return out

    return json.loads(body, object_pairs_hook=hook)


__all__ = [
    "WEIGHTS_NEXT_PATH",
    "RemoteWeightFetchError",
    "apply_cached_remote_vector_once",
    "fetch_signed_vector",
    "map_and_renormalize",
    "map_weights_to_uids",
    "poll_once",
    "run_remote_weight_loop",
]
