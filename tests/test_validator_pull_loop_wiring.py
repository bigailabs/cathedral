"""Stage A.2 — verify build_app wires run_pull_loop when public key is set."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cathedral.chain.client import Metagraph, MinerNode
from cathedral.chain.mock import MockChain
from cathedral.config import (
    HttpConfig,
    NetworkConfig,
    PolarisConfig,
    PublisherConfig,
    StallConfig,
    StorageConfig,
    ValidatorSettings,
    WeightsConfig,
    WorkerConfig,
)
from cathedral.evidence import EvidenceCollector
from cathedral.validator import build_app
from cathedral.validator.config_runtime import RuntimeContext
from cathedral.validator.health import Health
from tests.conftest import StubFetcher


def _settings(db_path: str) -> ValidatorSettings:
    return ValidatorSettings(
        network=NetworkConfig(name="local", netuid=1, validator_hotkey="5Vh"),
        polaris=PolarisConfig(
            base_url="http://example",
            public_key_hex="00" * 32,
            fetch_timeout_secs=5.0,
        ),
        http=HttpConfig(),
        weights=WeightsConfig(disabled=True),
        publisher=PublisherConfig(
            url="http://publisher.test",
            public_key_env="UNUSED_IN_TEST",
            pull_interval_secs=999.0,
        ),
        storage=StorageConfig(database_path=db_path),
        worker=WorkerConfig(poll_interval_secs=0.1, max_concurrent_verifications=1),
        stall=StallConfig(after_secs=600),
    )


@pytest.fixture
def deps(tmp_path):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key()
    settings = _settings(str(tmp_path / "val.db"))
    chain = MockChain(
        Metagraph(
            block=1,
            miners=(MinerNode(uid=0, hotkey="5Miner", last_update_block=1),),
        )
    )
    fetcher = StubFetcher()
    collector = EvidenceCollector(fetcher, pk)
    return settings, chain, collector, pk


def test_pull_loop_scheduled_when_public_key_present(deps) -> None:
    settings, chain, collector, pk = deps
    ctx = RuntimeContext(
        settings=settings,
        bearer="t",
        chain=chain,
        collector=collector,
        health=Health(),
        cathedral_public_key=pk,
    )
    app = build_app(ctx)
    with (
        patch("cathedral.validator.app.pull_loop.run_pull_loop") as run_pull,
        TestClient(app) as client,
    ):
        client.get("/health")
        assert run_pull.called, "run_pull_loop should be scheduled when key set"
        kwargs = run_pull.call_args.kwargs
        assert kwargs["publisher_url"] == "http://publisher.test"
        assert kwargs["interval_secs"] == 999.0
        assert kwargs["cathedral_public_key"] is pk


def test_pull_loop_skipped_when_public_key_absent(deps) -> None:
    settings, chain, collector, _ = deps
    ctx = RuntimeContext(
        settings=settings,
        bearer="t",
        chain=chain,
        collector=collector,
        health=Health(),
        cathedral_public_key=None,
    )
    app = build_app(ctx)
    with (
        patch("cathedral.validator.app.pull_loop.run_pull_loop") as run_pull,
        TestClient(app) as client,
    ):
        client.get("/health")
        assert not run_pull.called, "run_pull_loop must not be scheduled without a public key"
