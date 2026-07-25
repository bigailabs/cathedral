from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
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
    metagraph = SimpleNamespace(
        uids=[0, 30, 163],
        hotkeys=["burn-hotkey", "validator-hotkey", "tdx-miner"],
        validator_permit=[False, True, False],
        block=8680424,
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

    metagraph.validator_permit[1] = False
    with pytest.raises(validator_thin.wire.VectorError, match="lacks validator permit"):
        validator_thin.chain_preflight(
            network="finney",
            netuid=38,
            wallet_name="cathedral",
            wallet_hotkey="default",
        )


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
            receipt_block_hash if block_number == 8680430 else "0x" + "0" * 64
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


@pytest.mark.parametrize(
    ("commit_reveal", "timestamp_ms", "block_number", "expected"),
    [
        (False, 1784932200000, 901, True),
        (True, 1784932200000, 901, False),
        (False, 1784934000000, 901, False),
        (False, 1784932200000, 950, False),
    ],
)
def test_finalized_receipt_binds_policy_to_actual_inclusion_block(
    commit_reveal: bool,
    timestamp_ms: int,
    block_number: int,
    expected: bool,
) -> None:
    extrinsic_hash = "0x" + "a" * 64
    block_hash = "0x" + "d" * 64
    finalized_hash = "0x" + "e" * 64
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: finalized_hash,
        get_block_number=lambda _value: max(1000, block_number),
        get_block_hash=lambda _value: block_hash,
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
        query=lambda **_kw: timestamp_ms,
    )
    metagraph = SimpleNamespace(
        uids=[30, 163, 204],
        hotkeys=["validator-hotkey", "tdx-miner", "burn-hotkey"],
        block=block_number,
    )
    subtensor = SimpleNamespace(
        substrate=substrate,
        metagraph=lambda _netuid, block: metagraph,
        commit_reveal_enabled=lambda **_kw: commit_reveal,
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
    with pytest.raises(validator_thin.wire.VectorError, match="finalized-head"):
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
    with pytest.raises(validator_thin.wire.VectorError, match="release-grade"):
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
