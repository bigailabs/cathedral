"""Tests for the testnet mechanism weight-set + receipt stage.

Focus: the HARD safety gates on ``set_weights`` (mainnet refusal, triple-gated
live broadcast, dry-run default) and the signed-artifact shape (schema, per-uid
weights, merkle/receipt, verifiable signature).
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold.publisher import mechanism_weightset as mw
from scaffold.wire_vector import canonical_bytes


def _keypair() -> tuple[str, str]:
    sk = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    sk_hex = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    ).hex()
    pk_hex = (
        sk.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )
    return sk_hex, pk_hex


COMPOSED = {5: 0.5, 2: 0.3, 9: 0.2}


class _Spy:
    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, artifact):
        self.calls.append(artifact)


# --- HARD SAFETY: mainnet / finney refusal ----------------------------------


@pytest.mark.parametrize(
    "network,netuid",
    [
        ("finney", 123),  # finney network
        ("mainnet", 123),  # alias
        ("main", 123),  # alias
        ("test", 39),  # mainnet netuid even on a testnet network name
    ],
)
def test_set_weights_refuses_mainnet(network, netuid):
    sk_hex, _ = _keypair()
    spy = _Spy()
    with pytest.raises(mw.UnsafeNetworkError):
        mw.set_weights(
            COMPOSED,
            netuid=netuid,
            network=network,
            signing_key_hex=sk_hex,
            confirm=True,
            broadcast_fn=spy,
        )
    assert spy.calls == []  # never broadcast on refusal


def test_set_weights_refuses_unknown_network():
    sk_hex, _ = _keypair()
    with pytest.raises(mw.UnsafeNetworkError):
        mw.set_weights(COMPOSED, netuid=123, network="weirdnet", signing_key_hex=sk_hex)


def test_removed_mainnet_override_cannot_unlock_sn39(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATHEDRAL_MECH_WEIGHTSET_ALLOW_MAINNET", "true")
    monkeypatch.setenv(mw.LIVE_ENV, "true")
    sk_hex, _ = _keypair()
    spy = _Spy()
    with pytest.raises(mw.UnsafeNetworkError, match="immutable"):
        mw.set_weights(
            COMPOSED,
            netuid=39,
            network="test",
            signing_key_hex=sk_hex,
            confirm=True,
            broadcast_fn=spy,
        )
    assert spy.calls == []


# --- default dry-run ---------------------------------------------------------


def test_testnet_dry_run_returns_signed_artifact(monkeypatch):
    monkeypatch.delenv(mw.LIVE_ENV, raising=False)
    sk_hex, pk_hex = _keypair()
    spy = _Spy()

    out = mw.set_weights(
        COMPOSED,
        netuid=123,
        network="test",
        signing_key_hex=sk_hex,
        broadcast_fn=spy,
    )

    assert out["mode"] == "dry_run"
    assert out["broadcast"] is False
    assert spy.calls == []  # NEVER broadcasts in dry-run

    art = out["artifact"]
    assert art["schema"] == "cathedral.mechanism_weights.v1"
    assert art["testnet"] is True
    assert art["merkle_root"].startswith("sha256:")
    assert art["receipt_id"].startswith("sha256:")
    assert len(art["leaves"]) == 3

    # correct per-uid weights, sorted, preserved from `composed`
    assert out["would_set"] == {2: 0.3, 5: 0.5, 9: 0.2}
    assert [w["uid"] for w in art["weights"]] == [2, 5, 9]

    # signature verifies over canonical bytes with the matching key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pk_hex))
    pk.verify(base64.b64decode(art["signature"]), canonical_bytes(art))


def test_live_env_without_confirm_stays_dry_run(monkeypatch):
    monkeypatch.setenv(mw.LIVE_ENV, "true")
    sk_hex, _ = _keypair()
    spy = _Spy()

    out = mw.set_weights(
        COMPOSED,
        netuid=123,
        network="test",
        signing_key_hex=sk_hex,
        confirm=False,  # missing explicit confirm
        broadcast_fn=spy,
    )
    assert out["mode"] == "dry_run"
    assert out["broadcast"] is False
    assert spy.calls == []


def test_confirm_without_live_env_stays_dry_run(monkeypatch):
    monkeypatch.delenv(mw.LIVE_ENV, raising=False)
    sk_hex, _ = _keypair()
    spy = _Spy()
    out = mw.set_weights(
        COMPOSED,
        netuid=123,
        network="test",
        signing_key_hex=sk_hex,
        confirm=True,
        broadcast_fn=spy,
    )
    assert out["mode"] == "dry_run"
    assert spy.calls == []


# --- fully-gated live broadcast (testnet only) ------------------------------


def test_all_gates_satisfied_still_cannot_invoke_callback(monkeypatch):
    monkeypatch.setenv(mw.LIVE_ENV, "true")
    sk_hex, _ = _keypair()
    spy = _Spy()

    out = mw.set_weights(
        COMPOSED,
        netuid=123,
        network="test",
        signing_key_hex=sk_hex,
        confirm=True,
        broadcast_fn=spy,
    )
    assert out["mode"] == "dry_run"
    assert out["broadcast"] is False
    assert spy.calls == []
    assert "permanently disabled" in out["reason"]


def test_live_without_broadcast_fn_stays_dry_run(monkeypatch):
    monkeypatch.setenv(mw.LIVE_ENV, "true")
    sk_hex, _ = _keypair()
    out = mw.set_weights(
        COMPOSED,
        netuid=123,
        network="test",
        signing_key_hex=sk_hex,
        confirm=True,
        broadcast_fn=None,  # no injected chain path -> cannot go live
    )
    assert out["mode"] == "dry_run"
    assert out["broadcast"] is False


# --- artifact builder determinism / merkle ----------------------------------


def test_build_artifact_deterministic_merkle():
    sk_hex, _ = _keypair()
    a = mw.build_weight_artifact(
        COMPOSED, netuid=123, network="test", signing_key_hex=sk_hex
    )
    b = mw.build_weight_artifact(
        COMPOSED, netuid=123, network="test", signing_key_hex=sk_hex
    )
    # same inputs -> same merkle root / receipt id (signature+id/time may differ)
    assert a["merkle_root"] == b["merkle_root"]
    assert a["receipt_id"] == b["receipt_id"]
    assert a["merkle_root"].startswith("sha256:")


def test_build_artifact_filters_nonpositive_and_nonfinite():
    sk_hex, _ = _keypair()
    composed = {1: 0.5, 2: 0.0, 3: -1.0, 4: float("nan"), 5: 0.5}
    art = mw.build_weight_artifact(
        composed, netuid=123, network="test", signing_key_hex=sk_hex
    )
    assert [w["uid"] for w in art["weights"]] == [1, 5]


# --- read endpoint -----------------------------------------------------------


def test_read_endpoint_serves_latest_stamped_testnet(monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("httpx2")
    from fastapi.testclient import TestClient

    monkeypatch.delenv(mw.LIVE_ENV, raising=False)
    sk_hex, _ = _keypair()

    # reset the in-process holder so the 404 path is exercised cleanly
    monkeypatch.setattr(mw, "_LATEST", None)

    app = fastapi.FastAPI()
    app.include_router(mw.build_router())
    client = TestClient(app)

    # before any build -> 404
    assert client.get("/mechanisms/weights/next").status_code == 404

    mw.set_weights(COMPOSED, netuid=123, network="test", signing_key_hex=sk_hex)
    r = client.get("/mechanisms/weights/next")
    assert r.status_code == 200
    body = r.json()
    assert body["testnet"] is True
    assert body["schema"] == "cathedral.mechanism_weights.v1"
    assert {w["uid"]: w["weight"] for w in body["weights"]} == {2: 0.3, 5: 0.5, 9: 0.2}
