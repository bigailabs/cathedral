from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cathedral.evidence.fetch import MissingRecordError, PolarisFetcher
from cathedral.types import (
    Card,
    ConsumerClass,
    Jurisdiction,
    PolarisArtifactRecord,
    PolarisManifest,
    PolarisRunRecord,
    PolarisUsageRecord,
    Source,
    SourceClass,
    canonical_json_for_signing,
)


@pytest.fixture(autouse=True)
def _disable_submit_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """WS2: the per-hotkey submit rate limit defaults to 60s in production;
    disable it for tests (which fire many rapid submits per hotkey). Tests
    that specifically exercise rate limiting set their own interval."""
    monkeypatch.setenv("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "0")


@pytest.fixture
def keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key()


def _sign_model(sk: Ed25519PrivateKey, model_obj: Any) -> str:
    """Sign exactly the bytes the verifier will reconstruct from the model.

    Per CONTRACTS.md L3: must use exclude_none=True to match the verifier
    in cathedral.evidence.verify (which uses exclude_none) and the Polaris
    signer (polaris.services.cathedral_signing._to_canonical_dict, also
    exclude_none). This makes the manifest contract additive — new optional
    fields default to None and don't break old signatures.
    """
    dumped = model_obj.model_dump(by_alias=True, mode="json", exclude_none=True)
    payload = canonical_json_for_signing(dumped)
    return base64.b64encode(sk.sign(payload)).decode()


def make_signed_manifest(sk: Ed25519PrivateKey, agent_id: str = "agt_1") -> PolarisManifest:
    m = PolarisManifest.model_validate(
        {
            "polaris_agent_id": agent_id,
            "owner_wallet": "wallet_owner",
            "created_at": datetime.now(UTC).isoformat(),
            "schema": "polaris.agent.v1",
            "signature": "",
        }
    )
    m.signature = _sign_model(sk, m)
    return m


def make_signed_run(
    sk: Ed25519PrivateKey, run_id: str, agent_id: str = "agt_1"
) -> PolarisRunRecord:
    r = PolarisRunRecord.model_validate(
        {
            "run_id": run_id,
            "polaris_agent_id": agent_id,
            "started_at": datetime.now(UTC).isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "outcome": "ok",
            "signature": "",
        }
    )
    r.signature = _sign_model(sk, r)
    return r


def make_signed_artifact(
    sk: Ed25519PrivateKey,
    artifact_id: str,
    *,
    content: bytes,
    report: dict[str, Any] | None = None,
    agent_id: str = "agt_1",
) -> PolarisArtifactRecord:
    import blake3

    a = PolarisArtifactRecord.model_validate(
        {
            "artifact_id": artifact_id,
            "polaris_agent_id": agent_id,
            "run_id": None,
            "content_url": f"https://example.org/{artifact_id}",
            "content_hash": blake3.blake3(content).hexdigest(),
            "report_hash": json.dumps(report) if report is not None else None,
            "signature": "",
        }
    )
    a.signature = _sign_model(sk, a)
    return a


def make_signed_usage(
    sk: Ed25519PrivateKey,
    usage_id: str,
    *,
    consumer: ConsumerClass = ConsumerClass.EXTERNAL,
    consumer_wallet: str | None = "wallet_consumer",
    flagged: bool = False,
    refunded: bool = False,
    agent_id: str = "agt_1",
) -> PolarisUsageRecord:
    u = PolarisUsageRecord.model_validate(
        {
            "usage_id": usage_id,
            "polaris_agent_id": agent_id,
            "consumer": consumer.value,
            "consumer_wallet": consumer_wallet,
            "used_at": datetime.now(UTC).isoformat(),
            "flagged": flagged,
            "refunded": refunded,
            "signature": "",
        }
    )
    u.signature = _sign_model(sk, u)
    return u


def make_card(
    *,
    summary: str = "A summary of recent enforcement actions and guidance.",
    citations: list[Source] | None = None,
    no_legal_advice: bool = True,
    last_refreshed_at: datetime | None = None,
) -> Card:
    return Card(
        id="eu-ai-act",
        jurisdiction=Jurisdiction.EU,
        topic="EU AI Act enforcement",
        worker_owner_hotkey="5HotKey",
        polaris_agent_id="agt_1",
        title="EU AI Act update",
        summary=summary,
        what_changed="Recent rules tightened around foundation models. " * 5,
        why_it_matters="Affects compliance for many providers in the bloc. " * 4,
        action_notes="Review your model cards.",
        risks="Penalties up to 7 percent of revenue.",
        citations=citations
        if citations is not None
        else [
            Source(
                url="https://eur-lex.europa.eu/example",
                **{"class": SourceClass.OFFICIAL_JOURNAL},
                fetched_at=datetime.now(UTC),
                status=200,
                content_hash="deadbeef",
            )
        ],
        confidence=0.8,
        no_legal_advice=no_legal_advice,
        last_refreshed_at=last_refreshed_at or datetime.now(UTC),
        refresh_cadence_hours=24,
    )


class StubFetcher:
    """In-memory `PolarisFetcher` for tests."""

    def __init__(self) -> None:
        self.manifests: dict[str, PolarisManifest] = {}
        self.runs: dict[str, PolarisRunRecord] = {}
        self.artifacts: dict[str, PolarisArtifactRecord] = {}
        self.artifact_bytes: dict[str, bytes] = {}
        self.usage: dict[str, list[PolarisUsageRecord]] = {}

    async def fetch_manifest(self, polaris_agent_id: str) -> PolarisManifest:
        if polaris_agent_id not in self.manifests:
            raise MissingRecordError(polaris_agent_id)
        return self.manifests[polaris_agent_id]

    async def fetch_run(self, run_id: str) -> PolarisRunRecord:
        if run_id not in self.runs:
            raise MissingRecordError(run_id)
        return self.runs[run_id]

    async def fetch_artifact(self, artifact_id: str) -> PolarisArtifactRecord:
        if artifact_id not in self.artifacts:
            raise MissingRecordError(artifact_id)
        return self.artifacts[artifact_id]

    async def fetch_artifact_bytes(self, url: str) -> bytes:
        return self.artifact_bytes.get(url, b"")

    async def fetch_usage(self, polaris_agent_id: str) -> list[PolarisUsageRecord]:
        return self.usage.get(polaris_agent_id, [])


@pytest.fixture
def stub_fetcher() -> PolarisFetcher:  # type: ignore[return-value]
    return StubFetcher()
