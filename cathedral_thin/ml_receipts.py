"""Portable, validator-verifiable ML inference receipts.

The receipt deliberately separates three claims:

* the miner hotkey signed the canonical execution claim;
* the input, parameters, model, runtime, and output are immutably committed;
* a validator-pinned attestation verifier confirmed the execution binding.

Only the third claim makes ``attestation_verified`` true.  A signed receipt
without independently verified evidence is useful provenance, but it is not
proof that the committed model actually ran.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import urllib.parse
from typing import Any, Callable, Iterable, Mapping

from .core import ThinSubnetError
from .score_classes import canonical_json, format_time, parse_strict_json, parse_time


RECEIPT_SCHEMA = "cathedral_ml_inference_receipt_v1"
BUNDLE_SCHEMA = "cathedral_ml_inference_bundle_v1"
REQUEST_AUTHORIZATION_SCHEMA = "cathedral_ml_request_authorization_v1"
BUNDLE_CHECKPOINT_SCHEMA = "cathedral_ml_bundle_checkpoint_v1"
RECEIPT_DOMAIN = b"cathedral-ml-inference-receipt-v1\x00"
RECEIPT_ID_DOMAIN = b"cathedral-ml-inference-receipt-id-v1\x00"
BUNDLE_ID_DOMAIN = b"cathedral-ml-inference-bundle-id-v1\x00"
REQUEST_AUTHORIZATION_DOMAIN = b"cathedral-ml-request-authorization-v1\x00"
INPUT_COMMITMENT_DOMAIN = b"cathedral-ml-input-v1\x00"
PARAMETERS_COMMITMENT_DOMAIN = b"cathedral-ml-parameters-v1\x00"
OUTPUT_COMMITMENT_DOMAIN = b"cathedral-ml-output-v1\x00"
IDENTITY_BINDING_DOMAIN = b"cathedral-ml-identity-binding-v1\x00"
EXECUTION_BINDING_DOMAIN = b"cathedral-ml-execution-binding-v1\x00"

MAX_RECEIPT_BYTES = 262_144
MAX_BUNDLE_BYTES = 16_777_216
MAX_BUNDLE_RECEIPTS = 4096
MAX_ATTESTATION_BYTES = 1_048_576
MAX_MODEL_ID_BYTES = 256
MAX_KEY_BYTES = 128
MAX_WORK_UNITS_PER_RECEIPT = Decimal("1000000000000")

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,29})(?:\.[0-9]{1,12})?")
_REPORT_DATA_RE = re.compile(r"[0-9a-f]{128}")

_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "network",
        "netuid",
        "source_epoch",
        "request_id",
        "validator_hotkey",
        "miner_hotkey",
        "nonce_base64",
        "issued_at",
        "completed_at",
        "valid_from_block",
        "valid_until_block",
        "model",
        "runtime",
        "request",
        "request_authorization",
        "result",
        "attestation",
        "receipt_id",
        "signature",
    }
)
_MODEL_KEYS = frozenset({"model_id", "weights_digest", "tokenizer_digest"})
_RUNTIME_KEYS = frozenset({"image_digest", "runner_digest"})
_REQUEST_KEYS = frozenset({"input_commitment", "parameters_commitment"})
_RESULT_KEYS = frozenset(
    {"output_commitment", "input_tokens", "output_tokens", "latency_ms", "work_units"}
)
_ATTESTATION_KEYS = frozenset(
    {
        "kind",
        "evidence_digest",
        "evidence_uri",
        "policy_digest",
        "report_data_hex",
    }
)
_SIGNATURE_KEYS = frozenset({"algorithm", "value_base64"})
_REQUEST_AUTHORIZATION_FILE_KEYS = frozenset(
    {"schema", "request_id", "validator_hotkey", "miner_hotkey", "signature"}
)
_BUNDLE_CHECKPOINT_KEYS = frozenset(
    {"schema", "network", "netuid", "source_epoch", "bundle_id", "generated_at"}
)
_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "network",
        "netuid",
        "source_epoch",
        "generated_at",
        "valid_until",
        "valid_from_block",
        "valid_until_block",
        "previous_bundle_id",
        "receipts",
        "bundle_id",
    }
)


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if frozenset(value) != expected:
        missing = sorted(expected - frozenset(value))
        unknown = sorted(frozenset(value) - expected)
        raise ThinSubnetError(
            f"{label} fields mismatch missing={missing} unknown={unknown}"
        )


def sha256_digest(data: bytes) -> str:
    if not isinstance(data, bytes):
        raise ThinSubnetError("digest input must be bytes")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    try:
        with Path(path).expanduser().open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
    except OSError as exc:
        raise ThinSubnetError(f"could not hash file: {path}") from exc
    return "sha256:" + hasher.hexdigest()


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ThinSubnetError(f"{label} must be a canonical non-negative decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ThinSubnetError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ThinSubnetError(f"invalid {label}")
    return parsed


def canonical_decimal(value: Decimal | int | str) -> str:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ThinSubnetError("value is not a finite non-negative decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ThinSubnetError("value is not a finite non-negative decimal")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered == "-0":
        rendered = "0"
    if _DECIMAL_RE.fullmatch(rendered) is None:
        raise ThinSubnetError("decimal exceeds canonical precision or range")
    return rendered


def commit_payload(kind: str, payload: bytes) -> str:
    domains = {
        "input": INPUT_COMMITMENT_DOMAIN,
        "parameters": PARAMETERS_COMMITMENT_DOMAIN,
        "output": OUTPUT_COMMITMENT_DOMAIN,
    }
    try:
        domain = domains[kind]
    except KeyError as exc:
        raise ThinSubnetError(
            "commitment kind must be input, parameters, or output"
        ) from exc
    if not isinstance(payload, bytes):
        raise ThinSubnetError("commitment payload must be bytes")
    framed = len(payload).to_bytes(8, "big") + payload
    return sha256_digest(domain + framed)


def request_id_for(
    *,
    network: str,
    netuid: int,
    source_epoch: int,
    validator_hotkey: str,
    miner_hotkey: str,
    nonce: bytes,
    input_commitment: str,
    parameters_commitment: str,
) -> str:
    body = {
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "validator_hotkey": validator_hotkey,
        "miner_hotkey": miner_hotkey,
        "nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "input_commitment": input_commitment,
        "parameters_commitment": parameters_commitment,
    }
    return sha256_digest(b"cathedral-ml-request-v1\x00" + canonical_json(body))


def _unsigned_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in document.items()
        if key not in {"receipt_id", "signature"}
    }


def _signed_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in document.items() if key != "signature"}


def _request_authorization_body(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: document[key]
        for key in (
            "network",
            "netuid",
            "source_epoch",
            "request_id",
            "validator_hotkey",
            "miner_hotkey",
            "nonce_base64",
            "issued_at",
            "valid_from_block",
            "valid_until_block",
            "model",
            "runtime",
            "request",
        )
    }


def sign_request_authorization(document: Mapping[str, Any], keypair: Any) -> bytes:
    """Sign the exact model request before inference begins.

    The returned authorization is portable, but it cannot be applied to a
    different miner, nonce, prompt, parameters, model, runtime, or block
    window. The output is deliberately absent because it does not exist yet.
    """

    validator_hotkey = str(document.get("validator_hotkey", ""))
    signer_hotkey = str(getattr(keypair, "ss58_address", "") or "")
    if not validator_hotkey or signer_hotkey != validator_hotkey:
        raise ThinSubnetError(
            "request authorization signer does not match validator_hotkey"
        )
    try:
        signature = bytes(
            keypair.sign(
                REQUEST_AUTHORIZATION_DOMAIN
                + canonical_json(_request_authorization_body(document))
            )
        )
    except Exception as exc:
        raise ThinSubnetError("could not sign ML request authorization") from exc
    if len(signature) != 64:
        raise ThinSubnetError("request authorization signature must be 64 bytes")
    authorization = {
        "schema": REQUEST_AUTHORIZATION_SCHEMA,
        "request_id": document["request_id"],
        "validator_hotkey": validator_hotkey,
        "miner_hotkey": document["miner_hotkey"],
        "signature": {
            "algorithm": "sr25519",
            "value_base64": base64.b64encode(signature).decode("ascii"),
        },
    }
    return canonical_json(authorization)


def _verify_request_authorization(document: Mapping[str, Any]) -> bool:
    authorization = document.get("request_authorization")
    if authorization is None:
        return False
    if not isinstance(authorization, dict):
        raise ThinSubnetError("receipt request authorization must be an object")
    _exact_keys(
        authorization,
        _SIGNATURE_KEYS,
        "receipt request authorization",
    )
    if authorization["algorithm"] != "sr25519":
        raise ThinSubnetError("unsupported request authorization algorithm")
    try:
        signature = base64.b64decode(authorization["value_base64"], validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ThinSubnetError(
            "request authorization signature is not canonical base64"
        ) from exc
    if (
        len(signature) != 64
        or base64.b64encode(signature).decode("ascii") != authorization["value_base64"]
    ):
        raise ThinSubnetError("request authorization signature must be 64 bytes")
    validator_hotkey = str(document.get("validator_hotkey", ""))
    try:
        from bittensor_wallet import Keypair

        valid = Keypair(ss58_address=validator_hotkey).verify(
            REQUEST_AUTHORIZATION_DOMAIN
            + canonical_json(_request_authorization_body(document)),
            signature,
        )
    except Exception as exc:
        raise ThinSubnetError("receipt validator hotkey is invalid") from exc
    if not valid:
        raise ThinSubnetError("request authorization signature verification failed")
    return True


def apply_request_authorization(
    document: Mapping[str, Any], raw: bytes
) -> dict[str, Any]:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    authorization = parse_strict_json(encoded, maximum_bytes=16_384)
    if encoded != canonical_json(authorization):
        raise ThinSubnetError("ML request authorization JSON must be canonical")
    _exact_keys(
        authorization,
        _REQUEST_AUTHORIZATION_FILE_KEYS,
        "ML request authorization",
    )
    if authorization["schema"] != REQUEST_AUTHORIZATION_SCHEMA:
        raise ThinSubnetError("unsupported ML request authorization schema")
    for key in ("request_id", "validator_hotkey", "miner_hotkey"):
        if authorization[key] != document.get(key):
            raise ThinSubnetError(f"request authorization {key} mismatch")
    signed = dict(document)
    if signed.get("request_authorization") is not None:
        raise ThinSubnetError("receipt already has a request authorization")
    signed["request_authorization"] = authorization["signature"]
    _verify_request_authorization(signed)
    return signed


def receipt_id_for(document: Mapping[str, Any]) -> str:
    return sha256_digest(
        RECEIPT_ID_DOMAIN + canonical_json(_unsigned_receipt(document))
    )


def expected_report_data(document: Mapping[str, Any]) -> bytes:
    identity = {
        key: document[key]
        for key in (
            "network",
            "netuid",
            "source_epoch",
            "request_id",
            "validator_hotkey",
            "miner_hotkey",
            "nonce_base64",
            "issued_at",
            "completed_at",
            "valid_from_block",
            "valid_until_block",
        )
    }
    execution = {
        key: document[key]
        for key in ("request_id", "model", "runtime", "request", "result")
    }
    return (
        hashlib.sha256(IDENTITY_BINDING_DOMAIN + canonical_json(identity)).digest()
        + hashlib.sha256(EXECUTION_BINDING_DOMAIN + canonical_json(execution)).digest()
    )


def make_unsigned_receipt(
    *,
    network: str,
    netuid: int,
    source_epoch: int,
    validator_hotkey: str,
    miner_hotkey: str,
    nonce: bytes,
    issued_at: str,
    completed_at: str,
    valid_from_block: int,
    valid_until_block: int,
    model_id: str,
    weights_digest: str,
    tokenizer_digest: str | None,
    image_digest: str,
    runner_digest: str,
    input_commitment: str,
    parameters_commitment: str,
    output_commitment: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: Decimal | int | str,
    work_units: Decimal | int | str,
    attestation_kind: str = "none",
    attestation_evidence_digest: str | None = None,
    attestation_evidence_uri: str | None = None,
    attestation_policy_digest: str | None = None,
) -> dict[str, Any]:
    request_id = request_id_for(
        network=network,
        netuid=netuid,
        source_epoch=source_epoch,
        validator_hotkey=validator_hotkey,
        miner_hotkey=miner_hotkey,
        nonce=nonce,
        input_commitment=input_commitment,
        parameters_commitment=parameters_commitment,
    )
    document: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "request_id": request_id,
        "validator_hotkey": validator_hotkey,
        "miner_hotkey": miner_hotkey,
        "nonce_base64": base64.b64encode(nonce).decode("ascii"),
        "issued_at": issued_at,
        "completed_at": completed_at,
        "valid_from_block": valid_from_block,
        "valid_until_block": valid_until_block,
        "model": {
            "model_id": model_id,
            "weights_digest": weights_digest,
            "tokenizer_digest": tokenizer_digest,
        },
        "runtime": {
            "image_digest": image_digest,
            "runner_digest": runner_digest,
        },
        "request": {
            "input_commitment": input_commitment,
            "parameters_commitment": parameters_commitment,
        },
        "request_authorization": None,
        "result": {
            "output_commitment": output_commitment,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": canonical_decimal(latency_ms),
            "work_units": canonical_decimal(work_units),
        },
        "attestation": {
            "kind": attestation_kind,
            "evidence_digest": attestation_evidence_digest,
            "evidence_uri": attestation_evidence_uri,
            "policy_digest": attestation_policy_digest,
            "report_data_hex": None,
        },
    }
    if attestation_kind == "tdx":
        document["attestation"]["report_data_hex"] = expected_report_data(
            document
        ).hex()
    return document


def sign_receipt(document: Mapping[str, Any], keypair: Any) -> bytes:
    if "receipt_id" in document or "signature" in document:
        raise ThinSubnetError("unsigned receipt must omit receipt_id and signature")
    miner_hotkey = str(document.get("miner_hotkey", ""))
    signer_hotkey = str(getattr(keypair, "ss58_address", "") or "")
    if not miner_hotkey or signer_hotkey != miner_hotkey:
        raise ThinSubnetError("receipt signer does not match miner_hotkey")
    signed = dict(document)
    signed["receipt_id"] = receipt_id_for(signed)
    try:
        signature = bytes(
            keypair.sign(RECEIPT_DOMAIN + canonical_json(_signed_receipt(signed)))
        )
    except Exception as exc:
        raise ThinSubnetError("could not sign ML inference receipt") from exc
    if len(signature) != 64:
        raise ThinSubnetError("receipt signature must be 64 bytes")
    signed["signature"] = {
        "algorithm": "sr25519",
        "value_base64": base64.b64encode(signature).decode("ascii"),
    }
    return canonical_json(signed)


@dataclass(frozen=True)
class ReceiptPolicy:
    network: str
    netuid: int
    current_block: int
    max_age_seconds: int = 3600
    max_future_seconds: int = 30
    max_block_span: int = 7200
    require_attestation: bool = True
    expected_validator_hotkey: str | None = None
    allowed_model_digests: frozenset[str] = frozenset()
    allowed_image_digests: frozenset[str] = frozenset()
    allowed_runner_digests: frozenset[str] = frozenset()
    allowed_attestation_policy_digests: frozenset[str] = frozenset()
    allowed_verifier_digests: frozenset[str] = frozenset()


@dataclass(frozen=True)
class AttestationVerification:
    ok: bool
    verifier_digest: str
    measurement_digest: str | None
    reason: str


@dataclass(frozen=True)
class VerifiedInferenceReceipt:
    receipt_id: str
    request_id: str
    source_epoch: int
    miner_hotkey: str
    validator_hotkey: str
    weights_digest: str
    image_digest: str
    runner_digest: str
    completed_at: datetime
    work_units: Decimal
    input_tokens: int
    output_tokens: int
    attestation_verified: bool
    verifier_digest: str | None
    measurement_digest: str | None
    document: dict[str, Any]


AttestationVerifier = Callable[[bytes, bytes, str], AttestationVerification]


def _bounded_string(value: Any, label: str, *, maximum: int = MAX_KEY_BYTES) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= maximum:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ThinSubnetError(f"invalid {label}")
    return value


def _nonce(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ThinSubnetError("nonce must be canonical base64")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ThinSubnetError("nonce must be canonical base64") from exc
    if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
        raise ThinSubnetError("nonce must contain exactly 32 bytes")
    return raw


def _evidence_uri(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 2048:
        raise ThinSubnetError("invalid attestation evidence URI")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"https", "ipfs"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ThinSubnetError(
            "attestation evidence URI must be credential-free HTTPS or IPFS"
        )
    return value


def _validate_policy(policy: ReceiptPolicy) -> None:
    if (
        not isinstance(policy.network, str)
        or isinstance(policy.netuid, bool)
        or not isinstance(policy.netuid, int)
        or policy.netuid < 0
        or isinstance(policy.current_block, bool)
        or not isinstance(policy.current_block, int)
        or policy.current_block < 0
    ):
        raise ThinSubnetError("receipt policy network, netuid, or block is invalid")
    for name in ("max_age_seconds", "max_future_seconds", "max_block_span"):
        value = getattr(policy, name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ThinSubnetError(f"receipt policy {name} must be positive")
    for values, label in (
        (policy.allowed_model_digests, "allowed model digest"),
        (policy.allowed_image_digests, "allowed image digest"),
        (policy.allowed_runner_digests, "allowed runner digest"),
        (
            policy.allowed_attestation_policy_digests,
            "allowed attestation policy digest",
        ),
        (policy.allowed_verifier_digests, "allowed verifier digest"),
    ):
        for value in values:
            _digest(value, label)
    if policy.require_attestation and any(
        not values
        for values in (
            policy.allowed_model_digests,
            policy.allowed_image_digests,
            policy.allowed_runner_digests,
            policy.allowed_attestation_policy_digests,
            policy.allowed_verifier_digests,
        )
    ):
        raise ThinSubnetError(
            "attested receipt policy must separately pin model, image, runner, attestation policy, and verifier digests"
        )
    if policy.require_attestation and policy.expected_validator_hotkey is None:
        raise ThinSubnetError(
            "attested receipt policy must pin the request-authorizing validator hotkey"
        )


def verify_receipt(
    raw: bytes,
    policy: ReceiptPolicy,
    *,
    now: datetime | None = None,
    input_reveal: bytes | None = None,
    parameters_reveal: bytes | None = None,
    output_reveal: bytes | None = None,
    attestation_evidence: bytes | None = None,
    attestation_verifier: AttestationVerifier | None = None,
) -> VerifiedInferenceReceipt:
    _validate_policy(policy)
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=MAX_RECEIPT_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("ML inference receipt JSON must be canonical")
    _exact_keys(document, _RECEIPT_KEYS, "ML inference receipt")
    if document["schema"] != RECEIPT_SCHEMA:
        raise ThinSubnetError("unsupported ML inference receipt schema")
    network = _bounded_string(document["network"], "receipt network")
    netuid = _nonnegative_int(document["netuid"], "receipt netuid")
    source_epoch = _nonnegative_int(document["source_epoch"], "receipt source_epoch")
    if network != policy.network or netuid != policy.netuid:
        raise ThinSubnetError("receipt network or netuid mismatch")
    request_id = _digest(document["request_id"], "request id")
    validator_hotkey = _bounded_string(document["validator_hotkey"], "validator hotkey")
    miner_hotkey = _bounded_string(document["miner_hotkey"], "miner hotkey")
    nonce = _nonce(document["nonce_base64"])
    if (
        policy.expected_validator_hotkey is not None
        and validator_hotkey != policy.expected_validator_hotkey
    ):
        raise ThinSubnetError("receipt validator hotkey mismatch")

    first_block = _nonnegative_int(document["valid_from_block"], "valid_from_block")
    last_block = _nonnegative_int(document["valid_until_block"], "valid_until_block")
    if first_block > policy.current_block or policy.current_block >= last_block:
        raise ThinSubnetError("receipt is outside its authorized block window")
    if last_block <= first_block or last_block - first_block > policy.max_block_span:
        raise ThinSubnetError("receipt block window is invalid or too wide")
    issued_at = parse_time(document["issued_at"], "receipt issued_at")
    completed_at = parse_time(document["completed_at"], "receipt completed_at")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ThinSubnetError("receipt verification time must be UTC")
    age = (current - completed_at).total_seconds()
    if age < -policy.max_future_seconds or age > policy.max_age_seconds:
        raise ThinSubnetError("receipt is stale or future-dated")
    if completed_at < issued_at:
        raise ThinSubnetError("receipt completed_at precedes issued_at")

    model = document["model"]
    runtime = document["runtime"]
    request = document["request"]
    result = document["result"]
    attestation = document["attestation"]
    for value, expected, label in (
        (model, _MODEL_KEYS, "model"),
        (runtime, _RUNTIME_KEYS, "runtime"),
        (request, _REQUEST_KEYS, "request"),
        (result, _RESULT_KEYS, "result"),
        (attestation, _ATTESTATION_KEYS, "attestation"),
    ):
        if not isinstance(value, dict):
            raise ThinSubnetError(f"receipt {label} must be an object")
        _exact_keys(value, expected, f"receipt {label}")

    _bounded_string(model["model_id"], "model id", maximum=MAX_MODEL_ID_BYTES)
    weights_digest = _digest(model["weights_digest"], "model weights digest")
    if model["tokenizer_digest"] is not None:
        _digest(model["tokenizer_digest"], "tokenizer digest")
    image_digest = _digest(runtime["image_digest"], "runtime image digest")
    runner_digest = _digest(runtime["runner_digest"], "runner digest")
    input_commitment = _digest(request["input_commitment"], "input commitment")
    parameters_commitment = _digest(
        request["parameters_commitment"], "parameters commitment"
    )
    _digest(result["output_commitment"], "output commitment")
    input_tokens = _nonnegative_int(result["input_tokens"], "input_tokens")
    output_tokens = _nonnegative_int(result["output_tokens"], "output_tokens")
    _decimal(result["latency_ms"], "latency_ms")
    work_units = _decimal(result["work_units"], "work_units")
    if work_units > MAX_WORK_UNITS_PER_RECEIPT:
        raise ThinSubnetError("receipt work_units exceeds protocol limit")

    recomputed_request_id = request_id_for(
        network=network,
        netuid=netuid,
        source_epoch=source_epoch,
        validator_hotkey=validator_hotkey,
        miner_hotkey=miner_hotkey,
        nonce=nonce,
        input_commitment=input_commitment,
        parameters_commitment=parameters_commitment,
    )
    if not hmac.compare_digest(request_id, recomputed_request_id):
        raise ThinSubnetError("receipt request id does not match its bound request")
    request_authorized = _verify_request_authorization(document)
    if input_reveal is not None and not hmac.compare_digest(
        input_commitment, commit_payload("input", input_reveal)
    ):
        raise ThinSubnetError("input reveal does not match receipt commitment")
    if parameters_reveal is not None and not hmac.compare_digest(
        parameters_commitment, commit_payload("parameters", parameters_reveal)
    ):
        raise ThinSubnetError("parameters reveal does not match receipt commitment")
    if output_reveal is not None and not hmac.compare_digest(
        result["output_commitment"], commit_payload("output", output_reveal)
    ):
        raise ThinSubnetError("output reveal does not match receipt commitment")

    if (
        policy.allowed_model_digests
        and weights_digest not in policy.allowed_model_digests
    ):
        raise ThinSubnetError("model weights digest is not allowed by validator policy")
    if (
        policy.allowed_image_digests
        and image_digest not in policy.allowed_image_digests
    ):
        raise ThinSubnetError("runtime image digest is not allowed by validator policy")
    if (
        policy.allowed_runner_digests
        and runner_digest not in policy.allowed_runner_digests
    ):
        raise ThinSubnetError("runner digest is not allowed by validator policy")

    receipt_id = _digest(document["receipt_id"], "receipt id")
    if receipt_id != receipt_id_for(document):
        raise ThinSubnetError("receipt id does not match its canonical body")
    signature = document["signature"]
    if not isinstance(signature, dict):
        raise ThinSubnetError("receipt signature must be an object")
    _exact_keys(signature, _SIGNATURE_KEYS, "receipt signature")
    if signature["algorithm"] != "sr25519":
        raise ThinSubnetError("unsupported receipt signature algorithm")
    try:
        signature_bytes = base64.b64decode(signature["value_base64"], validate=True)
    except (TypeError, binascii.Error, ValueError) as exc:
        raise ThinSubnetError("receipt signature is not canonical base64") from exc
    if (
        len(signature_bytes) != 64
        or base64.b64encode(signature_bytes).decode("ascii")
        != signature["value_base64"]
    ):
        raise ThinSubnetError("receipt signature must be 64 bytes")
    try:
        from bittensor_wallet import Keypair

        signature_ok = Keypair(ss58_address=miner_hotkey).verify(
            RECEIPT_DOMAIN + canonical_json(_signed_receipt(document)),
            signature_bytes,
        )
    except Exception as exc:
        raise ThinSubnetError("receipt miner hotkey is invalid") from exc
    if not signature_ok:
        raise ThinSubnetError("receipt signature verification failed")

    kind = attestation["kind"]
    attestation_verified = False
    verifier_digest: str | None = None
    measurement_digest: str | None = None
    if kind == "none":
        if any(
            attestation[key] is not None
            for key in (
                "evidence_digest",
                "evidence_uri",
                "policy_digest",
                "report_data_hex",
            )
        ):
            raise ThinSubnetError("unattested receipt must omit attestation fields")
        if attestation_evidence is not None or attestation_verifier is not None:
            raise ThinSubnetError("unattested receipt cannot carry verifier evidence")
    elif kind == "tdx":
        if not request_authorized:
            raise ThinSubnetError(
                "attested receipt requires a validator-signed request authorization"
            )
        if any(
            not values
            for values in (
                policy.allowed_model_digests,
                policy.allowed_image_digests,
                policy.allowed_runner_digests,
                policy.allowed_attestation_policy_digests,
                policy.allowed_verifier_digests,
            )
        ):
            raise ThinSubnetError(
                "attestation verification requires every validator trust root to be pinned"
            )
        evidence_digest = _digest(
            attestation["evidence_digest"], "attestation evidence digest"
        )
        _evidence_uri(attestation["evidence_uri"])
        attestation_policy_digest = _digest(
            attestation["policy_digest"], "attestation policy digest"
        )
        report_data_hex = attestation["report_data_hex"]
        if (
            not isinstance(report_data_hex, str)
            or _REPORT_DATA_RE.fullmatch(report_data_hex) is None
        ):
            raise ThinSubnetError(
                "attestation report data must be 64-byte lowercase hex"
            )
        expected = expected_report_data(document)
        if not hmac.compare_digest(report_data_hex, expected.hex()):
            raise ThinSubnetError("attestation report data binding mismatch")
        if (
            policy.allowed_attestation_policy_digests
            and attestation_policy_digest
            not in policy.allowed_attestation_policy_digests
        ):
            raise ThinSubnetError(
                "attestation policy digest is not allowed by validator policy"
            )
        if attestation_evidence is not None:
            if not attestation_evidence:
                raise ThinSubnetError("attestation evidence must not be empty")
            if len(attestation_evidence) > MAX_ATTESTATION_BYTES:
                raise ThinSubnetError("attestation evidence exceeds size limit")
            if not hmac.compare_digest(
                sha256_digest(attestation_evidence), evidence_digest
            ):
                raise ThinSubnetError("attestation evidence digest mismatch")
        if attestation_verifier is not None:
            if attestation_evidence is None:
                raise ThinSubnetError("attestation verifier requires evidence bytes")
            try:
                verification = attestation_verifier(
                    attestation_evidence, expected, attestation_policy_digest
                )
            except ThinSubnetError:
                raise
            except Exception as exc:
                raise ThinSubnetError(
                    "attestation verifier raised an unexpected error"
                ) from exc
            verifier_digest = _digest(
                verification.verifier_digest, "attestation verifier digest"
            )
            if not verification.ok:
                raise ThinSubnetError(
                    f"attestation verification failed: {verification.reason[:160]}"
                )
            if (
                policy.allowed_verifier_digests
                and verifier_digest not in policy.allowed_verifier_digests
            ):
                raise ThinSubnetError(
                    "attestation verifier digest is not allowed by validator policy"
                )
            measurement_digest = verification.measurement_digest
            if measurement_digest is not None:
                _digest(measurement_digest, "attestation measurement digest")
            attestation_verified = True
    else:
        raise ThinSubnetError("attestation kind must be none or tdx")
    if policy.require_attestation and not attestation_verified:
        raise ThinSubnetError("validator policy requires verified attestation")

    return VerifiedInferenceReceipt(
        receipt_id=receipt_id,
        request_id=request_id,
        source_epoch=source_epoch,
        miner_hotkey=miner_hotkey,
        validator_hotkey=validator_hotkey,
        weights_digest=weights_digest,
        image_digest=image_digest,
        runner_digest=runner_digest,
        completed_at=completed_at,
        work_units=work_units,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        attestation_verified=attestation_verified,
        verifier_digest=verifier_digest,
        measurement_digest=measurement_digest,
        document=document,
    )


def _resolve_executable(command: list[str]) -> str:
    if not command:
        raise ThinSubnetError("attestation verifier command is empty")
    executable = command[0]
    resolved = executable if os.path.isabs(executable) else shutil.which(executable)
    if not resolved:
        raise ThinSubnetError(f"attestation verifier not found: {executable}")
    return os.path.realpath(resolved)


def subprocess_attestation_verifier(
    command: str | Iterable[str],
    *,
    expected_verifier_digest: str,
    timeout_seconds: float = 30.0,
) -> AttestationVerifier:
    """Build a fail-closed adapter for a validator-local quote verifier.

    The command is invoked without a shell and must use all four placeholders:
    ``{evidence_path}``, ``{report_data_hex}``, ``{policy_digest}``, and
    ``{result_path}``.  It must write JSON with ``ok``, ``report_data_match``,
    ``policy_digest``, ``measurement_digest`` (nullable), and ``reason``.
    """

    tokens = shlex.split(command) if isinstance(command, str) else list(command)
    required = {
        "{evidence_path}",
        "{report_data_hex}",
        "{policy_digest}",
        "{result_path}",
    }
    present = {
        placeholder
        for token in tokens
        for placeholder in required
        if placeholder in token
    }
    if present != required:
        raise ThinSubnetError("attestation verifier command is missing placeholders")
    executable = _resolve_executable(tokens)
    tokens[0] = executable
    actual_digest = digest_file(executable)
    if actual_digest != _digest(expected_verifier_digest, "expected verifier digest"):
        raise ThinSubnetError("attestation verifier executable digest mismatch")
    if timeout_seconds <= 0:
        raise ThinSubnetError("attestation verifier timeout must be positive")

    def verify(
        evidence: bytes, expected_report_data_bytes: bytes, policy_digest: str
    ) -> AttestationVerification:
        if len(evidence) > MAX_ATTESTATION_BYTES:
            raise ThinSubnetError("attestation evidence exceeds size limit")
        if digest_file(executable) != actual_digest:
            raise ThinSubnetError(
                "attestation verifier executable changed after pinning"
            )
        with tempfile.TemporaryDirectory(prefix="cathedral-verifyml-") as temp_dir:
            evidence_path = os.path.join(temp_dir, "evidence.bin")
            result_path = os.path.join(temp_dir, "result.json")
            with open(evidence_path, "xb") as handle:
                handle.write(evidence)
            values = {
                "{evidence_path}": evidence_path,
                "{report_data_hex}": expected_report_data_bytes.hex(),
                "{policy_digest}": policy_digest,
                "{result_path}": result_path,
            }
            argv: list[str] = []
            for token in tokens:
                for placeholder, replacement in values.items():
                    token = token.replace(placeholder, replacement)
                argv.append(token)
            try:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_seconds,
                    check=False,
                    text=True,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ThinSubnetError("attestation verifier execution failed") from exc
            if completed.returncode != 0:
                raise ThinSubnetError(
                    f"attestation verifier exited {completed.returncode}"
                )
            try:
                result_raw = Path(result_path).read_bytes()
            except OSError as exc:
                raise ThinSubnetError(
                    "attestation verifier did not write a result"
                ) from exc
            result = parse_strict_json(result_raw, maximum_bytes=16_384)
            expected_keys = frozenset(
                {
                    "ok",
                    "report_data_match",
                    "policy_digest",
                    "measurement_digest",
                    "reason",
                }
            )
            _exact_keys(result, expected_keys, "attestation verifier result")
            if result["policy_digest"] != policy_digest:
                raise ThinSubnetError("attestation verifier policy digest mismatch")
            measurement = result["measurement_digest"]
            if measurement is not None:
                measurement = _digest(measurement, "measurement digest")
            reason = _bounded_string(result["reason"], "verifier reason", maximum=512)
            ok = result["ok"] is True and result["report_data_match"] is True
            return AttestationVerification(ok, actual_digest, measurement, reason)

    return verify


@dataclass(frozen=True)
class BundleCheckpoint:
    network: str
    netuid: int
    source_epoch: int
    bundle_id: str
    generated_at: datetime


@dataclass(frozen=True)
class RejectedInferenceReceipt:
    receipt_id: str
    reason: str


@dataclass(frozen=True)
class VerifiedInferenceBundle:
    source_epoch: int
    bundle_id: str
    previous_bundle_id: str | None
    receipts: tuple[VerifiedInferenceReceipt, ...]
    rejections: tuple[RejectedInferenceReceipt, ...]
    document: dict[str, Any]


def bundle_checkpoint_bytes(checkpoint: BundleCheckpoint) -> bytes:
    if (
        checkpoint.generated_at.tzinfo is None
        or checkpoint.generated_at.utcoffset() != UTC.utcoffset(checkpoint.generated_at)
    ):
        raise ThinSubnetError("bundle checkpoint time must be UTC")
    return canonical_json(
        {
            "schema": BUNDLE_CHECKPOINT_SCHEMA,
            "network": checkpoint.network,
            "netuid": checkpoint.netuid,
            "source_epoch": checkpoint.source_epoch,
            "bundle_id": checkpoint.bundle_id,
            "generated_at": format_time(checkpoint.generated_at),
        }
    )


def parse_bundle_checkpoint(raw: bytes) -> BundleCheckpoint:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=16_384)
    if encoded != canonical_json(document):
        raise ThinSubnetError("ML bundle checkpoint JSON must be canonical")
    _exact_keys(document, _BUNDLE_CHECKPOINT_KEYS, "ML bundle checkpoint")
    if document["schema"] != BUNDLE_CHECKPOINT_SCHEMA:
        raise ThinSubnetError("unsupported ML bundle checkpoint schema")
    return BundleCheckpoint(
        network=_bounded_string(document["network"], "bundle checkpoint network"),
        netuid=_nonnegative_int(document["netuid"], "bundle checkpoint netuid"),
        source_epoch=_nonnegative_int(
            document["source_epoch"], "bundle checkpoint source_epoch"
        ),
        bundle_id=_digest(document["bundle_id"], "bundle checkpoint id"),
        generated_at=parse_time(
            document["generated_at"], "bundle checkpoint generated_at"
        ),
    )


def bundle_id_for(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "bundle_id"}
    return sha256_digest(BUNDLE_ID_DOMAIN + canonical_json(body))


def build_bundle(
    receipts: Iterable[bytes | Mapping[str, Any]],
    *,
    network: str,
    netuid: int,
    source_epoch: int,
    generated_at: str,
    valid_until: str,
    valid_from_block: int,
    valid_until_block: int,
    previous_bundle_id: str | None = None,
) -> bytes:
    documents: list[dict[str, Any]] = []
    for receipt in receipts:
        if isinstance(receipt, bytes):
            encoded = receipt[:-1] if receipt.endswith(b"\n") else receipt
            parsed = parse_strict_json(encoded, maximum_bytes=MAX_RECEIPT_BYTES)
            if encoded != canonical_json(parsed):
                raise ThinSubnetError("bundle receipt must be canonical JSON")
            documents.append(parsed)
        else:
            documents.append(dict(receipt))
    documents.sort(key=lambda item: str(item.get("receipt_id", "")))
    body: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "generated_at": generated_at,
        "valid_until": valid_until,
        "valid_from_block": valid_from_block,
        "valid_until_block": valid_until_block,
        "previous_bundle_id": previous_bundle_id,
        "receipts": documents,
    }
    body["bundle_id"] = bundle_id_for(body)
    encoded = canonical_json(body)
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ThinSubnetError("ML inference bundle exceeds size limit")
    return encoded


def verify_bundle(
    raw: bytes,
    policy: ReceiptPolicy,
    *,
    now: datetime | None = None,
    checkpoint: BundleCheckpoint | None = None,
    evidence_by_digest: Mapping[str, bytes] | None = None,
    attestation_verifier: AttestationVerifier | None = None,
) -> tuple[VerifiedInferenceBundle, BundleCheckpoint]:
    encoded = raw[:-1] if raw.endswith(b"\n") else raw
    document = parse_strict_json(encoded, maximum_bytes=MAX_BUNDLE_BYTES)
    if encoded != canonical_json(document):
        raise ThinSubnetError("ML inference bundle JSON must be canonical")
    _exact_keys(document, _BUNDLE_KEYS, "ML inference bundle")
    if document["schema"] != BUNDLE_SCHEMA:
        raise ThinSubnetError("unsupported ML inference bundle schema")
    if document["network"] != policy.network or document["netuid"] != policy.netuid:
        raise ThinSubnetError("ML inference bundle network or netuid mismatch")
    epoch = _nonnegative_int(document["source_epoch"], "bundle source_epoch")
    first_block = _nonnegative_int(
        document["valid_from_block"], "bundle valid_from_block"
    )
    last_block = _nonnegative_int(
        document["valid_until_block"], "bundle valid_until_block"
    )
    if first_block > policy.current_block or policy.current_block >= last_block:
        raise ThinSubnetError("ML inference bundle is outside its block window")
    if last_block <= first_block or last_block - first_block > policy.max_block_span:
        raise ThinSubnetError("ML inference bundle block window is invalid or too wide")
    generated = parse_time(document["generated_at"], "bundle generated_at")
    valid_until = parse_time(document["valid_until"], "bundle valid_until")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() != UTC.utcoffset(current):
        raise ThinSubnetError("bundle verification time must be UTC")
    age = (current - generated).total_seconds()
    if age < -policy.max_future_seconds or age > policy.max_age_seconds:
        raise ThinSubnetError("ML inference bundle is stale or future-dated")
    if not generated < valid_until or current >= valid_until:
        raise ThinSubnetError("ML inference bundle has expired")
    previous = document["previous_bundle_id"]
    if previous is not None:
        previous = _digest(previous, "previous bundle id")
    bundle_id = _digest(document["bundle_id"], "bundle id")
    if bundle_id != bundle_id_for(document):
        raise ThinSubnetError("bundle id does not match its canonical body")
    accepted = BundleCheckpoint(
        policy.network, policy.netuid, epoch, bundle_id, generated
    )
    newer_epoch = True
    if checkpoint is not None:
        if checkpoint.network != policy.network or checkpoint.netuid != policy.netuid:
            raise ThinSubnetError("ML inference bundle checkpoint network mismatch")
        if epoch < checkpoint.source_epoch:
            raise ThinSubnetError("ML inference bundle epoch rolled back")
        if epoch == checkpoint.source_epoch:
            if bundle_id != checkpoint.bundle_id:
                raise ThinSubnetError(
                    "ML inference bundle equivocated at accepted epoch"
                )
            accepted = checkpoint
            newer_epoch = False
        elif epoch > checkpoint.source_epoch + 1:
            raise ThinSubnetError("ML inference bundle skipped an accepted epoch")
        elif previous != checkpoint.bundle_id:
            raise ThinSubnetError("ML inference bundle does not extend accepted chain")
        elif generated <= checkpoint.generated_at:
            raise ThinSubnetError(
                "ML inference bundle time did not advance beyond its checkpoint"
            )
    receipts_raw = document["receipts"]
    if (
        not isinstance(receipts_raw, list)
        or not 1 <= len(receipts_raw) <= MAX_BUNDLE_RECEIPTS
    ):
        raise ThinSubnetError("bundle receipts must contain 1..4096 entries")
    receipt_ids = [
        str(item.get("receipt_id", ""))
        for item in receipts_raw
        if isinstance(item, dict)
    ]
    if len(receipt_ids) != len(receipts_raw) or receipt_ids != sorted(set(receipt_ids)):
        raise ThinSubnetError("bundle receipts must have sorted unique receipt ids")
    verified: list[VerifiedInferenceReceipt] = []
    rejected: list[RejectedInferenceReceipt] = []
    seen_requests: set[str] = set()
    evidence_map = evidence_by_digest or {}
    for index, receipt_document in enumerate(receipts_raw):
        raw_receipt_id = receipt_document.get("receipt_id")
        receipt_label = (
            raw_receipt_id
            if isinstance(raw_receipt_id, str) and len(raw_receipt_id) <= 80
            else f"entry:{index}"
        )
        evidence_digest = None
        attestation = receipt_document.get("attestation")
        if isinstance(attestation, dict):
            evidence_digest = attestation.get("evidence_digest")
        evidence = evidence_map.get(str(evidence_digest)) if evidence_digest else None
        try:
            item = verify_receipt(
                canonical_json(receipt_document),
                policy,
                now=current,
                attestation_evidence=evidence,
                attestation_verifier=attestation_verifier,
            )
            if item.source_epoch != epoch:
                raise ThinSubnetError(
                    "receipt authorization is bound to a different source epoch"
                )
            if item.completed_at > generated:
                raise ThinSubnetError(
                    "receipt completed after the containing bundle was generated"
                )
            if (
                checkpoint is not None
                and newer_epoch
                and item.completed_at <= checkpoint.generated_at
            ):
                raise ThinSubnetError(
                    "receipt was already eligible for a prior accepted epoch"
                )
            if item.request_id in seen_requests:
                raise ThinSubnetError("bundle reuses an inference request id")
        except ThinSubnetError as exc:
            rejected.append(
                RejectedInferenceReceipt(
                    receipt_id=receipt_label,
                    reason=str(exc)[:240],
                )
            )
            continue
        seen_requests.add(item.request_id)
        verified.append(item)
    if not verified:
        summary = rejected[0].reason if rejected else "no receipts supplied"
        raise ThinSubnetError(f"bundle has no admitted receipts: {summary}")
    return (
        VerifiedInferenceBundle(
            source_epoch=epoch,
            bundle_id=bundle_id,
            previous_bundle_id=previous,
            receipts=tuple(verified),
            rejections=tuple(rejected),
            document=document,
        ),
        accepted,
    )


def aggregate_receipts(
    receipts: Iterable[VerifiedInferenceReceipt],
) -> dict[str, dict[str, Any]]:
    """Aggregate validator-verified facts without assigning final weights."""

    aggregated: dict[str, dict[str, Any]] = {}
    seen_receipts: set[str] = set()
    seen_requests: set[str] = set()
    for receipt in receipts:
        if receipt.receipt_id in seen_receipts or receipt.request_id in seen_requests:
            raise ThinSubnetError("duplicate receipt or request cannot be aggregated")
        seen_receipts.add(receipt.receipt_id)
        seen_requests.add(receipt.request_id)
        if not receipt.attestation_verified:
            continue
        row = aggregated.setdefault(
            receipt.miner_hotkey,
            {
                "verified_work_units": Decimal(0),
                "verified_requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "receipt_ids": [],
                "model_digests": set(),
                "image_digests": set(),
                "runner_digests": set(),
            },
        )
        with localcontext() as context:
            context.prec = 64
            row["verified_work_units"] += receipt.work_units
        row["verified_requests"] += 1
        row["input_tokens"] += receipt.input_tokens
        row["output_tokens"] += receipt.output_tokens
        row["receipt_ids"].append(receipt.receipt_id)
        row["model_digests"].add(receipt.weights_digest)
        row["image_digests"].add(receipt.image_digest)
        row["runner_digests"].add(receipt.runner_digest)
    for row in aggregated.values():
        row["verified_work_units"] = canonical_decimal(row["verified_work_units"])
        row["receipt_ids"] = sorted(row["receipt_ids"])
        row["model_digests"] = sorted(row["model_digests"])
        row["image_digests"] = sorted(row["image_digests"])
        row["runner_digests"] = sorted(row["runner_digests"])
    return dict(sorted(aggregated.items()))


def score_report_body_from_bundle(
    bundle: VerifiedInferenceBundle,
    *,
    class_id: str,
    source_id: str,
    signing_key_id: str,
    policy_digest: str,
    verifier_digest: str,
    previous_report_id: str | None,
    evidence_uri: str | None,
) -> dict[str, Any]:
    """Create the unsigned v1 score-class body validators already consume.

    The body reports measurements, not weights.  Each validator still selects
    the metric, transform, cap, class allocation, coldkey collapse, and final
    on-chain vector locally.
    """

    from .score_classes import REPORT_SCHEMA

    aggregate = aggregate_receipts(bundle.receipts)
    if not aggregate:
        raise ThinSubnetError("bundle has no creditable inference receipts")
    claimed_verifier_digest = _digest(verifier_digest, "score verifier digest")
    observed_verifier_digests = {
        receipt.verifier_digest
        for receipt in bundle.receipts
        if receipt.attestation_verified
    }
    if observed_verifier_digests != {claimed_verifier_digest}:
        raise ThinSubnetError(
            "score verifier digest does not match every admitted receipt"
        )
    entries = []
    for hotkey, row in aggregate.items():
        reasons = [
            "model_identity_bound",
            "output_commitment_bound",
            "receipt_signature_verified",
            "request_binding_verified",
            "validator_request_authorized",
            "attestation_verified",
        ]
        entries.append(
            {
                "miner_hotkey": hotkey,
                "metrics": {
                    "input_tokens": str(row["input_tokens"]),
                    "output_tokens": str(row["output_tokens"]),
                    "verified_requests": str(row["verified_requests"]),
                    "verified_work_units": row["verified_work_units"],
                },
                "asserted_score": None,
                "reason_codes": sorted(reasons),
                "evidence": [
                    {
                        "kind": BUNDLE_SCHEMA,
                        "id": bundle.bundle_id,
                        "digest": bundle.bundle_id,
                        "uri": evidence_uri,
                    }
                ],
            }
        )
    document = bundle.document
    return {
        "schema": REPORT_SCHEMA,
        "network": document["network"],
        "netuid": document["netuid"],
        "class_id": class_id,
        "source_id": source_id,
        "source_epoch": bundle.source_epoch,
        "generated_at": document["generated_at"],
        "valid_until": document["valid_until"],
        "valid_from_block": document["valid_from_block"],
        "valid_until_block": document["valid_until_block"],
        "complete": True,
        "policy_digest": _digest(policy_digest, "score policy digest"),
        "verifier_digest": _digest(verifier_digest, "score verifier digest"),
        "previous_report_id": previous_report_id,
        "entries": entries,
        "signing_key_id": signing_key_id,
    }


def new_nonce() -> bytes:
    return os.urandom(32)


def utc_now() -> str:
    return format_time(datetime.now(UTC))
