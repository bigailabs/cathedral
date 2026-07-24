"""Two-mode validator: thin default + concurrent full-provenance audit.

Unit tests stub the audit; the integration tests at the bottom build a real
content-addressed evidence store with the ``cathedral`` package (installed via
the ``provenance`` extra) and run the actual audit against it.
"""
from __future__ import annotations

import json
import time
import unittest.mock as mock
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

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

    def build_stage(source_epoch: int, positive: bool, challenge_hex: str) -> None:
        epoch_id = ledger.begin_epoch(
            source_epoch,
            policy_registry_release=snapshot.release,
            policy_registry_digest=snapshot.digest,
        )
        attestation_rows = []
        receipts = []
        work_blobs: list[tuple[str, str]] = []
        if positive:
            from cathedral.lanes.sat_types import (
                SatCertificate,
                SatInstance,
                SatWorkItem,
            )
            from cathedral.runtime import _sat_manifest_bytes, _sat_result_bytes

            sat_instance = SatInstance(n_vars=3, clauses=[[1, 2, -3]] * 20)
            sat_item = SatWorkItem(
                instance=sat_instance, seed=7, challenge_id=challenge_hex
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
            nonce = bytes([source_epoch]) * 32
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
            claims = with_verified_channel(claims, b"binding", verified_at=verified_text)
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
                manifest_digest="sha256:"
                + hashlib.sha256(work_item_bytes).hexdigest(),
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
            ledger.add_lifecycle_snapshot(epoch_id, lifecycle, snapshot_at=verified_text)
            receipts.append((receipt, envelope_digest))
            attestation_rows.append(
                {
                    "hotkey": "tdx-miner",
                    "verdict": "VERIFIED",
                    "evidence_digest": "sha256:" + evidence_digest
                    if not evidence_digest.startswith("sha256:")
                    else evidence_digest,
                    "envelope_digest": envelope_digest,
                    "challenge_digest": "sha256:"
                    + hashlib.sha256(nonce).hexdigest(),
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
                "source": "enrollment_registry",
                "finalized_block": None,
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
            wire_report_sha256=None,
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
        recent_rows.insert(0, {"source_epoch": source_epoch, "manifest": manifest_digest})
        stage_indexes[source_epoch] = index_bytes

    build_stage(11, positive=True, challenge_hex="a" * 64)
    build_stage(12, positive=False, challenge_hex="b" * 64)
    build_stage(13, positive=True, challenge_hex="c" * 64)
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




def _run_audit_replay(settings, *, state=None, vector=None, network="finney", netuid=39):
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
        )


def test_real_audit_passes_and_agrees_with_a_matching_vector(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
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
    audit = _run_audit_replay(settings, vector=vector)
    assert audit.status == "PASS", audit.error
    assert audit.assurance == "full"
    assert audit.recomputed == {"tdx-miner": 1.0}
    assert audit.agrees_with_vector is True
    assert audit.receipt_hotkeys == ["tdx-miner"]


def test_real_audit_flags_a_diverging_vector(real_evidence) -> None:
    _store_root, settings, _stages = real_evidence
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
    audit = _run_audit_replay(settings, vector=vector)
    assert audit.status == "PASS"  # the chain itself verified
    assert audit.agrees_with_vector is False
    assert any("sybil-miner" in item for item in audit.discrepancies)


def test_real_audit_fails_closed_on_tampered_report_blob(real_evidence, tmp_path) -> None:
    store_root, settings, _stages = real_evidence
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
            ProvenanceSettings(mode="shadow", evidence_url="https://user:pw@host.example")
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
        state={"provenance_index_epoch": 99, "provenance_index_manifest": "sha256:" + "f" * 64},
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
    # The manifest's exhaustive candidate accounting proves every active
    # candidate was explicitly rejected: the all-burn state is FULL and
    # authority can submit 100% burn (old positive weights cannot survive).
    assert revoked.assurance == "full"
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


def test_authority_submits_full_burn_on_proven_revocation(
    real_evidence, tmp_path, monkeypatch
) -> None:
    """Counterexample N: on a PROVEN all-rejected epoch (FULL via exhaustive
    candidate accounting) authority derives and would submit 100% configured
    burn, so prior positive weights cannot survive revocation. A merely
    unproven zero-positive audit (receipts_only) still refuses."""
    import dataclasses
    import shutil

    from cathedral.evidence import EvidenceStore

    store_root, settings, stages = real_evidence
    # Private store copy: the shared store's producer guard (correctly)
    # refuses to move latest backwards after the lifecycle test ends at 13.
    private_root = tmp_path / "store-copy"
    shutil.copytree(store_root, private_root)
    (private_root / "index.json").unlink()
    EvidenceStore(private_root).write_index(stages[12])
    settings = dataclasses.replace(settings, evidence_dir=str(private_root))
    revoked = _run_audit_replay(settings)
    assert revoked.assurance == "full" and revoked.recomputed == {}
    monkeypatch.setattr(validator_thin, "run_audit", lambda *a, **k: revoked)
    _, recomputed = validator_thin._run_provenance_stage(
        _args(tmp_path, "authority"),
        validated_supply_payload(positive=False),
        tmp_path / "state.json",
    )
    weights = validator_thin._provenance_uid_weights(
        recomputed,
        mechanism="validated_supply_v1",
        burn_hotkey="burn-hotkey",
        hotkey_to_uid={"burn-hotkey": 204},
    )
    assert weights == {204: 1.0}  # 100% configured burn submitted

    unproven = _run_audit_replay(settings)
    unproven.assurance = "receipts_only"
    unproven.recomputed = {}
    monkeypatch.setattr(validator_thin, "run_audit", lambda *a, **k: unproven)
    with pytest.raises(validator_thin.wire.VectorError, match="FULL assurance"):
        validator_thin._run_provenance_stage(
            _args(tmp_path, "authority"),
            validated_supply_payload(positive=False),
            tmp_path / "state.json",
        )
