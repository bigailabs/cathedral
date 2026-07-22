from __future__ import annotations

import json
import stat
import time
from types import SimpleNamespace

import pytest

from cathedral_thin.core import (
    STATE_SCHEMA,
    StateStore,
    ThinSubnetError,
    mark_pending,
    note_submission_failure,
    note_submission_success,
    response_succeeded,
    submission_failure_is_ambiguous,
)


def test_state_secret_and_pending_vector_survive_restart(tmp_path):
    path = tmp_path / "validator.json"
    store = StateStore(path, fingerprint="fingerprint")
    state = store.load_or_create()
    secret = state.master_secret
    pending = mark_pending(
        state,
        uids=[1, 2],
        weights=[0.25, 0.75],
        hotkeys=["hot-1", "hot-2"],
    )
    store.save(state)

    reloaded = store.load_or_create()
    assert reloaded.master_secret == secret
    assert reloaded.pending_vector is not None
    assert reloaded.pending_vector.digest == pending.digest
    assert not list(tmp_path.glob("*.tmp"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_schema_five_replay_lists_migrate_with_safe_expiry(tmp_path):
    path = tmp_path / "validator.json"
    store = StateStore(path, fingerprint="x")
    store.load_or_create()
    payload = json.loads(path.read_text(encoding="utf-8"))
    receipt_id = "cc-gpu-receipt-sha256:" + "22" * 32
    payload["schema"] = 5
    payload["cc_gpu_replay_claims"] = {
        "attempt_ids": [],
        "evidence_digests": [],
        "job_ids": [],
        "receipt_ids": [receipt_id],
        "worker_ids": [],
    }
    payload.pop("cc_gpu_replay_watermark_ms")
    path.write_text(json.dumps(payload), encoding="utf-8")

    before_ms = time.time_ns() // 1_000_000
    migrated = store.load_or_create()
    assert migrated.schema == STATE_SCHEMA
    assert migrated.cc_gpu_replay_watermark_ms >= before_ms
    assert migrated.cc_gpu_replay_claims["receipt_ids"][receipt_id] > before_ms
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_schema_four_state_migrates_atomically_without_reset(tmp_path):
    path = tmp_path / "validator.json"
    store = StateStore(path, fingerprint="fingerprint")
    state = store.load_or_create()
    state.last_completed_round = 9
    state.ema_scores = {"miner": 0.75}
    state.class_checkpoints = {
        "external": {"source_epoch": 3, "report_id": "sha256:" + "11" * 32}
    }
    state.registration_checkpoints = {
        "external": {
            "owner_coldkey": "owner",
            "delegate_hotkey": "delegate",
            "sequence": 2,
            "registration_id": "sha256:" + "22" * 32,
        }
    }
    pending = mark_pending(
        state,
        uids=[1],
        weights=[1.0],
        hotkeys=["miner"],
        provenance_digest="sha256:" + "33" * 32,
    )
    store.save(state)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema"] = 4
    payload.pop("cc_gpu_replay_claims")
    payload.pop("cc_gpu_replay_watermark_ms")
    path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = store.load_or_create()
    assert migrated.schema == STATE_SCHEMA
    assert migrated.master_secret_hex == state.master_secret_hex
    assert migrated.last_completed_round == 9
    assert migrated.ema_scores == {"miner": 0.75}
    assert migrated.pending_vector is not None
    assert migrated.pending_vector.digest == pending.digest
    assert migrated.class_checkpoints == state.class_checkpoints
    assert migrated.registration_checkpoints == state.registration_checkpoints
    assert migrated.cc_gpu_replay_claims == {
        "attempt_ids": {},
        "evidence_digests": {},
        "job_ids": {},
        "receipt_ids": {},
        "worker_ids": {},
    }
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == STATE_SCHEMA
    assert not list(tmp_path.glob("*.tmp"))


def test_state_lease_rejects_a_second_validator(tmp_path):
    store = StateStore(tmp_path / "validator.json", fingerprint="x")
    with store.lease():
        with pytest.raises(ThinSubnetError, match="already locked"):
            with store.lease():
                pass


def test_corrupt_state_and_vector_fail_closed(tmp_path):
    path = tmp_path / "validator.json"
    path.write_text("not-json")
    with pytest.raises(ThinSubnetError, match="corrupt"):
        StateStore(path, fingerprint="x").load_or_create()

    path.write_text(
        f'{{"schema":{STATE_SCHEMA},"config_fingerprint":"x","master_secret_hex":"'
        + "00" * 32
        + '","cc_gpu_replay_claims":{"attempt_ids":{},"evidence_digests":{},'
        + '"job_ids":{},"receipt_ids":{},"worker_ids":{}},'
        + '"cc_gpu_replay_watermark_ms":0,"pending_vector":{"digest":"bad",'
        + '"uids":[1],"weights":[1.0],"hotkeys":["h"]}}'
    )
    with pytest.raises(ThinSubnetError, match="digest"):
        StateStore(path, fingerprint="x").load_or_create()


def test_invalid_pending_retry_metadata_fails_closed(tmp_path):
    path = tmp_path / "validator.json"
    store = StateStore(path, fingerprint="x")
    state = store.load_or_create()
    mark_pending(state, uids=[1], weights=[1.0], hotkeys=["hot-1"])
    assert state.pending_vector is not None
    state.pending_vector.ambiguous = "false"  # type: ignore[assignment]
    store.save(state)
    with pytest.raises(ThinSubnetError, match="retry metadata"):
        store.load_or_create()


def test_owner_registration_checkpoint_survives_restart_and_rejects_corruption(
    tmp_path,
):
    store = StateStore(tmp_path / "validator.json", fingerprint="x")
    state = store.load_or_create()
    state.registration_checkpoints = {
        "confidential_compute": {
            "owner_coldkey": "owner",
            "delegate_hotkey": "delegate",
            "sequence": 4,
            "registration_id": "sha256:" + "11" * 32,
        }
    }
    store.save(state)
    assert store.load_or_create().registration_checkpoints == (
        state.registration_checkpoints
    )

    state.registration_checkpoints["confidential_compute"]["sequence"] = True
    store.save(state)
    with pytest.raises(ThinSubnetError, match="owner registration checkpoint"):
        store.load_or_create()


def test_cc_gpu_replay_claims_survive_restart_and_use_typed_identifiers(tmp_path):
    store = StateStore(tmp_path / "validator.json", fingerprint="x")
    state = store.load_or_create()
    state.cc_gpu_replay_claims = {
        "attempt_ids": {"00000000-0000-4000-8000-000000000003": 2_000_000_000_000},
        "evidence_digests": {"sha256:" + "11" * 32: 2_000_000_000_000},
        "job_ids": {"00000000-0000-4000-8000-000000000002": 2_000_000_000_000},
        "receipt_ids": {"cc-gpu-receipt-sha256:" + "22" * 32: 2_000_000_000_000},
        "worker_ids": {"00000000-0000-4000-8000-000000000001": 2_000_000_000_000},
    }
    store.save(state)
    assert store.load_or_create().cc_gpu_replay_claims == state.cc_gpu_replay_claims

    state.cc_gpu_replay_claims["receipt_ids"] = {
        "sha256:" + "22" * 32: 2_000_000_000_000
    }
    store.save(state)
    with pytest.raises(ThinSubnetError, match="replay claim ledger"):
        store.load_or_create()


def test_cc_gpu_replay_watermark_rejects_type_coercion(tmp_path):
    path = tmp_path / "validator.json"
    store = StateStore(path, fingerprint="x")
    store.load_or_create()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["cc_gpu_replay_watermark_ms"] = "123"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ThinSubnetError, match="replay watermark"):
        store.load_or_create()


def test_pending_vector_digest_binds_owner_registration_ids(tmp_path):
    store = StateStore(tmp_path / "validator.json", fingerprint="x")
    state = store.load_or_create()
    mark_pending(
        state,
        uids=[1],
        weights=[1.0],
        hotkeys=["miner"],
        registration_ids={"confidential_compute": "sha256:" + "11" * 32},
    )
    store.save(state)
    state.pending_vector.registration_ids["confidential_compute"] = (
        "sha256:" + "22" * 32
    )
    store.save(state)
    with pytest.raises(ThinSubnetError, match="digest"):
        store.load_or_create()


@pytest.mark.parametrize(
    ("uids", "weights", "hotkeys"),
    [([True], [1.0], ["h"]), ([1], [True], ["h"]), ([1], [1.0], [1])],
)
def test_pending_vector_rejects_type_coercion(uids, weights, hotkeys):
    state = SimpleNamespace(pending_vector=None)
    with pytest.raises(ThinSubnetError, match="invalid UID"):
        mark_pending(state, uids=uids, weights=weights, hotkeys=hotkeys)


def test_config_change_requires_explicit_acceptance(tmp_path):
    path = tmp_path / "validator.json"
    old_store = StateStore(path, fingerprint="old")
    old_state = old_store.load_or_create()
    old_state.cc_gpu_replay_claims["receipt_ids"] = {
        "cc-gpu-receipt-sha256:" + "22" * 32: 2_000_000_000_000
    }
    old_store.save(old_state)
    with pytest.raises(ThinSubnetError, match="fingerprint"):
        StateStore(path, fingerprint="new").load_or_create()
    state = StateStore(path, fingerprint="new").load_or_create(allow_config_change=True)
    assert state.config_fingerprint == "new"
    assert state.ema_scores == {}
    assert state.last_completed_round == -1
    assert state.cc_gpu_replay_claims["receipt_ids"] == {
        "cc-gpu-receipt-sha256:" + "22" * 32: 2_000_000_000_000
    }


def test_config_change_cannot_discard_a_pending_vector(tmp_path):
    path = tmp_path / "validator.json"
    old = StateStore(path, fingerprint="old")
    state = old.load_or_create()
    mark_pending(state, uids=[1], weights=[1.0], hotkeys=["hot-1"])
    old.save(state)
    with pytest.raises(ThinSubnetError, match="pending"):
        StateStore(path, fingerprint="new").load_or_create(allow_config_change=True)


def test_state_symlink_is_rejected(tmp_path):
    real = tmp_path / "real.json"
    StateStore(real, fingerprint="x").load_or_create()
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ThinSubnetError, match="symlink"):
        StateStore(link, fingerprint="x").load_or_create()


def test_failure_backoff_and_success_transition(tmp_path):
    store = StateStore(tmp_path / "validator.json", fingerprint="x")
    state = store.load_or_create()
    pending = mark_pending(state, uids=[3], weights=[1.0], hotkeys=["hot-3"])
    note_submission_failure(state, now_ms=100, base_backoff_ms=10, max_backoff_ms=100)
    assert state.pending_vector is not None
    assert state.pending_vector.attempts == 1
    assert state.pending_vector.next_retry_at_ms == 110
    note_submission_success(state)
    assert state.pending_vector is None
    assert state.confirmed_vector_digest == pending.digest


@pytest.mark.parametrize(
    "response",
    [
        True,
        (True, "ok"),
        SimpleNamespace(success=True),
        SimpleNamespace(is_success=True),
    ],
)
def test_weight_response_success_shapes(response):
    assert response_succeeded(response)


@pytest.mark.parametrize(
    "response",
    [False, (), (False, "bad"), None, object(), SimpleNamespace(success=False)],
)
def test_weight_response_failure_shapes(response):
    assert not response_succeeded(response)


def test_modern_weight_failure_ambiguity_uses_receipt_and_error_metadata():
    assert submission_failure_is_ambiguous(None)
    assert submission_failure_is_ambiguous(
        SimpleNamespace(
            success=False,
            extrinsic=object(),
            extrinsic_receipt=None,
            error=ConnectionError("unknown"),
        )
    )
    assert not submission_failure_is_ambiguous(
        SimpleNamespace(
            success=False,
            extrinsic=object(),
            extrinsic_receipt=object(),
            error=RuntimeError("finalized rejection"),
        )
    )
    assert not submission_failure_is_ambiguous(SimpleNamespace(success=False))
