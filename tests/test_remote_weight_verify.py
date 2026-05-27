from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cathedral.config import ValidatorSettings
from cathedral.publisher.weight_policy import build_and_sign
from cathedral.validator.remote_weight_verify import (
    RemoteWeightVectorFetchError,
    fetch_remote_weight_vector_for_verification,
    load_remote_weight_public_key,
    verify_remote_weight_vector_for_settings,
)


def _settings(*, network: str = "finney", netuid: int = 39) -> ValidatorSettings:
    return ValidatorSettings.model_validate(
        {
            "network": {
                "name": network,
                "netuid": netuid,
                "validator_hotkey": "operator-hotkey",
                "wallet_name": "cathedral-validator",
            },
            "polaris": {
                "base_url": "https://api.polaris.computer/",
                "public_key_hex": "11" * 32,
            },
            "remote_weight_source": {
                "enabled": True,
                "url": "https://api.cathedral.computer",
                "key_id": "cathedral-weight-policy",
                "public_key_env": "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX",
            },
        }
    )


def _vector(sk: Ed25519PrivateKey, *, network: str = "finney", netuid: int = 39):
    issued = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
    return build_and_sign(
        {"hk-a": 0.75, "hk-b": 0.25},
        sk,
        vector_id="vec-1",
        policy_version=1,
        network=network,
        netuid=netuid,
        key_id="cathedral-weight-policy",
        policy_reason="staging-test",
        burn_uid=204,
        forced_burn_percentage=85.0,
        generated_at=issued,
        valid_for=timedelta(hours=1),
    )


def _client_for(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://publisher.example",
    )


def test_verify_remote_weight_vector_accepts_matching_signed_vector() -> None:
    sk = Ed25519PrivateKey.generate()
    result = verify_remote_weight_vector_for_settings(
        _vector(sk),
        _settings(),
        sk.public_key(),
        now_iso="2026-05-21T12:05:00.000Z",
    )

    assert result.ok
    assert result.errors == ()
    assert result.details["vector_id"] == "vec-1"
    assert result.details["weight_entries"] == 2


def test_verify_remote_weight_vector_rejects_wrong_key() -> None:
    sk = Ed25519PrivateKey.generate()
    wrong_sk = Ed25519PrivateKey.generate()

    result = verify_remote_weight_vector_for_settings(
        _vector(sk),
        _settings(),
        wrong_sk.public_key(),
        now_iso="2026-05-21T12:05:00.000Z",
    )

    assert not result.ok
    assert "ed25519 signature verify failed" in result.errors


def test_verify_remote_weight_vector_rejects_network_mismatch() -> None:
    sk = Ed25519PrivateKey.generate()
    result = verify_remote_weight_vector_for_settings(
        _vector(sk, network="test"),
        _settings(network="finney"),
        sk.public_key(),
        now_iso="2026-05-21T12:05:00.000Z",
    )

    assert not result.ok
    assert "network mismatch: vector='test', validator='finney'" in result.errors


def test_load_remote_weight_public_key_reads_expected_env() -> None:
    sk = Ed25519PrivateKey.generate()
    public_hex = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    public_key, error = load_remote_weight_public_key(
        _settings(),
        env={"CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX": public_hex},
    )

    assert error is None
    assert public_key is not None


def test_load_remote_weight_public_key_rejects_missing_env() -> None:
    public_key, error = load_remote_weight_public_key(_settings(), env={})

    assert public_key is None
    assert error == "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX is required"


@pytest.mark.asyncio
async def test_fetch_remote_weight_vector_for_verification_parses_vector() -> None:
    sk = Ed25519PrivateKey.generate()
    vector = _vector(sk)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=vector.to_payload())

    async with _client_for(handler) as client:
        result = await fetch_remote_weight_vector_for_verification(
            client,
            publisher_url="https://publisher.example",
        )

    assert result is not None
    assert result.vector_id == "vec-1"


@pytest.mark.asyncio
async def test_fetch_remote_weight_vector_for_verification_rejects_bad_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a vector"})

    async with _client_for(handler) as client:
        with pytest.raises(RemoteWeightVectorFetchError):
            await fetch_remote_weight_vector_for_verification(
                client,
                publisher_url="https://publisher.example",
            )
