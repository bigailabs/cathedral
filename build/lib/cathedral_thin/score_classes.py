"""Validator-owned score classes with signed, replay-safe provenance.

External systems may report facts and asserted scores, but they never receive
the validator wallet or the ability to submit weights.  A validator pins each
source key, chooses class allocations and assignment rules locally, and
deterministically composes the final hotkey vector.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .core import ThinSubnetError, coldkey_collapsed_weights


REPORT_SCHEMA = "cathedral_score_class_report_v1"
POLICY_SCHEMA = "cathedral_score_policy_v1"
DECISION_SCHEMA = "cathedral_weight_decision_v1"
REGISTRATION_SCHEMA = "cathedral_owner_score_registration_v1"
REPORT_DOMAIN = b"cathedral-score-class-report-v1\x00"
REPORT_ID_DOMAIN = b"cathedral-score-class-id-v1\x00"
POLICY_DOMAIN = b"cathedral-score-policy-v1\x00"
DECISION_DOMAIN = b"cathedral-weight-decision-v1\x00"
REGISTRATION_DOMAIN = b"cathedral-owner-score-registration-v1\x00"
REGISTRATION_ID_DOMAIN = b"cathedral-owner-score-registration-id-v1\x00"
MAX_REPORT_BYTES = 1_048_576
MAX_REPORT_ENTRIES = 4096
MAX_EVIDENCE_PER_ENTRY = 32
MAX_METRICS_PER_ENTRY = 32

_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST_RE = re.compile(r"(?:sha256|receipt-sha256):[0-9a-f]{64}")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,12})?")
_TIME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z"
)
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

_REPORT_KEYS = frozenset(
    {
        "schema",
        "network",
        "netuid",
        "class_id",
        "source_id",
        "source_epoch",
        "generated_at",
        "valid_until",
        "valid_from_block",
        "valid_until_block",
        "complete",
        "policy_digest",
        "verifier_digest",
        "previous_report_id",
        "entries",
        "signing_key_id",
        "report_id",
        "signature",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "miner_hotkey",
        "metrics",
        "asserted_score",
        "reason_codes",
        "evidence",
    }
)
_EVIDENCE_KEYS = frozenset({"kind", "id", "digest", "uri"})
_SIGNATURE_KEYS = frozenset({"algorithm", "value_base64"})
_POLICY_KEYS = frozenset({"schema", "network", "netuid", "classes"})
_POLICY_OPTIONAL_KEYS = frozenset({"burn_hotkey"})
_LOCAL_CLASS_KEYS = frozenset({"class_id", "kind", "allocation"})
_EXTERNAL_CLASS_KEYS = frozenset(
    {
        "class_id",
        "kind",
        "allocation",
        "source_id",
        "locations",
        "trusted_keys",
        "max_age_seconds",
        "max_future_seconds",
        "max_block_span",
        "require_evidence",
        "assignment",
    }
)
_REGISTERED_EXTERNAL_CLASS_KEYS = frozenset(
    {
        "class_id",
        "kind",
        "allocation",
        "source_id",
        "locations",
        "max_age_seconds",
        "max_future_seconds",
        "max_block_span",
        "require_evidence",
        "assignment",
        "owner_registration",
    }
)
_OWNER_REGISTRATION_POLICY_KEYS = frozenset(
    {
        "source_netuid",
        "locations",
        "max_age_seconds",
        "max_future_seconds",
        "max_block_span",
        "require_target_registration",
    }
)
_REGISTRATION_KEYS = frozenset(
    {
        "schema",
        "network",
        "source_netuid",
        "target_netuid",
        "owner_coldkey",
        "delegate_hotkey",
        "source_id",
        "class_ids",
        "report_locations",
        "report_keys",
        "sequence",
        "previous_registration_id",
        "issued_at",
        "expires_at",
        "valid_from_block",
        "valid_until_block",
        "registration_id",
        "signature",
    }
)
_ASSIGNMENT_KEYS = frozenset(
    {
        "mode",
        "metric",
        "transform",
        "cap",
        "required_reason_codes",
        "required_evidence_kinds",
    }
)


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ThinSubnetError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _parse_int(value: str) -> int:
    parsed = int(value)
    if not -(2**63) <= parsed <= 2**63 - 1:
        raise ThinSubnetError("JSON integer outside signed 64-bit range")
    return parsed


def parse_strict_json(
    raw: bytes, *, maximum_bytes: int = MAX_REPORT_BYTES
) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw or len(raw) > maximum_bytes:
        raise ThinSubnetError("JSON artifact is empty or exceeds the size limit")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_pairs,
            parse_int=_parse_int,
            parse_float=lambda _value: (_ for _ in ()).throw(
                ThinSubnetError("floating-point JSON is forbidden; use decimal strings")
            ),
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ThinSubnetError("non-finite JSON is forbidden")
            ),
        )
    except ThinSubnetError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ThinSubnetError("artifact is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ThinSubnetError("artifact must be a JSON object")
    return value


def canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ThinSubnetError("artifact contains a non-canonical value") from exc


def _require_exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    if frozenset(value) != expected:
        missing = sorted(expected - frozenset(value))
        unknown = sorted(frozenset(value) - expected)
        raise ThinSubnetError(
            f"{label} fields mismatch missing={missing} unknown={unknown}"
        )


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _key_id(value: Any) -> str:
    if not isinstance(value, str) or _KEY_ID_RE.fullmatch(value) is None:
        raise ThinSubnetError("invalid signing key id")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _decimal(value: Any, label: str, *, allow_zero: bool = True) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ThinSubnetError(
            f"{label} must be a canonical non-negative decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ThinSubnetError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise ThinSubnetError(f"invalid {label}")
    return parsed


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or _TIME_RE.fullmatch(value) is None:
        raise ThinSubnetError(
            f"{label} must be canonical UTC with six fractional digits"
        )
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ThinSubnetError(f"invalid {label}") from exc


def format_time(value: datetime | None = None) -> str:
    when = value or datetime.now(UTC)
    if when.tzinfo is None or when.utcoffset() != UTC.utcoffset(when):
        raise ThinSubnetError("time must be UTC")
    return when.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class EvidenceRef:
    kind: str
    id: str
    digest: str
    uri: str | None


@dataclass(frozen=True)
class ScoreEntry:
    miner_hotkey: str
    metrics: dict[str, Decimal]
    asserted_score: Decimal | None
    reason_codes: tuple[str, ...]
    evidence: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class VerifiedReport:
    class_id: str
    source_id: str
    source_epoch: int
    report_id: str
    previous_report_id: str | None
    generated_at: datetime
    valid_until: datetime
    valid_from_block: int
    valid_until_block: int
    policy_digest: str
    verifier_digest: str
    signing_key_id: str
    entries: tuple[ScoreEntry, ...]
    document: dict[str, Any]


@dataclass(frozen=True)
class AssignmentPolicy:
    mode: str
    metric: str | None
    transform: str
    cap: Decimal | None
    required_reason_codes: tuple[str, ...] = ()
    required_evidence_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalClassPolicy:
    class_id: str
    allocation: Decimal
    kind: str = "local_sat"


@dataclass(frozen=True)
class OwnerRegistrationPolicy:
    source_netuid: int
    locations: tuple[str, ...]
    max_age_seconds: int
    max_future_seconds: int
    max_block_span: int
    require_target_registration: bool


@dataclass(frozen=True)
class ExternalClassPolicy:
    class_id: str
    allocation: Decimal
    source_id: str
    locations: tuple[str, ...]
    trusted_keys: dict[str, bytes]
    max_age_seconds: int
    max_future_seconds: int
    max_block_span: int
    require_evidence: bool
    assignment: AssignmentPolicy
    owner_registration: OwnerRegistrationPolicy | None = None
    kind: str = "external"


@dataclass(frozen=True)
class RegistrationCheckpoint:
    owner_coldkey: str
    delegate_hotkey: str
    sequence: int
    registration_id: str


@dataclass(frozen=True)
class VerifiedOwnerRegistration:
    source_netuid: int
    target_netuid: int
    owner_coldkey: str
    delegate_hotkey: str
    source_id: str
    class_ids: tuple[str, ...]
    report_locations: tuple[str, ...]
    report_keys: dict[str, bytes]
    sequence: int
    previous_registration_id: str | None
    registration_id: str
    issued_at: datetime
    expires_at: datetime
    valid_from_block: int
    valid_until_block: int
    document: dict[str, Any]


@dataclass(frozen=True)
class ScorePolicy:
    network: str
    netuid: int
    classes: tuple[LocalClassPolicy | ExternalClassPolicy, ...]
    digest: str
    burn_hotkey: str | None = None

    @property
    def local_class(self) -> LocalClassPolicy | None:
        return next(
            (item for item in self.classes if isinstance(item, LocalClassPolicy)), None
        )

    @property
    def external_classes(self) -> tuple[ExternalClassPolicy, ...]:
        return tuple(
            item for item in self.classes if isinstance(item, ExternalClassPolicy)
        )


def default_score_policy(*, network: str, netuid: int) -> ScorePolicy:
    """The backward-compatible, validator-local SAT-only policy."""
    document = {
        "schema": POLICY_SCHEMA,
        "network": network,
        "netuid": netuid,
        "classes": [{"allocation": "1", "class_id": "local_sat", "kind": "local_sat"}],
    }
    raw = canonical_json(document)
    return ScorePolicy(
        network=network,
        netuid=netuid,
        classes=(LocalClassPolicy("local_sat", Decimal(1)),),
        digest="sha256:" + hashlib.sha256(POLICY_DOMAIN + raw).hexdigest(),
    )


@dataclass(frozen=True)
class SourceCheckpoint:
    source_epoch: int
    report_id: str


@dataclass(frozen=True)
class ClassDecision:
    class_id: str
    kind: str
    allocation: str
    source_id: str | None
    source_epoch: int | None
    report_id: str | None
    assignment: dict[str, Any]
    raw_scores: dict[str, float]
    normalized_weights: dict[str, float]
    provenance: dict[str, Any]


def _decode_public_key(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ThinSubnetError("public key must be canonical base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThinSubnetError("public key must be canonical base64") from exc
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
        raise ThinSubnetError("Ed25519 public key must be exactly 32 bytes")
    return raw


def _parse_assignment(value: Any) -> AssignmentPolicy:
    if not isinstance(value, dict):
        raise ThinSubnetError("assignment must be an object")
    _require_exact_keys(value, _ASSIGNMENT_KEYS, "assignment")
    mode = value["mode"]
    transform = value["transform"]
    metric = value["metric"]
    if mode not in {"metric", "asserted_score"}:
        raise ThinSubnetError("assignment mode must be metric or asserted_score")
    if transform not in {"linear", "binary"}:
        raise ThinSubnetError("assignment transform must be linear or binary")
    if mode == "metric":
        metric = _identifier(metric, "assignment metric")
    elif metric is not None:
        raise ThinSubnetError("asserted_score assignment metric must be null")
    cap = (
        None
        if value["cap"] is None
        else _decimal(value["cap"], "assignment cap", allow_zero=False)
    )
    reasons = value["required_reason_codes"]
    evidence_kinds = value["required_evidence_kinds"]
    if (
        not isinstance(reasons, list)
        or len(reasons) > 16
        or any(
            not isinstance(item, str) or _REASON_RE.fullmatch(item) is None
            for item in reasons
        )
        or reasons != sorted(set(reasons))
    ):
        raise ThinSubnetError("required_reason_codes must be sorted and unique")
    if (
        not isinstance(evidence_kinds, list)
        or len(evidence_kinds) > 16
        or any(
            not isinstance(item, str) or _IDENTIFIER_RE.fullmatch(item) is None
            for item in evidence_kinds
        )
        or evidence_kinds != sorted(set(evidence_kinds))
    ):
        raise ThinSubnetError("required_evidence_kinds must be sorted and unique")
    return AssignmentPolicy(
        mode=mode,
        metric=metric,
        transform=transform,
        cap=cap,
        required_reason_codes=tuple(reasons),
        required_evidence_kinds=tuple(evidence_kinds),
    )


def _locations(value: Any, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 8
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ThinSubnetError(f"{label} must contain 1..8 unique strings")
    return tuple(value)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ThinSubnetError(f"{label} must be a positive integer")
    return value


def load_score_policy(path: str | Path, *, network: str, netuid: int) -> ScorePolicy:
    policy_path = Path(path).expanduser()
    try:
        raw = policy_path.read_bytes()
    except OSError as exc:
        raise ThinSubnetError(f"could not read score policy: {policy_path}") from exc
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded)
    if encoded != canonical_json(document):
        raise ThinSubnetError("score policy JSON must be canonical")
    policy_keys = frozenset(document)
    if (
        not _POLICY_KEYS <= policy_keys
        or not (policy_keys - _POLICY_KEYS) <= _POLICY_OPTIONAL_KEYS
    ):
        missing = sorted(_POLICY_KEYS - policy_keys)
        unknown = sorted(policy_keys - _POLICY_KEYS - _POLICY_OPTIONAL_KEYS)
        raise ThinSubnetError(
            f"score policy fields mismatch missing={missing} unknown={unknown}"
        )
    if document["schema"] != POLICY_SCHEMA:
        raise ThinSubnetError("unsupported score policy schema")
    if (
        not isinstance(document["network"], str)
        or not 1 <= len(document["network"].encode("utf-8")) <= 128
        or isinstance(document["netuid"], bool)
        or not isinstance(document["netuid"], int)
        or document["netuid"] < 0
    ):
        raise ThinSubnetError("score policy network or netuid is invalid")
    if document["network"] != network or document["netuid"] != netuid:
        raise ThinSubnetError("score policy network or netuid mismatch")
    classes_raw = document["classes"]
    if not isinstance(classes_raw, list) or not 1 <= len(classes_raw) <= 32:
        raise ThinSubnetError("score policy must contain 1..32 classes")
    classes: list[LocalClassPolicy | ExternalClassPolicy] = []
    seen: set[str] = set()
    local_count = 0
    total = Decimal(0)
    for raw_class in classes_raw:
        if not isinstance(raw_class, dict):
            raise ThinSubnetError("score class policy must be an object")
        kind = raw_class.get("kind")
        if kind == "local_sat":
            expected = _LOCAL_CLASS_KEYS
        elif "owner_registration" in raw_class:
            expected = _REGISTERED_EXTERNAL_CLASS_KEYS
        else:
            expected = _EXTERNAL_CLASS_KEYS
        _require_exact_keys(raw_class, expected, "score class")
        class_id = _identifier(raw_class["class_id"], "class id")
        if class_id in seen:
            raise ThinSubnetError(f"duplicate score class: {class_id}")
        seen.add(class_id)
        allocation = _decimal(
            raw_class["allocation"], "class allocation", allow_zero=False
        )
        if allocation > 1:
            raise ThinSubnetError("class allocation cannot exceed 1")
        total += allocation
        if kind == "local_sat":
            local_count += 1
            if local_count > 1:
                raise ThinSubnetError("only one local_sat class is allowed")
            classes.append(LocalClassPolicy(class_id=class_id, allocation=allocation))
            continue
        if kind != "external":
            raise ThinSubnetError("class kind must be local_sat or external")
        source_id = _identifier(raw_class["source_id"], "source id")
        for name in ("max_age_seconds", "max_future_seconds", "max_block_span"):
            _positive_int(raw_class[name], name)
        if not isinstance(raw_class["require_evidence"], bool):
            raise ThinSubnetError("require_evidence must be boolean")
        owner_registration = None
        if "owner_registration" in raw_class:
            registration_raw = raw_class["owner_registration"]
            if not isinstance(registration_raw, dict):
                raise ThinSubnetError("owner_registration must be an object")
            _require_exact_keys(
                registration_raw,
                _OWNER_REGISTRATION_POLICY_KEYS,
                "owner registration policy",
            )
            source_netuid = registration_raw["source_netuid"]
            if (
                isinstance(source_netuid, bool)
                or not isinstance(source_netuid, int)
                or source_netuid < 0
            ):
                raise ThinSubnetError("owner registration source_netuid is invalid")
            for name in (
                "max_age_seconds",
                "max_future_seconds",
                "max_block_span",
            ):
                _positive_int(registration_raw[name], f"owner registration {name}")
            require_target = registration_raw["require_target_registration"]
            if require_target is not True:
                raise ThinSubnetError(
                    "owner registration require_target_registration must be true"
                )
            owner_registration = OwnerRegistrationPolicy(
                source_netuid=source_netuid,
                locations=_locations(
                    registration_raw["locations"], "owner registration locations"
                ),
                max_age_seconds=registration_raw["max_age_seconds"],
                max_future_seconds=registration_raw["max_future_seconds"],
                max_block_span=registration_raw["max_block_span"],
                require_target_registration=require_target,
            )
            locations = tuple(
                _delegated_report_location(value)
                for value in _locations(
                    raw_class["locations"], "registered external class locations"
                )
            )
            trusted_keys: dict[str, bytes] = {}
        else:
            locations = _locations(raw_class["locations"], "external class locations")
            keys = raw_class["trusted_keys"]
            if not isinstance(keys, dict) or not 1 <= len(keys) <= 8:
                raise ThinSubnetError("external class must pin 1..8 signing keys")
            trusted_keys = {
                _key_id(key): _decode_public_key(value) for key, value in keys.items()
            }
        classes.append(
            ExternalClassPolicy(
                class_id=class_id,
                allocation=allocation,
                source_id=source_id,
                locations=locations,
                trusted_keys=trusted_keys,
                max_age_seconds=raw_class["max_age_seconds"],
                max_future_seconds=raw_class["max_future_seconds"],
                max_block_span=raw_class["max_block_span"],
                require_evidence=raw_class["require_evidence"],
                assignment=_parse_assignment(raw_class["assignment"]),
                owner_registration=owner_registration,
            )
        )
    if total != Decimal(1):
        raise ThinSubnetError("class allocations must sum exactly to 1")
    burn_destination = document.get("burn_hotkey")
    if burn_destination is not None and (
        not isinstance(burn_destination, str)
        or not 1 <= len(burn_destination.encode("utf-8")) <= 128
    ):
        raise ThinSubnetError("score policy burn_hotkey is invalid")
    digest = "sha256:" + hashlib.sha256(POLICY_DOMAIN + encoded).hexdigest()
    return ScorePolicy(
        network=network,
        netuid=netuid,
        classes=tuple(classes),
        digest=digest,
        burn_hotkey=burn_destination,
    )


def _unsigned_report_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"report_id", "signature"}
    }


def _signed_report_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature"}


def report_id_for(document: Mapping[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            REPORT_ID_DOMAIN + canonical_json(_unsigned_report_body(document))
        ).hexdigest()
    )


def sign_report(document: Mapping[str, Any], private_key: Ed25519PrivateKey) -> bytes:
    if "report_id" in document or "signature" in document:
        raise ThinSubnetError("unsigned report must omit report_id and signature")
    signed = dict(document)
    signed["report_id"] = report_id_for(signed)
    signature = private_key.sign(
        REPORT_DOMAIN + canonical_json(_signed_report_body(signed))
    )
    signed["signature"] = {
        "algorithm": "ed25519",
        "value_base64": base64.b64encode(signature).decode("ascii"),
    }
    return canonical_json(signed)


def _unsigned_registration_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"registration_id", "signature"}
    }


def _signed_registration_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature"}


def registration_id_for(document: Mapping[str, Any]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            REGISTRATION_ID_DOMAIN
            + canonical_json(_unsigned_registration_body(document))
        ).hexdigest()
    )


def sign_owner_registration(document: Mapping[str, Any], owner_keypair: Any) -> bytes:
    """Sign a contributor delegation with the source subnet owner's coldkey."""
    if "registration_id" in document or "signature" in document:
        raise ThinSubnetError(
            "unsigned owner registration must omit registration_id and signature"
        )
    signed = dict(document)
    signed["registration_id"] = registration_id_for(signed)
    try:
        signature = bytes(
            owner_keypair.sign(
                REGISTRATION_DOMAIN + canonical_json(_signed_registration_body(signed))
            )
        )
    except Exception as exc:
        raise ThinSubnetError("could not sign owner registration") from exc
    if len(signature) != 64:
        raise ThinSubnetError("owner registration signature must be 64 bytes")
    signed["signature"] = {
        "algorithm": "sr25519",
        "value_base64": base64.b64encode(signature).decode("ascii"),
    }
    return canonical_json(signed)


def _delegated_report_location(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 2048:
        raise ThinSubnetError("delegated report location is invalid")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ThinSubnetError(
            "owner-delegated report locations must be credential-free HTTPS"
        )
    return value


def enforce_registration_checkpoint(
    registration: VerifiedOwnerRegistration,
    checkpoint: RegistrationCheckpoint | None,
) -> RegistrationCheckpoint:
    accepted = RegistrationCheckpoint(
        registration.owner_coldkey,
        registration.delegate_hotkey,
        registration.sequence,
        registration.registration_id,
    )
    if checkpoint is None or checkpoint.owner_coldkey != registration.owner_coldkey:
        return accepted
    if registration.sequence < checkpoint.sequence:
        raise ThinSubnetError("owner registration sequence rolled back")
    if registration.sequence == checkpoint.sequence:
        if registration.registration_id != checkpoint.registration_id:
            raise ThinSubnetError("subnet owner equivocated at an accepted sequence")
        return checkpoint
    if (
        registration.sequence == checkpoint.sequence + 1
        and registration.previous_registration_id != checkpoint.registration_id
    ):
        raise ThinSubnetError(
            "contiguous owner registration does not extend the accepted chain"
        )
    return accepted


def verify_owner_registration(
    raw: bytes,
    policy: ExternalClassPolicy,
    *,
    network: str,
    netuid: int,
    current_block: int,
    current_owner_coldkey: str,
    registered_hotkeys: Mapping[str, str],
    checkpoint: RegistrationCheckpoint | None = None,
    now: datetime | None = None,
) -> tuple[VerifiedOwnerRegistration, RegistrationCheckpoint]:
    registration_policy = policy.owner_registration
    if registration_policy is None:
        raise ThinSubnetError("score class is not owner-registered")
    document = parse_strict_json(raw)
    if raw != canonical_json(document):
        raise ThinSubnetError("owner registration JSON must be canonical")
    _require_exact_keys(document, _REGISTRATION_KEYS, "owner registration")
    if document["schema"] != REGISTRATION_SCHEMA:
        raise ThinSubnetError("unsupported owner registration schema")
    if document["network"] != network:
        raise ThinSubnetError("owner registration network mismatch")
    for name in ("source_netuid", "target_netuid", "sequence"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ThinSubnetError(f"owner registration {name} is invalid")
    if (
        document["source_netuid"] != registration_policy.source_netuid
        or document["target_netuid"] != netuid
    ):
        raise ThinSubnetError("owner registration subnet binding mismatch")
    owner_coldkey = document["owner_coldkey"]
    delegate_hotkey = document["delegate_hotkey"]
    if any(
        not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 128
        for value in (owner_coldkey, delegate_hotkey, current_owner_coldkey)
    ):
        raise ThinSubnetError("owner or delegate key is invalid")
    if owner_coldkey != current_owner_coldkey:
        raise ThinSubnetError(
            "registration signer is not the current source subnet owner"
        )
    if not registration_policy.require_target_registration:
        raise ThinSubnetError("owner registration must require target registration")
    if registered_hotkeys.get(delegate_hotkey) != owner_coldkey:
        raise ThinSubnetError(
            "owner delegate is not currently registered to the target subnet"
        )
    source_id = _identifier(document["source_id"], "registration source id")
    if source_id != policy.source_id:
        raise ThinSubnetError("owner registration source mismatch")
    class_ids_raw = document["class_ids"]
    if (
        not isinstance(class_ids_raw, list)
        or not 1 <= len(class_ids_raw) <= 32
        or any(
            not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None
            for value in class_ids_raw
        )
        or class_ids_raw != sorted(set(class_ids_raw))
    ):
        raise ThinSubnetError("owner registration class_ids must be sorted and unique")
    class_ids = tuple(
        _identifier(value, "registration class id") for value in class_ids_raw
    )
    if policy.class_id not in class_ids:
        raise ThinSubnetError("owner registration does not delegate this class")
    locations_raw = document["report_locations"]
    if (
        not isinstance(locations_raw, list)
        or not 1 <= len(locations_raw) <= 8
        or any(not isinstance(value, str) for value in locations_raw)
        or len(set(locations_raw)) != len(locations_raw)
    ):
        raise ThinSubnetError("owner registration report locations are invalid")
    report_locations = tuple(
        _delegated_report_location(value) for value in locations_raw
    )
    if report_locations != policy.locations:
        raise ThinSubnetError(
            "owner registration report locations do not match validator policy"
        )
    keys_raw = document["report_keys"]
    if not isinstance(keys_raw, dict) or not 1 <= len(keys_raw) <= 8:
        raise ThinSubnetError("owner registration must delegate 1..8 report keys")
    report_keys = {
        _key_id(key): _decode_public_key(value) for key, value in keys_raw.items()
    }
    for name in ("valid_from_block", "valid_until_block"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ThinSubnetError(f"owner registration {name} is invalid")
    first_block = document["valid_from_block"]
    last_block = document["valid_until_block"]
    if first_block > current_block or current_block >= last_block:
        raise ThinSubnetError("owner registration is outside its block window")
    if (
        last_block <= first_block
        or last_block - first_block > registration_policy.max_block_span
    ):
        raise ThinSubnetError("owner registration block window is invalid or too wide")
    issued_at = parse_time(document["issued_at"], "registration issued_at")
    expires_at = parse_time(document["expires_at"], "registration expires_at")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ThinSubnetError("owner registration verification time must be UTC")
    age = (current - issued_at).total_seconds()
    if (
        age < -registration_policy.max_future_seconds
        or age > registration_policy.max_age_seconds
    ):
        raise ThinSubnetError("owner registration is stale or future-dated")
    if not issued_at < expires_at or current >= expires_at:
        raise ThinSubnetError("owner registration has expired")
    previous = document["previous_registration_id"]
    if previous is not None:
        previous = _digest(previous, "previous registration id")
    registration_id = _digest(document["registration_id"], "registration id")
    if registration_id != registration_id_for(document):
        raise ThinSubnetError("owner registration id does not match its canonical body")
    signature = document["signature"]
    if not isinstance(signature, dict):
        raise ThinSubnetError("owner registration signature must be an object")
    _require_exact_keys(signature, _SIGNATURE_KEYS, "owner registration signature")
    if signature["algorithm"] != "sr25519":
        raise ThinSubnetError("unsupported owner registration signature algorithm")
    try:
        signature_bytes = base64.b64decode(signature["value_base64"], validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ThinSubnetError(
            "owner registration signature is not canonical base64"
        ) from exc
    if (
        len(signature_bytes) != 64
        or base64.b64encode(signature_bytes).decode("ascii")
        != signature["value_base64"]
    ):
        raise ThinSubnetError("owner registration signature must be 64 bytes")
    try:
        from bittensor_wallet import Keypair

        verified = Keypair(ss58_address=owner_coldkey).verify(
            REGISTRATION_DOMAIN + canonical_json(_signed_registration_body(document)),
            signature_bytes,
        )
    except Exception as exc:
        raise ThinSubnetError("owner registration signer address is invalid") from exc
    if not verified:
        raise ThinSubnetError("owner registration signature verification failed")
    registration = VerifiedOwnerRegistration(
        source_netuid=document["source_netuid"],
        target_netuid=document["target_netuid"],
        owner_coldkey=owner_coldkey,
        delegate_hotkey=delegate_hotkey,
        source_id=source_id,
        class_ids=class_ids,
        report_locations=report_locations,
        report_keys=report_keys,
        sequence=document["sequence"],
        previous_registration_id=previous,
        registration_id=registration_id,
        issued_at=issued_at,
        expires_at=expires_at,
        valid_from_block=first_block,
        valid_until_block=last_block,
        document=document,
    )
    return registration, enforce_registration_checkpoint(registration, checkpoint)


def materialize_registered_policy(
    policy: ExternalClassPolicy, registration: VerifiedOwnerRegistration
) -> ExternalClassPolicy:
    if policy.owner_registration is None:
        raise ThinSubnetError("cannot materialize a directly pinned class")
    if (
        registration.source_id != policy.source_id
        or policy.class_id not in registration.class_ids
    ):
        raise ThinSubnetError("owner registration does not match score class")
    return replace(
        policy,
        trusted_keys=dict(registration.report_keys),
    )


def _parse_entry(value: Any) -> ScoreEntry:
    if not isinstance(value, dict):
        raise ThinSubnetError("score entry must be an object")
    _require_exact_keys(value, _ENTRY_KEYS, "score entry")
    hotkey = value["miner_hotkey"]
    if not isinstance(hotkey, str) or not 1 <= len(hotkey.encode("utf-8")) <= 512:
        raise ThinSubnetError("invalid miner hotkey")
    metrics_raw = value["metrics"]
    if not isinstance(metrics_raw, dict) or len(metrics_raw) > MAX_METRICS_PER_ENTRY:
        raise ThinSubnetError("entry metrics must be a bounded object")
    metrics = {
        _identifier(name, "metric name"): _decimal(raw, f"metric {name}")
        for name, raw in metrics_raw.items()
    }
    asserted = (
        None
        if value["asserted_score"] is None
        else _decimal(value["asserted_score"], "asserted score")
    )
    reasons = value["reason_codes"]
    if (
        not isinstance(reasons, list)
        or not 1 <= len(reasons) <= 32
        or any(
            not isinstance(item, str) or _REASON_RE.fullmatch(item) is None
            for item in reasons
        )
        or reasons != sorted(set(reasons))
    ):
        raise ThinSubnetError("reason_codes must be a sorted unique non-empty list")
    evidence_raw = value["evidence"]
    if not isinstance(evidence_raw, list) or len(evidence_raw) > MAX_EVIDENCE_PER_ENTRY:
        raise ThinSubnetError("evidence must be a bounded list")
    evidence: list[EvidenceRef] = []
    seen_evidence: set[tuple[str, str]] = set()
    for item in evidence_raw:
        if not isinstance(item, dict):
            raise ThinSubnetError("evidence reference must be an object")
        _require_exact_keys(item, _EVIDENCE_KEYS, "evidence reference")
        kind = _identifier(item["kind"], "evidence kind")
        evidence_id = _digest(item["id"], "evidence id")
        digest = _digest(item["digest"], "evidence digest")
        uri = item["uri"]
        if uri is not None:
            if not isinstance(uri, str) or not 1 <= len(uri.encode("utf-8")) <= 2048:
                raise ThinSubnetError("evidence URI is invalid")
            parsed_uri = urllib.parse.urlsplit(uri)
            if (
                parsed_uri.scheme not in {"https", "ipfs"}
                or not parsed_uri.netloc
                or parsed_uri.username
                or parsed_uri.password
                or parsed_uri.fragment
            ):
                raise ThinSubnetError(
                    "evidence URI must be credential-free HTTPS or IPFS"
                )
        identity = (kind, evidence_id)
        if identity in seen_evidence:
            raise ThinSubnetError("duplicate evidence reference")
        seen_evidence.add(identity)
        evidence.append(EvidenceRef(kind, evidence_id, digest, uri))
    return ScoreEntry(hotkey, metrics, asserted, tuple(reasons), tuple(evidence))


def verify_report(
    raw: bytes,
    policy: ExternalClassPolicy,
    *,
    network: str,
    netuid: int,
    current_block: int,
    now: datetime | None = None,
) -> VerifiedReport:
    if (
        not isinstance(network, str)
        or isinstance(netuid, bool)
        or not isinstance(netuid, int)
        or netuid < 0
        or isinstance(current_block, bool)
        or not isinstance(current_block, int)
        or current_block < 0
    ):
        raise ThinSubnetError("validator network, netuid, or block is invalid")
    document = parse_strict_json(raw)
    if raw != canonical_json(document):
        raise ThinSubnetError("score report JSON must be canonical")
    _require_exact_keys(document, _REPORT_KEYS, "score report")
    if document["schema"] != REPORT_SCHEMA or document["complete"] is not True:
        raise ThinSubnetError("unsupported or incomplete score report")
    if (
        not isinstance(document["network"], str)
        or not 1 <= len(document["network"].encode("utf-8")) <= 128
        or isinstance(document["netuid"], bool)
        or not isinstance(document["netuid"], int)
        or document["netuid"] < 0
    ):
        raise ThinSubnetError("score report network or netuid is invalid")
    if document["network"] != network or document["netuid"] != netuid:
        raise ThinSubnetError("score report network or netuid mismatch")
    if (
        document["class_id"] != policy.class_id
        or document["source_id"] != policy.source_id
    ):
        raise ThinSubnetError("score report class or source mismatch")
    source_epoch = document["source_epoch"]
    if (
        isinstance(source_epoch, bool)
        or not isinstance(source_epoch, int)
        or source_epoch < 0
    ):
        raise ThinSubnetError("invalid source epoch")
    for name in ("valid_from_block", "valid_until_block"):
        value = document[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ThinSubnetError(f"invalid {name}")
    first_block = document["valid_from_block"]
    last_block = document["valid_until_block"]
    if first_block > current_block or current_block >= last_block:
        raise ThinSubnetError("score report is outside its authorized block window")
    if last_block <= first_block or last_block - first_block > policy.max_block_span:
        raise ThinSubnetError("score report block window is invalid or too wide")
    generated_at = parse_time(document["generated_at"], "generated_at")
    valid_until = parse_time(document["valid_until"], "valid_until")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ThinSubnetError("verification time must be UTC")
    age = (current - generated_at).total_seconds()
    if age < -policy.max_future_seconds or age > policy.max_age_seconds:
        raise ThinSubnetError("score report generation time is stale or future-dated")
    if not generated_at < valid_until or current >= valid_until:
        raise ThinSubnetError("score report validity window has expired")
    policy_digest = _digest(document["policy_digest"], "policy digest")
    verifier_digest = _digest(document["verifier_digest"], "verifier digest")
    previous = document["previous_report_id"]
    if previous is not None:
        previous = _digest(previous, "previous report id")
    report_id = _digest(document["report_id"], "report id")
    if report_id != report_id_for(document):
        raise ThinSubnetError("score report id does not match its canonical body")
    signing_key_id = _key_id(document["signing_key_id"])
    key = policy.trusted_keys.get(signing_key_id)
    if key is None:
        raise ThinSubnetError("score report signing key is not locally trusted")
    signature = document["signature"]
    if not isinstance(signature, dict):
        raise ThinSubnetError("score report signature must be an object")
    _require_exact_keys(signature, _SIGNATURE_KEYS, "score report signature")
    if signature["algorithm"] != "ed25519":
        raise ThinSubnetError("unsupported score report signature algorithm")
    try:
        signature_bytes = base64.b64decode(signature["value_base64"], validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ThinSubnetError("score report signature is not canonical base64") from exc
    if (
        len(signature_bytes) != 64
        or base64.b64encode(signature_bytes).decode("ascii")
        != signature["value_base64"]
    ):
        raise ThinSubnetError("score report signature must be 64 bytes")
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(
            signature_bytes,
            REPORT_DOMAIN + canonical_json(_signed_report_body(document)),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ThinSubnetError("score report signature verification failed") from exc
    entries_raw = document["entries"]
    if not isinstance(entries_raw, list) or len(entries_raw) > MAX_REPORT_ENTRIES:
        raise ThinSubnetError("score report entries must be a bounded list")
    entries = tuple(_parse_entry(item) for item in entries_raw)
    hotkeys = [entry.miner_hotkey for entry in entries]
    if hotkeys != sorted(set(hotkeys)):
        raise ThinSubnetError("score report entries must have sorted unique hotkeys")
    return VerifiedReport(
        class_id=policy.class_id,
        source_id=policy.source_id,
        source_epoch=source_epoch,
        report_id=report_id,
        previous_report_id=previous,
        generated_at=generated_at,
        valid_until=valid_until,
        valid_from_block=first_block,
        valid_until_block=last_block,
        policy_digest=policy_digest,
        verifier_digest=verifier_digest,
        signing_key_id=signing_key_id,
        entries=entries,
        document=document,
    )


def enforce_checkpoint(
    report: VerifiedReport, checkpoint: SourceCheckpoint | None
) -> SourceCheckpoint:
    if checkpoint is None:
        return SourceCheckpoint(report.source_epoch, report.report_id)
    if report.source_epoch < checkpoint.source_epoch:
        raise ThinSubnetError("score report source epoch rolled back")
    if report.source_epoch == checkpoint.source_epoch:
        if report.report_id != checkpoint.report_id:
            raise ThinSubnetError("score source equivocated at an accepted epoch")
        return checkpoint
    if (
        report.source_epoch == checkpoint.source_epoch + 1
        and report.previous_report_id != checkpoint.report_id
    ):
        raise ThinSubnetError(
            "contiguous score report does not extend the accepted chain"
        )
    return SourceCheckpoint(report.source_epoch, report.report_id)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def fetch_report(location: str, *, timeout: float = 10.0) -> bytes:
    parsed = urllib.parse.urlsplit(location)
    if parsed.scheme in {"http", "https"}:
        if parsed.scheme != "https":
            raise ThinSubnetError("remote score report locations must use HTTPS")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
        ):
            raise ThinSubnetError("invalid remote score report location")
        request = urllib.request.Request(
            location, headers={"Accept": "application/json"}, method="GET"
        )
        try:
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=timeout
            ) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as exc:
                        raise ThinSubnetError(
                            "invalid score report Content-Length"
                        ) from exc
                    if declared < 0 or declared > MAX_REPORT_BYTES:
                        raise ThinSubnetError("score report exceeds size limit")
                raw = response.read(MAX_REPORT_BYTES + 1)
        except ThinSubnetError:
            raise
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            socket.timeout,
        ) as exc:
            raise ThinSubnetError("could not fetch remote score report") from exc
        if len(raw) > MAX_REPORT_BYTES:
            raise ThinSubnetError("score report exceeds size limit")
        return raw
    if parsed.scheme not in {"", "file"}:
        raise ThinSubnetError(
            "score report location must be a path, file URI, or HTTPS URL"
        )
    path = Path(
        urllib.request.url2pathname(parsed.path)
        if parsed.scheme == "file"
        else location
    ).expanduser()
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise ThinSubnetError("score report exceeds size limit")
        raw = path.read_bytes()
    except ThinSubnetError:
        raise
    except OSError as exc:
        raise ThinSubnetError(f"could not read score report: {path}") from exc
    if len(raw) > MAX_REPORT_BYTES:
        raise ThinSubnetError("score report exceeds size limit")
    return raw


def load_best_report(
    policy: ExternalClassPolicy,
    *,
    network: str,
    netuid: int,
    current_block: int,
    checkpoint: SourceCheckpoint | None,
    now: datetime | None = None,
) -> tuple[VerifiedReport, SourceCheckpoint]:
    valid: list[VerifiedReport] = []
    errors: list[str] = []
    for index, location in enumerate(policy.locations):
        try:
            valid.append(
                verify_report(
                    fetch_report(location),
                    policy,
                    network=network,
                    netuid=netuid,
                    current_block=current_block,
                    now=now,
                )
            )
        except ThinSubnetError as exc:
            errors.append(f"mirror[{index}]:{exc}")
    if not valid:
        raise ThinSubnetError(
            f"no valid report for class {policy.class_id}: " + "; ".join(errors)
        )
    highest_epoch = max(item.source_epoch for item in valid)
    candidates = [item for item in valid if item.source_epoch == highest_epoch]
    report_ids = {item.report_id for item in candidates}
    if len(report_ids) != 1:
        raise ThinSubnetError("score report mirrors expose same-epoch equivocation")
    selected = candidates[0]
    return selected, enforce_checkpoint(selected, checkpoint)


def load_best_owner_registration(
    policy: ExternalClassPolicy,
    *,
    network: str,
    netuid: int,
    current_block: int,
    current_owner_coldkey: str,
    registered_hotkeys: Mapping[str, str],
    checkpoint: RegistrationCheckpoint | None,
    now: datetime | None = None,
) -> tuple[VerifiedOwnerRegistration, RegistrationCheckpoint]:
    registration_policy = policy.owner_registration
    if registration_policy is None:
        raise ThinSubnetError("score class does not use owner registration")
    valid: list[tuple[VerifiedOwnerRegistration, RegistrationCheckpoint]] = []
    errors: list[str] = []
    for index, location in enumerate(registration_policy.locations):
        try:
            valid.append(
                verify_owner_registration(
                    fetch_report(location),
                    policy,
                    network=network,
                    netuid=netuid,
                    current_block=current_block,
                    current_owner_coldkey=current_owner_coldkey,
                    registered_hotkeys=registered_hotkeys,
                    checkpoint=checkpoint,
                    now=now,
                )
            )
        except ThinSubnetError as exc:
            errors.append(f"mirror[{index}]:{exc}")
    if not valid:
        raise ThinSubnetError(
            f"no valid owner registration for class {policy.class_id}: "
            + "; ".join(errors)
        )
    highest_sequence = max(item[0].sequence for item in valid)
    candidates = [item for item in valid if item[0].sequence == highest_sequence]
    registration_ids = {item[0].registration_id for item in candidates}
    if len(registration_ids) != 1:
        raise ThinSubnetError(
            "owner registration mirrors expose same-sequence equivocation"
        )
    return candidates[0]


def assignment_score(entry: ScoreEntry, assignment: AssignmentPolicy) -> float:
    if assignment.mode == "metric":
        if assignment.metric not in entry.metrics:
            raise ThinSubnetError(
                f"entry {entry.miner_hotkey} lacks assigned metric {assignment.metric}"
            )
        value = entry.metrics[assignment.metric]
    else:
        if entry.asserted_score is None:
            raise ThinSubnetError(f"entry {entry.miner_hotkey} lacks asserted_score")
        value = entry.asserted_score
    if assignment.cap is not None:
        value = min(value, assignment.cap)
    if assignment.transform == "binary":
        value = Decimal(1) if value > 0 else Decimal(0)
    score = float(value)
    if not math.isfinite(score) or score < 0:
        raise ThinSubnetError("assigned score is non-finite or negative")
    return score


def external_class_decision(
    policy: ExternalClassPolicy,
    report: VerifiedReport,
    *,
    coldkey_of: dict[str, str],
    owner_registration: VerifiedOwnerRegistration | None = None,
) -> ClassDecision:
    raw_scores: dict[str, float] = {}
    provenance: dict[str, Any] = {}
    for entry in report.entries:
        score = assignment_score(entry, policy.assignment)
        if score > 0 and policy.require_evidence and not entry.evidence:
            raise ThinSubnetError(
                f"positive score for {entry.miner_hotkey} lacks required evidence"
            )
        if score > 0 and not set(policy.assignment.required_reason_codes).issubset(
            entry.reason_codes
        ):
            raise ThinSubnetError(
                f"positive score for {entry.miner_hotkey} lacks validator-required reasons"
            )
        evidence_kinds = {item.kind for item in entry.evidence}
        if score > 0 and not set(policy.assignment.required_evidence_kinds).issubset(
            evidence_kinds
        ):
            raise ThinSubnetError(
                f"positive score for {entry.miner_hotkey} lacks validator-required evidence kinds"
            )
        if entry.miner_hotkey not in coldkey_of:
            continue
        raw_scores[entry.miner_hotkey] = score
        provenance[entry.miner_hotkey] = {
            "metrics": {
                key: str(value) for key, value in sorted(entry.metrics.items())
            },
            "asserted_score": None
            if entry.asserted_score is None
            else str(entry.asserted_score),
            "reason_codes": list(entry.reason_codes),
            "evidence": [asdict(item) for item in entry.evidence],
        }
    normalized = coldkey_collapsed_weights(raw_scores, coldkey_of)
    if not normalized:
        raise ThinSubnetError(
            f"configured class {policy.class_id} has no positive scores"
        )
    assignment = {
        "mode": policy.assignment.mode,
        "metric": policy.assignment.metric,
        "transform": policy.assignment.transform,
        "cap": None if policy.assignment.cap is None else str(policy.assignment.cap),
        "require_evidence": policy.require_evidence,
        "policy_digest": report.policy_digest,
        "verifier_digest": report.verifier_digest,
        "signing_key_id": report.signing_key_id,
        "required_reason_codes": list(policy.assignment.required_reason_codes),
        "required_evidence_kinds": list(policy.assignment.required_evidence_kinds),
        "owner_registration": (
            None
            if owner_registration is None
            else {
                "schema": REGISTRATION_SCHEMA,
                "source_netuid": owner_registration.source_netuid,
                "target_netuid": owner_registration.target_netuid,
                "owner_coldkey": owner_registration.owner_coldkey,
                "delegate_hotkey": owner_registration.delegate_hotkey,
                "sequence": owner_registration.sequence,
                "registration_id": owner_registration.registration_id,
                "valid_from_block": owner_registration.valid_from_block,
                "valid_until_block": owner_registration.valid_until_block,
            }
        ),
    }
    return ClassDecision(
        class_id=policy.class_id,
        kind="external",
        allocation=str(policy.allocation),
        source_id=policy.source_id,
        source_epoch=report.source_epoch,
        report_id=report.report_id,
        assignment=assignment,
        raw_scores=raw_scores,
        normalized_weights=normalized,
        provenance=provenance,
    )


def local_class_decision(
    policy: LocalClassPolicy,
    raw_scores: dict[str, float],
    *,
    coldkey_of: dict[str, str],
    reasons: dict[str, str],
) -> ClassDecision:
    normalized = coldkey_collapsed_weights(raw_scores, coldkey_of)
    return ClassDecision(
        class_id=policy.class_id,
        kind="local_sat",
        allocation=str(policy.allocation),
        source_id=None,
        source_epoch=None,
        report_id=None,
        assignment={"mode": "validator_local_sat_v1"},
        raw_scores=dict(sorted(raw_scores.items())),
        normalized_weights=normalized,
        provenance={
            hotkey: {"reason_codes": [reasons.get(hotkey, "missing_result")]}
            for hotkey in sorted(raw_scores)
        },
    )


def compose_class_decisions(
    policy: ScorePolicy, decisions: list[ClassDecision]
) -> dict[str, float]:
    by_id = {item.class_id: item for item in decisions}
    if len(by_id) != len(decisions) or set(by_id) != {
        item.class_id for item in policy.classes
    }:
        raise ThinSubnetError("class decisions do not cover the configured policy")
    if (
        policy.burn_hotkey is None
        and decisions
        and all(not item.normalized_weights for item in decisions)
    ):
        # A fully empty round is a valid fail-closed outcome. The validator
        # records it and retains its prior on-chain vector without submitting.
        return {}
    final: dict[str, Decimal] = {}
    for class_policy in policy.classes:
        decision = by_id[class_policy.class_id]
        if decision.kind != class_policy.kind:
            raise ThinSubnetError("class decision kind mismatch")
        if not decision.normalized_weights:
            if policy.burn_hotkey is None:
                raise ThinSubnetError("class weights are not normalized")
            final[policy.burn_hotkey] = (
                final.get(policy.burn_hotkey, Decimal(0)) + class_policy.allocation
            )
            continue
        class_total = sum(
            Decimal(str(value)) for value in decision.normalized_weights.values()
        )
        if abs(class_total - Decimal(1)) > Decimal("0.000000001"):
            raise ThinSubnetError("class weights are not normalized")
        for hotkey, raw_weight in decision.normalized_weights.items():
            if hotkey == policy.burn_hotkey:
                raise ThinSubnetError("burn hotkey cannot earn validated supply")
            value = Decimal(str(raw_weight))
            final[hotkey] = (
                final.get(hotkey, Decimal(0)) + class_policy.allocation * value
            )
    total = sum(final.values())
    if abs(total - Decimal(1)) > Decimal("0.000000001"):
        raise ThinSubnetError("composed class weights do not conserve allocation")
    return {
        hotkey: float(value / total)
        for hotkey, value in sorted(final.items())
        if value > 0
    }


def decision_document(
    *,
    validator_hotkey: str,
    network: str,
    netuid: int,
    round_id: int,
    block: int,
    policy_digest: str,
    decisions: list[ClassDecision],
    peers: list[dict[str, Any]],
    uids: list[int],
    weights: list[float],
) -> tuple[dict[str, Any], str]:
    if len(uids) != len(weights) or len(set(uids)) != len(uids):
        raise ThinSubnetError("decision UID vector must be aligned and unique")
    if any(
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid < 0
        or not math.isfinite(float(weight))
        or float(weight) < 0
        for uid, weight in zip(uids, weights)
    ):
        raise ThinSubnetError("decision UID vector is invalid")
    document = {
        "schema": DECISION_SCHEMA,
        "validator_hotkey": validator_hotkey,
        "network": network,
        "netuid": netuid,
        "round": round_id,
        "block": block,
        "score_policy_digest": policy_digest,
        "classes": [asdict(item) for item in decisions],
        "metagraph": sorted(peers, key=lambda item: int(item["uid"])),
        "onchain_vector": [
            {"uid": int(uid), "weight": round(float(weight), 12)}
            for uid, weight in zip(uids, weights)
        ],
    }
    digest = (
        "sha256:"
        + hashlib.sha256(DECISION_DOMAIN + canonical_json(document)).hexdigest()
    )
    return {**document, "decision_digest": digest}, digest


class DecisionStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).expanduser()

    def write(self, document: Mapping[str, Any]) -> Path:
        digest = document.get("decision_digest")
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise ThinSubnetError("decision record lacks a valid digest")
        body = {
            key: value for key, value in document.items() if key != "decision_digest"
        }
        expected = (
            "sha256:"
            + hashlib.sha256(DECISION_DOMAIN + canonical_json(body)).hexdigest()
        )
        if digest != expected:
            raise ThinSubnetError("decision record digest mismatch")
        round_id = document.get("round")
        if isinstance(round_id, bool) or not isinstance(round_id, int) or round_id < 0:
            raise ThinSubnetError("decision record round is invalid")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ThinSubnetError("could not create decision directory") from exc
        path = (
            self.directory / f"round-{round_id}-{digest.removeprefix('sha256:')}.json"
        )
        raw = canonical_json(dict(document)) + b"\n"
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise ThinSubnetError(
                    "could not read existing decision record"
                ) from exc
            if existing != raw:
                raise ThinSubnetError(
                    "decision record path already contains different bytes"
                )
            return path
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise ThinSubnetError("could not persist decision record") from exc
        finally:
            if tmp.exists():
                tmp.unlink()
        return path
