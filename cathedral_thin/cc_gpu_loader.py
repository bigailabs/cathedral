"""Bounded transport and offline ingestion for confidential-GPU evidence exports."""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import itertools
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .cc_gpu_receipts import (
    CC_GPU_PROFILE_ID,
    CC_GPU_RECEIPT_SCHEMA,
    MAX_CC_GPU_EVIDENCE_BYTES,
    CcGpuEvidenceVerification,
    CcGpuEvidenceVerifier,
    CcGpuReceiptPolicy,
    CcGpuTrustedSigningKey,
    VerifiedCcGpuReceipt,
    sha256_digest,
    verify_cc_gpu_receipt,
    verify_unique_cc_gpu_receipts,
)
from .core import ThinSubnetError
from .score_classes import (
    ExternalClassPolicy,
    VerifiedReport,
    assignment_score,
    canonical_json,
    parse_strict_json,
)


CC_GPU_EVIDENCE_EXPORT_SCHEMA = "cathedral_cc_gpu_evidence_export_v1"
CC_GPU_LOADER_CONFIG_SCHEMA = "cathedral_cc_gpu_receipt_loader_config_v1"
MAX_CC_GPU_EXPORT_BYTES = 64 * 1024 * 1024
MAX_CC_GPU_EXPORT_ARTIFACTS = 16
MAX_CC_GPU_VERIFIER_RESULT_BYTES = 65_536
MAX_CC_GPU_RECEIPTS_PER_REPORT = 128
MAX_CC_GPU_EXPORT_BYTES_PER_REPORT = 256 * 1024 * 1024

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_EXPORT_KEYS = frozenset(
    {"schema", "receipt_id", "receipt", "artifacts", "authentication", "integrity"}
)
_ARTIFACT_KEYS = frozenset({"encoding", "data_base64", "byte_length", "kinds"})
_AUTHENTICATION = {"owner_scoped": True}
_INTEGRITY = {"receipt_signature": "ed25519", "artifact_digest": "sha256"}
_KIND_FIELDS = {
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
_CONFIG_KEYS = frozenset(
    {
        "schema",
        "local_export_directory",
        "allowed_https_origins",
        "authorization_bearer_env",
        "verifier_command",
        "verifier_executable_digest",
        "verifier_timeout_seconds",
        "batch_deadline_seconds",
        "receipt_policy",
    }
)
_RECEIPT_POLICY_KEYS = frozenset(
    {
        "expected_profile_id",
        "allowed_profile_authorities",
        "allowed_policy_digests",
        "allowed_image_digests",
        "allowed_model_digests",
        "policy_registry_release",
        "policy_registry_digest",
        "policy_registry_valid_from",
        "policy_registry_valid_until",
        "trusted_signing_keys",
        "allowed_verifier_digests",
        "max_age_seconds",
        "max_future_seconds",
    }
)
_TRUSTED_KEY_KEYS = frozenset({"public_key_base64", "valid_from", "valid_until"})
_VERIFIER_RESULT_KEYS = frozenset(
    {
        "ok",
        "cpu_measurement_digest",
        "gpu_measurement_digest",
        "cpu_nonce_digest",
        "gpu_nonce_digest",
        "job_context_digest",
        "subject_hotkey",
        "channel_binding_digest",
        "gpu_identity_set_digest",
        "same_guest",
        "gpu_cc_mode_enabled",
        "gpu_ready_state",
        "measurement_policy_ok",
        "runtime_isolation_ok",
        "secret_release_grant_digest",
        "secret_release_signature_verified",
        "secret_release_semantics_verified",
        "deletion_evidence_digest",
        "deletion_signature_verified",
        "deletion_semantics_verified",
        "provider_absent",
        "reason",
    }
)
_VERIFIER_PLACEHOLDERS = frozenset(
    {
        "{phase}",
        "{bundle_path}",
        "{cpu_evidence_path}",
        "{gpu_evidence_path}",
        "{gpu_identity_set_path}",
        "{secret_release_grant_path}",
        "{deletion_evidence_path}",
        "{receipt_path}",
        "{result_path}",
    }
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if frozenset(value) != expected:
        raise ThinSubnetError(f"{label} fields are invalid")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"{label} must be a canonical SHA-256 digest")
    return value


def _canonical_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ThinSubnetError(f"{label} must be canonical UTC time")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ThinSubnetError(f"{label} must be canonical UTC time") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise ThinSubnetError(f"{label} must be canonical UTC time")
    return parsed


def _read_regular_file(path: Path, *, maximum: int, label: str) -> bytes:
    try:
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ThinSubnetError(f"{label} must be a regular non-symlink file")
        if info.st_size <= 0 or info.st_size > maximum:
            raise ThinSubnetError(f"{label} exceeds size limit")
        raw = path.read_bytes()
    except ThinSubnetError:
        raise
    except OSError as exc:
        raise ThinSubnetError(f"could not read {label}: {path}") from exc
    if not raw or len(raw) > maximum:
        raise ThinSubnetError(f"{label} exceeds size limit")
    return raw


def _digest_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    try:
        offset = 0
        while chunk := os.pread(descriptor, 1_048_576, offset):
            digest.update(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise ThinSubnetError("could not hash CC GPU verifier executable") from exc
    return "sha256:" + digest.hexdigest()


def _native_elf_machine() -> int:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return 62
    if machine in {"aarch64", "arm64"}:
        return 183
    raise ThinSubnetError(f"unsupported CC GPU verifier host architecture: {machine}")


def _validate_static_native_elf(descriptor: int) -> None:
    try:
        header = os.pread(descriptor, 64, 0)
    except OSError as exc:
        raise ThinSubnetError("could not inspect CC GPU verifier ELF") from exc
    if (
        len(header) != 64
        or header[:4] != b"\x7fELF"
        or header[4] != 2
        or header[5] != 1
        or header[6] != 1
    ):
        raise ThinSubnetError("CC GPU verifier must be a 64-bit little-endian ELF")
    elf_type, machine = struct.unpack_from("<HH", header, 16)
    program_offset = struct.unpack_from("<Q", header, 32)[0]
    header_size, program_size, program_count = struct.unpack_from("<HHH", header, 52)
    if elf_type != 2 or machine != _native_elf_machine():
        raise ThinSubnetError("CC GPU verifier must be a native static ELF executable")
    if (
        header_size != 64
        or program_size != 56
        or not 1 <= program_count <= 128
        or program_offset < 64
    ):
        raise ThinSubnetError("CC GPU verifier ELF headers are invalid")
    table_size = program_size * program_count
    try:
        table = os.pread(descriptor, table_size, program_offset)
    except OSError as exc:
        raise ThinSubnetError(
            "could not inspect CC GPU verifier program headers"
        ) from exc
    if len(table) != table_size:
        raise ThinSubnetError("CC GPU verifier ELF program headers are truncated")
    program_types = {
        struct.unpack_from("<I", table, index * program_size)[0]
        for index in range(program_count)
    }
    if 2 in program_types or 3 in program_types:
        raise ThinSubnetError(
            "CC GPU verifier must be static and contain no interpreter or dynamic segment"
        )


def _resolve_executable(tokens: list[str]) -> str:
    if not tokens:
        raise ThinSubnetError("CC GPU verifier command is empty")
    executable = tokens[0]
    resolved = executable if os.path.isabs(executable) else shutil.which(executable)
    if not resolved:
        raise ThinSubnetError(f"CC GPU verifier not found: {executable}")
    resolved = os.path.realpath(resolved)
    descriptor = _open_verifier_fd(resolved)
    try:
        _validate_static_native_elf(descriptor)
    finally:
        os.close(descriptor)
    return resolved


def _open_verifier_fd(path: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ThinSubnetError("could not inspect CC GPU verifier executable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or not info.st_mode & 0o100
        or info.st_mode & 0o022
    ):
        os.close(descriptor)
        raise ThinSubnetError(
            "CC GPU verifier must be executable, regular, and not group/world writable"
        )
    return descriptor


def subprocess_cc_gpu_evidence_verifier(
    command: str | Iterable[str],
    *,
    expected_verifier_digest: str,
    timeout_seconds: int = 30,
) -> CcGpuEvidenceVerifier:
    """Build a pinned, shell-free adapter for a composite CC GPU verifier."""

    tokens = shlex.split(command) if isinstance(command, str) else list(command)
    if not tokens or any(not isinstance(token, str) or not token for token in tokens):
        raise ThinSubnetError("CC GPU verifier command contains an invalid token")
    present = {
        placeholder
        for token in tokens
        for placeholder in _VERIFIER_PLACEHOLDERS
        if placeholder in token
    }
    if present != _VERIFIER_PLACEHOLDERS:
        raise ThinSubnetError("CC GPU verifier command is missing placeholders")
    executable = _resolve_executable(tokens)
    tokens[0] = executable
    for token in tokens[1:]:
        if not any(placeholder in token for placeholder in _VERIFIER_PLACEHOLDERS):
            candidate = Path(token).expanduser()
            if candidate.exists() and candidate.is_file():
                raise ThinSubnetError(
                    "CC GPU verifier must be a directly pinned executable, not an interpreter plus script"
                )
    initial_descriptor = _open_verifier_fd(executable)
    try:
        actual_digest = _digest_fd(initial_descriptor)
    finally:
        os.close(initial_descriptor)
    if actual_digest != _digest(expected_verifier_digest, "verifier executable digest"):
        raise ThinSubnetError("CC GPU verifier executable digest mismatch")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 300
    ):
        raise ThinSubnetError("CC GPU verifier timeout must be 1..300 seconds")

    def verify(
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
    ) -> CcGpuEvidenceVerification:
        if phase not in {"admission", "completion"}:
            raise ThinSubnetError("invalid CC GPU evidence phase")
        if not sys.platform.startswith("linux"):
            raise ThinSubnetError("CC GPU verifier execution requires Linux")
        artifacts = {
            "bundle": bundle,
            "cpu_evidence": cpu,
            "gpu_evidence": gpu,
            "gpu_identity_set": identity_set,
            # This is a signed release-proof artifact, not model/key material.
            # Keep the protocol name stable while avoiding any implication that
            # the validator persists released plaintext secrets.
            "secret_release_grant": release_grant_evidence,
            "deletion_evidence": deletion_evidence,
        }
        if any(
            not value or len(value) > MAX_CC_GPU_EVIDENCE_BYTES
            for value in artifacts.values()
        ):
            raise ThinSubnetError("CC GPU verifier artifact is empty or oversized")
        with contextlib.ExitStack() as resources:
            verifier_descriptor = _open_verifier_fd(executable)
            resources.callback(os.close, verifier_descriptor)
            if _digest_fd(verifier_descriptor) != actual_digest:
                raise ThinSubnetError(
                    "CC GPU verifier executable changed after pinning"
                )
            _validate_static_native_elf(verifier_descriptor)
            temp_dir = resources.enter_context(
                tempfile.TemporaryDirectory(prefix="cathedral-cc-gpu-verify-")
            )
            paths: dict[str, str] = {}
            for name, raw in artifacts.items():
                path = os.path.join(temp_dir, f"{name}.bin")
                artifact_descriptor = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(artifact_descriptor, "wb") as handle:
                    handle.write(raw)
                paths[name] = path
            receipt_path = os.path.join(temp_dir, "receipt.json")
            result_path = os.path.join(temp_dir, "result.json")
            with open(receipt_path, "xb") as handle:
                handle.write(canonical_json(document))
            replacements = {
                "{phase}": phase,
                "{bundle_path}": paths["bundle"],
                "{cpu_evidence_path}": paths["cpu_evidence"],
                "{gpu_evidence_path}": paths["gpu_evidence"],
                "{gpu_identity_set_path}": paths["gpu_identity_set"],
                "{secret_release_grant_path}": paths["secret_release_grant"],
                "{deletion_evidence_path}": paths["deletion_evidence"],
                "{receipt_path}": receipt_path,
                "{result_path}": result_path,
            }
            argv: list[str] = []
            for original in tokens:
                token = original
                for placeholder, replacement in replacements.items():
                    token = token.replace(placeholder, replacement)
                argv.append(token)
            argv[0] = f"/proc/self/fd/{verifier_descriptor}"
            remaining = (
                float(timeout_seconds)
                if deadline_monotonic is None
                else min(float(timeout_seconds), deadline_monotonic - time.monotonic())
            )
            if remaining <= 0:
                raise ThinSubnetError("CC GPU verification batch deadline exceeded")
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={
                        "LANG": "C",
                        "LC_ALL": "C",
                        "PATH": "/usr/bin:/bin",
                        "TZ": "UTC",
                    },
                    pass_fds=(verifier_descriptor,),
                    start_new_session=True,
                )
                try:
                    returncode = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired as exc:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    finally:
                        process.wait()
                    raise ThinSubnetError(
                        "CC GPU verifier execution timed out"
                    ) from exc
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ThinSubnetError("CC GPU verifier execution failed") from exc
            if returncode != 0:
                raise ThinSubnetError(f"CC GPU verifier exited {returncode}")
            result_raw = _read_regular_file(
                Path(result_path),
                maximum=MAX_CC_GPU_VERIFIER_RESULT_BYTES,
                label="CC GPU verifier result",
            )
            result = parse_strict_json(
                result_raw, maximum_bytes=MAX_CC_GPU_VERIFIER_RESULT_BYTES
            )
            if result_raw != canonical_json(result):
                raise ThinSubnetError("CC GPU verifier result must be canonical JSON")
            _exact_keys(result, _VERIFIER_RESULT_KEYS, "CC GPU verifier result")
            reason = result["reason"]
            if not isinstance(reason, str) or len(reason.encode("utf-8")) > 512:
                raise ThinSubnetError("CC GPU verifier reason is invalid")
            return CcGpuEvidenceVerification(
                verifier_digest=actual_digest,
                reason=reason,
                **{key: result[key] for key in _VERIFIER_RESULT_KEYS - {"reason"}},
            )

    return verify


def _decode_artifacts(artifacts: Any, receipt: Mapping[str, Any]) -> dict[str, bytes]:
    if (
        not isinstance(artifacts, dict)
        or not 1 <= len(artifacts) <= MAX_CC_GPU_EXPORT_ARTIFACTS
    ):
        raise ThinSubnetError("CC GPU export artifacts must be a bounded object")
    evidence_by_digest: dict[str, bytes] = {}
    observed_kinds: dict[str, str] = {}
    for raw_digest, value in artifacts.items():
        digest = _digest(raw_digest, "artifact digest")
        if not isinstance(value, dict):
            raise ThinSubnetError("CC GPU export artifact must be an object")
        _exact_keys(value, _ARTIFACT_KEYS, "CC GPU export artifact")
        if value["encoding"] != "base64":
            raise ThinSubnetError("CC GPU export artifact encoding is unsupported")
        length = value["byte_length"]
        if (
            isinstance(length, bool)
            or not isinstance(length, int)
            or not 1 <= length <= MAX_CC_GPU_EVIDENCE_BYTES
        ):
            raise ThinSubnetError("CC GPU export artifact byte length is invalid")
        encoded = value["data_base64"]
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (TypeError, binascii.Error, ValueError) as exc:
            raise ThinSubnetError(
                "CC GPU export artifact is not canonical base64"
            ) from exc
        if (
            len(raw) != length
            or base64.b64encode(raw).decode("ascii") != encoded
            or sha256_digest(raw) != digest
        ):
            raise ThinSubnetError(
                "CC GPU export artifact length or digest is mismatched"
            )
        kinds = value["kinds"]
        if (
            not isinstance(kinds, list)
            or not kinds
            or any(not isinstance(kind, str) for kind in kinds)
            or kinds != sorted(set(kinds))
            or any(kind not in _KIND_FIELDS for kind in kinds)
        ):
            raise ThinSubnetError("CC GPU export artifact kinds are invalid")
        for kind in kinds:
            if kind in observed_kinds:
                raise ThinSubnetError("CC GPU export artifact kind is duplicated")
            observed_kinds[kind] = digest
        evidence_by_digest[digest] = raw
    if frozenset(observed_kinds) != frozenset(_KIND_FIELDS):
        raise ThinSubnetError("CC GPU export is missing required artifact kinds")
    for kind, field in _KIND_FIELDS.items():
        if observed_kinds[kind] != receipt.get(field):
            raise ThinSubnetError(
                f"CC GPU export {kind} does not match the receipt digest"
            )
    expected_digests = {receipt[field] for field in _KIND_FIELDS.values()}
    if set(evidence_by_digest) != expected_digests:
        raise ThinSubnetError("CC GPU export contains unreferenced artifacts")
    return evidence_by_digest


def verify_cc_gpu_evidence_export(
    raw: bytes,
    receipt_policy: CcGpuReceiptPolicy,
    evidence_verifier: CcGpuEvidenceVerifier,
    *,
    expected_receipt_id: str | None = None,
    expected_receipt_digest: str | None = None,
    now: datetime | None = None,
    deadline_monotonic: float | None = None,
) -> VerifiedCcGpuReceipt:
    document = parse_strict_json(raw, maximum_bytes=MAX_CC_GPU_EXPORT_BYTES)
    _exact_keys(document, _EXPORT_KEYS, "CC GPU evidence export")
    if document["schema"] != CC_GPU_EVIDENCE_EXPORT_SCHEMA:
        raise ThinSubnetError("unsupported CC GPU evidence export schema")
    if document["authentication"] != _AUTHENTICATION:
        raise ThinSubnetError("CC GPU export lacks owner-scoped authentication")
    if document["integrity"] != _INTEGRITY:
        raise ThinSubnetError("CC GPU export integrity contract is invalid")
    receipt = document["receipt"]
    if not isinstance(receipt, dict):
        raise ThinSubnetError("CC GPU export receipt must be an object")
    receipt_raw = canonical_json(receipt)
    receipt_id = document["receipt_id"]
    if not isinstance(receipt_id, str) or receipt_id != receipt.get("receipt_id"):
        raise ThinSubnetError("CC GPU export receipt id is mismatched")
    if expected_receipt_id is not None and receipt_id != expected_receipt_id:
        raise ThinSubnetError("CC GPU export does not match score evidence id")
    receipt_digest = sha256_digest(receipt_raw)
    if (
        expected_receipt_digest is not None
        and receipt_digest != expected_receipt_digest
    ):
        raise ThinSubnetError("CC GPU export does not match score evidence digest")
    evidence_by_digest = _decode_artifacts(document["artifacts"], receipt)
    verified = verify_cc_gpu_receipt(
        receipt_raw,
        receipt_policy,
        now=now,
        evidence_by_digest=evidence_by_digest,
        evidence_verifier=evidence_verifier,
        deadline_monotonic=deadline_monotonic,
    )
    if verified.receipt_id != receipt_id or verified.receipt_digest != receipt_digest:
        raise ThinSubnetError("CC GPU verified receipt differs from its export")
    return verified


def _origin(value: str) -> str:
    if not isinstance(value, str):
        raise ThinSubnetError("CC GPU export URL must be credential-free HTTPS")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ThinSubnetError(
            "CC GPU export URL must be credential-free HTTPS"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ThinSubnetError("CC GPU export URL must be credential-free HTTPS")
    authority = parsed.hostname.lower()
    if port is not None:
        authority = f"{authority}:{port}"
    return f"https://{authority}"


class CcGpuReceiptLoader:
    """Load signed receipts and digest-keyed evidence for one verified report."""

    def __init__(
        self,
        *,
        receipt_policy: CcGpuReceiptPolicy,
        evidence_verifier: CcGpuEvidenceVerifier,
        local_export_directory: str | Path | None = None,
        allowed_https_origins: Iterable[str] = (),
        bearer_token: str | None = None,
        timeout_seconds: int = 10,
        batch_deadline_seconds: int = 120,
        config_digest: str | None = None,
    ) -> None:
        self.receipt_policy = receipt_policy
        self.evidence_verifier = evidence_verifier
        self.local_export_directory = (
            Path(local_export_directory).expanduser().resolve()
            if local_export_directory is not None
            else None
        )
        origins = tuple(allowed_https_origins)
        if origins != tuple(sorted(set(origins))) or any(
            _origin(item) != item for item in origins
        ):
            raise ThinSubnetError(
                "CC GPU allowed HTTPS origins must be canonical and sorted"
            )
        if self.local_export_directory is None and not origins:
            raise ThinSubnetError(
                "CC GPU loader needs a local directory or HTTPS origin"
            )
        if origins and (not isinstance(bearer_token, str) or not bearer_token):
            raise ThinSubnetError("remote CC GPU exports require a bearer token")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 60
        ):
            raise ThinSubnetError("CC GPU export timeout must be 1..60 seconds")
        self.allowed_https_origins = frozenset(origins)
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        if (
            isinstance(batch_deadline_seconds, bool)
            or not isinstance(batch_deadline_seconds, int)
            or not 1 <= batch_deadline_seconds <= 600
        ):
            raise ThinSubnetError("CC GPU batch deadline must be 1..600 seconds")
        self.batch_deadline_seconds = batch_deadline_seconds
        self.config_digest = config_digest

    def _read_local(self, receipt_id: str) -> bytes:
        if self.local_export_directory is None:
            raise ThinSubnetError("CC GPU score evidence URI is required")
        suffix = receipt_id.removeprefix("cc-gpu-receipt-sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", suffix):
            raise ThinSubnetError("invalid CC GPU receipt id for local export")
        path = self.local_export_directory / f"{suffix}.evidence.json"
        return _read_regular_file(
            path, maximum=MAX_CC_GPU_EXPORT_BYTES, label="CC GPU evidence export"
        )

    def _fetch_remote(self, location: str, *, deadline_monotonic: float) -> bytes:
        if _origin(location) not in self.allowed_https_origins:
            raise ThinSubnetError("CC GPU export origin is not validator-pinned")
        request = urllib.request.Request(
            location,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.bearer_token}",
            },
            method="GET",
        )
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            raise ThinSubnetError("CC GPU verification batch deadline exceeded")
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=min(float(self.timeout_seconds), remaining)
            ) as response:
                content_type = str(response.headers.get("Content-Type") or "")
                if (
                    not content_type.lower().split(";", 1)[0].strip()
                    == "application/json"
                ):
                    raise ThinSubnetError("CC GPU export response is not JSON")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as exc:
                        raise ThinSubnetError(
                            "invalid CC GPU export Content-Length"
                        ) from exc
                    if declared <= 0 or declared > MAX_CC_GPU_EXPORT_BYTES:
                        raise ThinSubnetError("CC GPU export exceeds size limit")
                chunks: list[bytes] = []
                total = 0
                while True:
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        raise ThinSubnetError(
                            "CC GPU verification batch deadline exceeded"
                        )
                    try:
                        response.fp.raw._sock.settimeout(  # type: ignore[attr-defined]
                            min(float(self.timeout_seconds), remaining)
                        )
                    except (AttributeError, OSError) as exc:
                        raise ThinSubnetError(
                            "could not enforce CC GPU export deadline"
                        ) from exc
                    chunk = response.read1(
                        min(65_536, MAX_CC_GPU_EXPORT_BYTES + 1 - total)
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_CC_GPU_EXPORT_BYTES:
                        raise ThinSubnetError("CC GPU export exceeds size limit")
                raw = b"".join(chunks)
        except ThinSubnetError:
            raise
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            socket.timeout,
        ) as exc:
            raise ThinSubnetError(
                "could not fetch authenticated CC GPU export"
            ) from exc
        if not raw or len(raw) > MAX_CC_GPU_EXPORT_BYTES:
            raise ThinSubnetError("CC GPU export exceeds size limit")
        return raw

    def load_raw(
        self,
        raw: bytes,
        *,
        expected_receipt_id: str | None = None,
        expected_receipt_digest: str | None = None,
        now: datetime | None = None,
        deadline_monotonic: float | None = None,
    ) -> VerifiedCcGpuReceipt:
        return verify_cc_gpu_evidence_export(
            raw,
            self.receipt_policy,
            self.evidence_verifier,
            expected_receipt_id=expected_receipt_id,
            expected_receipt_digest=expected_receipt_digest,
            now=now,
            deadline_monotonic=deadline_monotonic,
        )

    def load_paths(
        self, paths: Iterable[str | Path], *, now: datetime | None = None
    ) -> tuple[VerifiedCcGpuReceipt, ...]:
        materialized = list(itertools.islice(paths, MAX_CC_GPU_RECEIPTS_PER_REPORT + 1))
        if not 1 <= len(materialized) <= MAX_CC_GPU_RECEIPTS_PER_REPORT:
            raise ThinSubnetError("CC GPU acceptance requires 1..128 exports")
        deadline = time.monotonic() + self.batch_deadline_seconds
        verified: list[VerifiedCcGpuReceipt] = []
        total_bytes = 0
        for path in materialized:
            if time.monotonic() >= deadline:
                raise ThinSubnetError("CC GPU verification batch deadline exceeded")
            raw = _read_regular_file(
                Path(path).expanduser(),
                maximum=MAX_CC_GPU_EXPORT_BYTES,
                label="CC GPU evidence export",
            )
            total_bytes += len(raw)
            if total_bytes > MAX_CC_GPU_EXPORT_BYTES_PER_REPORT:
                raise ThinSubnetError(
                    "CC GPU evidence batch exceeds aggregate size limit"
                )
            verified.append(self.load_raw(raw, now=now, deadline_monotonic=deadline))
        return verify_unique_cc_gpu_receipts(verified)

    def __call__(
        self,
        class_policy: ExternalClassPolicy,
        report: VerifiedReport,
        *,
        block: int,
    ) -> Mapping[str, VerifiedCcGpuReceipt]:
        del block
        references = []
        seen_ids: set[str] = set()
        for entry in report.entries:
            if assignment_score(entry, class_policy.assignment) <= 0:
                continue
            for reference in entry.evidence:
                if reference.kind != CC_GPU_RECEIPT_SCHEMA:
                    continue
                if reference.id in seen_ids:
                    raise ThinSubnetError("duplicate CC GPU export reference")
                seen_ids.add(reference.id)
                references.append(reference)
        if len(references) > MAX_CC_GPU_RECEIPTS_PER_REPORT:
            raise ThinSubnetError("CC GPU score report exceeds receipt limit")
        deadline = time.monotonic() + self.batch_deadline_seconds
        verified: list[VerifiedCcGpuReceipt] = []
        total_bytes = 0
        for reference in references:
            if time.monotonic() >= deadline:
                raise ThinSubnetError("CC GPU verification batch deadline exceeded")
            raw = (
                self._fetch_remote(reference.uri, deadline_monotonic=deadline)
                if reference.uri is not None
                else self._read_local(reference.id)
            )
            total_bytes += len(raw)
            if total_bytes > MAX_CC_GPU_EXPORT_BYTES_PER_REPORT:
                raise ThinSubnetError(
                    "CC GPU evidence batch exceeds aggregate size limit"
                )
            verified.append(
                self.load_raw(
                    raw,
                    expected_receipt_id=reference.id,
                    expected_receipt_digest=reference.digest,
                    deadline_monotonic=deadline,
                )
            )
        checked = verify_unique_cc_gpu_receipts(verified)
        return {item.receipt_id: item for item in checked}


def _string_list(value: Any, label: str) -> frozenset[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise ThinSubnetError(f"{label} must be a nonempty sorted unique list")
    return frozenset(value)


def _receipt_policy(value: Any) -> CcGpuReceiptPolicy:
    if not isinstance(value, dict):
        raise ThinSubnetError("CC GPU receipt policy must be an object")
    _exact_keys(value, _RECEIPT_POLICY_KEYS, "CC GPU receipt policy")
    trusted_raw = value["trusted_signing_keys"]
    if not isinstance(trusted_raw, dict) or not trusted_raw:
        raise ThinSubnetError("CC GPU trusted signing keys must be an object")
    trusted: dict[str, CcGpuTrustedSigningKey] = {}
    for key_id, item in trusted_raw.items():
        if not isinstance(key_id, str) or not isinstance(item, dict):
            raise ThinSubnetError("CC GPU trusted signing key is invalid")
        _exact_keys(item, _TRUSTED_KEY_KEYS, "CC GPU trusted signing key")
        encoded = item["public_key_base64"]
        try:
            public_key = base64.b64decode(encoded, validate=True)
        except (TypeError, binascii.Error, ValueError) as exc:
            raise ThinSubnetError("CC GPU signing public key is not base64") from exc
        if (
            len(public_key) != 32
            or base64.b64encode(public_key).decode("ascii") != encoded
        ):
            raise ThinSubnetError("CC GPU signing public key must be 32 bytes")
        Ed25519PublicKey.from_public_bytes(public_key).public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        trusted[key_id] = CcGpuTrustedSigningKey(
            public_key,
            _canonical_time(item["valid_from"], "signing key valid_from"),
            _canonical_time(item["valid_until"], "signing key valid_until"),
        )
    for name in (
        "policy_registry_release",
        "max_age_seconds",
        "max_future_seconds",
    ):
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ThinSubnetError(f"CC GPU receipt policy {name} must be positive")
    return CcGpuReceiptPolicy(
        expected_profile_id=str(value["expected_profile_id"]),
        allowed_profile_authorities=_string_list(
            value["allowed_profile_authorities"], "allowed profile authorities"
        ),
        allowed_policy_digests=_string_list(
            value["allowed_policy_digests"], "allowed policy digests"
        ),
        allowed_image_digests=_string_list(
            value["allowed_image_digests"], "allowed image digests"
        ),
        allowed_model_digests=_string_list(
            value["allowed_model_digests"], "allowed model digests"
        ),
        policy_registry_release=value["policy_registry_release"],
        policy_registry_digest=_digest(
            value["policy_registry_digest"], "policy registry digest"
        ),
        policy_registry_valid_from=_canonical_time(
            value["policy_registry_valid_from"], "policy registry valid_from"
        ),
        policy_registry_valid_until=_canonical_time(
            value["policy_registry_valid_until"], "policy registry valid_until"
        ),
        trusted_signing_keys=trusted,
        allowed_verifier_digests=_string_list(
            value["allowed_verifier_digests"], "allowed verifier digests"
        ),
        max_age_seconds=value["max_age_seconds"],
        max_future_seconds=value["max_future_seconds"],
    )


def load_cc_gpu_loader_config(path: str | Path) -> CcGpuReceiptLoader:
    config_path = Path(path).expanduser()
    raw = _read_regular_file(
        config_path, maximum=1_048_576, label="CC GPU loader config"
    )
    document = parse_strict_json(raw, maximum_bytes=1_048_576)
    if raw != canonical_json(document):
        raise ThinSubnetError("CC GPU loader config must be canonical JSON")
    _exact_keys(document, _CONFIG_KEYS, "CC GPU loader config")
    if document["schema"] != CC_GPU_LOADER_CONFIG_SCHEMA:
        raise ThinSubnetError("unsupported CC GPU loader config schema")
    if (
        not isinstance(document["receipt_policy"], dict)
        or document["receipt_policy"].get("expected_profile_id") != CC_GPU_PROFILE_ID
    ):
        raise ThinSubnetError("CC GPU loader config profile is unsupported")
    command = document["verifier_command"]
    if not isinstance(command, list):
        raise ThinSubnetError("CC GPU verifier command must be a token list")
    timeout = document["verifier_timeout_seconds"]
    verifier = subprocess_cc_gpu_evidence_verifier(
        command,
        expected_verifier_digest=document["verifier_executable_digest"],
        timeout_seconds=timeout,
    )
    receipt_policy = _receipt_policy(document["receipt_policy"])
    if (
        document["verifier_executable_digest"]
        not in receipt_policy.allowed_verifier_digests
    ):
        raise ThinSubnetError(
            "CC GPU verifier executable is not allowed by receipt policy"
        )
    local_directory = document["local_export_directory"]
    if local_directory is not None:
        if not isinstance(local_directory, str) or not local_directory:
            raise ThinSubnetError("CC GPU local export directory is invalid")
        local_path = Path(local_directory).expanduser()
        if not local_path.is_absolute():
            local_path = config_path.parent / local_path
        local_directory = local_path.resolve()
    origins = document["allowed_https_origins"]
    if not isinstance(origins, list):
        raise ThinSubnetError("CC GPU allowed HTTPS origins must be a list")
    bearer_env = document["authorization_bearer_env"]
    if bearer_env is not None and (
        not isinstance(bearer_env, str) or _ENV_NAME_RE.fullmatch(bearer_env) is None
    ):
        raise ThinSubnetError("CC GPU bearer environment variable name is invalid")
    bearer_token = os.environ.get(bearer_env, "") if bearer_env is not None else None
    return CcGpuReceiptLoader(
        receipt_policy=receipt_policy,
        evidence_verifier=verifier,
        local_export_directory=local_directory,
        allowed_https_origins=origins,
        bearer_token=bearer_token,
        batch_deadline_seconds=document["batch_deadline_seconds"],
        config_digest=sha256_digest(raw),
    )
