"""Operator CLI for Cathedral Verified Policy Work."""

from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from bittensor_wallet import Keypair
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .bt_compat import make_wallet
from .core import ThinSubnetError
from .ml_receipts import sha256_digest
from .score_classes import (
    canonical_json,
    compose_class_decisions,
    decision_document,
    external_class_decision,
    format_time,
    load_score_policy,
    sign_report,
    verify_report,
)
from .verified_policy import (
    EVALUATION_SCHEMA,
    evaluate_policy,
    hidden_suite_bytes,
    induce_policy,
    make_artifact,
    make_task,
    score_report_body_from_evaluations,
    sign_artifact,
    sign_evaluation,
    sign_task,
    verify_artifact,
    verify_evaluation,
    verify_task,
)


_SPEC_KEYS = frozenset(
    {
        "task_class",
        "feature_names",
        "labels",
        "rare_labels",
        "public_examples",
        "hidden_cases",
        "compactness_max_terms",
        "quality_floor",
        "rare_floor",
    }
)


def _read(path: str, label: str, maximum: int = 4_194_304) -> bytes:
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError as exc:
        raise ThinSubnetError(f"could not read {label}: {path}") from exc
    if len(raw) > maximum:
        raise ThinSubnetError(f"{label} exceeds its size limit")
    return raw


def _write(path: str, raw: bytes, *, replace: bool) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace:
        raise ThinSubnetError(f"refusing to overwrite existing file: {target}")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _wallet(args: argparse.Namespace) -> Any:
    import bittensor as bt

    return make_wallet(
        bt,
        name=args.wallet_name,
        hotkey=args.wallet_hotkey,
        path=args.wallet_path or None,
    )


def _json_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ThinSubnetError(f"{label} is not JSON") from exc
    if not isinstance(document, dict):
        raise ThinSubnetError(f"{label} must be a JSON object")
    return document


def issue(args: argparse.Namespace) -> dict[str, Any]:
    wallet = _wallet(args)
    spec = _json_object(_read(args.spec, "policy task spec"), "policy task spec")
    if frozenset(spec) != _SPEC_KEYS:
        raise ThinSubnetError("policy task spec fields do not match the v1 schema")
    task_nonce = os.urandom(32)
    hidden = hidden_suite_bytes(
        spec["hidden_cases"], salt=os.urandom(32), task_nonce=task_nonce
    )
    issued_at = format_time(datetime.now(UTC))
    body = make_task(
        network=args.network,
        netuid=args.netuid,
        source_epoch=args.source_epoch,
        task_class=spec["task_class"],
        validator_hotkey=str(wallet.hotkey.ss58_address),
        miner_hotkey=args.miner_hotkey,
        nonce=task_nonce,
        issued_at=issued_at,
        valid_from_block=args.current_block,
        valid_until_block=args.current_block + args.valid_blocks,
        feature_names=spec["feature_names"],
        labels=spec["labels"],
        rare_labels=spec["rare_labels"],
        public_examples=spec["public_examples"],
        hidden_suite=hidden,
        compactness_max_terms=spec["compactness_max_terms"],
        quality_floor=spec["quality_floor"],
        rare_floor=spec["rare_floor"],
    )
    task_raw = sign_task(body, wallet.hotkey)
    task_path = _write(args.task_output, task_raw, replace=args.replace)
    hidden_path = _write(args.hidden_output, hidden, replace=args.replace)
    return {
        "task": str(task_path),
        "hidden_suite": str(hidden_path),
        "validator_hotkey": str(wallet.hotkey.ss58_address),
        "miner_hotkey": args.miner_hotkey,
        "chain_write_submitted": False,
    }


def mine(args: argparse.Namespace) -> dict[str, Any]:
    wallet = _wallet(args)
    miner_hotkey = str(wallet.hotkey.ss58_address)
    task_raw = _read(args.task, "verified policy task")
    task = verify_task(
        task_raw,
        network=args.network,
        netuid=args.netuid,
        current_block=args.current_block,
        expected_miner_hotkey=miner_hotkey,
    )
    body = induce_policy(task, created_at=format_time(datetime.now(UTC)))
    artifact_raw = sign_artifact(body, wallet.hotkey, task)
    artifact = verify_artifact(artifact_raw, task)
    path = _write(args.output, artifact_raw, replace=args.replace)
    return {
        "artifact": str(path),
        "artifact_id": artifact.artifact_id,
        "task_id": task.task_id,
        "miner_hotkey": miner_hotkey,
        "rule_count": len(artifact.rules),
        "chain_write_submitted": False,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    wallet = _wallet(args)
    validator_hotkey = str(wallet.hotkey.ss58_address)
    task = verify_task(
        _read(args.task, "verified policy task"),
        network=args.network,
        netuid=args.netuid,
        current_block=args.current_block,
        expected_validator_hotkey=validator_hotkey,
    )
    artifact = verify_artifact(_read(args.artifact, "policy artifact"), task)
    hidden = _read(args.hidden_suite, "hidden suite")
    body = evaluate_policy(
        task,
        artifact,
        hidden,
        evaluated_at=format_time(datetime.now(UTC)),
    )
    raw = sign_evaluation(body, wallet.hotkey, task, artifact)
    verified = verify_evaluation(raw, task, artifact, hidden)
    path = _write(args.output, raw, replace=args.replace)
    return {
        "evaluation": str(path),
        "evaluation_id": verified.evaluation_id,
        "task_id": task.task_id,
        "artifact_id": artifact.artifact_id,
        "metrics": verified.metrics,
        "chain_write_submitted": False,
    }


def _demo_examples(prefix: str) -> list[dict[str, Any]]:
    rows = [
        ("01", False, False, False, "allow"),
        ("02", False, False, True, "allow"),
        ("03", True, False, True, "allow"),
        ("04", False, True, True, "allow"),
        ("05", True, False, False, "review"),
        ("06", False, True, False, "deny"),
        ("07", True, True, False, "deny"),
    ]
    return [
        {
            "example_id": f"{prefix}{suffix}",
            "features": {
                "approved": approved,
                "sensitive": sensitive,
                "writes": writes,
            },
            "label": label,
        }
        for suffix, writes, sensitive, approved, label in rows
    ]


def _demo_hidden_examples() -> list[dict[str, Any]]:
    rows = [
        ("h01", False, False, False, "allow"),
        ("h02", True, False, False, "review"),
        ("h03", False, True, False, "deny"),
        ("h04", True, True, False, "deny"),
        ("h05", False, False, True, "allow"),
        ("h06", True, False, True, "allow"),
        ("h07", False, True, True, "allow"),
        ("h08", True, True, True, "allow"),
    ]
    return [
        {
            "example_id": example_id,
            "features": {
                "approved": approved,
                "sensitive": sensitive,
                "writes": writes,
            },
            "label": label,
        }
        for example_id, writes, sensitive, approved, label in rows
    ]


def _demo_evaluation(
    *,
    validator: Keypair,
    miner: Keypair,
    block: int,
    now: datetime,
    nonce_byte: int,
    good: bool,
) -> Any:
    task_nonce = bytes([nonce_byte]) * 32
    hidden = hidden_suite_bytes(
        _demo_hidden_examples(),
        salt=bytes([nonce_byte + 1]) * 32,
        task_nonce=task_nonce,
    )
    issued_at = format_time(now - timedelta(seconds=20))
    task_raw = sign_task(
        make_task(
            network="local",
            netuid=39,
            source_epoch=1,
            task_class="agent_tool_policy",
            validator_hotkey=validator.ss58_address,
            miner_hotkey=miner.ss58_address,
            nonce=task_nonce,
            issued_at=issued_at,
            valid_from_block=block - 5,
            valid_until_block=block + 5,
            feature_names=["approved", "sensitive", "writes"],
            labels=["allow", "deny", "review"],
            rare_labels=["deny"],
            public_examples=_demo_examples("p"),
            hidden_suite=hidden,
        ),
        validator,
    )
    task = verify_task(
        task_raw,
        network="local",
        netuid=39,
        current_block=block,
        now=now,
    )
    created_at = format_time(now - timedelta(seconds=10))
    artifact_body = (
        induce_policy(task, created_at=created_at)
        if good
        else make_artifact(task, rules=[], default_label="allow", created_at=created_at)
    )
    artifact = verify_artifact(sign_artifact(artifact_body, miner, task), task)
    evaluation_body = evaluate_policy(
        task, artifact, hidden, evaluated_at=format_time(now)
    )
    return verify_evaluation(
        sign_evaluation(evaluation_body, validator, task, artifact),
        task,
        artifact,
        hidden,
    )


def _demo_policy(public_key: bytes) -> dict[str, Any]:
    allocations = {
        "evidence_faithfulness": "0.2",
        "policy_compactness": "0.1",
        "policy_fidelity": "0.4",
        "rare_case_retention": "0.3",
    }
    metrics = {
        "evidence_faithfulness": "policy_evidence_faithfulness",
        "policy_compactness": "policy_compactness",
        "policy_fidelity": "policy_balanced_accuracy",
        "rare_case_retention": "policy_rare_recall",
    }
    return {
        "schema": "cathedral_score_policy_v1",
        "network": "local",
        "netuid": 39,
        "classes": [
            {
                "allocation": allocations[class_id],
                "assignment": {
                    "cap": "1",
                    "metric": metrics[class_id],
                    "mode": "metric",
                    "required_evidence_kinds": [EVALUATION_SCHEMA],
                    "required_reason_codes": [
                        "hidden_suite_commitment_verified",
                        "policy_replayed",
                    ],
                    "transform": "linear",
                },
                "class_id": class_id,
                "kind": "external",
                "locations": [f"report-{class_id}.json"],
                "max_age_seconds": 600,
                "max_block_span": 100,
                "max_future_seconds": 30,
                "require_evidence": True,
                "source_id": "cathedral_verified_agent_work",
                "trusted_keys": {
                    "demo-score-key": base64.b64encode(public_key).decode("ascii")
                },
            }
            for class_id in sorted(allocations)
        ],
    }


def run_demo() -> dict[str, Any]:
    block = 1_000
    # A fixed clock and score key make this public proof byte-for-byte reproducible.
    # The wallet signing keys below are deterministic seeds for the same reason.
    now = datetime(2026, 7, 19, 16, 0, tzinfo=UTC)
    validator = Keypair.create_from_seed("11" * 32)
    strong_miner = Keypair.create_from_seed("22" * 32)
    weak_miner = Keypair.create_from_seed("33" * 32)
    evaluations = [
        _demo_evaluation(
            validator=validator,
            miner=strong_miner,
            block=block,
            now=now,
            nonce_byte=20,
            good=True,
        ),
        _demo_evaluation(
            validator=validator,
            miner=weak_miner,
            block=block,
            now=now,
            nonce_byte=30,
            good=False,
        ),
    ]
    report_key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("44" * 32))
    public_key = report_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    with tempfile.TemporaryDirectory(prefix="cathedral-policy-demo-") as tmpdir:
        policy_path = Path(tmpdir) / "score-policy.json"
        policy_path.write_bytes(canonical_json(_demo_policy(public_key)))
        policy = load_score_policy(policy_path, network="local", netuid=39)
        decisions = []
        for class_policy in policy.external_classes:
            body = score_report_body_from_evaluations(
                evaluations,
                network="local",
                netuid=39,
                class_id=class_policy.class_id,
                source_id=class_policy.source_id,
                source_epoch=1,
                generated_at=format_time(now),
                valid_until=format_time(now + timedelta(minutes=10)),
                valid_from_block=block - 5,
                valid_until_block=block + 5,
                signing_key_id="demo-score-key",
                policy_digest=sha256_digest(b"demo-source-policy"),
                score_verifier_digest=sha256_digest(b"verified-policy-v1"),
            )
            report = verify_report(
                sign_report(body, report_key),
                class_policy,
                network="local",
                netuid=39,
                current_block=block,
                now=now,
            )
            decisions.append(
                external_class_decision(
                    class_policy,
                    report,
                    coldkey_of={
                        strong_miner.ss58_address: "cold-strong",
                        weak_miner.ss58_address: "cold-weak",
                    },
                )
            )
        weights = compose_class_decisions(policy, decisions)
        uid_of = {strong_miner.ss58_address: 4, weak_miner.ss58_address: 9}
        ordered = sorted(weights, key=lambda hotkey: uid_of[hotkey])
        uids = [uid_of[hotkey] for hotkey in ordered]
        vector = [weights[hotkey] for hotkey in ordered]
        record, digest = decision_document(
            validator_hotkey=validator.ss58_address,
            network="local",
            netuid=39,
            round_id=1,
            block=block,
            policy_digest=policy.digest,
            decisions=decisions,
            peers=[
                {
                    "uid": uid_of[evaluation.miner_hotkey],
                    "hotkey": evaluation.miner_hotkey,
                    "coldkey": (
                        "cold-strong"
                        if evaluation.miner_hotkey == strong_miner.ss58_address
                        else "cold-weak"
                    ),
                }
                for evaluation in evaluations
            ],
            uids=uids,
            weights=vector,
        )
    return {
        "schema": "cathedral_verified_policy_demo_v1",
        "ok": True,
        "network": "local",
        "netuid": 39,
        "commodity": "compact evidence-bearing agent policy",
        "stages_exercised": [
            "validator_signed_task",
            "miner_policy_induction",
            "miner_signed_artifact",
            "hidden_suite_replay",
            "rare_case_scoring",
            "score_report_signing",
            "validator_class_assignment",
            "weight_composition",
            "onchain_vector_construction",
        ],
        "class_allocations": {
            item.class_id: str(item.allocation) for item in policy.external_classes
        },
        "miners": {
            evaluation.miner_hotkey: {
                "evaluation_id": evaluation.evaluation_id,
                "metrics": evaluation.metrics,
            }
            for evaluation in evaluations
        },
        "final_weights": weights,
        "decision_digest": digest,
        "onchain_vector": record["onchain_vector"],
        "owner_hosted_services": 0,
        "chain_write_submitted": False,
        "residual_risks": [
            "content can be copied and resigned even though cross-task replay fails",
            "validators choose hidden suites and class allocations",
            "attestation proves measured execution, not semantic quality",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue, mine, and evaluate Cathedral verified policy work"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common_wallet = argparse.ArgumentParser(add_help=False)
    common_wallet.add_argument("--wallet-name", required=True)
    common_wallet.add_argument("--wallet-hotkey", required=True)
    common_wallet.add_argument(
        "--wallet-path", default=os.environ.get("BT_WALLET_PATH", "")
    )

    common_task = argparse.ArgumentParser(add_help=False)
    common_task.add_argument("--network", default=os.environ.get("BT_NETWORK", "test"))
    common_task.add_argument("--netuid", type=int, required=True)
    common_task.add_argument("--current-block", type=int, required=True)

    issue_parser = subparsers.add_parser("issue", parents=[common_wallet, common_task])
    issue_parser.add_argument("--source-epoch", type=int, required=True)
    issue_parser.add_argument("--miner-hotkey", required=True)
    issue_parser.add_argument("--valid-blocks", type=int, default=100)
    issue_parser.add_argument("--spec", required=True)
    issue_parser.add_argument("--task-output", required=True)
    issue_parser.add_argument("--hidden-output", required=True)
    issue_parser.add_argument("--replace", action="store_true")

    mine_parser = subparsers.add_parser("mine", parents=[common_wallet, common_task])
    mine_parser.add_argument("--task", required=True)
    mine_parser.add_argument("--output", required=True)
    mine_parser.add_argument("--replace", action="store_true")

    evaluate_parser = subparsers.add_parser(
        "evaluate", parents=[common_wallet, common_task]
    )
    evaluate_parser.add_argument("--task", required=True)
    evaluate_parser.add_argument("--artifact", required=True)
    evaluate_parser.add_argument("--hidden-suite", required=True)
    evaluate_parser.add_argument("--output", required=True)
    evaluate_parser.add_argument("--replace", action="store_true")

    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("--output", default="")
    demo_parser.add_argument("--replace", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "issue":
            result = issue(args)
        elif args.command == "mine":
            result = mine(args)
        elif args.command == "evaluate":
            result = evaluate(args)
        else:
            result = run_demo()
            if args.output:
                _write(
                    args.output, canonical_json(result) + b"\n", replace=args.replace
                )
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except ThinSubnetError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
