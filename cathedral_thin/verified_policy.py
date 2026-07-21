"""Verified, evidence-bearing policy work for the thin Cathedral subnet.

The protocol is intentionally narrow. A validator signs an individualized task
containing public examples and a commitment to a hidden evaluation suite. A
miner returns a signed, compact decision list. The validator reveals and
replays the hidden suite locally, signs the resulting metrics, and may turn
those measurements into one or more existing score-class reports.

This module does not claim that a policy was independently invented by a
miner. It proves identity, task binding, deterministic behavior on committed
cases, cited-example consistency, and (when supplied) binding to a separately
verified Cathedral execution receipt.
"""

from __future__ import annotations

import base64
import binascii
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .core import ThinSubnetError
from .ml_receipts import (
    VerifiedInferenceReceipt,
    canonical_decimal,
    commit_payload,
    sha256_digest,
)
from .score_classes import (
    REPORT_SCHEMA,
    canonical_json,
    format_time,
    parse_strict_json,
    parse_time,
)


TASK_SCHEMA = "cathedral_verified_policy_task_v1"
HIDDEN_SUITE_SCHEMA = "cathedral_verified_policy_hidden_suite_v1"
ARTIFACT_SCHEMA = "cathedral_verified_policy_artifact_v1"
EVALUATION_SCHEMA = "cathedral_verified_policy_evaluation_v1"

TASK_DOMAIN = b"cathedral-verified-policy-task-v1\x00"
TASK_ID_DOMAIN = b"cathedral-verified-policy-task-id-v1\x00"
HIDDEN_SUITE_DOMAIN = b"cathedral-verified-policy-hidden-suite-v1\x00"
HIDDEN_NONCE_DOMAIN = b"cathedral-verified-policy-hidden-nonce-v1\x00"
ARTIFACT_DOMAIN = b"cathedral-verified-policy-artifact-v1\x00"
ARTIFACT_ID_DOMAIN = b"cathedral-verified-policy-artifact-id-v1\x00"
EVALUATION_DOMAIN = b"cathedral-verified-policy-evaluation-v1\x00"
EVALUATION_ID_DOMAIN = b"cathedral-verified-policy-evaluation-id-v1\x00"
CASE_RESULTS_DOMAIN = b"cathedral-verified-policy-case-results-v1\x00"
VERIFIER_DOMAIN = b"cathedral-verified-policy-verifier-v1\x00"

MAX_TASK_BYTES = 4_194_304
MAX_ARTIFACT_BYTES = 1_048_576
MAX_EVALUATION_BYTES = 1_048_576
MAX_EXAMPLES = 4096
MAX_FEATURES = 64
MAX_LABELS = 64
MAX_RULES = 1024
MAX_CONDITIONS = 64

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SIGNATURE_KEYS = frozenset({"algorithm", "value_base64"})
_EXAMPLE_KEYS = frozenset({"example_id", "features", "label"})
_CONDITION_KEYS = frozenset({"feature", "equals"})
_RULE_KEYS = frozenset({"rule_id", "when", "then", "support_example_ids"})
_METRIC_POLICY_KEYS = frozenset(
    {"compactness_max_terms", "quality_floor", "rare_floor"}
)
_TASK_KEYS = frozenset(
    {
        "schema",
        "network",
        "netuid",
        "source_epoch",
        "task_class",
        "validator_hotkey",
        "miner_hotkey",
        "nonce_base64",
        "issued_at",
        "valid_from_block",
        "valid_until_block",
        "feature_names",
        "labels",
        "rare_labels",
        "public_examples",
        "hidden_suite_commitment",
        "metric_policy",
        "task_id",
        "signature",
    }
)
_HIDDEN_SUITE_KEYS = frozenset({"schema", "task_nonce_digest", "salt_base64", "cases"})
_ARTIFACT_KEYS = frozenset(
    {
        "schema",
        "task_id",
        "miner_hotkey",
        "created_at",
        "rules",
        "default_label",
        "artifact_id",
        "signature",
    }
)
_EXECUTION_KEYS = frozenset(
    {
        "receipt_id",
        "request_id",
        "output_commitment_bound",
        "attestation_verified",
        "verifier_digest",
    }
)
_EVALUATION_KEYS = frozenset(
    {
        "schema",
        "network",
        "netuid",
        "source_epoch",
        "task_id",
        "artifact_id",
        "validator_hotkey",
        "miner_hotkey",
        "evaluated_at",
        "task_valid_from_block",
        "task_valid_until_block",
        "hidden_suite_commitment",
        "case_count",
        "rare_case_count",
        "case_results_digest",
        "metrics",
        "execution",
        "evaluation_id",
        "signature",
    }
)
_METRIC_KEYS = frozenset(
    {
        "policy_balanced_accuracy",
        "policy_rare_recall",
        "policy_evidence_faithfulness",
        "policy_compactness",
        "policy_artifact_signature",
        "policy_attested_execution",
    }
)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
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


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ThinSubnetError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ThinSubnetError(f"{label} must be a non-negative integer")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _scalar(value: Any, label: str) -> str | bool | int:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 1_000_000_000_000:
            raise ThinSubnetError(f"{label} integer is out of bounds")
        return value
    if isinstance(value, str) and 1 <= len(value.encode("utf-8")) <= 256:
        return value
    raise ThinSubnetError(f"{label} must be a bounded string, boolean, or integer")


def _canonical_b64(value: Any, label: str, *, size: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise ThinSubnetError(f"{label} must be canonical base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThinSubnetError(f"{label} must be canonical base64") from exc
    if base64.b64encode(raw).decode("ascii") != value or (
        size is not None and len(raw) != size
    ):
        suffix = f" containing {size} bytes" if size is not None else ""
        raise ThinSubnetError(f"{label} must be canonical base64{suffix}")
    return raw


def _signature(value: Any, label: str) -> bytes:
    if not isinstance(value, dict):
        raise ThinSubnetError(f"{label} signature must be an object")
    _exact_keys(value, _SIGNATURE_KEYS, f"{label} signature")
    if value["algorithm"] != "sr25519":
        raise ThinSubnetError(f"unsupported {label} signature algorithm")
    return _canonical_b64(value["value_base64"], f"{label} signature", size=64)


def _verify_sr25519(hotkey: str, message: bytes, signature: bytes, label: str) -> None:
    try:
        from bittensor_wallet import Keypair

        valid = Keypair(ss58_address=hotkey).verify(message, signature)
    except Exception as exc:
        raise ThinSubnetError(f"{label} hotkey is invalid") from exc
    if not valid:
        raise ThinSubnetError(f"{label} signature verification failed")


def _sign_sr25519(keypair: Any, message: bytes, label: str) -> dict[str, str]:
    try:
        signature = bytes(keypair.sign(message))
    except Exception as exc:
        raise ThinSubnetError(f"could not sign {label}") from exc
    if len(signature) != 64:
        raise ThinSubnetError(f"{label} signature must be 64 bytes")
    return {
        "algorithm": "sr25519",
        "value_base64": base64.b64encode(signature).decode("ascii"),
    }


def _unsigned(document: Mapping[str, Any], id_field: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {id_field, "signature"}
    }


def _signed(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature"}


def _ratio(numerator: int, denominator: int) -> str:
    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise ThinSubnetError("invalid score ratio")
    with localcontext() as context:
        context.prec = 28
        return _score_decimal(Decimal(numerator) / Decimal(denominator))


def _score_decimal(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 28
        rounded = value.quantize(Decimal("0.000000000001"))
    return canonical_decimal(rounded)


def _parse_floor(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ThinSubnetError(f"invalid {label}") from exc
    if (
        not isinstance(value, str)
        or canonical_decimal(parsed) != value
        or not 0 <= parsed <= 1
    ):
        raise ThinSubnetError(f"{label} must be a canonical decimal from 0 to 1")
    return parsed


def _validate_examples(
    raw: Any,
    *,
    feature_names: tuple[str, ...],
    labels: tuple[str, ...],
    label: str,
    minimum: int,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not minimum <= len(raw) <= MAX_EXAMPLES:
        raise ThinSubnetError(f"{label} must contain {minimum}..{MAX_EXAMPLES} cases")
    parsed: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            raise ThinSubnetError(f"{label} row must be an object")
        _exact_keys(row, _EXAMPLE_KEYS, f"{label} row")
        example_id = _identifier(row["example_id"], f"{label} example id")
        features = row["features"]
        if not isinstance(features, dict) or tuple(sorted(features)) != feature_names:
            raise ThinSubnetError(f"{label} features do not match the task schema")
        normalized = {
            key: _scalar(features[key], f"{label} feature {key}")
            for key in feature_names
        }
        decision = _identifier(row["label"], f"{label} decision label")
        if decision not in labels:
            raise ThinSubnetError(f"{label} decision label is not allowed")
        parsed.append(
            {"example_id": example_id, "features": normalized, "label": decision}
        )
    ids = [row["example_id"] for row in parsed]
    if ids != sorted(set(ids)):
        raise ThinSubnetError(f"{label} example ids must be sorted and unique")
    return tuple(parsed)


def hidden_suite_bytes(
    cases: Iterable[Mapping[str, Any]], *, salt: bytes, task_nonce: bytes
) -> bytes:
    if len(salt) != 32:
        raise ThinSubnetError("hidden suite salt must be exactly 32 bytes")
    if len(task_nonce) != 32:
        raise ThinSubnetError("task nonce must be exactly 32 bytes")
    rows = sorted(
        (dict(row) for row in cases), key=lambda row: str(row.get("example_id", ""))
    )
    return canonical_json(
        {
            "schema": HIDDEN_SUITE_SCHEMA,
            "task_nonce_digest": sha256_digest(HIDDEN_NONCE_DOMAIN + task_nonce),
            "salt_base64": base64.b64encode(salt).decode("ascii"),
            "cases": rows,
        }
    )


def _hidden_suite_binding(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "network",
            "netuid",
            "source_epoch",
            "task_class",
            "validator_hotkey",
            "miner_hotkey",
            "nonce_base64",
            "issued_at",
            "valid_from_block",
            "valid_until_block",
        )
    }


def hidden_suite_commitment(raw: bytes, *, task_binding: Mapping[str, Any]) -> str:
    if not isinstance(raw, bytes):
        raise ThinSubnetError("hidden suite must be bytes")
    binding = _hidden_suite_binding(task_binding)
    return sha256_digest(
        HIDDEN_SUITE_DOMAIN
        + canonical_json(binding)
        + len(raw).to_bytes(8, "big")
        + raw
    )


def task_id_for(document: Mapping[str, Any]) -> str:
    return sha256_digest(
        TASK_ID_DOMAIN + canonical_json(_unsigned(document, "task_id"))
    )


def make_task(
    *,
    network: str,
    netuid: int,
    source_epoch: int,
    task_class: str,
    validator_hotkey: str,
    miner_hotkey: str,
    nonce: bytes,
    issued_at: str,
    valid_from_block: int,
    valid_until_block: int,
    feature_names: Iterable[str],
    labels: Iterable[str],
    rare_labels: Iterable[str],
    public_examples: Iterable[Mapping[str, Any]],
    hidden_suite: bytes,
    compactness_max_terms: int = 64,
    quality_floor: str = "0.7",
    rare_floor: str = "0.5",
) -> dict[str, Any]:
    if not isinstance(nonce, bytes) or len(nonce) != 32:
        raise ThinSubnetError("task nonce must be exactly 32 bytes")
    hidden_document = parse_strict_json(hidden_suite, maximum_bytes=MAX_TASK_BYTES)
    if hidden_suite != canonical_json(hidden_document):
        raise ThinSubnetError("hidden suite JSON must be canonical")
    if not isinstance(hidden_document, dict):
        raise ThinSubnetError("hidden suite must be a JSON object")
    _exact_keys(hidden_document, _HIDDEN_SUITE_KEYS, "hidden suite")
    if hidden_document["schema"] != HIDDEN_SUITE_SCHEMA:
        raise ThinSubnetError("unsupported hidden suite schema")
    if hidden_document["task_nonce_digest"] != sha256_digest(
        HIDDEN_NONCE_DOMAIN + nonce
    ):
        raise ThinSubnetError("hidden suite is bound to a different task nonce")
    features = sorted(feature_names)
    decisions = sorted(labels)
    rare = sorted(rare_labels)
    examples = sorted(
        (dict(row) for row in public_examples),
        key=lambda row: str(row.get("example_id", "")),
    )
    document: dict[str, Any] = {
        "schema": TASK_SCHEMA,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "task_class": task_class,
        "validator_hotkey": validator_hotkey,
        "miner_hotkey": miner_hotkey,
        "nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "issued_at": issued_at,
        "valid_from_block": valid_from_block,
        "valid_until_block": valid_until_block,
        "feature_names": features,
        "labels": decisions,
        "rare_labels": rare,
        "public_examples": examples,
        "metric_policy": {
            "compactness_max_terms": compactness_max_terms,
            "quality_floor": quality_floor,
            "rare_floor": rare_floor,
        },
    }
    document["hidden_suite_commitment"] = hidden_suite_commitment(
        hidden_suite, task_binding=document
    )
    _validate_task_body(document)
    return document


def _validate_task_body(document: Mapping[str, Any]) -> None:
    if document.get("schema") != TASK_SCHEMA:
        raise ThinSubnetError("unsupported verified policy task schema")
    network = document.get("network")
    if not isinstance(network, str) or not 1 <= len(network.encode("utf-8")) <= 128:
        raise ThinSubnetError("task network is invalid")
    _nonnegative_int(document.get("netuid"), "task netuid")
    _nonnegative_int(document.get("source_epoch"), "task source epoch")
    _identifier(document.get("task_class"), "task class")
    for name in ("validator_hotkey", "miner_hotkey"):
        value = document.get(name)
        if not isinstance(value, str) or not value:
            raise ThinSubnetError(f"task {name} is invalid")
    _canonical_b64(document.get("nonce_base64"), "task nonce", size=32)
    parse_time(document.get("issued_at"), "task issued_at")
    first = _nonnegative_int(document.get("valid_from_block"), "valid_from_block")
    last = _nonnegative_int(document.get("valid_until_block"), "valid_until_block")
    if last <= first:
        raise ThinSubnetError("task block window is empty")
    features_raw = document.get("feature_names")
    labels_raw = document.get("labels")
    rare_raw = document.get("rare_labels")
    if (
        not isinstance(features_raw, list)
        or not 1 <= len(features_raw) <= MAX_FEATURES
        or any(
            _IDENTIFIER_RE.fullmatch(item) is None
            for item in features_raw
            if isinstance(item, str)
        )
        or any(not isinstance(item, str) for item in features_raw)
        or features_raw != sorted(set(features_raw))
    ):
        raise ThinSubnetError("feature names must be sorted, unique identifiers")
    if (
        not isinstance(labels_raw, list)
        or not 2 <= len(labels_raw) <= MAX_LABELS
        or any(
            not isinstance(item, str) or _IDENTIFIER_RE.fullmatch(item) is None
            for item in labels_raw
        )
        or labels_raw != sorted(set(labels_raw))
    ):
        raise ThinSubnetError("labels must be sorted, unique identifiers")
    if (
        not isinstance(rare_raw, list)
        or not rare_raw
        or rare_raw != sorted(set(rare_raw))
        or not set(rare_raw).issubset(labels_raw)
    ):
        raise ThinSubnetError("rare labels must be a non-empty sorted label subset")
    _validate_examples(
        document.get("public_examples"),
        feature_names=tuple(features_raw),
        labels=tuple(labels_raw),
        label="public examples",
        minimum=2,
    )
    _digest(document.get("hidden_suite_commitment"), "hidden suite commitment")
    metric_policy = document.get("metric_policy")
    if not isinstance(metric_policy, dict):
        raise ThinSubnetError("metric policy must be an object")
    _exact_keys(metric_policy, _METRIC_POLICY_KEYS, "metric policy")
    _positive_int(metric_policy["compactness_max_terms"], "compactness_max_terms")
    _parse_floor(metric_policy["quality_floor"], "quality_floor")
    _parse_floor(metric_policy["rare_floor"], "rare_floor")


def sign_task(document: Mapping[str, Any], validator_keypair: Any) -> bytes:
    if "task_id" in document or "signature" in document:
        raise ThinSubnetError("unsigned task must omit task_id and signature")
    _validate_task_body(document)
    signer = str(getattr(validator_keypair, "ss58_address", "") or "")
    if signer != document["validator_hotkey"]:
        raise ThinSubnetError("task signer does not match validator_hotkey")
    signed = dict(document)
    signed["task_id"] = task_id_for(signed)
    signed["signature"] = _sign_sr25519(
        validator_keypair,
        TASK_DOMAIN + canonical_json(_signed(signed)),
        "verified policy task",
    )
    return canonical_json(signed)


@dataclass(frozen=True)
class PolicyTask:
    task_id: str
    network: str
    netuid: int
    source_epoch: int
    validator_hotkey: str
    miner_hotkey: str
    issued_at: datetime
    valid_from_block: int
    valid_until_block: int
    feature_names: tuple[str, ...]
    labels: tuple[str, ...]
    rare_labels: tuple[str, ...]
    public_examples: tuple[dict[str, Any], ...]
    hidden_suite_commitment: str
    quality_floor: Decimal
    rare_floor: Decimal
    compactness_max_terms: int
    document: dict[str, Any]


def verify_task(
    raw: bytes,
    *,
    network: str,
    netuid: int,
    current_block: int,
    now: datetime | None = None,
    expected_validator_hotkey: str | None = None,
    expected_miner_hotkey: str | None = None,
    max_age_seconds: int = 3600,
    max_future_seconds: int = 30,
    max_block_span: int = 7200,
) -> PolicyTask:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=MAX_TASK_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("verified policy task JSON must be canonical")
    _exact_keys(document, _TASK_KEYS, "verified policy task")
    _validate_task_body(document)
    if document["network"] != network or document["netuid"] != netuid:
        raise ThinSubnetError("verified policy task network or netuid mismatch")
    if (
        expected_validator_hotkey
        and document["validator_hotkey"] != expected_validator_hotkey
    ):
        raise ThinSubnetError("verified policy task validator mismatch")
    if expected_miner_hotkey and document["miner_hotkey"] != expected_miner_hotkey:
        raise ThinSubnetError("verified policy task miner mismatch")
    current_block = _nonnegative_int(current_block, "current block")
    first = document["valid_from_block"]
    last = document["valid_until_block"]
    if first > current_block or current_block >= last:
        raise ThinSubnetError("verified policy task is outside its block window")
    if last - first > max_block_span:
        raise ThinSubnetError("verified policy task block window is too wide")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ThinSubnetError("task verification time must be UTC")
    issued = parse_time(document["issued_at"], "task issued_at")
    age = (current - issued).total_seconds()
    if age < -max_future_seconds or age > max_age_seconds:
        raise ThinSubnetError("verified policy task is stale or future-dated")
    task_id = _digest(document["task_id"], "task id")
    if task_id != task_id_for(document):
        raise ThinSubnetError("task id does not match its canonical body")
    signature = _signature(document["signature"], "task")
    _verify_sr25519(
        document["validator_hotkey"],
        TASK_DOMAIN + canonical_json(_signed(document)),
        signature,
        "task",
    )
    metric = document["metric_policy"]
    examples = _validate_examples(
        document["public_examples"],
        feature_names=tuple(document["feature_names"]),
        labels=tuple(document["labels"]),
        label="public examples",
        minimum=2,
    )
    return PolicyTask(
        task_id=task_id,
        network=document["network"],
        netuid=document["netuid"],
        source_epoch=document["source_epoch"],
        validator_hotkey=document["validator_hotkey"],
        miner_hotkey=document["miner_hotkey"],
        issued_at=issued,
        valid_from_block=first,
        valid_until_block=last,
        feature_names=tuple(document["feature_names"]),
        labels=tuple(document["labels"]),
        rare_labels=tuple(document["rare_labels"]),
        public_examples=examples,
        hidden_suite_commitment=document["hidden_suite_commitment"],
        quality_floor=_parse_floor(metric["quality_floor"], "quality_floor"),
        rare_floor=_parse_floor(metric["rare_floor"], "rare_floor"),
        compactness_max_terms=metric["compactness_max_terms"],
        document=document,
    )


def _condition_matches(
    condition: Mapping[str, Any], features: Mapping[str, Any]
) -> bool:
    actual = features.get(condition["feature"])
    expected = condition["equals"]
    return type(actual) is type(expected) and actual == expected


def _rule_matches(rule: Mapping[str, Any], features: Mapping[str, Any]) -> bool:
    return all(_condition_matches(condition, features) for condition in rule["when"])


def _normalize_rules(raw: Any, task: PolicyTask) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or len(raw) > MAX_RULES:
        raise ThinSubnetError("policy rules must be a bounded list")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise ThinSubnetError("policy rule must be an object")
        _exact_keys(row, _RULE_KEYS, "policy rule")
        rule_id = _identifier(row["rule_id"], "rule id")
        if rule_id in seen:
            raise ThinSubnetError("policy rule ids must be unique")
        seen.add(rule_id)
        when = row["when"]
        if not isinstance(when, list) or not 1 <= len(when) <= MAX_CONDITIONS:
            raise ThinSubnetError("policy rule must contain bounded conditions")
        conditions: list[dict[str, Any]] = []
        for condition in when:
            if not isinstance(condition, dict):
                raise ThinSubnetError("policy condition must be an object")
            _exact_keys(condition, _CONDITION_KEYS, "policy condition")
            feature = _identifier(condition["feature"], "condition feature")
            if feature not in task.feature_names:
                raise ThinSubnetError("policy condition uses an unknown feature")
            conditions.append(
                {
                    "feature": feature,
                    "equals": _scalar(condition["equals"], "condition value"),
                }
            )
        condition_features = [condition["feature"] for condition in conditions]
        if condition_features != sorted(set(condition_features)):
            raise ThinSubnetError("policy conditions must be sorted and unique")
        decision = _identifier(row["then"], "policy decision")
        if decision not in task.labels:
            raise ThinSubnetError("policy rule decision is not allowed")
        support = row["support_example_ids"]
        if (
            not isinstance(support, list)
            or len(support) > MAX_EXAMPLES
            or any(
                not isinstance(item, str) or _IDENTIFIER_RE.fullmatch(item) is None
                for item in support
            )
            or support != sorted(set(support))
        ):
            raise ThinSubnetError("rule support ids must be sorted and unique")
        output.append(
            {
                "rule_id": rule_id,
                "when": conditions,
                "then": decision,
                "support_example_ids": support,
            }
        )
    return tuple(output)


def make_artifact(
    task: PolicyTask,
    *,
    rules: Iterable[Mapping[str, Any]],
    default_label: str,
    created_at: str,
) -> dict[str, Any]:
    document = {
        "schema": ARTIFACT_SCHEMA,
        "task_id": task.task_id,
        "miner_hotkey": task.miner_hotkey,
        "created_at": created_at,
        "rules": [dict(rule) for rule in rules],
        "default_label": default_label,
    }
    _validate_artifact_body(document, task)
    return document


def _validate_artifact_body(document: Mapping[str, Any], task: PolicyTask) -> None:
    if document.get("schema") != ARTIFACT_SCHEMA:
        raise ThinSubnetError("unsupported verified policy artifact schema")
    if document.get("task_id") != task.task_id:
        raise ThinSubnetError("policy artifact task mismatch")
    if document.get("miner_hotkey") != task.miner_hotkey:
        raise ThinSubnetError("policy artifact miner mismatch")
    created = parse_time(document.get("created_at"), "artifact created_at")
    if created < task.issued_at:
        raise ThinSubnetError("policy artifact predates its task")
    default = _identifier(document.get("default_label"), "default label")
    if default not in task.labels:
        raise ThinSubnetError("policy artifact default label is not allowed")
    _normalize_rules(document.get("rules"), task)


def artifact_id_for(document: Mapping[str, Any]) -> str:
    return sha256_digest(
        ARTIFACT_ID_DOMAIN + canonical_json(_unsigned(document, "artifact_id"))
    )


def sign_artifact(
    document: Mapping[str, Any], miner_keypair: Any, task: PolicyTask
) -> bytes:
    if "artifact_id" in document or "signature" in document:
        raise ThinSubnetError("unsigned artifact must omit artifact_id and signature")
    _validate_artifact_body(document, task)
    signer = str(getattr(miner_keypair, "ss58_address", "") or "")
    if signer != task.miner_hotkey:
        raise ThinSubnetError("artifact signer does not match miner_hotkey")
    signed = dict(document)
    signed["artifact_id"] = artifact_id_for(signed)
    signed["signature"] = _sign_sr25519(
        miner_keypair,
        ARTIFACT_DOMAIN + canonical_json(_signed(signed)),
        "verified policy artifact",
    )
    return canonical_json(signed)


@dataclass(frozen=True)
class PolicyArtifact:
    artifact_id: str
    task_id: str
    miner_hotkey: str
    created_at: datetime
    rules: tuple[dict[str, Any], ...]
    default_label: str
    document: dict[str, Any]


def verify_artifact(raw: bytes, task: PolicyTask) -> PolicyArtifact:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=MAX_ARTIFACT_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("verified policy artifact JSON must be canonical")
    _exact_keys(document, _ARTIFACT_KEYS, "verified policy artifact")
    _validate_artifact_body(document, task)
    artifact_id = _digest(document["artifact_id"], "artifact id")
    if artifact_id != artifact_id_for(document):
        raise ThinSubnetError("artifact id does not match its canonical body")
    signature = _signature(document["signature"], "artifact")
    _verify_sr25519(
        task.miner_hotkey,
        ARTIFACT_DOMAIN + canonical_json(_signed(document)),
        signature,
        "artifact",
    )
    return PolicyArtifact(
        artifact_id=artifact_id,
        task_id=task.task_id,
        miner_hotkey=task.miner_hotkey,
        created_at=parse_time(document["created_at"], "artifact created_at"),
        rules=_normalize_rules(document["rules"], task),
        default_label=document["default_label"],
        document=document,
    )


def predict(artifact: PolicyArtifact, features: Mapping[str, Any]) -> str:
    for rule in artifact.rules:
        if _rule_matches(rule, features):
            return rule["then"]
    return artifact.default_label


def induce_policy(task: PolicyTask, *, created_at: str) -> dict[str, Any]:
    """Reference miner: greedily generalize pure rules from public examples."""

    examples = list(task.public_examples)
    candidates: dict[bytes, dict[str, Any]] = {}
    for example in examples:
        conditions = [
            {"feature": feature, "equals": example["features"][feature]}
            for feature in task.feature_names
        ]
        for feature in task.feature_names:
            trial = [row for row in conditions if row["feature"] != feature]
            matched = [
                row
                for row in examples
                if all(
                    _condition_matches(condition, row["features"])
                    for condition in trial
                )
            ]
            if (
                trial
                and matched
                and {row["label"] for row in matched} == {example["label"]}
            ):
                conditions = trial
        support = sorted(
            row["example_id"]
            for row in examples
            if row["label"] == example["label"]
            and all(
                _condition_matches(condition, row["features"])
                for condition in conditions
            )
        )
        key = canonical_json({"when": conditions, "then": example["label"]})
        candidates[key] = {
            "when": conditions,
            "then": example["label"],
            "support_example_ids": support,
        }
    ordered = sorted(
        candidates.values(),
        key=lambda row: (
            0 if row["then"] in task.rare_labels else 1,
            len(row["when"]),
            canonical_json(row),
        ),
    )
    rules = [
        {"rule_id": f"rule-{index:04d}", **row}
        for index, row in enumerate(ordered, start=1)
    ]
    counts = Counter(row["label"] for row in examples)
    default_label = min(task.labels, key=lambda item: (-counts[item], item))
    return make_artifact(
        task,
        rules=rules,
        default_label=default_label,
        created_at=created_at,
    )


def _hidden_cases(raw: bytes, task: PolicyTask) -> tuple[dict[str, Any], ...]:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    if (
        hidden_suite_commitment(encoded, task_binding=task.document)
        != task.hidden_suite_commitment
    ):
        raise ThinSubnetError("hidden suite commitment mismatch")
    document = parse_strict_json(encoded, maximum_bytes=MAX_TASK_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("hidden suite JSON must be canonical")
    _exact_keys(document, _HIDDEN_SUITE_KEYS, "hidden suite")
    if document["schema"] != HIDDEN_SUITE_SCHEMA:
        raise ThinSubnetError("unsupported hidden suite schema")
    nonce = _canonical_b64(task.document["nonce_base64"], "task nonce", size=32)
    if document["task_nonce_digest"] != sha256_digest(HIDDEN_NONCE_DOMAIN + nonce):
        raise ThinSubnetError("hidden suite is bound to a different task nonce")
    _canonical_b64(document["salt_base64"], "hidden suite salt", size=32)
    cases = _validate_examples(
        document["cases"],
        feature_names=task.feature_names,
        labels=task.labels,
        label="hidden suite",
        minimum=len(task.labels),
    )
    counts = Counter(row["label"] for row in cases)
    if any(counts[label] == 0 for label in task.labels):
        raise ThinSubnetError("hidden suite must cover every task label")
    return cases


def _evidence_faithfulness(task: PolicyTask, artifact: PolicyArtifact) -> str:
    if not artifact.rules:
        return "0"
    by_id = {row["example_id"]: row for row in task.public_examples}
    valid = 0
    for rule in artifact.rules:
        support = rule["support_example_ids"]
        rows = [by_id.get(item) for item in support]
        if support and all(
            row is not None
            and row["label"] == rule["then"]
            and _rule_matches(rule, row["features"])
            for row in rows
        ):
            valid += 1
    return _ratio(valid, len(artifact.rules))


def _execution_binding(
    task: PolicyTask,
    artifact: PolicyArtifact,
    receipt: VerifiedInferenceReceipt | None,
) -> dict[str, Any]:
    if receipt is None:
        return {
            "receipt_id": None,
            "request_id": None,
            "output_commitment_bound": False,
            "attestation_verified": False,
            "verifier_digest": None,
        }
    document = receipt.document
    if document.get("network") != task.network or document.get("netuid") != task.netuid:
        raise ThinSubnetError("execution receipt network or netuid mismatch")
    if receipt.miner_hotkey != artifact.miner_hotkey:
        raise ThinSubnetError("execution receipt miner mismatch")
    artifact_raw = canonical_json(artifact.document)
    if document["result"]["output_commitment"] != commit_payload(
        "output", artifact_raw
    ):
        raise ThinSubnetError("execution receipt does not bind the policy artifact")
    return {
        "receipt_id": receipt.receipt_id,
        "request_id": receipt.request_id,
        "output_commitment_bound": True,
        "attestation_verified": receipt.attestation_verified,
        "verifier_digest": receipt.verifier_digest,
    }


def evaluation_id_for(document: Mapping[str, Any]) -> str:
    return sha256_digest(
        EVALUATION_ID_DOMAIN + canonical_json(_unsigned(document, "evaluation_id"))
    )


def evaluate_policy(
    task: PolicyTask,
    artifact: PolicyArtifact,
    hidden_suite: bytes,
    *,
    evaluated_at: str,
    receipt: VerifiedInferenceReceipt | None = None,
) -> dict[str, Any]:
    cases = _hidden_cases(hidden_suite, task)
    results: list[dict[str, Any]] = []
    correct_by_label = Counter()
    total_by_label = Counter()
    for row in cases:
        predicted = predict(artifact, row["features"])
        correct = predicted == row["label"]
        total_by_label[row["label"]] += 1
        correct_by_label[row["label"]] += int(correct)
        results.append(
            {
                "example_id": row["example_id"],
                "predicted": predicted,
                "correct": correct,
            }
        )
    with localcontext() as context:
        context.prec = 28
        balanced = sum(
            Decimal(correct_by_label[label]) / Decimal(total_by_label[label])
            for label in task.labels
        ) / Decimal(len(task.labels))
        rare = sum(
            Decimal(correct_by_label[label]) / Decimal(total_by_label[label])
            for label in task.rare_labels
        ) / Decimal(len(task.rare_labels))
    term_count = len(artifact.rules) + sum(len(row["when"]) for row in artifact.rules)
    compact_numerator = max(0, task.compactness_max_terms - term_count)
    compactness = _ratio(compact_numerator, task.compactness_max_terms)
    faithfulness = _evidence_faithfulness(task, artifact)
    rare_score = _score_decimal(rare)
    if balanced < task.quality_floor or rare < task.rare_floor:
        compactness = "0"
        faithfulness = "0"
        rare_score = "0"
    execution = _execution_binding(task, artifact, receipt)
    metrics = {
        "policy_balanced_accuracy": _score_decimal(balanced),
        "policy_rare_recall": rare_score,
        "policy_evidence_faithfulness": faithfulness,
        "policy_compactness": compactness,
        "policy_artifact_signature": "1",
        "policy_attested_execution": "1" if execution["attestation_verified"] else "0",
    }
    return {
        "schema": EVALUATION_SCHEMA,
        "network": task.network,
        "netuid": task.netuid,
        "source_epoch": task.source_epoch,
        "task_id": task.task_id,
        "artifact_id": artifact.artifact_id,
        "validator_hotkey": task.validator_hotkey,
        "miner_hotkey": artifact.miner_hotkey,
        "evaluated_at": evaluated_at,
        "task_valid_from_block": task.valid_from_block,
        "task_valid_until_block": task.valid_until_block,
        "hidden_suite_commitment": task.hidden_suite_commitment,
        "case_count": len(cases),
        "rare_case_count": sum(total_by_label[label] for label in task.rare_labels),
        "case_results_digest": sha256_digest(
            CASE_RESULTS_DOMAIN + canonical_json({"results": results})
        ),
        "metrics": metrics,
        "execution": execution,
    }


def _validate_evaluation_body(
    document: Mapping[str, Any], task: PolicyTask, artifact: PolicyArtifact
) -> None:
    if document.get("schema") != EVALUATION_SCHEMA:
        raise ThinSubnetError("unsupported policy evaluation schema")
    for key, expected in (
        ("network", task.network),
        ("netuid", task.netuid),
        ("source_epoch", task.source_epoch),
        ("task_id", task.task_id),
        ("artifact_id", artifact.artifact_id),
        ("validator_hotkey", task.validator_hotkey),
        ("miner_hotkey", artifact.miner_hotkey),
        ("hidden_suite_commitment", task.hidden_suite_commitment),
        ("task_valid_from_block", task.valid_from_block),
        ("task_valid_until_block", task.valid_until_block),
    ):
        if document.get(key) != expected:
            raise ThinSubnetError(f"policy evaluation {key} mismatch")
    parse_time(document.get("evaluated_at"), "evaluation evaluated_at")
    _positive_int(document.get("case_count"), "evaluation case_count")
    _positive_int(document.get("rare_case_count"), "evaluation rare_case_count")
    _digest(document.get("case_results_digest"), "case results digest")
    metrics = document.get("metrics")
    if not isinstance(metrics, dict):
        raise ThinSubnetError("evaluation metrics must be an object")
    _exact_keys(metrics, _METRIC_KEYS, "evaluation metrics")
    for name, value in metrics.items():
        _parse_floor(value, name)
    execution = document.get("execution")
    if not isinstance(execution, dict):
        raise ThinSubnetError("evaluation execution must be an object")
    _exact_keys(execution, _EXECUTION_KEYS, "evaluation execution")
    if not isinstance(execution["output_commitment_bound"], bool) or not isinstance(
        execution["attestation_verified"], bool
    ):
        raise ThinSubnetError("evaluation execution flags must be boolean")
    if execution["attestation_verified"] and not execution["output_commitment_bound"]:
        raise ThinSubnetError("attested execution must bind the artifact output")
    for name in ("receipt_id", "request_id", "verifier_digest"):
        value = execution[name]
        if value is not None:
            _digest(value, f"execution {name}")


def sign_evaluation(
    document: Mapping[str, Any],
    validator_keypair: Any,
    task: PolicyTask,
    artifact: PolicyArtifact,
) -> bytes:
    if "evaluation_id" in document or "signature" in document:
        raise ThinSubnetError(
            "unsigned evaluation must omit evaluation_id and signature"
        )
    _validate_evaluation_body(document, task, artifact)
    signer = str(getattr(validator_keypair, "ss58_address", "") or "")
    if signer != task.validator_hotkey:
        raise ThinSubnetError("evaluation signer does not match validator_hotkey")
    signed = dict(document)
    signed["evaluation_id"] = evaluation_id_for(signed)
    signed["signature"] = _sign_sr25519(
        validator_keypair,
        EVALUATION_DOMAIN + canonical_json(_signed(signed)),
        "verified policy evaluation",
    )
    return canonical_json(signed)


@dataclass(frozen=True)
class PolicyEvaluation:
    evaluation_id: str
    network: str
    netuid: int
    source_epoch: int
    task_id: str
    artifact_id: str
    miner_hotkey: str
    evaluated_at: datetime
    task_valid_from_block: int
    task_valid_until_block: int
    metrics: dict[str, str]
    execution: dict[str, Any]
    document: dict[str, Any]


def verify_evaluation(
    raw: bytes,
    task: PolicyTask,
    artifact: PolicyArtifact,
    hidden_suite: bytes,
    *,
    receipt: VerifiedInferenceReceipt | None = None,
) -> PolicyEvaluation:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=MAX_EVALUATION_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("policy evaluation JSON must be canonical")
    _exact_keys(document, _EVALUATION_KEYS, "policy evaluation")
    _validate_evaluation_body(document, task, artifact)
    evaluation_id = _digest(document["evaluation_id"], "evaluation id")
    if evaluation_id != evaluation_id_for(document):
        raise ThinSubnetError("evaluation id does not match its canonical body")
    signature = _signature(document["signature"], "evaluation")
    _verify_sr25519(
        task.validator_hotkey,
        EVALUATION_DOMAIN + canonical_json(_signed(document)),
        signature,
        "evaluation",
    )
    recomputed = evaluate_policy(
        task,
        artifact,
        hidden_suite,
        evaluated_at=document["evaluated_at"],
        receipt=receipt,
    )
    if recomputed != _unsigned(document, "evaluation_id"):
        raise ThinSubnetError("policy evaluation does not match deterministic replay")
    return PolicyEvaluation(
        evaluation_id=evaluation_id,
        network=task.network,
        netuid=task.netuid,
        source_epoch=task.source_epoch,
        task_id=task.task_id,
        artifact_id=artifact.artifact_id,
        miner_hotkey=artifact.miner_hotkey,
        evaluated_at=parse_time(document["evaluated_at"], "evaluation evaluated_at"),
        task_valid_from_block=task.valid_from_block,
        task_valid_until_block=task.valid_until_block,
        metrics=dict(document["metrics"]),
        execution=dict(document["execution"]),
        document=document,
    )


def verifier_digest() -> str:
    try:
        source = Path(__file__).read_bytes()
    except OSError as exc:
        raise ThinSubnetError("could not hash verified policy implementation") from exc
    return sha256_digest(VERIFIER_DOMAIN + hashlib.sha256(source).digest())


def score_report_body_from_evaluations(
    evaluations: Iterable[PolicyEvaluation],
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
    score_verifier_digest: str,
    previous_report_id: str | None = None,
    evidence_uri: str | None = None,
) -> dict[str, Any]:
    rows = sorted(evaluations, key=lambda item: item.miner_hotkey)
    if not rows or len({row.miner_hotkey for row in rows}) != len(rows):
        raise ThinSubnetError("policy evaluations must contain unique miners")
    report_generated_at = parse_time(generated_at, "generated_at")
    report_valid_until = parse_time(valid_until, "valid_until")
    first_block = _nonnegative_int(valid_from_block, "valid_from_block")
    last_block = _nonnegative_int(valid_until_block, "valid_until_block")
    if last_block <= first_block:
        raise ThinSubnetError("score report block window is empty")
    entries = []
    for row in rows:
        if row.network != network or row.netuid != netuid:
            raise ThinSubnetError("policy evaluation network or netuid mismatch")
        if row.source_epoch != source_epoch:
            raise ThinSubnetError("policy evaluation source epoch mismatch")
        if not (
            row.task_valid_from_block
            <= first_block
            < last_block
            <= row.task_valid_until_block
        ):
            raise ThinSubnetError("score report exceeds an evaluation task window")
        if row.evaluated_at > report_generated_at:
            raise ThinSubnetError("score report predates a policy evaluation")
        reasons = [
            "artifact_signature_verified",
            "hidden_suite_commitment_verified",
            "policy_replayed",
            "rare_cases_scored",
            "validator_evaluation_signed",
        ]
        if row.execution["output_commitment_bound"]:
            reasons.append("execution_output_bound")
        if row.execution["attestation_verified"]:
            reasons.append("attestation_verified")
        entries.append(
            {
                "miner_hotkey": row.miner_hotkey,
                "metrics": row.metrics,
                "asserted_score": None,
                "reason_codes": sorted(reasons),
                "evidence": [
                    {
                        "kind": EVALUATION_SCHEMA,
                        "id": row.evaluation_id,
                        "digest": row.evaluation_id,
                        "uri": evidence_uri,
                    }
                ],
            }
        )
    return {
        "schema": REPORT_SCHEMA,
        "network": network,
        "netuid": netuid,
        "class_id": _identifier(class_id, "class id"),
        "source_id": _identifier(source_id, "source id"),
        "source_epoch": _nonnegative_int(source_epoch, "source epoch"),
        "generated_at": format_time(report_generated_at),
        "valid_until": format_time(report_valid_until),
        "valid_from_block": first_block,
        "valid_until_block": last_block,
        "complete": True,
        "policy_digest": _digest(policy_digest, "score policy digest"),
        "verifier_digest": _digest(score_verifier_digest, "score verifier digest"),
        "previous_report_id": previous_report_id,
        "entries": entries,
        "signing_key_id": _identifier(signing_key_id, "signing key id"),
    }
