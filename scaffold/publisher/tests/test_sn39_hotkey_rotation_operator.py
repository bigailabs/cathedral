from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import sn39_hotkey_rotation_operator as rotation

COLDKEY = "5" + "C" * 47
OLD_HOTKEY = "5" + "D" * 47
NEW_HOTKEY = "5" + "E" * 47
OWNER_HOTKEY = "5" + "F" * 47
FINALIZED_HASH = "0x" + "a" * 64
RECEIPT_BLOCK_HASH = "0x" + "b" * 64
EXTRINSIC_HASH = "0x" + "c" * 64
GENESIS_HASH = rotation.FINNEY_GENESIS_HASH


class _ScaleBytes:
    def to_hex(self) -> str:
        return "0x0a4801020304"


class _Call:
    data = _ScaleBytes()


class _Key:
    def __init__(self, address: str) -> None:
        self.ss58_address = address

    def verify(self, data: bytes, signature: str) -> bool:
        return (
            data.startswith(b"cathedral-sn39-new-hotkey-possession-v1:")
            and signature == "0x" + "42" * 64
        )


class _ProofKey(_Key):
    def __init__(self, address: str) -> None:
        super().__init__(address)
        self.sign_count = 0
        self.verifies = True

    def sign(self, data: bytes) -> bytes:
        assert data.startswith(b"cathedral-sn39-new-hotkey-possession-v1:")
        self.sign_count += 1
        return b"\x42" * 64

    def verify(self, data: bytes, signature: str) -> bool:
        assert data.startswith(b"cathedral-sn39-new-hotkey-possession-v1:")
        assert signature == "0x" + "42" * 64
        return self.verifies


class _Wallet:
    def __init__(self) -> None:
        self.coldkeypub = _Key(COLDKEY)
        self._coldkey = _Key(COLDKEY)
        self.unlock_count = 0
        self.unlock_exception: Exception | None = None

    @property
    def coldkey(self) -> _Key:
        self.unlock_count += 1
        if self.unlock_exception is not None:
            raise self.unlock_exception
        return self._coldkey


class _NewWallet:
    def __init__(self) -> None:
        self.hotkeypub = _Key(NEW_HOTKEY)
        self._hotkey = _ProofKey(NEW_HOTKEY)
        self.unlock_count = 0

    @property
    def hotkey(self) -> _ProofKey:
        self.unlock_count += 1
        return self._hotkey


class _Signed:
    class _Hash:
        def hex(self) -> str:
            return EXTRINSIC_HASH[2:]

    extrinsic_hash = _Hash()


class _Receipt:
    extrinsic_hash = EXTRINSIC_HASH
    block_hash = RECEIPT_BLOCK_HASH
    block_number = 101
    extrinsic_idx = 4
    finalized = True
    is_success = True
    error_message = None
    total_fee_amount = 80
    triggered_events = [
        {
            "event": {
                "module_id": "SubtensorModule",
                "event_id": "HotkeySwappedOnSubnet",
                "attributes": {
                    "coldkey": COLDKEY,
                    "old_hotkey": OLD_HOTKEY,
                    "new_hotkey": NEW_HOTKEY,
                    "netuid": 39,
                },
            }
        }
    ]


class _Substrate:
    def __init__(self) -> None:
        self.compose_calls: list[dict[str, object]] = []
        self.sign_calls: list[dict[str, object]] = []
        self.submit_calls: list[dict[str, object]] = []
        self.payment_calls: list[dict[str, object]] = []
        self.raise_on_submit = False
        self.receipt = _Receipt()
        self.timestamp_ms = 1_753_400_000_000
        self.runtime_version = {
            "specVersion": 322,
            "transactionVersion": 1,
        }
        self.account_nonce = 12
        self.finalized_block = 100
        self.best_block = 100
        self.canonical_call_mutation: str | None = None
        self.historical_success = True
        self.historical_index = 4
        self.historical_total_fee_rao: int | None = 80
        self.transaction_fee_rao = 100
        self.key_swap_cost_rao = 500
        self.coldkey_balance_rao = 1_000_000
        self.mutate_key_swap_cost_after_payment = False

    def get_chain_finalised_head(self) -> str:
        return self.get_block_hash(self.finalized_block)

    def get_chain_head(self) -> str:
        return self.get_block_hash(self.best_block)

    def get_block_number(self, block_hash: str) -> int:
        known = {
            GENESIS_HASH: 0,
            FINALIZED_HASH: 100,
            RECEIPT_BLOCK_HASH: 101,
        }
        if block_hash in known:
            return known[block_hash]
        return int(block_hash[2:], 16)

    def get_block_hash(self, block: int) -> str:
        known = {
            0: GENESIS_HASH,
            100: FINALIZED_HASH,
            101: RECEIPT_BLOCK_HASH,
        }
        return known.get(block, "0x" + f"{block:064x}")

    def get_account_next_index(self, address: str) -> int:
        assert address == COLDKEY
        return self.account_nonce

    def get_block_runtime_version(self, block_hash: str) -> dict[str, int]:
        assert block_hash == self.get_block_hash(self.get_block_number(block_hash))
        return dict(self.runtime_version)

    def compose_call(self, **kwargs):
        self.compose_calls.append(kwargs)
        return _Call()

    def create_signed_extrinsic(self, **kwargs):
        self.sign_calls.append(kwargs)
        return _Signed()

    def submit_extrinsic(self, signed, **kwargs):
        self.submit_calls.append({"signed": signed, **kwargs})
        if self.raise_on_submit:
            raise TimeoutError("response lost after request")
        return self.receipt

    def get_block(self, *, block_hash: str):
        if block_hash != RECEIPT_BLOCK_HASH:
            return {"extrinsics": []}
        call_args = [
            {"name": "hotkey", "value": OLD_HOTKEY},
            {"name": "new_hotkey", "value": NEW_HOTKEY},
            {"name": "netuid", "value": 39},
            {"name": "keep_stake", "value": True},
        ]
        observed = {
            "extrinsic_hash": EXTRINSIC_HASH,
            "address": COLDKEY,
            "call": {
                "call_module": "SubtensorModule",
                "call_function": "swap_hotkey_v2",
                "call_args": call_args,
            },
        }
        if self.canonical_call_mutation == "signer":
            observed["address"] = OWNER_HOTKEY
        elif self.canonical_call_mutation == "new_hotkey":
            call_args[1]["value"] = OWNER_HOTKEY
        elif self.canonical_call_mutation == "hash":
            observed["extrinsic_hash"] = "0x" + "d" * 64
        return {
            "extrinsics": [
                SimpleNamespace(value={"extrinsic_hash": "0x" + f"{index:x}" * 64})
                for index in range(4)
            ]
            + [SimpleNamespace(value=observed)]
        }

    def retrieve_extrinsic_by_hash(
        self,
        block_hash: str,
        extrinsic_hash: str,
    ):
        assert block_hash == RECEIPT_BLOCK_HASH
        assert extrinsic_hash == EXTRINSIC_HASH
        return SimpleNamespace(
            is_success=self.historical_success,
            error_message=None if self.historical_success else "dispatch failed",
            extrinsic_idx=self.historical_index,
            triggered_events=list(_Receipt.triggered_events),
            total_fee_amount=self.historical_total_fee_rao,
        )

    def get_payment_info(self, *, call, keypair, nonce, era, tip):
        self.payment_calls.append(
            {
                "call": call,
                "keypair": keypair,
                "nonce": nonce,
                "era": era,
                "tip": tip,
            }
        )
        assert isinstance(call, _Call)
        assert keypair.ss58_address == COLDKEY
        if self.mutate_key_swap_cost_after_payment:
            self.key_swap_cost_rao += 1
        return {"partial_fee": self.transaction_fee_rao}

    def query(self, **kwargs):
        if kwargs.get("module") == "System":
            assert kwargs == {
                "module": "System",
                "storage_function": "Account",
                "params": [COLDKEY],
                "block_hash": self.get_block_hash(self.finalized_block),
            }
            return SimpleNamespace(value={"data": {"free": self.coldkey_balance_rao}})
        assert kwargs == {
            "module": "Timestamp",
            "storage_function": "Now",
            "block_hash": RECEIPT_BLOCK_HASH,
        }
        return SimpleNamespace(value=self.timestamp_ms)


class _Subtensor:
    def __init__(self, substrate: _Substrate) -> None:
        self.substrate = substrate
        self.owner_hotkey = OWNER_HOTKEY
        self.owners = {
            OLD_HOTKEY: COLDKEY,
            NEW_HOTKEY: rotation.UNOWNED_HOTKEY_OWNER,
            OWNER_HOTKEY: COLDKEY,
        }
        self.post_values = {
            ("Owner", (NEW_HOTKEY,)): COLDKEY,
            ("LastHotkeySwapOnNetuid", (39, COLDKEY)): 101,
            ("HotkeySuccessor", (39, NEW_HOTKEY)): None,
            ("HotkeyRoot", (39, NEW_HOTKEY)): OLD_HOTKEY,
            ("HotkeySuccessor", (39, OLD_HOTKEY)): NEW_HOTKEY,
            ("HotkeyRoot", (39, OLD_HOTKEY)): None,
            ("ColdkeySwapAnnouncements", (COLDKEY,)): None,
        }

    def metagraph(self, netuid: int, *, block: int):
        assert netuid == 39
        if block != 101:
            return SimpleNamespace(
                block=block,
                uids=[7, 241],
                hotkeys=[OLD_HOTKEY, OWNER_HOTKEY],
            )
        return SimpleNamespace(
            block=101,
            uids=[7, 241],
            hotkeys=[NEW_HOTKEY, OWNER_HOTKEY],
        )

    def get_subnet_owner_hotkey(self, netuid: int, *, block: int) -> str:
        assert netuid == 39
        assert block > 0
        return self.owner_hotkey

    def query_subtensor(self, *, name: str, params: list[str], block: int):
        if block != 101:
            if name == "Owner":
                return SimpleNamespace(value=self.owners[params[0]])
            if name == "KeySwapOnSubnetCost":
                assert params == []
                return SimpleNamespace(value=self.substrate.key_swap_cost_rao)
            assert (name, params) == (
                "ColdkeySwapAnnouncements",
                [COLDKEY],
            )
            return SimpleNamespace(value=None)
        assert block == 101
        return SimpleNamespace(value=self.post_values[(name, tuple(params))])


@pytest.fixture(autouse=True)
def canonical_rotation_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    state_root = tmp_path / "rotation-state"
    state_root.mkdir(mode=0o755)
    directory = state_root / f"uid-{os.geteuid()}"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(rotation, "AUTHORITY_STATE_ROOT", state_root)
    monkeypatch.setattr(rotation, "ROOT_UID", os.getuid())
    return directory


def _runtime() -> rotation.Runtime:
    substrate = _Substrate()
    return rotation.Runtime(
        wallet=_Wallet(),
        new_wallet=_NewWallet(),
        subtensor=_Subtensor(substrate),
        substrate=substrate,
    )


def _options() -> rotation.Options:
    execution_context = {
        "schema": rotation.CONTEXT_SCHEMA,
        "source_sha": "a" * 40,
        "manifest_sha256": "sha256:" + "1" * 64,
        "bundle_tree_sha256": "sha256:" + "2" * 64,
        "venv_tree_sha256": "sha256:" + "3" * 64,
        "launcher_sha256": "sha256:" + "4" * 64,
        "authority_host": os.uname().nodename,
        "authority_uid": os.geteuid(),
        "authority_home": pwd.getpwuid(os.geteuid()).pw_dir,
        "authority_state_dir": str(
            rotation.AUTHORITY_STATE_ROOT / f"uid-{os.geteuid()}"
        ),
    }
    return rotation.Options(
        wallet_name="validator",
        new_wallet_name="launch",
        new_wallet_hotkey="rewarded",
        authority_host=os.uname().nodename,
        authority_uid=os.geteuid(),
        max_transaction_fee_rao=1_000,
        execution_context=execution_context,
        expected_coldkey=COLDKEY,
        old_hotkey=OLD_HOTKEY,
        new_hotkey=NEW_HOTKEY,
        expected_uid=7,
        role="rewarded",
        keep_stake=True,
    )


def _broadcast_options(
    inspected: dict[str, object],
    _tmp_path: Path,
) -> rotation.Options:
    state_name, receipt_name = rotation._artifact_names(_options())
    return replace(
        _options(),
        broadcast=True,
        confirmation_digest=str(inspected["confirmation_digest"]),
        state_file=rotation._canonical_attempt_dir(_options()) / state_name,
        receipt_out=rotation._canonical_attempt_dir(_options()) / receipt_name,
        reviewed_finalized_block=int(inspected["approval"]["reviewed_finalized_block"]),
        reviewed_finalized_hash=str(inspected["approval"]["reviewed_finalized_hash"]),
        reviewed_coldkey_nonce=int(inspected["approval"]["reviewed_coldkey_nonce"]),
        approval_valid_until_block=int(
            inspected["approval"]["approval_valid_until_block"]
        ),
    )


def _reconcile_options(options: rotation.Options) -> rotation.Options:
    return replace(
        options,
        broadcast=False,
        reconcile=True,
        confirmation_digest=None,
        reviewed_finalized_block=None,
        reviewed_finalized_hash=None,
        reviewed_coldkey_nonce=None,
        approval_valid_until_block=None,
    )


def test_default_mode_composes_without_unlock_sign_or_submit() -> None:
    runtime = _runtime()

    result = rotation.execute(_options(), runtime=runtime)

    assert result["status"] == "INSPECT_ONLY"
    assert result["chain_write"] is False
    assert result["signing"] is False
    assert result["approval"] == {
        "schema": rotation.REVIEW_SCHEMA,
        "execution_bundle": _options().execution_context,
        "network": "finney",
        "genesis_hash": GENESIS_HASH,
        "runtime_spec_version": 322,
        "runtime_transaction_version": 1,
        "authority_host": os.uname().nodename,
        "authority_uid": os.geteuid(),
        "netuid": 39,
        "role": "rewarded",
        "signer_coldkey": COLDKEY,
        "old_hotkey": OLD_HOTKEY,
        "new_hotkey": NEW_HOTKEY,
        "expected_uid": 7,
        "keep_stake": True,
        "era_period_blocks": rotation.ERA_PERIOD_BLOCKS,
        "reviewed_finalized_block": 100,
        "reviewed_finalized_hash": FINALIZED_HASH,
        "reviewed_coldkey_nonce": 12,
        "approval_valid_until_block": 100 + rotation.APPROVAL_LIFETIME_BLOCKS,
        "key_swap_cost_rao": 500,
        "coldkey_free_balance_rao": 1_000_000,
        "reviewed_transaction_fee_estimate_ceiling_rao": 1_000,
        "reviewed_maximum_estimated_spend_rao": 1_500,
        "on_chain_spend_cap_enforced": False,
        "cost_authorization_model": rotation.COST_AUTHORIZATION_MODEL,
        "call": "SubtensorModule.swap_hotkey_v2",
        "call_hex": "0x0a4801020304",
    }
    assert result["observation"]["old_uid"] == 7
    assert result["confirmation_digest"] == rotation._digest(result["approval"])
    state_name, receipt_name = rotation._artifact_names(_options())
    assert result["attempt_scope"] == {
        "id": rotation._target_id(_options()),
        "state_filename": state_name,
        "receipt_filename": receipt_name,
    }
    assert runtime.wallet.unlock_count == 0
    assert runtime.new_wallet.unlock_count == 0
    assert len(runtime.substrate.compose_calls) == 1
    assert runtime.substrate.compose_calls[0]["block_hash"] == FINALIZED_HASH
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []
    assert runtime.substrate.payment_calls == []


def test_confirmation_digest_binds_exact_executable_bundle() -> None:
    first = rotation.execute(_options(), runtime=_runtime())
    changed_context = dict(_options().execution_context or {})
    changed_context["bundle_tree_sha256"] = "sha256:" + "9" * 64
    changed = replace(_options(), execution_context=changed_context)
    second = rotation.execute(changed, runtime=_runtime())
    assert second["confirmation_digest"] != first["confirmation_digest"]

    old_approval = _broadcast_options(first, Path("/unused"))
    mismatched = replace(old_approval, execution_context=changed_context)
    runtime = _runtime()
    with pytest.raises(rotation.RotationError, match="confirmation digest differs"):
        rotation.execute(mismatched, runtime=runtime)
    assert runtime.wallet.unlock_count == 0
    assert runtime.new_wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_execution_context_is_mandatory_before_chain_access() -> None:
    runtime = _runtime()
    with pytest.raises(
        rotation.RotationError,
        match="execution context fields differ",
    ):
        rotation.execute(
            replace(_options(), execution_context=None),
            runtime=runtime,
        )
    assert runtime.substrate.compose_calls == []
    assert runtime.wallet.unlock_count == 0


def test_command_requires_python_isolated_mode_before_parsing_or_connecting() -> None:
    completed = subprocess.run(
        [sys.executable, "-B", str(Path(rotation.__file__)), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == (
        "SN39 rotation: FAIL: Python isolated mode (-I) is required"
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"authority_host": "not-this-authority-host"},
        {"authority_uid": os.geteuid() + 1},
    ),
)
def test_wrong_designated_authority_fails_before_chain_or_key_access(
    changes: dict[str, object],
) -> None:
    runtime = _runtime()

    with pytest.raises(
        rotation.RotationError,
        match="designated authority host and exact OS uid",
    ):
        rotation.execute(replace(_options(), **changes), runtime=runtime)

    assert runtime.wallet.unlock_count == 0
    assert runtime.new_wallet.unlock_count == 0
    assert runtime.substrate.compose_calls == []
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_broadcast_requires_separate_matching_digest_before_unlock(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = replace(
        _broadcast_options(inspected, tmp_path),
        confirmation_digest="sha256:" + "f" * 64,
    )

    with pytest.raises(rotation.RotationError, match="confirmation digest differs"):
        rotation.execute(options, runtime=runtime)

    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []
    assert not options.state_file.exists()
    assert not options.receipt_out.exists()


def test_broadcast_rejects_changed_nonce_bound_to_review_before_unlock(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.account_nonce += 1

    with pytest.raises(
        rotation.RotationError,
        match="snapshot, approval lifetime, or coldkey nonce changed",
    ):
        rotation.execute(options, runtime=runtime)

    assert runtime.new_wallet.unlock_count == 0
    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_broadcast_rejects_expired_review_snapshot_before_unlock(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.finalized_block = (
        int(inspected["approval"]["approval_valid_until_block"]) + 1
    )
    runtime.substrate.best_block = runtime.substrate.finalized_block

    with pytest.raises(
        rotation.RotationError,
        match="snapshot, approval lifetime, or coldkey nonce changed",
    ):
        rotation.execute(options, runtime=runtime)

    assert runtime.new_wallet.unlock_count == 0
    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_success_writes_exact_secret_free_receipt_and_final_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    result = rotation.execute(options, runtime=runtime)

    expected_keys = {
        "call",
        "extrinsic_hash",
        "block_hash",
        "block_number",
        "block_timestamp",
        "extrinsic_index",
        "coldkey",
        "old_hotkey",
        "new_hotkey",
        "netuid",
        "keep_stake",
        "event",
    }
    assert result["status"] == "PASS"
    assert result["chain_write"] is True
    assert set(result["receipt"]) == expected_keys
    assert result["receipt"] == {
        "call": "swap_hotkey_v2",
        "extrinsic_hash": EXTRINSIC_HASH,
        "block_hash": RECEIPT_BLOCK_HASH,
        "block_number": 101,
        "block_timestamp": "2025-07-24T23:33:20.000Z",
        "extrinsic_index": 4,
        "coldkey": COLDKEY,
        "old_hotkey": OLD_HOTKEY,
        "new_hotkey": NEW_HOTKEY,
        "netuid": 39,
        "keep_stake": True,
        "event": "HotkeySwappedOnSubnet",
    }
    receipt = json.loads(options.receipt_out.read_text())
    state = json.loads(options.state_file.read_text())
    assert receipt == result["receipt"]
    assert state["phase"] == "finalized"
    assert state["signed_intent"]["extrinsic_hash"] == EXTRINSIC_HASH
    assert state["signed_intent"]["era_reference_hash"] == FINALIZED_HASH
    assert state["economic_boundary"] == {
        "key_swap_cost_rao": 500,
        "estimated_transaction_fee_rao": 100,
        "reviewed_transaction_fee_estimate_ceiling_rao": 1_000,
        "reviewed_maximum_estimated_spend_rao": 1_500,
        "on_chain_spend_cap_enforced": False,
        "cost_authorization_model": rotation.COST_AUTHORIZATION_MODEL,
        "actual_transaction_fee_rao": 80,
        "actual_transaction_fee_source": (
            "submitted_receipt+canonical_historical_receipt"
        ),
        "actual_fee_within_reviewed_estimate_ceiling": True,
    }
    assert result["economic_boundary"] == state["economic_boundary"]
    assert (
        state["new_hotkey_possession"]["approval_digest"]
        == (result["confirmation_digest"])
    )
    assert state["new_hotkey_possession"]["signature"] == "0x" + "42" * 64
    assert state["receipt"] == receipt
    assert state["post_rotation_proof"]["uid"] == 7
    assert state["post_rotation_proof"]["old_hotkey_successor"] == NEW_HOTKEY
    assert state["receipt_sha256"] == rotation._digest(receipt)
    assert options.receipt_out.stat().st_mode & 0o777 == 0o600
    assert options.state_file.stat().st_mode & 0o777 == 0o600
    assert runtime.wallet.unlock_count == 1
    assert runtime.new_wallet.unlock_count == 1
    assert len(runtime.substrate.sign_calls) == 1
    assert runtime.substrate.sign_calls[0]["nonce"] == 12
    assert runtime.substrate.sign_calls[0]["era"] == {
        "period": rotation.ERA_PERIOD_BLOCKS,
        "current": 100,
    }
    assert runtime.substrate.sign_calls[0]["tip"] == 0
    assert len(runtime.substrate.payment_calls) == 1
    assert runtime.substrate.payment_calls[0]["nonce"] == 12
    assert runtime.substrate.payment_calls[0]["era"] == {
        "period": rotation.ERA_PERIOD_BLOCKS,
        "current": 100,
    }
    assert runtime.substrate.payment_calls[0]["tip"] == 0
    assert runtime.substrate.submit_calls[0]["wait_for_inclusion"] is True
    assert runtime.substrate.submit_calls[0]["wait_for_finalization"] is True


def test_estimated_fee_must_stay_below_approved_ceiling_before_intent_signing(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.transaction_fee_rao = options.max_transaction_fee_rao + 1

    with pytest.raises(rotation.RotationError, match="exceeds.*approved ceiling"):
        rotation.execute(options, runtime=runtime)

    assert runtime.new_wallet.unlock_count == 1
    assert runtime.wallet.unlock_count == 1
    assert len(runtime.substrate.payment_calls) == 1
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []
    assert not options.state_file.exists()
    assert not options.receipt_out.exists()


def test_runtime_economic_drift_after_exact_fee_estimate_fails_before_signing(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.mutate_key_swap_cost_after_payment = True

    with pytest.raises(
        rotation.RotationError,
        match="economic state differs",
    ):
        rotation.execute(options, runtime=runtime)

    assert len(runtime.substrate.payment_calls) == 1
    assert runtime.substrate.payment_calls[0]["nonce"] == 12
    assert runtime.substrate.payment_calls[0]["era"] == {
        "period": rotation.ERA_PERIOD_BLOCKS,
        "current": 100,
    }
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []
    assert not options.state_file.exists()
    assert not options.receipt_out.exists()


def test_finalized_receipt_fee_disagreement_is_not_proven(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    runtime.substrate.historical_total_fee_rao = 81
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationNotProven,
        match="disagree on the actual transaction fee",
    ):
        rotation.execute(options, runtime=runtime)

    assert json.loads(options.state_file.read_text())["phase"] == "broadcast_pending"
    assert not options.receipt_out.exists()
    assert len(runtime.substrate.submit_calls) == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("signer", "canonical call differs"),
        ("new_hotkey", "canonical call differs"),
        ("hash", "canonical block index"),
    ),
)
def test_canonical_rotation_call_mismatch_fails_and_keeps_attempt_fenced(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    runtime = _runtime()
    runtime.substrate.canonical_call_mutation = mutation
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(rotation.RotationError, match=message):
        rotation.execute(options, runtime=runtime)

    assert json.loads(options.state_file.read_text())["phase"] == "broadcast_pending"
    assert not options.receipt_out.exists()
    assert len(runtime.substrate.submit_calls) == 1


@pytest.mark.parametrize(
    ("historical_success", "historical_index"),
    ((False, 4), (True, 3)),
)
def test_historical_rotation_execution_must_match_success_and_index(
    tmp_path: Path,
    historical_success: bool,
    historical_index: int,
) -> None:
    runtime = _runtime()
    runtime.substrate.historical_success = historical_success
    runtime.substrate.historical_index = historical_index
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationError,
        match="failed or contradicts its historical execution",
    ):
        rotation.execute(options, runtime=runtime)

    assert json.loads(options.state_file.read_text())["phase"] == "broadcast_pending"
    assert not options.receipt_out.exists()
    assert len(runtime.substrate.submit_calls) == 1


def test_lost_submit_response_stays_fenced_and_cannot_be_retried(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.raise_on_submit = True

    with pytest.raises(rotation.RotationNotProven, match="may have broadcast"):
        rotation.execute(options, runtime=runtime)

    state = json.loads(options.state_file.read_text())
    assert state["phase"] == "broadcast_pending"
    assert state["signed_intent"]["extrinsic_hash"] == EXTRINSIC_HASH
    assert not options.receipt_out.exists()
    assert len(runtime.substrate.submit_calls) == 1

    signed_count = len(runtime.substrate.sign_calls)
    coldkey_unlocks = runtime.wallet.unlock_count
    new_hotkey_unlocks = runtime.new_wallet.unlock_count
    new_hotkey_signatures = runtime.new_wallet._hotkey.sign_count
    with pytest.raises(rotation.RotationError, match="cannot be retried"):
        rotation.execute(options, runtime=runtime)
    assert len(runtime.substrate.sign_calls) == signed_count
    assert len(runtime.substrate.submit_calls) == 1
    assert runtime.wallet.unlock_count == coldkey_unlocks
    assert runtime.new_wallet.unlock_count == new_hotkey_unlocks
    assert runtime.new_wallet._hotkey.sign_count == new_hotkey_signatures


def test_lost_submit_response_recovers_without_new_signing_or_submission(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    broadcast = _broadcast_options(inspected, tmp_path)
    runtime.substrate.raise_on_submit = True
    with pytest.raises(rotation.RotationNotProven, match="may have broadcast"):
        rotation.execute(broadcast, runtime=runtime)

    signed_count = len(runtime.substrate.sign_calls)
    submitted_count = len(runtime.substrate.submit_calls)
    coldkey_unlocks = runtime.wallet.unlock_count
    new_hotkey_unlocks = runtime.new_wallet.unlock_count
    runtime.substrate.finalized_block = (
        broadcast.reviewed_finalized_block + rotation.ERA_PERIOD_BLOCKS - 1
    )
    result = rotation.execute(
        _reconcile_options(broadcast),
        runtime=runtime,
    )

    assert result["status"] == "PASS"
    assert result["chain_write"] is False
    assert result["recovered_chain_write"] is True
    assert result["receipt"]["extrinsic_hash"] == EXTRINSIC_HASH
    assert result["economic_boundary"]["actual_transaction_fee_source"] == (
        "recovered_historical_receipt+canonical_historical_receipt"
    )
    assert json.loads(broadcast.state_file.read_text())["phase"] == "finalized"
    assert json.loads(broadcast.receipt_out.read_text()) == result["receipt"]
    assert len(runtime.substrate.sign_calls) == signed_count
    assert len(runtime.substrate.submit_calls) == submitted_count
    assert runtime.wallet.unlock_count == coldkey_unlocks
    assert runtime.new_wallet.unlock_count == new_hotkey_unlocks


def test_delayed_era_recovery_repins_original_human_reviewed_block(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    broadcast = _broadcast_options(inspected, tmp_path)
    runtime.substrate.finalized_block = 102
    runtime.substrate.best_block = 102
    runtime.substrate.raise_on_submit = True

    with pytest.raises(rotation.RotationNotProven, match="may have broadcast"):
        rotation.execute(broadcast, runtime=runtime)

    state = json.loads(broadcast.state_file.read_text())
    assert state["approval"]["reviewed_finalized_block"] == 100
    assert state["signed_intent"]["era_reference_block"] == 102
    assert state["signed_intent"][
        "era_reference_hash"
    ] == runtime.substrate.get_block_hash(102)
    signed_count = len(runtime.substrate.sign_calls)
    submitted_count = len(runtime.substrate.submit_calls)
    original_get_block_hash = runtime.substrate.get_block_hash

    def changed_reviewed_hash(block: int) -> str:
        if block == 100:
            return "0x" + "d" * 64
        return original_get_block_hash(block)

    runtime.substrate.get_block_hash = changed_reviewed_hash
    runtime.substrate.finalized_block = (
        state["signed_intent"]["era_reference_block"] + rotation.ERA_PERIOD_BLOCKS - 1
    )

    with pytest.raises(
        rotation.RotationError,
        match="exact approved canonical pinned Finney history",
    ):
        rotation.execute(_reconcile_options(broadcast), runtime=runtime)

    assert len(runtime.substrate.sign_calls) == signed_count
    assert len(runtime.substrate.submit_calls) == submitted_count


def test_recovery_rejects_changed_reviewed_finalized_hash_without_key_access(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    broadcast = _broadcast_options(inspected, tmp_path)
    runtime.substrate.raise_on_submit = True
    with pytest.raises(rotation.RotationNotProven, match="may have broadcast"):
        rotation.execute(broadcast, runtime=runtime)

    signed_count = len(runtime.substrate.sign_calls)
    submitted_count = len(runtime.substrate.submit_calls)
    coldkey_unlocks = runtime.wallet.unlock_count
    new_hotkey_unlocks = runtime.new_wallet.unlock_count
    new_hotkey_signatures = runtime.new_wallet._hotkey.sign_count
    original_get_block_hash = runtime.substrate.get_block_hash

    def changed_reviewed_hash(block: int) -> str:
        if block == int(inspected["approval"]["reviewed_finalized_block"]):
            return "0x" + "d" * 64
        return original_get_block_hash(block)

    runtime.substrate.get_block_hash = changed_reviewed_hash
    runtime.substrate.finalized_block = (
        broadcast.reviewed_finalized_block + rotation.ERA_PERIOD_BLOCKS - 1
    )

    with pytest.raises(
        rotation.RotationError,
        match="exact approved canonical pinned Finney history",
    ):
        rotation.execute(_reconcile_options(broadcast), runtime=runtime)

    assert len(runtime.substrate.sign_calls) == signed_count
    assert len(runtime.substrate.submit_calls) == submitted_count
    assert runtime.wallet.unlock_count == coldkey_unlocks
    assert runtime.new_wallet.unlock_count == new_hotkey_unlocks
    assert runtime.new_wallet._hotkey.sign_count == new_hotkey_signatures


def test_recovery_rejects_state_rebound_to_another_bundle(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    broadcast = _broadcast_options(inspected, tmp_path)
    runtime.substrate.raise_on_submit = True
    with pytest.raises(rotation.RotationNotProven):
        rotation.execute(broadcast, runtime=runtime)

    state = json.loads(broadcast.state_file.read_text())
    state["approval"]["execution_bundle"]["source_sha"] = "b" * 40
    state["confirmation_digest"] = rotation._digest(state["approval"])
    state["new_hotkey_possession"]["approval_digest"] = state["confirmation_digest"]
    broadcast.state_file.write_bytes(rotation._canonical(state) + b"\n")
    broadcast.state_file.chmod(0o600)

    with pytest.raises(
        rotation.RotationError,
        match="approved target or bundle",
    ):
        rotation.execute(
            _reconcile_options(broadcast),
            runtime=runtime,
        )
    assert len(runtime.substrate.submit_calls) == 1


def test_missing_exact_event_is_not_proven_and_keeps_pending_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    runtime.substrate.receipt = _Receipt()
    runtime.substrate.receipt.triggered_events = []
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationNotProven,
        match="no unique matching finalized event",
    ):
        rotation.execute(options, runtime=runtime)

    state = json.loads(options.state_file.read_text())
    assert state["phase"] == "broadcast_pending"
    assert not options.receipt_out.exists()


def test_non_finalized_receipt_is_not_proven_and_keeps_pending_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    runtime.substrate.receipt = _Receipt()
    runtime.substrate.receipt.finalized = False
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationNotProven,
        match="ambiguous finalized receipt",
    ):
        rotation.execute(options, runtime=runtime)

    state = json.loads(options.state_file.read_text())
    assert state["phase"] == "broadcast_pending"
    assert not options.receipt_out.exists()


def test_failed_receipt_for_another_hash_remains_not_proven(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    runtime.substrate.receipt = _Receipt()
    runtime.substrate.receipt.extrinsic_hash = "0x" + "d" * 64
    runtime.substrate.receipt.is_success = False
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationNotProven,
        match="ambiguous finalized receipt",
    ):
        rotation.execute(options, runtime=runtime)

    assert options.state_file.exists()
    assert not options.receipt_out.exists()


def test_runtime_change_invalidates_same_call_bytes_before_unlock(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.substrate.runtime_version["specVersion"] += 1

    with pytest.raises(rotation.RotationError, match="confirmation digest differs"):
        rotation.execute(options, runtime=runtime)

    assert runtime.new_wallet.unlock_count == 0
    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.submit_calls == []


def test_new_hotkey_possession_must_verify_before_coldkey_unlock(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    runtime.new_wallet._hotkey.verifies = False

    with pytest.raises(rotation.RotationError, match="proof-of-possession"):
        rotation.execute(options, runtime=runtime)

    assert runtime.new_wallet.unlock_count == 1
    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_coldkey_unlock_failure_is_sanitized_before_state_sign_or_submit(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    secret = "fake mnemonic must never escape"
    runtime.wallet.unlock_exception = RuntimeError(secret)

    with pytest.raises(rotation.RotationError, match="cannot unlock") as caught:
        rotation.execute(options, runtime=runtime)

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert not options.state_file.exists()
    assert not options.receipt_out.exists()
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


@pytest.mark.parametrize("timestamp_ms", (253_402_300_800_000, 10**1000))
def test_invalid_finalized_timestamp_is_not_proven_and_keeps_attempt_fenced(
    tmp_path: Path,
    timestamp_ms: int,
) -> None:
    runtime = _runtime()
    runtime.substrate.timestamp_ms = timestamp_ms
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(
        rotation.RotationNotProven,
        match="invalid finalized timestamp",
    ):
        rotation.execute(options, runtime=runtime)

    assert json.loads(options.state_file.read_text())["phase"] == ("broadcast_pending")
    assert not options.receipt_out.exists()
    assert len(runtime.substrate.submit_calls) == 1


def test_post_rotation_lineage_mismatch_preserves_receipt_and_fences_attempt(
    tmp_path: Path,
) -> None:
    runtime = _runtime()
    runtime.subtensor.post_values[("HotkeySuccessor", (39, OLD_HOTKEY))] = OWNER_HOTKEY
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)

    with pytest.raises(rotation.RotationError, match="does not preserve"):
        rotation.execute(options, runtime=runtime)

    assert json.loads(options.receipt_out.read_text())["extrinsic_hash"] == (
        EXTRINSIC_HASH
    )
    assert json.loads(options.state_file.read_text())["phase"] == ("broadcast_pending")


def test_role_and_owner_checks_reject_the_wrong_target() -> None:
    runtime = _runtime()

    with pytest.raises(rotation.RotationError, match="does not name"):
        rotation.execute(
            replace(_options(), role="owner-burn"),
            runtime=runtime,
        )
    runtime.subtensor.owners[OLD_HOTKEY] = OWNER_HOTKEY
    with pytest.raises(rotation.RotationError, match="does not own"):
        rotation.execute(_options(), runtime=runtime)
    with pytest.raises(rotation.RotationError, match="expected-uid"):
        rotation.execute(
            replace(_options(), expected_uid=8),
            runtime=_runtime(),
        )


def test_non_finney_genesis_is_rejected_before_unlock() -> None:
    runtime = _runtime()
    original_get_block_hash = runtime.substrate.get_block_hash

    def wrong_genesis(block: int) -> str:
        if block == 0:
            return "0x" + "f" * 64
        return original_get_block_hash(block)

    runtime.substrate.get_block_hash = wrong_genesis

    with pytest.raises(rotation.RotationError, match="pinned Finney genesis"):
        rotation.execute(_options(), runtime=runtime)

    assert runtime.wallet.unlock_count == 0
    assert runtime.substrate.sign_calls == []
    assert runtime.substrate.submit_calls == []


def test_broadcast_rejects_relative_or_reused_output_paths(tmp_path: Path) -> None:
    runtime = _runtime()
    inspected = rotation.execute(_options(), runtime=runtime)
    options = _broadcast_options(inspected, tmp_path)
    options.state_file.write_text("{}")
    options.state_file.chmod(0o600)

    with pytest.raises(rotation.RotationError, match="cannot be retried"):
        rotation.execute(options, runtime=runtime)
    with pytest.raises(rotation.RotationError, match="must be absolute"):
        rotation.execute(
            replace(
                options,
                state_file=Path(options.state_file.name),
                receipt_out=Path(options.receipt_out.name),
            ),
            runtime=runtime,
        )
    with pytest.raises(rotation.RotationError, match="deterministic"):
        rotation.execute(
            replace(
                options,
                state_file=tmp_path / "different.attempt.json",
                receipt_out=tmp_path / "different.receipt.json",
            ),
            runtime=runtime,
        )
    second_directory = tmp_path / "second-owner-directory"
    second_directory.mkdir(mode=0o700)
    with pytest.raises(rotation.RotationError, match="canonical"):
        rotation.execute(
            replace(
                options,
                state_file=second_directory / options.state_file.name,
                receipt_out=second_directory / options.receipt_out.name,
            ),
            runtime=runtime,
        )
    assert runtime.substrate.submit_calls == []
