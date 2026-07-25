from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scaffold import validator_thin


def _args(tmp_path: Path, approval: Path) -> SimpleNamespace:
    return SimpleNamespace(
        network="finney",
        netuid=39,
        publisher_url=validator_thin.SN39_PUBLISHER_URL,
        public_key_hex=validator_thin.DEFAULT_PUBLIC_KEY_HEX,
        key_id=validator_thin.SN39_WEIGHT_POLICY_KEY_ID,
        require_policy="validated_supply_v1",
        state_file=str(tmp_path / "thin-state.json"),
        runtime_root=str(tmp_path / "runtime"),
        wallet_name="validator",
        wallet_hotkey="default",
        evidence_url=validator_thin.SN39_EVIDENCE_URL,
        provenance_registry_keys="config/provenance/registry-keys.json",
        provenance_registry_keys_digest=validator_thin.SN39_REGISTRY_KEYS_DIGEST,
        provenance_report_keys="config/provenance/report-keys.json",
        provenance_report_keys_digest=validator_thin.SN39_REPORT_KEYS_DIGEST,
        provenance_index_keys="config/provenance/index-keys.json",
        provenance_index_keys_digest=validator_thin.SN39_INDEX_KEYS_DIGEST,
        provenance_verifier_digest=validator_thin.SN39_VERIFIER_DIGEST,
        provenance_source_revision=validator_thin.SN39_PRODUCER_REVISION,
        provenance_mechanism=validator_thin.MECHANISM_DEFAULT,
        provenance_burn_hotkey=validator_thin.SN39_BURN_HOTKEY,
        provenance_controlled_dir=str(validator_thin.SN39_LAUNCH_CONTROLLED_DIR),
        provenance_verifier_binary=str(validator_thin.SN39_LAUNCH_VERIFIER_BINARY),
        launch_approval_file=str(approval),
        launch_release_sha="a" * 40,
        launch_config_sha256="sha256:" + "b" * 64,
        require_full_provenance_for_broadcast=True,
        require_completed_launch_for_broadcast=False,
        launch_preflight=False,
        broadcast=True,
        offline=False,
        once=True,
        max_submissions=1,
        provenance="shadow",
        jsonl=None,
    )


def _payload() -> dict:
    return {
        "vector_id": "launch-vector",
        "policy_version": 17,
        "weights": [
            {
                "miner_hotkey": "tdx-miner",
                "weight": 1.0,
                "base_component": 0.0,
                "external_component": 1.0,
            }
        ],
        "burn_snapshot": {
            "burn_uid": None,
            "burn_hotkey": validator_thin.SN39_BURN_HOTKEY,
            "forced_burn_percentage": 10.0,
        },
    }


def _audit() -> SimpleNamespace:
    return SimpleNamespace(
        status="PASS",
        assurance="full",
        agrees_with_vector=True,
        recomputed={"tdx-miner": 1.0},
        receipt_hotkeys=["tdx-miner"],
        raw_replayed_hotkeys=["tdx-miner"],
        source_epoch=91,
        report_id="sha256:" + "1" * 64,
        manifest_digest="sha256:" + "2" * 64,
        policy_release=11,
        policy_digest="sha256:" + "3" * 64,
        mechanism="validated_supply_v1",
        verifier_binary_digest="sha256:" + "4" * 64,
        report_signing_key_id="cathedral-score-report",
        signed_index={"digest": "sha256:" + "5" * 64},
    )


def _preflight(*, block: int, hashes: dict[int, str], signer: str = "validator-hotkey"):
    substrate = SimpleNamespace(get_block_hash=lambda number: hashes[number])
    mapping = {
        "tdx-miner": 163,
        validator_thin.SN39_BURN_HOTKEY: 204,
        "validator-hotkey": 30,
    }
    if signer != "validator-hotkey":
        mapping[signer] = 31
    return validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid=mapping,
        validator_hotkey=signer,
        validator_uid=mapping[signer],
        block=block,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey=validator_thin.SN39_BURN_HOTKEY,
        finalized_hash=hashes[block],
    )


@pytest.fixture
def approval_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    approval = tmp_path / "sn39-launch-approval.json"
    monkeypatch.setattr(validator_thin, "SN39_LAUNCH_APPROVAL_FILE", approval)
    monkeypatch.setattr(validator_thin, "SN39_LAUNCH_APPROVAL_OWNER_UID", os.geteuid())
    monkeypatch.setattr(validator_thin, "_weight_version_key", lambda: 39)
    args = _args(tmp_path, approval)
    hashes = {
        100: "0x" + "1" * 64,
        104: "0x" + "2" * 64,
        161: "0x" + "3" * 64,
    }
    reviewed = _preflight(block=100, hashes=hashes)
    runtime = _preflight(block=104, hashes=hashes)
    payload = _payload()
    audit = _audit()
    weights = {163: 0.9, 204: 0.1}
    document = validator_thin._build_launch_approval(
        args,
        payload=payload,
        audit=audit,
        preflight=reviewed,
        uid_weights=weights,
        hotkey_to_uid=reviewed.hotkey_to_uid,
    )
    validator_thin._write_root_launch_approval(approval, document)
    return args, approval, hashes, payload, audit, weights, document, runtime


def test_root_approval_is_canonical_and_exact_runtime_consumes_it(
    approval_fixture,
) -> None:
    args, approval, _hashes, payload, audit, weights, document, runtime = (
        approval_fixture
    )
    assert approval.read_bytes().endswith(b"\n")
    assert validator_thin._read_root_launch_approval(approval) == document
    consumed = validator_thin._require_launch_approval(
        args,
        payload=payload,
        audit=audit,
        preflight=runtime,
        uid_weights=weights,
        hotkey_to_uid=runtime.hotkey_to_uid,
    )
    assert consumed["approval_digest"] == document["approval_digest"]


def test_tamper_stale_head_vector_signer_and_missing_approval_all_stop_before_writer(
    approval_fixture,
) -> None:
    args, approval, hashes, payload, audit, weights, document, runtime = (
        approval_fixture
    )

    def refused(
        *,
        candidate_payload=payload,
        candidate_preflight=runtime,
    ) -> None:
        writes: list[str] = []
        with pytest.raises(validator_thin.wire.VectorError):
            validator_thin._require_launch_approval(
                args,
                payload=candidate_payload,
                audit=audit,
                preflight=candidate_preflight,
                uid_weights=weights,
                hotkey_to_uid=candidate_preflight.hotkey_to_uid,
            )
            writes.append("writer")
        assert writes == []

    tampered = dict(document)
    tampered["approval_valid_until_block"] += 1
    approval.write_bytes(validator_thin._canonical_json_bytes(tampered) + b"\n")
    refused()

    validator_thin._write_root_launch_approval(approval, document)
    refused(candidate_preflight=_preflight(block=161, hashes=hashes))

    validator_thin._write_root_launch_approval(approval, document)
    bad_head = dict(hashes)
    bad_head[100] = "0x" + "f" * 64
    refused(candidate_preflight=_preflight(block=104, hashes=bad_head))

    validator_thin._write_root_launch_approval(approval, document)
    changed_vector = json.loads(json.dumps(payload))
    changed_vector["vector_id"] = "different-vector"
    refused(candidate_payload=changed_vector)

    validator_thin._write_root_launch_approval(approval, document)
    refused(
        candidate_preflight=_preflight(
            block=104,
            hashes=hashes,
            signer="other-validator-hotkey",
        )
    )

    approval.unlink()
    refused()


def test_exact_preflight_emits_approval_without_reservation_unlock_or_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval = tmp_path / "sn39-launch-approval.json"
    runtime_root = tmp_path / "runtime"
    state_file = tmp_path / "thin-state.json"
    monkeypatch.setattr(validator_thin, "SN39_LAUNCH_APPROVAL_FILE", approval)
    monkeypatch.setattr(validator_thin, "SN39_STATE_FILE", state_file)
    monkeypatch.setattr(validator_thin, "_VALIDATOR_RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(validator_thin, "SN39_LAUNCH_APPROVAL_OWNER_UID", os.geteuid())
    monkeypatch.setattr(validator_thin, "_weight_version_key", lambda: 39)
    args = _args(tmp_path, approval)
    args.state_file = str(state_file)
    args.runtime_root = str(runtime_root)
    payload = _payload()
    audit = _audit()
    hashes = {100: "0x" + "1" * 64}
    preflight = _preflight(block=100, hashes=hashes)
    weights = {163: 0.9, 204: 0.1}
    calls: list[str] = []

    monkeypatch.setattr(
        validator_thin, "_read_state_without_mutation", lambda _path: {}
    )
    monkeypatch.setattr(validator_thin, "fetch_vector", lambda _url: payload)
    monkeypatch.setattr(validator_thin, "accept_vector", lambda *_a, **_kw: None)
    monkeypatch.setattr(validator_thin, "chain_preflight", lambda **_kwargs: preflight)
    monkeypatch.setattr(
        validator_thin, "vector_to_uid_weights", lambda *_a, **_kw: weights
    )
    monkeypatch.setattr(
        validator_thin, "_validate_chain_constraints", lambda *_a, **_kw: None
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_no_validator_compute_reward",
        lambda *_a, **_kw: None,
    )

    def gate(*_args, **kwargs):
        assert kwargs["persist"] is False
        assert kwargs["state"] == {}
        return audit

    monkeypatch.setattr(validator_thin, "_run_launch_rewarded_set_gate", gate)
    monkeypatch.setattr(
        validator_thin,
        "_revalidate_launch_after_rewarded_set_replay",
        lambda *_a, **_kw: (preflight, preflight.hotkey_to_uid, weights),
    )
    monkeypatch.setattr(
        validator_thin,
        "_reserve_common_submission",
        lambda *_a, **_kw: calls.append("reserve"),
    )
    monkeypatch.setattr(
        validator_thin,
        "set_weights_on_chain",
        lambda *_a, **_kw: calls.append("writer"),
    )
    monkeypatch.setattr(
        "bittensor.core.types.ExtrinsicResponse.unlock_wallet",
        lambda *_a, **_kw: calls.append("unlock"),
    )

    result = validator_thin.run_launch_preflight(args, approval_out=approval)
    assert result["approval_digest"].startswith("sha256:")
    assert validator_thin._read_root_launch_approval(approval) == result
    assert calls == []


def test_finalized_head_drift_refuses_before_wallet_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed_hash = "0x" + "1" * 64
    advanced_hash = "0x" + "2" * 64
    substrate = SimpleNamespace(
        get_chain_finalised_head=lambda: advanced_hash,
        get_block_number=lambda value: 101 if value == advanced_hash else 100,
        get_block_hash=lambda number: advanced_hash if number == 101 else reviewed_hash,
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=SimpleNamespace(substrate=substrate),
        hotkey_to_uid={"validator-hotkey": 30},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=100,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        finalized_hash=reviewed_hash,
    )
    unlocks: list[str] = []
    monkeypatch.setattr(
        "bittensor.core.types.ExtrinsicResponse.unlock_wallet",
        lambda *_args, **_kwargs: unlocks.append("unlock"),
    )
    with pytest.raises(
        validator_thin._RetryablePreSignHeadDrift,
        match="head advanced",
    ):
        validator_thin._submit_exact_sn39_extrinsic(
            preflight,
            runtime_contract=object(),
            attempt_id="sha256:" + "9" * 64,
            netuid=39,
            version_key=39,
            wire_uids=[163, 204],
            wire_weights=[65535, 7282],
            mortal_period_blocks=validator_thin.SN39_MORTAL_PERIOD_BLOCKS,
        )
    assert unlocks == []
