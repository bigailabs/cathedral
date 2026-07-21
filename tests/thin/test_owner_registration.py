import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from bittensor_wallet import Keypair
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral_thin.core import ThinSubnetError
from cathedral_thin.contributor_cli import (
    _key_assignments,
    build_registration_body,
    registration_chain_preflight,
)
from cathedral_thin.score_classes import (
    AssignmentPolicy,
    ExternalClassPolicy,
    OwnerRegistrationPolicy,
    RegistrationCheckpoint,
    canonical_json,
    load_best_owner_registration,
    load_score_policy,
    materialize_registered_policy,
    sign_owner_registration,
    verify_owner_registration,
)


NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


def owner_key() -> Keypair:
    return Keypair.create_from_mnemonic(Keypair.generate_mnemonic())


def report_key() -> tuple[bytes, str]:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return raw, base64.b64encode(raw).decode("ascii")


def registered_policy(*, registration_locations=("registration",)):
    return ExternalClassPolicy(
        class_id="confidential_compute",
        allocation=Decimal(1),
        source_id="testnet_owner_source",
        locations=("https://reports.example/score-classes/confidential-compute.json",),
        trusted_keys={},
        max_age_seconds=600,
        max_future_seconds=30,
        max_block_span=100,
        require_evidence=True,
        assignment=AssignmentPolicy("metric", "verified_work_units", "linear", None),
        owner_registration=OwnerRegistrationPolicy(
            source_netuid=7,
            locations=registration_locations,
            max_age_seconds=86_400,
            max_future_seconds=30,
            max_block_span=10_000,
            require_target_registration=True,
        ),
    )


def registration_body(
    owner: Keypair,
    delegate: Keypair,
    public_b64: str,
    *,
    sequence=3,
    previous=None,
):
    return {
        "schema": "cathedral_owner_score_registration_v1",
        "network": "test",
        "source_netuid": 7,
        "target_netuid": 39,
        "owner_coldkey": owner.ss58_address,
        "delegate_hotkey": delegate.ss58_address,
        "source_id": "testnet_owner_source",
        "class_ids": ["confidential_compute"],
        "report_locations": [
            "https://reports.example/score-classes/confidential-compute.json"
        ],
        "report_keys": {"report-key-1": public_b64},
        "sequence": sequence,
        "previous_registration_id": previous,
        "issued_at": "2026-07-18T12:00:00.000000Z",
        "expires_at": "2026-07-19T12:00:00.000000Z",
        "valid_from_block": 1000,
        "valid_until_block": 2000,
    }


def verify(raw, policy, owner, delegate, checkpoint=None):
    return verify_owner_registration(
        raw,
        policy,
        network="test",
        netuid=39,
        current_block=1200,
        current_owner_coldkey=owner.ss58_address,
        registered_hotkeys={delegate.ss58_address: owner.ss58_address},
        checkpoint=checkpoint,
        now=NOW,
    )


def test_owner_registration_delegates_report_transport_but_not_assignment():
    owner = owner_key()
    delegate = owner_key()
    public_raw, public_b64 = report_key()
    policy = registered_policy()
    registration, checkpoint = verify(
        sign_owner_registration(registration_body(owner, delegate, public_b64), owner),
        policy,
        owner,
        delegate,
    )
    materialized = materialize_registered_policy(policy, registration)

    assert checkpoint == RegistrationCheckpoint(
        owner.ss58_address,
        delegate.ss58_address,
        3,
        registration.registration_id,
    )
    assert materialized.locations == registration.report_locations
    assert materialized.trusted_keys == {"report-key-1": public_raw}
    assert materialized.allocation == Decimal(1)
    assert materialized.assignment.metric == "verified_work_units"


def test_registration_requires_current_owner_and_live_target_registration():
    owner = owner_key()
    other_owner = owner_key()
    delegate = owner_key()
    _, public_b64 = report_key()
    policy = registered_policy()
    raw = sign_owner_registration(registration_body(owner, delegate, public_b64), owner)

    with pytest.raises(ThinSubnetError, match="current source subnet owner"):
        verify_owner_registration(
            raw,
            policy,
            network="test",
            netuid=39,
            current_block=1200,
            current_owner_coldkey=other_owner.ss58_address,
            registered_hotkeys={delegate.ss58_address: owner.ss58_address},
            now=NOW,
        )
    with pytest.raises(ThinSubnetError, match="not currently registered"):
        verify_owner_registration(
            raw,
            policy,
            network="test",
            netuid=39,
            current_block=1200,
            current_owner_coldkey=owner.ss58_address,
            registered_hotkeys={},
            now=NOW,
        )

    unapproved = registration_body(owner, delegate, public_b64)
    unapproved["report_locations"] = ["https://127.0.0.1/internal-admin"]
    with pytest.raises(ThinSubnetError, match="do not match validator policy"):
        verify(
            sign_owner_registration(unapproved, owner),
            policy,
            owner,
            delegate,
        )
    with pytest.raises(ThinSubnetError, match="not currently registered"):
        verify_owner_registration(
            raw,
            policy,
            network="test",
            netuid=39,
            current_block=1200,
            current_owner_coldkey=owner.ss58_address,
            registered_hotkeys={delegate.ss58_address: other_owner.ss58_address},
            now=NOW,
        )


def test_registration_tamper_replay_equivocation_and_owner_transfer_fail_safe():
    owner = owner_key()
    delegate = owner_key()
    _, public_b64 = report_key()
    policy = registered_policy()
    raw = sign_owner_registration(registration_body(owner, delegate, public_b64), owner)
    registration, checkpoint = verify(raw, policy, owner, delegate)

    tampered = json.loads(raw)
    tampered["source_id"] = "attacker"
    with pytest.raises(ThinSubnetError, match="source|id"):
        verify(canonical_json(tampered), policy, owner, delegate)

    older = sign_owner_registration(
        registration_body(owner, delegate, public_b64, sequence=2), owner
    )
    with pytest.raises(ThinSubnetError, match="rolled back"):
        verify(older, policy, owner, delegate, checkpoint)

    changed = registration_body(owner, delegate, public_b64)
    _, replacement_public_b64 = report_key()
    changed["report_keys"] = {"replacement-key": replacement_public_b64}
    equivocation = sign_owner_registration(changed, owner)
    with pytest.raises(ThinSubnetError, match="equivocated"):
        verify(equivocation, policy, owner, delegate, checkpoint)

    next_raw = sign_owner_registration(
        registration_body(
            owner,
            delegate,
            public_b64,
            sequence=4,
            previous="sha256:" + "11" * 32,
        ),
        owner,
    )
    with pytest.raises(ThinSubnetError, match="does not extend"):
        verify(next_raw, policy, owner, delegate, checkpoint)

    # A chain-confirmed ownership transfer is a new trust root. The former
    # owner's manifest fails, while the new owner may start a fresh sequence.
    new_owner = owner_key()
    new_delegate = owner_key()
    new_raw = sign_owner_registration(
        registration_body(new_owner, new_delegate, public_b64, sequence=0),
        new_owner,
    )
    transferred, transferred_checkpoint = verify(
        new_raw, policy, new_owner, new_delegate, checkpoint
    )
    assert transferred.owner_coldkey == new_owner.ss58_address
    assert transferred_checkpoint.owner_coldkey == new_owner.ss58_address
    assert transferred.registration_id != registration.registration_id


def test_registered_policy_is_canonical_and_has_no_validator_pinned_report_key(
    tmp_path,
):
    document = {
        "schema": "cathedral_score_policy_v1",
        "network": "test",
        "netuid": 39,
        "classes": [
            {
                "allocation": "1",
                "assignment": {
                    "cap": "100",
                    "metric": "verified_work_units",
                    "mode": "metric",
                    "required_evidence_kinds": ["receipt"],
                    "required_reason_codes": ["receipt_verified"],
                    "transform": "linear",
                },
                "class_id": "confidential_compute",
                "kind": "external",
                "locations": [
                    "https://reports.example/score-classes/confidential-compute.json"
                ],
                "max_age_seconds": 600,
                "max_block_span": 100,
                "max_future_seconds": 30,
                "owner_registration": {
                    "locations": ["/var/lib/cathedral/owner-registration.json"],
                    "max_age_seconds": 86400,
                    "max_block_span": 10000,
                    "max_future_seconds": 30,
                    "require_target_registration": True,
                    "source_netuid": 7,
                },
                "require_evidence": True,
                "source_id": "testnet_owner_source",
            }
        ],
    }
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_json(document))
    parsed = load_score_policy(path, network="test", netuid=39)
    external = parsed.external_classes[0]
    assert external.locations == (
        "https://reports.example/score-classes/confidential-compute.json",
    )
    assert external.trusted_keys == {}
    assert external.owner_registration.source_netuid == 7

    document["classes"][0]["owner_registration"]["require_target_registration"] = False
    path.write_bytes(canonical_json(document))
    with pytest.raises(ThinSubnetError, match="must be true"):
        load_score_policy(path, network="test", netuid=39)


def test_registration_mirrors_reject_same_sequence_disagreement(monkeypatch):
    owner = owner_key()
    delegate = owner_key()
    _, public_b64 = report_key()
    policy = registered_policy(registration_locations=("one", "two"))
    first = sign_owner_registration(
        registration_body(owner, delegate, public_b64), owner
    )
    changed_body = registration_body(owner, delegate, public_b64)
    _, replacement_public_b64 = report_key()
    changed_body["report_keys"] = {"replacement-key": replacement_public_b64}
    second = sign_owner_registration(changed_body, owner)
    payloads = {"one": first, "two": second}
    monkeypatch.setattr(
        "cathedral_thin.score_classes.fetch_report", lambda location: payloads[location]
    )
    with pytest.raises(ThinSubnetError, match="same-sequence equivocation"):
        load_best_owner_registration(
            policy,
            network="test",
            netuid=39,
            current_block=1200,
            current_owner_coldkey=owner.ss58_address,
            registered_hotkeys={delegate.ss58_address: owner.ss58_address},
            checkpoint=None,
            now=NOW,
        )


def test_registration_expiry_and_block_window_hold():
    owner = owner_key()
    delegate = owner_key()
    _, public_b64 = report_key()
    policy = registered_policy()
    raw = sign_owner_registration(registration_body(owner, delegate, public_b64), owner)
    with pytest.raises(ThinSubnetError, match="expired|stale"):
        verify_owner_registration(
            raw,
            policy,
            network="test",
            netuid=39,
            current_block=1200,
            current_owner_coldkey=owner.ss58_address,
            registered_hotkeys={delegate.ss58_address: owner.ss58_address},
            now=NOW + timedelta(days=2),
        )
    with pytest.raises(ThinSubnetError, match="block window"):
        verify_owner_registration(
            raw,
            policy,
            network="test",
            netuid=39,
            current_block=2000,
            current_owner_coldkey=owner.ss58_address,
            registered_hotkeys={delegate.ss58_address: owner.ss58_address},
            now=NOW,
        )


def test_contributor_cli_builds_bounded_body_and_checks_both_chain_roles():
    owner = owner_key()
    delegate = owner_key()
    _, public_b64 = report_key()

    class Chain:
        def subnet(self, netuid, *, block):
            assert (netuid, block) == (7, 1200)
            return SimpleNamespace(owner_coldkey=owner.ss58_address)

        def metagraph(self, netuid, *, lite):
            assert (netuid, lite) == (39, True)
            return SimpleNamespace(
                hotkeys=[delegate.ss58_address], coldkeys=[owner.ss58_address]
            )

    registered = registration_chain_preflight(
        subtensor=Chain(),
        source_netuid=7,
        target_netuid=39,
        owner_coldkey=owner.ss58_address,
        delegate_hotkey=delegate.ss58_address,
        block=1200,
    )
    keys = _key_assignments([f"report-key-1={public_b64}"])
    body = build_registration_body(
        network="test",
        source_netuid=7,
        target_netuid=39,
        owner_coldkey=owner.ss58_address,
        delegate_hotkey=delegate.ss58_address,
        source_id="testnet_owner_source",
        class_ids=["confidential_compute"],
        report_locations=["https://reports.example/latest.json"],
        report_keys=keys,
        sequence=0,
        previous_registration_id=None,
        block=1200,
        valid_blocks=100,
        issued_at=NOW,
        valid_seconds=600,
    )
    assert registered == {delegate.ss58_address: owner.ss58_address}
    assert body["valid_until_block"] == 1300
    assert body["expires_at"] == "2026-07-18T12:10:00.000000Z"

    with pytest.raises(ThinSubnetError, match="not the current"):
        registration_chain_preflight(
            subtensor=Chain(),
            source_netuid=7,
            target_netuid=39,
            owner_coldkey=owner_key().ss58_address,
            delegate_hotkey=delegate.ss58_address,
            block=1200,
        )
