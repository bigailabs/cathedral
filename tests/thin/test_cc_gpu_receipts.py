from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import os
import platform
from pathlib import Path
import struct
import subprocess
import time
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from cathedral_thin import cc_gpu_loader as cc_gpu_loader_module

from cathedral_thin.cc_gpu_receipts import (
    CC_GPU_CPU_TEE,
    CC_GPU_MACHINE_TYPE,
    CC_GPU_MODEL,
    CC_GPU_PROFILE_ID,
    CC_GPU_PROVIDER,
    CC_GPU_RECEIPT_SCHEMA,
    CC_GPU_ZONE,
    CcGpuEvidenceVerification,
    CcGpuReceiptPolicy,
    CcGpuTrustedSigningKey,
    VerifiedCcGpuReceipt,
    cc_gpu_score_report_body,
    job_context_digest_for,
    merge_cc_gpu_replay_claims,
    receipt_id_for,
    sha256_digest,
    verify_cc_gpu_receipt,
    verify_cc_gpu_receipt_batch,
)
from cathedral_thin.cc_gpu_acceptance import main as acceptance_main
from cathedral_thin.cc_gpu_loader import (
    CC_GPU_EVIDENCE_EXPORT_SCHEMA,
    CC_GPU_LOADER_CONFIG_SCHEMA,
    CcGpuReceiptLoader,
    subprocess_cc_gpu_evidence_verifier,
    verify_cc_gpu_evidence_export,
)
from cathedral_thin.core import (
    CC_GPU_REPLAY_RETENTION_SAFETY_SECONDS,
    MAX_CC_GPU_FUTURE_SKEW_SECONDS,
    MAX_CC_GPU_ID_REPLAY_CLAIMS,
    MAX_CC_GPU_RECEIPT_AGE_SECONDS,
    ThinSubnetError,
    empty_cc_gpu_replay_claims,
)
from cathedral_thin.score_classes import (
    canonical_json,
    external_class_decision,
    load_score_policy,
    sign_report,
    verify_report,
)


NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
ISSUED = "2026-07-21T11:59:59.000000Z"
REGISTRY_VALID_FROM = datetime(2026, 7, 21, 0, 0, 0, tzinfo=UTC)
REGISTRY_VALID_UNTIL = datetime(2026, 7, 22, 0, 0, 0, tzinfo=UTC)
RECEIPT_KEY = Ed25519PrivateKey.generate()
RECEIPT_PUBLIC = RECEIPT_KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)
REGISTRY_DIGEST = sha256_digest(b"cc-gpu-policy-registry-7")
PROFILE_DIGEST = sha256_digest(b"gcp-a3-high-h100-tdx-v1")
PROFILE_AUTHORITY = (
    f"gpu-profile:{CC_GPU_PROFILE_ID}@profile={PROFILE_DIGEST}"
    f"@release=7@registry={REGISTRY_DIGEST}"
)
POLICY_DIGEST = sha256_digest(b"cc-gpu-policy")
IMAGE_DIGEST = sha256_digest(b"cc-gpu-image")
MODEL_DIGEST = sha256_digest(b"cc-gpu-model")
VERIFIER_DIGEST = sha256_digest(b"cc-gpu-composite-verifier")
CPU_MEASUREMENT = sha256_digest(b"intel-tdx-measurement")
GPU_MEASUREMENT = sha256_digest(b"nvidia-h100-measurement")


def policy() -> CcGpuReceiptPolicy:
    return CcGpuReceiptPolicy(
        expected_profile_id=CC_GPU_PROFILE_ID,
        allowed_profile_authorities=frozenset({PROFILE_AUTHORITY}),
        allowed_policy_digests=frozenset({POLICY_DIGEST}),
        allowed_image_digests=frozenset({IMAGE_DIGEST}),
        allowed_model_digests=frozenset({MODEL_DIGEST}),
        policy_registry_release=7,
        policy_registry_digest=REGISTRY_DIGEST,
        policy_registry_valid_from=REGISTRY_VALID_FROM,
        policy_registry_valid_until=REGISTRY_VALID_UNTIL,
        trusted_signing_keys={
            "cc-gpu-receipt-1": CcGpuTrustedSigningKey(
                RECEIPT_PUBLIC, REGISTRY_VALID_FROM, REGISTRY_VALID_UNTIL
            )
        },
        allowed_verifier_digests=frozenset({VERIFIER_DIGEST}),
    )


def _uuid(value: int) -> str:
    return str(uuid.UUID(int=value, version=4))


def _evidence(label: str, seed: int) -> bytes:
    return f"{label}:{seed}".encode()


def _sign(document: dict) -> bytes:
    signed = dict(document)
    signed.pop("receipt_id", None)
    signed.pop("signature", None)
    signed["receipt_id"] = receipt_id_for(signed)
    signed["signature"] = {
        "algorithm": "ed25519",
        "value_base64": base64.b64encode(
            RECEIPT_KEY.sign(
                canonical_json({key: value for key, value in signed.items()})
            )
        ).decode("ascii"),
    }
    return canonical_json(signed)


def receipt(
    seed: int = 1, *, hotkey: str = "miner-a"
) -> tuple[bytes, dict[str, bytes]]:
    evidence = {
        label: _evidence(label, seed)
        for label in (
            "admission-bundle",
            "admission-cpu",
            "admission-gpu",
            "completion-bundle",
            "completion-cpu",
            "completion-gpu",
            "gpu-identity-set",
            "secret-release-grant",
            "provider-deletion",
        )
    }
    evidence_map = {sha256_digest(value): value for value in evidence.values()}
    document = {
        "schema": CC_GPU_RECEIPT_SCHEMA,
        "execution_class": "cc_gpu",
        "profile_id": CC_GPU_PROFILE_ID,
        "provider": CC_GPU_PROVIDER,
        "machine_type": CC_GPU_MACHINE_TYPE,
        "zone": CC_GPU_ZONE,
        "cpu_tee": CC_GPU_CPU_TEE,
        "gpu_model": CC_GPU_MODEL,
        "gpu_count": 1,
        "provisioning_model": "spot",
        "worker_id": _uuid(seed * 10 + 1),
        "job_id": _uuid(seed * 10 + 2),
        "attempt_id": _uuid(seed * 10 + 3),
        "subject_hotkey": hotkey,
        "profile_authority": PROFILE_AUTHORITY,
        "job_context_digest": sha256_digest(b"placeholder"),
        "admission_bundle_digest": sha256_digest(evidence["admission-bundle"]),
        "admission_nonce_digest": sha256_digest(_evidence("admission-nonce", seed)),
        "admission_cpu_evidence_digest": sha256_digest(evidence["admission-cpu"]),
        "admission_gpu_evidence_digest": sha256_digest(evidence["admission-gpu"]),
        "admission_gpu_identity_set_digest": sha256_digest(
            evidence["gpu-identity-set"]
        ),
        "completion_bundle_digest": sha256_digest(evidence["completion-bundle"]),
        "completion_nonce_digest": sha256_digest(_evidence("completion-nonce", seed)),
        "completion_cpu_evidence_digest": sha256_digest(evidence["completion-cpu"]),
        "completion_gpu_evidence_digest": sha256_digest(evidence["completion-gpu"]),
        "completion_gpu_identity_set_digest": sha256_digest(
            evidence["gpu-identity-set"]
        ),
        "channel_binding_digest": sha256_digest(_evidence("channel", seed)),
        "image_digest": IMAGE_DIGEST,
        "policy_digest": POLICY_DIGEST,
        "input_digest": sha256_digest(_evidence("input", seed)),
        "model_digest": MODEL_DIGEST,
        "result_digest": sha256_digest(_evidence("result", seed)),
        "artifact_manifest_digest": sha256_digest(_evidence("artifacts", seed)),
        "secret_release_grant_digest": sha256_digest(evidence["secret-release-grant"]),
        "outcome": "completed",
        "deletion_confirmed": True,
        "deletion_evidence_digest": sha256_digest(evidence["provider-deletion"]),
        "policy_registry_release": 7,
        "policy_registry_digest": REGISTRY_DIGEST,
        "issued_at": ISSUED,
        "signing_key_id": "cc-gpu-receipt-1",
    }
    document["job_context_digest"] = job_context_digest_for(document)
    return _sign(document), evidence_map


def resign(raw: bytes, **changes) -> bytes:
    import json

    document = json.loads(raw)
    document.update(changes)
    return _sign(document)


def verification(phase, document, **changes):
    values = {
        "ok": True,
        "verifier_digest": VERIFIER_DIGEST,
        "cpu_measurement_digest": CPU_MEASUREMENT,
        "gpu_measurement_digest": GPU_MEASUREMENT,
        "cpu_nonce_digest": document[f"{phase}_nonce_digest"],
        "gpu_nonce_digest": document[f"{phase}_nonce_digest"],
        "job_context_digest": document["job_context_digest"],
        "subject_hotkey": document["subject_hotkey"],
        "channel_binding_digest": document["channel_binding_digest"],
        "gpu_identity_set_digest": document[f"{phase}_gpu_identity_set_digest"],
        "same_guest": True,
        "gpu_cc_mode_enabled": True,
        "gpu_ready_state": True,
        "measurement_policy_ok": True,
        "runtime_isolation_ok": True,
        "secret_release_grant_digest": document["secret_release_grant_digest"],
        "secret_release_signature_verified": True,
        "secret_release_semantics_verified": True,
        "deletion_evidence_digest": document["deletion_evidence_digest"],
        "deletion_signature_verified": True,
        "deletion_semantics_verified": True,
        "provider_absent": True,
        "reason": "verified",
    }
    values.update(changes)
    return CcGpuEvidenceVerification(**values)


def verifier(
    phase,
    bundle,
    cpu,
    gpu,
    identity_set,
    secret_grant,
    deletion,
    document,
    *,
    deadline_monotonic=None,
):
    del deadline_monotonic
    assert phase in {"admission", "completion"}
    assert bundle and cpu and gpu and identity_set and secret_grant and deletion
    assert document["execution_class"] == "cc_gpu"
    return verification(phase, document)


EXPORT_KINDS = {
    "admission_bundle": "admission_bundle_digest",
    "admission_cpu_evidence": "admission_cpu_evidence_digest",
    "admission_gpu_evidence": "admission_gpu_evidence_digest",
    "admission_gpu_identity_set": "admission_gpu_identity_set_digest",
    "completion_bundle": "completion_bundle_digest",
    "completion_cpu_evidence": "completion_cpu_evidence_digest",
    "completion_gpu_evidence": "completion_gpu_evidence_digest",
    "completion_gpu_identity_set": "completion_gpu_identity_set_digest",
    "secret_release_grant": "secret_release_grant_digest",
    "deletion_evidence": "deletion_evidence_digest",
}


def evidence_export(raw_receipt: bytes, evidence: dict[str, bytes]) -> bytes:
    receipt_document = json.loads(raw_receipt)
    kinds_by_digest: dict[str, list[str]] = {}
    for kind, field in EXPORT_KINDS.items():
        kinds_by_digest.setdefault(receipt_document[field], []).append(kind)
    artifacts = {
        digest: {
            "encoding": "base64",
            "data_base64": base64.b64encode(evidence[digest]).decode("ascii"),
            "byte_length": len(evidence[digest]),
            "kinds": sorted(kinds),
        }
        for digest, kinds in sorted(kinds_by_digest.items())
    }
    return canonical_json(
        {
            "schema": CC_GPU_EVIDENCE_EXPORT_SCHEMA,
            "receipt_id": receipt_document["receipt_id"],
            "receipt": receipt_document,
            "artifacts": artifacts,
            "authentication": {"owner_scoped": True},
            "integrity": {
                "receipt_signature": "ed25519",
                "artifact_digest": "sha256",
            },
        }
    )


def test_flat_core_receipt_round_trip_requires_independent_evidence() -> None:
    raw, evidence = receipt()
    verified = verify_cc_gpu_receipt(
        raw, policy(), now=NOW, evidence_by_digest=evidence, evidence_verifier=verifier
    )
    assert verified.subject_hotkey == "miner-a"
    assert verified.document["profile_id"] == CC_GPU_PROFILE_ID
    assert verified.document["gpu_model"] == "nvidia_h100_80gb"
    assert verified.verifier_digest == VERIFIER_DIGEST

    missing = dict(evidence)
    missing.pop(verified.document["completion_gpu_evidence_digest"])
    with pytest.raises(ThinSubnetError, match="missing completion GPU evidence"):
        verify_cc_gpu_receipt(
            raw,
            policy(),
            now=NOW,
            evidence_by_digest=missing,
            evidence_verifier=verifier,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"execution_class": "hybrid_gpu_preview"}, "hybrid or CPU"),
        ({"outcome": "failed"}, "completed"),
        ({"deletion_confirmed": False}, "confirmed deletion"),
        ({"provider": "other"}, "profile is not allowed"),
        ({"provisioning_model": "flex_start"}, "profile is not allowed"),
        ({"gpu_model": "nvidia_a100"}, "profile is not allowed"),
        ({"gpu_count": True}, "profile is not allowed"),
        ({"policy_registry_release": 8}, "policy registry"),
        ({"policy_digest": sha256_digest(b"other-policy")}, "policy digest"),
        ({"subject_hotkey": " miner-a"}, "subject hotkey"),
    ],
)
def test_class_outcome_hardware_and_policy_confusion_fail_closed(
    changes, message
) -> None:
    raw, evidence = receipt()
    with pytest.raises(ThinSubnetError, match=message):
        verify_cc_gpu_receipt(
            resign(raw, **changes),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )


def test_unknown_stale_mismatched_and_reused_evidence_fail_closed() -> None:
    import json

    raw, evidence = receipt()
    unknown = json.loads(raw)
    unknown["trusted"] = True
    with pytest.raises(ThinSubnetError, match="unknown fields"):
        verify_cc_gpu_receipt(
            _sign(unknown),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )
    with pytest.raises(ThinSubnetError, match="stale"):
        verify_cc_gpu_receipt(
            raw,
            policy(),
            now=NOW + timedelta(seconds=300),
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )
    with pytest.raises(ThinSubnetError, match="context digest"):
        verify_cc_gpu_receipt(
            resign(raw, job_id=_uuid(999)),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )
    document = json.loads(raw)
    document["completion_gpu_identity_set_digest"] = sha256_digest(b"different-gpu")
    with pytest.raises(ThinSubnetError, match="GPU identities"):
        verify_cc_gpu_receipt(
            _sign(document),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )
    document = json.loads(raw)
    document["completion_gpu_evidence_digest"] = document[
        "admission_gpu_evidence_digest"
    ]
    with pytest.raises(ThinSubnetError, match="evidence was reused"):
        verify_cc_gpu_receipt(
            _sign(document),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )
    document = json.loads(raw)
    document["completion_nonce_digest"] = document["admission_nonce_digest"]
    with pytest.raises(ThinSubnetError, match="nonce was reused"):
        verify_cc_gpu_receipt(
            _sign(document),
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )


def test_cpu_and_gpu_evidence_must_bind_the_matching_phase_nonce() -> None:
    raw, evidence = receipt()

    def mismatched_nonce(
        phase,
        bundle,
        cpu,
        gpu,
        identity_set,
        secret_grant,
        deletion,
        document,
        **_kwargs,
    ):
        del bundle, cpu, gpu, identity_set, secret_grant, deletion
        return verification(
            phase,
            document,
            gpu_nonce_digest=sha256_digest(b"wrong-nonce"),
        )

    with pytest.raises(ThinSubnetError, match="evidence nonce is mismatched"):
        verify_cc_gpu_receipt(
            raw,
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=mismatched_nonce,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"job_context_digest": sha256_digest(b"wrong-context")}, "mismatched job"),
        ({"subject_hotkey": "other-miner"}, "mismatched job"),
        ({"channel_binding_digest": sha256_digest(b"wrong-channel")}, "mismatched job"),
        (
            {"gpu_identity_set_digest": sha256_digest(b"wrong-gpu-identity")},
            "mismatched job",
        ),
        ({"same_guest": False}, "did not prove isolation"),
        ({"gpu_cc_mode_enabled": False}, "did not prove isolation"),
        ({"gpu_ready_state": False}, "did not prove isolation"),
        ({"measurement_policy_ok": False}, "did not prove isolation"),
        ({"runtime_isolation_ok": False}, "did not prove isolation"),
        ({"secret_release_signature_verified": False}, "did not prove isolation"),
        ({"secret_release_semantics_verified": False}, "did not prove isolation"),
        ({"deletion_signature_verified": False}, "did not prove isolation"),
        ({"deletion_semantics_verified": False}, "did not prove isolation"),
        ({"provider_absent": False}, "did not prove isolation"),
    ],
)
def test_independent_verifier_must_prove_full_binding_and_auxiliary_evidence(
    changes, message
) -> None:
    raw, evidence = receipt()

    def incomplete_verifier(phase, *_args, **_kwargs):
        return verification(phase, _args[-1], **changes)

    with pytest.raises(ThinSubnetError, match=message):
        verify_cc_gpu_receipt(
            raw,
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=incomplete_verifier,
        )


def test_verifier_failure_and_signing_key_window_fail_closed() -> None:
    raw, evidence = receipt()

    def failed_verifier(phase, *_args, **_kwargs):
        return verification(phase, _args[-1], ok=False, reason="bad quote")

    with pytest.raises(ThinSubnetError, match="verification failed"):
        verify_cc_gpu_receipt(
            raw,
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=failed_verifier,
        )
    expired = replace(
        policy(),
        trusted_signing_keys={
            "cc-gpu-receipt-1": CcGpuTrustedSigningKey(
                RECEIPT_PUBLIC,
                datetime(2026, 7, 20, tzinfo=UTC),
                datetime(2026, 7, 21, 1, tzinfo=UTC),
            )
        },
    )
    with pytest.raises(ThinSubnetError, match="not active"):
        verify_cc_gpu_receipt(
            raw,
            expired,
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )


def test_batch_uniqueness_is_global_across_miner_hotkeys() -> None:
    first, first_evidence = receipt(1, hotkey="miner-a")
    second, second_evidence = receipt(2, hotkey="miner-b")
    evidence = {**first_evidence, **second_evidence}
    verified = verify_cc_gpu_receipt_batch(
        [first, second],
        policy(),
        now=NOW,
        evidence_by_digest=evidence,
        evidence_verifier=verifier,
    )
    assert {item.subject_hotkey for item in verified} == {"miner-a", "miner-b"}

    import json

    first_document = json.loads(first)
    second_document = json.loads(second)
    second_document["job_id"] = first_document["job_id"]
    second_document["job_context_digest"] = job_context_digest_for(second_document)
    with pytest.raises(ThinSubnetError, match="worker or job"):
        verify_cc_gpu_receipt_batch(
            [first, _sign(second_document)],
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )

    second_document = json.loads(second)
    second_document["admission_bundle_digest"] = first_document[
        "admission_bundle_digest"
    ]
    with pytest.raises(ThinSubnetError, match="evidence across miners"):
        verify_cc_gpu_receipt_batch(
            [first, _sign(second_document)],
            policy(),
            now=NOW,
            evidence_by_digest=evidence,
            evidence_verifier=verifier,
        )

    for replay_field in (
        "admission_nonce_digest",
        "completion_nonce_digest",
        "secret_release_grant_digest",
        "deletion_evidence_digest",
    ):
        second_document = json.loads(second)
        second_document[replay_field] = first_document[replay_field]
        with pytest.raises(ThinSubnetError, match="evidence across miners"):
            verify_cc_gpu_receipt_batch(
                [first, _sign(second_document)],
                policy(),
                now=NOW,
                evidence_by_digest=evidence,
                evidence_verifier=verifier,
            )


def test_replay_claims_expire_safely_and_roll_over_capacity() -> None:
    raw, evidence = receipt()
    verified = verify_cc_gpu_receipt(
        raw,
        policy(),
        now=NOW,
        evidence_by_digest=evidence,
        evidence_verifier=verifier,
    )
    full = empty_cc_gpu_replay_claims()
    expired_at = int((NOW - timedelta(seconds=1)).timestamp() * 1000)
    full["receipt_ids"] = {
        f"cc-gpu-receipt-sha256:{index:064x}": expired_at
        for index in range(MAX_CC_GPU_ID_REPLAY_CLAIMS)
    }
    merged, watermark = merge_cc_gpu_replay_claims(
        full, [verified], now=NOW, prior_watermark_ms=expired_at
    )
    assert merged["receipt_ids"] == {verified.receipt_id: verified.replay_expires_at_ms}
    assert watermark == int(NOW.timestamp() * 1000)

    with pytest.raises(ThinSubnetError, match="durable watermark"):
        merge_cc_gpu_replay_claims(
            merged,
            [replace(verified, receipt_id="cc-gpu-receipt-sha256:" + "ff" * 32)],
            now=NOW - timedelta(seconds=1),
            prior_watermark_ms=watermark,
        )


def test_replay_claim_retention_does_not_shrink_with_active_policy() -> None:
    raw, evidence = receipt()
    short_policy = replace(policy(), max_age_seconds=60, max_future_seconds=1)
    verified = verify_cc_gpu_receipt(
        raw,
        short_policy,
        now=NOW,
        evidence_by_digest=evidence,
        evidence_verifier=verifier,
    )
    protocol_expiry_ms = int(
        (
            verified.issued_at
            + timedelta(
                seconds=(
                    MAX_CC_GPU_RECEIPT_AGE_SECONDS
                    + MAX_CC_GPU_FUTURE_SKEW_SECONDS
                    + CC_GPU_REPLAY_RETENTION_SAFETY_SECONDS
                )
            )
        ).timestamp()
        * 1000
    )
    assert verified.replay_expires_at_ms == protocol_expiry_ms

    ledger, watermark = merge_cc_gpu_replay_claims(
        empty_cc_gpu_replay_claims(), [verified], now=NOW
    )
    after_short_policy_expiry = NOW + timedelta(minutes=2)
    with pytest.raises(ThinSubnetError, match="replayed CC GPU"):
        merge_cc_gpu_replay_claims(
            ledger,
            [verified],
            now=after_short_policy_expiry,
            prior_watermark_ms=watermark,
        )


def test_polaris_evidence_export_is_bounded_digest_keyed_and_duplicate_safe(
    tmp_path,
) -> None:
    raw, evidence = receipt()
    export = evidence_export(raw, evidence)
    verified = verify_cc_gpu_evidence_export(export, policy(), verifier, now=NOW)
    assert verified.receipt_id.startswith("cc-gpu-receipt-sha256:")

    export_path = tmp_path / "acceptance.json"
    export_path.write_bytes(export)
    loader = CcGpuReceiptLoader(
        receipt_policy=policy(),
        evidence_verifier=verifier,
        local_export_directory=tmp_path,
    )
    assert loader.load_paths([export_path], now=NOW) == (verified,)
    with pytest.raises(ThinSubnetError, match="duplicate CC GPU receipt id"):
        loader.load_paths([export_path, export_path], now=NOW)

    document = json.loads(export)
    first_digest = sorted(document["artifacts"])[0]
    document["artifacts"][first_digest]["data_base64"] = base64.b64encode(
        b"hash-mismatch"
    ).decode("ascii")
    document["artifacts"][first_digest]["byte_length"] = len(b"hash-mismatch")
    with pytest.raises(ThinSubnetError, match="length or digest"):
        verify_cc_gpu_evidence_export(
            canonical_json(document), policy(), verifier, now=NOW
        )

    document = json.loads(export)
    document["authentication"] = {"owner_scoped": False}
    with pytest.raises(ThinSubnetError, match="owner-scoped authentication"):
        verify_cc_gpu_evidence_export(
            canonical_json(document), policy(), verifier, now=NOW
        )


def _write_fixture_verifier(path: Path) -> str:
    machine = 62 if platform.machine().lower() in {"x86_64", "amd64"} else 183
    header = bytearray(64)
    header[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HHI", header, 16, 2, machine, 1)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<HHH", header, 52, 64, 56, 1)
    program = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, 120, 120, 4096)
    path.write_bytes(bytes(header) + program)
    path.chmod(0o700)
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _install_successful_static_verifier(monkeypatch) -> None:
    monkeypatch.setattr(cc_gpu_loader_module.sys, "platform", "linux")

    class FakeProcess:
        pid = 4242

        def __init__(self, argv, **kwargs):
            descriptor = kwargs["pass_fds"][0]
            assert argv[0] == f"/proc/self/fd/{descriptor}"
            assert os.pread(descriptor, 4, 0) == b"\x7fELF"
            assert kwargs["env"] == {
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin",
                "TZ": "UTC",
            }
            assert kwargs["start_new_session"] is True
            phase = argv[argv.index("--phase") + 1]
            receipt_path = Path(argv[argv.index("--receipt") + 1])
            grant_path = Path(argv[argv.index("--secret-release-grant") + 1])
            result_path = Path(argv[argv.index("--result") + 1])
            document = json.loads(receipt_path.read_bytes())
            grant_digest = (
                "sha256:" + hashlib.sha256(grant_path.read_bytes()).hexdigest()
            )
            result_path.write_bytes(
                canonical_json(
                    {
                        "ok": True,
                        "cpu_measurement_digest": CPU_MEASUREMENT,
                        "gpu_measurement_digest": GPU_MEASUREMENT,
                        "cpu_nonce_digest": document[f"{phase}_nonce_digest"],
                        "gpu_nonce_digest": document[f"{phase}_nonce_digest"],
                        "job_context_digest": document["job_context_digest"],
                        "subject_hotkey": document["subject_hotkey"],
                        "channel_binding_digest": document["channel_binding_digest"],
                        "gpu_identity_set_digest": document[
                            f"{phase}_gpu_identity_set_digest"
                        ],
                        "same_guest": True,
                        "gpu_cc_mode_enabled": True,
                        "gpu_ready_state": True,
                        "measurement_policy_ok": True,
                        "runtime_isolation_ok": True,
                        "secret_release_grant_digest": grant_digest,
                        "secret_release_signature_verified": True,
                        "secret_release_semantics_verified": True,
                        "deletion_evidence_digest": document[
                            "deletion_evidence_digest"
                        ],
                        "deletion_signature_verified": True,
                        "deletion_semantics_verified": True,
                        "provider_absent": True,
                        "reason": "test fixture verified",
                    }
                )
            )

        def wait(self, timeout=None):
            assert timeout is not None and timeout > 0
            return 0

    monkeypatch.setattr(cc_gpu_loader_module.subprocess, "Popen", FakeProcess)


def _loader_config(tmp_path: Path, verifier_path: Path, verifier_digest: str) -> bytes:
    return canonical_json(
        {
            "schema": CC_GPU_LOADER_CONFIG_SCHEMA,
            "local_export_directory": str(tmp_path),
            "allowed_https_origins": [],
            "authorization_bearer_env": None,
            "verifier_command": [
                str(verifier_path),
                "verify-receipt",
                "--phase",
                "{phase}",
                "--bundle",
                "{bundle_path}",
                "--cpu-evidence",
                "{cpu_evidence_path}",
                "--gpu-evidence",
                "{gpu_evidence_path}",
                "--gpu-identity-set",
                "{gpu_identity_set_path}",
                "--secret-release-grant",
                "{secret_release_grant_path}",
                "--deletion-evidence",
                "{deletion_evidence_path}",
                "--receipt",
                "{receipt_path}",
                "--result",
                "{result_path}",
            ],
            "verifier_executable_digest": verifier_digest,
            "verifier_timeout_seconds": 10,
            "batch_deadline_seconds": 120,
            "receipt_policy": {
                "expected_profile_id": CC_GPU_PROFILE_ID,
                "allowed_profile_authorities": [PROFILE_AUTHORITY],
                "allowed_policy_digests": [POLICY_DIGEST],
                "allowed_image_digests": [IMAGE_DIGEST],
                "allowed_model_digests": [MODEL_DIGEST],
                "policy_registry_release": 7,
                "policy_registry_digest": REGISTRY_DIGEST,
                "policy_registry_valid_from": "2026-07-21T00:00:00.000000Z",
                "policy_registry_valid_until": "2026-07-23T00:00:00.000000Z",
                "trusted_signing_keys": {
                    "cc-gpu-receipt-1": {
                        "public_key_base64": base64.b64encode(RECEIPT_PUBLIC).decode(
                            "ascii"
                        ),
                        "valid_from": "2026-07-21T00:00:00.000000Z",
                        "valid_until": "2026-07-23T00:00:00.000000Z",
                    }
                },
                "allowed_verifier_digests": [verifier_digest],
                "max_age_seconds": 172800,
                "max_future_seconds": 5,
            },
        }
    )


def test_remote_export_urls_and_verifier_timeouts_are_bounded(
    tmp_path, monkeypatch
) -> None:
    loader = CcGpuReceiptLoader(
        receipt_policy=policy(),
        evidence_verifier=verifier,
        allowed_https_origins=("https://polaris.example",),
        bearer_token="owner-scoped-token",
    )
    for location in (
        "https://user:secret@polaris.example/v1/receipts/id/evidence",
        "https://polaris.example/v1/receipts/id/evidence?token=secret",
        "https://polaris.example/v1/receipts/id/evidence#fragment",
    ):
        with pytest.raises(ThinSubnetError, match="credential-free HTTPS"):
            loader._fetch_remote(location, deadline_monotonic=time.monotonic() + 10)

    verifier_path = tmp_path / "fixture-cc-gpu-verifier"
    verifier_digest = _write_fixture_verifier(verifier_path)
    command = json.loads(_loader_config(tmp_path, verifier_path, verifier_digest))[
        "verifier_command"
    ]
    with pytest.raises(ThinSubnetError, match="timeout must be 1..300"):
        subprocess_cc_gpu_evidence_verifier(
            command,
            expected_verifier_digest=verifier_digest,
            timeout_seconds=0,
        )

    script_path = tmp_path / "script-verifier"
    script_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script_path.chmod(0o700)
    script_digest = "sha256:" + hashlib.sha256(script_path.read_bytes()).hexdigest()
    with pytest.raises(ThinSubnetError, match="64-bit little-endian ELF"):
        subprocess_cc_gpu_evidence_verifier(
            [str(script_path), *command[1:]],
            expected_verifier_digest=script_digest,
        )

    dynamic_path = tmp_path / "dynamic-elf-verifier"
    dynamic = bytearray(verifier_path.read_bytes())
    struct.pack_into("<I", dynamic, 64, 2)
    dynamic_path.write_bytes(dynamic)
    dynamic_path.chmod(0o700)
    dynamic_digest = "sha256:" + hashlib.sha256(dynamic).hexdigest()
    with pytest.raises(ThinSubnetError, match="no interpreter or dynamic segment"):
        subprocess_cc_gpu_evidence_verifier(
            [str(dynamic_path), *command[1:]],
            expected_verifier_digest=dynamic_digest,
        )

    monkeypatch.setattr(cc_gpu_loader_module.sys, "platform", "linux")
    sleeper = subprocess_cc_gpu_evidence_verifier(
        command,
        expected_verifier_digest=verifier_digest,
        timeout_seconds=1,
    )
    with pytest.raises(ThinSubnetError, match="batch deadline exceeded"):
        sleeper(
            "admission",
            b"b",
            b"c",
            b"g",
            b"i",
            b"s",
            b"d",
            {},
            deadline_monotonic=time.monotonic() - 1,
        )

    killed = []

    class TimedOutProcess:
        pid = 5252

        def __init__(self, _argv, **_kwargs):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("static-verifier", timeout)
            return -9

    monkeypatch.setattr(cc_gpu_loader_module.subprocess, "Popen", TimedOutProcess)
    monkeypatch.setattr(
        cc_gpu_loader_module.os,
        "killpg",
        lambda process_id, sig: killed.append((process_id, sig)),
    )
    with pytest.raises(ThinSubnetError, match="execution timed out"):
        sleeper(
            "admission",
            b"b",
            b"c",
            b"g",
            b"i",
            b"s",
            b"d",
            {},
            deadline_monotonic=time.monotonic() + 10,
        )
    assert killed == [(TimedOutProcess.pid, cc_gpu_loader_module.signal.SIGKILL)]


def test_loader_caps_receipt_count_and_aggregate_bytes(tmp_path, monkeypatch) -> None:
    loader = CcGpuReceiptLoader(
        receipt_policy=policy(),
        evidence_verifier=verifier,
        local_export_directory=tmp_path,
    )
    with pytest.raises(ThinSubnetError, match="1..128 exports"):
        loader.load_paths([tmp_path / "unused"] * 129, now=NOW)

    export_path = tmp_path / "oversized-batch.json"
    export_path.write_bytes(b"{}")
    monkeypatch.setattr(cc_gpu_loader_module, "MAX_CC_GPU_EXPORT_BYTES_PER_REPORT", 1)
    with pytest.raises(ThinSubnetError, match="aggregate size limit"):
        loader.load_paths([export_path], now=NOW)


def test_offline_acceptance_command_accepts_once_and_rejects_duplicate(
    tmp_path, capsys, monkeypatch
) -> None:
    _install_successful_static_verifier(monkeypatch)
    raw, evidence = receipt()
    export_path = tmp_path / "polaris-export.json"
    export_path.write_bytes(evidence_export(raw, evidence))
    verifier_path = tmp_path / "fixture-cc-gpu-verifier"
    verifier_digest = _write_fixture_verifier(verifier_path)
    config_path = tmp_path / "loader.json"
    config_path.write_bytes(_loader_config(tmp_path, verifier_path, verifier_digest))

    assert (
        acceptance_main(
            ["--loader-config", str(config_path), "--export", str(export_path)]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "PASS"
    assert accepted["count"] == 1
    assert accepted["launch_status"] == "NOT PROVEN"

    assert (
        acceptance_main(
            [
                "--loader-config",
                str(config_path),
                "--export",
                str(export_path),
                "--export",
                str(export_path),
            ]
        )
        == 1
    )
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "FAIL"
    assert "duplicate CC GPU receipt id" in rejected["error"]


def _score_policy(tmp_path, public_key: str):
    document = {
        "schema": "cathedral_score_policy_v1",
        "network": "finney",
        "netuid": 39,
        "classes": [
            {
                "allocation": "1",
                "assignment": {
                    "mode": "metric",
                    "metric": "verified_cc_gpu_jobs",
                    "transform": "linear",
                    "cap": "100",
                    "required_reason_codes": sorted(
                        [
                            "cc_gpu_admission_verified",
                            "cc_gpu_completion_verified",
                            "confirmed_deletion",
                            "receipt_signature_verified",
                        ]
                    ),
                    "required_evidence_kinds": [CC_GPU_RECEIPT_SCHEMA],
                },
                "class_id": "confidential_gpu_jobs",
                "kind": "external",
                "locations": [str(tmp_path / "report.json")],
                "max_age_seconds": 600,
                "max_block_span": 100,
                "max_future_seconds": 30,
                "require_evidence": True,
                "source_id": "cathedralconfidential",
                "trusted_keys": {"score-key-1": public_key},
            }
        ],
    }
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_json(document))
    return load_score_policy(path, network="finney", netuid=39)


def test_score_decision_rederives_jobs_from_validator_verified_receipts(
    tmp_path,
) -> None:
    raw, evidence = receipt()
    verified = verify_cc_gpu_receipt(
        raw, policy(), now=NOW, evidence_by_digest=evidence, evidence_verifier=verifier
    )
    score_key = Ed25519PrivateKey.generate()
    score_public = base64.b64encode(
        score_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    score_policy = _score_policy(tmp_path, score_public)
    invalid_policy_document = json.loads((tmp_path / "policy.json").read_bytes())
    invalid_policy_document["classes"][0]["assignment"]["mode"] = "asserted_score"
    invalid_policy_document["classes"][0]["assignment"]["metric"] = None
    (tmp_path / "policy.json").write_bytes(canonical_json(invalid_policy_document))
    with pytest.raises(ThinSubnetError, match="derive metric=verified_cc_gpu_jobs"):
        load_score_policy(tmp_path / "policy.json", network="finney", netuid=39)
    body = cc_gpu_score_report_body(
        [verified],
        network="finney",
        netuid=39,
        class_id="confidential_gpu_jobs",
        source_id="cathedralconfidential",
        source_epoch=9,
        generated_at="2026-07-21T12:00:00.000000Z",
        valid_until="2026-07-21T12:10:00.000000Z",
        valid_from_block=1000,
        valid_until_block=1050,
        signing_key_id="score-key-1",
        policy_digest=POLICY_DIGEST,
        verifier_digest=VERIFIER_DIGEST,
        previous_report_id=None,
    )
    report = verify_report(
        sign_report(body, score_key),
        score_policy.external_classes[0],
        network="finney",
        netuid=39,
        current_block=1010,
        now=NOW,
    )
    with pytest.raises(ThinSubnetError, match="validator-verified receipt bytes"):
        external_class_decision(
            score_policy.external_classes[0], report, coldkey_of={"miner-a": "cold-a"}
        )
    export_name = verified.receipt_id.removeprefix("cc-gpu-receipt-sha256:")
    (tmp_path / f"{export_name}.evidence.json").write_bytes(
        evidence_export(raw, evidence)
    )
    concrete_loader = CcGpuReceiptLoader(
        receipt_policy=replace(policy(), max_age_seconds=172800),
        evidence_verifier=verifier,
        local_export_directory=tmp_path,
    )
    loaded_receipts = concrete_loader(
        score_policy.external_classes[0], report, block=1010
    )
    assert set(loaded_receipts) == {verified.receipt_id}
    assert loaded_receipts[verified.receipt_id].document == verified.document
    decision = external_class_decision(
        score_policy.external_classes[0],
        report,
        coldkey_of={"miner-a": "cold-a"},
        verified_cc_gpu_receipts=loaded_receipts,
    )
    assert decision.raw_scores == {"miner-a": 1.0}

    wrong_subject: VerifiedCcGpuReceipt = replace(verified, subject_hotkey="miner-b")
    with pytest.raises(ThinSubnetError, match="subject"):
        external_class_decision(
            score_policy.external_classes[0],
            report,
            coldkey_of={"miner-a": "cold-a"},
            verified_cc_gpu_receipts={verified.receipt_id: wrong_subject},
        )

    wrong_verifier = replace(verified, verifier_digest="sha256:" + "ff" * 32)
    with pytest.raises(ThinSubnetError, match="receipt verifier"):
        external_class_decision(
            score_policy.external_classes[0],
            report,
            coldkey_of={"miner-a": "cold-a"},
            verified_cc_gpu_receipts={verified.receipt_id: wrong_verifier},
        )

    asserted_policy = replace(
        score_policy.external_classes[0],
        assignment=replace(
            score_policy.external_classes[0].assignment,
            mode="asserted_score",
            metric=None,
        ),
    )
    inflated_report = replace(
        report,
        entries=(
            replace(
                report.entries[0],
                metrics={},
                asserted_score=Decimal("1000000"),
            ),
        ),
    )
    with pytest.raises(ThinSubnetError, match="derive metric=verified_cc_gpu_jobs"):
        external_class_decision(
            asserted_policy,
            inflated_report,
            coldkey_of={"miner-a": "cold-a"},
            verified_cc_gpu_receipts={verified.receipt_id: verified},
        )
