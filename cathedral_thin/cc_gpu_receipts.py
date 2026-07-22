"""Validator enforcement for Cathedral's flat confidential-GPU receipt."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import itertools
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .core import (
    CC_GPU_REPLAY_RETENTION_SAFETY_SECONDS,
    CC_GPU_REPLAY_CLAIM_KEYS,
    MAX_CC_GPU_EVIDENCE_REPLAY_CLAIMS,
    MAX_CC_GPU_FUTURE_SKEW_SECONDS,
    MAX_CC_GPU_ID_REPLAY_CLAIMS,
    MAX_CC_GPU_RECEIPT_AGE_SECONDS,
    ThinSubnetError,
)
from .score_classes import canonical_json, parse_strict_json


CC_GPU_RECEIPT_SCHEMA = "cathedral_cc_gpu_job_receipt_v1"
CC_GPU_EXECUTION_CLASS = "cc_gpu"
CC_GPU_COMPLETED_OUTCOME = "completed"
CC_GPU_JOB_CONTEXT_DOMAIN = b"cathedral-cc-gpu-job-context-v1\0"
CC_GPU_PROFILE_ID = "gcp-a3-high-h100-tdx-v1"
CC_GPU_PROVIDER = "gcp"
CC_GPU_MACHINE_TYPE = "a3-highgpu-1g"
CC_GPU_ZONE = "us-central1-a"
CC_GPU_CPU_TEE = "intel_tdx"
CC_GPU_MODEL = "nvidia_h100_80gb"
CC_GPU_COUNT = 1
CC_GPU_PROVISIONING_MODEL = "spot"
MAX_CC_GPU_RECEIPT_BYTES = 1_048_576
MAX_CC_GPU_BATCH_RECEIPTS = 128
MAX_CC_GPU_EVIDENCE_BYTES = 4_194_304

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RECEIPT_ID_RE = re.compile(r"cc-gpu-receipt-sha256:[0-9a-f]{64}")
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_PROFILE_AUTHORITY_RE = re.compile(
    r"gpu-profile:[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"@profile=sha256:[0-9a-f]{64}"
    r"@release=[1-9][0-9]{0,18}@registry=sha256:[0-9a-f]{64}"
)
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_SIGNATURE_KEYS = frozenset({"algorithm", "value_base64"})
_TOP_KEYS = frozenset(
    {
        "schema",
        "receipt_id",
        "execution_class",
        "profile_id",
        "provider",
        "machine_type",
        "zone",
        "cpu_tee",
        "gpu_model",
        "gpu_count",
        "provisioning_model",
        "worker_id",
        "job_id",
        "attempt_id",
        "subject_hotkey",
        "profile_authority",
        "job_context_digest",
        "admission_bundle_digest",
        "admission_nonce_digest",
        "admission_cpu_evidence_digest",
        "admission_gpu_evidence_digest",
        "admission_gpu_identity_set_digest",
        "completion_bundle_digest",
        "completion_nonce_digest",
        "completion_cpu_evidence_digest",
        "completion_gpu_evidence_digest",
        "completion_gpu_identity_set_digest",
        "channel_binding_digest",
        "image_digest",
        "policy_digest",
        "input_digest",
        "model_digest",
        "result_digest",
        "artifact_manifest_digest",
        "secret_release_grant_digest",
        "outcome",
        "deletion_confirmed",
        "deletion_evidence_digest",
        "policy_registry_release",
        "policy_registry_digest",
        "issued_at",
        "signing_key_id",
        "signature",
    }
)
_EVIDENCE_FIELDS = (
    "admission_bundle_digest",
    "admission_cpu_evidence_digest",
    "admission_gpu_evidence_digest",
    "completion_bundle_digest",
    "completion_cpu_evidence_digest",
    "completion_gpu_evidence_digest",
)
_REPLAY_FIELDS = (
    *_EVIDENCE_FIELDS,
    "admission_nonce_digest",
    "completion_nonce_digest",
    "secret_release_grant_digest",
    "deletion_evidence_digest",
)
_DIGEST_FIELDS = (
    "job_context_digest",
    *_EVIDENCE_FIELDS,
    "admission_nonce_digest",
    "completion_nonce_digest",
    "admission_gpu_identity_set_digest",
    "completion_gpu_identity_set_digest",
    "channel_binding_digest",
    "image_digest",
    "policy_digest",
    "input_digest",
    "model_digest",
    "result_digest",
    "artifact_manifest_digest",
    "secret_release_grant_digest",
    "deletion_evidence_digest",
    "policy_registry_digest",
)


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"{label} must be a canonical SHA-256 digest")
    return value


def sha256_digest(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise ThinSubnetError("digest input must be bytes")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"invalid CC GPU {label}")
    return value


def _subject_hotkey(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ThinSubnetError("invalid CC GPU subject hotkey")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ThinSubnetError("invalid CC GPU subject hotkey") from exc
    if len(encoded) > 512 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise ThinSubnetError("invalid CC GPU subject hotkey")
    return value


def _time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"{label} must be canonical UTC time")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ThinSubnetError(f"invalid {label}") from exc


def _framed(*values: str) -> bytes:
    framed = bytearray()
    for value in values:
        try:
            encoded = value.encode("utf-8")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise ThinSubnetError("CC GPU binding text is invalid") from exc
        if not encoded or len(encoded) > 4096:
            raise ThinSubnetError("CC GPU binding text is out of bounds")
        framed.extend(len(encoded).to_bytes(4, "big"))
        framed.extend(encoded)
    return bytes(framed)


def _unsigned_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature"}


def _id_material(document: Mapping[str, Any]) -> bytes:
    return canonical_json(
        {
            key: value
            for key, value in document.items()
            if key not in {"receipt_id", "signature"}
        }
    )


def receipt_id_for(document: Mapping[str, Any]) -> str:
    return "cc-gpu-receipt-sha256:" + hashlib.sha256(_id_material(document)).hexdigest()


def job_context_digest_for(document: Mapping[str, Any]) -> str:
    return sha256_digest(
        CC_GPU_JOB_CONTEXT_DOMAIN
        + _framed(
            document["worker_id"],
            document["subject_hotkey"],
            document["job_id"],
            document["attempt_id"],
            document["profile_id"],
            document["provider"],
            document["machine_type"],
            document["zone"],
            document["cpu_tee"],
            document["gpu_model"],
            str(document["gpu_count"]),
            document["provisioning_model"],
            document["profile_authority"],
            document["image_digest"],
            document["policy_digest"],
            document["input_digest"],
            document["model_digest"],
        )
    )


@dataclass(frozen=True)
class CcGpuTrustedSigningKey:
    public_key: bytes
    valid_from: datetime
    valid_until: datetime


@dataclass(frozen=True)
class CcGpuReceiptPolicy:
    expected_profile_id: str
    allowed_profile_authorities: frozenset[str]
    allowed_policy_digests: frozenset[str]
    allowed_image_digests: frozenset[str]
    allowed_model_digests: frozenset[str]
    policy_registry_release: int
    policy_registry_digest: str
    policy_registry_valid_from: datetime
    policy_registry_valid_until: datetime
    trusted_signing_keys: Mapping[str, CcGpuTrustedSigningKey]
    allowed_verifier_digests: frozenset[str]
    max_age_seconds: int = 300
    max_future_seconds: int = 5


@dataclass(frozen=True)
class CcGpuEvidenceVerification:
    ok: bool
    verifier_digest: str
    cpu_measurement_digest: str | None
    gpu_measurement_digest: str | None
    cpu_nonce_digest: str | None
    gpu_nonce_digest: str | None
    job_context_digest: str | None
    subject_hotkey: str | None
    channel_binding_digest: str | None
    gpu_identity_set_digest: str | None
    same_guest: bool
    gpu_cc_mode_enabled: bool
    gpu_ready_state: bool
    measurement_policy_ok: bool
    runtime_isolation_ok: bool
    secret_release_grant_digest: str | None
    secret_release_signature_verified: bool
    secret_release_semantics_verified: bool
    deletion_evidence_digest: str | None
    deletion_signature_verified: bool
    deletion_semantics_verified: bool
    provider_absent: bool
    reason: str


class CcGpuEvidenceVerifier(Protocol):
    def __call__(
        self,
        phase: str,
        bundle: bytes,
        cpu: bytes,
        gpu: bytes,
        identity_set: bytes,
        release_grant_evidence: bytes,
        deletion_evidence: bytes,
        document: Mapping[str, Any],
        *,
        deadline_monotonic: float | None = None,
    ) -> CcGpuEvidenceVerification: ...


@dataclass(frozen=True)
class VerifiedCcGpuReceipt:
    receipt_id: str
    receipt_digest: str
    worker_id: str
    job_id: str
    attempt_id: str
    subject_hotkey: str
    profile_id: str
    issued_at: datetime
    replay_expires_at_ms: int
    verifier_digest: str
    evidence_digests: tuple[str, ...]
    document: dict[str, Any]


def _validate_policy(policy: CcGpuReceiptPolicy) -> None:
    if (
        not isinstance(policy.expected_profile_id, str)
        or not policy.expected_profile_id
        or not policy.allowed_profile_authorities
        or not policy.allowed_policy_digests
        or not policy.allowed_image_digests
        or not policy.allowed_model_digests
        or not policy.trusted_signing_keys
        or not policy.allowed_verifier_digests
    ):
        raise ThinSubnetError("CC GPU policy requires every validator trust root")
    if (
        isinstance(policy.policy_registry_release, bool)
        or not isinstance(policy.policy_registry_release, int)
        or policy.policy_registry_release <= 0
    ):
        raise ThinSubnetError("invalid CC GPU policy registry release")
    _digest(policy.policy_registry_digest, "policy registry digest")
    if (
        not isinstance(policy.policy_registry_valid_from, datetime)
        or not isinstance(policy.policy_registry_valid_until, datetime)
        or policy.policy_registry_valid_from.tzinfo is None
        or policy.policy_registry_valid_from.utcoffset() != timedelta(0)
        or policy.policy_registry_valid_until.tzinfo is None
        or policy.policy_registry_valid_until.utcoffset() != timedelta(0)
        or policy.policy_registry_valid_until <= policy.policy_registry_valid_from
    ):
        raise ThinSubnetError("invalid CC GPU policy registry validity window")
    for seconds, label in (
        (policy.max_age_seconds, "max_age_seconds"),
        (policy.max_future_seconds, "max_future_seconds"),
    ):
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise ThinSubnetError(f"CC GPU {label} must be positive")
    if policy.max_age_seconds > MAX_CC_GPU_RECEIPT_AGE_SECONDS:
        raise ThinSubnetError("CC GPU max_age_seconds exceeds the replay-safe limit")
    if policy.max_future_seconds > MAX_CC_GPU_FUTURE_SKEW_SECONDS:
        raise ThinSubnetError("CC GPU max_future_seconds exceeds the replay-safe limit")
    if any(
        not isinstance(authority, str)
        or _PROFILE_AUTHORITY_RE.fullmatch(authority) is None
        for authority in policy.allowed_profile_authorities
    ):
        raise ThinSubnetError("invalid allowed CC GPU profile authority")
    if any(
        not isinstance(key_id, str)
        or _KEY_ID_RE.fullmatch(key_id) is None
        or not isinstance(key, CcGpuTrustedSigningKey)
        or not isinstance(key.public_key, bytes)
        or len(key.public_key) != 32
        or not isinstance(key.valid_from, datetime)
        or not isinstance(key.valid_until, datetime)
        or key.valid_from.tzinfo is None
        or key.valid_from.utcoffset() != timedelta(0)
        or key.valid_until.tzinfo is None
        or key.valid_until.utcoffset() != timedelta(0)
        or key.valid_until <= key.valid_from
        for key_id, key in policy.trusted_signing_keys.items()
    ):
        raise ThinSubnetError("invalid pinned CC GPU signing key")


def _evidence_bytes(
    evidence_by_digest: Mapping[str, bytes], digest: str, label: str
) -> bytes:
    evidence = evidence_by_digest.get(digest)
    if not isinstance(evidence, bytes) or not evidence:
        raise ThinSubnetError(f"missing {label} bytes")
    if len(evidence) > MAX_CC_GPU_EVIDENCE_BYTES:
        raise ThinSubnetError(f"{label} exceeds size limit")
    if not hmac.compare_digest(sha256_digest(evidence), digest):
        raise ThinSubnetError(f"{label} digest mismatch")
    return evidence


def _verify_signature(
    document: Mapping[str, Any],
    policy: CcGpuReceiptPolicy,
    issued_at: datetime,
) -> None:
    key_id = document["signing_key_id"]
    if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
        raise ThinSubnetError("invalid CC GPU signing key id")
    trusted_key = policy.trusted_signing_keys.get(key_id)
    if trusted_key is None:
        raise ThinSubnetError("CC GPU signing key is not pinned")
    if not trusted_key.valid_from <= issued_at < trusted_key.valid_until:
        raise ThinSubnetError("CC GPU signing key was not active at issued_at")
    signature = document["signature"]
    if not isinstance(signature, dict) or frozenset(signature) != _SIGNATURE_KEYS:
        raise ThinSubnetError("CC GPU receipt signature fields are invalid")
    if signature["algorithm"] != "ed25519":
        raise ThinSubnetError("unsupported CC GPU receipt signature algorithm")
    try:
        raw_signature = base64.b64decode(signature["value_base64"], validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ThinSubnetError(
            "CC GPU receipt signature is not canonical base64"
        ) from exc
    if (
        len(raw_signature) != 64
        or base64.b64encode(raw_signature).decode("ascii") != signature["value_base64"]
    ):
        raise ThinSubnetError("CC GPU receipt signature must be 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(trusted_key.public_key).verify(
            raw_signature, canonical_json(_unsigned_document(document))
        )
    except (InvalidSignature, ValueError) as exc:
        raise ThinSubnetError("CC GPU receipt signature verification failed") from exc


def verify_cc_gpu_receipt(
    raw: bytes,
    policy: CcGpuReceiptPolicy,
    *,
    now: datetime | None = None,
    evidence_by_digest: Mapping[str, bytes],
    evidence_verifier: CcGpuEvidenceVerifier,
    deadline_monotonic: float | None = None,
) -> VerifiedCcGpuReceipt:
    if deadline_monotonic is not None:
        if time.monotonic() >= deadline_monotonic:
            raise ThinSubnetError("CC GPU verification batch deadline exceeded")
    _validate_policy(policy)
    document = parse_strict_json(raw, maximum_bytes=MAX_CC_GPU_RECEIPT_BYTES)
    if raw != canonical_json(document) or frozenset(document) != _TOP_KEYS:
        raise ThinSubnetError("CC GPU receipt has non-canonical or unknown fields")
    if document["schema"] != CC_GPU_RECEIPT_SCHEMA:
        raise ThinSubnetError("unsupported CC GPU receipt schema")
    if document["execution_class"] != CC_GPU_EXECUTION_CLASS:
        raise ThinSubnetError("hybrid or CPU receipts are not CC GPU receipts")
    if document["outcome"] != CC_GPU_COMPLETED_OUTCOME:
        raise ThinSubnetError("CC GPU receipt outcome must be completed")
    if document["deletion_confirmed"] is not True:
        raise ThinSubnetError("CC GPU receipt requires confirmed deletion")
    worker_id = _uuid(document["worker_id"], "worker id")
    job_id = _uuid(document["job_id"], "job id")
    attempt_id = _uuid(document["attempt_id"], "attempt id")
    subject_hotkey = _subject_hotkey(document["subject_hotkey"])
    provisioning_model = document["provisioning_model"]
    gpu_count = document["gpu_count"]
    if (
        policy.expected_profile_id != CC_GPU_PROFILE_ID
        or document["profile_id"] != CC_GPU_PROFILE_ID
        or document["provider"] != CC_GPU_PROVIDER
        or document["machine_type"] != CC_GPU_MACHINE_TYPE
        or document["zone"] != CC_GPU_ZONE
        or document["cpu_tee"] != CC_GPU_CPU_TEE
        or document["gpu_model"] != CC_GPU_MODEL
        or isinstance(gpu_count, bool)
        or not isinstance(gpu_count, int)
        or gpu_count != CC_GPU_COUNT
        or not isinstance(provisioning_model, str)
        or provisioning_model != CC_GPU_PROVISIONING_MODEL
    ):
        raise ThinSubnetError("CC GPU receipt profile is not allowed")
    authority = document["profile_authority"]
    if (
        not isinstance(authority, str)
        or _PROFILE_AUTHORITY_RE.fullmatch(authority) is None
        or authority not in policy.allowed_profile_authorities
        or not authority.startswith(f"gpu-profile:{policy.expected_profile_id}@")
        or not authority.endswith(
            f"@release={policy.policy_registry_release}"
            f"@registry={policy.policy_registry_digest}"
        )
    ):
        raise ThinSubnetError("CC GPU profile authority is not active")
    for name in _DIGEST_FIELDS:
        _digest(document[name], name.replace("_", " "))
    if document["policy_digest"] not in policy.allowed_policy_digests:
        raise ThinSubnetError("CC GPU policy digest is not allowed")
    if document["image_digest"] not in policy.allowed_image_digests:
        raise ThinSubnetError("CC GPU image digest is not allowed")
    if document["model_digest"] not in policy.allowed_model_digests:
        raise ThinSubnetError("CC GPU model digest is not allowed")
    if document["job_context_digest"] != job_context_digest_for(document):
        raise ThinSubnetError("CC GPU job context digest is mismatched")
    if len({document[name] for name in _EVIDENCE_FIELDS}) != len(_EVIDENCE_FIELDS):
        raise ThinSubnetError("CC GPU admission or completion evidence was reused")
    if document["admission_nonce_digest"] == document["completion_nonce_digest"]:
        raise ThinSubnetError("CC GPU admission or completion nonce was reused")
    if len({document[name] for name in _REPLAY_FIELDS}) != len(_REPLAY_FIELDS):
        raise ThinSubnetError(
            "CC GPU nonce, grant, deletion, or phase evidence was reused"
        )
    if (
        document["admission_gpu_identity_set_digest"]
        != document["completion_gpu_identity_set_digest"]
    ):
        raise ThinSubnetError("CC GPU admission and completion GPU identities differ")
    release = document["policy_registry_release"]
    if (
        isinstance(release, bool)
        or not isinstance(release, int)
        or release != policy.policy_registry_release
        or document["policy_registry_digest"] != policy.policy_registry_digest
    ):
        raise ThinSubnetError("CC GPU policy registry is mismatched")
    issued_at = _time(document["issued_at"], "issued_at")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ThinSubnetError("CC GPU verification time must be UTC")
    age = (current - issued_at).total_seconds()
    if age < -policy.max_future_seconds or age >= policy.max_age_seconds:
        raise ThinSubnetError("CC GPU receipt is stale or from the future")
    if (
        not policy.policy_registry_valid_from
        <= issued_at
        < policy.policy_registry_valid_until
    ):
        raise ThinSubnetError("CC GPU policy registry was not active at issued_at")
    receipt_id = document["receipt_id"]
    if (
        not isinstance(receipt_id, str)
        or _RECEIPT_ID_RE.fullmatch(receipt_id) is None
        or receipt_id != receipt_id_for(document)
    ):
        raise ThinSubnetError("CC GPU receipt id is mismatched")
    _verify_signature(document, policy, issued_at)

    identity_set = _evidence_bytes(
        evidence_by_digest,
        document["admission_gpu_identity_set_digest"],
        "GPU identity-set evidence",
    )
    secret_release_grant = _evidence_bytes(
        evidence_by_digest,
        document["secret_release_grant_digest"],
        "secret-release grant",
    )
    deletion_evidence = _evidence_bytes(
        evidence_by_digest,
        document["deletion_evidence_digest"],
        "provider deletion evidence",
    )
    verifier_digests: set[str] = set()
    for phase in ("admission", "completion"):
        if deadline_monotonic is not None:
            if time.monotonic() >= deadline_monotonic:
                raise ThinSubnetError("CC GPU verification batch deadline exceeded")
        bundle = _evidence_bytes(
            evidence_by_digest, document[f"{phase}_bundle_digest"], f"{phase} bundle"
        )
        cpu = _evidence_bytes(
            evidence_by_digest,
            document[f"{phase}_cpu_evidence_digest"],
            f"{phase} CPU evidence",
        )
        gpu = _evidence_bytes(
            evidence_by_digest,
            document[f"{phase}_gpu_evidence_digest"],
            f"{phase} GPU evidence",
        )
        try:
            verification = evidence_verifier(
                phase,
                bundle,
                cpu,
                gpu,
                identity_set,
                secret_release_grant,
                deletion_evidence,
                document,
                deadline_monotonic=deadline_monotonic,
            )
        except ThinSubnetError:
            raise
        except Exception as exc:
            raise ThinSubnetError(
                "CC GPU evidence verifier raised an unexpected error"
            ) from exc
        if not isinstance(verification, CcGpuEvidenceVerification):
            raise ThinSubnetError("CC GPU evidence verifier returned an invalid result")
        verifier_digest = _digest(
            verification.verifier_digest, "CC GPU verifier digest"
        )
        if not verification.ok:
            reason = (
                verification.reason
                if isinstance(verification.reason, str)
                else "unclassified verifier failure"
            )
            raise ThinSubnetError(
                f"CC GPU {phase} evidence verification failed: {reason[:160]}"
            )
        if verifier_digest not in policy.allowed_verifier_digests:
            raise ThinSubnetError("CC GPU verifier digest is not allowed")
        if verification.cpu_measurement_digest is None:
            raise ThinSubnetError("CC GPU verifier omitted CPU measurement")
        if verification.gpu_measurement_digest is None:
            raise ThinSubnetError("CC GPU verifier omitted GPU measurement")
        _digest(verification.cpu_measurement_digest, "CPU measurement digest")
        _digest(verification.gpu_measurement_digest, "GPU measurement digest")
        expected_nonce_digest = document[f"{phase}_nonce_digest"]
        cpu_nonce_digest = _digest(
            verification.cpu_nonce_digest, "CPU evidence nonce digest"
        )
        gpu_nonce_digest = _digest(
            verification.gpu_nonce_digest, "GPU evidence nonce digest"
        )
        if not hmac.compare_digest(
            cpu_nonce_digest, expected_nonce_digest
        ) or not hmac.compare_digest(gpu_nonce_digest, expected_nonce_digest):
            raise ThinSubnetError(
                f"CC GPU {phase} CPU or GPU evidence nonce is mismatched"
            )
        exact_bindings = (
            (verification.job_context_digest, document["job_context_digest"]),
            (verification.subject_hotkey, subject_hotkey),
            (verification.channel_binding_digest, document["channel_binding_digest"]),
            (
                verification.gpu_identity_set_digest,
                document[f"{phase}_gpu_identity_set_digest"],
            ),
            (
                verification.secret_release_grant_digest,
                document["secret_release_grant_digest"],
            ),
            (
                verification.deletion_evidence_digest,
                document["deletion_evidence_digest"],
            ),
        )
        if any(
            not isinstance(observed, str) or not hmac.compare_digest(observed, expected)
            for observed, expected in exact_bindings
        ):
            raise ThinSubnetError(
                f"CC GPU {phase} evidence has a mismatched job, channel, identity, grant, or deletion binding"
            )
        if not all(
            value is True
            for value in (
                verification.same_guest,
                verification.gpu_cc_mode_enabled,
                verification.gpu_ready_state,
                verification.measurement_policy_ok,
                verification.runtime_isolation_ok,
                verification.secret_release_signature_verified,
                verification.secret_release_semantics_verified,
                verification.deletion_signature_verified,
                verification.deletion_semantics_verified,
                verification.provider_absent,
            )
        ):
            raise ThinSubnetError(
                f"CC GPU {phase} evidence did not prove isolation, secret release, and provider deletion"
            )
        verifier_digests.add(verifier_digest)
    if len(verifier_digests) != 1:
        raise ThinSubnetError("CC GPU phases used different verifiers")
    return VerifiedCcGpuReceipt(
        receipt_id=receipt_id,
        receipt_digest=sha256_digest(raw),
        worker_id=worker_id,
        job_id=job_id,
        attempt_id=attempt_id,
        subject_hotkey=subject_hotkey,
        profile_id=document["profile_id"],
        issued_at=issued_at,
        replay_expires_at_ms=int(
            (
                issued_at
                + timedelta(
                    seconds=(
                        MAX_CC_GPU_RECEIPT_AGE_SECONDS
                        + MAX_CC_GPU_FUTURE_SKEW_SECONDS
                        + CC_GPU_REPLAY_RETENTION_SAFETY_SECONDS
                    )
                )
            ).timestamp()
            * 1000
        ),
        verifier_digest=next(iter(verifier_digests)),
        evidence_digests=tuple(document[name] for name in _REPLAY_FIELDS),
        document=document,
    )


def verify_cc_gpu_receipt_batch(
    receipts: Iterable[bytes],
    policy: CcGpuReceiptPolicy,
    *,
    now: datetime | None = None,
    evidence_by_digest: Mapping[str, bytes],
    evidence_verifier: CcGpuEvidenceVerifier,
    deadline_monotonic: float | None = None,
) -> tuple[VerifiedCcGpuReceipt, ...]:
    raw_receipts = list(itertools.islice(receipts, MAX_CC_GPU_BATCH_RECEIPTS + 1))
    if not 1 <= len(raw_receipts) <= MAX_CC_GPU_BATCH_RECEIPTS:
        raise ThinSubnetError("CC GPU receipt batch must contain 1..128 receipts")
    verified = [
        verify_cc_gpu_receipt(
            raw,
            policy,
            now=now,
            evidence_by_digest=evidence_by_digest,
            evidence_verifier=evidence_verifier,
            deadline_monotonic=deadline_monotonic,
        )
        for raw in raw_receipts
    ]
    receipt_ids: set[str] = set()
    worker_ids: set[str] = set()
    job_ids: set[str] = set()
    attempt_ids: set[str] = set()
    attempts: set[tuple[str, str, str]] = set()
    evidence_digests: set[str] = set()
    for receipt in verified:
        attempt = (receipt.worker_id, receipt.job_id, receipt.attempt_id)
        if receipt.receipt_id in receipt_ids:
            raise ThinSubnetError("duplicate CC GPU receipt id across miners")
        if receipt.worker_id in worker_ids or receipt.job_id in job_ids:
            raise ThinSubnetError("duplicate CC GPU worker or job id across miners")
        if receipt.attempt_id in attempt_ids or attempt in attempts:
            raise ThinSubnetError("duplicate CC GPU attempt across miners")
        if evidence_digests.intersection(receipt.evidence_digests):
            raise ThinSubnetError("duplicate CC GPU evidence across miners")
        receipt_ids.add(receipt.receipt_id)
        worker_ids.add(receipt.worker_id)
        job_ids.add(receipt.job_id)
        attempt_ids.add(receipt.attempt_id)
        attempts.add(attempt)
        evidence_digests.update(receipt.evidence_digests)
    return tuple(sorted(verified, key=lambda item: item.receipt_id))


def cc_gpu_score_report_body(
    receipts: Iterable[VerifiedCcGpuReceipt],
    *,
    network: str,
    netuid: int,
    class_id: str,
    source_id: str,
    source_epoch: int,
    generated_at: str,
    valid_until: str,
    valid_from_block: int,
    valid_until_block: int,
    signing_key_id: str,
    policy_digest: str,
    verifier_digest: str,
    previous_report_id: str | None,
    evidence_uris: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    from .score_classes import REPORT_SCHEMA

    verified = tuple(receipts)
    if not verified:
        raise ThinSubnetError("CC GPU score report requires verified receipts")
    verify_unique_cc_gpu_receipts(verified)
    claimed_verifier = _digest(verifier_digest, "score verifier digest")
    if {item.verifier_digest for item in verified} != {claimed_verifier}:
        raise ThinSubnetError("score verifier does not match every CC GPU receipt")
    grouped: dict[str, list[VerifiedCcGpuReceipt]] = {}
    for receipt in verified:
        grouped.setdefault(receipt.subject_hotkey, []).append(receipt)
    entries = []
    for hotkey, items in sorted(grouped.items()):
        if len(items) > 32:
            raise ThinSubnetError("CC GPU score entry exceeds evidence reference limit")
        items.sort(key=lambda item: item.receipt_id)
        entries.append(
            {
                "miner_hotkey": hotkey,
                "metrics": {"verified_cc_gpu_jobs": str(len(items))},
                "asserted_score": None,
                "reason_codes": sorted(
                    [
                        "cc_gpu_admission_verified",
                        "cc_gpu_completion_verified",
                        "confirmed_deletion",
                        "receipt_signature_verified",
                    ]
                ),
                "evidence": [
                    {
                        "kind": CC_GPU_RECEIPT_SCHEMA,
                        "id": item.receipt_id,
                        "digest": item.receipt_digest,
                        "uri": (evidence_uris or {}).get(item.receipt_id),
                    }
                    for item in items
                ],
            }
        )
    return {
        "schema": REPORT_SCHEMA,
        "network": network,
        "netuid": netuid,
        "class_id": class_id,
        "source_id": source_id,
        "source_epoch": source_epoch,
        "generated_at": generated_at,
        "valid_until": valid_until,
        "valid_from_block": valid_from_block,
        "valid_until_block": valid_until_block,
        "complete": True,
        "policy_digest": _digest(policy_digest, "score policy digest"),
        "verifier_digest": claimed_verifier,
        "previous_report_id": previous_report_id,
        "entries": entries,
        "signing_key_id": signing_key_id,
    }


def verify_unique_cc_gpu_receipts(
    receipts: Iterable[VerifiedCcGpuReceipt],
) -> tuple[VerifiedCcGpuReceipt, ...]:
    verified = tuple(receipts)
    receipt_ids: set[str] = set()
    workers: set[str] = set()
    jobs: set[str] = set()
    attempts: set[str] = set()
    evidence: set[str] = set()
    for receipt in verified:
        if receipt.receipt_id in receipt_ids:
            raise ThinSubnetError("duplicate CC GPU receipt id")
        if receipt.worker_id in workers or receipt.job_id in jobs:
            raise ThinSubnetError("duplicate CC GPU worker or job id")
        if receipt.attempt_id in attempts:
            raise ThinSubnetError("duplicate CC GPU attempt id")
        if evidence.intersection(receipt.evidence_digests):
            raise ThinSubnetError("duplicate CC GPU evidence")
        receipt_ids.add(receipt.receipt_id)
        workers.add(receipt.worker_id)
        jobs.add(receipt.job_id)
        attempts.add(receipt.attempt_id)
        evidence.update(receipt.evidence_digests)
    return verified


def merge_cc_gpu_replay_claims(
    existing: Mapping[str, Mapping[str, int]],
    receipts: Iterable[VerifiedCcGpuReceipt],
    *,
    now: datetime | None = None,
    prior_watermark_ms: int = 0,
) -> tuple[dict[str, dict[str, int]], int]:
    """Return a bounded replay ledger after rejecting all prior or batch reuse."""

    verified = verify_unique_cc_gpu_receipts(receipts)
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != timedelta(0):
        raise ThinSubnetError("CC GPU replay merge time must be UTC")
    now_ms = int(current.timestamp() * 1000)
    if now_ms < prior_watermark_ms:
        raise ThinSubnetError("CC GPU replay clock moved behind its durable watermark")
    additions: dict[str, dict[str, int]] = {
        "attempt_ids": {
            item.attempt_id: item.replay_expires_at_ms for item in verified
        },
        "evidence_digests": {
            digest: item.replay_expires_at_ms
            for item in verified
            for digest in item.evidence_digests
        },
        "job_ids": {item.job_id: item.replay_expires_at_ms for item in verified},
        "receipt_ids": {
            item.receipt_id: item.replay_expires_at_ms for item in verified
        },
        "worker_ids": {item.worker_id: item.replay_expires_at_ms for item in verified},
    }
    if tuple(sorted(existing)) != CC_GPU_REPLAY_CLAIM_KEYS:
        raise ThinSubnetError("invalid CC GPU replay claim ledger")
    for category in (
        "receipt_ids",
        "worker_ids",
        "job_ids",
        "attempt_ids",
        "evidence_digests",
    ):
        active = {
            claim: expiry
            for claim, expiry in existing[category].items()
            if expiry > now_ms
        }
        if set(active).intersection(additions[category]):
            raise ThinSubnetError(f"replayed CC GPU {category}")
    merged: dict[str, dict[str, int]] = {}
    for category in CC_GPU_REPLAY_CLAIM_KEYS:
        combined = {
            claim: expiry
            for claim, expiry in existing[category].items()
            if expiry > now_ms
        }
        combined.update(additions[category])
        maximum = (
            MAX_CC_GPU_EVIDENCE_REPLAY_CLAIMS
            if category == "evidence_digests"
            else MAX_CC_GPU_ID_REPLAY_CLAIMS
        )
        if len(combined) > maximum:
            raise ThinSubnetError("CC GPU replay claim ledger capacity exceeded")
        merged[category] = dict(sorted(combined.items()))
    return merged, now_ms
