from __future__ import annotations

from datetime import UTC, datetime

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from cathedral.cards.registry import CardRegistry
from cathedral.chain.client import Metagraph, MinerNode
from cathedral.chain.mock import MockChain
from cathedral.config import (
    HttpConfig,
    NetworkConfig,
    PolarisConfig,
    StallConfig,
    StorageConfig,
    ValidatorSettings,
    WeightsConfig,
    WorkerConfig,
)
from cathedral.evidence import EvidenceCollector
from cathedral.types import PolarisAgentClaim
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
        storage=StorageConfig(database_path=db_path),
        worker=WorkerConfig(poll_interval_secs=0.1, max_concurrent_verifications=1),
        stall=StallConfig(after_secs=600),
    )


@pytest.fixture
def app_and_client(tmp_path):
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
    ctx = RuntimeContext(
        settings=settings,
        bearer="testtoken",
        chain=chain,
        collector=collector,
        registry=CardRegistry.baseline(),
        health=Health(),
    )
    app = build_app(ctx)
    with TestClient(app) as client:
        yield app, client


def test_health_is_public(app_and_client) -> None:
    _, client = app_and_client
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "weight_status" in body
    assert "stalled" in body


def test_claim_requires_bearer(app_and_client) -> None:
    _, client = app_and_client
    claim = PolarisAgentClaim(
        miner_hotkey="5Miner",
        owner_wallet="5Owner",
        work_unit="card:eu-ai-act",
        polaris_agent_id="agt_1",
        submitted_at=datetime.now(UTC),
    )
    r = client.post("/v1/claim", json=claim.model_dump(mode="json"))
    assert r.status_code == 401


def test_claim_accepted_with_bearer(app_and_client) -> None:
    _, client = app_and_client
    claim = PolarisAgentClaim(
        miner_hotkey="5Miner",
        owner_wallet="5Owner",
        work_unit="card:eu-ai-act",
        polaris_agent_id="agt_1",
        submitted_at=datetime.now(UTC),
    )
    r = client.post(
        "/v1/claim",
        headers={"Authorization": "Bearer testtoken"},
        json=claim.model_dump(mode="json"),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending"
    assert isinstance(body["id"], int)
