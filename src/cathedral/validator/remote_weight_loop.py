"""Remote signed-weight loop (issue #155).

Validators that opt in run a remote fetch loop plus the normal
``run_weight_loop`` cadence. The fetch loop verifies and caches signed
vectors. The weight loop decides when a cached vector may reach
``chain.set_weights``.

What the loop does each tick:

1. Fetch ``GET /v1/validator/weights/next`` from the configured
   publisher URL.
2. Verify Ed25519 signature against the pinned ``key_id`` + public key.
3. Run structural invariants (network, netuid, expires_at, finite +
   nonnegative weights, signed burn policy).
4. Reject rollbacks against the durable ``last_accepted_policy_version``.
5. Persist the accepted vector so the normal weight loop can apply it
   on its own chain cadence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiosqlite
import httpx
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError

from cathedral.chain import Chain
from cathedral.chain.client import Metagraph, WeightStatus
from cathedral.chain.weights import apply_burn, normalize
from cathedral.policy.signing import (
    SignedWeightVector,
    VectorVerificationError,
    verify_vector,
)
from cathedral.validator import remote_state
from cathedral.validator.health import Health

logger = structlog.get_logger(__name__)


WEIGHTS_NEXT_PATH = "/v1/validator/weights/next"


class RemoteWeightFetchError(Exception):
    """Could not fetch a usable vector from the publisher this tick."""


async def fetch_signed_vector(
    client: httpx.AsyncClient,
    *,
    publisher_url: str,
) -> SignedWeightVector | None:
    """Fetch and parse the next signed weight vector from the publisher.

    Returns ``None`` for HTTP 503 (publisher has no vector yet - this is
    a normal "starting up" state, not an error). Raises
    ``RemoteWeightFetchError`` for any other non-2xx, network error, or
    JSON-decode / schema failure.
    """
    url = publisher_url.rstrip("/") + WEIGHTS_NEXT_PATH
    try:
        resp = await client.get(url)
    except httpx.HTTPError as e:
        raise RemoteWeightFetchError(f"network error fetching {url}: {e}") from e
    if resp.status_code == 503:
        return None
    if resp.status_code != 200:
        raise RemoteWeightFetchError(
            f"publisher returned {resp.status_code} for {url}: {resp.text[:200]!r}"
        )
    try:
        body = resp.json()
    except ValueError as e:
        raise RemoteWeightFetchError(f"publisher response is not JSON: {e}") from e
    try:
        return SignedWeightVector.model_validate(body)
    except ValidationError as e:
        raise RemoteWeightFetchError(f"publisher response failed schema validation: {e}") from e


def map_and_renormalize(
    vector: SignedWeightVector,
    *,
    uid_by_hotkey: dict[str, int],
) -> tuple[list[tuple[int, float]], list[str]]:
    """Map signed (hotkey, weight) entries onto live uids and renormalize.

    Hotkeys missing from the metagraph are dropped - the publisher does
    not have a perfect view of which hotkeys are currently registered,
    so the validator filters locally. The dropped hotkeys are returned
    as the second tuple member for log observability.

    Returns ``(weight_pairs, dropped_hotkeys)``. ``weight_pairs`` is the
    output of ``cathedral.chain.weights.normalize`` against the mapped
    entries; if every entry was dropped or weights summed to zero the
    list is empty and the caller MUST NOT call set_weights.
    """
    raw, dropped = map_weights_to_uids(vector, uid_by_hotkey=uid_by_hotkey)
    return normalize(raw), dropped


def map_weights_to_uids(
    vector: SignedWeightVector,
    *,
    uid_by_hotkey: dict[str, int],
) -> tuple[list[tuple[int, float]], list[str]]:
    """Map signed (hotkey, weight) entries onto live uids without normalizing."""
    raw: list[tuple[int, float]] = []
    dropped: list[str] = []
    for entry in vector.weights:
        uid = uid_by_hotkey.get(entry.miner_hotkey)
        if uid is None:
            dropped.append(entry.miner_hotkey)
            continue
        raw.append((uid, entry.weight))
    return raw, dropped


async def run_remote_weight_loop(
    conn: aiosqlite.Connection,
    health: Health,
    *,
    publisher_url: str,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
    poll_interval_secs: float = 60.0,
    request_timeout_secs: float = 10.0,
    stop: asyncio.Event | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Fetch, verify, and cache remote vectors without setting weights."""
    stop = stop or asyncio.Event()
    await remote_state.ensure_schema(conn)

    close_client = False
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=request_timeout_secs)
        close_client = True

    try:
        while not stop.is_set():
            await _run_one_poll(
                conn,
                health,
                http_client=http_client,
                publisher_url=publisher_url,
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


async def _run_one_poll(
    conn: aiosqlite.Connection,
    health: Health,
    *,
    http_client: httpx.AsyncClient,
    publisher_url: str,
    public_key: Ed25519PublicKey,
    expected_key_id: str,
    network: str,
    netuid: int,
) -> None:
    """Fetch one remote vector and cache it if signature and policy pass."""
    try:
        vector = await fetch_signed_vector(http_client, publisher_url=publisher_url)
    except RemoteWeightFetchError as e:
        logger.warning("remote_weight_fetch_error", error=str(e))
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    state = await remote_state.load_state(conn)

    if vector is None:
        # Publisher has no vector yet. Refuse to set_weights and surface
        # the state via health so an operator notices. last-known-good
        # semantics: if we previously accepted any vector we keep that
        # state untouched; we just do not relay anything new this tick.
        if state.last_accepted_vector_id is None:
            logger.info("remote_weight_no_vector_yet", publisher=publisher_url)
            await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        else:
            logger.info(
                "remote_weight_publisher_quiet_keeping_last_known_good",
                last_accepted_vector_id=state.last_accepted_vector_id,
                last_accepted_policy_version=state.last_accepted_policy_version,
            )
        return

    try:
        verify_vector(vector, public_key=public_key, expected_key_id=expected_key_id)
    except VectorVerificationError as e:
        logger.warning(
            "remote_weight_signature_invalid",
            error=str(e),
            vector_id=vector.vector_id,
            key_id=vector.key_id,
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        vector.invariant_check(network=network, netuid=netuid, now_iso=now_iso)
    except ValueError as e:
        logger.warning(
            "remote_weight_invariant_rejected",
            error=str(e),
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
            vector_policy_version=vector.policy_version,
            last_accepted_policy_version=state.last_accepted_policy_version,
            vector_id=vector.vector_id,
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
            "generated_at": vector.generated_at,
            "expires_at": vector.expires_at,
            "key_id": vector.key_id,
            "policy_reason": vector.policy_reason,
            "burn_snapshot": vector.burn_snapshot.model_dump(mode="json"),
            "policy_hash": vector.policy_hash,
            "policy_metadata": vector.policy_metadata,
            "weight_entries": len(vector.weights),
        },
    )
    logger.info(
        "remote_weight_vector_cached",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        expires_at=vector.expires_at,
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
    """Compatibility test helper. It only polls and caches."""
    del chain, disabled
    await _run_one_poll(
        conn,
        health,
        http_client=http_client,
        publisher_url=publisher_url,
        public_key=public_key,
        expected_key_id=expected_key_id,
        network=network,
        netuid=netuid,
    )


async def _refresh_remote_chain_health(chain: Chain, health: Health) -> Metagraph:
    """Refresh metagraph health for a remote-weight apply tick."""
    metagraph = await chain.metagraph()
    registered = await chain.is_registered()
    await health.update(current_block=metagraph.block, registered=registered)
    await health.heartbeat("last_metagraph_at")
    return metagraph


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
) -> None:
    """Apply the cached vector through the normal weight-loop cadence."""
    state = await remote_state.load_state(conn)
    if state.cached_vector is None:
        logger.warning("remote_weight_no_cached_vector")
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    try:
        vector = SignedWeightVector.model_validate(state.cached_vector)
        verify_vector(vector, public_key=public_key, expected_key_id=expected_key_id)
        now_iso = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        vector.invariant_check(network=network, netuid=netuid, now_iso=now_iso)
    except (ValidationError, VectorVerificationError, ValueError) as e:
        logger.warning("remote_weight_cached_vector_rejected", error=str(e))
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    # Even an idempotent repeat is a healthy chain tick. Refresh this before
    # the repeat return so the stall watchdog does not treat a long-lived
    # cached vector as a dead validator.
    metagraph = await _refresh_remote_chain_health(chain, health)

    if remote_state.already_applied(
        state,
        candidate_policy_version=vector.policy_version,
        candidate_vector_id=vector.vector_id,
    ):
        logger.info(
            "remote_weight_repeat_skip",
            vector_id=vector.vector_id,
            policy_version=vector.policy_version,
        )
        return

    uid_by_hotkey = metagraph.hotkey_to_uid()
    mapped, dropped = map_weights_to_uids(vector, uid_by_hotkey=uid_by_hotkey)
    burn_uid = vector.burn_snapshot.burn_uid
    forced_burn_percentage = vector.burn_snapshot.forced_burn_percentage
    if burn_uid is not None:
        normalized = normalize(
            apply_burn(
                mapped,
                burn_uid=burn_uid,
                forced_burn_percentage=forced_burn_percentage,
            )
        )
    else:
        normalized = normalize(mapped)

    logger.info(
        "remote_weight_mapped",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        signed_entries=len(vector.weights),
        mapped_entries=len(normalized),
        burn_uid=burn_uid,
        forced_burn_percentage=forced_burn_percentage,
        dropped_hotkeys=len(dropped),
        dropped_sample=[hk[:8] for hk in dropped[:5]],
    )

    if not normalized:
        # Every hotkey was dropped (or summed to zero). Mark accepted
        # (already done above) but skip the chain call - sending no
        # weights would silently no-op.
        logger.warning(
            "remote_weight_no_mapped_entries",
            vector_id=vector.vector_id,
            dropped_hotkeys=len(dropped),
        )
        await health.update(weight_status=WeightStatus.BLOCKED_BY_TRANSACTION_ERROR)
        return

    if disabled:
        status = WeightStatus.DISABLED
    else:
        status = await chain.set_weights(normalized)
    await health.update(weight_status=status)
    await health.heartbeat("last_weight_set_at")

    if status is WeightStatus.HEALTHY or status is WeightStatus.DISABLED:
        # Treat DISABLED as "we successfully completed the dry-run for
        # this vector" - record applied so a follow-up tick with the
        # same vector_id is correctly recognised as a repeat.
        await remote_state.record_applied(
            conn,
            policy_version=vector.policy_version,
            vector_id=vector.vector_id,
        )

    # Canonical event name for a Path B chain.set_weights completion. The
    # `status` field is authoritative - HEALTHY means the extrinsic landed,
    # DISABLED means dry-run completed, BLOCKED_BY_* means the chain rejected
    # or never accepted it. Pairs with `chain_weights_set_local` in
    # weight_loop.py for Path A. Operators querying "did the validator
    # complete a set_weights attempt this tick?" should grep for
    # `chain_weights_set_*` and then read `status` to know the outcome.
    logger.info(
        "chain_weights_set_remote",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        status=status.value,
        count=len(normalized),
        uids=[uid for uid, _ in normalized][:20],
    )
    # DEPRECATED: dual-emitted for one release so log consumers can migrate to
    # `chain_weights_set_remote`. Will be removed in the next minor release.
    # The name is misleading: this is the chain set_weights completion event
    # (status field is authoritative), not a relay step. See AGENTS.md glossary.
    logger.info(
        "remote_weight_relayed",
        vector_id=vector.vector_id,
        policy_version=vector.policy_version,
        status=status.value,
        count=len(normalized),
        uids=[uid for uid, _ in normalized][:20],
    )


__all__ = [
    "WEIGHTS_NEXT_PATH",
    "RemoteWeightFetchError",
    "apply_cached_remote_vector_once",
    "fetch_signed_vector",
    "map_and_renormalize",
    "map_weights_to_uids",
    "run_remote_weight_loop",
]
