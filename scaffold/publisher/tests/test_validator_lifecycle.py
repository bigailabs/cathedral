from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scaffold import validator_thin, wire_vector
from scaffold.events import _neutralize


@pytest.fixture(autouse=True)
def _isolated_submission_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "submission-runtime"
    )


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _signed_vector(policy_version: int = 42) -> tuple[dict, str]:
    now = datetime.now(UTC)
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes_raw().hex()
    payload = {
        "vector_id": "12345678-lifecycle-test",
        "policy_version": policy_version,
        "network": "finney",
        "netuid": 39,
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(minutes=5)),
        "burn_snapshot": {
            "burn_uid": None,
            "burn_hotkey": "burn-hotkey",
            "forced_burn_percentage": 10.0,
        },
        "policy_metadata": {
            "confidential_primary": {
                "contract_version": "v1",
                "mode": "confidential_primary",
                "source": "cathedral_confidential_tdx",
                "base_mass": 0.0,
                "confidential_mass": 0.0,
                "complete": False,
                "fresh": False,
                "confirmed": False,
            },
            "validated_supply": {
                "contract_version": "v2",
                "intel_tdx_allocation": 0.90,
                "fixed_burn_allocation": 0.10,
                "burn_hotkey": "burn-hotkey",
            },
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


def test_event_redaction_drops_every_url_query_and_fragment() -> None:
    rendered = _neutralize(
        "probe https://alice:p@ss@example.invalid/path"
        "?apikey=SENSITIVE&X-Amz-Signature=ALSO-SENSITIVE#fragment"
    )
    assert rendered == "probe <redacted-url>"
    assert "SENSITIVE" not in rendered
    assert "fragment" not in rendered


def test_startup_never_serializes_malformed_or_protocol_relative_endpoints(
    tmp_path, monkeypatch
) -> None:
    events = tmp_path / "events.jsonl"
    args = SimpleNamespace(
        publisher_url="https://alice:p ass@example.invalid/path?sig=SECRET",
        evidence_url="//bob:token@example.invalid/v1/evidence?key=ALSO_SECRET",
        state_file=str(tmp_path / "state.json"),
        public_key_hex="00" * 32,
        key_id="test-key",
        network="finney",
        netuid=39,
        offline=True,
        broadcast=False,
        wallet_name="validator",
        wallet_hotkey="default",
        require_policy=None,
        provenance="off",
        once=True,
        interval_secs=1,
        jsonl=str(events),
    )
    monkeypatch.setattr(validator_thin, "tick", lambda _args: True)

    assert validator_thin.run(args) == 0
    serialized = events.read_text()
    assert "SECRET" not in serialized
    assert "alice" not in serialized
    assert "bob" not in serialized
    startup = json.loads(serialized)
    assert startup["publisher_url"] == "<invalid-endpoint>"
    assert startup["provenance_evidence_url"] == "<invalid-endpoint>"


def test_attempted_policy_is_a_durable_rollback_high_water(tmp_path) -> None:
    state_file = tmp_path / "fence.json"
    validator_thin.save_fence(state_file, 8, "vector-8")
    validator_thin._write_state_fenced(
        state_file,
        {
            "highest_attempted_policy_version": 10,
            "thin_submission_attempt_id": "sha256:" + "a" * 64,
            "thin_submission_attempt_status": "pending",
            "thin_submission_identity": {"policy_version": 10},
        },
    )
    assert validator_thin.load_fence(state_file) == 10

    payload, public_key = _signed_vector(policy_version=9)
    with pytest.raises(wire_vector.VectorError, match="rollback/replay"):
        validator_thin.accept_vector(
            payload,
            public_key_hex=public_key,
            key_id="cathedral-weight-policy",
            network="finney",
            netuid=39,
            fence_version=validator_thin.load_fence(state_file),
        )


def test_vector_freshness_uses_canonical_utc_and_bounded_lifetime() -> None:
    now = datetime(2026, 7, 25, 2, 0, tzinfo=UTC)
    payload, _public_key = _signed_vector()
    payload["generated_at"] = _iso(now)
    payload["expires_at"] = _iso(now + timedelta(minutes=30))
    wire_vector.invariant_check(
        payload,
        network="finney",
        netuid=39,
        now_iso=_iso(now),
    )

    malformed = dict(payload, generated_at="2026-07-25T02:00:00+00:00")
    with pytest.raises(wire_vector.VectorError, match="canonical UTC"):
        wire_vector.invariant_check(
            malformed,
            network="finney",
            netuid=39,
            now_iso=_iso(now),
        )

    future = dict(payload, generated_at=_iso(now + timedelta(minutes=3)))
    future["expires_at"] = _iso(now + timedelta(minutes=33))
    with pytest.raises(wire_vector.VectorError, match="in the future"):
        wire_vector.invariant_check(
            future,
            network="finney",
            netuid=39,
            now_iso=_iso(now),
        )

    long_lived = dict(payload, expires_at=_iso(now + timedelta(hours=2)))
    with pytest.raises(wire_vector.VectorError, match="lifetime"):
        wire_vector.invariant_check(
            long_lived,
            network="finney",
            netuid=39,
            now_iso=_iso(now),
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
        publisher_url="https://user:secret@api.cathedral.computer?token=secret",  # pragma: allowlist secret
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


def test_finalized_submission_fence_precedes_fallible_telemetry(tmp_path, monkeypatch):
    payload, public_key = _signed_vector()
    submissions = {"count": 0}

    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: payload)
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kwargs: SimpleNamespace(
            hotkey_to_uid={"burn-hotkey": 204},
            block=123,
            validator_uid=30,
            validator_hotkey="validator-hotkey",
            genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
            commit_reveal_enabled=False,
            min_allowed_weights=1,
            max_weight_limit=1.0,
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_continuous_launch_transition",
        lambda _args: None,
    )

    def submit(*_args, **_kwargs):
        submissions["count"] += 1
        return validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "d" * 64,
            block_number=124,
            finalized=True,
        )

    class _FailAfterFinalization:
        def event(self, name, **_fields):
            if name == "WEIGHTS_SUBMITTED":
                raise OSError("simulated log flush failure")

    monkeypatch.setattr(validator_thin, "set_weights_on_chain", submit)
    monkeypatch.setattr(
        validator_thin,
        "_get_events",
        lambda _args: _FailAfterFinalization(),
    )
    state_file = tmp_path / "fence.json"
    args = SimpleNamespace(
        publisher_url="https://api.cathedral.computer",
        state_file=str(state_file),
        public_key_hex=public_key,
        key_id="cathedral-weight-policy",
        network="finney",
        netuid=39,
        offline=False,
        broadcast=True,
        wallet_name="validator",
        wallet_hotkey="default",
        require_policy="validated_supply_v1",
        provenance="off",
        runtime_root=str(validator_thin._VALIDATOR_RUNTIME_ROOT),
    )

    with pytest.raises(OSError, match="log flush"):
        validator_thin.tick(args)
    assert validator_thin.load_fence(state_file) == 42
    assert submissions["count"] == 1

    # The already-applied vector cannot be submitted again even though the
    # first tick's telemetry failed after finalization.
    with pytest.raises(wire_vector.VectorError, match="rollback/replay"):
        validator_thin.tick(args)
    assert submissions["count"] == 1


def test_pending_thin_attempt_blocks_retry_when_final_state_write_fails(
    tmp_path, monkeypatch
):
    payload, public_key = _signed_vector()
    submissions = {"count": 0}
    mapping_block = {"value": 123}

    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: payload)
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kwargs: SimpleNamespace(
            hotkey_to_uid={"burn-hotkey": 204},
            block=mapping_block["value"],
            validator_uid=30,
            validator_hotkey="validator-hotkey",
            genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
            commit_reveal_enabled=False,
            min_allowed_weights=1,
            max_weight_limit=1.0,
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_continuous_launch_transition",
        lambda _args: None,
    )

    def submit(*_args, **_kwargs):
        submissions["count"] += 1
        return validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "d" * 64,
            block_number=124,
            finalized=True,
        )

    monkeypatch.setattr(validator_thin, "set_weights_on_chain", submit)
    real_write_state = validator_thin._write_state

    def fail_finalization(path, updates):
        if updates.get("thin_submission_attempt_status") == "finalized":
            raise OSError("simulated final state fsync failure")
        return real_write_state(path, updates)

    monkeypatch.setattr(validator_thin, "_write_state", fail_finalization)
    state_file = tmp_path / "fence.json"
    args = SimpleNamespace(
        publisher_url="https://api.cathedral.computer",
        state_file=str(state_file),
        public_key_hex=public_key,
        key_id="cathedral-weight-policy",
        network="finney",
        netuid=39,
        offline=False,
        broadcast=True,
        wallet_name="validator",
        wallet_hotkey="default",
        require_policy="validated_supply_v1",
        provenance="off",
        runtime_root=str(validator_thin._VALIDATOR_RUNTIME_ROOT),
    )

    with pytest.raises(OSError, match="state fsync"):
        validator_thin.tick(args)
    assert submissions["count"] == 1
    pending = validator_thin._read_state(state_file)
    assert pending["thin_submission_attempt_status"] == "pending"
    assert pending["thin_submission_attempt_ids"] == [
        pending["thin_submission_attempt_id"]
    ]

    mapping_block["value"] = 124
    with pytest.raises(
        wire_vector.VectorError,
        match="rollback/replay",
    ):
        validator_thin.tick(args)
    assert submissions["count"] == 1
