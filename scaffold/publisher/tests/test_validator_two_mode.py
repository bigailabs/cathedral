"""Two-mode validator: thin default + concurrent full-provenance audit.

Unit tests stub the audit; the integration tests at the bottom build a real
content-addressed evidence store with the ``cathedral`` package (installed via
the ``provenance`` extra) and run the actual audit against it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scaffold import provenance_audit, validator_thin
from scaffold.provenance_audit import (
    ProvenanceAudit,
    ProvenanceAuditError,
    ProvenanceSettings,
    check_chain_state,
    run_audit,
)

from test_validator_thin_validated_supply import payload as validated_supply_payload


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
    base = dict(mechanism="validated_supply_v1", burn_hotkey="burn-hotkey")
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
    with pytest.raises(validator_thin.wire.VectorError, match="requires --provenance-burn-hotkey"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0}, mechanism="validated_supply_v1",
            burn_hotkey=None, hotkey_to_uid=mapping,
        )
    with pytest.raises(validator_thin.wire.VectorError, match="no pinned burn"):
        validator_thin._provenance_uid_weights(
            {"tdx-miner": 1.0}, mechanism="validated_supply_v99",
            burn_hotkey="burn-hotkey", hotkey_to_uid=mapping,
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


def _stub_audit(monkeypatch, audit: ProvenanceAudit) -> list[dict]:
    calls: list[dict] = []

    def fake_run_audit(settings, *, network, netuid, vector_payload, state):
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

    def slow_audit(settings, *, network, netuid, vector_payload, state):
        release.wait(10.0)
        return ProvenanceAudit(status="PASS", source_epoch=1, report_id="sha256:" + "a" * 64)

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
    _stub_audit(monkeypatch, ProvenanceAudit(status="NOT_PROVEN", error="not installed"))
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
    audit = ProvenanceAudit(status="PASS", source_epoch=10, report_id="sha256:" + "a" * 64)
    with pytest.raises(ProvenanceAuditError, match="rollback"):
        check_chain_state(
            audit,
            {"provenance_last_source_epoch": 11, "provenance_last_report_id": "x"},
        )


def test_chain_state_rejects_same_epoch_equivocation() -> None:
    audit = ProvenanceAudit(status="PASS", source_epoch=11, report_id="sha256:" + "a" * 64)
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

cathedral_provenance = pytest.importorskip(
    "cathedral.provenance", reason="provenance extra not installed"
)


@pytest.fixture(scope="module")
def real_evidence(tmp_path_factory):
    """Build a genuine registry→receipt→report→manifest→index chain."""
    import base64
    from datetime import UTC, datetime, timedelta

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from cathedral.assurance import (
        AssuranceDimension,
        ClaimStatus,
        attestation_claims,
        evaluated_claim,
        with_verified_channel,
    )
    from cathedral.common import Attested, Tier
    from cathedral.evidence import EvidenceStore, build_manifest, build_signed_index
    from cathedral.ledger import Ledger
    from cathedral.lifecycle import (
        LifecycleReason,
        LifecycleSnapshot,
        WorkerLifecycleState,
    )
    from cathedral.policy_registry import canonical_json, sign_registry
    from cathedral.receipt import ReceiptIssuer
    from cathedral.runtime import SAT_WORK_POLICY_DIGEST
    from cathedral.score_class import export_score_class_report
    from cathedral.policy_registry import verify_registry

    tmp_path = tmp_path_factory.mktemp("evidence")
    registry_seed = bytes(range(32))
    receipt_seed = bytes(range(32, 64))
    report_seed = bytes(range(64, 96))
    index_seed = bytes(range(96, 128))
    now = datetime.now(UTC).replace(microsecond=0)
    t0 = now - timedelta(hours=1)
    t1 = now + timedelta(hours=47)
    text = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731

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
                    "public_key_base64": base64.b64encode(pub_raw(receipt_seed)).decode(),
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
    claims = attestation_claims(b"raw-quote", policy, verified_at=verified_text)
    claims = with_verified_channel(claims, b"binding", verified_at=verified_text)
    claims = claims.with_claim(
        AssuranceDimension.WORK,
        evaluated_claim(
            ClaimStatus.PASSED,
            b"work-result",
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

    ledger = Ledger(tmp_path / "ledger.sqlite")
    epoch_id = ledger.begin_epoch(
        11,
        policy_registry_release=snapshot.release,
        policy_registry_digest=snapshot.digest,
    )
    challenge = "a" * 64
    receipt = ReceiptIssuer(snapshot, "receipt-test-1", receipt_seed).issue(
        epoch_id=epoch_id,
        source_epoch=11,
        subject_hotkey="tdx-miner",
        attested=attested,
        policy=policy,
        assurance=claims,
        worker_lifecycle=lifecycle,
        challenge_id=challenge,
        manifest_digest="sha256:" + "b" * 64,
        work_units=20.0,
        issued_at=now,
    )
    ledger.issue_challenge(challenge, "tdx-miner", epoch_id)
    ledger.resolve_challenge_with_receipt(
        challenge,
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
    )
    ledger.add_lifecycle_snapshot(epoch_id, lifecycle, snapshot_at=verified_text)
    ledger.complete_epoch(
        epoch_id,
        {"tdx-miner"},
        generated_at=verified_text,
        score_network="finney",
        score_netuid=39,
    )
    verifier_digest = "sha256:" + "d" * 64
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
        valid_from_block=1,
        valid_until_block=10_000_000_000,
        verifier_digest=verifier_digest,
    )
    report = json.loads(report_bytes)
    ledger.close()

    store_root = tmp_path / "store"
    store = EvidenceStore(store_root)
    registry_blob = store.put_blob(registry_bytes)
    report_blob = store.put_blob(report_bytes)
    receipt_blob = store.put_blob(receipt.receipt_bytes)
    manifest_bytes = build_manifest(
        network="finney",
        netuid=39,
        source_epoch=11,
        epoch_id=epoch_id,
        generated_at=None,
        mechanism_id="validated_supply_v1",
        mechanism_revision=1,
        source_revision="abc1234",
        registry_release=1,
        registry_digest=snapshot.digest,
        registry_blob=registry_blob,
        verifier_digest=verifier_digest,
        verifier_binary_blob=None,
        report_id=report["report_id"],
        report_blob=report_blob,
        report_signing_key_id="score-test-1",
        receipts=[
            {
                "receipt_id": receipt.receipt_id,
                "hotkey": "tdx-miner",
                "blob": receipt_blob,
            }
        ],
        attestations=[],
        wire_report_sha256=None,
    )
    manifest_digest = store.put_blob(manifest_bytes)
    store.write_index(
        build_signed_index(
            network="finney",
            netuid=39,
            latest_source_epoch=11,
            latest_manifest_digest=manifest_digest,
            recent=[],
            signing_key_id="evidence-index-test-1",
            private_key_seed=index_seed,
        )
    )

    def keyfile(name: str, mapping: dict[str, bytes]) -> str:
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {kid: base64.b64encode(raw).decode() for kid, raw in mapping.items()}
            )
        )
        return str(path)

    settings = ProvenanceSettings(
        mode="shadow",
        evidence_dir=str(store_root),
        registry_keys=keyfile("registry-keys.json", trusted),
        report_keys=keyfile("report-keys.json", {"score-test-1": pub_raw(report_seed)}),
        index_keys=keyfile(
            "index-keys.json", {"evidence-index-test-1": pub_raw(index_seed)}
        ),
        verifier_digest=verifier_digest,
    )
    return store_root, settings


def test_real_audit_passes_and_agrees_with_a_matching_vector(real_evidence) -> None:
    _store_root, settings = real_evidence
    vector = {
        "weights": [
            {
                "miner_hotkey": "tdx-miner",
                "weight": 1.0,
                "base_component": 0.0,
                "external_component": 1.0,
            }
        ]
    }
    audit = run_audit(
        settings, network="finney", netuid=39, vector_payload=vector, state={}
    )
    assert audit.status == "PASS", audit.error
    assert audit.recomputed == {"tdx-miner": 1.0}
    assert audit.agrees_with_vector is True
    assert audit.receipt_hotkeys == ["tdx-miner"]


def test_real_audit_flags_a_diverging_vector(real_evidence) -> None:
    _store_root, settings = real_evidence
    vector = {
        "weights": [
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
    }
    audit = run_audit(
        settings, network="finney", netuid=39, vector_payload=vector, state={}
    )
    assert audit.status == "PASS"  # the chain itself verified
    assert audit.agrees_with_vector is False
    assert any("sybil-miner" in item for item in audit.discrepancies)


def test_real_audit_fails_closed_on_tampered_report_blob(real_evidence, tmp_path) -> None:
    store_root, settings = real_evidence
    manifest_digest = json.loads((store_root / "index.json").read_text())["latest"][
        "manifest"
    ]
    manifest = json.loads(
        (store_root / "blobs" / "sha256" / manifest_digest.split(":", 1)[1]).read_bytes()
    )
    report_blob = manifest["score_report"]["blob"]
    blob_path = store_root / "blobs" / "sha256" / report_blob.split(":", 1)[1]
    original = blob_path.read_bytes()
    try:
        blob_path.write_bytes(original.replace(b"20", b"99"))
        audit = run_audit(
            settings, network="finney", netuid=39, vector_payload=None, state={}
        )
        assert audit.status == "FAIL"
        assert audit.error
    finally:
        blob_path.write_bytes(original)


def test_real_audit_rejects_wrong_network_pin(real_evidence) -> None:
    _store_root, settings = real_evidence
    audit = run_audit(
        settings, network="testnet", netuid=292, vector_payload=None, state={}
    )
    assert audit.status == "FAIL"
