"""Issue #155: validator remote signed-weight loop + durable state tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cathedral.chain.client import Metagraph, MinerNode, WeightStatus
from cathedral.chain.mock import MockChain
from cathedral.policy.signing import WeightEntry
from cathedral.publisher.weight_policy import build_and_sign
from cathedral.validator import remote_state, remote_weight_loop
from cathedral.validator.app import from_settings
from cathedral.validator.db import connect
from cathedral.validator.health import Health

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _make_signed_vector(
    sk: Ed25519PrivateKey,
    *,
    policy_version: int = 1,
    vector_id: str = "vec-1",
    network: str = "finney",
    netuid: int = 39,
    key_id: str = "pinned",
    weights: dict[str, float] | None = None,
    valid_for: timedelta = timedelta(minutes=10),
    burn_uid: int | None = None,
    forced_burn_percentage: float = 0.0,
):
    scores = weights if weights is not None else {"hk-a": 0.6, "hk-b": 0.4}
    return build_and_sign(
        scores,
        sk,
        vector_id=vector_id,
        policy_version=policy_version,
        network=network,
        netuid=netuid,
        key_id=key_id,
        policy_reason="test policy",
        burn_uid=burn_uid,
        forced_burn_percentage=forced_burn_percentage,
        issued_at=_now(),
        valid_for=valid_for,
        policy_metadata={"source": "unit"},
    )


class _MockTransport(httpx.MockTransport):
    """Returns a configurable response (or sequence) for every GET."""

    def __init__(self, handler) -> None:
        super().__init__(handler)


def _client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_MockTransport(handler))


def _metagraph(hotkeys: list[str]) -> Metagraph:
    miners = tuple(
        MinerNode(uid=10 + i, hotkey=hk, last_update_block=1) for i, hk in enumerate(hotkeys)
    )
    return Metagraph(block=1, miners=miners)


async def _apply_cached(
    conn,
    chain: MockChain,
    health: Health,
    *,
    public_key,
    disabled: bool = False,
) -> None:
    await remote_weight_loop.apply_cached_remote_vector_once(
        conn,
        chain,
        health,
        public_key=public_key,
        expected_key_id="pinned",
        network="finney",
        netuid=39,
        disabled=disabled,
    )


# --------------------------------------------------------------------------
# Remote state - durable rollback protection
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_state_records_and_loads(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    try:
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
        assert s.last_applied_policy_version is None

        await remote_state.record_accepted(conn, policy_version=5, vector_id="vec-A")
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version == 5
        assert s.last_accepted_vector_id == "vec-A"
        # Apply must NOT have been touched.
        assert s.last_applied_policy_version is None

        await remote_state.record_applied(conn, policy_version=5, vector_id="vec-A")
        s = await remote_state.load_state(conn)
        assert s.last_applied_policy_version == 5
        assert s.last_applied_vector_id == "vec-A"
        # Accepted columns preserved.
        assert s.last_accepted_policy_version == 5
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_remote_state_survives_reconnect(tmp_path) -> None:
    db_path = str(tmp_path / "v.db")
    conn = await connect(db_path)
    try:
        await remote_state.record_accepted(conn, policy_version=9, vector_id="vec-Z")
        await remote_state.record_applied(conn, policy_version=9, vector_id="vec-Z")
    finally:
        await conn.close()

    # Fresh connection - same DB file. Durable state must round-trip.
    conn2 = await connect(db_path)
    try:
        s = await remote_state.load_state(conn2)
        assert s.last_accepted_policy_version == 9
        assert s.last_applied_vector_id == "vec-Z"
    finally:
        await conn2.close()


def test_is_rollback_strictly_less_than() -> None:
    s = remote_state.RemoteWeightState(last_accepted_policy_version=10)
    assert remote_state.is_rollback(s, candidate_policy_version=9) is True
    assert remote_state.is_rollback(s, candidate_policy_version=10) is False
    assert remote_state.is_rollback(s, candidate_policy_version=11) is False
    # Bootstrap path: first accept never counts as rollback.
    empty = remote_state.RemoteWeightState()
    assert remote_state.is_rollback(empty, candidate_policy_version=0) is False


def test_is_replay_or_rollback_rejects_same_version_new_vector_id() -> None:
    s = remote_state.RemoteWeightState(
        last_accepted_policy_version=10,
        last_accepted_vector_id="vec-A",
    )
    assert (
        remote_state.is_replay_or_rollback(
            s, candidate_policy_version=9, candidate_vector_id="vec-old"
        )
        is True
    )
    assert (
        remote_state.is_replay_or_rollback(
            s, candidate_policy_version=10, candidate_vector_id="vec-B"
        )
        is True
    )
    assert (
        remote_state.is_replay_or_rollback(
            s, candidate_policy_version=10, candidate_vector_id="vec-A"
        )
        is False
    )
    assert (
        remote_state.is_replay_or_rollback(
            s, candidate_policy_version=11, candidate_vector_id="vec-C"
        )
        is False
    )


def test_already_applied_requires_both_match() -> None:
    s = remote_state.RemoteWeightState(
        last_applied_policy_version=4, last_applied_vector_id="vec-A"
    )
    assert (
        remote_state.already_applied(s, candidate_policy_version=4, candidate_vector_id="vec-A")
        is True
    )
    assert (
        remote_state.already_applied(s, candidate_policy_version=4, candidate_vector_id="vec-B")
        is False
    )
    assert (
        remote_state.already_applied(s, candidate_policy_version=5, candidate_vector_id="vec-A")
        is False
    )


# --------------------------------------------------------------------------
# fetch_signed_vector
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_returns_none_for_503() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no vector yet"})

    async with _client_for(handler) as client:
        result = await remote_weight_loop.fetch_signed_vector(
            client, publisher_url="https://pub.example"
        )
        assert result is None


@pytest.mark.asyncio
async def test_fetch_parses_200_body() -> None:
    sk = Ed25519PrivateKey.generate()
    vector = _make_signed_vector(sk)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    async with _client_for(handler) as client:
        result = await remote_weight_loop.fetch_signed_vector(
            client, publisher_url="https://pub.example"
        )
        assert result is not None
        assert result.vector_id == vector.vector_id


@pytest.mark.asyncio
async def test_fetch_raises_on_invalid_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"garbage": True})

    async with _client_for(handler) as client:
        with pytest.raises(remote_weight_loop.RemoteWeightFetchError):
            await remote_weight_loop.fetch_signed_vector(
                client, publisher_url="https://pub.example"
            )


# --------------------------------------------------------------------------
# map_and_renormalize
# --------------------------------------------------------------------------


def test_map_and_renormalize_drops_unknown_hotkeys() -> None:
    sk = Ed25519PrivateKey.generate()
    vector = _make_signed_vector(sk, weights={"hk-known": 0.5, "hk-missing": 0.5})
    uid_by_hotkey = {"hk-known": 17}
    normalized, dropped = remote_weight_loop.map_and_renormalize(
        vector, uid_by_hotkey=uid_by_hotkey
    )
    assert dropped == ["hk-missing"]
    assert normalized == [(17, 1.0)]


def test_map_and_renormalize_returns_empty_when_all_dropped() -> None:
    sk = Ed25519PrivateKey.generate()
    vector = _make_signed_vector(sk, weights={"hk-x": 0.5, "hk-y": 0.5})
    normalized, dropped = remote_weight_loop.map_and_renormalize(vector, uid_by_hotkey={})
    assert normalized == []
    assert set(dropped) == {"hk-x", "hk-y"}


# --------------------------------------------------------------------------
# _run_one_tick - end-to-end branches
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_with_503_and_no_prior_state_refuses_set_weights(tmp_path) -> None:
    """Remote startup with no vector: must refuse set_weights, must
    report unhealthy via Health, must not touch the chain."""
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no vector"})

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        snap = await health.get()
        assert snap.weight_status is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
        assert snap.last_weight_set_at is None
        assert chain.last_weights == []
        # Durable state must not have advanced.
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
        assert s.last_applied_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_relays_accepted_vector_and_records_durable_state(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a", "hk-b"]))
    health = Health()

    vector = _make_signed_vector(
        sk,
        policy_version=3,
        vector_id="vec-X",
        weights={"hk-a": 0.7, "hk-b": 0.3},
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            assert chain.last_weights == []
            await _apply_cached(conn, chain, health, public_key=pk)

        snap = await health.get()
        assert snap.weight_status is WeightStatus.HEALTHY
        assert snap.last_weight_set_at is not None
        # Chain received normalized weights for the live uids.
        assert len(chain.last_weights) == 2
        # Both uids present.
        assert {uid for uid, _ in chain.last_weights} == {10, 11}
        total = sum(w for _, w in chain.last_weights)
        assert total == pytest.approx(1.0)

        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version == 3
        assert s.last_accepted_vector_id == "vec-X"
        assert s.last_applied_policy_version == 3
        assert s.last_applied_vector_id == "vec-X"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_does_not_resubmit_same_vector_twice(tmp_path) -> None:
    """Idempotency: repeated same vector_id+policy_version must not
    cause a second chain submission. (Decouples poll cadence from
    chain set_weights cadence.)"""
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(sk, policy_version=5, vector_id="vec-Y", weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
            assert len(chain.last_weights) == 1

            # Clear the chain history; second tick with the same vector
            # must NOT push again.
            chain.last_weights = []
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
            assert chain.last_weights == []
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_rollback_policy_version(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    # Pre-seed durable state at policy_version=10.
    await remote_state.record_accepted(conn, policy_version=10, vector_id="prior")
    await remote_state.record_applied(conn, policy_version=10, vector_id="prior")

    # Publisher tries to serve policy_version=9 - must be rejected.
    older = _make_signed_vector(sk, policy_version=9, vector_id="vec-rollback")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=older.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        # Chain must not have been called.
        assert chain.last_weights == []
        # Durable accepted state must NOT have regressed.
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version == 10
        assert s.last_accepted_vector_id == "prior"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_same_policy_version_different_vector_id(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    await remote_state.record_accepted(conn, policy_version=10, vector_id="prior")
    await remote_state.record_applied(conn, policy_version=10, vector_id="prior")

    replay = _make_signed_vector(sk, policy_version=10, vector_id="different")

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=replay.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_vector_id == "prior"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_wrong_key_id(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    # Vector says "rotated-key" but validator is pinned to "pinned".
    vector = _make_signed_vector(sk, key_id="rotated-key", weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_network_mismatch(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    # Vector targets 'test' but validator expects 'finney'.
    vector = _make_signed_vector(sk, network="test", weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_netuid_mismatch(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(sk, netuid=12, weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_tampered_signature(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(sk, weights={"hk-a": 1.0})
    tampered = vector.model_copy(update={"weights": [WeightEntry(miner_hotkey="hk-a", weight=0.2)]})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=tampered.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_rejects_expired_vector(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(sk, weights={"hk-a": 1.0}, valid_for=timedelta(seconds=-1))

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
        assert chain.last_weights == []
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_applies_signed_burn_policy(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(
        sk,
        policy_version=6,
        vector_id="vec-burn",
        weights={"hk-a": 1.0},
        burn_uid=204,
        forced_burn_percentage=95.0,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
        weights = dict(chain.last_weights)
        assert weights[10] == pytest.approx(0.05)
        assert weights[204] == pytest.approx(0.95)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_empty_scores_with_signed_burn_sets_burn_uid(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph([]))
    health = Health()

    vector = _make_signed_vector(
        sk,
        policy_version=7,
        vector_id="vec-empty-burn",
        weights={},
        burn_uid=204,
        forced_burn_percentage=95.0,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
        assert chain.last_weights == [(204, 1.0)]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_in_disabled_mode_records_state_but_skips_chain(tmp_path) -> None:
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(sk, policy_version=2, vector_id="vec-dry", weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=True,
            )
            await _apply_cached(conn, chain, health, public_key=pk, disabled=True)
        # Dry-run: chain was NOT called.
        assert chain.last_weights == []
        snap = await health.get()
        assert snap.weight_status is WeightStatus.DISABLED
        # Durable state still records accept + applied (so a follow-up
        # tick with the same vector_id correctly short-circuits).
        s = await remote_state.load_state(conn)
        assert s.last_accepted_policy_version == 2
        assert s.last_applied_policy_version == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_dropped_hotkeys_renormalize_remainder(tmp_path) -> None:
    """Validator drops hotkeys not on chain and renormalizes the rest."""
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    # Only hk-a registered on chain; hk-b is in the signed vector but
    # absent from the metagraph.
    chain = MockChain(_metagraph(["hk-a"]))
    health = Health()

    vector = _make_signed_vector(
        sk,
        policy_version=1,
        vector_id="vec-drop",
        weights={"hk-a": 0.3, "hk-b": 0.7},
    )

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
        # Only the surviving uid; renormalized to 1.0.
        assert chain.last_weights == [(10, 1.0)]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_tick_with_no_mapped_entries_skips_chain(tmp_path) -> None:
    """If every signed hotkey is missing on chain, refuse to submit
    rather than silently no-op."""
    conn = await connect(str(tmp_path / "v.db"))
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    chain = MockChain(_metagraph([]))  # empty metagraph
    health = Health()

    vector = _make_signed_vector(sk, weights={"hk-a": 1.0})

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    try:
        async with _client_for(handler) as client:
            await remote_weight_loop._run_one_tick(
                conn,
                chain,
                health,
                http_client=client,
                publisher_url="https://pub.example",
                public_key=pk,
                expected_key_id="pinned",
                network="finney",
                netuid=39,
                disabled=False,
            )
            await _apply_cached(conn, chain, health, public_key=pk)
        assert chain.last_weights == []
        snap = await health.get()
        assert snap.weight_status is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
    finally:
        await conn.close()


def test_from_settings_remote_enabled_missing_key_fails_closed(tmp_path, monkeypatch) -> None:
    polaris_pk = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            Encoding.Raw,
            PublicFormat.Raw,
        )
    )
    cfg = tmp_path / "validator.toml"
    cfg.write_text(
        "\n".join(
            [
                "[network]",
                'name = "finney"',
                "netuid = 39",
                'validator_hotkey = "default"',
                "",
                "[polaris]",
                'base_url = "https://polaris.invalid"',
                f'public_key_hex = "{polaris_pk.hex()}"',
                "",
                "[remote_weight_source]",
                "enabled = true",
                'public_key_env = "MISSING_REMOTE_WEIGHT_POLICY_KEY"',
                "",
            ]
        )
    )
    monkeypatch.delenv("MISSING_REMOTE_WEIGHT_POLICY_KEY", raising=False)
    monkeypatch.setenv("CATHEDRAL_BEARER", "test-bearer")

    with pytest.raises(RuntimeError, match="refusing local fallback"):
        from_settings(str(cfg))
