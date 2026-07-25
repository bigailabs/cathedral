"""Separate, bounded authorization for recurring SN39 mainnet writes.

The root-signed public launch seal proves one historical launch write.  It does
not grant a daemon permission to keep writing.  This module verifies a second,
operator-created authorization whose exact bytes are signed by the pinned
release-attestation key and whose scope is limited by signer, chain, release,
submission journal and lane, time, finalized blocks, a durable attempt count,
and a rollback-resistant validator account-nonce interval.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "cathedral_sn39_recurring_write_authorization_v1"
SIGNATURE_SCHEMA = "cathedral_sn39_recurring_write_signature_v1"
PURPOSE = "authorize bounded recurring set_mechanism_weights writes"
RELEASE_KEY_ID = "cathedral-release-attestation-sn39-20260724"
RELEASE_PUBLIC_KEY_BASE64 = "+yUHLO30+pc0ymdwLbqu+Y4aR4vxM2iGxrfBNEpLwd0="
PRIVATE_SEED = Path("/etc/cathedral/release-attestation-signing-sn39-20260724.key")
RELEASE_MANIFEST = Path("/etc/cathedral/sn39-release-manifest.json")
AUTHORIZATION_PATH = Path("/etc/cathedral/sn39-recurring-write-authorization.json")
SIGNATURE_PATH = Path("/etc/cathedral/sn39-recurring-write-authorization.json.sig")
AUTHORIZER_CONTEXT_ENV = "CATHEDRAL_SN39_RECURRING_AUTHORIZER_CONTEXT"
RELEASE_SHA_ENV = "CATHEDRAL_SN39_RELEASE_SHA"
RUNTIME_ROOT = "/var/lib/cathedral-validator"
STATE_FILE = "/var/lib/cathedral-validator/thin-state.json"
FINNEY_GENESIS_HASH = (
    "0x2f0555cc76fc2840a25a6ea3b9637146806f1f44b090c175ffde2a7e5ab36c03"
)
MECHANISM = "validated_supply_v1"
MAX_ATTEMPTS = 96
MAX_VALIDITY_SECONDS = 72 * 60 * 60
MAX_VALIDITY_BLOCKS = 21_600
MIN_REMAINING_SECONDS = 240
MIN_REMAINING_BLOCKS = 4
MAX_ARTIFACT_BYTES = 32 * 1024
ROOT_UID = 0
SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
CHAIN_HASH = re.compile(r"0x[0-9a-f]{64}")
HOTKEY = re.compile(r"[1-9A-HJ-NP-Za-km-z]{40,64}")


class AuthorizationError(RuntimeError):
    """The recurring-write authorization is absent, invalid, or out of scope."""


@dataclass(frozen=True)
class VerifiedAuthorization:
    authorization_sha256: str
    submission_journal: str
    launch_attempt_id: str
    release_sha256: str
    reproducer_revision: str
    validator_hotkey: str
    genesis_hash: str
    lanes: tuple[str, ...]
    issued_at: str
    valid_from_time: str
    valid_until_time: str
    valid_from_block: int
    valid_until_block: int
    valid_from_nonce: int
    valid_until_nonce_exclusive: int
    max_attempts: int


def canonical_json(document: Mapping[str, Any]) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_time(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise AuthorizationError("authorization time must be timezone-aware")
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + (f"{utc.microsecond // 1000:03d}Z")


def parse_time(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise AuthorizationError(f"{field} must be canonical UTC")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AuthorizationError(f"{field} must be canonical UTC") from exc
    if canonical_time(parsed) != value:
        raise AuthorizationError(f"{field} must use millisecond canonical UTC")
    return parsed


def strict_json(
    payload: bytes,
    *,
    label: str,
    require_canonical: bool = True,
) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise AuthorizationError(f"{label} has duplicate keys")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=no_duplicates,
            parse_float=lambda raw: (
                float(raw)
                if math.isfinite(float(raw))
                else (_ for _ in ()).throw(
                    AuthorizationError(f"{label} has non-finite numbers")
                )
            ),
            parse_constant=lambda _raw: (_ for _ in ()).throw(
                AuthorizationError(f"{label} has non-finite numbers")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError(f"{label} is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise AuthorizationError(f"{label} must be a JSON object")
    if require_canonical and payload != canonical_json(document) + b"\n":
        raise AuthorizationError(f"{label} is not byte-canonical JSON")
    return document


def read_root_controlled(
    path: Path,
    *,
    label: str,
    expected_uid: int = ROOT_UID,
    require_private_mode: bool = False,
    expected_mode: int | None = None,
) -> bytes:
    """Read one fixed root-controlled file without following path links."""
    if not path.is_absolute():
        raise AuthorizationError(f"{label} path must be absolute")
    try:
        parent = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise AuthorizationError(f"{label} directory is unavailable") from exc
    try:
        parent_info = os.fstat(parent)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != expected_uid
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise AuthorizationError(f"{label} directory is not root-controlled")
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            raise AuthorizationError(f"{label} is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            permissions = stat.S_IMODE(info.st_mode)
            unsafe_permissions = (
                permissions & 0o077 if require_private_mode else permissions & 0o022
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != expected_uid
                or info.st_nlink != 1
                or unsafe_permissions
                or (expected_mode is not None and permissions != expected_mode)
                or info.st_size > MAX_ARTIFACT_BYTES
            ):
                raise AuthorizationError(
                    f"{label} is not an immutable root-controlled file"
                )
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(MAX_ARTIFACT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent)
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise AuthorizationError(f"{label} exceeds its bounded size")
    return payload


def _verify_signature(
    authorization_bytes: bytes,
    signature_bytes: bytes,
    *,
    public_key_base64: str = RELEASE_PUBLIC_KEY_BASE64,
) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = strict_json(signature_bytes, label="recurring-write signature")
    expected_fields = {
        "schema",
        "algorithm",
        "key_id",
        "payload",
        "payload_sha256",
        "signature",
    }
    expected_digest = "sha256:" + hashlib.sha256(authorization_bytes).hexdigest()
    if (
        set(signature) != expected_fields
        or signature.get("schema") != SIGNATURE_SCHEMA
        or signature.get("algorithm") != "Ed25519"
        or signature.get("key_id") != RELEASE_KEY_ID
        or signature.get("payload")
        != "sn39-recurring-write-authorization.json exact bytes"
        or signature.get("payload_sha256") != expected_digest
    ):
        raise AuthorizationError("recurring-write signature envelope differs")
    try:
        public = base64.b64decode(public_key_base64, validate=True)
        detached = base64.b64decode(str(signature["signature"]), validate=True)
        if len(public) != 32:
            raise ValueError("public key length")
        Ed25519PublicKey.from_public_bytes(public).verify(detached, authorization_bytes)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(
            "recurring-write signature encoding is invalid"
        ) from exc
    except Exception as exc:
        raise AuthorizationError("recurring-write signature is invalid") from exc


def validate_document(
    document: dict[str, Any],
    *,
    expected: Mapping[str, Any],
    lane: str,
    finalized_block: int,
    now: datetime,
    authorization_sha256: str,
) -> VerifiedAuthorization:
    expected_fields = {
        "schema",
        "purpose",
        "network",
        "genesis_hash",
        "netuid",
        "validator_hotkey",
        "runtime_root",
        "state_file",
        "submission_journal",
        "mechanism",
        "call_module",
        "call_function",
        "mecid",
        "lanes",
        "launch_attempt_id",
        "release_sha256",
        "reproducer_revision",
        "issued_at",
        "valid_from_time",
        "valid_until_time",
        "valid_from_block",
        "valid_until_block",
        "valid_from_nonce",
        "valid_until_nonce_exclusive",
        "max_attempts",
    }
    if set(document) != expected_fields:
        raise AuthorizationError("recurring-write authorization fields differ")
    fixed = {
        "schema": SCHEMA,
        "purpose": PURPOSE,
        "network": "finney",
        "netuid": 39,
        "runtime_root": RUNTIME_ROOT,
        "state_file": STATE_FILE,
        "mechanism": MECHANISM,
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "mecid": 0,
    }
    for key, value in fixed.items():
        if document.get(key) != value:
            raise AuthorizationError(f"recurring-write authorization {key} differs")
    for key in (
        "submission_journal",
        "genesis_hash",
        "validator_hotkey",
        "launch_attempt_id",
        "release_sha256",
        "reproducer_revision",
    ):
        if document.get(key) != expected.get(key):
            raise AuthorizationError(f"recurring-write authorization {key} differs")
    if (
        Path(str(document["submission_journal"])).parent != Path(RUNTIME_ROOT)
        or re.fullmatch(
            r"journal-[0-9a-f]{64}\.json",
            Path(str(document["submission_journal"])).name,
        )
        is None
        or document["genesis_hash"] != FINNEY_GENESIS_HASH
        or CHAIN_HASH.fullmatch(str(document["genesis_hash"])) is None
        or HOTKEY.fullmatch(str(document["validator_hotkey"])) is None
        or SHA256.fullmatch(str(document["launch_attempt_id"])) is None
        or SHA256.fullmatch(str(document["release_sha256"])) is None
        or SHA.fullmatch(str(document["reproducer_revision"])) is None
    ):
        raise AuthorizationError("recurring-write authorization identity is malformed")
    lanes = document.get("lanes")
    if (
        not isinstance(lanes, list)
        or not lanes
        or lanes != sorted(set(lanes))
        or any(item not in {"thin", "authority"} for item in lanes)
        or lane not in lanes
    ):
        raise AuthorizationError("recurring-write authorization lane is not approved")
    max_attempts = document.get("max_attempts")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_ATTEMPTS
    ):
        raise AuthorizationError("recurring-write attempt allowance is invalid")
    for field in ("valid_from_nonce", "valid_until_nonce_exclusive"):
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AuthorizationError(f"{field} is invalid")
    valid_from_nonce = int(document["valid_from_nonce"])
    valid_until_nonce_exclusive = int(document["valid_until_nonce_exclusive"])
    if (
        valid_until_nonce_exclusive <= valid_from_nonce
        or valid_until_nonce_exclusive - valid_from_nonce != max_attempts
    ):
        raise AuthorizationError(
            "recurring-write nonce window differs from its attempt allowance"
        )
    for field in ("valid_from_block", "valid_until_block"):
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AuthorizationError(f"{field} is invalid")
    valid_from_block = int(document["valid_from_block"])
    valid_until_block = int(document["valid_until_block"])
    if (
        valid_until_block <= valid_from_block
        or valid_until_block - valid_from_block > MAX_VALIDITY_BLOCKS
        or not valid_from_block <= finalized_block < valid_until_block
        or valid_until_block - finalized_block < MIN_REMAINING_BLOCKS
    ):
        raise AuthorizationError(
            "recurring-write authorization block window is invalid or expired"
        )
    if now.tzinfo is None:
        raise AuthorizationError("authorization verification clock is naive")
    now = now.astimezone(UTC)
    issued_at = parse_time(document["issued_at"], field="issued_at")
    valid_from_time = parse_time(document["valid_from_time"], field="valid_from_time")
    valid_until_time = parse_time(
        document["valid_until_time"], field="valid_until_time"
    )
    not_before = expected.get("not_before_time")
    if isinstance(not_before, str):
        not_before_time = parse_time(not_before, field="continuous enabled time")
    elif isinstance(not_before, datetime):
        if not_before.tzinfo is None:
            raise AuthorizationError("continuous enabled time is naive")
        not_before_time = not_before.astimezone(UTC)
    else:
        raise AuthorizationError("continuous enabled time is missing")
    if (
        issued_at < not_before_time
        or valid_from_time < issued_at
        or valid_until_time <= valid_from_time
        or (valid_until_time - issued_at).total_seconds() > MAX_VALIDITY_SECONDS
        or not valid_from_time <= now < valid_until_time
        or (valid_until_time - now).total_seconds() < MIN_REMAINING_SECONDS
    ):
        raise AuthorizationError(
            "recurring-write authorization time window is invalid or expired"
        )
    return VerifiedAuthorization(
        authorization_sha256=authorization_sha256,
        submission_journal=str(document["submission_journal"]),
        launch_attempt_id=str(document["launch_attempt_id"]),
        release_sha256=str(document["release_sha256"]),
        reproducer_revision=str(document["reproducer_revision"]),
        validator_hotkey=str(document["validator_hotkey"]),
        genesis_hash=str(document["genesis_hash"]),
        lanes=tuple(lanes),
        issued_at=str(document["issued_at"]),
        valid_from_time=str(document["valid_from_time"]),
        valid_until_time=str(document["valid_until_time"]),
        valid_from_block=valid_from_block,
        valid_until_block=valid_until_block,
        valid_from_nonce=valid_from_nonce,
        valid_until_nonce_exclusive=valid_until_nonce_exclusive,
        max_attempts=max_attempts,
    )


def verify_authorization(
    *,
    expected: Mapping[str, Any],
    lane: str,
    finalized_block: int,
    now: datetime | None = None,
    authorization_path: Path = AUTHORIZATION_PATH,
    signature_path: Path = SIGNATURE_PATH,
    expected_uid: int = ROOT_UID,
    public_key_base64: str = RELEASE_PUBLIC_KEY_BASE64,
) -> VerifiedAuthorization:
    if (
        isinstance(finalized_block, bool)
        or not isinstance(finalized_block, int)
        or finalized_block <= 0
    ):
        raise AuthorizationError(
            "recurring-write authorization needs a finalized block"
        )
    authorization_bytes = read_root_controlled(
        authorization_path,
        label="recurring-write authorization",
        expected_uid=expected_uid,
    )
    signature_bytes = read_root_controlled(
        signature_path,
        label="recurring-write signature",
        expected_uid=expected_uid,
    )
    _verify_signature(
        authorization_bytes,
        signature_bytes,
        public_key_base64=public_key_base64,
    )
    document = strict_json(authorization_bytes, label="recurring-write authorization")
    digest = "sha256:" + hashlib.sha256(authorization_bytes).hexdigest()
    return validate_document(
        document,
        expected=expected,
        lane=lane,
        finalized_block=finalized_block,
        now=now or datetime.now(UTC),
        authorization_sha256=digest,
    )


def assert_still_ready(
    authorization: VerifiedAuthorization,
    *,
    lane: str,
    finalized_block: int,
    now: datetime | None = None,
) -> None:
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        raise AuthorizationError("authorization verification clock is naive")
    valid_from = parse_time(authorization.valid_from_time, field="valid_from_time")
    valid_until = parse_time(authorization.valid_until_time, field="valid_until_time")
    if (
        lane not in authorization.lanes
        or not authorization.valid_from_block
        <= finalized_block
        < authorization.valid_until_block
        or authorization.valid_until_block - finalized_block < MIN_REMAINING_BLOCKS
        or not valid_from <= moment.astimezone(UTC) < valid_until
        or (valid_until - moment.astimezone(UTC)).total_seconds()
        < MIN_REMAINING_SECONDS
    ):
        raise AuthorizationError(
            "recurring-write authorization expired before the chain boundary"
        )


def assert_nonce_ready(
    authorization: VerifiedAuthorization | Any,
    *,
    account_nonce: int,
) -> None:
    """Enforce a rollback-resistant accepted-write ceiling on chain state.

    The service-owned crash journal prevents honest retries, but it can be
    restored from an old snapshot.  The validator account nonce cannot roll
    back. Binding each authorization to exactly ``max_attempts`` consecutive
    nonces therefore limits accepted writes even if local state is restored.
    Other accepted transactions consume the same conservative allowance.
    """
    if (
        isinstance(account_nonce, bool)
        or not isinstance(account_nonce, int)
        or account_nonce < authorization.valid_from_nonce
        or account_nonce >= authorization.valid_until_nonce_exclusive
    ):
        raise AuthorizationError(
            "validator account nonce is outside the recurring-write allowance"
        )


def signature_document(
    authorization_bytes: bytes,
    *,
    seed: bytes,
) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(seed) != 32:
        raise AuthorizationError("release signing seed must be exactly 32 bytes")
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    expected_public = base64.b64decode(RELEASE_PUBLIC_KEY_BASE64, validate=True)
    if public != expected_public:
        raise AuthorizationError("private release key differs from the compiled pin")
    return {
        "schema": SIGNATURE_SCHEMA,
        "algorithm": "Ed25519",
        "key_id": RELEASE_KEY_ID,
        "payload": "sn39-recurring-write-authorization.json exact bytes",
        "payload_sha256": "sha256:" + hashlib.sha256(authorization_bytes).hexdigest(),
        "signature": base64.b64encode(private.sign(authorization_bytes)).decode(
            "ascii"
        ),
    }


def _read_seed(path: Path = PRIVATE_SEED) -> bytes:
    raw = read_root_controlled(
        path,
        label="release signing seed",
        require_private_mode=True,
        expected_mode=0o600,
    ).strip()
    try:
        seed = base64.b64decode(raw, validate=True)
    except (TypeError, ValueError) as exc:
        raise AuthorizationError(
            "release signing seed is not canonical base64"
        ) from exc
    if len(seed) != 32:
        raise AuthorizationError("release signing seed must be exactly 32 bytes")
    return seed


def authorizer_context_digest(
    *,
    release_sha: str,
    manifest_digest: str,
    arguments: Sequence[str],
) -> str:
    payload = canonical_json(
        {
            "schema": "cathedral_sn39_recurring_authorizer_context_v1",
            "release_sha": release_sha,
            "manifest_digest": manifest_digest,
            "arguments": list(arguments),
        }
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def require_launcher_context(
    arguments: Sequence[str],
    *,
    manifest_path: Path = RELEASE_MANIFEST,
    expected_uid: int = ROOT_UID,
) -> None:
    """Reject direct entry outside the verified immutable release launcher."""
    release_sha = os.environ.pop(RELEASE_SHA_ENV, "")
    supplied = os.environ.pop(AUTHORIZER_CONTEXT_ENV, "")
    if SHA.fullmatch(release_sha) is None:
        raise AuthorizationError("immutable release launcher identity is missing")
    manifest = read_root_controlled(
        manifest_path,
        label="release install manifest",
        expected_uid=expected_uid,
    )
    expected = authorizer_context_digest(
        release_sha=release_sha,
        manifest_digest="sha256:" + hashlib.sha256(manifest).hexdigest(),
        arguments=arguments,
    )
    if not hmac.compare_digest(supplied, expected):
        raise AuthorizationError(
            "recurring authorizer was not entered through the immutable launcher"
        )


def _read_journal(path: Path) -> dict[str, Any]:
    if (
        not path.is_absolute()
        or path.parent != Path(RUNTIME_ROOT)
        or re.fullmatch(r"journal-[0-9a-f]{64}\.json", path.name) is None
    ):
        raise AuthorizationError("journal path is outside the fixed runtime root")
    try:
        parent = os.open(
            path.parent,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise AuthorizationError("continuous journal directory is unavailable") from exc
    try:
        parent_info = os.fstat(parent)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) & 0o077
        ):
            raise AuthorizationError(
                "continuous journal directory is not owner-controlled"
            )
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent,
            )
        except OSError as exc:
            raise AuthorizationError("continuous journal is unavailable") from exc
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != parent_info.st_uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 4 * 1024 * 1024
            ):
                raise AuthorizationError("continuous journal is not safely controlled")
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                payload = handle.read(4 * 1024 * 1024 + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent)
    if len(payload) > 4 * 1024 * 1024:
        raise AuthorizationError("continuous journal exceeds its bounded size")
    return strict_json(
        payload,
        label="continuous journal",
        require_canonical=False,
    )


def build_from_journal(
    state: Mapping[str, Any],
    *,
    journal_path: Path,
    expected_validator_hotkey: str,
    reviewed_finalized_block: int,
    reviewed_validator_nonce: int,
    max_attempts: int,
    valid_for_blocks: int,
    valid_for_seconds: int,
    allow_authority_lane: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    if (
        not journal_path.is_absolute()
        or journal_path.parent != Path(RUNTIME_ROOT)
        or re.fullmatch(r"journal-[0-9a-f]{64}\.json", journal_path.name) is None
    ):
        raise AuthorizationError("journal path is outside the fixed runtime root")
    if (
        state.get("submission_continuous_enabled") is not True
        or state.get("submission_launch_status") != "finalized"
        or state.get("submission_continuous_launch_attempt_id")
        != state.get("submission_launch_attempt_id")
    ):
        raise AuthorizationError(
            "journal has no independently reconciled finalized launch"
        )
    if (
        state.get("submission_validator_hotkey") != expected_validator_hotkey
        or HOTKEY.fullmatch(expected_validator_hotkey) is None
    ):
        raise AuthorizationError("reviewed validator hotkey differs from the journal")
    journal_identity = {
        "genesis_hash": str(state.get("submission_genesis_hash", "")).lower(),
        "netuid": 39,
        "validator_hotkey": expected_validator_hotkey,
    }
    expected_journal = Path(RUNTIME_ROOT) / (
        "journal-"
        + hashlib.sha256(canonical_json(journal_identity)).hexdigest()
        + ".json"
    )
    if journal_path != expected_journal:
        raise AuthorizationError(
            "journal filename differs from its canonical chain and signer identity"
        )
    if (
        isinstance(reviewed_finalized_block, bool)
        or not isinstance(reviewed_finalized_block, int)
        or reviewed_finalized_block <= 0
        or isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= MAX_ATTEMPTS
        or isinstance(reviewed_validator_nonce, bool)
        or not isinstance(reviewed_validator_nonce, int)
        or reviewed_validator_nonce < 0
        or isinstance(valid_for_blocks, bool)
        or not isinstance(valid_for_blocks, int)
        or not MIN_REMAINING_BLOCKS <= valid_for_blocks <= MAX_VALIDITY_BLOCKS
        or isinstance(valid_for_seconds, bool)
        or not isinstance(valid_for_seconds, int)
        or not MIN_REMAINING_SECONDS <= valid_for_seconds <= MAX_VALIDITY_SECONDS
    ):
        raise AuthorizationError("recurring-write bounds are invalid")
    issued_at = canonical_time(moment)
    return {
        "schema": SCHEMA,
        "purpose": PURPOSE,
        "network": "finney",
        "genesis_hash": state.get("submission_genesis_hash"),
        "netuid": 39,
        "validator_hotkey": expected_validator_hotkey,
        "runtime_root": RUNTIME_ROOT,
        "state_file": STATE_FILE,
        "submission_journal": str(journal_path),
        "mechanism": MECHANISM,
        "call_module": "SubtensorModule",
        "call_function": "set_mechanism_weights",
        "mecid": 0,
        "lanes": ["authority", "thin"] if allow_authority_lane else ["thin"],
        "launch_attempt_id": state.get("submission_launch_attempt_id"),
        "release_sha256": state.get("submission_continuous_release_sha256"),
        "reproducer_revision": state.get("submission_continuous_reproducer_revision"),
        "issued_at": issued_at,
        "valid_from_time": issued_at,
        "valid_until_time": canonical_time(
            moment + timedelta(seconds=valid_for_seconds)
        ),
        "valid_from_block": reviewed_finalized_block,
        "valid_until_block": reviewed_finalized_block + valid_for_blocks,
        "valid_from_nonce": reviewed_validator_nonce,
        "valid_until_nonce_exclusive": reviewed_validator_nonce + max_attempts,
        "max_attempts": max_attempts,
    }


def verify_journal_public_release(state: Mapping[str, Any]) -> dict[str, Any]:
    """Independently bind a service journal to the signed public launch seal.

    The validator service owns its journal, so a root operator must not create
    recurring authority from those mutable fields alone.  Reproduce the
    separately signed public release and require it to name this exact launch
    before the private release key is read.
    """
    identity = state.get("submission_launch_identity")
    if not isinstance(identity, dict):
        raise AuthorizationError("journal has no exact finalized launch identity")
    try:
        from scaffold import sn39_public_reproduction, validator_thin

        public_result = sn39_public_reproduction.verify_public_release()
        seal = validator_thin._match_signed_public_release_to_launch(
            public_result=public_result,
            state=dict(state),
            identity=identity,
        )
    except AuthorizationError:
        raise
    except Exception as exc:
        raise AuthorizationError(
            "journal does not reproduce against the signed public launch release"
        ) from exc
    if seal.get("release_sha256") != state.get(
        "submission_continuous_release_sha256"
    ) or seal.get("reproducer_revision") != state.get(
        "submission_continuous_reproducer_revision"
    ):
        raise AuthorizationError(
            "journal recurring transition differs from the signed public release"
        )
    return seal


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    if path.parent != AUTHORIZATION_PATH.parent:
        raise AuthorizationError("authorization output directory differs")
    parent = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    temp_name = path.name + ".tmp"
    try:
        info = os.fstat(parent)
        if (
            info.st_uid != ROOT_UID
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise AuthorizationError(
                "authorization output directory is not root-controlled"
            )
        try:
            os.unlink(temp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        if not replace:
            try:
                existing = os.open(
                    path.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent,
                )
            except FileNotFoundError:
                existing = -1
            if existing >= 0:
                os.close(existing)
                raise AuthorizationError(
                    "authorization already exists; renewal requires --replace-existing"
                )
        descriptor = os.open(
            temp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o644,
            dir_fd=parent,
        )
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        os.replace(temp_name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temp_name, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a separate bounded SN39 recurring-write authorization"
    )
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--expected-validator-hotkey", required=True)
    parser.add_argument("--reviewed-finalized-block", type=int, required=True)
    parser.add_argument("--reviewed-validator-nonce", type=int, required=True)
    parser.add_argument("--max-attempts", type=int, required=True)
    parser.add_argument("--valid-for-blocks", type=int, required=True)
    parser.add_argument("--valid-for-seconds", type=int, required=True)
    parser.add_argument("--allow-full-authority-writes", action="store_true")
    parser.add_argument("--replace-existing", action="store_true")
    parser.add_argument(
        "--i-authorize-recurring-mainnet-writes",
        action="store_true",
        help="required explicit acknowledgement; this command itself never writes chain",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(argv if argv is not None else sys.argv[1:])
    args = build_parser().parse_args(raw_arguments)
    if os.geteuid() != ROOT_UID:
        print("recurring authorization must be created by root", file=sys.stderr)
        return 1
    if not args.i_authorize_recurring_mainnet_writes:
        print(
            "refusing: --i-authorize-recurring-mainnet-writes is required",
            file=sys.stderr,
        )
        return 2
    try:
        require_launcher_context(raw_arguments)
        state = _read_journal(args.journal)
        verify_journal_public_release(state)
        moment = datetime.now(UTC)
        document = build_from_journal(
            state,
            journal_path=args.journal,
            expected_validator_hotkey=args.expected_validator_hotkey,
            reviewed_finalized_block=args.reviewed_finalized_block,
            reviewed_validator_nonce=args.reviewed_validator_nonce,
            max_attempts=args.max_attempts,
            valid_for_blocks=args.valid_for_blocks,
            valid_for_seconds=args.valid_for_seconds,
            allow_authority_lane=args.allow_full_authority_writes,
            now=moment,
        )
        authorization_bytes = canonical_json(document) + b"\n"
        validate_document(
            document,
            expected={
                "submission_journal": str(args.journal),
                "genesis_hash": state.get("submission_genesis_hash"),
                "validator_hotkey": state.get("submission_validator_hotkey"),
                "launch_attempt_id": state.get("submission_launch_attempt_id"),
                "release_sha256": state.get("submission_continuous_release_sha256"),
                "reproducer_revision": state.get(
                    "submission_continuous_reproducer_revision"
                ),
                "not_before_time": state.get("submission_continuous_enabled_at"),
            },
            lane="thin",
            finalized_block=args.reviewed_finalized_block,
            now=moment,
            authorization_sha256=(
                "sha256:" + hashlib.sha256(authorization_bytes).hexdigest()
            ),
        )
        signature_bytes = (
            canonical_json(signature_document(authorization_bytes, seed=_read_seed()))
            + b"\n"
        )
        # The detached signature is written first. A crash between files leaves
        # a mismatched pair that the daemon rejects; it can never broaden scope.
        _atomic_write(
            SIGNATURE_PATH,
            signature_bytes,
            replace=args.replace_existing,
        )
        _atomic_write(
            AUTHORIZATION_PATH,
            authorization_bytes,
            replace=args.replace_existing,
        )
    except AuthorizationError as exc:
        print(f"recurring authorization refused: {exc}", file=sys.stderr)
        return 1
    print(
        "recurring authorization created "
        + "sha256:"
        + hashlib.sha256(authorization_bytes).hexdigest()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
