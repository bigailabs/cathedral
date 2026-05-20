from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral.chain.client import Metagraph, MinerNode, WeightStatus
from cathedral.chain.mock import MockChain
from cathedral.publisher.weight_policy import build_and_sign
from cathedral.validator import remote_state, remote_weight_loop
from cathedral.validator.db import connect
from cathedral.validator.health import Health


def _make_vector(
    sk: Ed25519PrivateKey,
    *,
    policy_version: int = 1,
    vector_id: str = "vec-1",
    network: str = "finney",
    netuid: int = 39,
    key_id: str = "pinned",
    scores: dict[str, float] | None = None,
    issued_at: datetime | None = None,
    valid_for: timedelta = timedelta(minutes=10),
):
    return build_and_sign(
        scores or {"hk-a": 1.0},
        sk,
        vector_id=vector_id,
        policy_version=policy_version,
        network=network,
        netuid=netuid,
        metagraph_block=100,
        key_id=key_id,
        burn_hotkey="burn-hotkey",
        burn_uid_snapshot=204,
        burn_share=0.5,
        issued_at=issued_at or datetime.now(UTC),
        valid_for=valid_for,
    )


def _client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _chain(hotkeys: list[str]) -> MockChain:
    return MockChain(
        Metagraph(
            block=10,
            miners=tuple(
                MinerNode(uid=i + 1, hotkey=hotkey, last_update_block=1)
                for i, hotkey in enumerate(hotkeys)
            ),
        )
    )


@pytest.mark.asyncio
async def test_503_startup_without_cached_vector_refuses_set_weights(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    health = Health()
    chain = _chain(["hk-a"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no vector"})

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://publisher.example",
                public_key=sk.public_key(),
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        snap = await health.get()
        assert snap.weight_status is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
        assert chain.last_weights == []
        state = await remote_state.load_state(conn)
        assert state.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_poll_caches_and_weight_cadence_applies_signed_vector(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    vector = _make_vector(sk, scores={"hk-a": 1.0}, policy_version=2, vector_id="vec-2")
    health = Health()
    chain = _chain(["burn-hotkey", "hk-a"])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://publisher.example",
                public_key=sk.public_key(),
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []

        await remote_weight_loop.apply_cached_remote_vector_once(
            conn,
            chain,
            health,
            public_key=sk.public_key(),
            expected_key_id="pinned",
            network="finney",
            netuid=39,
            disabled=False,
            fallback_after_stale_minutes=0.0,
            refuse_after_stale_minutes=30.0,
        )
        assert dict(chain.last_weights) == {1: pytest.approx(0.5), 2: pytest.approx(0.5)}
        state = await remote_state.load_state(conn)
        assert state.last_accepted_policy_version == 2
        assert state.last_applied_policy_version == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_durable_state_rejects_rollback_after_reconnect(tmp_path) -> None:
    db_path = str(tmp_path / "v.db")
    conn = await connect(db_path)
    await remote_state.record_accepted(
        conn,
        policy_version=10,
        vector_id="prior",
        vector_payload=_make_vector(Ed25519PrivateKey.generate()).to_payload(),
    )
    await conn.close()

    conn = await connect(db_path)
    sk = Ed25519PrivateKey.generate()
    older = _make_vector(sk, policy_version=9, vector_id="older")
    health = Health()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=older.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                _chain(["hk-a"]),
                health,
                http_client=client,
                publisher_url="https://publisher.example",
                public_key=sk.public_key(),
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        state = await remote_state.load_state(conn)
        assert state.last_accepted_policy_version == 10
        assert state.last_accepted_vector_id == "prior"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_wrong_key_id_and_network_mismatch_are_rejected(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    health = Health()
    wrong_key = _make_vector(sk, key_id="rotated")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=wrong_key.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                _chain(["hk-a"]),
                health,
                http_client=client,
                publisher_url="https://publisher.example",
                public_key=sk.public_key(),
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert (await remote_state.load_state(conn)).last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cached_vector_fallback_then_refuse_after_stale_window(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    issued = datetime(2026, 5, 19, 0, 0, tzinfo=UTC)
    vector = _make_vector(sk, issued_at=issued, valid_for=timedelta(minutes=10))
    await remote_state.record_accepted(
        conn,
        policy_version=vector.policy_version,
        vector_id=vector.vector_id,
        vector_payload=vector.to_payload(),
    )
    health = Health()
    chain = _chain(["burn-hotkey", "hk-a"])
    try:
        await remote_weight_loop.apply_cached_remote_vector_once(
            conn,
            chain,
            health,
            public_key=sk.public_key(),
            expected_key_id="pinned",
            network="finney",
            netuid=39,
            disabled=False,
            fallback_after_stale_minutes=0.0,
            refuse_after_stale_minutes=30.0,
            now=datetime(2026, 5, 19, 0, 20, tzinfo=UTC),
        )
        assert chain.last_weights

        chain.last_weights = []
        await remote_weight_loop.apply_cached_remote_vector_once(
            conn,
            chain,
            health,
            public_key=sk.public_key(),
            expected_key_id="pinned",
            network="finney",
            netuid=39,
            disabled=False,
            fallback_after_stale_minutes=0.0,
            refuse_after_stale_minutes=30.0,
            now=datetime(2026, 5, 19, 0, 41, tzinfo=UTC),
        )
        assert chain.last_weights == []
        assert (await health.get()).weight_status is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_all_mapped_hotkeys_dropped_refuses_set_weights(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    vector = _make_vector(sk, scores={"hk-a": 1.0})
    await remote_state.record_accepted(
        conn,
        policy_version=vector.policy_version,
        vector_id=vector.vector_id,
        vector_payload=vector.to_payload(),
    )
    chain = _chain([])
    try:
        await remote_weight_loop.apply_cached_remote_vector_once(
            conn,
            chain,
            Health(),
            public_key=sk.public_key(),
            expected_key_id="pinned",
            network="finney",
            netuid=39,
            disabled=False,
            fallback_after_stale_minutes=0.0,
            refuse_after_stale_minutes=30.0,
        )
        assert chain.last_weights == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_unmapped_burn_hotkey_refuses_set_weights(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    vector = _make_vector(sk, scores={"hk-a": 1.0})
    await remote_state.record_accepted(
        conn,
        policy_version=vector.policy_version,
        vector_id=vector.vector_id,
        vector_payload=vector.to_payload(),
    )
    chain = _chain(["hk-a"])
    health = Health()
    try:
        await remote_weight_loop.apply_cached_remote_vector_once(
            conn,
            chain,
            health,
            public_key=sk.public_key(),
            expected_key_id="pinned",
            network="finney",
            netuid=39,
            disabled=False,
            fallback_after_stale_minutes=0.0,
            refuse_after_stale_minutes=30.0,
        )
        assert chain.last_weights == []
        assert (await health.get()).weight_status is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_duplicate_json_key_is_rejected() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"schema_version":1,"schema_version":1}',
            headers={"content-type": "application/json"},
        )

    async with _client_for(handler) as client:
        with pytest.raises(remote_weight_loop.RemoteWeightFetchError, match="duplicate"):
            await remote_weight_loop.fetch_signed_vector(
                client,
                publisher_weights_url="https://publisher.example/v1/validator/weights/next",
            )
