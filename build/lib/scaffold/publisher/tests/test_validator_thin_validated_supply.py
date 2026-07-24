from __future__ import annotations

from types import SimpleNamespace

import pytest

from scaffold import validator_thin


PIN = validator_thin.REQUIRE_POLICY_VALIDATED_SUPPLY_V1


def payload(*, positive: bool = True, burn_hotkey: str = "burn-hotkey") -> dict:
    mass = 1.0 if positive else 0.0
    rows = (
        [
            {
                "miner_hotkey": "tdx-miner",
                "weight": 1.0,
                "base_component": 0.0,
                "external_component": 1.0,
            }
        ]
        if positive
        else []
    )
    return {
        "weights": rows,
        "burn_snapshot": {
            "burn_uid": None,
            "burn_hotkey": burn_hotkey,
            "forced_burn_percentage": 10.0,
        },
        "policy_metadata": {
            "confidential_primary": {
                "contract_version": "v1",
                "mode": "confidential_primary",
                "source": "cathedral_confidential_tdx",
                "base_mass": 0.0,
                "confidential_mass": mass,
                "complete": positive,
                "fresh": positive,
                "confirmed": True,
            },
            "validated_supply": {
                "contract_version": "v1",
                "intel_tdx_allocation": 0.90,
                "verified_gpu_allocation": 0.10,
                "verified_gpu_admitted": False,
                "burn_hotkey": burn_hotkey,
            },
        },
    }


def test_positive_tdx_receives_90_and_empty_gpu_class_burns_10() -> None:
    result = validator_thin.vector_to_uid_weights(
        payload(), {"burn-hotkey": 0, "tdx-miner": 163}, require_policy=PIN
    )
    assert result == {0: pytest.approx(0.10), 163: pytest.approx(0.90)}


def test_revoked_tdx_moves_full_vector_to_current_burn_uid() -> None:
    first = validator_thin.vector_to_uid_weights(
        payload(positive=False), {"burn-hotkey": 0}, require_policy=PIN
    )
    moved = validator_thin.vector_to_uid_weights(
        payload(positive=False), {"burn-hotkey": 44}, require_policy=PIN
    )
    assert first == {0: 1.0}
    assert moved == {44: 1.0}


def test_missing_or_stale_burn_hotkey_fails_closed() -> None:
    with pytest.raises(validator_thin.wire.VectorError, match="no current metagraph UID"):
        validator_thin.vector_to_uid_weights(
            payload(), {"tdx-miner": 163}, require_policy=PIN
        )


def test_historical_burn_uid_is_rejected() -> None:
    document = payload()
    document["burn_snapshot"]["burn_uid"] = 0
    with pytest.raises(validator_thin.wire.VectorError, match="must not pin a UID"):
        validator_thin.vector_to_uid_weights(
            document, {"burn-hotkey": 0, "tdx-miner": 163}, require_policy=PIN
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("intel_tdx_allocation", 0.89, "Intel TDX allocation"),
        ("verified_gpu_allocation", 0.11, "Verified GPU allocation"),
        ("verified_gpu_admitted", True, "cannot admit Verified GPU"),
    ],
)
def test_policy_drift_fails_closed(field: str, value: object, message: str) -> None:
    document = payload()
    document["policy_metadata"]["validated_supply"][field] = value
    with pytest.raises(validator_thin.wire.VectorError, match=message):
        validator_thin.vector_to_uid_weights(
            document, {"burn-hotkey": 0, "tdx-miner": 163}, require_policy=PIN
        )


def test_burn_hotkey_cannot_also_earn_tdx_weight() -> None:
    document = payload(burn_hotkey="tdx-miner")
    with pytest.raises(validator_thin.wire.VectorError, match="resolves to burn UID"):
        validator_thin.vector_to_uid_weights(
            document, {"tdx-miner": 163}, require_policy=PIN
        )


def test_chain_preflight_resolves_validator_and_requires_permit(monkeypatch) -> None:
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator-hotkey"))
    metagraph = SimpleNamespace(
        uids=[0, 30, 163],
        hotkeys=["burn-hotkey", "validator-hotkey", "tdx-miner"],
        validator_permit=[False, True, False],
        block=8680424,
    )
    subtensor = SimpleNamespace(
        metagraph=lambda _netuid: metagraph,
        min_allowed_weights=lambda **_kwargs: 1,
        max_weight_limit=lambda **_kwargs: 1.0,
    )
    monkeypatch.setattr(
        validator_thin, "_bt_wallet", lambda _bt: lambda **_kwargs: wallet
    )
    monkeypatch.setattr(
        validator_thin, "_bt_subtensor", lambda _bt: lambda **_kwargs: subtensor
    )

    result = validator_thin.chain_preflight(
        network="finney", netuid=39, wallet_name="cathedral", wallet_hotkey="default"
    )
    assert result.validator_uid == 30
    assert result.hotkey_to_uid["tdx-miner"] == 163
    assert result.block == 8680424
    assert result.min_allowed_weights == 1
    assert result.max_weight_limit == 1.0

    metagraph.validator_permit[1] = False
    with pytest.raises(validator_thin.wire.VectorError, match="lacks validator permit"):
        validator_thin.chain_preflight(
            network="finney",
            netuid=39,
            wallet_name="cathedral",
            wallet_hotkey="default",
        )


def test_chain_submission_uses_preflight_snapshot_and_waits_for_finality() -> None:
    calls = []

    def set_weights(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            extrinsic_receipt=SimpleNamespace(
                extrinsic_hash="0xabc", block_hash="0xdef", block_number=8680430
            ),
        )

    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(set_weights=set_weights),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
    )
    assert validator_thin.set_weights_on_chain(
        {0: 0.1, 163: 0.9},
        network="finney",
        netuid=39,
        wallet_name="cathedral",
        wallet_hotkey="default",
        broadcast=True,
        preflight=preflight,
    )
    assert calls == [
        {
            "wallet": preflight.wallet,
            "netuid": 39,
            "uids": [0, 163],
            "weights": [0.1, 0.9],
            "wait_for_inclusion": True,
            "wait_for_finalization": True,
        }
    ]
