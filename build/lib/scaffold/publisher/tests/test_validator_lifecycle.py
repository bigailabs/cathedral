from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold import validator_thin, wire_vector


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _signed_vector() -> tuple[dict, str]:
    now = datetime.now(timezone.utc)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw().hex()
    payload = {
        "vector_id": "12345678-lifecycle-test",
        "policy_version": 42,
        "network": "finney",
        "netuid": 39,
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=5)),
        "burn_snapshot": {
            "burn_uid": None,
            "burn_hotkey": "burn-hotkey",
            "forced_burn_percentage": 100.0,
        },
        "policy_hash": "sha256:test",
        "key_id": "cathedral-weight-policy",
        "weights": [],
    }
    payload["signature"] = base64.b64encode(
        private_key.sign(wire_vector.canonical_bytes(payload))
    ).decode()
    return payload, public_key


def test_feed_label_never_logs_credentials_query_or_fragment():
    assert (
        validator_thin._feed_label(
            "https://user:secret@api.cathedral.computer:8443/path?token=secret#fragment"
        )
        == "https://api.cathedral.computer:8443"
    )


def test_tick_emits_sanitized_verdict_and_mapping_lifecycle(
    tmp_path, monkeypatch, capsys
):
    payload, public_key = _signed_vector()
    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: payload)
    monkeypatch.setattr(
        validator_thin,
        "set_weights_on_chain",
        lambda *_args, **_kwargs: True,
    )
    args = SimpleNamespace(
        publisher_url="https://user:secret@api.cathedral.computer?token=secret",
        state_file=str(tmp_path / "fence.json"),
        public_key_hex=public_key,
        key_id="cathedral-weight-policy",
        network="finney",
        netuid=39,
        offline=True,
        broadcast=False,
        wallet_name="unused",
        wallet_hotkey="unused",
        require_policy=None,
    )

    assert validator_thin.tick(args)

    output = capsys.readouterr().out
    assert "FEED fetch source=https://api.cathedral.computer" in output
    assert "FEED fetched id=12345678 policy_version=42" in output
    assert "SIGNATURE valid key_id=cathedral-weight-policy" in output
    assert "FRESHNESS valid network=finney netuid=39" in output
    assert "ROLLBACK valid policy_version=42 prior_fence=-1" in output
    assert (
        "MAP complete uids=1 burn_uid=0 burn_share=1.000000 vector=0:1.000000" in output
    )
    assert "secret" not in output
