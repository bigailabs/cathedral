"""Two-mode validator: thin default + concurrent full-provenance audit.

Unit tests stub the audit; the integration tests at the bottom build a real
content-addressed evidence store with the ``cathedral`` package (installed via
the ``provenance`` extra) and run the actual audit against it.
"""

from __future__ import annotations

import grp
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import tomllib
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from test_validator_thin_validated_supply import payload as validated_supply_payload

from scaffold import cli, provenance_audit, sn39_public_reproduction, validator_thin
from scaffold.provenance_audit import (
    ProvenanceAudit,
    ProvenanceAuditError,
    ProvenanceSettings,
    ProvenanceUnavailable,
    check_chain_state,
    run_audit,
)


@pytest.fixture(autouse=True)
def _isolated_submission_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "submission-runtime"
    )


# ---------------------------------------------------------------------------
# Authority-mode UID vector construction
# ---------------------------------------------------------------------------


def test_authority_weights_use_configured_burn_and_fixed_fraction() -> None:
    weights = validator_thin._provenance_uid_weights(
        {"tdx-miner": 1.0},
        mechanism="validated_supply_v1",
        burn_hotkey="burn-hotkey",
        hotkey_to_uid={"burn-hotkey": 0, "tdx-miner": 163},
    )
    assert weights == {0: pytest.approx(0.10), 163: pytest.approx(0.90)}
    # Empty verified set: everything to the configured burn destination.
    empty = validator_thin._provenance_uid_weights(
        {},
        mechanism="validated_supply_v1",
        burn_hotkey="burn-hotkey",
        hotkey_to_uid={"burn-hotkey": 7},
    )
    assert empty == {7: 1.0}


def test_authority_weights_fail_closed_on_bad_inputs() -> None:
    base = {"mechanism": "validated_supply_v1", "burn_hotkey": "burn-hotkey"}
    mapping = {"burn-hotkey": 0, "tdx-miner": 163}
    with pytest.raises(validator_thin.wire.VectorError, match="no current metagraph"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0}, hotkey_to_uid={"burn-hotkey": 0}, **base
        )
    with pytest.raises(validator_thin.wire.VectorError, match="non-finite or negative"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": float("nan")}, hotkey_to_uid=mapping, **base
        )
    with pytest.raises(validator_thin.wire.VectorError, match="non-finite or negative"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": -0.2}, hotkey_to_uid=mapping, **base
        )
    with pytest.raises(validator_thin.wire.VectorError, match="sum to"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 0.5}, hotkey_to_uid=mapping, **base
        )
    with pytest.raises(validator_thin.wire.VectorError, match="burn UID"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0},
            hotkey_to_uid={"burn-hotkey": 163, "tdx-miner": 163},
            **base,
        )
    with pytest.raises(
        validator_thin.wire.VectorError, match="requires --provenance-burn-hotkey"
    ):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0},
            mechanism="validated_supply_v1",
            burn_hotkey=None,
            hotkey_to_uid=mapping,
        )
    with pytest.raises(validator_thin.wire.VectorError, match="no pinned burn"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0},
            mechanism="validated_supply_v99",
            burn_hotkey="burn-hotkey",
            hotkey_to_uid=mapping,
        )


# ---------------------------------------------------------------------------
# Shadow vs authority behavior around the audit (stubbed audit)
# ---------------------------------------------------------------------------


def _args(tmp_path: Path, mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        publisher_url="https://publisher.example",
        network="finney",
        netuid=39,
        state_file=str(tmp_path / "state.json"),
        provenance=mode,
        evidence_url="https://publisher.example/v1/evidence",
        evidence_dir=None,
        provenance_registry_keys="registry.json",
        provenance_report_keys="report.json",
        provenance_index_keys="index.json",
        provenance_verifier_digest="sha256:" + "d" * 64,
        provenance_mechanism="validated_supply_v1",
        jsonl=None,
    )


def _pin_sn39_runtime(args: SimpleNamespace, *, launch: bool = False) -> None:
    args.publisher_url = validator_thin.SN39_PUBLISHER_URL
    args.public_key_hex = validator_thin.DEFAULT_PUBLIC_KEY_HEX
    args.key_id = validator_thin.SN39_WEIGHT_POLICY_KEY_ID
    args.require_policy = "validated_supply_v1"
    args.wallet_name = "validator"
    args.wallet_hotkey = "default"
    args.state_file = str(validator_thin.SN39_STATE_FILE)
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    args.evidence_url = validator_thin.SN39_EVIDENCE_URL
    args.provenance_registry_keys_digest = validator_thin.SN39_REGISTRY_KEYS_DIGEST
    args.provenance_report_keys_digest = validator_thin.SN39_REPORT_KEYS_DIGEST
    args.provenance_index_keys_digest = validator_thin.SN39_INDEX_KEYS_DIGEST
    args.provenance_verifier_digest = validator_thin.SN39_VERIFIER_DIGEST
    args.provenance_source_revision = validator_thin.SN39_PRODUCER_REVISION
    args.provenance_mechanism = validator_thin.MECHANISM_DEFAULT
    args.provenance_burn_hotkey = validator_thin.SN39_BURN_HOTKEY
    args.launch_approval_file = str(validator_thin.SN39_LAUNCH_APPROVAL_FILE)
    args.launch_release_sha = "a" * 40
    args.launch_config_sha256 = "sha256:" + "b" * 64
    args.launch_preflight = False
    args.require_completed_launch_for_broadcast = not launch
    if launch:
        args.provenance_controlled_dir = str(validator_thin.SN39_LAUNCH_CONTROLLED_DIR)
        args.provenance_verifier_binary = str(
            validator_thin.SN39_LAUNCH_VERIFIER_BINARY
        )


def _stub_audit(monkeypatch, audit: ProvenanceAudit) -> list[dict]:
    calls: list[dict] = []

    def fake_run_audit(settings, *, network, netuid, vector_payload, state, **_kw):
        calls.append({"settings": settings, "state": dict(state)})
        return audit

    monkeypatch.setattr(validator_thin, "run_audit", fake_run_audit)
    return calls


def _drain_shadow(args, timeout: float = 5.0) -> None:
    auditor = validator_thin._get_shadow_auditor(args)
    deadline = time.monotonic() + timeout
    while auditor.busy() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_shadow_mode_never_blocks_and_logs_on_next_tick(tmp_path, monkeypatch) -> None:
    calls = _stub_audit(
        monkeypatch,
        ProvenanceAudit(status="FAIL", error="evidence endpoint unreachable"),
    )
    args = _args(tmp_path, "shadow")
    started = time.monotonic()
    status, recomputed = validator_thin._run_provenance_stage(
        args, validated_supply_payload(), tmp_path / "state.json"
    )
    assert time.monotonic() - started < 1.0  # never blocks the thin path
    assert status == "PENDING"
    assert recomputed is None  # thin submission proceeds untouched
    _drain_shadow(args)
    # The completed audit is drained and logged on the NEXT tick.
    validator_thin._run_provenance_stage(
        args, validated_supply_payload(), tmp_path / "state.json"
    )
    assert len(calls) >= 1


def test_slow_shadow_audit_is_single_flight_and_cannot_delay_thin(
    tmp_path, monkeypatch
) -> None:
    import threading as threading_module

    release = threading_module.Event()

    def slow_audit(settings, *, network, netuid, vector_payload, state, **_kw):
        release.wait(10.0)
        return ProvenanceAudit(
            status="PASS", source_epoch=1, report_id="sha256:" + "a" * 64
        )

    monkeypatch.setattr(validator_thin, "run_audit", slow_audit)
    args = _args(tmp_path, "shadow")
    started = time.monotonic()
    status1, _ = validator_thin._run_provenance_stage(
        args, validated_supply_payload(), tmp_path / "state.json"
    )
    status2, _ = validator_thin._run_provenance_stage(
        args, validated_supply_payload(), tmp_path / "state.json"
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.0  # a 10s audit cannot delay two thin ticks
    assert status1 == "PENDING" and status2 == "PENDING"
    assert validator_thin._get_shadow_auditor(args).busy()  # single flight
    release.set()
    _drain_shadow(args)


def test_shadow_mode_records_chain_state_on_pass(tmp_path, monkeypatch) -> None:
    _stub_audit(
        monkeypatch,
        ProvenanceAudit(
            status="PASS",
            assurance="full",
            source_epoch=77,
            report_id="sha256:" + "a" * 64,
            recomputed={"tdx-miner": 1.0},
            agrees_with_vector=True,
        ),
    )
    state_file = tmp_path / "state.json"
    args = _args(tmp_path, "shadow")
    validator_thin._run_provenance_stage(args, validated_supply_payload(), state_file)
    _drain_shadow(args)
    validator_thin._run_provenance_stage(args, validated_supply_payload(), state_file)
    state = json.loads(state_file.read_text())
    assert state["provenance_last_source_epoch"] == 77
    assert state["provenance_last_report_id"] == "sha256:" + "a" * 64


def test_authority_mode_refuses_to_submit_without_a_pass(tmp_path, monkeypatch) -> None:
    _stub_audit(
        monkeypatch, ProvenanceAudit(status="NOT_PROVEN", error="not installed")
    )
    with pytest.raises(validator_thin.wire.VectorError, match="did not PASS"):
        validator_thin._run_provenance_stage(
            _args(tmp_path, "authority"),
            validated_supply_payload(),
            tmp_path / "state.json",
        )


def test_authority_mode_requires_full_assurance(tmp_path, monkeypatch) -> None:
    _stub_audit(
        monkeypatch,
        ProvenanceAudit(
            status="PASS",
            assurance="receipts_only",
            source_epoch=78,
            report_id="sha256:" + "b" * 64,
            recomputed={"tdx-miner": 1.0},
        ),
    )
    with pytest.raises(validator_thin.wire.VectorError, match="FULL assurance"):
        validator_thin._run_provenance_stage(
            args=_args(tmp_path, "authority"),
            payload=validated_supply_payload(),
            state_file=tmp_path / "state.json",
        )


def test_authority_mode_returns_the_recomputation(tmp_path, monkeypatch) -> None:
    _stub_audit(
        monkeypatch,
        ProvenanceAudit(
            status="PASS",
            assurance="full",
            source_epoch=78,
            report_id="sha256:" + "b" * 64,
            recomputed={"tdx-miner": 1.0},
            agrees_with_vector=False,
            discrepancies=["tdx-miner: recomputed=1.0 signed_vector=0.8"],
        ),
    )
    status, recomputed = validator_thin._run_provenance_stage(
        _args(tmp_path, "authority"),
        validated_supply_payload(),
        tmp_path / "state.json",
    )
    assert status == "PASS"
    assert recomputed == {"tdx-miner": 1.0}  # OUR numbers, not the vector's


def test_state_fence_and_provenance_state_share_the_file(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    validator_thin.save_fence(state_file, 41, "vector-41")
    validator_thin._write_state(state_file, {"provenance_last_source_epoch": 9})
    assert validator_thin.load_fence(state_file) == 41
    document = json.loads(state_file.read_text())
    assert document["provenance_last_source_epoch"] == 9
    assert document["last_vector_id"] == "vector-41"


# ---------------------------------------------------------------------------
# Anti-equivocation chain state
# ---------------------------------------------------------------------------


def test_chain_state_rejects_source_epoch_rollback() -> None:
    audit = ProvenanceAudit(
        status="PASS", source_epoch=10, report_id="sha256:" + "a" * 64
    )
    with pytest.raises(ProvenanceAuditError, match="rollback"):
        check_chain_state(
            audit,
            {"provenance_last_source_epoch": 11, "provenance_last_report_id": "x"},
        )


@pytest.mark.parametrize("lane", ["authority", "thin"])
def test_submission_attempt_journal_refuses_a_b_a_replay(
    tmp_path: Path, lane: str
) -> None:
    state_file = tmp_path / "state.json"
    attempt_a = "sha256:" + "a" * 64
    attempt_b = "sha256:" + "b" * 64
    key = f"{lane}_submission_attempt_id"
    history_key = f"{lane}_submission_attempt_ids"

    validator_thin._write_state_fenced(state_file, {key: attempt_a})
    validator_thin._write_state_fenced(state_file, {key: attempt_b})
    state = validator_thin._read_state(state_file)
    assert state[history_key] == [attempt_a, attempt_b]
    with pytest.raises(ValueError, match="already attempted"):
        validator_thin._write_state_fenced(state_file, {key: attempt_a})


def test_chain_state_rejects_same_epoch_equivocation() -> None:
    audit = ProvenanceAudit(
        status="PASS", source_epoch=11, report_id="sha256:" + "a" * 64
    )
    with pytest.raises(ProvenanceAuditError, match="equivocation"):
        check_chain_state(
            audit,
            {
                "provenance_last_source_epoch": 11,
                "provenance_last_report_id": "sha256:" + "b" * 64,
            },
        )
    # The identical report replayed is fine (idempotent audit).
    check_chain_state(
        audit,
        {
            "provenance_last_source_epoch": 11,
            "provenance_last_report_id": "sha256:" + "a" * 64,
        },
    )


def test_unconfigured_shadow_audit_reports_not_proven(tmp_path) -> None:
    audit = run_audit(
        ProvenanceSettings(mode="shadow", evidence_url="https://x.example"),
        network="finney",
        netuid=39,
        vector_payload=None,
        state={},
    )
    assert audit.status == "NOT_PROVEN"
    assert "not configured" in audit.error
    assert audit.remediation


# ---------------------------------------------------------------------------
# Integration: a real evidence store audited by the real cathedral package
# ---------------------------------------------------------------------------

# The integration fixtures below REQUIRE the cathedral package; the unit
# tests above must always collect and run. Only fixture construction skips.
try:
    import cathedral.provenance  # noqa: F401

    _CATHEDRAL_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs the extra
    _CATHEDRAL_AVAILABLE = False

requires_cathedral = pytest.mark.skipif(
    not _CATHEDRAL_AVAILABLE, reason="provenance extra not installed"
)


VERIFIER_SCRIPT = b"""#!/usr/bin/env python3
import json, sys
quote = json.load(open(sys.argv[1]))
claims = dict(quote["claims"])
claims["report_data"] = quote["report_data_hex"]
claims["report_data_match"] = sys.argv[2] == quote["report_data_hex"]
print(json.dumps(claims))
"""

FULL_CLAIMS = {
    "intel_verified": True,
    "measurement": "tdx-measurement-sha256:sample-v1",
    "tcb_status": "UpToDate",
    "advisory_ids": [],
    "debug_enabled": False,
    "collateral_current": True,
    "platform_identity_kind": "stable",
    "platform_identity_verified": True,
    "claims_bound_to_quote": True,
    "stable_platform_id": "tdx-platform-sha256:" + "c" * 64,
    "platform_id": "tdx-platform-sha256:" + "c" * 64,
    "tdx_pck_cert_id": "tdx-pck-cert-sha256:" + "d" * 64,
    "tdx_attestation_key_id": "tdx-ak-sha256:" + "e" * 64,
    "tcb_svn": "01" * 16,
}


@pytest.fixture(scope="module")
def real_evidence(tmp_path_factory):
    if not _CATHEDRAL_AVAILABLE:
        pytest.skip("provenance extra not installed")
    """A genuine registry→receipt→report→manifest→index chain across THREE
    epochs (positive 11, revoked 12, restored 13), with real raw Evidence
    envelopes whose quote bytes are what each receipt's hardware claim
    hashes, a controlled-disclosure directory, and an executable verifier
    fixture driven through the canonical strict path."""
    import base64
    import hashlib
    from datetime import UTC, datetime, timedelta

    from cathedral.assurance import (
        AssuranceDimension,
        ClaimStatus,
        attestation_claims,
        evaluated_claim,
        with_verified_channel,
    )
    from cathedral.common import (
        Attested,
        Evidence,
        EvidenceKind,
        Tier,
        evidence_report_data,
    )
    from cathedral.evidence import EvidenceStore, build_manifest, build_signed_index
    from cathedral.ledger import Ledger
    from cathedral.lifecycle import (
        LifecycleReason,
        LifecycleSnapshot,
        WorkerLifecycleState,
    )
    from cathedral.policy_registry import canonical_json, sign_registry, verify_registry
    from cathedral.receipt import ReceiptIssuer
    from cathedral.runtime import (
        SAT_WORK_POLICY_DIGEST,
        _evidence_digest,
        _retained_evidence_envelope,
    )
    from cathedral.score_class import export_score_class_report
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    tmp_path = tmp_path_factory.mktemp("evidence")
    registry_seed = bytes(range(32))
    receipt_seed = bytes(range(32, 64))
    report_seed = bytes(range(64, 96))
    index_seed = bytes(range(96, 128))
    now = datetime.now(UTC).replace(microsecond=0)
    t0 = now - timedelta(hours=1)
    t1 = now + timedelta(hours=47)

    def text(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def pub_raw(seed: bytes) -> bytes:
        return (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )

    registry_document = sign_registry(
        {
            "schema": "cathedral_policy_registry_v1",
            "release": 1,
            "generated_at": text(t0),
            "valid_from": text(t0),
            "valid_until": text(t1),
            "signing_key_id": "cathedral-policy-test-1",
            "receipt_signing_keys": [
                {
                    "id": "receipt-test-1",
                    "algorithm": "ed25519",
                    "public_key_base64": base64.b64encode(
                        pub_raw(receipt_seed)
                    ).decode(),
                    "purpose": "assurance_receipt",
                    "status": "active",
                    "status_changed_at": text(t0),
                    "valid_from": text(t0),
                    "valid_until": text(t1),
                    "revoked_at": None,
                    "replacement_key_id": None,
                    "metadata": {"environment": "test-only"},
                }
            ],
            "profiles": [
                {
                    "id": "cpu-tdx-sample-v1",
                    "kind": "cpu_tdx",
                    "status": "active",
                    "status_changed_at": text(t0),
                    "valid_from": text(t0),
                    "valid_until": text(t1),
                    "retire_at": None,
                    "measurements": ["tdx-measurement-sha256:sample-v1"],
                    "runtime_measurements": ["runtime-sha256:sample-v1"],
                    "allowed_firmware": [],
                    "min_tcb": 0,
                    "tdx_allowed_tcb_statuses": ["UpToDate"],
                    "tdx_allowed_advisories": [],
                    "metadata": {"description": "test CPU profile"},
                }
            ],
            "metadata": {"purpose": "two-mode integration"},
        },
        registry_seed,
    )
    registry_bytes = canonical_json(registry_document)
    trusted = {"cathedral-policy-test-1": pub_raw(registry_seed)}
    snapshot = verify_registry(registry_bytes, trusted, now=now)
    policy = snapshot.to_policy(at=now)
    verified_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    verifier_digest = "sha256:" + "d" * 64

    verifier_path = tmp_path / "verifier.py"
    verifier_path.write_bytes(VERIFIER_SCRIPT)
    verifier_path.chmod(0o755)

    store_root = tmp_path / "store"
    store = EvidenceStore(store_root)
    controlled_root = tmp_path / "controlled"
    controlled_root.mkdir(mode=0o700)
    registry_blob = store.put_blob(registry_bytes)
    ledger = Ledger(tmp_path / "ledger.sqlite")
    declared = ("/opt/cathedral/bin/cathedral-tdx-verifier-test",)

    stage_indexes: dict[int, bytes] = {}
    recent_rows: list[dict] = []

    ANCHOR_BLOCK = 100
    ANCHOR_HASH = "0x" + "ab" * 32

    def build_stage(source_epoch: int, positive: bool) -> None:
        epoch_id = ledger.begin_epoch(
            source_epoch,
            policy_registry_release=snapshot.release,
            policy_registry_digest=snapshot.digest,
            network="finney",
            netuid=39,
            challenge_anchor_block=ANCHOR_BLOCK,
            challenge_anchor_hash=ANCHOR_HASH,
        )
        attestation_rows = []
        receipts = []
        work_blobs: list[tuple[str, str]] = []
        if positive:
            from cathedral.lanes.sat import _compute_challenge_id
            from cathedral.lanes.sat_types import (
                SatCertificate,
                SatInstance,
                SatWorkItem,
            )
            from cathedral.runtime import _sat_manifest_bytes, _sat_result_bytes

            sat_instance = SatInstance(n_vars=3, clauses=[[1, 2, -3]] * 20)
            sat_seed = source_epoch
            challenge_hex = _compute_challenge_id(sat_instance, sat_seed)
            sat_item = SatWorkItem(
                instance=sat_instance, seed=sat_seed, challenge_id=challenge_hex
            )
            sat_certificate = SatCertificate(
                satisfiable=True,
                assignment=[1, 2, -3],
                work_units=20.0,
                challenge_id=challenge_hex,
                assigned_hotkey="tdx-miner",
            )
            work_item_bytes = _sat_manifest_bytes(sat_item)
            result_bytes = _sat_result_bytes(sat_item, sat_certificate)
            from cathedral.challenge import derive_challenge_nonce

            nonce = derive_challenge_nonce(
                block=ANCHOR_BLOCK,
                block_hash=ANCHOR_HASH,
                network="finney",
                netuid=39,
                source_epoch=source_epoch,
                miner_hotkey="tdx-miner",
            )
            seed_evidence = Evidence(
                kind=EvidenceKind.TDX,
                quote=b"placeholder",
                nonce=nonce,
                miner_hotkey="tdx-miner",
            )
            expected = evidence_report_data(seed_evidence, nonce)
            quote = json.dumps(
                {"claims": FULL_CLAIMS, "report_data_hex": expected.hex()},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            evidence = Evidence(
                kind=EvidenceKind.TDX,
                quote=quote,
                nonce=nonce,
                miner_hotkey="tdx-miner",
            )
            evidence_digest = _evidence_digest(evidence)
            envelope = _retained_evidence_envelope((evidence,), evidence_digest)
            envelope_digest = "sha256:" + hashlib.sha256(envelope).hexdigest()
            (controlled_root / f"{envelope_digest.split(':', 1)[1]}.json").write_bytes(
                envelope
            )
            # The receipt's hardware claim hashes the EXACT raw quote bytes.
            claims = attestation_claims(quote, policy, verified_at=verified_text)
            claims = with_verified_channel(
                claims, b"binding", verified_at=verified_text
            )
            claims = claims.with_claim(
                AssuranceDimension.WORK,
                evaluated_claim(
                    ClaimStatus.PASSED,
                    result_bytes,
                    SAT_WORK_POLICY_DIGEST,
                    verified_at=verified_text,
                ),
            )
            attested = Attested(
                tier=Tier.CC_CPU_TDX,
                chip_id="tdx-platform-sha256:" + "c" * 64,
                measurement="tdx-measurement-sha256:sample-v1",
                tcb=1,
                tcb_status="UpToDate",
                advisory_ids=(),
                debug_enabled=False,
                collateral_current=True,
                tcb_svn="01" * 16,
                policy_mode="strict",
                assurance=claims,
            )
            lifecycle = LifecycleSnapshot(
                hotkey="tdx-miner",
                state=WorkerLifecycleState.ATTESTED,
                generation=1,
                revision=2,
                event_id=2,
                reason=LifecycleReason.ATTESTATION_VERIFIED,
                state_changed_at=now,
                evidence_verified_at=now,
                evidence_expires_at=now + timedelta(hours=1),
                measurement="tdx-measurement-sha256:sample-v1",
                evidence_digest=claims.hardware.evidence_digest,
                policy_digest=claims.software.policy_digest,
                policy_registry_release=policy.registry_release,
                policy_registry_digest=policy.registry_digest,
            )
            receipt = ReceiptIssuer(snapshot, "receipt-test-1", receipt_seed).issue(
                epoch_id=epoch_id,
                source_epoch=source_epoch,
                subject_hotkey="tdx-miner",
                attested=attested,
                policy=policy,
                assurance=claims,
                worker_lifecycle=lifecycle,
                challenge_id=challenge_hex,
                manifest_digest="sha256:" + hashlib.sha256(work_item_bytes).hexdigest(),
                work_units=20.0,
                issued_at=now,
            )
            ledger.record_work_artifacts(challenge_hex, work_item_bytes, result_bytes)
            work_blobs.append(
                (store.put_blob(work_item_bytes), store.put_blob(result_bytes))
            )
            ledger.issue_challenge(challenge_hex, "tdx-miner", epoch_id)
            ledger.resolve_challenge_with_receipt(
                challenge_hex,
                "verified",
                20.0,
                validator_derived=True,
                receipt_id=receipt.receipt_id,
                receipt_body=receipt.receipt_bytes,
                receipt_digest=receipt.receipt_digest,
                issued_at=verified_text,
            )
            ledger.add_attestation(
                epoch_id,
                "tdx-miner",
                verdict="VERIFIED",
                tee_type="TDX",
                workload="CPU",
                evidence_digest=claims.hardware.evidence_digest,
                policy_mode="strict",
                envelope_digest=envelope_digest,
            )
            ledger.add_lifecycle_snapshot(
                epoch_id, lifecycle, snapshot_at=verified_text
            )
            receipts.append((receipt, envelope_digest))
            attestation_rows.append(
                {
                    "hotkey": "tdx-miner",
                    "verdict": "VERIFIED",
                    "evidence_digest": "sha256:" + evidence_digest
                    if not evidence_digest.startswith("sha256:")
                    else evidence_digest,
                    "envelope_digest": envelope_digest,
                    "challenge_digest": "sha256:" + hashlib.sha256(nonce).hexdigest(),
                    "disclosure": "controlled",
                }
            )
        ledger.complete_epoch(
            epoch_id,
            {"tdx-miner"},
            generated_at=verified_text,
            score_network="finney",
            score_netuid=39,
        )
        wire_report_sha256 = hashlib.sha256(ledger.report_bytes(epoch_id)).hexdigest()
        report_bytes = export_score_class_report(
            ledger,
            epoch_id,
            network="finney",
            netuid=39,
            class_id="confidential_compute",
            source_id="cathedralconfidential",
            signing_key_id="score-test-1",
            private_key_seed=report_seed,
            generated_at=now,
            valid_until=now + timedelta(minutes=30),
            valid_from_block=ANCHOR_BLOCK,
            valid_until_block=10_000_000_000,
            verifier_digest=verifier_digest,
            candidate_snapshot={
                "schema": "cathedral_candidate_snapshot_v1",
                "network": "finney",
                "netuid": 39,
                "block": ANCHOR_BLOCK,
                "block_hash": ANCHOR_HASH,
                "hotkeys": ["tdx-miner"],
            },
        )
        report = json.loads(report_bytes)
        ledger.mark_published(epoch_id)
        report_blob = store.put_blob(report_bytes)
        manifest_receipts = [
            {
                "receipt_id": receipt.receipt_id,
                "hotkey": "tdx-miner",
                "blob": store.put_blob(receipt.receipt_bytes),
                "work_item_blob": work_blobs[index][0],
                "result_blob": work_blobs[index][1],
            }
            for index, (receipt, _) in enumerate(receipts)
        ]
        manifest_bytes = build_manifest(
            network="finney",
            netuid=39,
            source_epoch=source_epoch,
            epoch_id=epoch_id,
            generated_at=None,
            mechanism_id="validated_supply_v1",
            mechanism_revision=1,
            source_revision="abc1234",
            registry_release=1,
            registry_digest=snapshot.digest,
            registry_blob=registry_blob,
            verifier_digest=verifier_digest,
            verifier_binary_blob=store.put_blob(VERIFIER_SCRIPT),
            verifier_command=list(declared),
            verifier_artifacts=list(declared),
            report_id=report["report_id"],
            report_blob=report_blob,
            report_signing_key_id="score-test-1",
            receipts=manifest_receipts,
            attestations=attestation_rows,
            candidate_set={
                "source": "sn39_metagraph",
                "network": "finney",
                "netuid": 39,
                "block": ANCHOR_BLOCK,
                "block_hash": ANCHOR_HASH,
                "candidates": [
                    {
                        "hotkey": "tdx-miner",
                        "outcome": "verified" if positive else "rejected",
                        "reason": (
                            "receipt_verified" if positive else "no_verified_work"
                        ),
                    }
                ],
            },
            wire_report_sha256=wire_report_sha256,
        )
        manifest_digest = store.put_blob(manifest_bytes)
        index_bytes = build_signed_index(
            network="finney",
            netuid=39,
            latest_source_epoch=source_epoch,
            latest_manifest_digest=manifest_digest,
            recent=list(recent_rows),
            signing_key_id="evidence-index-test-1",
            private_key_seed=index_seed,
        )
        recent_rows.insert(
            0, {"source_epoch": source_epoch, "manifest": manifest_digest}
        )
        stage_indexes[source_epoch] = index_bytes

    build_stage(11, positive=True)
    build_stage(12, positive=False)
    build_stage(13, positive=True)
    ledger.close()
    store.write_index(stage_indexes[11])

    def keyfile(name: str, mapping: dict[str, bytes]) -> tuple[str, str]:
        path = tmp_path / name
        body = json.dumps(
            {kid: base64.b64encode(raw).decode() for kid, raw in mapping.items()}
        ).encode()
        path.write_bytes(body)
        return str(path), "sha256:" + hashlib.sha256(body).hexdigest()

    registry_keys, registry_keys_digest = keyfile("registry-keys.json", trusted)
    report_keys, report_keys_digest = keyfile(
        "report-keys.json", {"score-test-1": pub_raw(report_seed)}
    )
    index_keys, index_keys_digest = keyfile(
        "index-keys.json", {"evidence-index-test-1": pub_raw(index_seed)}
    )
    settings = ProvenanceSettings(
        mode="shadow",
        evidence_dir=str(store_root),
        registry_keys=registry_keys,
        registry_keys_digest=registry_keys_digest,
        report_keys=report_keys,
        report_keys_digest=report_keys_digest,
        index_keys=index_keys,
        index_keys_digest=index_keys_digest,
        verifier_digest=verifier_digest,
        controlled_dir=str(controlled_root),
        verifier_binary=str(verifier_path),
        source_revision="abc1234",
    )
    return store_root, settings, stage_indexes


def _bound_vector(store_root: Path, *, positive: bool = True) -> dict:
    """Build the real signed-vector contract bound to the store's latest epoch."""
    index = json.loads((store_root / "index.json").read_bytes())
    manifest_digest = index["latest"]["manifest"]
    manifest = json.loads(
        (
            store_root / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
        ).read_bytes()
    )
    vector = validated_supply_payload(positive=positive)
    vector["policy_metadata"]["external_scores"] = {
        "enabled": True,
        "source": "cathedral_confidential_tdx",
        "mode": "confidential_primary",
        "latest_complete": True,
        "latest_epoch": int(manifest["source_epoch"]),
        "latest_report_sha256": manifest["score_report"]["blob"],
        "latest_body_sha256": manifest["wire_report_sha256"],
    }
    vector["policy_metadata"]["score_source"] = (
        "confidential_primary:cathedral_confidential_tdx"
    )
    return vector


def _historical_lookup(block: int):
    """Fixture chain history: the anchored block resolves to exactly the
    fixture miner; any other block is unknown history."""
    return {"tdx-miner"} if block == 100 else None


def _block_hash(block: int):
    return ("0x" + "ab" * 32) if block == 100 else None


def _run_audit_replay(
    settings,
    *,
    state=None,
    vector=None,
    network="finney",
    netuid=39,
    current_block=None,
    historical_hotkeys_lookup=_historical_lookup,
    block_hash_lookup=_block_hash,
):
    """Real full-path audit. ONLY the static-ELF verifier-bytes
    authentication is stubbed (it has its own adversarial matrix and cannot
    pass for a script on this host); envelope digests, canonical strict
    subprocess execution, claim gates, receipt bindings, and recompute all
    run for real."""
    with mock.patch("cathedral.replay.authenticate_verifier_bytes"):
        return run_audit(
            settings,
            network=network,
            netuid=netuid,
            vector_payload=vector,
            state=state or {},
            current_block=current_block,
            historical_hotkeys_lookup=historical_hotkeys_lookup,
            block_hash_lookup=block_hash_lookup,
        )


def test_real_audit_passes_and_agrees_with_a_matching_vector(real_evidence) -> None:
    store_root, settings, _stages = real_evidence
    vector = _bound_vector(store_root)
    audit = _run_audit_replay(settings, vector=vector)
    assert audit.status == "PASS", audit.error
    assert audit.assurance == "full"
    assert audit.recomputed == {"tdx-miner": 1.0}
    assert audit.agrees_with_vector is True
    assert audit.receipt_hotkeys == ["tdx-miner"]
    assert audit.report_signing_key_id
    assert audit.verifier_binary_digest
    assert audit.signed_index
    assert audit.signed_index["latest"] == {
        "source_epoch": audit.source_epoch,
        "manifest": audit.manifest_digest,
    }


def test_real_audit_flags_a_diverging_vector(real_evidence) -> None:
    store_root, settings, _stages = real_evidence
    vector = _bound_vector(store_root)
    vector["weights"] = [
        {
            "miner_hotkey": "tdx-miner",
            "weight": 0.5,
            "base_component": 0.0,
            "external_component": 0.5,
        },
        {
            "miner_hotkey": "sybil-miner",
            "weight": 0.5,
            "base_component": 0.0,
            "external_component": 0.5,
        },
    ]
    audit = _run_audit_replay(settings, vector=vector)
    assert audit.status == "PASS"  # the chain itself verified
    assert audit.agrees_with_vector is False
    assert any("sybil-miner" in item for item in audit.discrepancies)


def test_real_audit_fails_closed_on_tampered_report_blob(
    real_evidence, tmp_path
) -> None:
    store_root, settings, _stages = real_evidence
    manifest_digest = json.loads((store_root / "index.json").read_text())["latest"][
        "manifest"
    ]
    manifest = json.loads(
        (
            store_root / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]
        ).read_bytes()
    )
    report_blob = manifest["score_report"]["blob"]
    blob_path = store_root / "blobs" / "sha256" / report_blob.split(":", 1)[1]
    original = blob_path.read_bytes()
    try:
        blob_path.write_bytes(original.replace(b"20", b"99"))
        audit = _run_audit_replay(settings)
        assert audit.status == "FAIL"
        assert audit.error
    finally:
        blob_path.write_bytes(original)


def test_real_audit_rejects_wrong_network_pin(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(settings, network="testnet", netuid=292)
    assert audit.status == "FAIL"


def test_authority_requires_every_immutable_pin() -> None:
    settings = ProvenanceSettings(
        mode="authority",
        evidence_url="https://api.example",
        registry_keys="r.json",
        report_keys="p.json",
        index_keys="i.json",
        verifier_digest="sha256:" + "d" * 64,
    )
    with pytest.raises(ProvenanceAuditError, match="authority mode requires"):
        settings.validate_for_audit()


def test_fetcher_rejects_credentials_and_private_hosts() -> None:
    from scaffold.provenance_audit import _fetcher

    with pytest.raises(ProvenanceAuditError, match="credential-free"):
        _fetcher(
            ProvenanceSettings(
                mode="shadow", evidence_url="https://user:pw@host.example"
            )
        )
    with pytest.raises(ProvenanceAuditError, match="non-public address"):
        _fetcher(ProvenanceSettings(mode="shadow", evidence_url="https://127.0.0.1"))
    # The explicit dev flag permits it (connection itself not attempted here).
    _fetcher(
        ProvenanceSettings(
            mode="shadow",
            evidence_url="https://127.0.0.1",
            allow_private_hosts=True,
        )
    )


def test_index_rollback_and_equivocation_fences(real_evidence) -> None:
    """Counterexample 3, consumer side: a signed-but-older index, or the same
    epoch re-signed to a different manifest, must fail against durable state."""
    _store_root, settings, _stages = real_evidence
    good = _run_audit_replay(settings)
    assert good.status == "PASS"
    assert good.index_source_epoch == 11

    rollback = _run_audit_replay(
        settings,
        state={
            "provenance_index_epoch": 99,
            "provenance_index_manifest": "sha256:" + "f" * 64,
        },
    )
    assert rollback.status == "FAIL"
    assert "rollback" in rollback.error

    equivocation = _run_audit_replay(
        settings,
        state={
            "provenance_index_epoch": 11,
            "provenance_index_manifest": "sha256:" + "f" * 64,
        },
    )
    assert equivocation.status == "FAIL"
    assert "equivocation" in equivocation.error


def _state_after(audit) -> dict:
    return {
        "provenance_last_source_epoch": audit.source_epoch,
        "provenance_last_report_id": audit.report_id,
        "provenance_index_epoch": audit.index_source_epoch,
        "provenance_index_manifest": audit.index_manifest,
    }


def test_full_path_positive_revoked_restored(real_evidence) -> None:
    """Counterexample 13: the REAL run_audit full path (controlled envelopes,
    canonical strict verifier subprocess, receipt/report/manifest bindings)
    across positive -> revoked -> restored epochs, with rolling durable
    state, while the thin path stays unaffected."""
    from cathedral.evidence import EvidenceStore

    store_root, settings, stages = real_evidence
    store = EvidenceStore(store_root)
    state: dict = {}

    store.write_index(stages[11])
    positive = _run_audit_replay(settings, state=state)
    assert positive.status == "PASS", positive.error
    assert positive.assurance == "full"
    assert positive.recomputed == {"tdx-miner": 1.0}
    state = _state_after(positive)

    store.write_index(stages[12])
    revoked = _run_audit_replay(settings, state=state)
    assert revoked.status == "PASS", revoked.error
    assert revoked.recomputed == {}  # everything to burn
    # A signed rejected label is not independently replayable negative
    # evidence. The chain remains valid, but full assurance deliberately
    # downgrades and cannot become an authority vector.
    assert revoked.assurance == "receipts_only"
    state = _state_after(revoked)

    store.write_index(stages[13])
    restored = _run_audit_replay(settings, state=state)
    assert restored.status == "PASS", restored.error
    assert restored.assurance == "full"
    assert restored.recomputed == {"tdx-miner": 1.0}

    # Rolling back the index to the revoked epoch now fails the fences.
    store.write_index(stages[13])  # store guard: cannot re-publish 12 anyway
    stale = _run_audit_replay(settings, state=_state_after(restored))
    assert stale.status == "PASS"  # same epoch, same manifest: idempotent

    # Thin mapping is byte-identical regardless of audit outcomes.
    thin = validator_thin.vector_to_uid_weights(
        validated_supply_payload(),
        {"burn-hotkey": 0, "tdx-miner": 163},
        require_policy=validator_thin.REQUIRE_POLICY_VALIDATED_SUPPLY_V1,
    )
    assert thin == {0: pytest.approx(0.10), 163: pytest.approx(0.90)}


def test_authority_refuses_all_burn_without_replayable_negative_evidence(
    real_evidence, tmp_path, monkeypatch
) -> None:
    """A signed all-rejected epoch is valid but not independently FULL.

    The launch bundle publishes no raw candidate-specific negative evidence,
    so authority mode refuses rather than treating an operator assertion as a
    verified 100% burn decision. Thin mode still follows the authenticated
    signed vector and its hard burn policy.
    """
    import dataclasses
    import shutil

    from cathedral.evidence import EvidenceStore

    store_root, settings, stages = real_evidence
    # Private store copy: the shared store's producer guard (correctly)
    # refuses to move latest backwards after the lifecycle test ends at 13.
    private_root = tmp_path / "store-copy"
    shutil.copytree(store_root, private_root)
    (private_root / "index.json").unlink()
    (private_root / ".index-highwater.json").unlink(missing_ok=True)
    EvidenceStore(private_root).write_index(stages[12])
    settings = dataclasses.replace(settings, evidence_dir=str(private_root))
    revoked = _run_audit_replay(settings)
    assert revoked.status == "PASS"
    assert revoked.assurance == "receipts_only"
    assert revoked.recomputed == {}
    monkeypatch.setattr(validator_thin, "run_audit", lambda *a, **k: revoked)
    with pytest.raises(validator_thin.wire.VectorError, match="FULL assurance"):
        validator_thin._run_provenance_stage(
            _args(tmp_path, "authority"),
            validated_supply_payload(positive=False),
            tmp_path / "state.json",
        )


def test_cross_epoch_challenge_reuse_never_upgrades_full(real_evidence) -> None:
    """Defect-5 proof: the challenge is DERIVED from the anchored block hash,
    audience, epoch, and hotkey — a commitment from another epoch cannot
    derive for this one, so stale envelopes fail cryptographically (no
    forgetful replay cache involved)."""
    from cathedral.challenge import expected_challenge_digest
    from cathedral.provenance import (
        ProvenanceError,
        replay_positive_miners,
    )

    _store_root, _settings, _stages = real_evidence
    epoch_11 = expected_challenge_digest(
        block=100,
        block_hash="0x" + "ab" * 32,
        network="finney",
        netuid=39,
        source_epoch=11,
        miner_hotkey="tdx-miner",
    )
    epoch_13 = expected_challenge_digest(
        block=100,
        block_hash="0x" + "ab" * 32,
        network="finney",
        netuid=39,
        source_epoch=13,
        miner_hotkey="tdx-miner",
    )
    assert epoch_11 != epoch_13  # every (epoch, hotkey) slot is distinct

    import cathedral.provenance as provenance_module

    class _Miner:
        hotkey = "tdx-miner"
        receipt_verified = True
        measurement = "tdx-measurement-sha256:sample-v1"
        issued_at = "2026-07-24T00:00:00.000000Z"
        hardware_evidence_digest = "sha256:" + "0" * 64
        work_verified = True

    result = provenance_module.ProvenanceResult(
        report_id="sha256:" + "1" * 64,
        previous_report_id=None,
        signing_key_id="score-test-1",
        policy_release=1,
        policy_digest="sha256:" + "2" * 64,
        verifier_digest="sha256:" + "d" * 64,
        mechanism_id="validated_supply_v1",
        source_epoch=13,
        generated_at="2026-07-24T00:00:00.000000Z",
        valid_until="2026-07-24T01:00:00.000000Z",
        candidate_snapshot={
            "digest": "sha256:" + "6" * 64,
            "block": 100,
            "block_hash": "ab" * 32,
            "hotkeys": ["tdx-miner"],
        },
        miners=[_Miner()],
        recomputed_hotkey_weights={"tdx-miner": 1.0},
    )
    with pytest.raises(ProvenanceError, match="does not derive"):
        replay_positive_miners(
            result,
            registry=None,
            envelopes_by_hotkey={},
            attestation_bindings={
                "tdx-miner": {
                    "envelope_digest": "sha256:" + "3" * 64,
                    "evidence_digest": "sha256:" + "4" * 64,
                    "challenge_digest": epoch_11,  # stale epoch's commitment
                }
            },
            verifier_binary=b"",
            verifier_blob_digest="sha256:" + "5" * 64,
            verifier_command=("/x",),
            verifier_artifacts=("/x",),
            candidate_outcomes={"tdx-miner": "verified"},
            epoch_generated_at="2026-07-24T00:00:00.000000Z",
            challenge_anchor={
                "block": 100,
                "block_hash": "0x" + "ab" * 32,
                "network": "finney",
                "netuid": 39,
            },
            independent_candidates={"tdx-miner"},
            independent_block_hash="0x" + "ab" * 32,
        )


def test_authority_mode_refuses_private_host_bypass(tmp_path) -> None:
    """Defect-8 proof (subnet): authority + allow_private_hosts fails."""
    settings = ProvenanceSettings(
        mode="authority",
        evidence_url="https://api.example",
        registry_keys="r.json",
        registry_keys_digest="sha256:" + "0" * 64,
        report_keys="p.json",
        report_keys_digest="sha256:" + "0" * 64,
        index_keys="i.json",
        index_keys_digest="sha256:" + "0" * 64,
        verifier_digest="sha256:" + "d" * 64,
        source_revision="abc1234",
        verifier_binary="/x/verifier",
        controlled_dir="/x/controlled",
        allow_private_hosts=True,
    )
    with pytest.raises(ProvenanceAuditError, match="testing-only"):
        settings.validate_for_audit()


def test_fenced_state_two_thread_stale_and_equivocation(tmp_path) -> None:
    """Defect-8 counterexample: the authority high-water check and the
    reservation are ONE atomic flock transaction. Under real concurrent
    threads, a writer holding a STALE view (older epoch) RAISES instead of
    overwriting the newer reservation, and a same-epoch writer with a
    DIFFERENT manifest RAISES equivocation."""
    import threading

    state_file = tmp_path / "validator-state.json"
    reserved_12 = threading.Event()
    outcomes: dict[str, object] = {}

    def _writer(name, updates, wait_for=None, then_set=None):
        try:
            if wait_for is not None:
                assert wait_for.wait(timeout=10)
            validator_thin._write_state_fenced(state_file, updates)
            outcomes[name] = "ok"
        except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcomes[name] = exc
        finally:
            if then_set is not None:
                then_set.set()

    fresh = threading.Thread(
        target=_writer,
        args=(
            "fresh",
            {
                "provenance_index_epoch": 12,
                "provenance_index_manifest": "sha256:" + "a" * 64,
                "provenance_policy_release": 3,
                "provenance_policy_digest": "sha256:" + "b" * 64,
            },
        ),
        kwargs={"then_set": reserved_12},
    )
    stale = threading.Thread(
        target=_writer,
        args=(
            "stale",
            {
                "provenance_index_epoch": 11,
                "provenance_index_manifest": "sha256:" + "c" * 64,
            },
        ),
        kwargs={"wait_for": reserved_12},
    )
    equivocator = threading.Thread(
        target=_writer,
        args=(
            "equivocator",
            {
                "provenance_index_epoch": 12,
                "provenance_index_manifest": "sha256:" + "e" * 64,
            },
        ),
        kwargs={"wait_for": reserved_12},
    )
    for thread in (fresh, stale, equivocator):
        thread.start()
    for thread in (fresh, stale, equivocator):
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert outcomes["fresh"] == "ok"
    assert isinstance(outcomes["stale"], ValueError)
    assert "stale reservation" in str(outcomes["stale"])
    assert isinstance(outcomes["equivocator"], ValueError)
    assert "reservation equivocation" in str(outcomes["equivocator"])
    final = validator_thin._read_state(state_file)
    assert final["provenance_index_epoch"] == 12
    assert final["provenance_index_manifest"] == "sha256:" + "a" * 64

    # The policy line is fenced the same way ...
    with pytest.raises(ValueError, match="policy release 2 <"):
        validator_thin._write_state_fenced(
            state_file,
            {
                "provenance_policy_release": 2,
                "provenance_policy_digest": "sha256:" + "b" * 64,
            },
        )
    with pytest.raises(ValueError, match="same release, different digest"):
        validator_thin._write_state_fenced(
            state_file,
            {
                "provenance_policy_release": 3,
                "provenance_policy_digest": "sha256:" + "f" * 64,
            },
        )
    # ... while re-reserving the SAME (epoch, manifest) stays idempotent.
    validator_thin._write_state_fenced(
        state_file,
        {
            "provenance_index_epoch": 12,
            "provenance_index_manifest": "sha256:" + "a" * 64,
        },
    )


# ---------------------------------------------------------------------------
# Round-four defect 1: EXACT historical-metagraph equality
# ---------------------------------------------------------------------------


def test_full_audit_fails_on_omitted_historical_candidate(real_evidence) -> None:
    """A hotkey registered at the anchored block but missing from the
    manifest candidate set is an omission — FAIL, not a subset pass."""
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(
        settings,
        historical_hotkeys_lookup=lambda block: {"tdx-miner", "omitted-miner"},
    )
    assert audit.status == "FAIL"
    assert "manifest omits candidates" in audit.error
    assert "omitted-miner" in audit.error


def test_full_audit_fails_on_extra_manifest_candidate(real_evidence) -> None:
    """A manifest candidate that was NOT registered at the anchored block is
    fabricated membership — FAIL."""
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(
        settings,
        historical_hotkeys_lookup=lambda block: {"someone-else"},
    )
    assert audit.status == "FAIL"
    assert "not registered on the historical metagraph" in audit.error
    assert "tdx-miner" in audit.error


def test_full_audit_ignores_current_membership_drift(real_evidence) -> None:
    """The CURRENT metagraph is deliberately not an input to the audit: a
    miner deregistered today still audits cleanly against the HISTORICAL
    set at the anchored block. Only history proves the epoch."""
    _store_root, settings, _stages = real_evidence
    vector = None
    audit = _run_audit_replay(
        settings,
        vector=vector,
        # Simulated drift: today's chain no longer contains tdx-miner, but
        # the anchored-block history does — and history is what counts.
        historical_hotkeys_lookup=lambda block: {"tdx-miner"},
    )
    assert audit.status == "PASS"
    assert audit.assurance == "full"


def test_full_audit_is_not_proven_without_historical_lookups(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(
        settings, historical_hotkeys_lookup=None, block_hash_lookup=None
    )
    assert audit.status == "NOT_PROVEN"
    assert "historical chain lookups are unavailable" in audit.error


def test_full_audit_is_not_proven_when_history_is_unavailable(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
    unavailable = _run_audit_replay(
        settings, historical_hotkeys_lookup=lambda block: None
    )
    assert unavailable.status == "NOT_PROVEN"
    assert "historical metagraph" in unavailable.error

    def broken(block):
        raise RuntimeError("archive node down")

    raising = _run_audit_replay(settings, historical_hotkeys_lookup=broken)
    assert raising.status == "NOT_PROVEN"
    assert "historical metagraph lookup failed" in raising.error

    malformed = _run_audit_replay(
        settings, historical_hotkeys_lookup=lambda block: set()
    )
    assert malformed.status == "NOT_PROVEN"
    assert "malformed" in malformed.error


def test_full_audit_bounds_a_hung_historical_chain_client(real_evidence) -> None:
    import threading
    from dataclasses import replace

    _store_root, settings, _stages = real_evidence
    release = threading.Event()

    def hung_history(_block):
        release.wait(10)
        return {"tdx-miner"}

    started = time.monotonic()
    try:
        audit = _run_audit_replay(
            replace(settings, audit_deadline_secs=0.5),
            historical_hotkeys_lookup=hung_history,
        )
    finally:
        release.set()
    assert audit.status == "NOT_PROVEN"
    assert "historical metagraph lookup exceeded the audit deadline" in audit.error
    assert time.monotonic() - started < 1.5


def test_full_audit_is_not_proven_without_the_block_hash(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(settings, block_hash_lookup=lambda block: None)
    assert audit.status == "NOT_PROVEN"
    assert "unavailable" in audit.error

    mismatched = _run_audit_replay(
        settings, block_hash_lookup=lambda block: "0x" + "cd" * 32
    )
    assert mismatched.status == "FAIL"
    assert "does not match the independently queried chain" in mismatched.error


# ---------------------------------------------------------------------------
# Round-four defect 2: authority reserves under the fence BEFORE any PASS
# ---------------------------------------------------------------------------


def test_authority_reserves_before_pass_and_stale_auditor_cannot_pass(
    tmp_path, monkeypatch
) -> None:
    """Two threads audit from the SAME stale state; the newer reservation
    lands first. The stale/equivocating thread must raise WITHOUT emitting
    PASS and WITHOUT overwriting the newer reservation — and the fresh
    thread's fenced reservation is ordered strictly BEFORE its PASS event."""
    import threading

    timeline: list[tuple[str, str]] = []
    timeline_lock = threading.Lock()

    class _Recorder:
        def event(self, name, **_kw):
            with timeline_lock:
                timeline.append((threading.current_thread().name, name))

    monkeypatch.setattr(validator_thin, "_get_events", lambda args: _Recorder())

    audits = {
        "fresh": ProvenanceAudit(
            status="PASS",
            assurance="full",
            index_source_epoch=12,
            index_manifest="sha256:" + "a" * 64,
            policy_digest="sha256:" + "c" * 64,
            source_epoch=12,
            report_id="sha256:" + "b" * 64,
            policy_release=3,
            recomputed={"tdx-miner": 1.0},
        ),
        "stale": ProvenanceAudit(
            status="PASS",
            assurance="full",
            index_source_epoch=11,
            index_manifest="sha256:" + "d" * 64,
            policy_digest="sha256:" + "c" * 64,
            source_epoch=11,
            report_id="sha256:" + "e" * 64,
            policy_release=3,
            recomputed={"tdx-miner": 1.0},
        ),
    }

    def fake_run_audit(settings, **_kw):
        return audits[threading.current_thread().name]

    monkeypatch.setattr(validator_thin, "run_audit", fake_run_audit)
    real_fenced = validator_thin._write_state_fenced

    def traced_fenced(state_file, updates):
        with timeline_lock:
            timeline.append((threading.current_thread().name, "__FENCED_RESERVE__"))
        return real_fenced(state_file, updates)

    monkeypatch.setattr(validator_thin, "_write_state_fenced", traced_fenced)

    args = _args(tmp_path, "authority")
    state_file = tmp_path / "state.json"
    fresh_finished = threading.Event()
    outcomes: dict[str, object] = {}

    def runner(name, wait_for=None, then_set=None):
        try:
            if wait_for is not None:
                assert wait_for.wait(10)
            outcomes[name] = validator_thin._run_provenance_stage(args, {}, state_file)
        except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcomes[name] = exc
        finally:
            if then_set is not None:
                then_set.set()

    fresh = threading.Thread(
        target=runner,
        args=("fresh",),
        kwargs={"then_set": fresh_finished},
        name="fresh",
    )
    stale = threading.Thread(
        target=runner,
        args=("stale",),
        kwargs={"wait_for": fresh_finished},
        name="stale",
    )
    fresh.start()
    stale.start()
    for thread in (fresh, stale):
        thread.join(timeout=10)
        assert not thread.is_alive()

    # Fresh: reserved first, PASS second — never the other way around.
    assert outcomes["fresh"] == ("PASS", {"tdx-miner": 1.0})
    fresh_events = [name for who, name in timeline if who == "fresh"]
    assert fresh_events.index("__FENCED_RESERVE__") < fresh_events.index(
        "PROVENANCE_AUDIT_PASS"
    )

    # Stale: raises, emits NO PASS, and reports the refused reservation.
    assert isinstance(outcomes["stale"], validator_thin.wire.VectorError)
    assert "reservation refused" in str(outcomes["stale"])
    stale_events = [name for who, name in timeline if who == "stale"]
    assert "PROVENANCE_AUDIT_PASS" not in stale_events
    assert "PROVENANCE_RESERVATION_REFUSED" in stale_events

    # The newer reservation survives on disk.
    state = json.loads(state_file.read_text())
    assert state["provenance_index_epoch"] == 12
    assert state["provenance_index_manifest"] == "sha256:" + "a" * 64
    assert state["provenance_last_source_epoch"] == 12
    assert state["provenance_last_report_id"] == "sha256:" + "b" * 64


def test_fenced_state_pins_the_chain_identity(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    validator_thin._write_state_fenced(
        state_file, {"provenance_network": "finney", "provenance_netuid": 39}
    )
    with pytest.raises(ValueError, match="chain-identity mismatch"):
        validator_thin._write_state_fenced(
            state_file, {"provenance_network": "test", "provenance_netuid": 39}
        )
    with pytest.raises(ValueError, match="chain-identity mismatch"):
        validator_thin._write_state_fenced(
            state_file, {"provenance_network": "finney", "provenance_netuid": 40}
        )


# ---------------------------------------------------------------------------
# Round-four defect 5: bounded resolver slot pool (subnet side)
# ---------------------------------------------------------------------------


def test_audit_resolver_slot_pool_bounds_abandoned_lookups(monkeypatch) -> None:
    import socket
    import threading
    import time

    monkeypatch.setattr(provenance_audit, "_RESOLVER_SLOTS", None)
    release = threading.Event()

    def hung_resolver(*_a, **_k):
        release.wait(10)
        return [(socket.AF_INET, 0, 6, "", ("34.71.88.140", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", hung_resolver)
    baseline_threads = threading.active_count()

    for _ in range(provenance_audit.RESOLVER_SLOT_CAP):
        started = time.monotonic()
        with pytest.raises(ProvenanceAuditError, match="exceeded the audit deadline"):
            provenance_audit._getaddrinfo_bounded("example.com", 443, 0.001)
        assert time.monotonic() - started < 0.5

    started = time.monotonic()
    with pytest.raises(ProvenanceAuditError, match="capacity exhausted"):
        provenance_audit._getaddrinfo_bounded("example.com", 443, 0.001)
    assert time.monotonic() - started < 0.5
    assert threading.active_count() <= (
        baseline_threads + provenance_audit.RESOLVER_SLOT_CAP + 1
    )

    release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            assert provenance_audit._getaddrinfo_bounded("example.com", 443, 1.0)
            break
        except ProvenanceAuditError:
            time.sleep(0.05)
    else:
        pytest.fail("resolver slots were never released after completion")


def test_chain_lookup_obeys_audit_deadline_and_bounds_abandoned_clients(
    monkeypatch,
) -> None:
    import threading
    import time

    monkeypatch.setattr(provenance_audit, "_CHAIN_LOOKUP_SLOTS", None)
    release = threading.Event()

    initialization_barrier = threading.Barrier(12)
    initialized_slots: list[object] = []
    initialized_lock = threading.Lock()

    def initialize_concurrently() -> None:
        initialization_barrier.wait()
        candidate = provenance_audit._chain_lookup_slots()
        with initialized_lock:
            initialized_slots.append(candidate)

    initializers = [threading.Thread(target=initialize_concurrently) for _ in range(12)]
    for initializer in initializers:
        initializer.start()
    for initializer in initializers:
        initializer.join(timeout=2)
    assert all(not initializer.is_alive() for initializer in initializers)
    assert len(initialized_slots) == 12
    assert len({id(candidate) for candidate in initialized_slots}) == 1

    def hung_lookup(_block):
        release.wait(10)
        return {"tdx-miner"}

    baseline_threads = threading.active_count()
    try:
        for _ in range(provenance_audit.CHAIN_LOOKUP_SLOT_CAP):
            started = time.monotonic()
            with pytest.raises(ProvenanceUnavailable, match="audit deadline"):
                provenance_audit._chain_lookup_bounded(
                    hung_lookup,
                    100,
                    time.monotonic() + 0.01,
                    "historical metagraph lookup",
                )
            assert time.monotonic() - started < 0.5

        started = time.monotonic()
        with pytest.raises(ProvenanceUnavailable, match="capacity is exhausted"):
            provenance_audit._chain_lookup_bounded(
                hung_lookup,
                100,
                time.monotonic() + 0.01,
                "historical metagraph lookup",
            )
        assert time.monotonic() - started < 0.5
        assert threading.active_count() <= (
            baseline_threads + provenance_audit.CHAIN_LOOKUP_SLOT_CAP + 1
        )
    finally:
        release.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            assert provenance_audit._chain_lookup_bounded(
                lambda block: {str(block)},
                100,
                time.monotonic() + 1,
                "historical metagraph lookup",
            ) == {"100"}
            break
        except ProvenanceUnavailable:
            time.sleep(0.05)
    else:
        pytest.fail("chain lookup slots were never released after completion")


# ---------------------------------------------------------------------------
# Round-five: linearized authority tick (cross-process audit→reserve→submit)
# ---------------------------------------------------------------------------


def _authority_args(tmp_path: Path) -> SimpleNamespace:
    args = _args(tmp_path, "authority")
    args.offline = True
    args.broadcast = False
    args.wallet_name = "wallet"
    args.wallet_hotkey = "hotkey"
    args.provenance_burn_hotkey = "burn-hotkey"
    return args


def _epoch_audit(source_epoch: int, manifest_seed: str, report_seed: str):
    return ProvenanceAudit(
        status="PASS",
        assurance="full",
        index_source_epoch=source_epoch,
        index_manifest="sha256:" + manifest_seed * 64,
        policy_digest="sha256:" + "c" * 64,
        source_epoch=source_epoch,
        report_id="sha256:" + report_seed * 64,
        policy_release=3,
        recomputed={"tdx-miner": 1.0},
    )


def test_authority_tick_lock_forbids_newer_then_older_submission(
    tmp_path, monkeypatch
) -> None:
    """Round-five proof: the previously demonstrated interleaving — older
    epoch reserves, newer reserves AND submits, older still submits last —
    is impossible. The whole audit→reserve→submit sequence is ONE critical
    section per state file: while the older tick is inside (held at its
    submission point), the newer tick REFUSES before even auditing; run
    sequentially afterwards, submissions land strictly oldest→newest, so
    the newest submission is always last on-chain."""
    import threading

    audits = {"stale": _epoch_audit(11, "d", "e"), "fresh": _epoch_audit(12, "a", "b")}
    audit_calls: list[str] = []
    submissions: list[str] = []
    record_lock = threading.Lock()
    stale_at_submission = threading.Event()
    release_stale = threading.Event()

    def fake_run_audit(settings, **_kw):
        name = threading.current_thread().name
        with record_lock:
            audit_calls.append(name)
        return audits[name]

    def fake_set_weights(uid_weights, **_kw):
        name = threading.current_thread().name
        if name == "stale":
            # Hold the critical section AT THE SUBMISSION POINT: the fence
            # reservation already happened, the on-chain write has not.
            stale_at_submission.set()
            assert release_stale.wait(10)
        with record_lock:
            submissions.append(name)
        return True

    monkeypatch.setattr(validator_thin, "run_audit", fake_run_audit)
    monkeypatch.setattr(validator_thin, "set_weights_on_chain", fake_set_weights)

    args = _authority_args(tmp_path)
    state_file = Path(args.state_file)
    outcomes: dict[str, object] = {}

    def runner(name):
        try:
            outcomes[name] = validator_thin._authority_tick(args, None)
        except BaseException as exc:  # noqa: BLE001 - the outcome IS the assertion
            outcomes[name] = exc

    stale = threading.Thread(target=runner, args=("stale",), name="stale")
    stale.start()
    assert stale_at_submission.wait(10)

    # The newer tick arrives while the older one is mid-critical-section:
    # it must refuse BEFORE auditing and BEFORE submitting anything.
    fresh = threading.Thread(target=runner, args=("fresh",), name="fresh")
    fresh.start()
    fresh.join(timeout=10)
    assert not fresh.is_alive()
    assert isinstance(outcomes["fresh"], validator_thin.wire.VectorError)
    assert "refusing before audit or submission" in str(outcomes["fresh"])
    assert "fresh" not in audit_calls  # refused before the audit ran
    assert submissions == []  # and before ANY submission happened

    release_stale.set()
    stale.join(timeout=10)
    assert not stale.is_alive()
    assert outcomes["stale"] is True
    assert submissions == ["stale"]
    assert json.loads(state_file.read_text())["provenance_last_source_epoch"] == 11

    # Run the newer tick sequentially: submissions are strictly
    # oldest→newest, so the NEWEST weights are last on-chain — the reviewed
    # newer-then-older ordering cannot be produced.
    outcomes.clear()
    runner_thread = threading.Thread(target=runner, args=("fresh",), name="fresh")
    runner_thread.start()
    runner_thread.join(timeout=10)
    assert outcomes["fresh"] is True
    assert submissions == ["stale", "fresh"]
    state = json.loads(state_file.read_text())
    assert state["provenance_last_source_epoch"] == 12
    assert state["provenance_index_manifest"] == "sha256:" + "a" * 64


def test_authority_tick_lock_errors_refuse_before_submission(
    tmp_path, monkeypatch
) -> None:
    """Round-five: a broken lock (flock raising) refuses the tick before
    any audit or on-chain submission — fail closed, never fail open."""
    import fcntl
    import threading

    called = {"audit": 0, "submit": 0}

    def fake_run_audit(settings, **_kw):
        called["audit"] += 1
        return _epoch_audit(12, "a", "b")

    def fake_set_weights(uid_weights, **_kw):
        called["submit"] += 1
        return True

    monkeypatch.setattr(validator_thin, "run_audit", fake_run_audit)
    monkeypatch.setattr(validator_thin, "set_weights_on_chain", fake_set_weights)

    def broken_flock(descriptor, flags):
        raise OSError("lock storage failed")

    monkeypatch.setattr(fcntl, "flock", broken_flock)
    args = _authority_args(tmp_path)
    with pytest.raises(
        validator_thin.wire.VectorError, match="refusing before audit or submission"
    ):
        validator_thin._authority_tick(args, None)
    assert called == {"audit": 0, "submit": 0}
    assert threading.active_count() >= 1  # trivial liveness sanity


def test_thin_and_authority_share_one_submission_lock(tmp_path: Path) -> None:
    args = _authority_args(tmp_path)
    args.runtime_root = str(tmp_path / "runtime")
    with validator_thin._thin_tick_lock(args):
        with pytest.raises(
            validator_thin.wire.VectorError,
            match="cross-mode linearized single-flight",
        ):
            with validator_thin._authority_tick_lock(args):
                pytest.fail("authority entered while thin held the submission lock")
    with validator_thin._authority_tick_lock(args):
        with pytest.raises(
            validator_thin.wire.VectorError,
            match="cross-mode linearized single-flight",
        ):
            with validator_thin._thin_tick_lock(args):
                pytest.fail("thin entered while authority held the submission lock")


def test_common_finalization_survives_lane_telemetry_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalized authority write is never repeated when event I/O fails."""
    args = _authority_args(tmp_path)
    args.offline = False
    args.broadcast = True
    args.network = "finney"
    args.netuid = 39
    args.require_policy = "validated_supply_v1"
    state_file = Path(args.state_file)
    submissions: list[dict[int, float]] = []

    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={"tdx-miner": 163, "burn-hotkey": 204},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        commit_reveal_enabled=False,
        subnet_owner_hotkey="burn-hotkey",
        blocks_until_next_epoch=80,
        next_epoch_start_block=980,
        weights_rate_limit=0,
        validator_blocks_since_last_update=1,
        uid_mapping_stable_until_block=904,
        replacement_safe_hotkeys=frozenset({"tdx-miner", "burn-hotkey"}),
    )
    monkeypatch.setattr(validator_thin, "chain_preflight", lambda **_kw: preflight)
    monkeypatch.setattr(
        validator_thin,
        "_require_uid_mapping_stability",
        lambda *_args, **_kwargs: {
            "schema": "cathedral_sn39_uid_safety_v2",
            "registration": {"fixture": True},
            "rotation": {"status": "PASS", "targets": []},
        },
    )
    monkeypatch.setattr(
        validator_thin,
        "_historical_metagraph_lookup",
        lambda *_a: lambda _block: {"tdx-miner", "burn-hotkey"},
    )
    monkeypatch.setattr(
        validator_thin,
        "_block_hash_lookup",
        lambda *_a: lambda _block: "0x" + "a" * 64,
    )
    monkeypatch.setattr(
        validator_thin, "_continuous_transition_required", lambda _args: False
    )

    def provenance_stage(*_args, **_kwargs):
        validator_thin._write_state_fenced(
            state_file,
            {
                "provenance_network": "finney",
                "provenance_netuid": 39,
                "provenance_last_source_epoch": 12,
                "provenance_last_report_id": "sha256:" + "b" * 64,
                "provenance_index_epoch": 12,
                "provenance_index_manifest": "sha256:" + "c" * 64,
                "provenance_policy_release": 3,
                "provenance_policy_digest": "sha256:" + "d" * 64,
            },
        )
        args._authority_full_audit = ProvenanceAudit(
            status="PASS",
            assurance="full",
            report_generated_at="2026-07-24T00:00:00.000Z",
            report_valid_until="2099-01-01T00:00:00.000Z",
            report_valid_from_block=800,
            report_valid_until_block=1200,
        )
        return "PASS", {"tdx-miner": 1.0}

    monkeypatch.setattr(validator_thin, "_run_provenance_stage", provenance_stage)

    def submit(weights, **_kw):
        submissions.append(dict(weights))
        journal = validator_thin._read_state(
            validator_thin._submission_state_path(args)
        )
        validator_thin._record_pending_broadcast_intent(
            args,
            attempt_id=journal["submission_pending_id"],
            extrinsic_hash="0x" + "a" * 64,
            nonce=17,
            era_reference_block=900,
            mortal_period_blocks=4,
            version_key=validator_thin._weight_version_key(),
            wire_uids=[163, 204],
            wire_weights=[65535, 7282],
        )
        return validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "d" * 64,
            block_number=901,
            finalized=True,
        )

    monkeypatch.setattr(validator_thin, "set_weights_on_chain", submit)

    real_write_state_fenced = validator_thin._write_state_fenced

    def fail_lane_telemetry(path, updates):
        if "authority_submission_attempt_status" in updates:
            raise OSError("simulated lane telemetry failure")
        return real_write_state_fenced(path, updates)

    monkeypatch.setattr(
        validator_thin,
        "_write_state_fenced",
        fail_lane_telemetry,
    )

    with pytest.raises(OSError, match="lane telemetry"):
        validator_thin._authority_tick(args, None)
    lane_state = json.loads(state_file.read_text())
    assert "authority_submission_attempt_status" not in lane_state
    common_state = validator_thin._read_state(
        validator_thin._submission_state_path(args)
    )
    assert common_state["submission_pending_id"] is None
    assert common_state["submission_finalized_id"].startswith("sha256:")
    assert len(submissions) == 1

    with pytest.raises(
        validator_thin.wire.VectorError,
        match="attempt fence refused before chain write",
    ):
        validator_thin._authority_tick(args, None)
    assert len(submissions) == 1


def test_shadow_audit_stage_never_touches_the_submission_lock(
    tmp_path, monkeypatch
) -> None:
    """The audit worker alone never creates/acquires the cross-mode lock."""
    monkeypatch.setattr(validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "runtime")
    _stub_audit(monkeypatch, ProvenanceAudit(status="PASS", source_epoch=5))
    args = _authority_args(tmp_path)
    args.provenance = "shadow"
    args.runtime_root = str(tmp_path / "runtime")
    state_file = tmp_path / "state.json"
    status, _ = validator_thin._run_provenance_stage(args, {}, state_file)
    _drain_shadow(args)
    assert status == "PENDING"
    assert not validator_thin._submission_lock_path(args).exists()


# ---------------------------------------------------------------------------
# Round-six adversarial regressions
# ---------------------------------------------------------------------------


def test_thin_feed_fetch_rejects_malformed_and_private_endpoints(monkeypatch) -> None:
    """Round-six S2: userinfo, query, fragment, ambiguity, and non-public
    peers are all rejected fail-closed before any request is sent."""
    import socket

    for url, message in (
        ("http://publisher.example", "must be https"),
        ("https://user:pw@publisher.example", "credential-free"),
        ("https://publisher.example/?q=1", "no query or fragment"),
        ("https://publisher.example/#frag", "no query or fragment"),
        ("https://publisher.example/a b", "malformed"),
        ("https://publisher.example:notaport/x", "port is malformed"),
        ("https://", "no host"),
    ):
        with pytest.raises(validator_thin.wire.VectorError, match=message):
            validator_thin.fetch_vector(url)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, 0, 6, "", ("34.71.88.140", 443)),
            (socket.AF_INET, 0, 6, "", ("127.0.0.1", 443)),  # ONE bad peer
        ],
    )
    with pytest.raises(validator_thin.wire.VectorError, match="non-public address"):
        validator_thin.fetch_vector("https://publisher.example")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [
            (socket.AF_INET, 0, 6, "", ("100.64.0.1", 443)),
        ],
    )
    with pytest.raises(validator_thin.wire.VectorError, match="non-public address"):
        validator_thin.fetch_vector("https://publisher.example")


def test_thin_feed_fetch_refuses_redirects_and_oversized_bodies(monkeypatch) -> None:
    """Round-six S2: any non-200 (a redirect included) fails; the body is
    size-bounded; both under the pinned-peer connection."""
    import http.client
    import socket
    from types import SimpleNamespace as NS

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, 0, 6, "", ("34.71.88.140", 443))],
    )

    import ssl

    class _FakeSock:
        def settimeout(self, _t):
            pass

        def close(self):
            pass

    responses = {}

    class _FakeContext:
        def wrap_socket(self, _raw, server_hostname=None):
            assert server_hostname == "publisher.example"  # SNI hostname kept
            return _FakeSock()

    def fake_request(self, *_a, **_k):
        pass

    def fake_getresponse(self):
        return responses["current"]

    monkeypatch.setattr(socket, "create_connection", lambda _addr, _t: _FakeSock())
    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeContext())
    monkeypatch.setattr(http.client.HTTPSConnection, "request", fake_request)
    monkeypatch.setattr(http.client.HTTPSConnection, "getresponse", fake_getresponse)

    responses["current"] = NS(status=302, read=lambda n: b"")
    with pytest.raises(validator_thin.wire.VectorError, match="redirects are never"):
        validator_thin.fetch_vector("https://publisher.example")

    remaining = {"bytes": validator_thin.MAX_VECTOR_FETCH_BYTES + 2}

    def endless_read(n):
        chunk = b"x" * min(n, remaining["bytes"])
        remaining["bytes"] -= len(chunk)
        return chunk

    responses["current"] = NS(status=200, read=endless_read)
    with pytest.raises(validator_thin.wire.VectorError, match="bounded size limit"):
        validator_thin.fetch_vector("https://publisher.example")

    chunks = iter((b'{"weight":1e400}', b""))
    responses["current"] = NS(status=200, read=lambda _n: next(chunks))
    with pytest.raises(validator_thin.wire.VectorError, match="non-finite"):
        validator_thin.fetch_vector("https://publisher.example")


def test_receipts_only_shadow_pass_is_not_proven_and_never_persists(
    tmp_path, monkeypatch
) -> None:
    """Round-six S3: a receipts-only shadow PASS emits NOT_PROVEN — never
    PROVENANCE_AUDIT_PASS — and persists nothing."""
    events_seen: list[tuple[str, dict]] = []

    class _Recorder:
        def event(self, name, **kw):
            events_seen.append((name, kw))

    monkeypatch.setattr(validator_thin, "_get_events", lambda args: _Recorder())
    _stub_audit(
        monkeypatch,
        ProvenanceAudit(
            status="PASS",
            assurance="receipts_only",
            source_epoch=77,
            report_id="sha256:" + "a" * 64,
            index_source_epoch=77,
            index_manifest="sha256:" + "b" * 64,
            policy_release=3,
            policy_digest="sha256:" + "c" * 64,
            recomputed={"tdx-miner": 1.0},
            raw_replayed_hotkeys=["tdx-miner"],
            not_proven_reasons=[
                "non-verified anchored candidates lack replayable negative evidence"
            ],
        ),
    )
    state_file = tmp_path / "state.json"
    args = _args(tmp_path, "shadow")
    validator_thin._run_provenance_stage(args, {}, state_file)
    _drain_shadow(args)
    validator_thin._run_provenance_stage(args, {}, state_file)
    _drain_shadow(args)
    names = [name for name, _fields in events_seen]
    assert "PROVENANCE_AUDIT_NOT_PROVEN" in names
    assert "PROVENANCE_AUDIT_PASS" not in names
    not_proven = next(
        fields for name, fields in events_seen if name == "PROVENANCE_AUDIT_NOT_PROVEN"
    )
    assert "positive raw evidence replayed for 1 miner(s)" in not_proven["detail"]
    assert "replayable negative evidence" in not_proven["detail"]
    assert "raw evidence was not replayed" not in not_proven["detail"]
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    assert "provenance_last_source_epoch" not in state
    assert "provenance_index_epoch" not in state


def test_receipts_only_without_positive_replay_does_not_claim_one(
    tmp_path, monkeypatch
) -> None:
    events_seen: list[tuple[str, dict]] = []

    class _Recorder:
        def event(self, name, **kw):
            events_seen.append((name, kw))

    monkeypatch.setattr(validator_thin, "_get_events", lambda args: _Recorder())
    validator_thin._log_audit_events(
        _args(tmp_path, "shadow"),
        ProvenanceAudit(
            status="PASS",
            assurance="receipts_only",
            not_proven_reasons=["no positive raw replays"],
        ),
        tmp_path / "state.json",
    )
    fields = next(
        fields for name, fields in events_seen if name == "PROVENANCE_AUDIT_NOT_PROVEN"
    )
    assert "no positive raw evidence replayed" in fields["detail"]
    assert "positive raw evidence replayed for" not in fields["detail"]


@pytest.mark.parametrize("assurance", ["receipts_only", "full"])
def test_vector_mismatch_is_the_terminal_provenance_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    assurance: str,
) -> None:
    events_seen: list[str] = []

    class _Recorder:
        def event(self, name, **_kw):
            events_seen.append(name)

    monkeypatch.setattr(validator_thin, "_get_events", lambda _args: _Recorder())
    validator_thin._log_audit_events(
        _args(tmp_path, "shadow"),
        ProvenanceAudit(
            status="PASS",
            assurance=assurance,
            agrees_with_vector=False,
            discrepancies=["recomputed weight differs"],
        ),
        tmp_path / "state.json",
    )
    assert events_seen == ["PROVENANCE_VECTOR_MISMATCH"]


def test_output_surfaces_redact_paths_and_use_stable_error_codes(capsys) -> None:
    """Round-six S4: absolute filesystem paths and usernames never reach
    TTY/JSONL/lifecycle output; OS errors become stable errno codes."""
    import errno

    from scaffold.events import _neutralize, stable_error

    assert "<path>" in _neutralize(
        "state write failed at /Users/alice/secret/state.json"
    )
    assert "alice" not in _neutralize(
        "state write failed at /Users/alice/secret/state.json"
    )
    assert "<path>" in _neutralize("lock ~bob/launch/state.lock is held")
    assert "bob" not in _neutralize("lock ~bob/launch/state.lock is held")
    redacted_url = _neutralize(
        "https://alice:s3cr3t@example.invalid/path?token=still-secret"
    )
    assert "alice" not in redacted_url
    assert "s3cr3t" not in redacted_url
    assert "still-secret" not in redacted_url
    assert redacted_url == "<redacted-url>"
    malformed_userinfo = _neutralize(
        "https://alice:p@ss@example.invalid/path?token=still-secret"
    )
    assert malformed_userinfo == "<redacted-url>"
    assert "alice" not in malformed_userinfo
    assert "p@ss" not in malformed_userinfo
    for raw in (
        "wss://alice:s3cr3t@example.invalid/ws",
        "postgresql://alice:s3cr3t@example.invalid/db",
        "https://opaque-token@example.invalid/path",
    ):
        scrubbed = _neutralize(raw)
        assert "alice" not in scrubbed
        assert "s3cr3t" not in scrubbed
        assert "opaque-token" not in scrubbed
        assert scrubbed == "<redacted-url>"
    long_secret = "opaque-" + "x" * 800
    long_scrubbed = _neutralize(f"custom+ssh://{long_secret}@example.invalid/path")
    assert long_secret not in long_scrubbed
    assert long_scrubbed == "<redacted-url>"
    assert (
        stable_error(OSError(errno.EACCES, "denied", "/home/carol/x"))
        == "OSError[EACCES]"
    )
    assert "carol" not in stable_error(OSError(errno.EACCES, "denied", "/home/carol/x"))
    assert stable_error(ValueError("plain reason")).startswith(
        "ValueError: plain reason"
    )

    validator_thin._lifecycle("STATE failed", "path=/Users/dave/launch/state.json")
    line = capsys.readouterr().out
    assert "dave" not in line and "<path>" in line


def test_state_reader_migrates_safe_legacy_mode_and_rejects_unsafe_or_symlink(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "private-state"
    state_dir.mkdir(mode=0o755)
    state = state_dir / "state.json"
    state.write_text("{}")
    os.chmod(state, 0o644)
    assert validator_thin._read_state(state) == {}
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    os.chmod(state, 0o666)  # nosec B103 - intentional unsafe migration fixture
    with pytest.raises(ValueError, match="mode 0600"):
        validator_thin._read_state(state)
    os.chmod(state, 0o600)
    state.unlink()
    victim = state_dir / "victim.json"
    victim.write_text("{}")
    os.chmod(victim, 0o600)
    state.symlink_to(victim)
    with pytest.raises(OSError):
        validator_thin._read_state(state)


def test_cli_banner_never_prints_raw_endpoint_credentials_or_jsonl_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    secret_endpoint = (
        "https://alice:supersecret@example.invalid/path?token=URLSECRET#fragment"
    )
    secret_path = tmp_path / "private" / "validator-secret.jsonl"
    monkeypatch.setattr(validator_thin, "run", lambda _cfg: 0)
    assert (
        cli.main(
            [
                "serve",
                "--publisher-url",
                secret_endpoint,
                "--public-key-hex",
                "00" * 32,
                "--jsonl",
                str(secret_path),
                "--dry-run",
                "--once",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "supersecret" not in output
    assert "URLSECRET" not in output
    assert str(secret_path) not in output
    assert "publisher=<invalid-endpoint>" in output


def test_historical_lookup_validates_the_raw_sequence(real_evidence) -> None:
    """Round-six S5: duplicates, wrong-block metagraphs, malformed hotkeys,
    and non-sequences are all unavailable history (NOT_PROVEN) — never a
    silently deduplicated set."""
    validate = validator_thin._validated_historical_hotkeys
    assert validate(["a", "b"], metagraph_block=100, requested_block=100) == frozenset(
        {"a", "b"}
    )
    assert validate(["a", "a"], metagraph_block=100, requested_block=100) is None
    assert validate(["a"], metagraph_block=101, requested_block=100) is None
    assert validate(["a"], metagraph_block=None, requested_block=100) is None
    assert validate(["a"], metagraph_block=True, requested_block=100) is None
    assert validate("not-a-sequence", metagraph_block=100, requested_block=100) is None
    assert validate(["a", 7], metagraph_block=100, requested_block=100) is None
    assert validate([""], metagraph_block=100, requested_block=100) is None
    assert validate([], metagraph_block=100, requested_block=100) is None

    # End to end: a duplicated historical answer is NOT_PROVEN, not a pass.
    _store_root, settings, _stages = real_evidence
    audit = _run_audit_replay(
        settings,
        historical_hotkeys_lookup=lambda block: (
            validator_thin._validated_historical_hotkeys(
                ["tdx-miner", "tdx-miner"], metagraph_block=100, requested_block=block
            )
        ),
    )
    assert audit.status == "NOT_PROVEN"


def test_authority_lock_refuses_symlinks_and_unsafe_modes(
    tmp_path, monkeypatch
) -> None:
    """Round-six S6: a symlinked or group/other-accessible lock target is
    refused BEFORE any audit or submission; nonblocking semantics and the
    shadow path stay untouched."""
    import os

    called = {"audit": 0, "submit": 0}
    monkeypatch.setattr(
        validator_thin,
        "run_audit",
        lambda *a, **k: (
            called.__setitem__("audit", called["audit"] + 1)
            or _epoch_audit(12, "a", "b")
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "set_weights_on_chain",
        lambda *a, **k: (
            called.__setitem__("submit", called["submit"] + 1)
            or validator_thin.ChainSubmission(
                success=True,
                extrinsic_hash="0x" + "a" * 64,
                block_hash="0x" + "d" * 64,
                block_number=123,
                finalized=True,
            )
        ),
    )
    args = _authority_args(tmp_path)
    monkeypatch.setattr(validator_thin, "_VALIDATOR_RUNTIME_ROOT", tmp_path / "runtime")
    args.runtime_root = str(tmp_path / "runtime")
    lock_path = validator_thin._submission_lock_path(args)
    lock_path.parent.mkdir(mode=0o700, parents=True)

    # Symlinked lock target: O_NOFOLLOW refuses at open.
    victim = tmp_path / "victim.file"
    victim.write_text("x")
    lock_path.symlink_to(victim)
    with pytest.raises(validator_thin.wire.VectorError, match="refusing"):
        validator_thin._authority_tick(args, None)
    lock_path.unlink()

    # Unsafe pre-existing mode (group/other access) is refused.
    lock_path.touch(mode=0o600)
    # Deliberately create the unsafe mode that the production code must reject.
    os.chmod(lock_path, 0o666)  # nosec B103
    with pytest.raises(validator_thin.wire.VectorError, match="refusing"):
        validator_thin._authority_tick(args, None)
    os.chmod(lock_path, 0o600)
    assert called == {"audit": 0, "submit": 0}

    # A safe lock file proceeds normally (nonblocking semantics intact).
    assert validator_thin._authority_tick(args, None) is True
    assert called == {"audit": 1, "submit": 1}


# ---------------------------------------------------------------------------
# Round-seven adversarial regressions
# ---------------------------------------------------------------------------

# The fixture's deterministic key seeds (module-scope literals above).
_R7_REPORT_SEED = bytes(range(64, 96))
_R7_INDEX_SEED = bytes(range(96, 128))


def _index_rows(index_bytes: bytes) -> tuple[dict, list[dict]]:
    document = json.loads(index_bytes)
    return document["latest"], document["recent"]


def _store_blob(store_root: Path, digest: str) -> bytes:
    return (store_root / "blobs" / "sha256" / digest.split(":", 1)[1]).read_bytes()


def _private_store(real_evidence, tmp_path: Path):
    """A writable clone of the fixture store with the index guard reset, so
    round-seven tests can publish adversarial indexes without perturbing the
    shared module-scoped store."""
    import dataclasses
    import shutil

    store_root, settings, stages = real_evidence
    private_root = tmp_path / "store-copy"
    shutil.copytree(store_root, private_root)
    (private_root / "index.json").unlink(missing_ok=True)
    (private_root / ".index-highwater.json").unlink(missing_ok=True)
    return (
        private_root,
        dataclasses.replace(settings, evidence_dir=str(private_root)),
        stages,
    )


def _resign_report(report_bytes: bytes, **overrides) -> bytes:
    """Re-sign the fixture report with modified fields (the tests hold the
    fixture's signing seed, exactly like a compromised-producer adversary)."""
    from cathedral.score_class import _sign_report

    document = json.loads(report_bytes)
    document.pop("signature", None)
    document.pop("report_id", None)
    document.update(overrides)
    return _sign_report(document, _R7_REPORT_SEED)


def _rebuild_manifest_with_report(store, manifest_bytes: bytes, report_bytes: bytes):
    """A canonical manifest identical to ``manifest_bytes`` but binding the
    supplied report blob; returns the new manifest digest."""
    from cathedral.policy_registry import canonical_json

    manifest = json.loads(manifest_bytes)
    report = json.loads(report_bytes)
    manifest["score_report"] = dict(manifest["score_report"])
    manifest["score_report"]["blob"] = store.put_blob(report_bytes)
    manifest["score_report"]["report_id"] = report["report_id"]
    return store.put_blob(canonical_json(manifest))


def _sign_index(latest_epoch: int, latest_manifest: str, recent: list[dict]) -> bytes:
    from cathedral.evidence import build_signed_index

    return build_signed_index(
        network="finney",
        netuid=39,
        latest_source_epoch=latest_epoch,
        latest_manifest_digest=latest_manifest,
        recent=recent,
        signing_key_id="evidence-index-test-1",
        private_key_seed=_R7_INDEX_SEED,
    )


def test_finalized_block_rejects_missing_and_malformed() -> None:
    """Round-seven F1: only a positive integral block survives coercion —
    missing, boolean, fractional, junk, and non-positive values are None
    (and authority refuses on None instead of skipping validity checks)."""
    coerce = validator_thin._finalized_block
    assert coerce(200) == 200
    assert coerce("200") == 200
    assert coerce(200.0) == 200
    assert coerce(None) is None
    assert coerce(True) is None
    assert coerce(False) is None
    assert coerce("abc") is None
    assert coerce(200.5) is None
    assert coerce(float("nan")) is None
    assert coerce(-5) is None
    assert coerce(0) is None


def test_authority_refuses_to_audit_without_a_finalized_block(
    tmp_path, monkeypatch
) -> None:
    """Round-seven F1: a metagraph snapshot without a usable finalized block
    refuses the authority tick BEFORE the audit — current_block=None must
    never silently skip the report block-validity check — while a genuine
    block flows into the audit unchanged."""
    called = {"audit": 0}

    def fake_run_audit(settings, **_kw):
        called["audit"] += 1
        return _epoch_audit(12, "a", "b")

    monkeypatch.setattr(validator_thin, "run_audit", fake_run_audit)
    monkeypatch.setattr(validator_thin, "set_weights_on_chain", lambda *a, **k: True)
    args = _authority_args(tmp_path)
    args.offline = False
    args.runtime_root = str(tmp_path / "runtime")
    missing_block = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={"burn-hotkey": 0, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=None,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        commit_reveal_enabled=False,
    )
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kw: missing_block,
    )
    with pytest.raises(validator_thin.wire.VectorError, match="finalized integer"):
        validator_thin._authority_tick(args, None)
    assert called["audit"] == 0  # refused BEFORE any audit ran

    seen_blocks: list = []

    def recording_run_audit(settings, *, current_block=None, **_kw):
        seen_blocks.append(current_block)
        return _epoch_audit(12, "a", "b")

    monkeypatch.setattr(validator_thin, "run_audit", recording_run_audit)
    finalized = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={"burn-hotkey": 0, "tdx-miner": 163},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=200,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        commit_reveal_enabled=False,
    )
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kw: finalized,
    )
    args._tick_preflight = None
    assert validator_thin._authority_tick(args, None) is True
    assert seen_blocks == [200]  # the real block reached the audit


def test_run_audit_authority_gate_requires_current_block(real_evidence) -> None:
    """Round-seven F1 (audit layer): a fully pinned authority audit without
    a finalized integer block FAILs outright instead of skipping the
    validity-window check; with a genuine in-window block it passes."""
    import dataclasses

    _store_root, settings, _stages = real_evidence
    authority = dataclasses.replace(settings, mode="authority")
    for bad_block in (None, True, 0, -3):
        audit = _run_audit_replay(authority, current_block=bad_block)
        assert audit.status == "FAIL"
        assert "finalized integer chain block" in audit.error


def test_report_block_window_enforced_with_a_real_block(
    real_evidence, tmp_path
) -> None:
    """Round-seven F1 counterexample: a report valid for blocks [100, 101)
    audited at finalized block 200 FAILS — exactly the check the silent
    current_block=None used to skip — while block 100 (in window) passes."""
    from cathedral.evidence import EvidenceStore

    private_root, settings, stages = _private_store(real_evidence, tmp_path)
    store = EvidenceStore(private_root)
    latest11, _recent11 = _index_rows(stages[11])
    manifest11 = _store_blob(private_root, latest11["manifest"])
    report11 = _store_blob(private_root, json.loads(manifest11)["score_report"]["blob"])
    narrow = _resign_report(report11, valid_until_block=101)
    narrow_manifest = _rebuild_manifest_with_report(store, manifest11, narrow)
    store.write_index(_sign_index(11, narrow_manifest, []))

    inside = _run_audit_replay(settings, current_block=100)
    assert inside.status == "PASS", inside.error

    outside = _run_audit_replay(settings, current_block=200)
    assert outside.status == "FAIL"
    assert "validity window" in outside.error


def test_recent_chain_walk_recovers_after_missed_epochs(
    real_evidence, tmp_path
) -> None:
    """Round-seven F2 counterexample: after auditing epoch 11, a consumer
    whose next observation is epoch 13 walks the SIGNED recent chain through
    the missed epoch 12 and recovers — instead of wedging forever on the
    predecessor check."""
    from cathedral.evidence import EvidenceStore

    private_root, settings, stages = _private_store(real_evidence, tmp_path)
    store = EvidenceStore(private_root)
    store.write_index(stages[11])
    first = _run_audit_replay(settings)
    assert first.status == "PASS", first.error
    assert first.source_epoch == 11
    state = _state_after(first)

    store.write_index(stages[13])  # 12 was published while we were away
    recovered = _run_audit_replay(settings, state=state)
    assert recovered.status == "PASS", recovered.error
    assert recovered.source_epoch == 13
    assert recovered.assurance == "full"
    assert recovered.recomputed == {"tdx-miner": 1.0}

    # And the durable state now advances to 13: the wedge is gone for good.
    after = _run_audit_replay(settings, state=_state_after(recovered))
    assert after.status == "PASS"  # idempotent at the new tip


def test_recent_chain_missing_intermediate_fails_closed(
    real_evidence, tmp_path
) -> None:
    """Round-seven F2: a signed index whose recent list omits the bridging
    epoch cannot bridge the gap — the audit FAILs instead of accepting an
    unverifiable chain."""
    from cathedral.evidence import EvidenceStore

    private_root, settings, stages = _private_store(real_evidence, tmp_path)
    store = EvidenceStore(private_root)
    store.write_index(stages[11])
    state = _state_after(_run_audit_replay(settings))

    latest13, recent13 = _index_rows(stages[13])
    hole = [row for row in recent13 if row["source_epoch"] != 12]
    store.write_index(_sign_index(13, latest13["manifest"], hole))
    wedged = _run_audit_replay(settings, state=state)
    assert wedged.status == "FAIL"
    assert "bridge the gap" in wedged.error


def test_recent_chain_forked_intermediate_fails_closed(real_evidence, tmp_path) -> None:
    """Round-seven F2: an intermediate whose report does NOT cite the
    recorded predecessor (a fork, even correctly signed) breaks the walk —
    equivocation fences survive the recovery path."""
    from cathedral.evidence import EvidenceStore

    private_root, settings, stages = _private_store(real_evidence, tmp_path)
    store = EvidenceStore(private_root)
    store.write_index(stages[11])
    state = _state_after(_run_audit_replay(settings))

    latest12, _ = _index_rows(stages[12])
    manifest12 = _store_blob(private_root, latest12["manifest"])
    report12 = _store_blob(private_root, json.loads(manifest12)["score_report"]["blob"])
    forked_report = _resign_report(report12, previous_report_id="sha256:" + "9" * 64)
    forked_manifest = _rebuild_manifest_with_report(store, manifest12, forked_report)
    latest13, recent13 = _index_rows(stages[13])
    forged_recent = [
        {"source_epoch": 12, "manifest": forked_manifest}
        if row["source_epoch"] == 12
        else row
        for row in recent13
    ]
    store.write_index(_sign_index(13, latest13["manifest"], forged_recent))
    forked = _run_audit_replay(settings, state=state)
    assert forked.status == "FAIL"
    assert "recent-chain link for epoch 12" in forked.error


def test_recent_chain_walk_is_bounded(real_evidence) -> None:
    """Round-seven F2: the walk hard-bounds its window BEFORE any blob is
    fetched, even if an index verifier regression admitted a longer list."""
    _store_root, settings, _stages = real_evidence
    bound = provenance_audit.MAX_RECENT_WALK
    rows = [
        {"source_epoch": epoch, "manifest": "sha256:" + f"{epoch:064x}"}
        for epoch in range(12, 12 + bound + 1)
    ]
    with pytest.raises(ProvenanceAuditError, match="bounded window"):
        provenance_audit._verify_recent_chain_bridge(
            rows,
            settings=settings,
            network="finney",
            netuid=39,
            registry_keys={},
            report_keys={},
            state={},
            last_epoch=11,
            last_report_id="sha256:" + "a" * 64,
            latest_epoch=12 + bound + 5,
            latest_previous_report_id="sha256:" + "b" * 64,
            load_blob=lambda digest: (_ for _ in ()).throw(
                AssertionError("no blob fetch may precede the bound check")
            ),
        )


def test_required_ci_collection_gate_returns_zero() -> None:
    """Round-seven F3: the required workflow's collection gate must find the
    anchor test and exit 0. Supported pytest under -qq prints only a count
    (no node ids), so the old `-q --co -q` form exited 1 before the suite."""
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[3]
    workflow = repo_root / ".github" / "workflows" / "two-mode-provenance.yml"
    text = workflow.read_text()
    assert "-q --co -q" not in text  # the broken double-quiet form is gone
    assert "--collect-only -q" in text
    command = (
        f"{sys.executable} -m pytest "
        "scaffold/publisher/tests/test_validator_two_mode.py "
        "--collect-only -q | grep -q test_full_path_positive_revoked_restored"
    )
    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=repo_root,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _once_args(tmp_path: Path) -> SimpleNamespace:
    args = _args(tmp_path, "shadow")
    args.once = True
    args.interval_secs = 0.01
    args.offline = True
    args.broadcast = False
    args.require_policy = None
    return args


def test_pending_receipt_unavailability_emits_not_proven_and_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen: list[tuple[str, str | None]] = []

    class _Recorder:
        def event(self, name, **fields):
            events_seen.append((name, fields.get("status")))

    monkeypatch.setattr(validator_thin, "_get_events", lambda _args: _Recorder())
    monkeypatch.setattr(
        validator_thin,
        "_recover_pending_launch_receipt",
        lambda _args: (_ for _ in ()).throw(
            validator_thin._PendingReceiptNotProven("archive unavailable")
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "tick",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("a fenced receipt must stop before a new tick")
        ),
    )
    assert validator_thin.run(_once_args(tmp_path)) == 1
    assert ("PENDING_RECEIPT_NOT_PROVEN", "NOT_PROVEN") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def test_pending_receipt_contradiction_emits_fail_and_stops_before_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen: list[tuple[str, str | None]] = []

    class _Recorder:
        def event(self, name, **fields):
            events_seen.append((name, fields.get("status")))

    monkeypatch.setattr(validator_thin, "_get_events", lambda _args: _Recorder())
    monkeypatch.setattr(
        validator_thin,
        "_recover_pending_launch_receipt",
        lambda _args: (_ for _ in ()).throw(
            validator_thin._PostSignedSubmissionMismatch(
                "positive historical contradiction"
            )
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "tick",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("a contradictory signed attempt must stop before a tick")
        ),
    )

    assert validator_thin.run(_once_args(tmp_path)) == 1
    assert ("PENDING_RECEIPT_CONTRADICTION", "FAIL") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def _online_recovery_args(tmp_path: Path) -> SimpleNamespace:
    args = _once_args(tmp_path)
    args.offline = False
    args.broadcast = True
    return args


def _record_recovery_events(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str | None]]:
    events_seen: list[tuple[str, str | None]] = []

    class _Recorder:
        def event(self, name, **fields):
            events_seen.append((name, fields.get("status")))

    monkeypatch.setattr(validator_thin, "_get_events", lambda _args: _Recorder())
    monkeypatch.setattr(
        validator_thin, "_validate_runtime_contract", lambda _args: None
    )
    monkeypatch.setattr(
        validator_thin,
        "tick",
        lambda _args: (_ for _ in ()).throw(
            AssertionError("startup recovery failure must stop before a tick")
        ),
    )
    return events_seen


def test_startup_recovery_malformed_journal_emits_fail_before_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen = _record_recovery_events(monkeypatch)
    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _args: tmp_path / "submission.json",
    )
    monkeypatch.setattr(
        validator_thin,
        "_read_state",
        lambda _path: {
            "submission_pending_id": "not-a-digest",
            "submission_pending_lane": "thin",
        },
    )

    assert validator_thin.run(_online_recovery_args(tmp_path)) == 1
    assert events_seen[0][0] == "STARTUP"
    assert ("PENDING_RECEIPT_CONTRADICTION", "FAIL") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def test_startup_recovery_state_io_failure_emits_not_proven_before_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen = _record_recovery_events(monkeypatch)
    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(validator_thin, "_thin_tick_lock", lambda _args: nullcontext())
    monkeypatch.setattr(
        validator_thin,
        "_submission_state_path",
        lambda _args: tmp_path / "submission.json",
    )
    monkeypatch.setattr(
        validator_thin,
        "_read_state",
        lambda _path: (_ for _ in ()).throw(OSError("state fsync unavailable")),
    )

    assert validator_thin.run(_online_recovery_args(tmp_path)) == 1
    assert events_seen[0][0] == "STARTUP"
    assert ("PENDING_RECEIPT_NOT_PROVEN", "NOT_PROVEN") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def test_startup_recovery_preflight_failure_emits_not_proven_before_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen = _record_recovery_events(monkeypatch)
    monkeypatch.setattr(
        validator_thin,
        "_prepare_tick_preflight",
        lambda _args: (_ for _ in ()).throw(
            validator_thin.wire.VectorError("RPC unavailable")
        ),
    )

    assert validator_thin.run(_online_recovery_args(tmp_path)) == 1
    assert events_seen[0][0] == "STARTUP"
    assert ("PENDING_RECEIPT_NOT_PROVEN", "NOT_PROVEN") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def test_startup_recovery_lock_failure_emits_not_proven_before_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events_seen = _record_recovery_events(monkeypatch)

    class _UnavailableLock:
        def __enter__(self):
            raise validator_thin.wire.VectorError("submission lock unavailable")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(validator_thin, "_prepare_tick_preflight", lambda _args: None)
    monkeypatch.setattr(
        validator_thin, "_thin_tick_lock", lambda _args: _UnavailableLock()
    )

    assert validator_thin.run(_online_recovery_args(tmp_path)) == 1
    assert events_seen[0][0] == "STARTUP"
    assert ("PENDING_RECEIPT_NOT_PROVEN", "NOT_PROVEN") in events_seen
    assert not any(name == "TICK_FAILED" for name, _status in events_seen)


def test_once_mode_drains_and_reports_the_shadow_audit(tmp_path, monkeypatch) -> None:
    """Round-seven F4: --once must not exit while the shadow audit daemon is
    still running — the outcome is awaited within the documented bound and
    reported, and the exit code stays truthful."""
    events_seen: list[str] = []

    class _Recorder:
        def event(self, name, **_kw):
            events_seen.append(name)

    monkeypatch.setattr(validator_thin, "_get_events", lambda args: _Recorder())
    monkeypatch.setattr(
        validator_thin,
        "run_audit",
        lambda settings, **_kw: ProvenanceAudit(
            status="FAIL", error="endpoint died", duration_ms=1.0
        ),
    )

    def fake_tick(a):
        validator_thin._run_provenance_stage(
            a, validated_supply_payload(), Path(a.state_file)
        )
        return True

    monkeypatch.setattr(validator_thin, "tick", fake_tick)
    args = _once_args(tmp_path)
    assert validator_thin.run(args) == 1
    assert "PROVENANCE_AUDIT_FAIL" in events_seen  # outcome captured, not lost
    assert "PROVENANCE_HEALTH_GATE_FAILED" in events_seen


def test_once_mode_exits_zero_only_for_full_agreeing_shadow_audit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        validator_thin,
        "run_audit",
        lambda settings, **_kw: ProvenanceAudit(
            status="PASS",
            assurance="full",
            agrees_with_vector=True,
            source_epoch=1,
            report_id="sha256:" + "a" * 64,
            index_source_epoch=1,
            index_manifest="sha256:" + "b" * 64,
            policy_release=1,
            policy_digest="sha256:" + "c" * 64,
        ),
    )

    def fake_tick(a):
        validator_thin._run_provenance_stage(
            a, validated_supply_payload(), Path(a.state_file)
        )
        return True

    monkeypatch.setattr(validator_thin, "tick", fake_tick)
    assert validator_thin.run(_once_args(tmp_path)) == 0


def test_once_mode_fails_when_full_audit_state_cannot_persist(
    tmp_path, monkeypatch
) -> None:
    events_seen: list[tuple[str, str | None]] = []

    class _Recorder:
        def event(self, name, **fields):
            events_seen.append((name, fields.get("status")))

    monkeypatch.setattr(validator_thin, "_get_events", lambda _args: _Recorder())
    monkeypatch.setattr(
        validator_thin,
        "run_audit",
        lambda settings, **_kw: ProvenanceAudit(
            status="PASS",
            assurance="full",
            agrees_with_vector=True,
            source_epoch=1,
            report_id="sha256:" + "a" * 64,
            index_source_epoch=1,
            index_manifest="sha256:" + "b" * 64,
            policy_release=1,
            policy_digest="sha256:" + "c" * 64,
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "_write_state_fenced",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated persistence failure")
        ),
    )

    def fake_tick(a):
        validator_thin._run_provenance_stage(
            a, validated_supply_payload(), Path(a.state_file)
        )
        return True

    monkeypatch.setattr(validator_thin, "tick", fake_tick)
    assert validator_thin.run(_once_args(tmp_path)) == 1
    assert ("PROVENANCE_STATE_WRITE_FAILED", "NOT_PROVEN") in events_seen
    assert not any(name == "PROVENANCE_AUDIT_PASS" for name, _status in events_seen)


def test_once_mode_truthful_exit_when_audit_cannot_be_captured(
    tmp_path, monkeypatch
) -> None:
    """Round-seven F4: when the audit cannot complete within the documented
    bound, --once says so — a stable UNRESOLVED event and a nonzero exit —
    instead of silently reporting success."""
    import dataclasses
    import threading as threading_module

    release = threading_module.Event()
    events_seen: list[str] = []

    class _Recorder:
        def event(self, name, **_kw):
            events_seen.append(name)

    monkeypatch.setattr(validator_thin, "_get_events", lambda args: _Recorder())

    def hung_audit(settings, **_kw):
        release.wait(10.0)
        return ProvenanceAudit(status="PASS")

    monkeypatch.setattr(validator_thin, "run_audit", hung_audit)
    real_settings = validator_thin._provenance_settings
    monkeypatch.setattr(
        validator_thin,
        "_provenance_settings",
        lambda args: dataclasses.replace(real_settings(args), audit_deadline_secs=0.2),
    )

    def fake_tick(a):
        validator_thin._run_provenance_stage(
            a, validated_supply_payload(), Path(a.state_file)
        )
        return True

    monkeypatch.setattr(validator_thin, "tick", fake_tick)
    args = _once_args(tmp_path)
    try:
        assert validator_thin.run(args) == 1  # truthful: outcome NOT captured
        assert "PROVENANCE_AUDIT_UNRESOLVED" in events_seen
    finally:
        release.set()


def test_shadow_completion_race_is_lossless_and_exactly_once(
    tmp_path, monkeypatch
) -> None:
    """Round-seven F5 counterexample: audit A completes BETWEEN drain() and
    the next submit(); completed audit B must not overwrite unreported A.
    Every completed audit is drained exactly once, in completion order."""
    import threading as threading_module

    gate_a = threading_module.Event()
    audit_a = ProvenanceAudit(
        status="PASS", source_epoch=1, report_id="sha256:" + "a" * 64
    )
    audit_b = ProvenanceAudit(
        status="PASS", source_epoch=2, report_id="sha256:" + "b" * 64
    )
    outputs = iter([audit_a, audit_b])
    gates = iter([gate_a, None])

    def scripted_audit(settings, **_kw):
        gate = next(gates)
        if gate is not None:
            assert gate.wait(5.0)
        return next(outputs)

    monkeypatch.setattr(validator_thin, "run_audit", scripted_audit)
    auditor = validator_thin._ShadowAuditor()
    submit_kwargs = {
        "network": "finney",
        "netuid": 39,
        "payload": {},
        "state": {},
        "state_file": tmp_path / "state.json",
    }
    assert auditor.submit(None, **submit_kwargs)
    assert auditor.drain() == []  # the tick drained BEFORE A finished
    gate_a.set()
    auditor._thread.join(5.0)  # A completes between drain() and submit()
    assert auditor.submit(None, **submit_kwargs)  # B admitted: A's thread done
    auditor._thread.join(5.0)  # B completes as well
    drained = auditor.drain()
    assert [item[0] for item in drained] == [audit_a, audit_b]  # lossless
    assert auditor.drain() == []  # exactly once


def test_thin_feed_fetch_tries_every_validated_address(monkeypatch) -> None:
    """Round-seven F6: a dead first resolved address must not fail the thin
    feed fetch — the healthy second (already validated public) address
    serves it, with TLS/SNI still for the ORIGINAL hostname."""
    import http.client
    import socket
    import ssl
    from types import SimpleNamespace as NS

    monkeypatch.setattr(
        provenance_audit,
        "_getaddrinfo_bounded",
        lambda host, port, timeout: [
            (socket.AF_INET, 0, 6, "", ("34.71.88.140", 443)),
            (socket.AF_INET, 0, 6, "", ("34.71.88.141", 443)),
        ],
    )
    attempts: list[str] = []

    class _FakeSock:
        def settimeout(self, _t):
            pass

        def close(self):
            pass

    def fake_create_connection(address, _timeout=None):
        attempts.append(address[0])
        if address[0] == "34.71.88.140":
            raise ConnectionRefusedError("dead peer")
        return _FakeSock()

    class _FakeContext:
        def wrap_socket(self, _raw, server_hostname=None):
            assert server_hostname == "publisher.example"  # SNI: hostname
            return _FakeSock()

    sent = {"done": False}

    def read_body(n):
        if sent["done"]:
            return b""
        sent["done"] = True
        return b'{"ok": true}'

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeContext())
    monkeypatch.setattr(
        http.client.HTTPSConnection, "request", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        http.client.HTTPSConnection,
        "getresponse",
        lambda self: NS(status=200, read=read_body),
    )
    assert validator_thin.fetch_vector("https://publisher.example") == {"ok": True}
    assert attempts == ["34.71.88.140", "34.71.88.141"]


def test_thin_feed_body_cap_is_shared_across_address_attempts(monkeypatch) -> None:
    """Round-seven F6: a peer that streams the whole cap and then dies
    cannot reset the body budget by failing over — the aggregate cap spans
    every address attempt."""
    import http.client
    import socket
    import ssl
    from types import SimpleNamespace as NS

    monkeypatch.setattr(
        provenance_audit,
        "_getaddrinfo_bounded",
        lambda host, port, timeout: [
            (socket.AF_INET, 0, 6, "", ("34.71.88.140", 443)),
            (socket.AF_INET, 0, 6, "", ("34.71.88.141", 443)),
        ],
    )
    attempts: list[str] = []

    class _FakeSock:
        def settimeout(self, _t):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, _timeout=None: attempts.append(address[0]) or _FakeSock(),
    )

    class _FakeContext:
        def wrap_socket(self, _raw, server_hostname=None):
            return _FakeSock()

    responses = {"attempt": 0}
    cap = validator_thin.MAX_VECTOR_FETCH_BYTES

    def fake_getresponse(self):
        responses["attempt"] += 1
        if responses["attempt"] == 1:
            state = {"remaining": cap}

            def stream_then_die(n):
                if state["remaining"] <= 0:
                    raise ConnectionResetError("mid-body death")
                chunk = b"x" * min(n, state["remaining"])
                state["remaining"] -= len(chunk)
                return chunk

            return NS(status=200, read=stream_then_die)
        # Attempt 2: ANY further byte must exceed the shared budget.
        return NS(status=200, read=lambda n: b"y")

    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeContext())
    monkeypatch.setattr(
        http.client.HTTPSConnection, "request", lambda self, *a, **k: None
    )
    monkeypatch.setattr(http.client.HTTPSConnection, "getresponse", fake_getresponse)
    with pytest.raises(validator_thin.wire.VectorError, match="bounded size limit"):
        validator_thin.fetch_vector("https://publisher.example")
    assert attempts == ["34.71.88.140", "34.71.88.141"]


def _patched_evidence_transport(
    monkeypatch,
    *,
    dns_delay: float = 0.0,
    read_delay: float = 0.0,
    connect_delay: float = 0.0,
    dead: frozenset = frozenset(),
    body: bytes = b'{"i": 1}',
    status: int = 200,
) -> list[str]:
    """Fake DNS/socket/TLS/HTTP plumbing for evidence _fetcher tests; the
    returned list records connection attempts in order."""
    import http.client
    import socket
    import ssl
    from types import SimpleNamespace as NS

    def fake_resolver(host, port, timeout):
        if dns_delay:
            time.sleep(dns_delay)
        return [
            (socket.AF_INET, 0, 6, "", ("34.71.88.140", 443)),
            (socket.AF_INET, 0, 6, "", ("34.71.88.141", 443)),
        ]

    monkeypatch.setattr(provenance_audit, "_getaddrinfo_bounded", fake_resolver)
    attempts: list[str] = []

    class _FakeSock:
        def settimeout(self, _t):
            pass

        def close(self):
            pass

    def fake_create_connection(address, _timeout=None):
        attempts.append(address[0])
        if connect_delay:
            time.sleep(connect_delay)
        if address[0] in dead:
            raise ConnectionRefusedError("dead peer")
        return _FakeSock()

    class _FakeContext:
        def wrap_socket(self, _raw, server_hostname=None):
            assert server_hostname == "evidence.example"  # SNI: hostname
            return _FakeSock()

    sent = {"done": False}

    def fake_read(n):
        if read_delay:
            time.sleep(read_delay)
        if sent["done"]:
            return b""
        sent["done"] = True
        return body

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeContext())
    monkeypatch.setattr(
        http.client.HTTPSConnection, "request", lambda self, *a, **k: None
    )
    monkeypatch.setattr(
        http.client.HTTPSConnection,
        "getresponse",
        lambda self: NS(status=status, read=fake_read),
    )
    return attempts


def test_evidence_fetcher_tries_every_validated_address(monkeypatch) -> None:
    """Round-seven F6: the evidence fetcher fails over from a dead first
    address to the healthy second one instead of failing the whole audit."""
    attempts = _patched_evidence_transport(
        monkeypatch, dead=frozenset({"34.71.88.140"}), body=b'{"i": 1}'
    )
    settings = ProvenanceSettings(
        mode="shadow", evidence_url="https://evidence.example"
    )
    load_index, _load_blob = provenance_audit._fetcher(settings)
    assert load_index() == b'{"i": 1}'
    assert attempts == ["34.71.88.140", "34.71.88.141"]


def test_evidence_fetcher_stops_failover_when_budget_is_exhausted(
    monkeypatch,
) -> None:
    """Round-seven F6: address failover never outlives the total deadline —
    once the budget is spent, the next address is NOT tried."""
    attempts = _patched_evidence_transport(
        monkeypatch,
        connect_delay=0.06,
        dead=frozenset({"34.71.88.140", "34.71.88.141"}),
    )
    settings = ProvenanceSettings(
        mode="shadow",
        evidence_url="https://evidence.example",
        audit_deadline_secs=0.05,
    )
    load_index, _load_blob = provenance_audit._fetcher(settings)
    with pytest.raises(ProvenanceAuditError, match="total deadline"):
        load_index()
    assert attempts == ["34.71.88.140"]  # the second address was never dialed


def test_audit_deadline_starts_before_dns_and_rebounds_every_phase(
    monkeypatch,
) -> None:
    """Round-seven F7 counterexample: 0.04s of DNS plus a 0.04s body read
    must FAIL a 0.05s whole-audit budget. Previously the deadline started
    after DNS and a stale socket allowance survived into body reads, so
    this exact sequence succeeded."""
    _patched_evidence_transport(monkeypatch, dns_delay=0.04, read_delay=0.04)
    settings = ProvenanceSettings(
        mode="shadow",
        evidence_url="https://evidence.example",
        audit_deadline_secs=0.05,
    )
    load_index, _load_blob = provenance_audit._fetcher(settings)
    with pytest.raises(ProvenanceAuditError, match="total deadline"):
        load_index()


def test_named_evidence_fetch_refuses_redirects(monkeypatch) -> None:
    _patched_evidence_transport(monkeypatch, status=302)
    settings = ProvenanceSettings(
        mode="shadow",
        evidence_url="https://evidence.example",
    )
    _load_index, _load_blob, fetch_named = provenance_audit._fetcher(
        settings,
        include_raw_fetch=True,
    )
    with pytest.raises(ProvenanceAuditError, match="redirects are never"):
        fetch_named("/release.json")
    with pytest.raises(ProvenanceAuditError, match="path is malformed"):
        fetch_named("/../private")


def test_mainnet_launch_bundle_is_byte_pinned_and_shadow_by_default() -> None:
    """The public copy-paste config must resolve to the reviewed launch pins.

    A final newline or a stale digest is security-relevant here: validators
    pin the exact public-key file bytes before trusting signed evidence.
    """
    root = Path(__file__).resolve().parents[3]
    expected = {
        "registry_keys": (
            "registry-keys.json",
            "sha256:5fb8f00cd2541606927373f596c2ba77d4ce485df0539f4afd5091858af48512",
        ),
        "report_keys": (
            "report-keys.json",
            "sha256:30e438fff5b0508402b233eb5eec590a834882801a552edbbf7e62e45cf98c70",
        ),
        "index_keys": (
            "index-keys.json",
            "sha256:1e35b9ce36b3da3362a88feb93dfa90f1fe03ab7c42e902b13ac3789324f7611",
        ),
    }
    config_path = root / "config" / "validator-mainnet-sn39.toml"
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)

    assert config["network"] == {
        "name": "finney",
        "netuid": 39,
        "wallet_name": "validator",
        "validator_hotkey": "default",
    }
    assert config["weight_policy"]["require_policy"] == "validated_supply_v1"
    assert config["weight_policy"]["public_key_hex"] == (
        "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"
    )
    assert config["weight_policy"]["key_id"] == "cathedral-weight-policy"
    assert config["provenance"]["mode"] == "shadow"
    assert config["provenance"]["mechanism"] == "validated_supply_v1"
    assert config["provenance"]["verifier_digest"] == (
        "sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
    )
    assert config["provenance"]["source_revision"] == (
        "655c264421a1f5f2e625a372a40f595aa1e114ab"
    )

    for config_key, (name, digest) in expected.items():
        path = root / "config" / "provenance" / name
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest
        assert config["provenance"][config_key] == f"config/provenance/{name}"
        assert config["provenance"][config_key + "_digest"] == digest
    release_key_path = root / "config" / "provenance" / "release-attestation-keys.json"
    assert "sha256:" + hashlib.sha256(release_key_path.read_bytes()).hexdigest() == (
        "sha256:1a60a22de160853d460b22853a426d0534fab4df0fe9f89e5859d60bb4ed3d12"
    )

    captured: list[SimpleNamespace] = []
    with (
        mock.patch.dict(os.environ, {}, clear=True),
        mock.patch.object(
            validator_thin,
            "run",
            side_effect=lambda resolved: captured.append(resolved) or 0,
        ),
    ):
        assert (
            cli.main(
                [
                    "serve",
                    "--config",
                    str(config_path),
                    "--dry-run",
                    "--once",
                ]
            )
            == 0
        )
    assert len(captured) == 1
    resolved = captured[0]
    assert resolved.broadcast is False
    assert resolved.once is True
    assert resolved.public_key_hex == config["weight_policy"]["public_key_hex"]
    assert resolved.key_id == config["weight_policy"]["key_id"]
    assert resolved.provenance == "shadow"
    assert (
        resolved.provenance_verifier_digest == config["provenance"]["verifier_digest"]
    )


def test_public_key_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    duplicate = (
        b'{"same":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",'
        b'"same":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}'
    )
    path = tmp_path / "keys.json"
    path.write_bytes(duplicate)
    digest = "sha256:" + hashlib.sha256(duplicate).hexdigest()
    with pytest.raises(ProvenanceAuditError, match="must map key ids"):
        provenance_audit._load_pubkeys(str(path), digest, "test keys")


def test_public_reproduction_assertion_rejects_shadow_failures(tmp_path: Path) -> None:
    from scripts.assert_sn39_public_reproduction import (
        EXPECTED_STARTUP,
        ReproductionError,
        assert_current_dry_run,
    )

    path = tmp_path / "events.jsonl"
    base = [
        {
            "event": "STARTUP",
            "status": "INFO",
            "detail": "submission_authority=thin provenance=shadow",
            **EXPECTED_STARTUP,
        },
        {
            "event": "WEIGHTS_DRY_RUN",
            "status": "PASS",
            "detail": "authority=thin uids=2 burn_share=0.100000",
            "authority": "thin",
            "uid_count": 2,
            "burn_uid": 204,
            "burn_share": 0.1,
            "uid_weights": {"163": 0.9, "204": 0.1},
            "wire_uids": [163, 204],
            "wire_weights": [65535, 7282],
            "version_key": 10005000,
            "mapping_block": 8694000,
            "validator_uid": 30,
            "validator_hotkey": "validator-hotkey",
        },
        {
            "event": "PROVENANCE_AUDIT_PASS",
            "status": "PASS",
            "detail": "whole-epoch FULL assurance established",
            "vector_agrees": True,
        },
    ]
    path.write_text("\n".join(json.dumps(event) for event in base) + "\n")
    assert assert_current_dry_run(path)["current_dry_run"] == "PASS"

    path.write_text(
        "\n".join(
            json.dumps(event)
            for event in [
                *base,
                {
                    "event": "PROVENANCE_VECTOR_MISMATCH",
                    "status": "FAIL",
                    "detail": "counterexample",
                },
            ]
        )
        + "\n"
    )
    with pytest.raises(ReproductionError, match="fail-closed"):
        assert_current_dry_run(path)

    twenty_uids = [dict(event) for event in base]
    twenty_uids[1]["detail"] = "authority=thin uids=20 burn_share=0.100000"
    twenty_uids[1]["uid_count"] = 20
    path.write_text("\n".join(json.dumps(event) for event in twenty_uids) + "\n")
    with pytest.raises(ReproductionError, match="rewarded/burn"):
        assert_current_dry_run(path)

    wrong_uids = [dict(event) for event in base]
    wrong_uids[1]["burn_uid"] = 999
    wrong_uids[1]["uid_weights"] = {"7": 0.9, "999": 0.1}
    path.write_text("\n".join(json.dumps(event) for event in wrong_uids) + "\n")
    with pytest.raises(ReproductionError, match="rewarded/burn"):
        assert_current_dry_run(path)

    alternate = [dict(event) for event in base]
    alternate[0]["publisher_url"] = "https://alternate.example"
    path.write_text("\n".join(json.dumps(event) for event in alternate) + "\n")
    with pytest.raises(ReproductionError, match="resolved launch pins"):
        assert_current_dry_run(path)

    wrong_burn = [dict(event) for event in base]
    wrong_burn[1]["burn_share"] = 0.2
    path.write_text("\n".join(json.dumps(event) for event in wrong_burn) + "\n")
    with pytest.raises(ReproductionError, match="rewarded/burn"):
        assert_current_dry_run(path)


def test_receipts_only_mismatch_reaches_public_assertion(tmp_path: Path) -> None:
    """Exercise the real logger-to-assertion path for the former fail-open."""
    from scaffold.events import EventLogger
    from scripts.assert_sn39_public_reproduction import (
        EXPECTED_STARTUP,
        ReproductionError,
        assert_current_dry_run,
    )

    path = tmp_path / "events.jsonl"
    logger = EventLogger(mode="thin", jsonl_path=str(path), tty=None)
    logger.event(
        "STARTUP",
        stage="startup",
        status="INFO",
        detail="submission_authority=thin provenance=shadow",
        **EXPECTED_STARTUP,
    )
    logger.event(
        "WEIGHTS_DRY_RUN",
        stage="submit",
        status="PASS",
        detail="authority=thin uids=2 burn_share=0.100000",
        authority="thin",
        uid_count=2,
        burn_uid=204,
        burn_share=0.1,
        uid_weights={"163": 0.9, "204": 0.1},
        wire_uids=[163, 204],
        wire_weights=[65535, 7282],
        version_key=10005000,
        mapping_block=8694000,
        validator_uid=30,
        validator_hotkey="validator-hotkey",
    )
    args = SimpleNamespace(_events=logger)
    validator_thin._log_audit_events(
        args,
        ProvenanceAudit(
            status="PASS",
            assurance="receipts_only",
            agrees_with_vector=False,
            discrepancies=["tdx-miner weight differs"],
            remediation="inspect evidence",
            not_proven_reasons=["negative evidence unavailable"],
        ),
        tmp_path / "state.json",
    )
    logger.close()
    with pytest.raises(ReproductionError, match="fail-closed"):
        assert_current_dry_run(path)


def test_event_log_explicit_reader_group_is_exactly_0640(tmp_path: Path) -> None:
    from scaffold.events import EventLogger

    path = tmp_path / "validator-events.jsonl"
    group = grp.getgrgid(os.getegid()).gr_name
    logger = EventLogger(
        mode="thin",
        jsonl_path=str(path),
        jsonl_group=group,
        tty=None,
    )
    logger.event("STARTUP", stage="startup", status="INFO")
    logger.close()

    metadata = path.stat()
    assert metadata.st_gid == os.getegid()
    assert metadata.st_mode & 0o777 == 0o640


def test_event_log_without_reader_group_remains_private(tmp_path: Path) -> None:
    from scaffold.events import EventLogger

    path = tmp_path / "validator-events.jsonl"
    logger = EventLogger(mode="thin", jsonl_path=str(path), tty=None)
    logger.event("STARTUP", stage="startup", status="INFO")
    logger.close()
    assert path.stat().st_mode & 0o777 == 0o600


def test_cli_to_tick_to_current_assertion_and_immutable_reproducer(
    tmp_path: Path, monkeypatch
) -> None:
    """Current health is explicit; immutable proof never substitutes its feed."""
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        assert_current_dry_run,
    )
    from scripts.run_sn39_public_reproduction import run as run_reproduction

    root = Path(__file__).resolve().parents[3]
    config = root / "config/validator-mainnet-sn39.toml"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    vector = validated_supply_payload()
    vector.update(
        {
            "vector_id": "launch-vector",
            "policy_version": 1,
            "key_id": "cathedral-weight-policy",
            "network": "finney",
            "netuid": 39,
            "generated_at": "2026-07-24T22:00:00.000Z",
            "expires_at": "2026-07-24T23:00:00.000Z",
        }
    )
    monkeypatch.setattr(
        validator_thin,
        "fetch_vector",
        lambda _url: vector,
    )
    monkeypatch.setattr(validator_thin, "accept_vector", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kw: validator_thin.ChainPreflight(
            wallet=object(),
            subtensor=object(),
            hotkey_to_uid={"burn-hotkey": 204, "tdx-miner": 163},
            validator_hotkey="validator-hotkey",
            validator_uid=30,
            block=8694000,
            min_allowed_weights=1,
            max_weight_limit=1.0,
            genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
            commit_reveal_enabled=False,
        ),
    )
    monkeypatch.setattr(
        validator_thin,
        "run_audit",
        lambda *_args, **_kw: ProvenanceAudit(
            status="PASS",
            assurance="full",
            agrees_with_vector=True,
            source_epoch=77,
            report_id="sha256:" + "a" * 64,
            index_source_epoch=77,
            index_manifest="sha256:" + "b" * 64,
            policy_release=1,
            policy_digest="sha256:" + "c" * 64,
            mechanism="validated_supply_v1",
            recomputed={"tdx-miner": 1.0},
            receipt_hotkeys=["tdx-miner"],
            raw_replayed_hotkeys=["tdx-miner"],
            not_proven_reasons=[],
        ),
    )
    with mock.patch.dict(os.environ, {}, clear=True):
        assert (
            cli.main(
                [
                    "serve",
                    "--config",
                    str(config),
                    "--state-file",
                    str(state),
                    "--runtime-root",
                    str(tmp_path / "runtime"),
                    "--jsonl",
                    str(events),
                    "--dry-run",
                    "--once",
                ]
            )
            == 0
        )
    result = assert_current_dry_run(events)
    assert result["current_dry_run"] == "PASS"
    immutable = run_reproduction(
        release_result={
            "release_attestation": "PASS",
            "historical_launch": "PASS",
            "evidence_checkpoint": "PASS",
            "evidence_candidate_set": "PASS",
            "reproducer_revision": "a" * 40,
        },
    )
    assert immutable["public_recomputation"] == "PASS"
    with pytest.raises(ReproductionError, match="candidate set"):
        run_reproduction(
            release_result={
                "release_attestation": "PASS",
                "historical_launch": "PASS",
                "evidence_checkpoint": "PASS",
                "evidence_candidate_set": "NOT_PROVEN",
                "reproducer_revision": "a" * 40,
            },
        )


def _fixture_uid_safety(hotkeys: list[str]) -> dict[str, object]:
    rotation_block_hash = "0x" + "9" * 64
    rotation_timestamp = "2026-07-24T21:00:00.000Z"

    def target(
        *,
        uid: int,
        coldkey: str,
        extrinsic_hash: str,
        extrinsic_index: int,
    ) -> dict[str, object]:
        old_hotkey = f"old-{hotkeys[uid]}"
        return {
            "uid": uid,
            "hotkey": hotkeys[uid],
            "coldkey": coldkey,
            "last_hotkey_swap_block": 99,
            "hotkey_swap_safe_until_block": 7299,
            "swap_lock": "active",
            "pending_coldkey_swap": None,
            "hotkey_successor": None,
            "hotkey_root": old_hotkey,
            "rotation_receipt": {
                "call": "swap_hotkey_v2",
                "extrinsic_hash": extrinsic_hash,
                "block_hash": rotation_block_hash,
                "block_number": 99,
                "block_timestamp": rotation_timestamp,
                "extrinsic_index": extrinsic_index,
                "coldkey": coldkey,
                "old_hotkey": old_hotkey,
                "new_hotkey": hotkeys[uid],
                "netuid": 39,
                "keep_stake": False,
                "event": "HotkeySwappedOnSubnet",
            },
            "registration_replacement_safe": True,
        }

    return {
        "schema": "cathedral_sn39_uid_safety_v2",
        "stability_basis": "operator_controlled_coldkeys",
        "registration": {
            "max_uids": 256,
            "max_regs_per_block": 1,
            "immunity_period": 15000,
            "min_nonimmune_uids": 10,
            "block_at_registration": [
                {
                    "uid": uid,
                    "hotkey": hotkey,
                    "block_at_registration": 0,
                }
                for uid, hotkey in enumerate(hotkeys)
            ],
            "subnet_owner_coldkey": "subnet-owner-coldkey",
            "owned_hotkeys": [hotkeys[204]],
            "immune_owner_uids_limit": 1,
            "free_uid_slots": 51,
            "maximum_era_registrations": 4,
            "owner_immortal_hotkeys": [hotkeys[204]],
            "replacement_safe_hotkeys": sorted(hotkeys),
        },
        "rotation": {
            "status": "PASS",
            "mapping_block": 100,
            "mapping_block_hash": "0x" + "a" * 64,
            "mortal_period_blocks": 4,
            "era_last_block": 103,
            "hotkey_swap_on_subnet_interval": 7200,
            "coldkey_swap_announcement_delay": 36000,
            "targets": [
                target(
                    uid=163,
                    coldkey="coldkey-163",
                    extrinsic_hash="0x" + "1" * 64,
                    extrinsic_index=0,
                ),
                target(
                    uid=204,
                    coldkey="subnet-owner-coldkey",
                    extrinsic_hash="0x" + "2" * 64,
                    extrinsic_index=1,
                ),
            ],
        },
    }


def _fixture_freshness_boundary() -> dict[str, object]:
    return {
        "schema": "cathedral_sn39_post_rotation_evidence_v2",
        "rotation_floor_block": 99,
        "rotation_floor_timestamp": "2026-07-24T21:00:00.000Z",
        "candidate_block": 100,
        "candidate_block_hash": "0x" + "a" * 64,
        "manifest_generated_at": "2026-07-24T21:30:00.000Z",
        "report_generated_at": "2026-07-24T21:45:00.000Z",
        "report_valid_from_block": 100,
        "vector_generated_at": "2026-07-24T22:00:00.000Z",
        "index_generated_at": "2026-07-24T22:00:00.000Z",
    }


def _unlocked_uid_safety(*, locked_uids: set[int]) -> dict[str, object]:
    """Fixture UID safety where only ``locked_uids`` still hold a live lock."""
    uid_safety = _fixture_uid_safety([f"hotkey-{uid}" for uid in range(205)])
    for target in uid_safety["rotation"]["targets"]:
        if target["uid"] in locked_uids:
            continue
        target["swap_lock"] = "never_rotated"
        target["last_hotkey_swap_block"] = 0
        target["hotkey_swap_safe_until_block"] = None
        target["hotkey_root"] = None
        target["rotation_receipt"] = None
    return uid_safety


def _boundary_audit(*, candidate_block: int) -> ProvenanceAudit:
    return ProvenanceAudit(
        status="PASS",
        assurance="full",
        agrees_with_vector=True,
        recomputed={"tdx-miner": 1.0},
        manifest_generated_at="2026-07-24T21:30:00.000Z",
        candidate_block=candidate_block,
        candidate_block_hash="0x" + "a" * 64,
        report_generated_at="2026-07-24T21:45:00.000Z",
        report_valid_until="2099-01-01T00:00:00.000Z",
        report_valid_from_block=candidate_block,
        report_valid_until_block=candidate_block + 100,
        signed_index={"generated_at": "2026-07-24T22:00:00.000Z"},
    )


def test_launch_evidence_boundary_without_rotations_has_a_null_floor() -> None:
    boundary = validator_thin._require_launch_evidence_after_rotations(
        payload={"generated_at": "2026-07-24T22:00:00.000Z"},
        audit=_boundary_audit(candidate_block=1),
        uid_safety=_unlocked_uid_safety(locked_uids=set()),
    )
    assert boundary["schema"] == "cathedral_sn39_post_rotation_evidence_v2"
    assert boundary["rotation_floor_block"] is None
    assert boundary["rotation_floor_timestamp"] is None
    assert boundary["candidate_block"] == 1
    assert boundary["candidate_block_hash"] == "0x" + "a" * 64
    assert boundary["vector_generated_at"] == "2026-07-24T22:00:00.000Z"
    assert boundary["index_generated_at"] == "2026-07-24T22:00:00.000Z"


@pytest.mark.parametrize("locked_uids", [{204}, {163, 204}])
def test_launch_evidence_boundary_enforces_every_proven_rotation(
    locked_uids: set[int],
) -> None:
    uid_safety = _unlocked_uid_safety(locked_uids=locked_uids)
    boundary = validator_thin._require_launch_evidence_after_rotations(
        payload={"generated_at": "2026-07-24T22:00:00.000Z"},
        audit=_boundary_audit(candidate_block=100),
        uid_safety=uid_safety,
    )
    assert boundary["rotation_floor_block"] == 99
    assert boundary["rotation_floor_timestamp"] == "2026-07-24T21:00:00.000Z"
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="strictly after every proven target rotation",
    ):
        validator_thin._require_launch_evidence_after_rotations(
            payload={"generated_at": "2026-07-24T22:00:00.000Z"},
            audit=_boundary_audit(candidate_block=99),
            uid_safety=uid_safety,
        )


def test_release_attestation_binds_exact_reproducer_revision() -> None:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from scripts.assert_sn39_public_reproduction import (
        EXPECTED_PRODUCER_REVISION,
        EXPECTED_RELEASE_PINS,
        RELEASE_KEY_ID,
        ReproductionError,
        verify_release_bytes,
    )

    revision = "a" * 40
    hotkeys = [f"hotkey-{uid}" for uid in range(205)]
    vector = {
        "vector_id": "launch-vector-id",
        "policy_version": 77,
        "network": "finney",
        "netuid": 39,
        "key_id": "cathedral-weight-policy",
        "generated_at": "2026-07-24T22:00:00.000Z",
        "expires_at": "2026-07-24T23:00:00.000Z",
        "weights": [{"miner_hotkey": hotkeys[163], "weight": 1.0}],
        "burn_snapshot": {
            "burn_hotkey": hotkeys[204],
            "burn_uid": None,
            "forced_burn_percentage": 10.0,
        },
        "policy_metadata": {
            "confidential_primary": {
                "contract_version": "v1",
                "mode": "confidential_primary",
                "source": "cathedral_confidential_tdx",
                "base_mass": 0.0,
                "confidential_mass": 1.0,
                "complete": True,
                "fresh": True,
                "confirmed": True,
            },
            "validated_supply": {
                "contract_version": "v2",
                "intel_tdx_allocation": 0.90,
                "fixed_burn_allocation": 0.10,
                "burn_hotkey": hotkeys[204],
            },
        },
        "signature": "fixture",
    }
    vector_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest_digest = "sha256:" + "b" * 64
    release = {
        "schema": "cathedral_sn39_provenance_release_v2",
        "network": "finney",
        "netuid": 39,
        "validated_capability": "intel_tdx_cpu",
        "submission_authority_default": "thin",
        "full_provenance_mode": "concurrent_shadow",
        "claim": "SN39 mainnet: validated Intel TDX CPU compute.",
        "reward_mechanism": {
            "id": "validated_supply_v1",
            "revision": 1,
            "validated_supply_share": 0.9,
            "burn_share": 0.1,
            "wire_quantization": {
                "weights_u16": [65535, 7282],
                "effective_validated_supply_share": (65535 / (65535 + 7282)),
                "effective_burn_share": 7282 / (65535 + 7282),
            },
        },
        "launch_submission": {
            "vector_id": vector["vector_id"],
            "policy_version": vector["policy_version"],
            "signed_vector_sha256": vector_digest,
            "signed_vector": vector,
            "broadcast_intent": {
                "extrinsic_hash": "0x" + "c" * 64,
                "nonce": 17,
                "era_reference_block": 100,
                "mortal_period_blocks": 4,
                "version_key": 10005000,
                "wire_uids": [163, 204],
                "wire_weights": [65535, 7282],
            },
            "mapping": {
                "block": 100,
                "validator_uid": 30,
                "validator_hotkey": hotkeys[30],
                "rewarded_uid": 163,
                "rewarded_hotkey": hotkeys[163],
                "burn_uid": 204,
                "burn_hotkey": hotkeys[204],
                "commit_reveal_enabled": False,
                "next_epoch_start_block": 120,
                "uid_weights": {"163": 0.9, "204": 0.1},
                "uid_safety": _fixture_uid_safety(hotkeys),
                "metagraph_snapshot": {
                    "network": "finney",
                    "netuid": 39,
                    "block": 100,
                    "block_hash": "0x" + "a" * 64,
                    "uids": list(range(205)),
                    "hotkeys": hotkeys,
                    "validator_permit": [index == 30 for index in range(len(hotkeys))],
                },
            },
            "extrinsic": {
                "hash": "0x" + "c" * 64,
                "block": 101,
                "block_hash": "0x" + "d" * 64,
                "validator_uid": 30,
                "uids": [163, 204],
                "weights_u16": [65535, 7282],
                "version_key": 10005000,
            },
            "evidence_checkpoint": {
                "source_epoch": 90,
                "manifest": manifest_digest,
                "report_id": "sha256:" + "e" * 64,
                "policy_release": 10,
                "policy_digest": "sha256:" + "f" * 64,
                "report_signing_key_id": "cathedral-score-sn39-20260724",
                "reward_mechanism": {
                    "id": "validated_supply_v1",
                    "revision": 1,
                },
                "verifier_digest": (
                    "sha256:"
                    "8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
                ),
                "verifier_binary_digest": (
                    "sha256:"
                    "35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
                ),
                "replay_result": "sha256:" + "9" * 64,
                "public_assurance": "receipts_only",
                "signed_index": {
                    "generated_at": "2026-07-24T22:00:00.000Z",
                    "latest": {
                        "manifest": manifest_digest,
                        "source_epoch": 90,
                    },
                },
                "freshness_boundary": _fixture_freshness_boundary(),
            },
        },
        "reproducer_revision": revision,
        "source_revisions": {
            "producer": EXPECTED_PRODUCER_REVISION,
            "validator": revision,
        },
        "pins": EXPECTED_RELEASE_PINS,
        "release_attestation": {"key_id": RELEASE_KEY_ID},
    }
    release_bytes = json.dumps(release, sort_keys=True, separators=(",", ":")).encode()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    signature = {
        "algorithm": "Ed25519",
        "key_id": RELEASE_KEY_ID,
        "payload": "release.json exact bytes",
        "payload_sha256": "sha256:" + hashlib.sha256(release_bytes).hexdigest(),
        "signature": base64.b64encode(private.sign(release_bytes)).decode(),
    }
    signature_bytes = json.dumps(
        signature,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    keys = {RELEASE_KEY_ID: base64.b64encode(public).decode()}
    assert (
        verify_release_bytes(
            release_bytes,
            signature_bytes,
            public_keys=keys,
            repo_revision=revision,
        )["release_attestation"]
        == "PASS"
    )
    with pytest.raises(ReproductionError, match="signed reproducer revision"):
        verify_release_bytes(
            release_bytes,
            signature_bytes,
            public_keys=keys,
            repo_revision="b" * 40,
        )
    tampered = release_bytes.replace(b"intel_tdx_cpu", b"hybrid_gpu_preview")
    with pytest.raises(ReproductionError, match="payload digest"):
        verify_release_bytes(
            tampered,
            signature_bytes,
            public_keys=keys,
            repo_revision=revision,
        )
    noncanonical = json.dumps(release, sort_keys=True, indent=2).encode()
    noncanonical_signature = dict(signature)
    noncanonical_signature["payload_sha256"] = (
        "sha256:" + hashlib.sha256(noncanonical).hexdigest()
    )
    noncanonical_signature["signature"] = base64.b64encode(
        private.sign(noncanonical)
    ).decode()
    with pytest.raises(ReproductionError, match="canonical JSON"):
        verify_release_bytes(
            noncanonical,
            json.dumps(
                noncanonical_signature,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            public_keys=keys,
            repo_revision=revision,
        )
    duplicate = b'{"schema":"cathedral_sn39_provenance_release_v2",' + release_bytes[1:]
    duplicate_signature = dict(signature)
    duplicate_signature["payload_sha256"] = (
        "sha256:" + hashlib.sha256(duplicate).hexdigest()
    )
    duplicate_signature["signature"] = base64.b64encode(
        private.sign(duplicate)
    ).decode()
    with pytest.raises(ReproductionError, match="duplicate JSON"):
        verify_release_bytes(
            duplicate,
            json.dumps(
                duplicate_signature,
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            public_keys=keys,
            repo_revision=revision,
        )


def test_launch_publishes_protocol_quantized_wire_share() -> None:
    from scripts.assert_sn39_public_reproduction import (
        WIRE_BURN_SHARE,
        WIRE_BURN_U16,
        WIRE_VALIDATED_SUPPLY_SHARE,
        WIRE_VALIDATED_SUPPLY_U16,
    )

    assert (WIRE_VALIDATED_SUPPLY_U16, WIRE_BURN_U16) == (65535, 7282)
    assert WIRE_VALIDATED_SUPPLY_SHARE + WIRE_BURN_SHARE == pytest.approx(1.0)
    assert WIRE_BURN_SHARE > 0.1
    assert WIRE_BURN_SHARE == pytest.approx(0.10000411991705234)


def _historical_launch_fixture() -> tuple[dict[str, object], list[str]]:
    hotkeys = [f"hotkey-{uid}" for uid in range(205)]
    vector = {
        "vector_id": "launch-vector-id",
        "policy_version": 77,
        "network": "finney",
        "netuid": 39,
        "key_id": "cathedral-weight-policy",
        "generated_at": "2026-07-24T22:00:00.000Z",
        "expires_at": "2026-07-24T23:00:00.000Z",
        "weights": [
            {
                "miner_hotkey": hotkeys[163],
                "weight": 1.0,
                "base_component": 0.0,
                "external_component": 1.0,
            }
        ],
        "burn_snapshot": {
            "burn_hotkey": hotkeys[204],
            "burn_uid": None,
            "forced_burn_percentage": 10.0,
        },
        "policy_metadata": {
            "confidential_primary": {
                "contract_version": "v1",
                "mode": "confidential_primary",
                "source": "cathedral_confidential_tdx",
                "base_mass": 0.0,
                "confidential_mass": 1.0,
                "complete": True,
                "fresh": True,
                "confirmed": True,
            },
            "validated_supply": {
                "contract_version": "v2",
                "intel_tdx_allocation": 0.90,
                "fixed_burn_allocation": 0.10,
                "burn_hotkey": hotkeys[204],
            },
        },
        "signature": "fixture",
    }
    vector_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(vector, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest_digest = "sha256:" + "b" * 64
    return (
        {
            "launch_submission": {
                "vector_id": vector["vector_id"],
                "policy_version": vector["policy_version"],
                "signed_vector_sha256": vector_digest,
                "signed_vector": vector,
                "broadcast_intent": {
                    "extrinsic_hash": "0x" + "c" * 64,
                    "nonce": 17,
                    "era_reference_block": 100,
                    "mortal_period_blocks": 4,
                    "version_key": 10005000,
                    "wire_uids": [163, 204],
                    "wire_weights": [65535, 7282],
                },
                "mapping": {
                    "block": 100,
                    "validator_uid": 30,
                    "validator_hotkey": hotkeys[30],
                    "rewarded_uid": 163,
                    "rewarded_hotkey": hotkeys[163],
                    "burn_uid": 204,
                    "burn_hotkey": hotkeys[204],
                    "commit_reveal_enabled": False,
                    "next_epoch_start_block": 120,
                    "uid_weights": {"163": 0.9, "204": 0.1},
                    "uid_safety": _fixture_uid_safety(hotkeys),
                    "metagraph_snapshot": {
                        "network": "finney",
                        "netuid": 39,
                        "block": 100,
                        "block_hash": "0x" + "a" * 64,
                        "uids": list(range(205)),
                        "hotkeys": hotkeys,
                        "validator_permit": [
                            index == 30 for index in range(len(hotkeys))
                        ],
                    },
                },
                "extrinsic": {
                    "hash": "0x" + "c" * 64,
                    "block": 101,
                    "block_hash": "0x" + "d" * 64,
                    "validator_uid": 30,
                    "uids": [163, 204],
                    "weights_u16": [65535, 7282],
                    "version_key": 10005000,
                },
                "evidence_checkpoint": {
                    "source_epoch": 90,
                    "manifest": manifest_digest,
                    "report_id": "sha256:" + "e" * 64,
                    "policy_release": 10,
                    "policy_digest": "sha256:" + "f" * 64,
                    "report_signing_key_id": "cathedral-score-sn39-20260724",
                    "reward_mechanism": {
                        "id": "validated_supply_v1",
                        "revision": 1,
                    },
                    "verifier_digest": (
                        "sha256:"
                        "8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
                    ),
                    "verifier_binary_digest": (
                        "sha256:"
                        "35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
                    ),
                    "replay_result": "sha256:" + "9" * 64,
                    "public_assurance": "receipts_only",
                    "signed_index": {
                        "generated_at": "2026-07-24T22:00:00.000Z",
                        "latest": {
                            "manifest": manifest_digest,
                            "source_epoch": 90,
                        },
                    },
                    "freshness_boundary": _fixture_freshness_boundary(),
                },
            }
        },
        hotkeys,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("extrinsic_hash", "0x" + "f" * 64),
        ("nonce", -1),
        ("era_reference_block", 99),
        ("mortal_period_blocks", 8),
        ("version_key", 1),
        ("wire_uids", [204, 163]),
        ("wire_weights", [65535, 1]),
    ],
)
def test_signed_launch_broadcast_intent_rejects_every_boundary_mutation(
    field: str,
    value: object,
) -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    assert isinstance(launch, dict)
    intent = launch["broadcast_intent"]
    assert isinstance(intent, dict)
    intent[field] = value
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="broadcast intent is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


def test_signed_launch_broadcast_intent_rejects_out_of_era_inclusion() -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    assert isinstance(launch, dict)
    extrinsic = launch["extrinsic"]
    assert isinstance(extrinsic, dict)
    extrinsic["block"] = 104
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="broadcast intent is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("root", "schema", "cathedral_sn39_uid_safety_v0"),
        ("rotation", "status", "NOT_PROVEN"),
        ("rotation", "mapping_block", 99),
        ("rotation", "mortal_period_blocks", 8),
        ("rotation", "era_last_block", 104),
        ("rotation", "targets", []),
    ],
)
def test_signed_launch_uid_safety_rejects_boundary_mutation(
    section: str,
    field: str,
    value: object,
) -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    assert isinstance(launch, dict)
    mapping = launch["mapping"]
    assert isinstance(mapping, dict)
    safety = mapping["uid_safety"]
    assert isinstance(safety, dict)
    target = safety if section == "root" else safety[section]
    assert isinstance(target, dict)
    target[field] = value
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="UID/hotkey safety proof is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("new_hotkey", "different-hotkey"),
        ("coldkey", "different-coldkey"),
        ("netuid", 38),
        ("keep_stake", "false"),
        ("event", "DifferentEvent"),
        ("extrinsic_index", -1),
    ],
)
def test_signed_target_rotation_receipt_rejects_boundary_mutation(
    field: str,
    value: object,
) -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    receipt = launch["mapping"]["uid_safety"]["rotation"]["targets"][0][
        "rotation_receipt"
    ]
    receipt[field] = value
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="rotation receipt is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_block", 99),
        ("report_valid_from_block", 99),
        ("manifest_generated_at", "2026-07-24T20:00:00.000Z"),
        ("report_generated_at", "2026-07-24T20:00:00.000Z"),
        ("vector_generated_at", "2026-07-24T20:00:00.000Z"),
        ("index_generated_at", "2026-07-24T20:00:00.000Z"),
    ],
)
def test_signed_post_rotation_boundary_rejects_stale_artifacts(
    field: str,
    value: object,
) -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    launch["evidence_checkpoint"]["freshness_boundary"][field] = value
    with pytest.raises(sn39_public_reproduction.ReproductionError):
        sn39_public_reproduction._validate_launch_submission(launch)


def _unlock_uid_safety_targets(
    uid_safety: dict[str, object],
    boundary: dict[str, object],
) -> None:
    """Rewrite a signed proof so no target claims a live rotation lock."""
    for target in uid_safety["rotation"]["targets"]:
        target["swap_lock"] = "never_rotated"
        target["last_hotkey_swap_block"] = 0
        target["hotkey_swap_safe_until_block"] = None
        target["hotkey_root"] = None
        target["rotation_receipt"] = None
    boundary["rotation_floor_block"] = None
    boundary["rotation_floor_timestamp"] = None


def _unlock_signed_launch_targets(launch: dict[str, object]) -> None:
    _unlock_uid_safety_targets(
        launch["mapping"]["uid_safety"],
        launch["evidence_checkpoint"]["freshness_boundary"],
    )


def test_signed_launch_accepts_targets_without_a_rotation_lock() -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    _unlock_signed_launch_targets(launch)
    # Nothing to postdate, so evidence older than the historical rotations is
    # no longer a boundary violation.
    launch["evidence_checkpoint"]["freshness_boundary"]["candidate_block"] = 1
    assert sn39_public_reproduction._validate_launch_submission(launch) is launch


def test_signed_launch_rejects_an_unlocked_target_carrying_a_receipt() -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    targets = launch["mapping"]["uid_safety"]["rotation"]["targets"]
    _unlock_signed_launch_targets(launch)
    targets[0]["rotation_receipt"] = {"block_number": 99}
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="rotation receipt is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


@pytest.mark.parametrize("swap_lock", ["", "ACTIVE", None, True])
def test_signed_launch_rejects_an_unknown_rotation_lock_state(
    swap_lock: object,
) -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    launch["mapping"]["uid_safety"]["rotation"]["targets"][0]["swap_lock"] = swap_lock
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="rotation lock state is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


def test_signed_launch_requires_the_recorded_stability_basis() -> None:
    release, _hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    launch["mapping"]["uid_safety"]["stability_basis"] = "unreviewed"
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="UID/hotkey safety proof is malformed",
    ):
        sn39_public_reproduction._validate_launch_submission(launch)


class _HistoricalSubstrate:
    def __init__(self, owner: _HistoricalSubtensor) -> None:
        self.owner = owner

    def get_block(self, *, block_hash: str) -> dict[str, object]:
        if block_hash == "0x" + "9" * 64:
            rotations = []
            for uid, coldkey, extrinsic_hash in (
                (163, "coldkey-163", "0x" + "1" * 64),
                (204, "subnet-owner-coldkey", "0x" + "2" * 64),
            ):
                rotations.append(
                    SimpleNamespace(
                        value={
                            "extrinsic_hash": extrinsic_hash,
                            "address": coldkey,
                            "call": {
                                "call_module": "SubtensorModule",
                                "call_function": "swap_hotkey_v2",
                                "call_args": [
                                    {
                                        "name": "hotkey",
                                        "value": f"old-{self.owner.hotkeys[uid]}",
                                    },
                                    {
                                        "name": "new_hotkey",
                                        "value": (
                                            "different-hotkey"
                                            if self.owner.mutation == "rotation_call"
                                            and uid == 163
                                            else self.owner.hotkeys[uid]
                                        ),
                                    },
                                    {"name": "netuid", "value": 39},
                                    {"name": "keep_stake", "value": False},
                                ],
                            },
                        }
                    )
                )
            return {
                "header": {"number": 99, "hash": block_hash},
                "extrinsics": rotations,
            }
        assert block_hash == "0x" + "d" * 64
        call_weights = (
            [65535, 1] if self.owner.mutation == "extrinsic_args" else [65535, 7282]
        )
        extrinsic_hash = (
            "0x" + "e" * 64
            if self.owner.mutation == "absent_extrinsic"
            else "0x" + "c" * 64
        )
        observed_extrinsic = {
            "extrinsic_hash": extrinsic_hash,
            "address": self.owner.hotkeys[30],
            "nonce": 18 if self.owner.mutation == "extrinsic_nonce" else 17,
            "era": {
                "period": 8 if self.owner.mutation == "extrinsic_era" else 4,
                "phase": 0,
            },
            "call": {
                "call_module": "SubtensorModule",
                "call_function": "set_mechanism_weights",
                "call_args": [
                    {"name": "netuid", "value": 39},
                    {"name": "mecid", "value": 0},
                    {"name": "version_key", "value": 10005000},
                    {"name": "dests", "value": [163, 204]},
                    {"name": "weights", "value": call_weights},
                ],
            },
        }
        if self.owner.mutation == "missing_nonce":
            observed_extrinsic.pop("nonce")
        if self.owner.mutation == "missing_era":
            observed_extrinsic.pop("era")
        return {
            "header": {
                "number": 101,
                "hash": (
                    "0x" + "e" * 64
                    if self.owner.mutation == "inclusion_block"
                    else block_hash
                ),
            },
            "extrinsics": [SimpleNamespace(value=observed_extrinsic)],
        }

    def retrieve_extrinsic_by_hash(
        self,
        block_hash: str,
        extrinsic_hash: str,
    ) -> SimpleNamespace:
        if block_hash == "0x" + "9" * 64:
            rotation = {
                "0x" + "1" * 64: (
                    0,
                    163,
                    "coldkey-163",
                ),
                "0x" + "2" * 64: (
                    1,
                    204,
                    "subnet-owner-coldkey",
                ),
            }[extrinsic_hash]
            index, uid, coldkey = rotation
            return SimpleNamespace(
                extrinsic_idx=index,
                is_success=not (
                    self.owner.mutation == "rotation_failure" and uid == 163
                ),
                error_message=(
                    "simulated rotation failure"
                    if self.owner.mutation == "rotation_failure" and uid == 163
                    else None
                ),
                triggered_events=[
                    {
                        "event": {
                            "module_id": "SubtensorModule",
                            "event_id": "HotkeySwappedOnSubnet",
                            "attributes": {
                                "coldkey": coldkey,
                                "old_hotkey": f"old-{self.owner.hotkeys[uid]}",
                                "new_hotkey": self.owner.hotkeys[uid],
                                **(
                                    {"new_hotkey": "different-hotkey"}
                                    if self.owner.mutation == "rotation_event"
                                    and uid == 163
                                    else {}
                                ),
                                "netuid": 39,
                            },
                        }
                    }
                ],
            )
        assert block_hash == "0x" + "d" * 64
        assert extrinsic_hash == "0x" + "c" * 64
        return SimpleNamespace(
            extrinsic_idx=0,
            is_success=self.owner.mutation != "extrinsic_failure",
            error_message=(
                "simulated dispatch failure"
                if self.owner.mutation == "extrinsic_failure"
                else None
            ),
        )

    def query(
        self,
        *,
        module: str,
        storage_function: str,
        block_hash: str,
    ) -> SimpleNamespace:
        assert (module, storage_function) == ("Timestamp", "Now")
        if block_hash == "0x" + "9" * 64:
            timestamp = datetime(2026, 7, 24, 21, 0, tzinfo=UTC)
            return SimpleNamespace(value=int(timestamp.timestamp() * 1000))
        assert block_hash == "0x" + "d" * 64
        timestamp = datetime(2026, 7, 24, 22, 30, tzinfo=UTC)
        if self.owner.mutation == "inclusion_timestamp":
            timestamp = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
        return SimpleNamespace(value=int(timestamp.timestamp() * 1000))

    def get_chain_finalised_head(self) -> str:
        return "0x" + "f" * 64

    def get_block_number(self, block_hash: str) -> int:
        if block_hash == "0x" + "9" * 64:
            return 99
        assert block_hash == "0x" + "f" * 64
        return 200

    def get_block_hash(self, block_number: int) -> str:
        if block_number == 0:
            if self.owner.mutation == "genesis":
                return "0x" + "0" * 64
            return sn39_public_reproduction.FINNEY_GENESIS_HASH
        if block_number == 99:
            return "0x" + "9" * 64
        if block_number == 100:
            return "0x" + "a" * 64
        assert block_number == 200
        return "0x" + "f" * 64

    def get_constant(
        self,
        *,
        module_name: str,
        constant_name: str,
        block_hash: str,
    ) -> int:
        assert module_name == "SubtensorModule"
        assert block_hash == "0x" + "a" * 64
        assert constant_name == "HotkeySwapOnSubnetInterval"
        return 7200


class _HistoricalSubtensor:
    def __init__(self, hotkeys: list[str], *, mutation: str = "") -> None:
        self.hotkeys = hotkeys
        self.mutation = mutation
        self.substrate = _HistoricalSubstrate(self)

    def get_block_hash(self, block: int) -> str:
        if block == 100:
            return (
                "0x" + "f" * 64 if self.mutation == "mapping_block" else "0x" + "a" * 64
            )
        if block == 101:
            return "0x" + "d" * 64
        raise AssertionError(f"unexpected block {block}")

    def metagraph(self, netuid: int, *, block: int) -> SimpleNamespace:
        assert netuid == 39
        assert block in (100, 101)
        hotkeys = list(self.hotkeys)
        if self.mutation == "metagraph" and block == 100:
            hotkeys[163] = "different-hotkey"
        if self.mutation == "inclusion_metagraph" and block == 101:
            hotkeys[163] = "different-hotkey"
        validator_permit = [index == 30 for index in range(len(hotkeys))]
        if self.mutation == "validator_permit" and block == 100:
            validator_permit[30] = False
        if self.mutation == "inclusion_validator_permit" and block == 101:
            validator_permit[30] = False
        return SimpleNamespace(
            block=block,
            uids=list(range(len(hotkeys))),
            hotkeys=hotkeys,
            validator_permit=validator_permit,
            max_uids=256,
            hparams=SimpleNamespace(
                max_regs_per_block=1,
                immunity_period=15000,
            ),
            block_at_registration=[0 for _hotkey in hotkeys],
        )

    def weights(self, netuid: int, *, block: int) -> list[tuple[int, object]]:
        assert netuid == 39
        assert block == 101
        weights = (
            [(163, 65535), (204, 1)]
            if self.mutation == "chain_weights"
            else [(163, 65535), (204, 7282)]
        )
        return [(30, weights)]

    def commit_reveal_enabled(self, *, netuid: int, block: int) -> bool:
        assert netuid == 39
        if block == 100:
            return self.mutation == "commit_reveal"
        assert block == 101
        return self.mutation == "inclusion_commit_reveal"

    def get_subnet_owner_hotkey(self, netuid: int, *, block: int) -> str:
        assert netuid == 39
        assert block in (100, 101)
        return self.hotkeys[204]

    def get_next_epoch_start_block(self, netuid: int, *, block: int) -> int:
        assert netuid == 39
        assert block in (100, 101)
        return 121 if self.mutation == "epoch_schedule" else 120

    def query_subtensor(
        self,
        *,
        name: str,
        params: list[object],
        block: int,
    ) -> object:
        assert block == 100
        if name == "MinNonImmuneUids":
            return 10
        if name == "SubnetOwner":
            return "subnet-owner-coldkey"
        if name == "OwnedHotkeys":
            assert params == ["subnet-owner-coldkey"]
            return [self.hotkeys[204]]
        if name == "ImmuneOwnerUidsLimit":
            return 1
        if name == "ColdkeySwapAnnouncementDelay":
            assert params == []
            return 36000
        if name == "Owner":
            hotkey = str(params[0])
            return (
                "subnet-owner-coldkey"
                if hotkey == self.hotkeys[204]
                else f"coldkey-{self.hotkeys.index(hotkey)}"
            )
        if name == "LastHotkeySwapOnNetuid":
            return 0 if self.mutation == "never_rotated" else 99
        if name == "ColdkeySwapAnnouncements":
            return (
                {"execution_block": 101}
                if self.mutation == "pending_coldkey_swap"
                else None
            )
        if name in {"HotkeySuccessor", "HotkeyRoot"}:
            assert len(params) == 2 and params[0] == 39
            hotkey = str(params[1])
            for uid in (163, 204):
                current = self.hotkeys[uid]
                old = f"old-{current}"
                if name == "HotkeySuccessor":
                    if hotkey == old:
                        return (
                            "different-hotkey"
                            if self.mutation == "rotation_lineage" and uid == 163
                            else current
                        )
                    if hotkey == current:
                        return None
                if name == "HotkeyRoot":
                    if hotkey == current:
                        return old
                    if hotkey == old:
                        return None
            raise AssertionError(f"unexpected lineage hotkey {hotkey}")
        raise AssertionError(f"unexpected storage query {name}")


def _finalizer_state() -> tuple[dict[str, object], list[str]]:
    from scripts.assert_sn39_public_reproduction import EXPECTED_PRODUCER_REVISION

    fixture, hotkeys = _historical_launch_fixture()
    launch = fixture["launch_submission"]
    vector = launch["signed_vector"]
    attempt = "sha256:" + "8" * 64
    manifest = launch["evidence_checkpoint"]["manifest"]
    state = {
        "submission_pending_id": None,
        "submission_launch_status": "finalized",
        "submission_launch_budget_limit": 1,
        "submission_launch_attempt_id": attempt,
        "submission_launch_attempt_ids": [attempt],
        "submission_continuous_enabled": False,
        "submission_launch_extrinsic_hash": launch["extrinsic"]["hash"],
        "submission_launch_block_hash": launch["extrinsic"]["block_hash"],
        "submission_launch_block_number": launch["extrinsic"]["block"],
        "submission_launch_version_key": launch["extrinsic"]["version_key"],
        "submission_launch_broadcast_intent": launch["broadcast_intent"],
        "submission_launch_uid_safety": launch["mapping"]["uid_safety"],
        "submission_launch_identity": {
            "network": "finney",
            "netuid": 39,
            "mapping_block": launch["mapping"]["block"],
            "next_epoch_start_block": launch["mapping"]["next_epoch_start_block"],
            "validator_hotkey": hotkeys[30],
            "validator_uid": 30,
            "vector_id": vector["vector_id"],
            "policy_version": vector["policy_version"],
            "signed_vector_sha256": launch["signed_vector_sha256"],
            "signed_vector": vector,
            "burn_hotkey": hotkeys[204],
            "uid_weights": [[163, 0.9], [204, 0.1]],
            "uid_hotkeys": [[163, hotkeys[163]], [204, hotkeys[204]]],
            "uid_safety": launch["mapping"]["uid_safety"],
            "full_provenance": {
                "source_epoch": 90,
                "report_id": "sha256:" + "e" * 64,
                "manifest": manifest,
                "policy_release": 10,
                "policy_digest": "sha256:" + "f" * 64,
                "mechanism": "validated_supply_v1",
                "scope": "rewarded_set_full",
                "whole_epoch_assurance": "receipts_only",
                "vector_agrees": True,
                "rewarded_hotkeys": [hotkeys[163]],
                "raw_replayed_hotkeys": [hotkeys[163]],
                "verifier_digest": (
                    "sha256:"
                    "8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
                ),
                "verifier_binary_digest": (
                    "sha256:"
                    "35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
                ),
                "report_signing_key_id": "cathedral-score-sn39-20260724",
                "signed_index": {
                    "schema": "cathedral_evidence_index_v1",
                    "network": "finney",
                    "netuid": 39,
                    "generated_at": "2026-07-24T22:00:00.000Z",
                    "latest": {"source_epoch": 90, "manifest": manifest},
                    "recent": [{"source_epoch": 90, "manifest": manifest}],
                    "signing_key_id": "cathedral-evidence-index-sn39-20260724",
                    "signature": {
                        "algorithm": "ed25519",
                        "value_base64": "fixture",
                    },
                },
                "source_revision": EXPECTED_PRODUCER_REVISION,
                "freshness_boundary": _fixture_freshness_boundary(),
            },
        },
    }
    return state, hotkeys


def _stub_root_finalizer_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scripts.finalize_sn39_public_release._replay_frozen_controlled_positive",
        lambda **_kwargs: {
            "schema": "cathedral_sn39_tdx_replay_result_v2",
            "status": "PASS",
            "assurance": "root_finalizer_positive_raw_replay",
            "test_fixture": True,
        },
    )


def test_release_finalizer_builds_the_exact_archive_verified_seal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    release, replay_bytes = build_release(
        state,
        release_sha="a" * 40,
        subtensor=_HistoricalSubtensor(hotkeys),
    )
    launch = release["launch_submission"]
    assert launch["mapping"]["metagraph_snapshot"]["hotkeys"] == hotkeys
    assert launch["mapping"]["metagraph_snapshot"]["validator_permit"][30] is True
    assert launch["extrinsic"]["weights_u16"] == [65535, 7282]
    assert launch["evidence_checkpoint"]["replay_result"] == (
        "sha256:" + hashlib.sha256(replay_bytes).hexdigest()
    )
    assert release["source_revisions"]["validator"] == "a" * 40


def test_release_finalizer_seals_targets_that_never_rotated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    identity = state["submission_launch_identity"]
    _unlock_uid_safety_targets(
        identity["uid_safety"],
        identity["full_provenance"]["freshness_boundary"],
    )
    release, _replay_bytes = build_release(
        state,
        release_sha="a" * 40,
        subtensor=_HistoricalSubtensor(hotkeys, mutation="never_rotated"),
    )
    targets = release["launch_submission"]["mapping"]["uid_safety"]["rotation"][
        "targets"
    ]
    assert [row["swap_lock"] for row in targets] == ["never_rotated"] * 2
    assert [row["rotation_receipt"] for row in targets] == [None, None]
    assert [row["hotkey_swap_safe_until_block"] for row in targets] == [None, None]


def test_release_finalizer_rejects_a_journal_lock_the_archive_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import ReleaseError, build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    with pytest.raises(ReleaseError, match="archive UID-safety proof differs"):
        build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(hotkeys, mutation="never_rotated"),
        )


def test_release_finalizer_rejects_inclusion_uid_reassignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="inclusion UID mapping",
    ):
        build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(hotkeys, mutation="inclusion_metagraph"),
        )


def test_release_finalizer_rejects_non_finney_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import ReleaseError, build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    with pytest.raises(ReleaseError, match="pinned Finney genesis"):
        build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(hotkeys, mutation="genesis"),
        )


def test_release_finalizer_rejects_validator_without_mapping_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import ReleaseError, build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    with pytest.raises(
        ReleaseError,
        match="archive mapping",
    ):
        build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(hotkeys, mutation="validator_permit"),
        )


def test_release_finalizer_rejects_validator_without_inclusion_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.finalize_sn39_public_release import build_release

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    with pytest.raises(
        sn39_public_reproduction.ReproductionError,
        match="launch inclusion UID mapping",
    ):
        build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(
                hotkeys,
                mutation="inclusion_validator_permit",
            ),
        )


def test_release_finalizer_signature_matches_the_committed_public_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from scripts.finalize_sn39_public_release import (
        RELEASE_KEY_ID,
        build_release,
        build_signature,
        canonical_json,
    )

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    _stub_root_finalizer_replay(monkeypatch)
    state, hotkeys = _finalizer_state()
    release, _replay = build_release(
        state,
        release_sha="a" * 40,
        subtensor=_HistoricalSubtensor(hotkeys),
    )
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key_path = tmp_path / "config/provenance/release-attestation-keys.json"
    key_path.parent.mkdir(parents=True)
    key_path.write_text(
        json.dumps(
            {RELEASE_KEY_ID: base64.b64encode(public).decode("ascii")},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    signature = build_signature(
        canonical_json(release),
        seed=private.private_bytes_raw(),
        release_sha="a" * 40,
        release_root=tmp_path,
    )
    envelope = json.loads(signature)
    assert envelope["key_id"] == RELEASE_KEY_ID
    assert envelope["payload_sha256"] == (
        "sha256:" + hashlib.sha256(canonical_json(release)).hexdigest()
    )


def test_release_finalizer_reads_only_service_owned_private_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    monkeypatch.setattr(finalizer, "RUNTIME_ROOT", runtime)
    journal = runtime / ("journal-" + "1" * 64 + ".json")
    journal.write_text('{"status":"finalized"}')
    journal.chmod(0o600)
    assert finalizer.read_launch_journal(journal) == {"status": "finalized"}

    alias = runtime / "journal-alias.json"
    os.link(journal, alias)
    with pytest.raises(finalizer.ReleaseError, match="service-owned private"):
        finalizer.read_launch_journal(journal)
    alias.unlink()

    journal.chmod(0o644)
    with pytest.raises(finalizer.ReleaseError, match="service-owned private"):
        finalizer.read_launch_journal(journal)
    journal.unlink()
    target = runtime / "target.json"
    target.write_text('{"status":"finalized"}')
    target.chmod(0o600)
    journal.symlink_to(target)
    with pytest.raises(finalizer.ReleaseError, match="cannot open"):
        finalizer.read_launch_journal(journal)


def test_release_finalizer_requires_exact_launcher_manifest_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    manifest = tmp_path / "sn39-release-manifest.json"
    manifest.write_text('{"schema":"fixture"}')
    manifest.chmod(0o444)
    journal = finalizer.RUNTIME_ROOT / ("journal-" + "1" * 64 + ".json")
    release_sha = "a" * 40
    monkeypatch.setattr(finalizer, "MANIFEST", manifest)
    monkeypatch.setattr(finalizer, "ROOT_UID", os.getuid())
    manifest_digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    context = finalizer._launcher_context_digest(
        release_sha=release_sha,
        journal=journal,
        manifest_digest=manifest_digest,
    )
    monkeypatch.setenv(finalizer.FINALIZER_CONTEXT_ENV, context)
    finalizer._require_launcher_context(
        release_sha=release_sha,
        journal=journal,
    )
    assert finalizer.FINALIZER_CONTEXT_ENV not in os.environ

    alias = tmp_path / "manifest-alias.json"
    os.link(manifest, alias)
    monkeypatch.setenv(finalizer.FINALIZER_CONTEXT_ENV, context)
    with pytest.raises(finalizer.ReleaseError, match="immutable root-owned"):
        finalizer._require_launcher_context(
            release_sha=release_sha,
            journal=journal,
        )
    alias.unlink()

    monkeypatch.setenv(finalizer.FINALIZER_CONTEXT_ENV, context)
    manifest.chmod(0o644)
    manifest.write_text('{"schema":"tampered"}')
    manifest.chmod(0o444)
    with pytest.raises(finalizer.ReleaseError, match="immutable-install launcher"):
        finalizer._require_launcher_context(
            release_sha=release_sha,
            journal=journal,
        )


def _finalizer_public_tree(tmp_path: Path) -> Path:
    root = tmp_path / "public"
    (root / "blobs" / "sha256").mkdir(parents=True)
    for directory in (root, root / "blobs", root / "blobs" / "sha256"):
        directory.chmod(0o755)
    return root


def test_release_finalizer_blobs_are_bounded_owner_controlled_and_immutable(
    tmp_path: Path,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    payload = b"operator-attested replay"
    digest = finalizer.put_blob(root, payload)
    blob = root / "blobs" / "sha256" / digest.split(":", 1)[1]
    assert blob.read_bytes() == payload
    assert stat.S_IMODE(blob.stat().st_mode) == 0o644
    assert finalizer.put_blob(root, payload) == digest

    blob.write_bytes(b"tampered")
    with pytest.raises(finalizer.ReleaseError, match="collides"):
        finalizer.put_blob(root, payload)
    with pytest.raises(finalizer.ReleaseError, match="size cap"):
        finalizer.put_blob(root, b"x" * (finalizer.MAX_PUBLIC_BLOB_BYTES + 1))


def test_release_finalizer_rejects_public_hardlink_aliases(tmp_path: Path) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    payload = b"root-replayed-evidence"
    digest = finalizer.put_blob(root, payload)
    blob = root / "blobs" / "sha256" / digest.split(":", 1)[1]
    alias = root / "blobs" / "sha256" / "external-alias"
    os.link(blob, alias)
    with pytest.raises(finalizer.ReleaseError, match="hardlink alias"):
        finalizer.put_blob(root, payload)
    alias.unlink()


def test_release_finalizer_recovers_blob_crash_after_durable_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    payload = b"crash-recoverable-replay"
    real_unlink = finalizer.os.unlink
    crashed = False

    def crash_once(path):
        nonlocal crashed
        if not crashed and str(path).endswith(".pending"):
            crashed = True
            raise RuntimeError("simulated kill after durable link")
        return real_unlink(path)

    monkeypatch.setattr(finalizer.os, "unlink", crash_once)
    with pytest.raises(RuntimeError, match="simulated kill"):
        finalizer.put_blob(root, payload)
    monkeypatch.setattr(finalizer.os, "unlink", real_unlink)
    digest = finalizer.put_blob(root, payload)
    blob = root / "blobs" / "sha256" / digest.split(":", 1)[1]
    assert blob.read_bytes() == payload
    assert blob.stat().st_nlink == 1
    assert not (blob.parent / f".{blob.name}.pending").exists()
    publication_lock = blob.parent / ".sn39-publication.lock"
    assert stat.S_IMODE(publication_lock.stat().st_mode) == 0o600
    assert publication_lock.stat().st_nlink == 1


def test_release_finalizer_public_seal_is_publish_once(tmp_path: Path) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    release = root / "release.json"
    finalizer.atomic_write(release, b'{"release":1}')
    finalizer.atomic_write(release, b'{"release":1}')
    assert release.read_bytes() == b'{"release":1}'
    with pytest.raises(finalizer.ReleaseError, match="already sealed differently"):
        finalizer.atomic_write(release, b'{"release":2}')
    assert release.read_bytes() == b'{"release":1}'


def test_release_finalizer_recovers_partial_and_linked_public_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    release = root / "release.json"
    pending = root / ".release.json.pending"
    pending.write_bytes(b'{"release":')
    pending.chmod(0o644)
    real_link = finalizer.os.link
    crashed = False

    def crash_once(source, target, *, follow_symlinks=True):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("simulated kill before durable link")
        return real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(finalizer.os, "link", crash_once)
    with pytest.raises(RuntimeError, match="simulated kill"):
        finalizer.atomic_write(release, b'{"release":1}')
    assert not release.exists()
    assert pending.read_bytes() == b'{"release":1}'
    monkeypatch.setattr(finalizer.os, "link", real_link)
    finalizer.atomic_write(release, b'{"release":1}')
    assert release.read_bytes() == b'{"release":1}'
    assert release.stat().st_nlink == 1
    assert not pending.exists()


def test_release_finalizer_serializes_overlapping_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from scripts import finalize_sn39_public_release as finalizer

    root = _finalizer_public_tree(tmp_path)
    release = root / "release.json"
    first_at_link = threading.Event()
    allow_first_link = threading.Event()
    second_started = threading.Event()
    second_done = threading.Event()
    outcomes: dict[str, object] = {}
    real_link = finalizer.os.link

    def controlled_link(source, target, *, follow_symlinks=True):
        if threading.current_thread().name == "first-finalizer":
            first_at_link.set()
            assert allow_first_link.wait(2)
        return real_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(finalizer.os, "link", controlled_link)

    def publish_first() -> None:
        try:
            finalizer.atomic_write(release, b'{"release":1}')
            outcomes["first"] = "PASS"
        except BaseException as exc:  # noqa: BLE001 - transfer from worker
            outcomes["first"] = exc

    def publish_second() -> None:
        second_started.set()
        try:
            finalizer.atomic_write(release, b'{"release":2}')
            outcomes["second"] = "PASS"
        except BaseException as exc:  # noqa: BLE001 - transfer from worker
            outcomes["second"] = exc
        finally:
            second_done.set()

    first = threading.Thread(target=publish_first, name="first-finalizer")
    second = threading.Thread(target=publish_second, name="second-finalizer")
    first.start()
    assert first_at_link.wait(2)
    second.start()
    assert second_started.wait(2)
    assert not second_done.wait(0.2)
    allow_first_link.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes["first"] == "PASS"
    assert isinstance(outcomes["second"], finalizer.ReleaseError)
    assert "already sealed differently" in str(outcomes["second"])
    assert release.read_bytes() == b'{"release":1}'
    assert release.stat().st_nlink == 1
    publication_lock = root / ".sn39-publication.lock"
    assert stat.S_IMODE(publication_lock.stat().st_mode) == 0o600
    assert publication_lock.stat().st_nlink == 1


def test_release_finalizer_reads_exact_private_controlled_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    monkeypatch.setattr(finalizer, "ROOT_UID", os.getuid())
    controlled = tmp_path / "controlled"
    controlled.mkdir(mode=0o750)
    controlled.chmod(0o750)
    envelope = b'{"private":"raw-tdx-envelope"}'
    digest = "sha256:" + hashlib.sha256(envelope).hexdigest()
    envelope_path = controlled / f"{digest.split(':', 1)[1]}.json"
    envelope_path.write_bytes(envelope)
    envelope_path.chmod(0o640)
    assert finalizer._read_controlled_envelope(controlled, digest) == envelope

    envelope_alias = controlled / "envelope-alias.json"
    os.link(envelope_path, envelope_alias)
    with pytest.raises(finalizer.ReleaseError, match="single-link"):
        finalizer._read_controlled_envelope(controlled, digest)
    envelope_alias.unlink()

    verifier = tmp_path / "verifier"
    verifier.write_bytes(b"reviewed-verifier-bytes")
    verifier.chmod(0o755)
    assert finalizer._read_verifier_binary(verifier) == b"reviewed-verifier-bytes"
    verifier_alias = tmp_path / "verifier-alias"
    os.link(verifier, verifier_alias)
    with pytest.raises(finalizer.ReleaseError, match="single-link"):
        finalizer._read_verifier_binary(verifier)


def test_release_finalizer_never_synthesizes_replay_from_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import finalize_sn39_public_release as finalizer

    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    state, hotkeys = _finalizer_state()
    calls = 0

    def require_real_inputs(**kwargs):
        nonlocal calls
        calls += 1
        assert (
            kwargs["signed_vector"]
            == state["submission_launch_identity"]["signed_vector"]
        )
        raise finalizer.ReleaseError("controlled replay envelope is unavailable")

    monkeypatch.setattr(
        finalizer,
        "_replay_frozen_controlled_positive",
        require_real_inputs,
    )
    with pytest.raises(finalizer.ReleaseError, match="controlled replay envelope"):
        finalizer.build_release(
            state,
            release_sha="a" * 40,
            subtensor=_HistoricalSubtensor(hotkeys),
        )
    assert calls == 1


def _candidate_manifest(hotkeys: list[str]) -> dict[str, object]:
    return {
        "candidate_set": {
            "network": "finney",
            "netuid": 39,
            "source": "sn39_metagraph",
            "block": 100,
            "block_hash": "0x" + "a" * 64,
            "candidates": [
                {
                    "hotkey": hotkey,
                    "outcome": "rejected",
                    "reason": "no_verified_work",
                }
                for hotkey in hotkeys
            ],
        }
    }


def test_historical_candidates_equal_the_archive_metagraph() -> None:
    from scripts.assert_sn39_public_reproduction import verify_historical_candidates

    hotkeys = [f"hotkey-{uid}" for uid in range(205)]
    verify_historical_candidates(
        _candidate_manifest(hotkeys),
        subtensor=_HistoricalSubtensor(hotkeys),
    )


@pytest.mark.parametrize(
    "mutation",
    ["block_hash", "metagraph", "missing", "extra", "duplicate"],
)
def test_historical_candidates_reject_snapshot_tampering(mutation: str) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        verify_historical_candidates,
    )

    hotkeys = [f"hotkey-{uid}" for uid in range(205)]
    manifest = _candidate_manifest(hotkeys)
    snapshot = manifest["candidate_set"]
    assert isinstance(snapshot, dict)
    candidates = snapshot["candidates"]
    assert isinstance(candidates, list)
    subtensor_mutation = ""
    if mutation == "block_hash":
        snapshot["block_hash"] = "0x" + "f" * 64
    elif mutation == "metagraph":
        subtensor_mutation = "metagraph"
    elif mutation == "missing":
        candidates.pop()
    elif mutation == "extra":
        candidates.append(
            {
                "hotkey": "not-in-metagraph",
                "outcome": "rejected",
                "reason": "no_verified_work",
            }
        )
    elif mutation == "duplicate":
        candidates[-1] = dict(candidates[0])
    with pytest.raises(ReproductionError, match="candidate"):
        verify_historical_candidates(
            manifest,
            subtensor=_HistoricalSubtensor(
                hotkeys,
                mutation=subtensor_mutation,
            ),
        )


def _frozen_cross_binding_fixture() -> tuple[dict[str, object], dict[str, object]]:
    checkpoint = {
        "source_epoch": 90,
        "report_id": "sha256:" + "a" * 64,
        "policy_release": 10,
        "policy_digest": "sha256:" + "b" * 64,
        "report_signing_key_id": "cathedral-score-sn39-20260724",
        "reward_mechanism": {"id": "validated_supply_v1", "revision": 1},
        "verifier_digest": (
            "sha256:8292b085e4dbe228f8ffd2ec7046a1c0f1324ff5e7a29d1574ce16963f9b098f"
        ),
        "verifier_binary_digest": (
            "sha256:35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
        ),
        "freshness_boundary": _fixture_freshness_boundary(),
    }
    manifest = {
        "network": "finney",
        "netuid": 39,
        "source_epoch": checkpoint["source_epoch"],
        "generated_at": "2026-07-24T21:30:00.000Z",
        "candidate_set": {
            "block": 100,
            "block_hash": "0x" + "a" * 64,
        },
        "source_revision": "655c264421a1f5f2e625a372a40f595aa1e114ab",
        "reward_mechanism": checkpoint["reward_mechanism"],
        "policy_registry": {
            "release": checkpoint["policy_release"],
            "digest": checkpoint["policy_digest"],
            "blob": checkpoint["policy_digest"],
        },
        "score_report": {
            "report_id": checkpoint["report_id"],
            "signing_key_id": checkpoint["report_signing_key_id"],
        },
        "verifier": {
            "digest": checkpoint["verifier_digest"],
            "binary_blob": checkpoint["verifier_binary_digest"],
        },
        "attestations": [
            {
                "hotkey": "tdx-miner",
                "envelope_digest": "sha256:" + "1" * 64,
                "evidence_digest": "sha256:" + "2" * 64,
                "challenge_digest": "sha256:" + "3" * 64,
            }
        ],
        "receipts": [
            {
                "hotkey": "tdx-miner",
                "receipt_id": "receipt-tdx-miner",
                "blob": "sha256:" + "4" * 64,
                "work_item_blob": "sha256:" + "5" * 64,
                "result_blob": "sha256:" + "6" * 64,
            }
        ],
    }
    return checkpoint, manifest


def test_controlled_replay_result_is_exactly_bound_to_checkpoint() -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        _validate_controlled_replay_result,
    )

    checkpoint, manifest = _frozen_cross_binding_fixture()
    checkpoint["manifest"] = "sha256:" + "e" * 64
    launch = {
        "signed_vector": {
            "weights": [
                {"miner_hotkey": "tdx-miner", "weight": 1.0},
                {"miner_hotkey": "zero-miner", "weight": 0.0},
            ]
        }
    }
    result = {
        "schema": "cathedral_sn39_tdx_replay_result_v2",
        "status": "PASS",
        "assurance": "root_finalizer_positive_raw_replay",
        "source_epoch": checkpoint["source_epoch"],
        "manifest": checkpoint["manifest"],
        "report_id": checkpoint["report_id"],
        "policy_release": checkpoint["policy_release"],
        "policy_digest": checkpoint["policy_digest"],
        "reward_mechanism": checkpoint["reward_mechanism"],
        "verifier_digest": checkpoint["verifier_digest"],
        "verifier_binary_digest": checkpoint["verifier_binary_digest"],
        "replayed_hotkeys": ["tdx-miner"],
        "replay_inputs": [
            {
                "hotkey": "tdx-miner",
                "receipt_id": "receipt-tdx-miner",
                "receipt_blob": "sha256:" + "4" * 64,
                "work_item_blob": "sha256:" + "5" * 64,
                "result_blob": "sha256:" + "6" * 64,
                "envelope_digest": "sha256:" + "1" * 64,
                "evidence_digest": "sha256:" + "2" * 64,
                "challenge_digest": "sha256:" + "3" * 64,
            }
        ],
    }
    _validate_controlled_replay_result(result, checkpoint, launch, manifest)
    result["replayed_hotkeys"] = ["different"]
    with pytest.raises(ReproductionError, match="controlled TDX replay"):
        _validate_controlled_replay_result(result, checkpoint, launch, manifest)


@pytest.mark.parametrize(
    "mutation",
    [
        "network",
        "netuid",
        "source_epoch",
        "source_revision",
        "reward_mechanism",
        "policy_release",
        "policy_digest",
        "policy_blob",
        "report_id",
        "report_signing_key_id",
        "verifier_digest",
        "verifier_binary_digest",
    ],
)
def test_frozen_manifest_rejects_every_cross_binding_tamper(mutation: str) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        _validate_frozen_manifest,
    )

    checkpoint, manifest = _frozen_cross_binding_fixture()
    _validate_frozen_manifest(manifest, checkpoint)
    if mutation in {"network", "source_revision"}:
        manifest[mutation] = "different"
    elif mutation in {"netuid", "source_epoch"}:
        manifest[mutation] = 999
    elif mutation == "reward_mechanism":
        manifest[mutation] = {"id": "different", "revision": 1}
    elif mutation == "policy_release":
        manifest["policy_registry"]["release"] = 999
    elif mutation == "policy_digest":
        manifest["policy_registry"]["digest"] = "sha256:" + "c" * 64
    elif mutation == "policy_blob":
        manifest["policy_registry"]["blob"] = "sha256:" + "c" * 64
    elif mutation == "report_id":
        manifest["score_report"]["report_id"] = "different"
    elif mutation == "report_signing_key_id":
        manifest["score_report"]["signing_key_id"] = "different"
    elif mutation in {"verifier_digest", "verifier_binary_digest"}:
        key = "digest" if mutation == "verifier_digest" else "binary_blob"
        manifest["verifier"][key] = "sha256:" + "c" * 64
    with pytest.raises(ReproductionError, match="signed checkpoint"):
        _validate_frozen_manifest(manifest, checkpoint)


@pytest.mark.parametrize(
    "mutation",
    [
        "source_epoch",
        "report_id",
        "signing_key_id",
        "policy_release",
        "policy_digest",
        "verifier_digest",
        "mechanism_id",
        "mechanism_revision",
        "assurance_level",
    ],
)
def test_frozen_result_rejects_every_cross_binding_tamper(mutation: str) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        _validate_frozen_result,
    )

    checkpoint, _manifest = _frozen_cross_binding_fixture()
    values = {
        "source_epoch": checkpoint["source_epoch"],
        "report_id": checkpoint["report_id"],
        "signing_key_id": checkpoint["report_signing_key_id"],
        "policy_release": checkpoint["policy_release"],
        "policy_digest": checkpoint["policy_digest"],
        "verifier_digest": checkpoint["verifier_digest"],
        "mechanism_id": checkpoint["reward_mechanism"]["id"],
        "mechanism_revision": checkpoint["reward_mechanism"]["revision"],
        "assurance_level": "receipts_only",
    }
    _validate_frozen_result(SimpleNamespace(**values), checkpoint)
    values[mutation] = 999 if isinstance(values[mutation], int) else "different"
    with pytest.raises(ReproductionError, match="signed checkpoint"):
        _validate_frozen_result(SimpleNamespace(**values), checkpoint)


def test_historical_launch_verifies_exact_archive_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.assert_sn39_public_reproduction import verify_historical_launch

    release, hotkeys = _historical_launch_fixture()
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    result = verify_historical_launch(
        release,
        subtensor=_HistoricalSubtensor(hotkeys),
    )
    assert result == {
        "historical_launch": "PASS",
        "launch_extrinsic": "0x" + "c" * 64,
        "launch_block": 101,
        "finalized_head_block": 200,
    }


def test_historical_launch_verifies_targets_that_never_rotated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.assert_sn39_public_reproduction import verify_historical_launch

    release, hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    assert isinstance(launch, dict)
    _unlock_signed_launch_targets(launch)
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    result = verify_historical_launch(
        release,
        subtensor=_HistoricalSubtensor(hotkeys, mutation="never_rotated"),
    )
    assert result["historical_launch"] == "PASS"


def test_historical_launch_rejects_a_signed_lock_the_archive_denies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        verify_historical_launch,
    )

    # The signed release claims a live lock; the archive says it never rotated.
    release, hotkeys = _historical_launch_fixture()
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    with pytest.raises(ReproductionError, match="historical UID/hotkey safety differs"):
        verify_historical_launch(
            release,
            subtensor=_HistoricalSubtensor(hotkeys, mutation="never_rotated"),
        )


def test_historical_launch_rejects_mapping_at_or_after_extrinsic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        verify_historical_launch,
    )

    release, hotkeys = _historical_launch_fixture()
    launch = release["launch_submission"]
    assert isinstance(launch, dict)
    mapping = launch["mapping"]
    snapshot = mapping["metagraph_snapshot"]
    assert isinstance(mapping, dict) and isinstance(snapshot, dict)
    mapping["block"] = 101
    snapshot["block"] = 101
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    with pytest.raises(ReproductionError, match="extrinsic is malformed"):
        verify_historical_launch(
            release,
            subtensor=_HistoricalSubtensor(hotkeys),
        )


def test_public_reproduction_deadline_bounds_archive_calls() -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionNotProven,
        _bounded_archive_call,
    )

    with pytest.raises(ReproductionNotProven, match="deadline exceeded"):
        _bounded_archive_call(
            time.monotonic() + 0.01,
            "blocked archive read",
            lambda: time.sleep(1),
        )


def test_public_reproduction_archive_unavailability_is_not_a_contradiction() -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        ReproductionNotProven,
        _bounded_archive_call,
    )

    def unavailable() -> None:
        raise ConnectionError("archive offline")

    with pytest.raises(ReproductionNotProven, match="archive lookup is unavailable"):
        _bounded_archive_call(None, "archive lookup", unavailable)

    def contradictory() -> None:
        raise ReproductionError("archive value contradicts the signed release")

    with pytest.raises(
        ReproductionError,
        match="contradicts the signed release",
    ) as error:
        _bounded_archive_call(None, "archive lookup", contradictory)
    assert type(error.value) is ReproductionError


def test_public_reproduction_fetch_unavailability_is_not_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionNotProven,
        verify_public_release,
    )

    def unavailable_fetcher(*_args, **_kwargs):
        raise ProvenanceAuditError("public evidence endpoint timed out")

    monkeypatch.setattr(provenance_audit, "_fetcher", unavailable_fetcher)
    with pytest.raises(
        ReproductionNotProven,
        match="public evidence fetch is unavailable",
    ):
        verify_public_release()

    monkeypatch.setattr(
        provenance_audit,
        "_fetcher",
        lambda *_args, **_kwargs: (
            lambda: None,
            lambda _digest: None,
            lambda _path: None,
        ),
    )
    with pytest.raises(
        ReproductionNotProven,
        match="fetch returned incomplete material",
    ):
        verify_public_release()


def test_public_reproduction_incomplete_archive_material_is_not_proven() -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionNotProven,
        _block_timestamp_ms,
    )

    substrate = SimpleNamespace(
        query=lambda **_kwargs: SimpleNamespace(value=None),
    )
    with pytest.raises(
        ReproductionNotProven,
        match="timestamp is unavailable",
    ):
        _block_timestamp_ms(substrate, "0x" + "a" * 64)


@pytest.mark.parametrize(
    ("module_name", "call_name"),
    [
        ("scaffold.sn39_public_reproduction", "assert_public_reproduction"),
        ("scripts.run_sn39_public_reproduction", "run"),
    ],
)
def test_public_reproduction_cli_has_typed_exit_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    module_name: str,
    call_name: str,
) -> None:
    module = __import__(module_name, fromlist=["main"])
    not_proven = sn39_public_reproduction.ReproductionNotProven
    contradiction = sn39_public_reproduction.ReproductionError

    monkeypatch.setattr(
        module,
        call_name,
        lambda: (_ for _ in ()).throw(not_proven("archive unavailable")),
    )
    if module_name == "scripts.run_sn39_public_reproduction":
        monkeypatch.setattr(sys, "argv", ["run_sn39_public_reproduction.py"])
        assert module.main() == 3
    else:
        assert module.main([]) == 3
    assert "NOT_PROVEN: archive unavailable" in capsys.readouterr().err

    monkeypatch.setattr(
        module,
        call_name,
        lambda: (_ for _ in ()).throw(contradiction("signed bytes differ")),
    )
    if module_name == "scripts.run_sn39_public_reproduction":
        assert module.main() == 1
    else:
        assert module.main([]) == 1
    assert "FAIL: signed bytes differ" in capsys.readouterr().err

    monkeypatch.setattr(module, call_name, lambda: {"historical_launch": "PASS"})
    if module_name == "scripts.run_sn39_public_reproduction":
        assert module.main() == 0
    else:
        assert module.main([]) == 0
    assert "SN39 public reproduction: PASS" in capsys.readouterr().out


def test_public_key_bundle_is_checked_against_compiled_pin(tmp_path: Path) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        _load_pinned_key_document,
    )

    path = tmp_path / "release-attestation-keys.json"
    path.write_text('{"attacker":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}')
    with pytest.raises(ReproductionError, match="compiled byte pin"):
        _load_pinned_key_document(path, "release_attestation_keys")


def test_public_json_rejects_exponent_overflow() -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        _strict_json_bytes,
    )

    with pytest.raises(ReproductionError, match="non-finite"):
        _strict_json_bytes(
            b'{"value":1e400}',
            label="counterexample",
            canonical=False,
        )


def test_launch_contract_is_one_shot_full_gated_and_fully_pinned(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.once = True
    args.max_submissions = 1
    args.require_full_provenance_for_broadcast = True
    _pin_sn39_runtime(args, launch=True)
    validator_thin._validate_runtime_contract(args)

    read_only = SimpleNamespace(**vars(args))
    read_only.launch_preflight = True
    read_only.broadcast = False
    validator_thin._validate_runtime_contract(read_only)

    for field, value in (
        ("once", False),
        ("max_submissions", 2),
        ("provenance_controlled_dir", None),
    ):
        broken = SimpleNamespace(**vars(args))
        setattr(broken, field, value)
        with pytest.raises(validator_thin.wire.VectorError, match="launch"):
            validator_thin._validate_runtime_contract(broken)


def test_launch_rewarded_set_gate_requires_exact_independent_uid_agreement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, "shadow")
    args.provenance_controlled_dir = "/controlled"
    args.provenance_verifier_binary = "/verifier"
    args.provenance_burn_hotkey = "burn-hotkey"
    args.provenance_registry_keys_digest = "sha256:" + "1" * 64
    args.provenance_report_keys_digest = "sha256:" + "2" * 64
    args.provenance_index_keys_digest = "sha256:" + "3" * 64
    args.provenance_source_revision = "a" * 40
    monkeypatch.setattr(
        validator_thin,
        "_historical_metagraph_lookup",
        lambda *_a: lambda _block: {"tdx-miner", "burn-hotkey"},
    )
    monkeypatch.setattr(
        validator_thin,
        "_block_hash_lookup",
        lambda *_a: lambda _block: "0x" + "a" * 64,
    )

    audit = ProvenanceAudit(
        status="PASS",
        assurance="full",
        agrees_with_vector=True,
        source_epoch=12,
        report_id="sha256:" + "a" * 64,
        index_source_epoch=12,
        index_manifest="sha256:" + "b" * 64,
        policy_release=3,
        policy_digest="sha256:" + "c" * 64,
        manifest_digest="sha256:" + "b" * 64,
        recomputed={"tdx-miner": 1.0},
        receipt_hotkeys=["tdx-miner"],
        raw_replayed_hotkeys=["tdx-miner"],
    )
    monkeypatch.setattr(validator_thin, "run_audit", lambda *_a, **_k: audit)
    validator_thin._run_launch_rewarded_set_gate(
        args,
        payload=validated_supply_payload(),
        uid_weights={163: 0.9, 204: 0.1},
        hotkey_to_uid={"tdx-miner": 163, "burn-hotkey": 204},
        current_block=900,
        state_file=Path(args.state_file),
    )
    assert args._launch_rewarded_set_audit is audit

    with pytest.raises(validator_thin.wire.VectorError, match="does not match"):
        validator_thin._run_launch_rewarded_set_gate(
            args,
            payload=validated_supply_payload(),
            uid_weights={163: 0.8, 204: 0.2},
            hotkey_to_uid={"tdx-miner": 163, "burn-hotkey": 204},
            current_block=900,
            state_file=Path(args.state_file),
        )


def test_common_journal_enforces_attempt_budget_and_lane_transition(
    tmp_path: Path,
) -> None:
    args = _authority_args(tmp_path)
    args.max_submissions = 1
    first = "sha256:" + "a" * 64
    validator_thin._reserve_common_submission(
        args,
        lane="authority",
        attempt_id=first,
        identity={"source_epoch": 10, "uid_weights": [[1, 1.0]]},
    )
    validator_thin._record_pending_broadcast_intent(
        args,
        attempt_id=first,
        extrinsic_hash="0x" + "a" * 64,
        nonce=1,
        era_reference_block=99,
        mortal_period_blocks=4,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[1],
        wire_weights=[65535],
    )
    validator_thin._finalize_common_submission(
        args,
        attempt_id=first,
        submission=validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "b" * 64,
            block_number=100,
            finalized=True,
        ),
    )
    with pytest.raises(ValueError, match="budget 1 is exhausted"):
        validator_thin._reserve_common_submission(
            args,
            lane="authority",
            attempt_id="sha256:" + "b" * 64,
            identity={"source_epoch": 11},
        )

    transition = _authority_args(tmp_path / "transition")
    transition.runtime_root = str(tmp_path / "transition-runtime")
    transition.max_submissions = 0
    authority_id = "sha256:" + "c" * 64
    validator_thin._reserve_common_submission(
        transition,
        lane="authority",
        attempt_id=authority_id,
        identity={"source_epoch": 20, "uid_weights": [[1, 1.0]]},
    )
    validator_thin._record_pending_broadcast_intent(
        transition,
        attempt_id=authority_id,
        extrinsic_hash="0x" + "c" * 64,
        nonce=2,
        era_reference_block=199,
        mortal_period_blocks=4,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[1],
        wire_weights=[65535],
    )
    validator_thin._finalize_common_submission(
        transition,
        attempt_id=authority_id,
        submission=validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "c" * 64,
            block_hash="0x" + "d" * 64,
            block_number=200,
            finalized=True,
        ),
    )
    with pytest.raises(ValueError, match="lane changed"):
        validator_thin._reserve_common_submission(
            transition,
            lane="thin",
            attempt_id="sha256:" + "d" * 64,
            identity={"policy_version": 21},
        )


def test_root_sealed_launch_allows_one_way_thin_to_full_authority_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _authority_args(tmp_path)
    args.offline = False
    args.broadcast = True
    args.network = "finney"
    args.netuid = 39
    args.max_submissions = 0
    args.require_full_provenance_for_broadcast = False
    args.runtime_root = str(tmp_path / "runtime")
    args.state_file = str(tmp_path / "authority.json")
    args._submission_validator_hotkey = "validator-hotkey"
    args._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    launch_attempt = "sha256:" + "1" * 64
    release_sha = "sha256:" + "2" * 64
    reproducer_revision = "3" * 40
    common_path = validator_thin._submission_state_path(args)
    validator_thin._write_state(
        common_path,
        {
            "submission_active_lane": "thin",
            "submission_attempt_ids": [launch_attempt],
            "submission_launch_attempt_ids": [launch_attempt],
            "submission_launch_attempt_id": launch_attempt,
            "submission_launch_status": "finalized",
            "submission_continuous_enabled": True,
            "submission_continuous_launch_attempt_id": launch_attempt,
            "submission_continuous_release_sha256": release_sha,
            "submission_continuous_reproducer_revision": reproducer_revision,
            "submission_validator_hotkey": "validator-hotkey",
            "submission_genesis_hash": validator_thin.FINNEY_GENESIS_HASH,
            "provenance_netuid": 39,
        },
    )
    continuous_authorization = validator_thin.ContinuousAuthorization(
        authorization_sha256="sha256:" + "9" * 64,
        submission_journal=str(common_path),
        launch_attempt_id=launch_attempt,
        release_sha256=release_sha,
        reproducer_revision=reproducer_revision,
        validator_hotkey="validator-hotkey",
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        lanes=("authority", "thin"),
        issued_at="2026-07-24T22:00:00.000Z",
        valid_from_time="2026-07-24T22:00:00.000Z",
        valid_until_time="2099-01-01T00:00:00.000Z",
        valid_from_block=100,
        valid_until_block=300,
        valid_from_nonce=0,
        valid_until_nonce_exclusive=2,
        max_attempts=2,
    )
    from scaffold import sn39_continuous_authorization as recurring

    recurring_verify_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        recurring,
        "verify_authorization",
        lambda **kwargs: (
            recurring_verify_calls.append(kwargs)
            or recurring.VerifiedAuthorization(
                authorization_sha256=continuous_authorization.authorization_sha256,
                submission_journal=continuous_authorization.submission_journal,
                launch_attempt_id=continuous_authorization.launch_attempt_id,
                release_sha256=continuous_authorization.release_sha256,
                reproducer_revision=continuous_authorization.reproducer_revision,
                validator_hotkey=continuous_authorization.validator_hotkey,
                genesis_hash=continuous_authorization.genesis_hash,
                lanes=continuous_authorization.lanes,
                issued_at=continuous_authorization.issued_at,
                valid_from_time=continuous_authorization.valid_from_time,
                valid_until_time=continuous_authorization.valid_until_time,
                valid_from_block=continuous_authorization.valid_from_block,
                valid_until_block=continuous_authorization.valid_until_block,
                valid_from_nonce=continuous_authorization.valid_from_nonce,
                valid_until_nonce_exclusive=(
                    continuous_authorization.valid_until_nonce_exclusive
                ),
                max_attempts=continuous_authorization.max_attempts,
            )
        ),
    )
    authorization = validator_thin._continuous_authorization_identity(
        continuous_authorization
    )
    args._continuous_submission_authorization = continuous_authorization
    with pytest.raises(ValueError, match="recurring authorization differs"):
        validator_thin._reserve_common_submission(
            args,
            lane="authority",
            attempt_id="sha256:" + "4" * 64,
            identity={
                "network": "finney",
                "netuid": 39,
                "validator_hotkey": "validator-hotkey",
                "source_epoch": 20,
                "continuous_authorization": {
                    **authorization,
                    "release_sha256": "sha256:" + "f" * 64,
                },
            },
        )
    with pytest.raises(ValueError, match="lane changed"):
        validator_thin._reserve_common_submission(
            args,
            lane="authority",
            attempt_id="sha256:" + "7" * 64,
            identity={
                "network": "finney",
                "netuid": 38,
                "validator_hotkey": "validator-hotkey",
                "source_epoch": 20,
                "continuous_authorization": authorization,
            },
        )

    policy = validator_thin.InclusionPolicy(
        valid_from_block=100,
        valid_until_block=300,
        valid_from_time=datetime(2026, 7, 24, 22, 0, tzinfo=UTC),
        valid_until_time=datetime(2099, 1, 1, 0, 0, tzinfo=UTC),
        expected_next_epoch_start_block=240,
    )
    uid_safety = {"schema": "fixture_uid_safety"}
    authority_identity = {
        "network": "finney",
        "netuid": 39,
        "validator_hotkey": "validator-hotkey",
        "mapping_block": 199,
        "source_epoch": 20,
        "report_id": "sha256:" + "8" * 64,
        "burn_hotkey": "burn-hotkey",
        "uid_weights": [[1, 0.9], [2, 0.1]],
        "uid_hotkeys": [[1, "tdx-miner"], [2, "burn-hotkey"]],
        "uid_safety": uid_safety,
        "inclusion_policy": validator_thin._inclusion_policy_identity(policy),
        "next_epoch_start_block": 240,
        "continuous_authorization": authorization,
    }
    authority_attempt = "sha256:" + "5" * 64
    validator_thin._reserve_common_submission(
        args,
        lane="authority",
        attempt_id=authority_attempt,
        identity=authority_identity,
    )
    reserved = validator_thin._read_state(common_path)
    assert reserved["submission_pending_lane_transition_from"] == "thin"
    assert reserved["submission_active_lane"] == "thin"
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "tdx-miner": 1,
            "burn-hotkey": 2,
            "validator-hotkey": 30,
        },
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=199,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        next_epoch_start_block=240,
    )
    monkeypatch.setattr(
        validator_thin, "_validate_runtime_contract", lambda _args: None
    )
    monkeypatch.setattr(
        validator_thin,
        "_validate_resolved_chain_contract",
        lambda _args, _preflight: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_inclusion_policy_ready",
        lambda _policy, _preflight: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_require_uid_mapping_stability",
        lambda *_args, **_kwargs: uid_safety,
    )
    submit_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        validator_thin,
        "_validate_chain_constraints",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        validator_thin,
        "_chain_operation_deadline",
        lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        validator_thin,
        "_classify_finalized_receipt",
        lambda *_args, **_kwargs: validator_thin.PASS,
    )

    def submit_exact(
        _preflight: validator_thin.ChainPreflight,
        *,
        runtime_contract: object,
        attempt_id: str,
        version_key: int,
        wire_uids: list[int],
        wire_weights: list[int],
        mortal_period_blocks: int,
        **_kwargs: object,
    ) -> SimpleNamespace:
        submit_calls.append(
            {
                "attempt_id": attempt_id,
                "wire_uids": list(wire_uids),
                "wire_weights": list(wire_weights),
            }
        )
        validator_thin._record_pending_broadcast_intent(
            runtime_contract,
            attempt_id=attempt_id,
            extrinsic_hash="0x" + "a" * 64,
            nonce=2,
            era_reference_block=preflight.block,
            mortal_period_blocks=mortal_period_blocks,
            version_key=version_key,
            wire_uids=wire_uids,
            wire_weights=wire_weights,
        )
        return SimpleNamespace(
            extrinsic_hash="0x" + "a" * 64,
            block_hash="0x" + "b" * 64,
            block_number=200,
            is_success=True,
        )

    monkeypatch.setattr(
        validator_thin,
        "_submit_exact_sn39_extrinsic",
        submit_exact,
    )
    validator_thin._write_state(
        common_path,
        {"submission_pending_lane_transition_from": None},
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="no exact durable state-machine reservation",
    ):
        validator_thin.set_weights_on_chain(
            {1: 0.9, 2: 0.1},
            network="finney",
            netuid=39,
            wallet_name=args.wallet_name,
            wallet_hotkey=args.wallet_hotkey,
            broadcast=True,
            preflight=preflight,
            uid_hotkeys={1: "tdx-miner", 2: "burn-hotkey"},
            inclusion_policy=policy,
            runtime_contract=args,
        )
    assert submit_calls == []
    validator_thin._write_state(
        common_path,
        {"submission_pending_lane_transition_from": "thin"},
    )
    submission = validator_thin.set_weights_on_chain(
        {1: 0.9, 2: 0.1},
        network="finney",
        netuid=39,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        broadcast=True,
        preflight=preflight,
        uid_hotkeys={1: "tdx-miner", 2: "burn-hotkey"},
        inclusion_policy=policy,
        runtime_contract=args,
    )
    assert submission.finalized is True
    assert len(submit_calls) == 1
    assert len(recurring_verify_calls) == 1
    signed = validator_thin._read_state(common_path)
    assert signed["submission_active_lane"] == "authority"
    validator_thin._finalize_common_submission(
        args,
        attempt_id=authority_attempt,
        submission=submission,
    )
    with pytest.raises(ValueError, match="recurring authorization differs"):
        validator_thin._reserve_common_submission(
            args,
            lane="thin",
            attempt_id="sha256:" + "6" * 64,
            identity={"policy_version": 21},
        )


def test_shipped_launch_and_continuous_profiles_share_one_journal_and_gate(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[3]

    def resolved(name: str, *, once: bool = False):
        return cli._resolve_serve_config(
            SimpleNamespace(
                config=str(root / "config" / name),
                dry_run=False,
                once=once,
                offline=False,
            )
        )

    continuous = resolved("validator-mainnet-sn39.toml")
    launch = resolved("validator-mainnet-sn39-launch.toml", once=True)
    for args in (continuous, launch):
        args.runtime_root = str(tmp_path / "shared-runtime")
        args._submission_genesis_hash = "0x" + "1" * 64
        args._submission_validator_hotkey = "5CanonicalValidator"
    assert validator_thin._submission_state_path(
        continuous
    ) == validator_thin._submission_state_path(launch)
    assert continuous.require_completed_launch_for_broadcast is True
    assert launch.require_full_provenance_for_broadcast is True
    assert launch.launch_approval_file == str(validator_thin.SN39_LAUNCH_APPROVAL_FILE)
    with pytest.raises(validator_thin.wire.VectorError, match="reconcile-launch"):
        validator_thin._require_continuous_launch_transition(continuous)
    continuous.require_completed_launch_for_broadcast = False
    continuous.require_policy = "validated_supply_v1"
    continuous.broadcast = True
    # Clearing the flag no longer clears an obligation the runtime actually
    # has: the authority lane originates weights, so it stays gated regardless.
    continuous.provenance = "authority"
    assert validator_thin._continuous_transition_required(continuous) is True
    # A pure relay owes SN39 no launch of its own, so the same flag is an
    # honest opt-out for it. Anything stronger locks third parties off SN39.
    continuous.provenance = "shadow"
    assert validator_thin._continuous_transition_required(continuous) is False


def test_forged_validator_owned_transition_journal_cannot_authorize(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    args._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    args._submission_validator_hotkey = "5CanonicalValidator"
    attempt = "sha256:" + "a" * 64
    validator_thin._write_state_fenced(
        validator_thin._submission_state_path(args),
        {
            "submission_continuous_enabled": True,
            "submission_launch_status": "finalized",
            "submission_launch_attempt_id": attempt,
            "submission_continuous_launch_attempt_id": attempt,
        },
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="continuous launch identity is missing",
    ):
        validator_thin._require_continuous_launch_transition(args)


def test_sn39_mainnet_runtime_root_cannot_be_redirected(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.max_submissions = 0
    args.require_full_provenance_for_broadcast = False
    _pin_sn39_runtime(args)
    args.runtime_root = str(tmp_path / "attacker-controlled-runtime")
    with pytest.raises(validator_thin.wire.VectorError, match="canonical owner-only"):
        validator_thin._validate_runtime_contract(args)


def test_sn39_broadcast_cannot_hide_finney_behind_another_label(
    tmp_path: Path,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    _pin_sn39_runtime(args)
    args.network = "test"
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="immutable trust profile",
    ):
        validator_thin._validate_runtime_contract(args)
    assert validator_thin._continuous_transition_required(args) is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("publisher_url", "https://attacker.invalid"),
        ("public_key_hex", "00" * 32),
        ("key_id", "attacker-policy"),
        ("require_policy", "confidential_primary_v1"),
        ("provenance", "off"),
        ("evidence_url", "https://attacker.invalid/evidence"),
        ("provenance_registry_keys_digest", "sha256:" + "1" * 64),
        ("provenance_report_keys_digest", "sha256:" + "2" * 64),
        ("provenance_index_keys_digest", "sha256:" + "3" * 64),
        ("provenance_verifier_digest", "sha256:" + "4" * 64),
        ("provenance_source_revision", "5" * 40),
        ("provenance_mechanism", "attacker_mechanism"),
        ("provenance_burn_hotkey", "5AttackerBurn"),
        ("state_file", "/tmp/attacker-state.json"),
    ],
)
def test_sn39_broadcast_rejects_every_mutable_trust_profile_override(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.max_submissions = 0
    args.require_full_provenance_for_broadcast = False
    _pin_sn39_runtime(args)
    setattr(args, field, value)
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="immutable trust profile",
    ):
        validator_thin._validate_runtime_contract(args)


@pytest.mark.parametrize(
    "network",
    [
        "test",
        "archive",
        "wss://entrypoint-finney.opentensor.ai:443",
        "wss://self-hosted-finney.example",
    ],
)
def test_resolved_finney_sn39_requires_finney_audience(
    tmp_path: Path,
    network: str,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.require_policy = "validated_supply_v1"
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    args.network = network
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=1,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    with pytest.raises(validator_thin.wire.VectorError, match="finney"):
        validator_thin._validate_resolved_chain_contract(args, preflight)


def test_sn39_broadcast_requires_pinned_finney_genesis(tmp_path: Path) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.require_policy = "validated_supply_v1"
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=1,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash="0x" + "1" * 64,
    )
    with pytest.raises(validator_thin.wire.VectorError, match="pinned Finney"):
        validator_thin._validate_resolved_chain_contract(args, preflight)


@pytest.mark.parametrize(
    ("min_allowed", "max_limit", "commit_reveal"),
    [(2, 1.0, False), (1, 0.9, False), (1, 1.0, True)],
)
def test_sn39_resolved_contract_requires_burn_only_fail_safe(
    tmp_path: Path,
    min_allowed: int,
    max_limit: float,
    commit_reveal: bool,
) -> None:
    args = _args(tmp_path, "shadow")
    args.broadcast = True
    args.offline = False
    args.require_policy = "validated_supply_v1"
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={},
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=1,
        min_allowed_weights=min_allowed,
        max_weight_limit=max_limit,
        commit_reveal_enabled=commit_reveal,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
    )
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="fail safe to burn|commit-reveal disabled",
    ):
        validator_thin._validate_resolved_chain_contract(args, preflight)


def test_immutable_install_binds_venv_and_masks_legacy_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[3]

    def load(path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    builder = load(
        root / "scripts/build_sn39_release_manifest.py",
        "sn39_manifest_builder_test",
    )
    launcher = load(
        root / "deploy/sn39/cathedral-sn39-release-launcher.py",
        "sn39_release_launcher_test",
    )
    expected_distributions = builder.expected_locked_distributions(
        root / "requirements/sn39-reproduction.lock",
        root / "requirements/sn39-build.lock",
    )
    reproduction_lock_digest = (
        "sha256:"
        + hashlib.sha256(
            (root / "requirements/sn39-reproduction.lock").read_bytes()
        ).hexdigest()
    )
    assert (
        reproduction_lock_digest
        == sn39_public_reproduction.EXPECTED_RELEASE_PINS["reproduction_dependencies"]
    )
    build_lock_digest = (
        "sha256:"
        + hashlib.sha256(
            (root / "requirements/sn39-build.lock").read_bytes()
        ).hexdigest()
    )
    assert (
        build_lock_digest
        == sn39_public_reproduction.EXPECTED_RELEASE_PINS[
            "reproduction_build_dependencies"
        ]
    )
    pyproject = (root / "pyproject.toml").read_text()
    assert "#sha256=" + builder.EXPECTED_CATHEDRAL_ARCHIVE_SHA256 in pyproject
    assert "cathedral-sn39-reproduce =" not in pyproject
    inspected = {
        "installed": [
            {
                "metadata": {"name": name, "version": version},
                **(
                    {
                        "direct_url": {
                            "url": builder.EXPECTED_CATHEDRAL_URL,
                            "archive_info": {
                                "hashes": {
                                    "sha256": (
                                        builder.EXPECTED_CATHEDRAL_ARCHIVE_SHA256
                                    )
                                }
                            },
                        }
                    }
                    if name == "cathedral"
                    else {}
                ),
            }
            for name, version in expected_distributions.items()
        ]
        + [{"metadata": {"name": "pip", "version": "26.0"}}]
    }
    builder.validate_installed_distributions(inspected, expected_distributions)
    inspected["installed"][0]["metadata"]["version"] = "0.0.0-substituted"
    with pytest.raises(SystemExit, match="differs from the hash lock"):
        builder.validate_installed_distributions(inspected, expected_distributions)

    venv = tmp_path / "venv"
    binary = venv / "bin/python-real"
    package = venv / "lib/python/site-packages/cathedral.py"
    package.parent.mkdir(parents=True)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"reviewed-python-bytes")
    package.write_bytes(b"reviewed-cathedral-bytes")
    (venv / "bin/python").symlink_to("python-real")
    (venv / "lib64").symlink_to("lib", target_is_directory=True)
    for directory in (
        venv,
        venv / "bin",
        venv / "lib",
        venv / "lib/python",
        package.parent,
    ):
        directory.chmod(0o755)
    binary.chmod(0o555)
    package.chmod(0o444)

    monkeypatch.setattr(builder, "ROOT_UID", os.getuid())
    monkeypatch.setattr(launcher, "ROOT_UID", os.getuid())
    assert builder.BOOTSTRAP_PYTHON == launcher.BOOTSTRAP_PYTHON
    assert launcher.BOOTSTRAP_PYTHON == Path("/usr/bin/python3.12")
    assert builder.INSTALL_ROOT == launcher.INSTALL_ROOT
    assert launcher.INSTALL_ROOT == Path("/etc/cathedral-validator")
    assert launcher.MANIFEST == (
        Path("/etc/cathedral-validator/sn39-release-manifest.json")
    )
    assert set(launcher.CONFIGS.values()) == {
        Path("/etc/cathedral-validator/validator-mainnet-sn39.toml"),
        Path("/etc/cathedral-validator/validator-mainnet-sn39-launch.toml"),
    }
    assert validator_thin.SN39_LAUNCH_CONTROLLED_DIR == Path(
        "/var/lib/cathedral-validator-controlled-sn39/current"
    )
    assert validator_thin.SN39_LAUNCH_APPROVAL_FILE == Path(
        "/etc/cathedral-validator/sn39-launch-approval.json"
    )
    assert (
        'MANIFEST = Path("/etc/cathedral-validator/sn39-release-manifest.json")'
        in (root / "scripts/finalize_sn39_public_release.py").read_text()
    )
    assert (
        "CONTROLLED_ROOT = Path("
        '"/var/lib/cathedral-validator-controlled-sn39/current")'
        in (root / "scripts/finalize_sn39_public_release.py").read_text()
    )
    assert (
        "RELEASE_MANIFEST = Path("
        '"/etc/cathedral-validator/sn39-release-manifest.json")'
        in (root / "scaffold/sn39_continuous_authorization.py").read_text()
    )
    for unit_name in (
        "cathedral-validator-sn39.service",
        "cathedral-validator-sn39-launch.service",
    ):
        unit = (root / "deploy/sn39" / unit_name).read_text("utf-8")
        assert "SupplementaryGroups=cathedral-validator-evidence\n" in unit
        assert (
            "ReadOnlyPaths=/var/lib/cathedral-validator-controlled-sn39/current "
            "/opt/cathedral-sn39/bin/cathedral-tdx-verifier\n"
        ) in unit
    git_calls: list[tuple[list[str], dict[str, object]]] = []
    with pytest.MonkeyPatch.context() as git_patch:
        git_patch.setattr(
            launcher.subprocess,
            "check_output",
            lambda command, **kwargs: (
                git_calls.append((command, kwargs)),
                "reviewed\n",
            )[1],
        )
        assert launcher._git_output(venv, "rev-parse", "HEAD") == "reviewed"
    assert git_calls == [
        (
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={venv}",
                "rev-parse",
                "HEAD",
            ],
            {
                "cwd": venv,
                "text": True,
                "stderr": subprocess.DEVNULL,
                "env": {"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            },
        )
    ]
    assert (
        (root / "deploy/sn39/cathedral-sn39-release-launcher.py")
        .read_text()
        .startswith("#!/usr/bin/python3.12 -I\n")
    )
    with pytest.MonkeyPatch.context() as access_patch:
        access_patch.setattr(
            launcher.os,
            "access",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(
                    "root-capability access checks cannot prove interpreter "
                    "immutability"
                )
            ),
        )
        launcher._require_service_interpreter(venv / "bin/python")
    binary.chmod(0o550)
    with pytest.raises(launcher.InstallError, match="service accounts"):
        launcher._require_service_interpreter(venv / "bin/python")
    binary.chmod(0o555)
    expected = builder.immutable_tree_digest(venv)
    assert launcher._immutable_tree_digest(venv) == expected

    venv.chmod(0o700)
    with pytest.raises(SystemExit, match="readable, and searchable"):
        builder.immutable_tree_digest(venv)
    with pytest.raises(launcher.InstallError, match="root-controlled"):
        launcher._immutable_tree_digest(venv)
    venv.chmod(0o755)

    package.chmod(0o400)
    with pytest.raises(SystemExit, match="service-readable"):
        builder.immutable_tree_digest(venv)
    with pytest.raises(
        launcher.InstallError,
        match="mutable or unsupported",
    ):
        launcher._immutable_tree_digest(venv)
    package.chmod(0o444)

    outside_directory = tmp_path / "outside-lib"
    outside_directory.mkdir()
    outside_directory.chmod(0o755)
    (venv / "external-lib").symlink_to(
        outside_directory,
        target_is_directory=True,
    )
    with pytest.raises(SystemExit, match="symlink target is unsupported"):
        builder.immutable_tree_digest(venv)
    with pytest.raises(launcher.InstallError, match="unsafe target"):
        launcher._immutable_tree_digest(venv)
    (venv / "external-lib").unlink()

    package.chmod(0o644)
    package.write_bytes(b"substituted-cathedral-bytes")
    package.chmod(0o444)
    assert launcher._immutable_tree_digest(venv) != expected

    external_link = tmp_path / "hard-linked-cathedral.py"
    os.link(package, external_link)
    with pytest.raises(SystemExit, match="single-linked"):
        builder.immutable_tree_digest(venv)
    with pytest.raises(
        launcher.InstallError,
        match="mutable or unsupported",
    ):
        launcher._immutable_tree_digest(venv)
    external_link.unlink()

    mask = tmp_path / "cathedral-thin-validator.service"
    mask.symlink_to("/dev/null")
    monkeypatch.setattr(launcher, "LEGACY_SERVICE_MASK", mask)
    launcher._require_legacy_service_masked()
    mask.unlink()
    mask.write_text("legacy writer remains startable")
    with pytest.raises(launcher.InstallError, match="durably masked"):
        launcher._require_legacy_service_masked()

    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout="inactive\n",
            returncode=3,
        ),
    )
    launcher._require_legacy_service_stopped()
    for state in ("active", "activating", "deactivating"):
        monkeypatch.setattr(
            launcher.subprocess,
            "run",
            lambda *_args, _state=state, **_kwargs: SimpleNamespace(
                stdout=f"{_state}\n",
                returncode=0 if _state == "active" else 3,
            ),
        )
        with pytest.raises(launcher.InstallError, match="is not stopped"):
            launcher._require_legacy_service_stopped()

    continuous_unit = (
        root / "deploy/sn39/cathedral-validator-sn39.service"
    ).read_text()
    assert "Conflicts=cathedral-thin-validator.service" in continuous_unit
    assert "After=network-online.target cathedral-thin-validator.service" in (
        continuous_unit
    )
    assert "Group=cathedral-validator-log" in continuous_unit
    assert "EnvironmentFile=" not in continuous_unit
    assert (
        "ExecStart=/usr/bin/python3.12 -I -E -s "
        "/usr/local/libexec/cathedral-sn39-release continuous"
    ) in continuous_unit
    reconcile_unit = (
        root / "deploy/sn39/cathedral-validator-sn39-reconcile.service"
    ).read_text()
    assert "User=cathedral-validator" in reconcile_unit
    assert "Environment=HOME=/var/lib/cathedral-validator" in reconcile_unit
    assert "EnvironmentFile=" not in reconcile_unit
    assert (
        "ExecStart=/usr/bin/python3.12 -I -E -s "
        "/usr/local/libexec/cathedral-sn39-release reconcile"
    ) in reconcile_unit
    assert (
        "After=network-online.target cathedral-validator-sn39-launch.service "
        "cathedral-thin-validator.service"
    ) in reconcile_unit
    launch_unit = (
        root / "deploy/sn39/cathedral-validator-sn39-launch.service"
    ).read_text()
    assert "EnvironmentFile=" not in launch_unit
    assert "TimeoutStartSec=20min" in launch_unit
    assert (
        "ExecStart=/usr/bin/python3.12 -I -E -s "
        "/usr/local/libexec/cathedral-sn39-release launch"
    ) in launch_unit
    assert "After=network-online.target cathedral-thin-validator.service" in (
        launch_unit
    )
    child_environment = launcher._child_environment()
    assert set(child_environment) == {
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "CATHEDRAL_VALIDATOR_JSONL_GROUP",
    }
    assert (
        child_environment["CATHEDRAL_VALIDATOR_JSONL_GROUP"]
        == "cathedral-validator-log"
    )
    status_environment = launcher._child_environment("status")
    assert status_environment["HOME"] == "/var/lib/cathedral-public-evidence"
    assert "CATHEDRAL_VALIDATOR_JSONL_GROUP" not in status_environment
    launch_environment = launcher._child_environment(
        "launch",
        release_sha="a" * 40,
        launch_config_sha256="sha256:" + "b" * 64,
    )
    assert launch_environment["CATHEDRAL_SN39_RELEASE_SHA"] == "a" * 40
    assert launch_environment["CATHEDRAL_SN39_LAUNCH_CONFIG_SHA256"] == (
        "sha256:" + "b" * 64
    )
    assert "preflight" in launcher.MODES
    assert "authorize-recurring" in launcher.MODES
    status_unit = (
        root / "deploy/sn39/cathedral-sn39-public-status.service"
    ).read_text()
    # The status publisher runs as the account that owns the directory it
    # writes. ReadWritePaths= is a mount-namespace control, not a permission
    # grant, so an identity that does not own /var/lib/cathedral-public-evidence/logs
    # gets EACCES on its own output unless something first chowns a tree the
    # producer is actively writing.
    assert "User=polaris" in status_unit
    assert "Group=polaris" in status_unit
    assert "SupplementaryGroups=cathedral-validator-log" in status_unit

    def _directives(text: str) -> list[str]:
        """Non-comment, non-blank lines. Comments explain the removed identity
        by name, so a naive substring check would match the explanation."""
        return [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    assert not [ln for ln in _directives(status_unit) if "cathedral-status" in ln]
    sysusers = (root / "deploy/sn39/cathedral-sn39-validator.sysusers").read_text()
    assert "u cathedral-validator " in sysusers
    assert "g cathedral-validator-log " in sysusers
    # No cathedral-status identity is declared anywhere in the contract. An
    # account that nothing runs as is worse than no account: it produced units
    # that could not write their own output on a host where the account had
    # never been created.
    assert not [ln for ln in _directives(sysusers) if "cathedral-status" in ln]
    assert (
        "ExecStart=/usr/bin/python3.12 -I -E -s "
        "/usr/local/libexec/cathedral-sn39-release status"
    ) in status_unit
    release_guide = (root / "docs/SN39_MAINNET_RELEASE_20260724.md").read_text()
    assert '/usr/bin/python3.12 -m venv "$venv"' in release_guide
    assert (
        "/usr/bin/python3.12 -I -E -s \\\n"
        '  "$release/scripts/build_sn39_release_manifest.py"'
    ) in release_guide
    assert (
        "/usr/local/libexec/cathedral-sn39-release finalize "
        "\\\n  /var/lib/cathedral-validator/journal-<64-hex-digest>.json"
    ) in release_guide
    assert '"$release/scripts/finalize_sn39_public_release.py"' not in release_guide
    assert (
        "ReadOnlyPaths=/var/log/cathedral-validator/validator-events.jsonl"
        in status_unit
    )
    assert "ReadWritePaths=/var/lib/cathedral-public-evidence/logs" in status_unit
    # release.json exists only after a root-signed release is sealed. An
    # unprefixed ReadOnlyPaths= on a missing path fails mount-namespace setup
    # and takes the unit down, converting "no sealed release yet" into "status
    # publishing is broken".
    assert (
        "ReadOnlyPaths=-/var/lib/cathedral-public-evidence/release.json" in status_unit
    )
    # Units that declare the same LogsDirectory= must declare the same Group=.
    # systemd applies the unit's User:Group to a logs directory it manages, so
    # a mismatch lets whichever unit ran last silently re-group the directory
    # and revoke the status publisher's group read on validator-events.jsonl.
    _logs_dir_group: dict[str, set[str]] = {}
    for _unit_name in (
        "cathedral-validator-sn39.service",
        "cathedral-validator-sn39-launch.service",
        "cathedral-validator-sn39-reconcile.service",
    ):
        _text = (root / "deploy/sn39" / _unit_name).read_text()
        _logs = [
            line.split("=", 1)[1].strip()
            for line in _text.splitlines()
            if line.startswith("LogsDirectory=")
        ]
        _groups = [
            line.split("=", 1)[1].strip()
            for line in _text.splitlines()
            if line.startswith("Group=")
        ]
        assert len(_logs) == 1 and len(_groups) == 1, _unit_name
        _logs_dir_group.setdefault(_logs[0], set()).add(_groups[0])
    for _logs_dir, _groups_seen in _logs_dir_group.items():
        assert len(_groups_seen) == 1, (
            f"{_logs_dir} is managed by units with differing Group= "
            f"({sorted(_groups_seen)}); the last unit to run re-groups it"
        )
    assert _logs_dir_group["cathedral-validator"] == {"cathedral-validator-log"}
    tmpfiles = (root / "deploy/sn39/cathedral-sn39-validator.tmpfiles").read_text()
    assert "d /var/lib/cathedral-public-evidence :0755 :root :root -" in tmpfiles
    assert (
        "d /var/lib/cathedral-public-evidence/blobs/sha256 :0755 :root :root -"
        in tmpfiles
    )
    assert (
        "d /var/lib/cathedral-public-evidence/logs :0755 :polaris :polaris -"
    ) in tmpfiles
    assert not [ln for ln in _directives(tmpfiles) if "cathedral-status" in ln]
    # The contract describes the whole published tree, not a subset. A fresh
    # host that is missing these has to have them created by the producer at
    # first export instead of by the reviewed contract.
    for _subdir in ("epochs", "pins", "receipts", "score-classes"):
        assert (
            f"d /var/lib/cathedral-public-evidence/{_subdir} :0755 :root :root -"
            in tmpfiles
        )
    # The evidence tree is the producer's on an established host and is written
    # every few minutes by a running service. systemd-tmpfiles applies an
    # unprefixed mode or ownership field to inodes that ALREADY exist, so a
    # single unprefixed line would chown that live directory on every
    # `systemd-tmpfiles --create`, including the one systemd runs at boot.
    # Assert the property, not just the three known lines, so a future line
    # cannot reintroduce the hazard.
    tmpfiles_directives = [
        line.split()
        for line in tmpfiles.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert tmpfiles_directives
    for fields in tmpfiles_directives:
        kind, path, mode, user, group = fields[0], fields[1], *fields[2:5]
        # z/Z create nothing; they exist only to force mode and ownership onto
        # an inode that already exists.
        assert kind == "d", f"{path} uses {kind!r}, which only mutates existing inodes"
        assert mode.startswith(":"), f"{path} applies mode {mode} to existing inodes"
        assert user.startswith(":"), f"{path} chowns existing inodes to {user}"
        assert group.startswith(":"), f"{path} chgrps existing inodes to {group}"
    assert {
        "deploy/sn39/cathedral-validator-sn39.service",
        "deploy/sn39/cathedral-validator-sn39-launch.service",
        "deploy/sn39/cathedral-validator-sn39-reconcile.service",
        "deploy/sn39/cathedral-sn39-public-status.service",
        "deploy/sn39/cathedral-sn39-public-status.timer",
        "deploy/sn39/cathedral-sn39-validator.sysusers",
        "deploy/sn39/cathedral-sn39-validator.tmpfiles",
        "scripts/publish_sn39_validator_status.py",
        "scripts/finalize_sn39_public_release.py",
        "scripts/build_sn39_rotation_manifest.py",
        "scripts/sn39_hotkey_rotation_operator.py",
        "deploy/sn39/cathedral-sn39-rotation-launcher.py",
        "requirements/sn39-build.in",
        "requirements/sn39-build.lock",
    }.issubset(set(builder.RELEASE_FILES))


def test_release_manifest_git_uses_absolute_binary_and_sanitized_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "sn39_manifest_builder_git_test",
        root / "scripts/build_sn39_release_manifest.py",
    )
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def checked_output(argv: list[str], **kwargs: object) -> str:
        calls.append((argv, kwargs))
        return "reviewed\n"

    monkeypatch.setattr(builder.subprocess, "check_output", checked_output)
    assert builder.git(tmp_path, "rev-parse", "HEAD") == "reviewed"
    assert calls == [
        (
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={tmp_path}",
                "rev-parse",
                "HEAD",
            ],
            {
                "cwd": tmp_path,
                "text": True,
                "stderr": subprocess.DEVNULL,
                "env": {"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            },
        )
    ]
    monkeypatch.setattr(
        builder.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("git unavailable")),
    )
    with pytest.raises(SystemExit, match="cannot verify reviewed release source"):
        builder.git(tmp_path, "status", "--porcelain=v1")


def test_finalizer_launcher_rechecks_install_and_binds_exact_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "sn39_finalizer_launcher_test",
        root / "deploy/sn39/cathedral-sn39-release-launcher.py",
    )
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    release_sha = "a" * 40
    release = tmp_path / release_sha
    python = tmp_path / "venv/bin/python"
    release.mkdir()
    manifest_digest = "sha256:" + "b" * 64
    journal = launcher.RUNTIME_ROOT / ("journal-" + "c" * 64 + ".json")
    verified_modes: list[str] = []
    executed: dict[str, object] = {}

    def verify(mode: str):
        verified_modes.append(mode)
        return release, python, manifest_digest

    def execve(
        executable: Path,
        command: list[str],
        environment: dict[str, str],
    ) -> None:
        executed.update(
            executable=executable,
            command=command,
            environment=environment,
        )
        raise RuntimeError("captured exec")

    monkeypatch.setattr(launcher, "ROOT_UID", os.getuid())
    monkeypatch.setattr(launcher, "_verify", verify)
    monkeypatch.setattr(launcher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(launcher.os, "execve", execve)
    with pytest.raises(RuntimeError, match="captured exec"):
        launcher.main(["finalize", str(journal)])

    assert verified_modes == ["finalize"]
    assert executed["executable"] == python
    assert executed["command"] == [
        str(python),
        "-I",
        "-B",
        str(release / "scripts/finalize_sn39_public_release.py"),
        "--release",
        str(release),
        "--release-sha",
        release_sha,
        "--journal",
        str(journal),
    ]
    environment = executed["environment"]
    assert isinstance(environment, dict)
    assert environment[launcher.FINALIZER_CONTEXT_ENV] == (
        launcher._finalizer_context_digest(
            release_sha=release_sha,
            journal=journal,
            manifest_digest=manifest_digest,
        )
    )

    verified_modes.clear()
    assert launcher.main(["finalize", str(tmp_path / journal.name)]) == 1
    assert verified_modes == []

    recurring_args = [
        "--journal",
        str(journal),
        "--expected-validator-hotkey",
        "5" + "A" * 47,
        "--reviewed-finalized-block",
        "100",
        "--reviewed-validator-nonce",
        "17",
        "--max-attempts",
        "1",
        "--valid-for-blocks",
        "4",
        "--valid-for-seconds",
        "240",
        "--i-authorize-recurring-mainnet-writes",
    ]
    executed.clear()
    verified_modes.clear()
    with pytest.raises(RuntimeError, match="captured exec"):
        launcher.main(["authorize-recurring", *recurring_args])
    assert verified_modes == ["authorize-recurring"]
    assert executed["command"][:6] == [
        str(python),
        "-I",
        "-E",
        "-s",
        "-B",
        "-c",
    ]
    assert executed["command"][7:] == [
        str(release),
        "scaffold.sn39_continuous_authorization",
        *recurring_args,
    ]
    recurring_environment = executed["environment"]
    assert isinstance(recurring_environment, dict)
    assert recurring_environment[launcher.RECURRING_AUTHORIZER_CONTEXT_ENV] == (
        launcher._recurring_authorizer_context_digest(
            release_sha=release_sha,
            manifest_digest=manifest_digest,
            arguments=recurring_args,
        )
    )


def test_launch_rechecks_fresh_mapping_and_report_window_after_rewarded_set_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, "shadow")
    args.offline = False
    args.require_policy = "validated_supply_v1"
    args.public_key_hex = "00" * 32
    args.key_id = "cathedral-weight-policy"
    args.wallet_name = "validator"
    args.wallet_hotkey = "default"
    args.provenance_burn_hotkey = "burn-hotkey"
    args._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    args._submission_validator_hotkey = "validator-hotkey"
    payload = validated_supply_payload()
    payload.update(
        {
            "generated_at": "2026-07-24T22:00:00.000Z",
            "expires_at": "2099-01-01T00:00:00.000Z",
        }
    )
    audit = ProvenanceAudit(
        status="PASS",
        assurance="full",
        agrees_with_vector=True,
        recomputed={"tdx-miner": 1.0},
        manifest_generated_at="2026-07-24T22:10:00.000Z",
        candidate_block=900,
        candidate_block_hash="0x" + "a" * 64,
        report_generated_at="2026-07-24T22:30:00.000Z",
        report_valid_until="2099-01-01T00:00:00.000Z",
        report_valid_from_block=900,
        report_valid_until_block=1000,
        signed_index={"generated_at": "2026-07-24T22:20:00.000Z"},
    )
    monkeypatch.setattr(validator_thin, "accept_vector", lambda *_a, **_k: None)
    monkeypatch.setattr(
        validator_thin,
        "_require_uid_mapping_stability",
        lambda _preflight, uid_hotkeys, **_kwargs: {
            "schema": "cathedral_sn39_uid_safety_v2",
            "registration": {"fixture": True},
            "rotation": {
                "status": "PASS",
                "targets": [
                    {
                        "uid": uid,
                        "hotkey": hotkey,
                        "rotation_receipt": {
                            "block_number": 899,
                            "block_timestamp": "2026-07-24T21:00:00.000Z",
                        },
                    }
                    for uid, hotkey in sorted(uid_hotkeys.items())
                ],
            },
        },
    )
    rewarded_uid = {"value": 163}
    monkeypatch.setattr(
        validator_thin,
        "chain_preflight",
        lambda **_kw: validator_thin.ChainPreflight(
            wallet=object(),
            subtensor=object(),
            hotkey_to_uid={
                "tdx-miner": rewarded_uid["value"],
                "burn-hotkey": 204,
                "validator-hotkey": 30,
            },
            validator_hotkey="validator-hotkey",
            validator_uid=30,
            block=950,
            min_allowed_weights=1,
            max_weight_limit=1.0,
            genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
            subnet_owner_hotkey="burn-hotkey",
            blocks_until_next_epoch=80,
            next_epoch_start_block=1030,
            weights_rate_limit=0,
            validator_blocks_since_last_update=1,
            uid_mapping_stable_until_block=954,
            replacement_safe_hotkeys=frozenset({"tdx-miner", "burn-hotkey"}),
        ),
    )
    fresh, mapping, weights = (
        validator_thin._revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=audit,
            fence_version=-1,
        )
    )
    assert fresh.block == 950
    assert mapping["tdx-miner"] == 163
    assert weights == {163: 0.9, 204: 0.1}
    assert args._launch_inclusion_policy.valid_from_block == 900
    assert args._launch_inclusion_policy.valid_until_block == 1000

    rewarded_uid["value"] = 164
    _fresh, moved_mapping, moved_weights = (
        validator_thin._revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=audit,
            fence_version=-1,
        )
    )
    assert moved_mapping["tdx-miner"] == 164
    assert moved_weights == {164: 0.9, 204: 0.1}

    rewarded_uid["value"] = 30
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="rewarded-hotkey and subnet-owner burn-hotkey 90/10",
    ):
        validator_thin._revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=audit,
            fence_version=-1,
        )

    rewarded_uid["value"] = 163
    audit.report_valid_until_block = 950
    with pytest.raises(validator_thin.wire.VectorError, match="validity window"):
        validator_thin._revalidate_launch_after_rewarded_set_replay(
            args,
            payload=payload,
            audit=audit,
            fence_version=-1,
        )


def test_authority_refreshes_mapping_and_validity_after_full_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, "authority")
    args.offline = False
    args.broadcast = True
    args.require_policy = "validated_supply_v1"
    args.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    args.wallet_name = "validator"
    args.wallet_hotkey = "default"
    args.provenance_burn_hotkey = "burn-hotkey"
    args._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    args._submission_validator_hotkey = "validator-hotkey"
    args._tick_preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "tdx-miner": 163,
            "burn-hotkey": 204,
            "validator-hotkey": 30,
        },
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=900,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        commit_reveal_enabled=False,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey="burn-hotkey",
        blocks_until_next_epoch=80,
        next_epoch_start_block=980,
        weights_rate_limit=0,
        validator_blocks_since_last_update=1,
        uid_mapping_stable_until_block=904,
        replacement_safe_hotkeys=frozenset({"tdx-miner", "burn-hotkey"}),
    )
    audit = ProvenanceAudit(
        status="PASS",
        assurance="full",
        recomputed={"tdx-miner": 1.0},
        report_generated_at="2026-07-24T22:30:00.000Z",
        report_valid_until="2099-01-01T00:00:00.000Z",
        report_valid_from_block=900,
        report_valid_until_block=1000,
    )
    fresh_block = {"value": 950}

    def fresh_preflight(**_kwargs):
        return validator_thin.ChainPreflight(
            wallet=object(),
            subtensor=object(),
            hotkey_to_uid={
                "tdx-miner": 164,
                "burn-hotkey": 204,
                "validator-hotkey": 30,
            },
            validator_hotkey="validator-hotkey",
            validator_uid=30,
            block=fresh_block["value"],
            min_allowed_weights=1,
            max_weight_limit=1.0,
            commit_reveal_enabled=False,
            genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
            subnet_owner_hotkey="burn-hotkey",
            blocks_until_next_epoch=80,
            next_epoch_start_block=fresh_block["value"] + 80,
            weights_rate_limit=0,
            validator_blocks_since_last_update=1,
            uid_mapping_stable_until_block=fresh_block["value"] + 4,
            replacement_safe_hotkeys=frozenset({"tdx-miner", "burn-hotkey"}),
        )

    monkeypatch.setattr(validator_thin, "chain_preflight", fresh_preflight)
    monkeypatch.setattr(
        validator_thin,
        "_require_uid_mapping_stability",
        lambda *_args, **_kwargs: {
            "schema": "cathedral_sn39_uid_safety_v2",
            "registration": {"fixture": True},
            "rotation": {"status": "PASS", "targets": []},
        },
    )
    fresh, mapping, weights, policy = validator_thin._revalidate_authority_after_audit(
        args,
        audit=audit,
        recomputed={"tdx-miner": 1.0},
    )
    assert fresh is args._tick_preflight
    assert mapping["tdx-miner"] == 164
    assert weights == {164: pytest.approx(0.9), 204: pytest.approx(0.1)}
    assert policy.valid_until_block == 1000

    fresh_block["value"] = 1000
    with pytest.raises(validator_thin.wire.VectorError, match="mortal era"):
        validator_thin._revalidate_authority_after_audit(
            args,
            audit=audit,
            recomputed={"tdx-miner": 1.0},
        )


def test_documented_bytecode_disabled_run_keeps_reproducer_checkout_pristine(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    package = checkout / "probe"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 39\n")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "config", "user.email", "sn39-reproducer@example.invalid"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "SN39 Reproducer"],
        cwd=checkout,
        check=True,
    )
    subprocess.run(["git", "add", "probe/__init__.py"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        cwd=checkout,
        check=True,
    )

    environment = {
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run(
        [sys.executable, "-B", "-c", "import probe; assert probe.VALUE == 39"],
        cwd=checkout,
        env=environment,
        check=True,
    )
    expected = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=checkout,
        text=True,
    ).strip()
    assert sn39_public_reproduction._repo_revision(checkout) == expected


def test_documented_direct_reproducer_resolves_its_own_checkout(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    runner = repository / "scripts/run_sn39_public_reproduction.py"
    result = subprocess.run(
        [sys.executable, "-B", str(runner), "unexpected-argument"],
        cwd=tmp_path,
        env={
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "usage: run_sn39_public_reproduction.py" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def test_reconcile_launch_proves_record_and_unlocks_shared_continuous_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = _args(tmp_path, "shadow")
    launch.offline = False
    launch.broadcast = True
    launch.once = True
    launch.max_submissions = 1
    launch.require_full_provenance_for_broadcast = True
    launch.require_policy = "validated_supply_v1"
    launch.runtime_root = str(validator_thin._VALIDATOR_RUNTIME_ROOT)
    launch.wallet_name = "validator"
    launch.wallet_hotkey = "default"
    launch.provenance_burn_hotkey = "burn-hotkey"
    launch._submission_genesis_hash = validator_thin.FINNEY_GENESIS_HASH
    launch._submission_validator_hotkey = "validator-hotkey"
    attempt = "sha256:" + "a" * 64
    uid_safety = {
        "schema": "cathedral_sn39_uid_safety_v2",
        "registration": {"fixture": True},
        "rotation": {"status": "PASS", "targets": []},
    }
    identity = {
        "network": "finney",
        "netuid": 39,
        "mapping_block": 900,
        "next_epoch_start_block": 940,
        "policy_version": 1,
        "validator_hotkey": "validator-hotkey",
        "validator_uid": 30,
        "vector_id": "launch-vector",
        "signed_vector_sha256": "sha256:" + "2" * 64,
        "burn_hotkey": "burn-hotkey",
        "uid_weights": [[163, 0.9], [204, 0.1]],
        "uid_hotkeys": [[163, "tdx-miner"], [204, "burn-hotkey"]],
        "uid_safety": uid_safety,
        "full_provenance": {
            "source_epoch": 90,
            "report_id": "sha256:" + "3" * 64,
            "manifest": "sha256:" + "4" * 64,
            "policy_release": 10,
            "policy_digest": "sha256:" + "5" * 64,
            "mechanism": "validated_supply_v1",
            "scope": "rewarded_set_full",
            "whole_epoch_assurance": "receipts_only",
            "vector_agrees": True,
            "rewarded_hotkeys": ["tdx-miner"],
            "raw_replayed_hotkeys": ["tdx-miner"],
            "verifier_digest": "sha256:" + "6" * 64,
            "source_revision": "7" * 40,
        },
    }
    validator_thin._reserve_common_submission(
        launch,
        lane="thin",
        attempt_id=attempt,
        identity=identity,
    )
    validator_thin._record_pending_broadcast_intent(
        launch,
        attempt_id=attempt,
        extrinsic_hash="0x" + "b" * 64,
        nonce=17,
        era_reference_block=900,
        mortal_period_blocks=4,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[163, 204],
        wire_weights=[65535, 7282],
    )
    validator_thin._finalize_common_submission(
        launch,
        attempt_id=attempt,
        submission=validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "b" * 64,
            block_hash="0x" + "c" * 64,
            block_number=901,
            finalized=True,
        ),
    )
    preflight = validator_thin.ChainPreflight(
        wallet=object(),
        subtensor=object(),
        hotkey_to_uid={
            "tdx-miner": 163,
            "burn-hotkey": 204,
            "validator-hotkey": 30,
        },
        validator_hotkey="validator-hotkey",
        validator_uid=30,
        block=902,
        min_allowed_weights=1,
        max_weight_limit=1.0,
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        subnet_owner_hotkey="burn-hotkey",
        blocks_until_next_epoch=80,
        weights_rate_limit=0,
        validator_blocks_since_last_update=0,
    )
    monkeypatch.setattr(validator_thin, "chain_preflight", lambda **_kw: preflight)
    monkeypatch.setattr(
        validator_thin,
        "_prove_finalized_receipt",
        lambda *_a, **_kw: True,
    )
    hotkeys = [f"hotkey-{uid}" for uid in range(205)]
    hotkeys[30] = "validator-hotkey"
    hotkeys[163] = "tdx-miner"
    hotkeys[204] = "burn-hotkey"
    public_release = {
        "network": "finney",
        "netuid": 39,
        "source_revisions": {
            "producer": "7" * 40,
            "validator": "8" * 40,
        },
        "launch_submission": {
            "vector_id": "launch-vector",
            "policy_version": 1,
            "signed_vector_sha256": "sha256:" + "2" * 64,
            "broadcast_intent": {
                "extrinsic_hash": "0x" + "b" * 64,
                "nonce": 17,
                "era_reference_block": 900,
                "mortal_period_blocks": 4,
                "version_key": validator_thin._weight_version_key(),
                "wire_uids": [163, 204],
                "wire_weights": [65535, 7282],
            },
            "mapping": {
                "block": 900,
                "validator_hotkey": "validator-hotkey",
                "validator_uid": 30,
                "rewarded_uid": 163,
                "rewarded_hotkey": "tdx-miner",
                "burn_uid": 204,
                "burn_hotkey": "burn-hotkey",
                "next_epoch_start_block": 940,
                "uid_weights": {"163": 0.9, "204": 0.1},
                "uid_safety": uid_safety,
                "metagraph_snapshot": {
                    "block": 900,
                    "uids": list(range(205)),
                    "hotkeys": hotkeys,
                    "validator_permit": [index == 30 for index in range(len(hotkeys))],
                },
            },
            "extrinsic": {
                "hash": "0x" + "b" * 64,
                "block_hash": "0x" + "c" * 64,
                "block": 901,
                "validator_uid": 30,
                "uids": [163, 204],
                "weights_u16": [65535, 7282],
                "version_key": validator_thin._weight_version_key(),
            },
            "evidence_checkpoint": {
                "source_epoch": 90,
                "report_id": "sha256:" + "3" * 64,
                "manifest": "sha256:" + "4" * 64,
                "policy_release": 10,
                "policy_digest": "sha256:" + "5" * 64,
                "verifier_digest": "sha256:" + "6" * 64,
                "reward_mechanism": {"id": "validated_supply_v1"},
            },
        },
    }
    public_result = {
        "release_attestation": "PASS",
        "historical_launch": "PASS",
        "evidence_checkpoint": "PASS",
        "reproducer_revision": "8" * 40,
        "release": public_release,
    }
    monkeypatch.setattr(
        "scaffold.sn39_public_reproduction.verify_public_release",
        lambda: public_result,
    )
    continuous = SimpleNamespace(**vars(launch))
    continuous.require_full_provenance_for_broadcast = False
    continuous.max_submissions = 0
    result = validator_thin.reconcile_launch_transition(continuous)
    assert result["status"] == "PASS"
    assert result["release_attestation"] == "PASS"
    continuous._tick_preflight = preflight
    with pytest.raises(
        validator_thin.wire.VectorError,
        match="separate root-signed recurring-write authorization",
    ):
        validator_thin._require_continuous_launch_transition(continuous)

    from scaffold import sn39_continuous_authorization as recurring

    verified_recurring = recurring.VerifiedAuthorization(
        authorization_sha256="sha256:" + "d" * 64,
        submission_journal=str(validator_thin._submission_state_path(continuous)),
        launch_attempt_id=attempt,
        release_sha256=result["release_sha256"],
        reproducer_revision=result["reproducer_revision"],
        validator_hotkey="validator-hotkey",
        genesis_hash=validator_thin.FINNEY_GENESIS_HASH,
        lanes=("thin",),
        issued_at="2026-07-25T00:00:00.000Z",
        valid_from_time="2026-07-25T00:00:00.000Z",
        valid_until_time="2026-07-28T00:00:00.000Z",
        valid_from_block=900,
        valid_until_block=10_000,
        valid_from_nonce=17,
        valid_until_nonce_exclusive=20,
        max_attempts=3,
    )
    monkeypatch.setattr(
        recurring,
        "verify_authorization",
        lambda **_kwargs: verified_recurring,
    )
    recurring_authorization = validator_thin._require_continuous_launch_transition(
        continuous
    )
    continuous._continuous_submission_authorization = recurring_authorization

    # Later continuous attempts overwrite the current pending/finalized
    # journal fields, but can never overwrite the separately sealed launch
    # identity and receipt used for every subsequent authorization.
    continuous_attempt = "sha256:" + "e" * 64
    continuous_identity = {
        "network": "finney",
        "netuid": 39,
        "mapping_block": 903,
        "policy_version": 2,
        "validator_hotkey": "validator-hotkey",
        "validator_uid": 30,
        "vector_id": "continuous-vector",
        "signed_vector_sha256": "sha256:" + "9" * 64,
        "burn_hotkey": "burn-hotkey",
        "uid_weights": [[163, 0.9], [204, 0.1]],
        "uid_hotkeys": [[163, "tdx-miner"], [204, "burn-hotkey"]],
        "continuous_authorization": (
            validator_thin._continuous_authorization_identity(recurring_authorization)
        ),
    }
    validator_thin._reserve_common_submission(
        continuous,
        lane="thin",
        attempt_id=continuous_attempt,
        identity=continuous_identity,
    )
    validator_thin._record_pending_broadcast_intent(
        continuous,
        attempt_id=continuous_attempt,
        extrinsic_hash="0x" + "e" * 64,
        nonce=18,
        era_reference_block=903,
        mortal_period_blocks=4,
        version_key=validator_thin._weight_version_key(),
        wire_uids=[163, 204],
        wire_weights=[65535, 7282],
    )
    validator_thin._finalize_common_submission(
        continuous,
        attempt_id=continuous_attempt,
        submission=validator_thin.ChainSubmission(
            success=True,
            extrinsic_hash="0x" + "e" * 64,
            block_hash="0x" + "f" * 64,
            block_number=903,
            finalized=True,
        ),
    )
    validator_thin._require_continuous_launch_transition(continuous)

    # The validator-owned journal alone cannot unlock continuous operation:
    # the root-signed public seal must name the exact irreversible write.
    public_release["launch_submission"]["extrinsic"]["hash"] = "0x" + "d" * 64
    with pytest.raises(validator_thin.wire.VectorError, match="does not match"):
        validator_thin._require_continuous_launch_transition(continuous)
    validator_thin._write_state_fenced(
        validator_thin._submission_state_path(continuous),
        {"submission_continuous_enabled": False},
    )
    with pytest.raises(validator_thin.wire.VectorError, match="does not match"):
        validator_thin.reconcile_launch_transition(continuous)


def test_submission_lock_identity_uses_genesis_and_ss58_not_wallet_aliases(
    tmp_path: Path,
) -> None:
    first = _args(tmp_path, "off")
    second = _args(tmp_path, "off")
    for args in (first, second):
        args.offline = False
        args.runtime_root = str(tmp_path / "runtime")
        args._submission_genesis_hash = "0x" + "1" * 64
        args._submission_validator_hotkey = "5CanonicalValidator"
    first.wallet_name = "alias-a"
    second.wallet_name = "alias-b"
    assert validator_thin._submission_lock_path(
        first
    ) == validator_thin._submission_lock_path(second)

    second._submission_validator_hotkey = "5DifferentValidator"
    assert validator_thin._submission_lock_path(
        first
    ) != validator_thin._submission_lock_path(second)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("mapping_block", "historical metagraph differs"),
        ("metagraph", "historical metagraph differs"),
        ("epoch_schedule", "epoch schedule"),
        ("commit_reveal", "commit-reveal state changed"),
        ("inclusion_block", "launch inclusion block differs"),
        ("inclusion_metagraph", "launch inclusion UID mapping differs"),
        ("inclusion_commit_reveal", "policy was not valid"),
        ("inclusion_timestamp", "policy was not valid"),
        ("absent_extrinsic", "exact launch extrinsic is absent"),
        ("extrinsic_args", "launch extrinsic call differs"),
        ("extrinsic_nonce", "nonce or mortal era contradicts"),
        ("extrinsic_era", "nonce or mortal era contradicts"),
        ("extrinsic_failure", "did not execute successfully"),
        ("chain_weights", "historical on-chain weights differ"),
        ("pending_coldkey_swap", "pending swap announcement"),
        ("genesis", "pinned Finney genesis"),
        ("rotation_call", "unique exact swap_hotkey_v2"),
        ("rotation_failure", "receipt is incomplete or failed"),
        ("rotation_event", "rotation event is absent"),
        ("rotation_lineage", "lineage differs"),
    ],
)
def test_historical_launch_rejects_archive_tampering(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionError,
        verify_historical_launch,
    )

    release, hotkeys = _historical_launch_fixture()
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    with pytest.raises(ReproductionError, match=message):
        verify_historical_launch(
            release,
            subtensor=_HistoricalSubtensor(hotkeys, mutation=mutation),
        )


@pytest.mark.parametrize("mutation", ["missing_nonce", "missing_era"])
def test_historical_launch_missing_decoded_intent_is_not_proven(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    from scripts.assert_sn39_public_reproduction import (
        ReproductionNotProven,
        verify_historical_launch,
    )

    release, hotkeys = _historical_launch_fixture()
    monkeypatch.setattr("scaffold.wire_vector.verify_signature", lambda *_a, **_k: None)
    with pytest.raises(
        ReproductionNotProven,
        match="nonce or mortal era is unavailable",
    ):
        verify_historical_launch(
            release,
            subtensor=_HistoricalSubtensor(hotkeys, mutation=mutation),
        )
