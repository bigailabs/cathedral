"""Command-line tools for Cathedral VerifyML receipts and score inputs."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .bt_compat import make_wallet
from .core import ThinSubnetError
from .ml_receipts import (
    ReceiptPolicy,
    apply_request_authorization,
    build_bundle,
    bundle_checkpoint_bytes,
    commit_payload,
    digest_file,
    make_unsigned_receipt,
    new_nonce,
    parse_bundle_checkpoint,
    score_report_body_from_bundle,
    sha256_digest,
    sign_request_authorization,
    sign_receipt,
    subprocess_attestation_verifier,
    utc_now,
    verify_bundle,
    verify_receipt,
)
from .score_classes import canonical_json


def _digest_or_file(digest: str, path: str, label: str) -> str:
    if digest and path:
        raise ThinSubnetError(f"choose either --{label}-digest or --{label}-file")
    if digest:
        return digest
    if path:
        return digest_file(path)
    raise ThinSubnetError(f"--{label}-digest or --{label}-file is required")


def _read(path: str, label: str, *, maximum: int = 16_777_216) -> bytes:
    try:
        value = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise ThinSubnetError(f"could not read {label}: {path}") from exc
    if len(value) > maximum:
        raise ThinSubnetError(f"{label} exceeds size limit")
    return value


def _write(path: str, value: bytes, *, replace: bool) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace:
        raise ThinSubnetError(f"refusing to overwrite existing file: {target}")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _nonce(args: argparse.Namespace) -> bytes:
    if args.nonce_base64:
        try:
            value = base64.b64decode(args.nonce_base64, validate=True)
        except Exception as exc:
            raise ThinSubnetError("--nonce-base64 is invalid") from exc
        if len(value) != 32:
            raise ThinSubnetError("--nonce-base64 must contain exactly 32 bytes")
        return value
    return new_nonce()


def _wallet(args: argparse.Namespace) -> Any:
    import bittensor as bt

    wallet = make_wallet(
        bt,
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=args.wallet_path or None,
    )
    return wallet


def _receipt_bytes(
    args: argparse.Namespace,
    *,
    output_payload: bytes,
    latency_ms: str,
) -> bytes:
    wallet = _wallet(args)
    miner_hotkey = str(wallet.hotkey.ss58_address)
    input_payload = _read(args.input_file, "input")
    parameters_payload = _read(args.parameters_file, "parameters")
    evidence = (
        _read(args.attestation_evidence, "attestation evidence", maximum=1_048_576)
        if args.attestation_evidence
        else None
    )
    attestation_kind = "tdx" if evidence is not None else "none"
    if evidence == b"":
        raise ThinSubnetError("attestation evidence must not be empty")
    if (evidence is not None) != bool(args.attestation_policy_digest):
        raise ThinSubnetError(
            "TDX issue requires both --attestation-evidence and --attestation-policy-digest"
        )
    if args.attestation_evidence_uri and evidence is None:
        raise ThinSubnetError(
            "--attestation-evidence-uri requires --attestation-evidence"
        )
    if evidence is not None and not args.request_authorization:
        raise ThinSubnetError(
            "TDX issue requires a validator-signed --request-authorization"
        )
    issued_at = args.issued_at or utc_now()
    completed_at = args.completed_at or utc_now()
    unsigned = make_unsigned_receipt(
        network=args.network,
        netuid=args.netuid,
        source_epoch=args.source_epoch,
        validator_hotkey=args.validator_hotkey,
        miner_hotkey=miner_hotkey,
        nonce=_nonce(args),
        issued_at=issued_at,
        completed_at=completed_at,
        valid_from_block=args.valid_from_block,
        valid_until_block=args.valid_until_block,
        model_id=args.model_id,
        weights_digest=_digest_or_file(
            args.weights_digest, args.weights_file, "weights"
        ),
        tokenizer_digest=(
            _digest_or_file(args.tokenizer_digest, args.tokenizer_file, "tokenizer")
            if args.tokenizer_digest or args.tokenizer_file
            else None
        ),
        image_digest=args.image_digest,
        runner_digest=_digest_or_file(args.runner_digest, args.runner_file, "runner"),
        input_commitment=commit_payload("input", input_payload),
        parameters_commitment=commit_payload("parameters", parameters_payload),
        output_commitment=commit_payload("output", output_payload),
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        latency_ms=latency_ms,
        work_units=args.work_units,
        attestation_kind=attestation_kind,
        attestation_evidence_digest=(
            sha256_digest(evidence) if evidence is not None else None
        ),
        attestation_evidence_uri=args.attestation_evidence_uri or None,
        attestation_policy_digest=args.attestation_policy_digest or None,
    )
    if args.request_authorization:
        unsigned = apply_request_authorization(
            unsigned,
            _read(
                args.request_authorization,
                "request authorization",
                maximum=16_384,
            ),
        )
    return sign_receipt(unsigned, wallet.hotkey)


def _authorize(args: argparse.Namespace) -> int:
    wallet = _wallet(args)
    nonce = _nonce(args)
    issued_at = args.issued_at or utc_now()
    template = make_unsigned_receipt(
        network=args.network,
        netuid=args.netuid,
        source_epoch=args.source_epoch,
        validator_hotkey=str(wallet.hotkey.ss58_address),
        miner_hotkey=args.miner_hotkey,
        nonce=nonce,
        issued_at=issued_at,
        completed_at=issued_at,
        valid_from_block=args.valid_from_block,
        valid_until_block=args.valid_until_block,
        model_id=args.model_id,
        weights_digest=_digest_or_file(
            args.weights_digest, args.weights_file, "weights"
        ),
        tokenizer_digest=(
            _digest_or_file(args.tokenizer_digest, args.tokenizer_file, "tokenizer")
            if args.tokenizer_digest or args.tokenizer_file
            else None
        ),
        image_digest=args.image_digest,
        runner_digest=_digest_or_file(args.runner_digest, args.runner_file, "runner"),
        input_commitment=commit_payload("input", _read(args.input_file, "input")),
        parameters_commitment=commit_payload(
            "parameters", _read(args.parameters_file, "parameters")
        ),
        output_commitment=commit_payload("output", b""),
        input_tokens=0,
        output_tokens=0,
        latency_ms="0",
        work_units="0",
    )
    raw = sign_request_authorization(template, wallet.hotkey)
    target = _write(args.output, raw, replace=args.replace)
    print(
        json.dumps(
            {
                "ok": True,
                "request_authorization": str(target),
                "request_id": template["request_id"],
                "source_epoch": template["source_epoch"],
                "validator_hotkey": template["validator_hotkey"],
                "miner_hotkey": template["miner_hotkey"],
                "nonce_base64": base64.b64encode(nonce).decode("ascii"),
                "issued_at": issued_at,
                "valid_from_block": args.valid_from_block,
                "valid_until_block": args.valid_until_block,
            },
            sort_keys=True,
        )
    )
    return 0


def _issue(args: argparse.Namespace) -> int:
    output_payload = _read(args.output_file, "output")
    raw = _receipt_bytes(
        args, output_payload=output_payload, latency_ms=args.latency_ms
    )
    target = _write(args.receipt, raw, replace=args.replace)
    document = json.loads(raw)
    print(
        json.dumps(
            {
                "ok": True,
                "receipt": str(target),
                "receipt_id": document["receipt_id"],
                "attestation": document["attestation"]["kind"],
            },
            sort_keys=True,
        )
    )
    return 0


def _run_local(args: argparse.Namespace) -> int:
    if not args.command:
        raise ThinSubnetError("run-local requires a command after --")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ThinSubnetError("run-local command is empty")
    executable = command[0]
    resolved = executable if os.path.isabs(executable) else shutil.which(executable)
    if not resolved:
        raise ThinSubnetError(f"model runner not found: {executable}")
    input_path = str(Path(args.input_file).expanduser().resolve())
    model_path = (
        str(Path(args.weights_file).expanduser().resolve()) if args.weights_file else ""
    )
    argv = [
        token.replace("{input_path}", input_path).replace("{model_path}", model_path)
        for token in command
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            input=_read(args.input_file, "input"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ThinSubnetError("local model execution failed") from exc
    elapsed = (time.monotonic() - started) * 1000.0
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[:240]
        raise ThinSubnetError(
            f"local model runner exited {completed.returncode}: {detail}"
        )
    if len(completed.stdout) > args.max_output_bytes:
        raise ThinSubnetError("local model output exceeds configured limit")
    output_target = _write(args.output_file, completed.stdout, replace=args.replace)
    # The runner was observed locally, not inside independently verified TDX.
    # Force an unattested receipt regardless of issue-only flags.
    args.attestation_evidence = ""
    args.attestation_policy_digest = ""
    args.attestation_evidence_uri = ""
    raw = _receipt_bytes(
        args,
        output_payload=completed.stdout,
        latency_ms=f"{elapsed:.3f}",
    )
    receipt_target = _write(args.receipt, raw, replace=args.replace)
    document = json.loads(raw)
    print(
        json.dumps(
            {
                "ok": True,
                "mode": "local_unattested",
                "output": str(output_target),
                "receipt": str(receipt_target),
                "receipt_id": document["receipt_id"],
                "creditable_as_verified_work": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _digest_set(values: list[str]) -> frozenset[str]:
    return frozenset(value for value in values if value)


def _policy(args: argparse.Namespace) -> ReceiptPolicy:
    return ReceiptPolicy(
        network=args.network,
        netuid=args.netuid,
        current_block=args.current_block,
        max_age_seconds=args.max_age_seconds,
        max_future_seconds=args.max_future_seconds,
        max_block_span=args.max_block_span,
        require_attestation=not args.allow_unattested,
        expected_validator_hotkey=args.expected_validator_hotkey or None,
        allowed_model_digests=_digest_set(args.allow_model_digest),
        allowed_image_digests=_digest_set(args.allow_image_digest),
        allowed_runner_digests=_digest_set(args.allow_runner_digest),
        allowed_attestation_policy_digests=_digest_set(
            args.allow_attestation_policy_digest
        ),
        allowed_verifier_digests=_digest_set(args.allow_verifier_digest),
    )


def _verifier(args: argparse.Namespace):
    if not args.attestation_verifier_command:
        return None
    if not args.attestation_verifier_digest:
        raise ThinSubnetError(
            "--attestation-verifier-digest is required with a verifier command"
        )
    return subprocess_attestation_verifier(
        args.attestation_verifier_command,
        expected_verifier_digest=args.attestation_verifier_digest,
        timeout_seconds=args.attestation_verifier_timeout,
    )


def _verify(args: argparse.Namespace) -> int:
    raw = _read(args.receipt, "receipt", maximum=262_144)
    receipt = verify_receipt(
        raw,
        _policy(args),
        input_reveal=(
            _read(args.input_reveal, "input reveal") if args.input_reveal else None
        ),
        parameters_reveal=(
            _read(args.parameters_reveal, "parameters reveal")
            if args.parameters_reveal
            else None
        ),
        output_reveal=(
            _read(args.output_reveal, "output reveal") if args.output_reveal else None
        ),
        attestation_evidence=(
            _read(args.attestation_evidence, "attestation evidence", maximum=1_048_576)
            if args.attestation_evidence
            else None
        ),
        attestation_verifier=_verifier(args),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "receipt_id": receipt.receipt_id,
                "request_id": receipt.request_id,
                "source_epoch": receipt.source_epoch,
                "miner_hotkey": receipt.miner_hotkey,
                "model_digest": receipt.weights_digest,
                "image_digest": receipt.image_digest,
                "runner_digest": receipt.runner_digest,
                "attestation_verified": receipt.attestation_verified,
                "verifier_digest": receipt.verifier_digest,
                "measurement_digest": receipt.measurement_digest,
                "work_units": str(receipt.work_units),
            },
            sort_keys=True,
        )
    )
    return 0


def _bundle(args: argparse.Namespace) -> int:
    raw = build_bundle(
        [_read(path, "receipt", maximum=262_144) for path in args.receipts],
        network=args.network,
        netuid=args.netuid,
        source_epoch=args.source_epoch,
        generated_at=args.generated_at or utc_now(),
        valid_until=args.valid_until,
        valid_from_block=args.valid_from_block,
        valid_until_block=args.valid_until_block,
        previous_bundle_id=args.previous_bundle_id or None,
    )
    target = _write(args.output, raw, replace=args.replace)
    document = json.loads(raw)
    print(
        json.dumps(
            {
                "ok": True,
                "bundle": str(target),
                "bundle_id": document["bundle_id"],
                "receipts": len(document["receipts"]),
            },
            sort_keys=True,
        )
    )
    return 0


def _evidence_map(values: list[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for item in values:
        digest, separator, path = item.partition("=")
        if not separator or not digest or not path or digest in out:
            raise ThinSubnetError("--evidence must be unique DIGEST=PATH assignments")
        raw = _read(path, "attestation evidence", maximum=1_048_576)
        if sha256_digest(raw) != digest:
            raise ThinSubnetError(f"evidence file does not match digest: {digest}")
        out[digest] = raw
    return out


def _verified_bundle(args: argparse.Namespace):
    checkpoint_path = _checkpoint_path(args)
    checkpoint = None
    if checkpoint_path.exists():
        checkpoint = parse_bundle_checkpoint(
            _read(str(checkpoint_path), "bundle checkpoint", maximum=16_384)
        )
    return verify_bundle(
        _read(args.bundle, "bundle", maximum=16_777_216),
        _policy(args),
        checkpoint=checkpoint,
        evidence_by_digest=_evidence_map(args.evidence),
        attestation_verifier=_verifier(args),
    )


def _checkpoint_path(args: argparse.Namespace) -> Path:
    checkpoint_path = Path(args.checkpoint).expanduser().resolve()
    bundle_path = Path(args.bundle).expanduser().resolve()
    output_path = (
        Path(args.output).expanduser().resolve()
        if getattr(args, "output", "")
        else None
    )
    if checkpoint_path == bundle_path:
        raise ThinSubnetError("checkpoint path must differ from bundle path")
    if output_path is not None and checkpoint_path == output_path:
        raise ThinSubnetError("checkpoint path must differ from output path")
    return checkpoint_path


@contextmanager
def _checkpoint_transaction(args: argparse.Namespace):
    checkpoint_path = _checkpoint_path(args)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = checkpoint_path.with_name(f".{checkpoint_path.name}.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _persist_checkpoint(args: argparse.Namespace, checkpoint: Any) -> None:
    checkpoint_path = _checkpoint_path(args)
    _write(
        str(checkpoint_path),
        bundle_checkpoint_bytes(checkpoint),
        replace=True,
    )


def _verify_bundle(args: argparse.Namespace) -> int:
    with _checkpoint_transaction(args):
        bundle, checkpoint = _verified_bundle(args)
        _persist_checkpoint(args, checkpoint)
    print(
        json.dumps(
            {
                "ok": True,
                "bundle_id": bundle.bundle_id,
                "source_epoch": bundle.source_epoch,
                "receipts_admitted": len(bundle.receipts),
                "receipts_rejected": len(bundle.rejections),
                "rejections": [
                    {"receipt_id": item.receipt_id, "reason": item.reason}
                    for item in bundle.rejections
                ],
                "attestation_verified": all(
                    item.attestation_verified for item in bundle.receipts
                ),
                "miners": len({item.miner_hotkey for item in bundle.receipts}),
            },
            sort_keys=True,
        )
    )
    return 0


def _score_body(args: argparse.Namespace) -> int:
    with _checkpoint_transaction(args):
        bundle, checkpoint = _verified_bundle(args)
        body = score_report_body_from_bundle(
            bundle,
            class_id=args.class_id,
            source_id=args.source_id,
            signing_key_id=args.signing_key_id,
            policy_digest=args.score_policy_digest,
            verifier_digest=args.score_verifier_digest,
            previous_report_id=args.previous_report_id or None,
            evidence_uri=args.bundle_uri or None,
        )
        target = _write(args.output, canonical_json(body), replace=args.replace)
        _persist_checkpoint(args, checkpoint)
    print(
        json.dumps(
            {
                "ok": True,
                "score_body": str(target),
                "entries": len(body["entries"]),
                "receipts_admitted": len(bundle.receipts),
                "receipts_rejected": len(bundle.rejections),
                "metric": "verified_work_units",
                "weights_assigned": False,
            },
            sort_keys=True,
        )
    )
    return 0


def _add_issue_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=39)
    parser.add_argument("--source-epoch", type=int, required=True)
    parser.add_argument("--wallet-name", default="miner")
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--wallet-path", default="")
    parser.add_argument("--validator-hotkey", required=True)
    parser.add_argument("--valid-from-block", type=int, required=True)
    parser.add_argument("--valid-until-block", type=int, required=True)
    parser.add_argument("--issued-at", default="")
    parser.add_argument("--completed-at", default="")
    parser.add_argument("--nonce-base64", default="")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--weights-digest", default="")
    parser.add_argument("--weights-file", default="")
    parser.add_argument("--tokenizer-digest", default="")
    parser.add_argument("--tokenizer-file", default="")
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--runner-digest", default="")
    parser.add_argument("--runner-file", default="")
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--parameters-file", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--work-units", default="0")
    parser.add_argument("--request-authorization", default="")
    parser.add_argument("--attestation-evidence", default="")
    parser.add_argument("--attestation-evidence-uri", default="")
    parser.add_argument("--attestation-policy-digest", default="")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--replace", action="store_true")


def _add_verification_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--network", default="finney")
    parser.add_argument("--netuid", type=int, default=39)
    parser.add_argument("--current-block", type=int, required=True)
    parser.add_argument("--max-age-seconds", type=int, default=3600)
    parser.add_argument("--max-future-seconds", type=int, default=30)
    parser.add_argument("--max-block-span", type=int, default=7200)
    parser.add_argument("--expected-validator-hotkey", default="")
    parser.add_argument("--allow-unattested", action="store_true")
    parser.add_argument("--allow-model-digest", action="append", default=[])
    parser.add_argument("--allow-image-digest", action="append", default=[])
    parser.add_argument("--allow-runner-digest", action="append", default=[])
    parser.add_argument(
        "--allow-attestation-policy-digest", action="append", default=[]
    )
    parser.add_argument("--allow-verifier-digest", action="append", default=[])
    parser.add_argument("--attestation-verifier-command", default="")
    parser.add_argument("--attestation-verifier-digest", default="")
    parser.add_argument("--attestation-verifier-timeout", type=float, default=30)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and independently verify Cathedral ML inference receipts"
    )
    commands = parser.add_subparsers(dest="command_name", required=True)

    authorize = commands.add_parser(
        "authorize", help="sign a miner-targeted inference request as a validator"
    )
    authorize.add_argument("--network", default="finney")
    authorize.add_argument("--netuid", type=int, default=39)
    authorize.add_argument("--source-epoch", type=int, required=True)
    authorize.add_argument("--wallet-name", default="validator")
    authorize.add_argument("--wallet-hotkey", default="default")
    authorize.add_argument("--wallet-path", default="")
    authorize.add_argument("--miner-hotkey", required=True)
    authorize.add_argument("--valid-from-block", type=int, required=True)
    authorize.add_argument("--valid-until-block", type=int, required=True)
    authorize.add_argument("--issued-at", default="")
    authorize.add_argument("--nonce-base64", default="")
    authorize.add_argument("--model-id", required=True)
    authorize.add_argument("--weights-digest", default="")
    authorize.add_argument("--weights-file", default="")
    authorize.add_argument("--tokenizer-digest", default="")
    authorize.add_argument("--tokenizer-file", default="")
    authorize.add_argument("--image-digest", required=True)
    authorize.add_argument("--runner-digest", default="")
    authorize.add_argument("--runner-file", default="")
    authorize.add_argument("--input-file", required=True)
    authorize.add_argument("--parameters-file", required=True)
    authorize.add_argument("--output", required=True)
    authorize.add_argument("--replace", action="store_true")
    authorize.set_defaults(handler=_authorize)

    issue = commands.add_parser("issue", help="sign a receipt for an existing output")
    _add_issue_arguments(issue)
    issue.add_argument("--latency-ms", required=True)
    issue.set_defaults(handler=_issue)

    run = commands.add_parser(
        "run-local",
        help="run a local model command and issue an unattested test receipt",
    )
    _add_issue_arguments(run)
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--max-output-bytes", type=int, default=1_048_576)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=_run_local)

    verify = commands.add_parser("verify", help="verify one receipt")
    _add_verification_arguments(verify)
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--input-reveal", default="")
    verify.add_argument("--parameters-reveal", default="")
    verify.add_argument("--output-reveal", default="")
    verify.add_argument("--attestation-evidence", default="")
    verify.set_defaults(handler=_verify)

    bundle = commands.add_parser(
        "bundle", help="build a content-addressed receipt bundle"
    )
    bundle.add_argument("--network", default="finney")
    bundle.add_argument("--netuid", type=int, default=39)
    bundle.add_argument("--source-epoch", type=int, required=True)
    bundle.add_argument("--generated-at", default="")
    bundle.add_argument("--valid-until", required=True)
    bundle.add_argument("--valid-from-block", type=int, required=True)
    bundle.add_argument("--valid-until-block", type=int, required=True)
    bundle.add_argument("--previous-bundle-id", default="")
    bundle.add_argument("--receipt", dest="receipts", action="append", required=True)
    bundle.add_argument("--output", required=True)
    bundle.add_argument("--replace", action="store_true")
    bundle.set_defaults(handler=_bundle)

    verify_bundle_parser = commands.add_parser(
        "verify-bundle", help="verify every receipt in a bundle"
    )
    _add_verification_arguments(verify_bundle_parser)
    verify_bundle_parser.add_argument("--bundle", required=True)
    verify_bundle_parser.add_argument("--checkpoint", required=True)
    verify_bundle_parser.add_argument("--evidence", action="append", default=[])
    verify_bundle_parser.set_defaults(handler=_verify_bundle)

    score = commands.add_parser(
        "score-body", help="derive an unsigned score-class report body from a bundle"
    )
    _add_verification_arguments(score)
    score.add_argument("--bundle", required=True)
    score.add_argument("--checkpoint", required=True)
    score.add_argument("--evidence", action="append", default=[])
    score.add_argument("--class-id", default="verified_inference")
    score.add_argument("--source-id", required=True)
    score.add_argument("--signing-key-id", required=True)
    score.add_argument("--score-policy-digest", required=True)
    score.add_argument("--score-verifier-digest", required=True)
    score.add_argument("--previous-report-id", default="")
    score.add_argument("--bundle-uri", default="")
    score.add_argument("--output", required=True)
    score.add_argument("--replace", action="store_true")
    score.set_defaults(handler=_score_body)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ThinSubnetError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
