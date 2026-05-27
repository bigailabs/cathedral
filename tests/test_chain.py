from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cathedral import SPEC_VERSION
from cathedral.chain import MockChain, normalize
from cathedral.chain.client import (
    BittensorChain,
    Metagraph,
    MinerNode,
    WeightStatus,
    _classify_error,
    network_endpoint,
)


def test_network_endpoint_known() -> None:
    assert network_endpoint("finney").startswith("wss://")
    assert network_endpoint("test").startswith("wss://")
    assert network_endpoint("local").startswith("ws://")


def test_network_endpoint_unknown_raises() -> None:
    with pytest.raises(ValueError):
        network_endpoint("nope")


def test_classify_error_stake_fragments() -> None:
    assert _classify_error("validator permit not held") is WeightStatus.BLOCKED_BY_STAKE
    assert _classify_error("not enough stake") is WeightStatus.BLOCKED_BY_STAKE
    assert _classify_error("min_allowed_weights") is WeightStatus.BLOCKED_BY_STAKE


def test_classify_error_other() -> None:
    assert _classify_error("rpc timeout") is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR
    assert _classify_error("") is WeightStatus.BLOCKED_BY_TRANSACTION_ERROR


@pytest.mark.asyncio
async def test_mock_chain_is_registered_default() -> None:
    chain = MockChain(
        Metagraph(block=1, miners=(MinerNode(uid=0, hotkey="5h", last_update_block=1),))
    )
    assert await chain.is_registered() is True
    assert (await chain.metagraph()).block == 1


@pytest.mark.asyncio
async def test_mock_chain_set_weights_records_input() -> None:
    chain = MockChain()
    status = await chain.set_weights([(1, 0.5), (2, 0.5)])
    assert status is WeightStatus.HEALTHY
    assert chain.last_weights == [(1, 0.5), (2, 0.5)]


def test_spec_version_matches_release() -> None:
    # MAJOR=2, MINOR=0, PATCH=1 -> 2_000_001
    assert SPEC_VERSION == 2_000_001


@pytest.mark.asyncio
async def test_bittensor_chain_set_weights_passes_spec_version() -> None:
    """The real BittensorChain must stamp version_key=SPEC_VERSION on
    every set_weights extrinsic so on-chain observers can identify
    Cathedral-binary weight-sets unambiguously."""
    chain = BittensorChain.__new__(BittensorChain)
    chain.netuid = 39
    mock_subtensor = MagicMock()
    mock_subtensor.set_weights.return_value = SimpleNamespace(success=True, message="")
    chain._subtensor = mock_subtensor
    chain._wallet = MagicMock()
    chain._ensure_clients = MagicMock()

    status = await chain.set_weights([(1, 0.5), (2, 0.5)])

    assert status is WeightStatus.HEALTHY
    kwargs = mock_subtensor.set_weights.call_args.kwargs
    assert kwargs["version_key"] == SPEC_VERSION
    assert kwargs["netuid"] == 39
    assert kwargs["uids"] == [1, 2]
    assert kwargs["weights"] == [0.5, 0.5]


@pytest.mark.asyncio
async def test_bittensor_chain_metagraph_carries_permit_and_stake() -> None:
    chain = BittensorChain.__new__(BittensorChain)
    chain.netuid = 39
    chain._ensure_clients = MagicMock()
    chain._wallet = MagicMock()
    chain._subtensor = MagicMock()
    chain._subtensor.metagraph.return_value = SimpleNamespace(
        uids=[7],
        hotkeys=["5hk"],
        last_update=[120],
        validator_permit=[True],
        S=[123.5],
        block=130,
    )

    metagraph = await chain.metagraph()

    assert metagraph.block == 130
    assert metagraph.miners == (
        MinerNode(
            uid=7,
            hotkey="5hk",
            last_update_block=120,
            validator_permit=True,
            stake=123.5,
        ),
    )


@pytest.mark.asyncio
async def test_bittensor_chain_reads_wallet_hotkey_and_hyperparameters() -> None:
    chain = BittensorChain.__new__(BittensorChain)
    chain.netuid = 39
    chain._ensure_clients = MagicMock()
    chain._wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5validator"))
    chain._subtensor = MagicMock()
    chain._subtensor.get_subnet_hyperparameters.return_value = SimpleNamespace(
        weights_rate_limit=100,
        commit_reveal_weights_enabled=True,
    )

    assert await chain.validator_hotkey_ss58() == "5validator"
    assert await chain.subnet_hyperparameters() == {
        "commit_reveal_weights_enabled": True,
        "weights_rate_limit": 100,
    }


def test_normalize_basic() -> None:
    out = normalize([(0, 1.0), (1, 1.0), (2, 2.0)])
    total = sum(w for _, w in out)
    assert abs(total - 1.0) < 1e-6
