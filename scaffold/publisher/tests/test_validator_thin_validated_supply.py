from __future__ import annotations

import hashlib
import json
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scaffold import validator_thin


PIN = validator_thin.REQUIRE_POLICY_VALIDATED_SUPPLY_V1


@pytest.fixture(autouse=True)
def _isolated_submission_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "submission-runtime"
    )


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
                "contract_version": "v2",
                "intel_tdx_allocation": 0.90,
                "fixed_burn_allocation": 0.10,
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
    with pytest.raises(
        validator_thin.wire.VectorError, match="no current metagraph UID"
    ):
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
        ("fixed_burn_allocation", 0.11, "fixed burn allocation"),
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
    finalized_hash = "0x" + "f" * 64
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator-hotkey"))
    query_values = {
        "MinNonImmuneUids": 10,
        "SubnetOwner": "owner-coldkey",
        "OwnedHotkeys": ["burn-hotkey"],
        "ImmuneOwnerUidsLimit": 1,
    }
    metagraph = SimpleNamespace(
        uids=[0, 30, 163],
        hotkeys=["burn-hotkey", "validator-hotkey", "tdx-miner"],
        validator_permit=[False, True, False],
        block=8680424,
        max_uids=256,
        hparams=SimpleNamespace(max_regs_per_block=2, immunity_period=15000),
        block_at_registration=[8600000, 8600000, 8680400],
    )
    subtensor = SimpleNamespace(
        substrate=SimpleNamespace(
            get_chain_finalised_head=lambda: finalized_hash,
            get_block_number=lambda value: 8680424 if value == finalized_hash else 0,
            get_block_hash=lambda block: (
                finalized_hash if block == 8680424 else "0x" + "0" * 64
            ),
        ),
        metagraph=lambda _netuid, block: (
            metagraph
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        min_allowed_weights=lambda **_kwargs: 1,
        max_weight_limit=lambda **_kwargs: 1.0,
        commit_reveal_enabled=lambda **_kwargs: False,
        get_subnet_owner_hotkey=lambda _netuid, block: (
            "burn-hotkey"
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        blocks_until_next_epoch=lambda _netuid, block: (
            200
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        get_next_epoch_start_block=lambda _netuid, block: (
            8680624
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        weights_rate_limit=lambda _netuid, block: (
            100
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        blocks_since_last_update=lambda _netuid, _uid, block: (
            120
            if block == 8680424
            else (_ for _ in ()).throw(AssertionError("wrong finalized block"))
        ),
        query_subtensor=lambda **kwargs: query_values[kwargs["name"]],
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
    assert result.commit_reveal_enabled is False
    assert result.subnet_owner_hotkey == "burn-hotkey"
    assert result.blocks_until_next_epoch == 200
    assert result.next_epoch_start_block == 8680624
    assert result.weights_rate_limit == 100
    assert result.validator_blocks_since_last_update == 120
    assert result.subnet_free_uid_slots == 253
    assert result.uid_mapping_stable_until_block == 8680550
    assert result.replacement_safe_hotkeys == frozenset(metagraph.hotkeys)
    assert result.subnet_owner_coldkey == "owner-coldkey"
    assert result.subnet_immune_owner_uids_limit == 1
    assert result.subnet_owner_immortal_hotkeys == frozenset({"burn-hotkey"})

    metagraph.validator_permit[1] = False
    with pytest.raises(validator_thin.wire.VectorError, match="lacks validator permit"):
        validator_thin.chain_preflight(
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
        )


def test_locked_sdk_epoch_countdown_has_no_inclusive_plus_one() -> None:
    from bittensor.core.subtensor import Subtensor

    probe = SimpleNamespace(
        block=999,
        get_next_epoch_start_block=lambda _netuid, *, block: (
            1100
            if block == 900
            else (_ for _ in ()).throw(AssertionError("wrong reference block"))
        ),
    )
    assert Subtensor.blocks_until_next_epoch(probe, 39, block=900) == 200


def test_full_subnet_uses_runtime_immunity_buffer_for_uid_stability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_hash = "0x" + "f" * 64
    genesis_hash = "0x" + "0" * 64
    owner = "owner-hotkey"
    validator = "validator-hotkey"
    worker = "recent-worker-hotkey"
    uids = [7, 30, 241, *range(40, 57)]
    hotkeys = [owner, validator, worker, *[f"other-{i}" for i in range(17)]]
    registration_blocks = [0, 0, 999, *([0] * 17)]
    owner_owned_hotkeys = [owner]
    owner_immune_limit = [1]
    query_values = {
        "MinNonImmuneUids": lambda: 10,
        "SubnetOwner": lambda: "owner-coldkey",
        "OwnedHotkeys": lambda: list(owner_owned_hotkeys),
        "ImmuneOwnerUidsLimit": lambda: owner_immune_limit[0],
    }
    metagraph = SimpleNamespace(
        uids=uids,
        hotkeys=hotkeys,
        validator_permit=[False, True, *([False] * 18)],
        block=1000,
        max_uids=20,
        hparams=SimpleNamespace(max_regs_per_block=1, immunity_period=100),
        block_at_registration=registration_blocks,
    )
    subtensor = SimpleNamespace(
        substrate=SimpleNamespace(
            get_chain_finalised_head=lambda: finalized_hash,
            get_block_number=lambda value: 1000 if value == finalized_hash else 0,
            get_block_hash=lambda block: (
                finalized_hash if block == 1000 else genesis_hash
            ),
        ),
        metagraph=lambda _netuid, block: metagraph,
        min_allowed_weights=lambda **_kwargs: 1,
        max_weight_limit=lambda **_kwargs: 1.0,
        commit_reveal_enabled=lambda **_kwargs: False,
        get_subnet_owner_hotkey=lambda _netuid, block: owner,
        blocks_until_next_epoch=lambda _netuid, block: 200,
        get_next_epoch_start_block=lambda _netuid, block: 1200,
        weights_rate_limit=lambda _netuid, block: 100,
        blocks_since_last_update=lambda _netuid, _uid, block: 101,
        query_subtensor=lambda **kwargs: query_values[kwargs["name"]](),
    )
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address=validator))
    monkeypatch.setattr(
        validator_thin, "_bt_wallet", lambda _bt: lambda **_kwargs: wallet
    )
    monkeypatch.setattr(
        validator_thin, "_bt_subtensor", lambda _bt: lambda **_kwargs: subtensor
    )

    preflight = validator_thin.chain_preflight(
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
    )
    assert preflight.subnet_free_uid_slots == 0
    assert preflight.subnet_temporally_immune_uids == 1
    assert preflight.replacement_safe_hotkeys == frozenset({owner, worker})
    assert preflight.subnet_owner_immortal_hotkeys == frozenset({owner})

    owner_owned_hotkeys[:] = [
        owner,
        validator,
        "other-0",
        "other-1",
        "other-2",
    ]
    owner_immune_limit[0] = 5
    owner_crowded = validator_thin.chain_preflight(
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
    )
    assert worker not in owner_crowded.replacement_safe_hotkeys
    assert owner_crowded.subnet_owner_immortal_hotkeys == frozenset(owner_owned_hotkeys)

    owner_owned_hotkeys[:] = [owner]
    owner_immune_limit[0] = 1
    registration_blocks[2] = 0
    expired = validator_thin.chain_preflight(
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
    )
    assert expired.replacement_safe_hotkeys == frozenset({owner})


def test_chain_preflight_rejects_best_head_mapping_newer_than_finalized(
    monkeypatch,
) -> None:
    finalized_hash = "0x" + "f" * 64
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator-hotkey"))
    metagraph = SimpleNamespace(
        uids=[30],
        hotkeys=["validator-hotkey"],
        validator_permit=[True],
        block=101,
    )
    subtensor = SimpleNamespace(
        substrate=SimpleNamespace(
            get_chain_finalised_head=lambda: finalized_hash,
            get_block_number=lambda _value: 100,
            get_block_hash=lambda _block: finalized_hash,
        ),
        metagraph=lambda _netuid, block: metagraph,
        min_allowed_weights=lambda **_kwargs: 1,
        max_weight_limit=lambda **_kwargs: 1.0,
        commit_reveal_enabled=lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        validator_thin, "_bt_wallet", lambda _bt: lambda **_kwargs: wallet
    )
    monkeypatch.setattr(
        validator_thin, "_bt_subtensor", lambda _bt: lambda **_kwargs: subtensor
    )
    with pytest.raises(validator_thin.wire.VectorError, match="finalized chain head"):
        validator_thin.chain_preflight(
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
        )


def test_offline_chain_submission_skips_preflight(monkeypatch) -> None:
    events: list[tuple[str, str]] = []

    def forbidden_preflight(**_kwargs):
        raise AssertionError("offline dry-run must not initialize a chain client")

    monkeypatch.setattr(validator_thin, "chain_preflight", forbidden_preflight)
    monkeypatch.setattr(
        validator_thin,
        "_lifecycle",
        lambda event, details: events.append((event, details)),
    )

    result = validator_thin.set_weights_on_chain(
        {0: 0.1, 163: 0.9},
        network="finney",
        netuid=39,
        wallet_name="unused",
        wallet_hotkey="unused",
        broadcast=False,
        preflight=None,
    )

    assert result.success is True
    assert len(events) == 1
    assert events[0][0] == "WEIGHTS dry-run"
    assert "wire_uids=[0, 163]" in events[0][1]


def test_chain_submission_uses_preflight_snapshot_and_waits_for_finality() -> None:
    calls = []
    extrinsic_hash = "0x" + "a" * 64
    receipt_block_hash = "0x" + "d" * 64
    finalized_head_hash = "0x" + "e" * 64

    def set_weights(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            success=True,
            extrinsic_receipt=SimpleNamespace(
                extrinsic_hash=extrinsic_hash,
                block_hash=receipt_block_hash,
                block_number=8680430,
                is_success=True,
            ),
        )

    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_head_hash,
        get_block_number=lambda block_hash: (
            8680432 if block_hash == finalized_head_hash else 0
        ),
        get_block_hash=lambda block_number: (
            receipt_block_hash
            if block_number == 8680430
            else finalized_head_hash
            if block_number == 8680432
            else "0x" + "0" * 64
        ),
        get_block=lambda **_kwargs: {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator-hotkey",
                        "call": {
                            "call_module": "SubtensorModule",
                            "call_function": "set_mechanism_weights",
                            "call_args": [
                                {"name": "netuid", "value": 38},
                                {"name": "mecid", "value": 0},
                                {
                                    "name": "version_key",
                                    "value": validator_thin._weight_version_key(),
                                },
                                {"name": "dests", "value": [0, 163]},
                                {"name": "weights", "value": [7282, 65535]},
                            ],
                        },
                    }
                )
            ]
        },
        retrieve_extrinsic_by_hash=lambda *_args: SimpleNamespace(
            is_success=True,
            error_message=None,
            extrinsic_idx=0,
        ),
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(
            set_weights=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("generic set_weights must never be called")
            ),
            substrate=substrate,
        ),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "bittensor.core.extrinsics.weights.set_weights_extrinsic",
            set_weights,
        )
        assert validator_thin.set_weights_on_chain(
            {0: 0.1, 163: 0.9},
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
        )
    assert calls == [
        {
            "subtensor": preflight.subtensor,
            "wallet": preflight.wallet,
            "netuid": 38,
            "mechid": 0,
            "uids": [0, 163],
            "weights": [0.1, 0.9],
            "version_key": validator_thin._weight_version_key(),
            "mev_protection": False,
            "period": 128,
            "raise_error": True,
            "wait_for_inclusion": True,
            "wait_for_finalization": True,
            "wait_for_revealed_execution": False,
        }
    ]


def test_direct_sn39_chain_submission_requires_state_machine_authorization(
    monkeypatch,
) -> None:
    called = []
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "burn-hotkey": 204,
            "validator-hotkey": 30,
            "tdx-miner": 163,
        },
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.weights.set_weights_extrinsic",
        lambda **kwargs: called.append(kwargs),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="authorized validator runtime",
    ):
        validator_thin.set_weights_on_chain(
            {163: 0.9, 204: 0.1},
            network="finney",
            netuid=39,
            wallet_name="validator",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
            uid_hotkeys={163: "tdx-miner", 204: "burn-hotkey"},
        )
    assert called == []


def test_finalized_receipt_rejects_inclusion_block_uid_reassignment() -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "d" * 64
    finalized_hash = "0x" + "e" * 64
    call = {
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "call_args": [
            {"name": "netuid", "value": 39},
            {"name": "mecid", "value": 0},
            {"name": "version_key", "value": validator_thin._weight_version_key()},
            {"name": "dests", "value": [163, 204]},
            {"name": "weights", "value": [65535, 7282]},
        ],
    }
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _value: 902,
        get_block_hash=lambda _value: block_hash,
        get_block=lambda **_kw: {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator-hotkey",
                        "call": call,
                    }
                )
            ]
        },
    )
    metagraph = SimpleNamespace(
        uids=[30, 163, 204],
        hotkeys=["validator-hotkey", "attacker-hotkey", "burn-hotkey"],
        block=901,
    )
    subtensor = SimpleNamespace(
        substrate=substrate,
        metagraph=lambda _netuid, block: metagraph,
    )
    assert (
        validator_thin._prove_finalized_receipt(
            subtensor,
            receipt=SimpleNamespace(is_success=True),
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=901,
            validator_hotkey="validator-hotkey",
            netuid=38,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[163, 204],
            wire_weights=[65535, 7282],
            uid_hotkeys={163: "tdx-miner", 204: "burn-hotkey"},
        )
        is False
    )


def test_archive_rpc_fault_is_not_a_positive_receipt_mismatch() -> None:
    subtensor = SimpleNamespace(
        substrate=SimpleNamespace(
            get_chain_finalised_head=lambda: (_ for _ in ()).throw(
                ConnectionError("archive unavailable")
            )
        )
    )
    assert (
        validator_thin._classify_finalized_receipt(
            subtensor,
            receipt=None,
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "b" * 64,
            block_number=901,
            validator_hotkey="validator",
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
            require_receipt=False,
        )
        == validator_thin.NOT_PROVEN
    )


def test_receipt_is_not_proven_when_finalized_head_fails_reverse_check() -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "b" * 64
    finalized_hash = "0x" + "f" * 64
    conflicting_finalized_hash = "0x" + "e" * 64
    call = {
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "call_args": [
            {"name": "netuid", "value": 39},
            {"name": "mecid", "value": 0},
            {
                "name": "version_key",
                "value": validator_thin._weight_version_key(),
            },
            {"name": "dests", "value": [7, 241]},
            {"name": "weights", "value": [65535, 7282]},
        ],
    }
    requested_heights: list[int] = []

    def block_hash_at(number: int) -> str:
        requested_heights.append(number)
        if number == 901:
            return block_hash
        assert number == 902
        return conflicting_finalized_hash

    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _hash: 902,
        get_block_hash=block_hash_at,
        get_block=lambda **_kwargs: {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator",
                        "call": call,
                    }
                )
            ]
        },
        retrieve_extrinsic_by_hash=lambda *_args: SimpleNamespace(
            is_success=True,
            error_message=None,
            extrinsic_idx=0,
        ),
    )
    assert (
        validator_thin._classify_finalized_receipt(
            SimpleNamespace(substrate=substrate),
            receipt=SimpleNamespace(is_success=True, error_message=None),
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=901,
            validator_hotkey="validator",
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
        )
        == validator_thin.NOT_PROVEN
    )
    assert requested_heights == [902, 901]


def test_incomplete_archive_block_is_not_a_positive_receipt_mismatch() -> None:
    block_hash = "0x" + "b" * 64
    finalized_hash = "0x" + "f" * 64
    subtensor = SimpleNamespace(
        substrate=SimpleNamespace(
            get_chain_finalised_head=lambda: finalized_hash,
            get_block_number=lambda _hash: 902,
            get_block_hash=lambda number: (
                finalized_hash if number == 902 else block_hash
            ),
            get_block=lambda **_kwargs: {"extrinsics": []},
        )
    )
    assert (
        validator_thin._classify_finalized_receipt(
            subtensor,
            receipt=SimpleNamespace(is_success=True),
            extrinsic_hash="0x" + "a" * 64,
            block_hash=block_hash,
            block_number=901,
            validator_hotkey="validator",
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
        )
        == validator_thin.NOT_PROVEN
    )


@pytest.mark.parametrize(
    (
        "is_success",
        "error_message",
        "extrinsic_idx",
        "require_receipt",
        "expected",
    ),
    [
        (True, None, 0, False, validator_thin.PASS),
        (False, "SettingWeightsTooFast", 0, False, validator_thin.FAIL),
        (True, None, 1, False, validator_thin.FAIL),
        (False, "SettingWeightsTooFast", 0, True, validator_thin.FAIL),
        (True, None, 1, True, validator_thin.FAIL),
    ],
)
def test_receipt_proves_independent_historical_execution_result(
    is_success: bool,
    error_message: str | None,
    extrinsic_idx: int,
    require_receipt: bool,
    expected: str,
) -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "b" * 64
    finalized_hash = "0x" + "f" * 64
    call = {
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "call_args": [
            {"name": "netuid", "value": 39},
            {"name": "mecid", "value": 0},
            {
                "name": "version_key",
                "value": validator_thin._weight_version_key(),
            },
            {"name": "dests", "value": [7, 241]},
            {"name": "weights", "value": [65535, 7282]},
        ],
    }
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _hash: 902,
        get_block_hash=lambda number: finalized_hash if number == 902 else block_hash,
        get_block=lambda **_kwargs: {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator",
                        "call": call,
                    }
                )
            ]
        },
        retrieve_extrinsic_by_hash=lambda *_args: SimpleNamespace(
            is_success=is_success,
            error_message=error_message,
            extrinsic_idx=extrinsic_idx,
        ),
    )
    assert (
        validator_thin._classify_finalized_receipt(
            SimpleNamespace(substrate=substrate),
            receipt=(
                SimpleNamespace(is_success=True, error_message=None)
                if require_receipt
                else None
            ),
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=901,
            validator_hotkey="validator",
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
            require_receipt=require_receipt,
        )
        == expected
    )


def test_restart_locates_one_exact_noncontiguous_uid_call_without_writing() -> None:
    extrinsic_hash = "0x" + "a" * 64
    finalized_hash = "0x" + "f" * 64
    hashes = {number: f"0x{number:064x}" for number in range(900, 904)}
    hashes[904] = finalized_hash
    by_hash = {value: number for number, value in hashes.items()}
    exact_call = {
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "call_args": [
            {"name": "netuid", "value": 39},
            {"name": "mecid", "value": 0},
            {
                "name": "version_key",
                "value": validator_thin._weight_version_key(),
            },
            {"name": "dests", "value": [7, 241]},
            {"name": "weights", "value": [65535, 7282]},
        ],
    }

    def block(*, block_hash: str) -> dict[str, object]:
        if by_hash[block_hash] != 901:
            return {"extrinsics": []}
        return {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": "0x" + "b" * 64,
                        "address": "validator",
                        "call": exact_call,
                    }
                ),
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator",
                        "call": exact_call,
                    }
                ),
            ]
        }

    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _hash: 904,
        get_block_hash=lambda number: hashes[number],
        get_block=block,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime.now(UTC) - timedelta(minutes=1),
        valid_until_time=datetime.now(UTC) + timedelta(minutes=10),
        expected_next_epoch_start_block=1100,
    )
    status, receipt = validator_thin._locate_pending_broadcast_receipt(
        SimpleNamespace(substrate=substrate),
        extrinsic_hash=extrinsic_hash,
        era_reference_block=900,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        validator_hotkey="validator",
        netuid=39,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[7, 241],
        wire_weights=[65535, 7282],
        inclusion_policy=policy,
    )
    assert status == validator_thin.PASS
    assert receipt == validator_thin.ChainSubmission(
        success=True,
        extrinsic_hash=extrinsic_hash,
        block_hash=hashes[901],
        block_number=901,
        finalized=True,
    )


def test_absent_signed_hash_is_terminal_only_after_complete_mortal_era() -> None:
    extrinsic_hash = "0x" + "a" * 64
    hashes = {number: f"0x{number:064x}" for number in range(900, 905)}
    by_hash = {value: number for number, value in hashes.items()}
    finalized = {"number": 902}
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: hashes[finalized["number"]],
        get_block_number=lambda block_hash: by_hash[block_hash],
        get_block_hash=lambda number: hashes[number],
        get_block=lambda **_kwargs: {"extrinsics": []},
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime.now(UTC) - timedelta(minutes=1),
        valid_until_time=datetime.now(UTC) + timedelta(minutes=10),
        expected_next_epoch_start_block=1100,
    )
    kwargs = {
        "extrinsic_hash": extrinsic_hash,
        "era_reference_block": 900,
        "mortal_period_blocks": validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        "validator_hotkey": "validator",
        "netuid": 39,
        "version_key": validator_thin._weight_version_key(),
        "wire_uids": [7, 241],
        "wire_weights": [65535, 7282],
        "inclusion_policy": policy,
    }
    assert validator_thin._locate_pending_broadcast_receipt(
        SimpleNamespace(substrate=substrate),
        **kwargs,
    ) == (validator_thin.NOT_PROVEN, None)

    finalized["number"] = 903
    assert validator_thin._locate_pending_broadcast_receipt(
        SimpleNamespace(substrate=substrate),
        **kwargs,
    ) == (validator_thin.EXPIRED_WITHOUT_INCLUSION, None)


@pytest.mark.parametrize(
    (
        "commit_reveal",
        "timestamp_ms",
        "block_number",
        "owner_hotkey",
        "validator_permit",
        "expected",
    ),
    [
        (False, 1784932200000, 901, "burn-hotkey", True, True),
        (True, 1784932200000, 901, "burn-hotkey", True, False),
        (False, 1784934000000, 901, "burn-hotkey", True, False),
        (False, 1784932200000, 950, "burn-hotkey", True, False),
        (False, 1784932200000, 901, "replacement-owner", True, False),
        (False, 1784932200000, 901, "burn-hotkey", False, False),
    ],
)
def test_finalized_receipt_binds_policy_to_actual_inclusion_block(
    commit_reveal: bool,
    timestamp_ms: int,
    block_number: int,
    owner_hotkey: str,
    validator_permit: bool,
    expected: bool,
) -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "d" * 64
    finalized_hash = "0x" + "e" * 64
    finalized_number = max(1000, block_number)
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _value: finalized_number,
        get_block_hash=lambda number: (
            finalized_hash if number == finalized_number else block_hash
        ),
        get_block=lambda **_kw: {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": extrinsic_hash,
                        "address": "validator-hotkey",
                        "call": {
                            "call_module": "SubtensorModule",
                            "call_function": "set_mechanism_weights",
                            "call_args": [
                                {"name": "netuid", "value": 39},
                                {"name": "mecid", "value": 0},
                                {
                                    "name": "version_key",
                                    "value": validator_thin._weight_version_key(),
                                },
                                {"name": "dests", "value": [163, 204]},
                                {"name": "weights", "value": [65535, 7282]},
                            ],
                        },
                    }
                )
            ]
        },
        retrieve_extrinsic_by_hash=lambda *_args: SimpleNamespace(
            is_success=True,
            error_message=None,
            extrinsic_idx=0,
        ),
        query=lambda **_kw: timestamp_ms,
    )
    metagraph = SimpleNamespace(
        uids=[30, 163, 204],
        hotkeys=["validator-hotkey", "tdx-miner", "burn-hotkey"],
        validator_permit=[validator_permit, False, False],
        block=block_number,
    )
    subtensor = SimpleNamespace(
        substrate=substrate,
        metagraph=lambda _netuid, block: metagraph,
        commit_reveal_enabled=lambda **_kw: commit_reveal,
        get_subnet_owner_hotkey=lambda _netuid, block: owner_hotkey,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=950,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
    )
    assert (
        validator_thin._prove_finalized_receipt(
            subtensor,
            receipt=SimpleNamespace(is_success=True),
            extrinsic_hash=extrinsic_hash,
            block_hash=block_hash,
            block_number=block_number,
            validator_hotkey="validator-hotkey",
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[163, 204],
            wire_weights=[65535, 7282],
            uid_hotkeys={163: "tdx-miner", 204: "burn-hotkey"},
            expected_subnet_owner_hotkey="burn-hotkey",
            inclusion_policy=policy,
        )
        is expected
    )


def test_vector_inclusion_policy_refuses_near_expiry_before_reservation() -> None:
    now = datetime.now(UTC)
    document = payload()
    document["generated_at"] = validator_thin._canonical_policy_time(now)
    document["expires_at"] = validator_thin._canonical_policy_time(
        now
        + timedelta(
            seconds=validator_thin.CHAIN_OPERATION_DEADLINE_SECS
            + validator_thin.SN39_MIN_VALIDITY_MARGIN_SECS
            - 1
        )
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="validity remaining is shorter",
    ):
        validator_thin._vector_inclusion_policy(document, preflight)


def test_inclusion_policy_refuses_without_epoch_finality_room() -> None:
    now = datetime.now(UTC)
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={validator_thin.SN39_BURN_HOTKEY: 7},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey=validator_thin.SN39_BURN_HOTKEY,
        blocks_until_next_epoch=35,
        next_epoch_start_block=935,
        weights_rate_limit=100,
        validator_blocks_since_last_update=101,
        uid_mapping_stable_until_block=904,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=1000,
        valid_from_time=now - timedelta(seconds=1),
        valid_until_time=now + timedelta(hours=1),
        expected_next_epoch_start_block=935,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="enough room",
    ):
        validator_thin._require_inclusion_policy_ready(
            policy,
            preflight,
            now=now,
        )


def test_inclusion_policy_refuses_active_validator_weight_cooldown() -> None:
    now = datetime.now(UTC)
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={validator_thin.SN39_BURN_HOTKEY: 7},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey=validator_thin.SN39_BURN_HOTKEY,
        blocks_until_next_epoch=200,
        next_epoch_start_block=1100,
        weights_rate_limit=100,
        validator_blocks_since_last_update=100,
        uid_mapping_stable_until_block=904,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=1000,
        valid_from_time=now - timedelta(seconds=1),
        valid_until_time=now + timedelta(hours=1),
        expected_next_epoch_start_block=1100,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="inside the live validator weight-update cooldown",
    ):
        validator_thin._require_inclusion_policy_ready(
            policy,
            preflight,
            now=now,
        )


def test_validator_cannot_reward_its_own_compute_uid() -> None:
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="cannot receive validated-compute weight",
    ):
        validator_thin._require_no_validator_compute_reward(
            {30: 0.9, 204: 0.1},
            preflight=preflight,
            burn_uid=204,
        )

    validator_thin._require_no_validator_compute_reward(
        {30: 1.0},
        preflight=preflight,
        burn_uid=30,
    )


def test_automatic_uid_rebind_correction_is_removed() -> None:
    assert not hasattr(validator_thin, "_emergency_owner_burn_correction")


def test_ambiguous_success_cannot_attempt_owner_burn_correction() -> None:
    assert not hasattr(validator_thin, "_emergency_owner_burn_correction")


def test_sdk_exception_cannot_attempt_rate_limit_aware_correction() -> None:
    assert not hasattr(validator_thin, "_emergency_owner_burn_correction")


def test_uid_capacity_guard_requires_stability_for_complete_mortal_era() -> None:
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={"worker": 7, "validator": 30, "burn": 241},
        validator_hotkey="validator",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        blocks_until_next_epoch=200,
        next_epoch_start_block=1100,
        weights_rate_limit=100,
        validator_blocks_since_last_update=101,
        replacement_safe_hotkeys=frozenset({"burn"}),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="UID mappings stable",
    ):
        validator_thin._require_uid_mapping_stability(
            preflight,
            {7: "worker", 241: "burn"},
            mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        )
    validator_thin._require_uid_mapping_stability(
        validator_thin.replace(
            preflight,
            replacement_safe_hotkeys=frozenset({"worker", "burn"}),
        ),
        {7: "worker", 241: "burn"},
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
    )


def test_exact_sn39_signer_pins_era_and_journals_hash_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_hash = "0x" + "f" * 64
    exact_hash = "0x" + "a" * 64
    signed = SimpleNamespace(extrinsic_hash=bytes.fromhex("a" * 64))
    receipt = SimpleNamespace(extrinsic_hash=exact_hash, is_success=True)
    sequence: list[str] = []
    signed_calls: list[dict[str, object]] = []
    intents: list[dict[str, object]] = []
    composed_calls: list[dict[str, object]] = []

    def create_signed_extrinsic(**kwargs):
        sequence.append("sign")
        signed_calls.append(kwargs)
        return signed

    def submit_extrinsic(
        observed,
        *,
        wait_for_inclusion: bool,
        wait_for_finalization: bool,
    ):
        assert observed is signed
        assert sequence[-1] == "intent"
        assert wait_for_inclusion is True
        assert wait_for_finalization is True
        sequence.append("submit")
        return receipt

    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _hash: 900,
        get_block_hash=lambda _number: finalized_hash,
        get_account_next_index=lambda _hotkey: 17,
        create_signed_extrinsic=create_signed_extrinsic,
        submit_extrinsic=submit_extrinsic,
    )
    wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator"))
    preflight = validator_thin.ChainPreflight(
        wallet=wallet,
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid={"validator": 30, "worker": 7, "burn": 241},
        validator_hotkey="validator",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        finalized_hash=finalized_hash,
    )
    monkeypatch.setattr(
        "bittensor.core.types.ExtrinsicResponse.unlock_wallet",
        lambda *_args, **_kwargs: SimpleNamespace(success=True),
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.pallets.SubtensorModule",
        lambda _subtensor: SimpleNamespace(
            set_mechanism_weights=lambda **kwargs: (
                composed_calls.append(kwargs) or "call"
            )
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_broadcast_intent",
        lambda _args, **kwargs: sequence.append("intent") or intents.append(kwargs),
    )

    assert (
        validator_thin._submit_exact_sn39_extrinsic(
            preflight,
            runtime_contract=SimpleNamespace(
                require_full_provenance_for_broadcast=True
            ),
            attempt_id="sha256:" + "1" * 64,
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
            mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        )
        is receipt
    )
    assert sequence == ["sign", "intent", "submit"]
    assert composed_calls == [
        {
            "netuid": 39,
            "mecid": 0,
            "dests": [7, 241],
            "weights": [65535, 7282],
            "version_key": validator_thin._weight_version_key(),
        }
    ]
    assert signed_calls == [
        {
            "call": "call",
            "keypair": wallet.hotkey,
            "nonce": 17,
            "era": {
                "period": validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
                "current": 900,
            },
        }
    ]
    assert intents[0]["extrinsic_hash"] == exact_hash
    assert intents[0]["nonce"] == 17
    assert intents[0]["era_reference_block"] == 900


def test_exact_sn39_signer_refuses_head_drift_before_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finalized_hash = "0x" + "f" * 64
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _hash: 901,
        get_block_hash=lambda _number: finalized_hash,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=SimpleNamespace(hotkey=SimpleNamespace(ss58_address="validator")),
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid={"validator": 30},
        validator_hotkey="validator",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
    )
    monkeypatch.setattr(
        "bittensor.core.types.ExtrinsicResponse.unlock_wallet",
        lambda *_args, **_kwargs: SimpleNamespace(success=True),
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.pallets.SubtensorModule",
        lambda _subtensor: (_ for _ in ()).throw(
            AssertionError("head drift must refuse before composing")
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_broadcast_intent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("head drift must refuse before journaling a signed hash")
        ),
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="head advanced after preflight",
    ):
        validator_thin._submit_exact_sn39_extrinsic(
            preflight,
            runtime_contract=object(),
            attempt_id="sha256:" + "1" * 64,
            netuid=39,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[7, 241],
            wire_weights=[65535, 7282],
            mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        )


def test_transient_archive_fault_records_receipt_without_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "b" * 64
    receipt = SimpleNamespace(
        extrinsic_hash=extrinsic_hash,
        block_hash=block_hash,
        block_number=901,
        is_success=True,
    )
    sdk_calls: list[dict[str, object]] = []
    recorded: list[validator_thin.ChainSubmission] = []
    statuses: list[str] = []
    sequence: list[str] = []
    attempt_id = "sha256:" + "1" * 64
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey=validator_thin.SN39_BURN_HOTKEY,
        blocks_until_next_epoch=200,
        next_epoch_start_block=1100,
        weights_rate_limit=100,
        validator_blocks_since_last_update=101,
        uid_mapping_stable_until_block=904,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=1000,
        valid_from_time=datetime.now(UTC) - timedelta(seconds=1),
        valid_until_time=datetime.now(UTC) + timedelta(hours=1),
        expected_next_epoch_start_block=1100,
    )
    monkeypatch.setattr(
        validator_thin,
        "_authorize_sn39_chain_submission",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **kwargs: (
            sequence.append("signed") or sdk_calls.append(kwargs) or receipt
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_read_state",
        lambda _path: {"submission_pending_id": attempt_id},
    )
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _runtime: tmp_path / "journal.json",
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_submission_receipt",
        lambda _args, **kwargs: recorded.append(kwargs["submission"]),
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_proof_status",
        lambda _args, **kwargs: statuses.append(kwargs["status"]),
    )
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **_kwargs: validator_thin.NOT_PROVEN,
    )

    with pytest.raises(
        validator_thin._PendingReceiptNotProven,
        match="temporarily unavailable",
    ):
        validator_thin.set_weights_on_chain(
            {7: 0.9, 241: 0.1},
            network="finney",
            netuid=39,
            wallet_name="validator",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
            uid_hotkeys={
                7: "worker",
                241: validator_thin.SN39_BURN_HOTKEY,
            },
            inclusion_policy=policy,
            runtime_contract=object(),
        )
    assert len(sdk_calls) == 1
    assert sequence == ["signed"]
    assert len(recorded) == 1
    assert recorded[0].extrinsic_hash == extrinsic_hash
    assert statuses == [validator_thin.NOT_PROVEN]
    assert not hasattr(validator_thin, "_emergency_owner_burn_correction")


@pytest.mark.parametrize("failure_stage", ["submit_response", "receipt_persistence"])
def test_post_signed_uncertainty_is_not_proven_not_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    attempt_id = "sha256:" + "1" * 64
    extrinsic_hash = "0x" + "a" * 64
    receipt = SimpleNamespace(
        extrinsic_hash=extrinsic_hash,
        block_hash="0x" + "b" * 64,
        block_number=901,
        is_success=True,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey=validator_thin.SN39_BURN_HOTKEY,
        blocks_until_next_epoch=200,
        next_epoch_start_block=1100,
        weights_rate_limit=100,
        validator_blocks_since_last_update=101,
        uid_mapping_stable_until_block=904,
    )
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=1000,
        valid_from_time=datetime.now(UTC) - timedelta(seconds=1),
        valid_until_time=datetime.now(UTC) + timedelta(hours=1),
        expected_next_epoch_start_block=1100,
    )
    monkeypatch.setattr(
        validator_thin,
        "_authorize_sn39_chain_submission",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_read_state",
        lambda _path: {
            "submission_pending_id": attempt_id,
            "submission_pending_phase": "signed_intent",
        },
    )
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _runtime: tmp_path / "journal.json",
    )
    monkeypatch.setattr(
        validator_thin,
        "_abort_unsigned_common_submission",
        lambda *_args, **_kwargs: False,
    )
    if failure_stage == "submit_response":
        monkeypatch.setattr(
            validator_thin,
            "_submit_exact_sn39_extrinsic",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("response lost after submit")
            ),
        )
    else:
        monkeypatch.setattr(
            validator_thin,
            "_submit_exact_sn39_extrinsic",
            lambda *_args, **_kwargs: receipt,
        )
        monkeypatch.setattr(
            validator_thin,
            "_record_pending_submission_receipt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("receipt fsync failed")
            ),
        )

    with pytest.raises(
        validator_thin._PendingReceiptNotProven,
        match="exact signed transaction may have finalized",
    ):
        validator_thin.set_weights_on_chain(
            {7: 0.9, 241: 0.1},
            network="finney",
            netuid=39,
            wallet_name="validator",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
            uid_hotkeys={
                7: "worker",
                241: validator_thin.SN39_BURN_HOTKEY,
            },
            inclusion_policy=policy,
            runtime_contract=object(),
        )


def test_restart_reproves_noncontiguous_uid_receipt_without_chain_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "sha256:" + "1" * 64
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=1000,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
        expected_next_epoch_start_block=1100,
    )
    wire_uids, wire_weights = validator_thin._wire_weights(
        [7, 241],
        [0.9, 0.1],
    )
    identity = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator",
        "mapping_block": 900,
        "policy_version": 7,
        "vector_id": "recovered-vector",
        "signed_vector_sha256": "sha256:" + "c" * 64,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [
            [7, "worker"],
            [241, validator_thin.SN39_BURN_HOTKEY],
        ],
        "next_epoch_start_block": 1100,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
    }
    state = {
        "submission_pending_id": attempt_id,
        "submission_pending_lane": "thin",
        "submission_pending_phase": "signed_intent",
        "submission_pending_identity": identity,
        "submission_pending_proof_status": validator_thin.NOT_PROVEN,
        "submission_pending_receipt_candidate": {
            "extrinsic_hash": "0x" + "a" * 64,
            "block_hash": "0x" + "b" * 64,
            "block_number": 901,
            "version_key": validator_thin._weight_version_key(),
            "wire_uids": wire_uids,
            "wire_weights": wire_weights,
        },
        "submission_pending_broadcast_intent": {
            "extrinsic_hash": "0x" + "a" * 64,
            "nonce": 17,
            "era_reference_block": 900,
            "mortal_period_blocks": validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
            "version_key": validator_thin._weight_version_key(),
            "wire_uids": wire_uids,
            "wire_weights": wire_weights,
        },
        "submission_genesis_hash": validator_thin.FINNEY_GENESIS_HASH,
    }
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=905,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    args = SimpleNamespace(
        broadcast=True,
        offline=False,
        require_full_provenance_for_broadcast=True,
        state_file=str(tmp_path / "thin.json"),
    )
    finalizations: list[dict[str, object]] = []
    lane_updates: list[dict[str, object]] = []
    proof_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda runtime: setattr(runtime, "_tick_preflight", preflight),
    )
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _args: tmp_path / "journal.json",
    )
    monkeypatch.setattr(validator_thin, "_read_state", lambda _path: state)
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **kwargs: proof_calls.append(kwargs) or validator_thin.PASS,
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_proof_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_finalize_common_submission",
        lambda *_args, **kwargs: finalizations.append(kwargs),
    )
    monkeypatch.setattr(
        validator_thin,
        "_write_state",
        lambda _path, updates: lane_updates.append(dict(updates)),
    )
    monkeypatch.setattr(
        validator_thin,
        "_write_state_fenced",
        lambda _path, updates: lane_updates.append(dict(updates)),
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must never write")
        ),
    )

    recovered = validator_thin._recover_pending_launch_receipt(args)
    assert isinstance(recovered, validator_thin.RecoveredSubmission)
    assert recovered.policy_version == 7
    assert proof_calls[0]["require_receipt"] is False
    assert proof_calls[0]["wire_uids"] == wire_uids
    assert len(finalizations) == 1
    assert finalizations[0]["attempt_id"] == attempt_id
    assert lane_updates[0]["thin_submission_block_number"] == 901


def test_continuous_restart_recovers_crash_before_receipt_without_chain_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "sha256:" + "2" * 64
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
        expected_next_epoch_start_block=1100,
    )
    wire_uids, wire_weights = validator_thin._wire_weights([7, 241], [0.9, 0.1])
    identity = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator",
        "mapping_block": 900,
        "policy_version": 8,
        "vector_id": "continuous-recovered-vector",
        "signed_vector_sha256": "sha256:" + "d" * 64,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [
            [7, "worker"],
            [241, validator_thin.SN39_BURN_HOTKEY],
        ],
        "next_epoch_start_block": 1100,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
    }
    state = {
        "submission_pending_id": attempt_id,
        "submission_pending_lane": "thin",
        "submission_pending_phase": "signed_intent",
        "submission_pending_identity": identity,
        "submission_pending_proof_status": "pending",
        "submission_pending_broadcast_intent": {
            "extrinsic_hash": "0x" + "a" * 64,
            "nonce": 18,
            "era_reference_block": 900,
            "mortal_period_blocks": validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
            "version_key": validator_thin._weight_version_key(),
            "wire_uids": wire_uids,
            "wire_weights": wire_weights,
        },
        "submission_genesis_hash": validator_thin.FINNEY_GENESIS_HASH,
    }
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=905,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    args = SimpleNamespace(
        broadcast=True,
        offline=False,
        require_full_provenance_for_broadcast=False,
        state_file=str(tmp_path / "thin.json"),
    )
    located = validator_thin.ChainSubmission(
        success=True,
        extrinsic_hash="0x" + "a" * 64,
        block_hash="0x" + "b" * 64,
        block_number=901,
        finalized=True,
    )
    recorded: list[validator_thin.ChainSubmission] = []
    finalizations: list[dict[str, object]] = []
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda runtime: setattr(runtime, "_tick_preflight", preflight),
    )
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _args: tmp_path / "journal.json",
    )
    monkeypatch.setattr(validator_thin, "_read_state", lambda _path: state)
    monkeypatch.setattr(
        validator_thin,
        "_locate_pending_broadcast_receipt",
        lambda *_args, **_kwargs: (validator_thin.PASS, located),
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_submission_receipt",
        lambda _args, **kwargs: recorded.append(kwargs["submission"]),
    )
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **_kwargs: validator_thin.PASS,
    )
    monkeypatch.setattr(
        validator_thin,
        "_record_pending_proof_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_finalize_common_submission",
        lambda *_args, **kwargs: finalizations.append(kwargs),
    )
    monkeypatch.setattr(validator_thin, "_write_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        validator_thin,
        "_write_state_fenced",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recovery must never write")
        ),
    )

    recovered = validator_thin._recover_pending_launch_receipt(args)
    assert isinstance(recovered, validator_thin.RecoveredSubmission)
    assert recorded == [located]
    assert finalizations[0]["submission"] == located


def test_full_authority_restart_finalizes_exact_signed_attempt_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "sha256:" + "6" * 64
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
        expected_next_epoch_start_block=1100,
    )
    wire_uids, wire_weights = validator_thin._wire_weights([7, 241], [0.9, 0.1])
    args = SimpleNamespace(
        broadcast=True,
        offline=False,
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
        require_full_provenance_for_broadcast=False,
        max_submissions=0,
        runtime_root=str(tmp_path / "runtime"),
        state_file=str(tmp_path / "authority.json"),
        _submission_validator_hotkey="validator",
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    identity = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator",
        "mapping_block": 900,
        "source_epoch": 12,
        "report_id": "sha256:" + "c" * 64,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [
            [7, "worker"],
            [241, validator_thin.SN39_BURN_HOTKEY],
        ],
        "next_epoch_start_block": 1100,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
    }
    validator_thin._reserve_common_submission(
        args,
        lane="authority",
        attempt_id=attempt_id,
        identity=identity,
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=attempt_id,
        extrinsic_hash="0x" + "a" * 64,
        nonce=18,
        era_reference_block=900,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    receipt = validator_thin.ChainSubmission(
        success=True,
        extrinsic_hash="0x" + "a" * 64,
        block_hash="0x" + "b" * 64,
        block_number=901,
        finalized=True,
    )
    validator_thin._record_pending_submission_receipt(
        args,
        attempt_id=attempt_id,
        submission=receipt,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=905,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda runtime: setattr(runtime, "_tick_preflight", preflight),
    )
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **_kwargs: validator_thin.PASS,
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("authority recovery must never write")
        ),
    )

    recovered = validator_thin._recover_pending_launch_receipt(args)
    assert isinstance(recovered, validator_thin.RecoveredAuthoritySubmission)
    assert recovered.source_epoch == 12
    assert recovered.report_id == "sha256:" + "c" * 64
    assert recovered.boundary_detail == (
        "authority=full_provenance uids=2 burn_uid=241 "
        "burn_share=0.100000 vector=7:0.900000,241:0.100000"
    )
    common = validator_thin._read_state(validator_thin._submission_state_path(args))
    assert common["submission_pending_id"] is None
    assert common["submission_finalized_id"] == attempt_id
    lane = validator_thin._read_state(Path(args.state_file))
    assert lane["authority_submission_attempt_status"] == "finalized"
    assert lane["authority_submission_finalized_id"] == attempt_id


def _finalize_common_without_lane_mirror(
    tmp_path: Path,
    *,
    lane: str,
) -> tuple[SimpleNamespace, str, dict[str, object], Path]:
    attempt_id = "sha256:" + ("8" if lane == "thin" else "9") * 64
    wire_uids, wire_weights = validator_thin._wire_weights([7, 241], [0.9, 0.1])
    args = SimpleNamespace(
        broadcast=True,
        offline=False,
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
        require_full_provenance_for_broadcast=False,
        max_submissions=0,
        runtime_root=str(tmp_path / f"{lane}-runtime"),
        state_file=str(tmp_path / f"{lane}-lane.json"),
        _submission_validator_hotkey="validator",
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    identity: dict[str, object] = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator",
        "mapping_block": 900,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [
            [7, "worker"],
            [241, validator_thin.SN39_BURN_HOTKEY],
        ],
    }
    if lane == "thin":
        identity.update(
            {
                "policy_version": 31,
                "vector_id": "crash-window-vector",
                "signed_vector_sha256": "sha256:" + "c" * 64,
            }
        )
    else:
        identity.update(
            {
                "source_epoch": 32,
                "report_id": "sha256:" + "d" * 64,
            }
        )
    validator_thin._reserve_common_submission(
        args,
        lane=lane,
        attempt_id=attempt_id,
        identity=identity,
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=attempt_id,
        extrinsic_hash="0x" + "a" * 64,
        nonce=20,
        era_reference_block=900,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    receipt = validator_thin.ChainSubmission(
        success=True,
        extrinsic_hash="0x" + "a" * 64,
        block_hash="0x" + "b" * 64,
        block_number=901,
        finalized=True,
    )
    validator_thin._record_pending_submission_receipt(
        args,
        attempt_id=attempt_id,
        submission=receipt,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    validator_thin._record_pending_proof_status(
        args,
        attempt_id=attempt_id,
        status=validator_thin.PASS,
    )
    validator_thin._finalize_common_submission(
        args,
        attempt_id=attempt_id,
        submission=receipt,
    )
    lane_path = Path(args.state_file)
    assert not lane_path.exists()
    return args, attempt_id, identity, validator_thin._submission_state_path(args)


@pytest.mark.parametrize("lane", ["thin", "authority"])
def test_common_finalization_crash_repairs_lane_once_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
) -> None:
    args, attempt_id, identity, _common_path = _finalize_common_without_lane_mirror(
        tmp_path,
        lane=lane,
    )
    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("finalized crash recovery must never write")
        ),
    )

    recovered = validator_thin._recover_pending_launch_receipt(args)
    if lane == "thin":
        assert isinstance(recovered, validator_thin.RecoveredSubmission)
        assert recovered.policy_version == 31
        assert recovered.vector_id == "crash-window-vector"
        prefix = "thin"
    else:
        assert isinstance(recovered, validator_thin.RecoveredAuthoritySubmission)
        assert recovered.source_epoch == 32
        assert recovered.report_id == "sha256:" + "d" * 64
        prefix = "authority"
    lane_path = Path(args.state_file)
    lane_state = validator_thin._read_state(lane_path)
    assert lane_state[f"{prefix}_submission_attempt_status"] == "finalized"
    assert lane_state[f"{prefix}_submission_finalized_id"] == attempt_id
    assert lane_state[f"{prefix}_submission_identity"] == identity
    before_second_recovery = lane_path.read_bytes()

    assert validator_thin._recover_pending_launch_receipt(args) is None
    assert lane_path.read_bytes() == before_second_recovery


@pytest.mark.parametrize(
    "field",
    [
        "submission_finalized_broadcast_intent",
        "submission_finalized_receipt",
    ],
)
def test_common_finalization_tamper_fails_before_lane_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    args, _attempt_id, _identity, common_path = _finalize_common_without_lane_mirror(
        tmp_path, lane="thin"
    )
    common = validator_thin._read_state(common_path)
    tampered = dict(common[field])
    tampered["extrinsic_hash"] = "0x" + "e" * 64
    validator_thin._write_state(common_path, {field: tampered})
    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("contradictory finalized state must never write")
        ),
    )

    with pytest.raises(
        validator_thin._PostSignedSubmissionMismatch,
        match="contradictory|does not match its durable identity",
    ):
        validator_thin._recover_pending_launch_receipt(args)
    assert not Path(args.state_file).exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            ValueError("conflicting lane fence"),
            validator_thin._PostSignedSubmissionMismatch,
        ),
        (OSError("lane fsync unavailable"), validator_thin._PendingReceiptNotProven),
    ],
)
def test_common_finalization_lane_repair_classifies_local_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected: type[Exception],
) -> None:
    args, _attempt_id, _identity, _common_path = _finalize_common_without_lane_mirror(
        tmp_path, lane="thin"
    )
    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_write_state_fenced",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed lane repair must never submit")
        ),
    )

    with pytest.raises(expected):
        validator_thin._recover_pending_launch_receipt(args)
    assert not Path(args.state_file).exists()


def test_continuous_profile_persistently_finalizes_journaled_launch_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "sha256:" + "7" * 64
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
        expected_next_epoch_start_block=1100,
    )
    wire_uids, wire_weights = validator_thin._wire_weights([7, 241], [0.9, 0.1])
    identity = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator",
        "mapping_block": 900,
        "policy_version": 9,
        "vector_id": "launch-recovered-by-continuous",
        "signed_vector_sha256": "sha256:" + "e" * 64,
        "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [
            [7, "worker"],
            [241, validator_thin.SN39_BURN_HOTKEY],
        ],
        "next_epoch_start_block": 1100,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
        "uid_safety": {
            "schema": "cathedral_sn39_uid_safety_v2",
            "registration": {"fixture": True},
            "rotation": {"status": validator_thin.PASS},
        },
    }
    launch = SimpleNamespace(
        broadcast=True,
        offline=False,
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
        max_submissions=1,
        require_full_provenance_for_broadcast=True,
        runtime_root=str(tmp_path / "runtime"),
        state_file=str(tmp_path / "thin.json"),
        _submission_validator_hotkey="validator",
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    validator_thin._reserve_common_submission(
        launch,
        lane="thin",
        attempt_id=attempt_id,
        identity=identity,
    )
    validator_thin._record_pending_broadcast_intent(
        launch,
        attempt_id=attempt_id,
        extrinsic_hash="0x" + "a" * 64,
        nonce=19,
        era_reference_block=900,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    receipt = validator_thin.ChainSubmission(
        success=True,
        extrinsic_hash="0x" + "a" * 64,
        block_hash="0x" + "b" * 64,
        block_number=901,
        finalized=True,
    )
    validator_thin._record_pending_submission_receipt(
        launch,
        attempt_id=attempt_id,
        submission=receipt,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    continuous = SimpleNamespace(**vars(launch))
    continuous.require_full_provenance_for_broadcast = False
    continuous.max_submissions = 0
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=905,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda runtime: setattr(runtime, "_tick_preflight", preflight),
    )
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **_kwargs: validator_thin.PASS,
    )
    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-profile recovery must never write")
        ),
    )

    recovered = validator_thin._recover_pending_launch_receipt(continuous)
    assert isinstance(recovered, validator_thin.RecoveredSubmission)
    journal = validator_thin._read_state(
        validator_thin._submission_state_path(continuous)
    )
    assert journal["submission_pending_id"] is None
    assert journal["submission_launch_status"] == "finalized"
    assert journal["submission_launch_attempt_id"] == attempt_id
    assert journal["submission_launch_identity"] == identity
    assert journal["submission_launch_broadcast_intent"]["nonce"] == 19
    assert journal["submission_launch_attempt_ids"] == [attempt_id]


def test_exact_recovered_vector_is_idle_without_second_chain_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = {
        **payload(),
        "vector_id": "continuous-recovered-vector",
        "policy_version": 8,
        "key_id": "cathedral-weight-policy",
    }
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                vector,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    state_path = tmp_path / "thin.json"
    validator_thin._write_state(
        state_path,
        {
            "last_accepted_policy_version": 8,
            "thin_recovered_policy_version": 8,
            "thin_recovered_vector_id": vector["vector_id"],
            "thin_recovered_signed_vector_sha256": digest,
        },
    )
    events: list[tuple[str, dict[str, object]]] = []
    args = SimpleNamespace(
        publisher_url="https://example.invalid/vector",
        state_file=str(state_path),
        public_key_hex="fixture",
        key_id="cathedral-weight-policy",
        _events=SimpleNamespace(
            event=lambda code, **fields: events.append((code, fields))
        ),
    )
    verified: list[dict[str, object]] = []
    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: vector)
    monkeypatch.setattr(
        validator_thin.wire,
        "verify_signature",
        lambda observed, **_kwargs: verified.append(observed),
    )
    monkeypatch.setattr(
        validator_thin,
        "set_weights_on_chain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact recovered vector must not write")
        ),
    )

    assert validator_thin._thin_tick_locked(args) is True
    assert verified == [vector]
    assert [code for code, _fields in events] == ["RECOVERED_VECTOR_IDLE"]


def test_same_version_different_vector_is_not_reclassified_as_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recovered_vector = {
        **payload(),
        "vector_id": "continuous-recovered-vector",
        "policy_version": 8,
        "key_id": "cathedral-weight-policy",
    }
    recovered_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recovered_vector,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    changed_vector = json.loads(json.dumps(recovered_vector))
    changed_vector["weights"][0]["weight"] = 0.5
    state_path = tmp_path / "thin.json"
    validator_thin._write_state(
        state_path,
        {
            "last_accepted_policy_version": 8,
            "thin_recovered_policy_version": 8,
            "thin_recovered_vector_id": recovered_vector["vector_id"],
            "thin_recovered_signed_vector_sha256": recovered_digest,
        },
    )
    args = SimpleNamespace(
        publisher_url="https://example.invalid/vector",
        state_file=str(state_path),
        public_key_hex="fixture",
        key_id="cathedral-weight-policy",
        network="finney",
        netuid=39,
    )
    accepted: list[dict[str, object]] = []
    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: changed_vector)

    def reject_changed(observed: dict[str, object], **_kwargs: object) -> None:
        accepted.append(observed)
        raise validator_thin.wire.VectorError("rollback fence rejects changed vector")

    monkeypatch.setattr(validator_thin, "accept_vector", reject_changed)
    monkeypatch.setattr(
        validator_thin,
        "set_weights_on_chain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("same-version changed vector must not write")
        ),
    )

    with pytest.raises(validator_thin.wire.VectorError, match="rollback fence"):
        validator_thin._thin_tick_locked(args)
    assert accepted == [changed_vector]


def test_chain_submission_refuses_commit_reveal_before_sdk_call() -> None:
    calls = []
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(set_weights=lambda **kwargs: calls.append(kwargs)),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=True,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    with pytest.raises(validator_thin.wire.VectorError, match="commit-reveal"):
        validator_thin.set_weights_on_chain(
            {0: 0.1, 163: 0.9},
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
        )
    assert calls == []


def test_chain_submission_requires_canonical_finalized_head_proof(
    monkeypatch,
) -> None:
    receipt_block_hash = "0x" + "d" * 64
    finalized_head_hash = "0x" + "e" * 64
    receipt = SimpleNamespace(
        extrinsic_hash="0x" + "a" * 64,
        block_hash=receipt_block_hash,
        block_number=8680430,
        is_success=True,
        finalized=False,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(
            set_weights=lambda **_kwargs: SimpleNamespace(
                success=True, extrinsic_receipt=receipt
            ),
            substrate=SimpleNamespace(
                get_chain_finalised_head=lambda: finalized_head_hash,
                get_block_number=lambda _block_hash: 8680429,
                get_block_hash=lambda _block_number: receipt_block_hash,
            ),
        ),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.weights.set_weights_extrinsic",
        lambda **_kwargs: SimpleNamespace(success=True, extrinsic_receipt=receipt),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="archive/RPC proof"):
        validator_thin.set_weights_on_chain(
            {0: 0.1, 163: 0.9},
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
        )


def test_chain_submission_requires_release_grade_receipt_identity(monkeypatch) -> None:
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(
            set_weights=lambda **_kwargs: SimpleNamespace(
                success=True, extrinsic_receipt=None
            )
        ),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.weights.set_weights_extrinsic",
        lambda **_kwargs: SimpleNamespace(success=True, extrinsic_receipt=None),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="canonical receipt"):
        validator_thin.set_weights_on_chain(
            {0: 0.1, 163: 0.9},
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
        )


def test_chain_submission_has_a_validator_controlled_wall_clock_deadline(
    monkeypatch,
) -> None:
    def stalled(**_kwargs):
        time.sleep(0.2)
        return SimpleNamespace(success=False)

    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(set_weights=stalled),
        hotkey_to_uid={"burn-hotkey": 0, "validator-hotkey": 30, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=8680424,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        "bittensor.core.extrinsics.weights.set_weights_extrinsic",
        stalled,
    )
    started = time.monotonic()
    with pytest.raises(validator_thin.wire.VectorError, match="wall-clock deadline"):
        validator_thin.set_weights_on_chain(
            {0: 0.1, 163: 0.9},
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
            broadcast=True,
            preflight=preflight,
            deadline_secs=0.03,
        )
    assert time.monotonic() - started < 0.15


def _finney_uid_safety_fixture(
    *,
    hotkey_swap_interval: int = 7200,
) -> SimpleNamespace:
    """Two Finney targets whose coldkeys both carry a live rotation lock."""
    mapping_hash = "0x" + "a" * 64
    values: dict[tuple[str, tuple[object, ...]], object] = {
        ("ColdkeySwapAnnouncementDelay", ()): 36000,
        ("Owner", ("worker",)): "worker-coldkey",
        ("Owner", ("burn",)): "burn-coldkey",
        ("LastHotkeySwapOnNetuid", (39, "worker-coldkey")): 98,
        ("LastHotkeySwapOnNetuid", (39, "burn-coldkey")): 99,
        ("ColdkeySwapAnnouncements", ("worker-coldkey",)): None,
        ("ColdkeySwapAnnouncements", ("burn-coldkey",)): None,
        ("HotkeySuccessor", (39, "worker")): None,
        ("HotkeySuccessor", (39, "burn")): None,
        ("HotkeySuccessor", (39, "old-worker")): "worker",
        ("HotkeySuccessor", (39, "old-burn")): "burn",
        ("HotkeyRoot", (39, "worker")): "old-worker",
        ("HotkeyRoot", (39, "burn")): "old-burn",
        ("HotkeyRoot", (39, "old-worker")): None,
        ("HotkeyRoot", (39, "old-burn")): None,
    }
    rotations = {
        98: {
            "block_hash": "0x" + "b" * 64,
            "extrinsic_hash": "0x" + "1" * 64,
            "coldkey": "worker-coldkey",
            "old_hotkey": "old-worker",
            "new_hotkey": "worker",
        },
        99: {
            "block_hash": "0x" + "c" * 64,
            "extrinsic_hash": "0x" + "2" * 64,
            "coldkey": "burn-coldkey",
            "old_hotkey": "old-burn",
            "new_hotkey": "burn",
        },
    }

    proved_rotation_blocks: list[int] = []

    def get_block_hash(block: int) -> str | None:
        if block == 100:
            return mapping_hash
        proved_rotation_blocks.append(block)
        return str(rotations[block]["block_hash"]) if block in rotations else None

    def get_block_number(block_hash: str) -> int:
        return next(
            block for block, row in rotations.items() if row["block_hash"] == block_hash
        )

    def get_block(*, block_hash: str) -> dict[str, object]:
        row = next(row for row in rotations.values() if row["block_hash"] == block_hash)
        return {
            "extrinsics": [
                SimpleNamespace(
                    value={
                        "extrinsic_hash": row["extrinsic_hash"],
                        "address": row["coldkey"],
                        "call": {
                            "call_module": "SubtensorModule",
                            "call_function": "swap_hotkey_v2",
                            "call_args": [
                                {"name": "hotkey", "value": row["old_hotkey"]},
                                {
                                    "name": "new_hotkey",
                                    "value": row["new_hotkey"],
                                },
                                {"name": "netuid", "value": 39},
                                {"name": "keep_stake", "value": False},
                            ],
                        },
                    }
                )
            ]
        }

    def retrieve_extrinsic_by_hash(
        block_hash: str,
        extrinsic_hash: str,
    ) -> SimpleNamespace:
        row = next(row for row in rotations.values() if row["block_hash"] == block_hash)
        assert extrinsic_hash == row["extrinsic_hash"]
        return SimpleNamespace(
            extrinsic_idx=0,
            is_success=True,
            error_message=None,
            triggered_events=[
                {
                    "event": {
                        "module_id": "SubtensorModule",
                        "event_id": "HotkeySwappedOnSubnet",
                        "attributes": {
                            "coldkey": row["coldkey"],
                            "old_hotkey": row["old_hotkey"],
                            "new_hotkey": row["new_hotkey"],
                            "netuid": 39,
                        },
                    }
                }
            ],
        )

    substrate = SimpleNamespace(
        get_block_hash=get_block_hash,
        get_block_number=get_block_number,
        get_block=get_block,
        retrieve_extrinsic_by_hash=retrieve_extrinsic_by_hash,
        query=lambda **_kwargs: SimpleNamespace(
            value=int(datetime(2026, 7, 24, 21, 0, tzinfo=UTC).timestamp() * 1000)
        ),
        get_constant=lambda *, constant_name, **_kwargs: (
            hotkey_swap_interval
            if constant_name == "HotkeySwapOnSubnetInterval"
            else (_ for _ in ()).throw(AssertionError("coldkey delay must use storage"))
        ),
    )
    subtensor = SimpleNamespace(
        substrate=substrate,
        query_subtensor=lambda *, name, params, block: (
            values[(name, tuple(params))]
            if block == 100
            else (_ for _ in ()).throw(AssertionError("wrong mapping block"))
        ),
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=subtensor,
        hotkey_to_uid={"worker": 7, "burn": 241},
        validator_hotkey="validator",
        validator_uid=30,
        block=100,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        replacement_safe_hotkeys=frozenset({"worker", "burn"}),
        subnet_free_uid_slots=2,
        subnet_max_regs_per_block=0,
        subnet_min_nonimmune_uids=10,
        subnet_immunity_period=15000,
        subnet_owner_coldkey="subnet-owner-coldkey",
        subnet_immune_owner_uids_limit=1,
        subnet_owner_immortal_hotkeys=frozenset({"burn"}),
        subnet_max_uids=4,
        subnet_registration_blocks=((7, "worker", 90), (241, "burn", 1)),
        subnet_owned_hotkeys=("burn",),
    )
    return SimpleNamespace(
        values=values,
        rotations=rotations,
        preflight=preflight,
        proved_rotation_blocks=proved_rotation_blocks,
    )


def _uid_safety(fixture: SimpleNamespace) -> dict[str, object]:
    return validator_thin._require_uid_mapping_stability(
        fixture.preflight,
        {7: "worker", 241: "burn"},
        mortal_period_blocks=4,
    )


def test_finney_uid_safety_proves_an_active_rotation_lock() -> None:
    fixture = _finney_uid_safety_fixture()
    proof = _uid_safety(fixture)
    assert proof["schema"] == "cathedral_sn39_uid_safety_v2"
    assert proof["stability_basis"] == "operator_controlled_coldkeys"
    assert proof["rotation"]["status"] == validator_thin.PASS
    assert proof["rotation"]["era_last_block"] == 103
    targets = proof["rotation"]["targets"]
    assert [row["uid"] for row in targets] == [7, 241]
    assert [row["swap_lock"] for row in targets] == ["active", "active"]
    assert [row["hotkey_swap_safe_until_block"] for row in targets] == [7298, 7299]
    assert [row["hotkey_root"] for row in targets] == ["old-worker", "old-burn"]
    assert all(row["rotation_receipt"]["call"] == "swap_hotkey_v2" for row in targets)
    assert fixture.proved_rotation_blocks == [98, 99]


def test_finney_uid_safety_accepts_a_target_that_never_rotated() -> None:
    fixture = _finney_uid_safety_fixture()
    fixture.values[("LastHotkeySwapOnNetuid", (39, "worker-coldkey"))] = 0
    proof = _uid_safety(fixture)
    never = proof["rotation"]["targets"][0]
    assert never["uid"] == 7
    assert never["swap_lock"] == "never_rotated"
    assert never["last_hotkey_swap_block"] == 0
    assert never["hotkey_swap_safe_until_block"] is None
    assert never["hotkey_root"] is None
    assert never["rotation_receipt"] is None
    # No lock is claimed, so no rotation block is fetched to prove one.
    assert fixture.proved_rotation_blocks == [99]


def test_finney_uid_safety_accepts_an_expired_rotation_cooldown() -> None:
    # A cooldown of exactly the mortal era still passes the constants check, but
    # it only covers the era for the more recent of the two rotations.
    fixture = _finney_uid_safety_fixture(hotkey_swap_interval=4)
    proof = _uid_safety(fixture)
    targets = proof["rotation"]["targets"]
    assert [row["swap_lock"] for row in targets] == ["expired", "active"]
    assert [row["hotkey_swap_safe_until_block"] for row in targets] == [102, 103]
    assert targets[0]["rotation_receipt"] is None
    assert targets[0]["hotkey_root"] is None
    assert targets[1]["rotation_receipt"]["block_number"] == 99
    # Only the still-locked target is proven.
    assert fixture.proved_rotation_blocks == [99]


def test_finney_uid_safety_accepts_every_cooldown_expired() -> None:
    fixture = _finney_uid_safety_fixture(hotkey_swap_interval=4)
    fixture.values[("LastHotkeySwapOnNetuid", (39, "burn-coldkey"))] = 98
    proof = _uid_safety(fixture)
    targets = proof["rotation"]["targets"]
    assert [row["swap_lock"] for row in targets] == ["expired", "expired"]
    assert [row["rotation_receipt"] for row in targets] == [None, None]
    assert [row["hotkey_root"] for row in targets] == [None, None]
    assert fixture.proved_rotation_blocks == []


def test_finney_uid_safety_still_proves_a_claimed_active_lock() -> None:
    fixture = _finney_uid_safety_fixture()
    fixture.rotations[98]["new_hotkey"] = "someone-else"
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="unique exact swap_hotkey_v2",
    ):
        _uid_safety(fixture)


def test_finney_uid_safety_still_refuses_a_pending_coldkey_swap() -> None:
    fixture = _finney_uid_safety_fixture()
    fixture.values[("ColdkeySwapAnnouncements", ("worker-coldkey",))] = {
        "execution_block": 101
    }
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="pending coldkey swap",
    ):
        _uid_safety(fixture)
    # A never-rotated target with a scheduled coldkey transfer still refuses.
    fixture.values[("LastHotkeySwapOnNetuid", (39, "worker-coldkey"))] = 0
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="pending coldkey swap",
    ):
        _uid_safety(fixture)


def test_unsigned_reservation_does_not_consume_budget_until_signed_intent(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        netuid=39,
        offline=False,
        max_submissions=1,
        require_full_provenance_for_broadcast=True,
        _submission_validator_hotkey="validator",
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    attempt_id = "sha256:" + "1" * 64
    identity = {
        "policy_version": 7,
        "uid_weights": [[7, 0.9], [241, 0.1]],
        "uid_hotkeys": [[7, "worker"], [241, "burn"]],
    }
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id=attempt_id,
        identity=identity,
    )
    journal_path = validator_thin._submission_state_path(args)
    reserved = validator_thin._read_state(journal_path)
    assert reserved["submission_pending_phase"] == "unsigned_reserved"
    assert reserved.get("submission_attempt_ids", []) == []
    assert reserved.get("submission_launch_attempt_ids", []) == []
    assert reserved.get("submission_attempt_budgets", {}) == {}
    assert reserved.get("submission_highest_policy_version") is None

    assert validator_thin._abort_unsigned_common_submission(
        args,
        attempt_id=attempt_id,
    )
    abandoned = validator_thin._read_state(journal_path)
    assert abandoned["submission_pending_id"] is None
    assert abandoned.get("submission_attempt_ids", []) == []
    assert abandoned.get("submission_launch_attempt_ids", []) == []

    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id=attempt_id,
        identity=identity,
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=attempt_id,
        extrinsic_hash="0x" + "a" * 64,
        nonce=17,
        era_reference_block=100,
        mortal_period_blocks=4,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[7, 241],
        wire_weights=[65535, 7282],
    )
    signed = validator_thin._read_state(journal_path)
    assert signed["submission_pending_phase"] == "signed_intent"
    assert signed["submission_attempt_ids"] == [attempt_id]
    assert signed["submission_launch_attempt_ids"] == [attempt_id]
    assert signed["submission_highest_policy_version"] == 7
    assert signed["submission_attempt_budgets"]["launch_full_gate"] == {
        "limit": 1,
        "ids": [attempt_id],
    }
    assert not validator_thin._abort_unsigned_common_submission(
        args,
        attempt_id=attempt_id,
    )


def test_receipt_block_number_requires_canonical_hash_height_round_trip() -> None:
    block_hash = "0x" + "b" * 64
    good = SimpleNamespace(
        substrate=SimpleNamespace(
            get_block_number=lambda value: 101 if value == block_hash else None,
            get_block_hash=lambda number: block_hash if number == 101 else None,
        )
    )
    assert validator_thin._canonical_receipt_block_number(good, block_hash) == 101

    bad = SimpleNamespace(
        substrate=SimpleNamespace(
            get_block_number=lambda _value: 101,
            get_block_hash=lambda _number: "0x" + "c" * 64,
        )
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="canonical height disagree",
    ):
        validator_thin._canonical_receipt_block_number(bad, block_hash)


def test_restart_retires_exhaustively_absent_attempt_but_preserves_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_attempt = "sha256:" + "1" * 64
    later_attempt = "sha256:" + "2" * 64
    policy = validator_thin.InclusionPolicy(
        valid_from_block=900,
        valid_until_block=904,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2026, 7, 24, 23, 0, tzinfo=UTC),
        expected_next_epoch_start_block=1100,
    )
    authorization = validator_thin.ContinuousAuthorization(
        authorization_sha256="sha256:" + "3" * 64,
        submission_journal=str(tmp_path / "journal.json"),
        launch_attempt_id="sha256:" + "4" * 64,
        release_sha256="sha256:" + "5" * 64,
        reproducer_revision="6" * 40,
        validator_hotkey="validator",
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        lanes=("thin",),
        issued_at="2026-07-24T21:00:00.000Z",
        valid_from_time="2026-07-24T21:00:00.000Z",
        valid_until_time="2026-07-25T21:00:00.000Z",
        valid_from_block=800,
        valid_until_block=1200,
        valid_from_nonce=17,
        valid_until_nonce_exclusive=19,
        max_attempts=2,
    )
    args = SimpleNamespace(
        broadcast=True,
        offline=False,
        network="finney",
        netuid=39,
        wallet_name="validator",
        wallet_hotkey="default",
        max_submissions=2,
        require_full_provenance_for_broadcast=False,
        require_completed_launch_for_broadcast=True,
        runtime_root=str(tmp_path / "runtime"),
        state_file=str(tmp_path / "thin.json"),
        _submission_validator_hotkey="validator",
        _submission_genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        _continuous_submission_authorization=authorization,
    )
    wire_uids, wire_weights = validator_thin._wire_weights([7, 241], [0.9, 0.1])

    def identity(policy_version: int) -> dict[str, object]:
        return {
            "network": "finney",
            "netuid": 39,
            "validator_hotkey": "validator",
            "mapping_block": 900,
            "policy_version": policy_version,
            "vector_id": f"vector-{policy_version}",
            "signed_vector_sha256": "sha256:" + "7" * 64,
            "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
            "uid_weights": [[7, 0.9], [241, 0.1]],
            "uid_hotkeys": [
                [7, "worker"],
                [241, validator_thin.SN39_BURN_HOTKEY],
            ],
            "next_epoch_start_block": 1100,
            "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
            "continuous_authorization": (
                validator_thin._continuous_authorization_identity(authorization)
            ),
        }

    first_identity = identity(8)
    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id=first_attempt,
        identity=first_identity,
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=first_attempt,
        extrinsic_hash="0x" + "a" * 64,
        nonce=17,
        era_reference_block=900,
        mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        version_key=validator_thin._weight_version_key(),
        wire_uids=wire_uids,
        wire_weights=wire_weights,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "worker": 7,
            "validator": 30,
            validator_thin.SN39_BURN_HOTKEY: 241,
        },
        validator_hotkey="validator",
        validator_uid=30,
        block=905,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda runtime: setattr(runtime, "_tick_preflight", preflight),
    )
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_locate_pending_broadcast_receipt",
        lambda *_args, **_kwargs: (
            validator_thin.EXPIRED_WITHOUT_INCLUSION,
            None,
        ),
    )

    assert validator_thin._recover_pending_launch_receipt(args) is None
    journal_path = validator_thin._submission_state_path(args)
    retired = validator_thin._read_state(journal_path)
    budget_scope = authorization.authorization_sha256.removeprefix("sha256:")
    assert retired["submission_pending_id"] is None
    assert retired["submission_expired_status"] == "expired_without_inclusion"
    assert retired["submission_expired_id"] == first_attempt
    assert retired["submission_attempt_ids"] == [first_attempt]
    assert retired["submission_attempt_budgets"][budget_scope] == {
        "limit": 2,
        "ids": [first_attempt],
    }
    assert retired["submission_highest_policy_version"] == 8

    with pytest.raises(ValueError, match="already attempted"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id=first_attempt,
            identity=first_identity,
        )

    validator_thin._reserve_common_submission(
        args,
        lane="thin",
        attempt_id=later_attempt,
        identity=identity(9),
    )
    later = validator_thin._read_state(journal_path)
    assert later["submission_pending_id"] == later_attempt
    assert later["submission_pending_phase"] == "unsigned_reserved"
    assert later["submission_attempt_ids"] == [first_attempt]
    assert later["submission_attempt_budgets"][budget_scope]["ids"] == [first_attempt]
