"""Stage B.4 - publisher-side ProbeRunner."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import blake3
import httpx
import pytest

# Pre-warm the publisher import path before touching cathedral.eval.* - same
# pattern as tests/v1/test_polaris_runtime_orchestrator_wiring.py.
import cathedral.publisher.app  # noqa: F401
from cathedral.eval.runner_types import PolarisRunnerError
from cathedral.eval.probe_runner import (
    ProbeRunner,
    ProbeSignatureError,
    ProbeTransportError,
    _canonical_json,
)
from cathedral.v1_types import EvalTask


def _make_task(card_id: str = "eu-ai-act", prompt: str = "what changed?") -> EvalTask:
    return EvalTask(
        card_id=card_id,
        epoch=1,
        round_index=0,
        prompt=prompt,
        sources=[],
        deadline_minutes=25,
    )


def _make_signed_probe_output(
    *,
    keypair: Any,
    card_id: str = "eu-ai-act",
    sub_id: str = "sub_test",
    output_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output_card = output_card or {
        "id": card_id,
        "title": "test",
        "summary": "test summary",
        "worker_owner_hotkey": keypair.ss58_address,
    }
    output_card_hash = blake3.blake3(_canonical_json(output_card)).hexdigest()
    payload = {
        "submission_id": sub_id,
        "card_id": card_id,
        "output_card": output_card,
        "output_card_hash": output_card_hash,
        "task_hash": "0" * 64,
        "ran_at": datetime.now(UTC).isoformat(),
        "duration_ms": 123,
    }
    sig = keypair.sign(_canonical_json(payload))
    payload["miner_signature"] = base64.b64encode(sig).decode()
    return payload


@pytest.fixture
def alice():
    from bittensor_wallet import Keypair

    return Keypair.create_from_uri("//Alice")


@pytest.fixture
def bob():
    from bittensor_wallet import Keypair

    return Keypair.create_from_uri("//Bob")


@pytest.fixture
def httpx_mock_factory(monkeypatch):
    """Patches httpx.AsyncClient.post to return a configured response."""

    class _Captured:
        def __init__(self) -> None:
            self.body: dict[str, Any] = {}
            self.url: str = ""

    captured = _Captured()

    def _make(response_json: dict[str, Any], status_code: int = 200):
        async def fake_post(self, url, json=None, **kwargs):
            captured.url = url
            captured.body = json
            req = httpx.Request("POST", url)
            return httpx.Response(status_code, json=response_json, request=req)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return captured

    return _make


@pytest.mark.asyncio
async def test_probe_run_verifies_signature_and_returns_card(alice, httpx_mock_factory) -> None:
    payload = _make_signed_probe_output(keypair=alice)
    captured = httpx_mock_factory(payload)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    result = await runner.run(
        bundle_bytes=b"fake-bundle",
        bundle_hash="h" * 64,
        task=_make_task(),
        miner_hotkey=alice.ss58_address,
        submission={"id": "sub_test"},
    )

    assert result.output_card_json["id"] == "eu-ai-act"
    assert result.duration_ms == 123
    assert result.probe_attestation is not None
    assert result.probe_attestation["miner_ss58"] == alice.ss58_address
    assert captured.url == "https://probe-1.test:8088/probe/run"
    assert captured.body["card_id"] == "eu-ai-act"
    assert captured.body["bundle_bytes_b64"] == base64.b64encode(b"fake-bundle").decode()


@pytest.mark.asyncio
async def test_probe_run_rejects_wrong_signer(alice, bob, httpx_mock_factory) -> None:
    payload = _make_signed_probe_output(keypair=bob)
    httpx_mock_factory(payload)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(ProbeSignatureError):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(),
            miner_hotkey=alice.ss58_address,
            submission={"id": "sub_test"},
        )


@pytest.mark.asyncio
async def test_probe_run_rejects_tampered_card(alice, httpx_mock_factory) -> None:
    payload = _make_signed_probe_output(keypair=alice)
    payload["output_card"] = {**payload["output_card"], "title": "tampered"}
    httpx_mock_factory(payload)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(PolarisRunnerError, match="output_card_hash mismatch"):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(),
            miner_hotkey=alice.ss58_address,
            submission={"id": "sub_test"},
        )


@pytest.mark.asyncio
async def test_probe_run_rejects_card_id_mismatch(alice, httpx_mock_factory) -> None:
    payload = _make_signed_probe_output(keypair=alice, card_id="wrong-card")
    httpx_mock_factory(payload)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(PolarisRunnerError, match="card_id"):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(card_id="eu-ai-act"),
            miner_hotkey=alice.ss58_address,
            submission={"id": "sub_test"},
        )


@pytest.mark.asyncio
async def test_probe_run_rejects_miner_hotkey_mismatch(alice, bob) -> None:
    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(ProbeSignatureError, match="does not match"):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(),
            miner_hotkey=bob.ss58_address,
            submission={"id": "sub_test"},
        )


@pytest.mark.asyncio
async def test_probe_run_wraps_transport_errors(alice, monkeypatch) -> None:
    async def fake_post(self, url, **kwargs):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(ProbeTransportError, match="probe transport"):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(),
            miner_hotkey=alice.ss58_address,
            submission={"id": "sub_test"},
        )


@pytest.mark.asyncio
async def test_probe_run_rejects_missing_fields(alice, httpx_mock_factory) -> None:
    payload = _make_signed_probe_output(keypair=alice)
    del payload["task_hash"]
    httpx_mock_factory(payload)

    runner = ProbeRunner(probe_url="https://probe-1.test:8088", miner_ss58=alice.ss58_address)
    with pytest.raises(PolarisRunnerError, match="missing field"):
        await runner.run(
            bundle_bytes=b"x",
            bundle_hash="h" * 64,
            task=_make_task(),
            miner_hotkey=alice.ss58_address,
            submission={"id": "sub_test"},
        )
