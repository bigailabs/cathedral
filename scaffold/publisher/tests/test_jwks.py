import base64

import pytest
from fastapi.testclient import TestClient

from scaffold.publisher import rows, weights
from scaffold.publisher.app import build_app
from scaffold.publisher.store import Store


EVAL_KEY_HEX = "11" * 32
WEIGHT_POLICY_KEY_HEX = "22" * 32
JWKS_PATH = "/.well-known/cathedral-jwks.json"


def _get_jwks(monkeypatch, *, weight_key: str | None, weight_kid: str | None = None):
    if weight_key is None:
        monkeypatch.delenv(weights.SIGNING_KEY_ENV, raising=False)
    else:
        monkeypatch.setenv(weights.SIGNING_KEY_ENV, weight_key)
    if weight_kid is None:
        monkeypatch.delenv(weights.KEY_ID_ENV, raising=False)
    else:
        monkeypatch.setenv(weights.KEY_ID_ENV, weight_kid)

    app = build_app(database_path=":memory:", signing_key_hex=EVAL_KEY_HEX)
    response = TestClient(app).get(JWKS_PATH)
    assert response.status_code == 200
    return response.json()


def test_jwks_publishes_eval_and_dedicated_weight_policy_keys(monkeypatch):
    doc = _get_jwks(
        monkeypatch,
        weight_key=WEIGHT_POLICY_KEY_HEX,
        weight_kid="cathedral-weight-policy-2026",
    )

    keys_by_id = {key["kid"]: key for key in doc["keys"]}
    assert list(keys_by_id) == [
        "cathedral-eval-signing",
        "cathedral-weight-policy-2026",
    ]
    assert keys_by_id["cathedral-eval-signing"]["public_key_hex"] == rows.public_key_hex(
        EVAL_KEY_HEX
    )
    assert keys_by_id["cathedral-weight-policy-2026"][
        "public_key_hex"
    ] == rows.public_key_hex(WEIGHT_POLICY_KEY_HEX)


def test_jwks_uses_default_weight_policy_kid(monkeypatch):
    doc = _get_jwks(monkeypatch, weight_key=WEIGHT_POLICY_KEY_HEX)

    assert [key["kid"] for key in doc["keys"]] == [
        "cathedral-eval-signing",
        "cathedral-weight-policy",
    ]


def test_jwks_publishes_weight_policy_alias_for_eval_fallback(monkeypatch):
    doc = _get_jwks(
        monkeypatch,
        weight_key=None,
        weight_kid="fallback-weight-policy",
    )

    public_key_hex = rows.public_key_hex(EVAL_KEY_HEX)
    keys_by_id = {key["kid"]: key for key in doc["keys"]}
    assert doc["issuer"] == "cathedral.computer"
    assert list(keys_by_id) == [
        "cathedral-eval-signing",
        "fallback-weight-policy",
    ]
    assert keys_by_id["cathedral-eval-signing"] == {
        "kid": "cathedral-eval-signing",
        "use": "sig",
        "alg": "EdDSA",
        "kty": "OKP",
        "crv": "Ed25519",
        "x": base64.urlsafe_b64encode(bytes.fromhex(public_key_hex))
        .rstrip(b"=")
        .decode("ascii"),
        "public_key_hex": public_key_hex,
        "purpose": "Cathedral signs every EvalRun projection served from "
                   "/v1/leaderboard/recent. Pin this key in your validator config.",
    }
    assert keys_by_id["fallback-weight-policy"]["public_key_hex"] == public_key_hex


def test_jwks_keeps_same_public_key_under_distinct_key_ids(monkeypatch):
    doc = _get_jwks(monkeypatch, weight_key=EVAL_KEY_HEX)

    assert [key["kid"] for key in doc["keys"]] == [
        "cathedral-eval-signing",
        "cathedral-weight-policy",
    ]
    assert len({key["public_key_hex"] for key in doc["keys"]}) == 1


def test_jwks_omits_exact_duplicate_key_entry(monkeypatch):
    doc = _get_jwks(
        monkeypatch,
        weight_key=EVAL_KEY_HEX,
        weight_kid="cathedral-eval-signing",
    )

    assert len(doc["keys"]) == 1
    assert doc["keys"][0]["kid"] == "cathedral-eval-signing"


@pytest.mark.parametrize("weight_key", [None, WEIGHT_POLICY_KEY_HEX])
def test_jwks_key_verifies_actual_weight_vector_signature(
    tmp_path, monkeypatch, weight_key
):
    doc = _get_jwks(monkeypatch, weight_key=weight_key)
    signing_key = weight_key or EVAL_KEY_HEX
    store = Store(str(tmp_path / "weights.sqlite"), prefer_env_database_url=False)
    vector = weights.build_signed_vector(store, signing_key_hex=signing_key)
    published_key = next(
        key for key in doc["keys"] if key["kid"] == vector["key_id"]
    )

    weights.verify_signature(
        vector,
        public_key_hex=published_key["public_key_hex"],
        expected_key_id=published_key["kid"],
    )


def test_actual_weight_vector_rejects_wrong_published_key(tmp_path, monkeypatch):
    doc = _get_jwks(monkeypatch, weight_key=WEIGHT_POLICY_KEY_HEX)
    store = Store(str(tmp_path / "weights.sqlite"), prefer_env_database_url=False)
    vector = weights.build_signed_vector(
        store,
        signing_key_hex=WEIGHT_POLICY_KEY_HEX,
    )
    eval_key = next(
        key for key in doc["keys"] if key["kid"] == "cathedral-eval-signing"
    )

    with pytest.raises(weights.VectorError, match="ed25519 signature verify failed"):
        weights.verify_signature(
            vector,
            public_key_hex=eval_key["public_key_hex"],
            expected_key_id=vector["key_id"],
        )


def test_malformed_dedicated_weight_key_fails_app_startup(monkeypatch):
    monkeypatch.setenv(weights.SIGNING_KEY_ENV, "not-valid-hex")

    with pytest.raises(
        ValueError,
        match=r"invalid CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY: expected a 32-byte",
    ):
        build_app(database_path=":memory:", signing_key_hex=EVAL_KEY_HEX)
