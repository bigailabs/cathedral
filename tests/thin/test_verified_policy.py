from __future__ import annotations

import base64
from datetime import UTC, datetime
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from bittensor_wallet import Keypair

from cathedral_thin.core import ThinSubnetError
from cathedral_thin.ml_receipts import (
    ReceiptPolicy,
    commit_payload,
    make_unsigned_receipt,
    sha256_digest,
    sign_receipt,
    verify_receipt,
)
from cathedral_thin.policy_cli import run_demo
from cathedral_thin.score_classes import (
    canonical_json,
    compose_class_decisions,
    external_class_decision,
    load_score_policy,
    sign_report,
    verify_report,
)
from cathedral_thin.verified_policy import (
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


NOW = datetime(2026, 7, 19, 15, 0, tzinfo=UTC)
ISSUED = "2026-07-19T14:59:00.000000Z"
CREATED = "2026-07-19T14:59:10.000000Z"
EVALUATED = "2026-07-19T14:59:20.000000Z"
BLOCK = 1_000


def keypair(seed: int) -> Keypair:
    return Keypair.create_from_seed(bytes([seed]).hex() * 32)


def examples(prefix: str = "p") -> list[dict]:
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


def hidden_examples() -> list[dict]:
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


def signed_task_and_hidden(
    miner: Keypair,
    validator: Keypair,
    *,
    nonce_byte: int = 7,
) -> tuple[bytes, bytes]:
    task_nonce = bytes([nonce_byte]) * 32
    hidden = hidden_suite_bytes(
        hidden_examples(),
        salt=bytes([nonce_byte + 1]) * 32,
        task_nonce=task_nonce,
    )
    body = make_task(
        network="finney",
        netuid=39,
        source_epoch=9,
        task_class="agent_tool_policy",
        validator_hotkey=validator.ss58_address,
        miner_hotkey=miner.ss58_address,
        nonce=task_nonce,
        issued_at=ISSUED,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        feature_names=["writes", "sensitive", "approved"],
        labels=["review", "deny", "allow"],
        rare_labels=["deny"],
        public_examples=examples(),
        hidden_suite=hidden,
    )
    return sign_task(body, validator), hidden


def verified_task(miner: Keypair, validator: Keypair, *, nonce_byte: int = 7):
    raw, hidden = signed_task_and_hidden(miner, validator, nonce_byte=nonce_byte)
    task = verify_task(
        raw,
        network="finney",
        netuid=39,
        current_block=BLOCK,
        now=NOW,
        expected_validator_hotkey=validator.ss58_address,
        expected_miner_hotkey=miner.ss58_address,
    )
    return task, raw, hidden


def good_artifact(miner: Keypair, validator: Keypair, *, nonce_byte: int = 7):
    task, task_raw, hidden = verified_task(miner, validator, nonce_byte=nonce_byte)
    body = induce_policy(task, created_at=CREATED)
    raw = sign_artifact(body, miner, task)
    artifact = verify_artifact(raw, task)
    return task, task_raw, hidden, artifact, raw


def test_reference_miner_and_validator_replay_produce_separate_metrics() -> None:
    miner = keypair(1)
    validator = keypair(2)
    task, _, hidden, artifact, _ = good_artifact(miner, validator)
    body = evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED)
    raw = sign_evaluation(body, validator, task, artifact)
    evaluation = verify_evaluation(raw, task, artifact, hidden)

    assert DecimalForTest(
        evaluation.metrics["policy_balanced_accuracy"]
    ) > DecimalForTest("0.9")
    assert evaluation.metrics["policy_rare_recall"] == "1"
    assert evaluation.metrics["policy_evidence_faithfulness"] == "1"
    assert DecimalForTest(evaluation.metrics["policy_compactness"]) > 0
    assert evaluation.metrics["policy_attested_execution"] == "0"
    assert evaluation.execution["output_commitment_bound"] is False


def DecimalForTest(value: str):
    from decimal import Decimal

    return Decimal(value)


def test_task_is_individualized_signed_and_replay_bounded() -> None:
    validator = keypair(3)
    miner_a = keypair(4)
    miner_b = keypair(5)
    raw, _ = signed_task_and_hidden(miner_a, validator)

    with pytest.raises(ThinSubnetError, match="miner mismatch"):
        verify_task(
            raw,
            network="finney",
            netuid=39,
            current_block=BLOCK,
            now=NOW,
            expected_miner_hotkey=miner_b.ss58_address,
        )
    with pytest.raises(ThinSubnetError, match="block window"):
        verify_task(
            raw,
            network="finney",
            netuid=39,
            current_block=BLOCK + 5,
            now=NOW,
        )

    tampered = json.loads(raw)
    tampered["rare_labels"] = ["review"]
    with pytest.raises(ThinSubnetError, match="id|signature"):
        verify_task(
            canonical_json(tampered),
            network="finney",
            netuid=39,
            current_block=BLOCK,
            now=NOW,
        )


def test_artifact_copy_requires_resigning_but_content_copy_remains_possible() -> None:
    validator = keypair(6)
    miner_a = keypair(7)
    miner_b = keypair(8)
    _, _, _, _, artifact_raw = good_artifact(miner_a, validator)
    task_b, _, _ = verified_task(miner_b, validator, nonce_byte=12)

    with pytest.raises(ThinSubnetError, match="task mismatch"):
        verify_artifact(artifact_raw, task_b)

    copied = json.loads(artifact_raw)
    copied["task_id"] = task_b.task_id
    copied["miner_hotkey"] = miner_b.ss58_address
    copied.pop("artifact_id")
    copied.pop("signature")
    resigned = sign_artifact(copied, miner_b, task_b)
    assert verify_artifact(resigned, task_b).miner_hotkey == miner_b.ss58_address


def test_hidden_suite_tamper_and_evaluation_tamper_fail_closed() -> None:
    miner = keypair(9)
    validator = keypair(10)
    task, _, hidden, artifact, _ = good_artifact(miner, validator)
    modified = json.loads(hidden)
    modified["cases"][0]["label"] = "deny"
    with pytest.raises(ThinSubnetError, match="commitment"):
        evaluate_policy(
            task, artifact, canonical_json(modified), evaluated_at=EVALUATED
        )

    body = evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED)
    raw = sign_evaluation(body, validator, task, artifact)
    tampered = json.loads(raw)
    tampered["metrics"]["policy_rare_recall"] = "0"
    with pytest.raises(ThinSubnetError, match="id|signature|replay"):
        verify_evaluation(canonical_json(tampered), task, artifact, hidden)


def test_hidden_suite_is_cryptographically_bound_to_task_nonce() -> None:
    miner = keypair(31)
    validator = keypair(32)
    _, hidden = signed_task_and_hidden(miner, validator, nonce_byte=40)
    with pytest.raises(ThinSubnetError, match="different task nonce"):
        make_task(
            network="finney",
            netuid=39,
            source_epoch=9,
            task_class="agent_tool_policy",
            validator_hotkey=validator.ss58_address,
            miner_hotkey=miner.ss58_address,
            nonce=bytes([41]) * 32,
            issued_at=ISSUED,
            valid_from_block=BLOCK - 5,
            valid_until_block=BLOCK + 5,
            feature_names=["approved", "sensitive", "writes"],
            labels=["allow", "deny", "review"],
            rare_labels=["deny"],
            public_examples=examples(),
            hidden_suite=hidden,
        )


def test_compactness_is_zero_when_quality_or_rare_floor_is_missed() -> None:
    miner = keypair(11)
    validator = keypair(12)
    task, _, hidden = verified_task(miner, validator)
    body = make_artifact(task, rules=[], default_label="allow", created_at=CREATED)
    artifact = verify_artifact(sign_artifact(body, miner, task), task)
    evaluation = evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED)
    assert evaluation["metrics"]["policy_rare_recall"] == "0"
    assert evaluation["metrics"]["policy_compactness"] == "0"
    assert evaluation["metrics"]["policy_evidence_faithfulness"] == "0"


def test_rare_default_cannot_capture_auxiliary_class_budgets() -> None:
    miner = keypair(33)
    validator = keypair(34)
    task, _, hidden = verified_task(miner, validator, nonce_byte=42)
    rare_rule = {
        "rule_id": "rare-example",
        "when": [
            {"feature": "approved", "equals": False},
            {"feature": "sensitive", "equals": True},
            {"feature": "writes", "equals": False},
        ],
        "then": "deny",
        "support_example_ids": ["p06"],
    }
    body = make_artifact(
        task, rules=[rare_rule], default_label="deny", created_at=CREATED
    )
    artifact = verify_artifact(sign_artifact(body, miner, task), task)
    evaluation = evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED)
    assert DecimalForTest(
        evaluation["metrics"]["policy_balanced_accuracy"]
    ) < DecimalForTest("0.7")
    assert evaluation["metrics"]["policy_rare_recall"] == "0"
    assert evaluation["metrics"]["policy_evidence_faithfulness"] == "0"
    assert evaluation["metrics"]["policy_compactness"] == "0"


def test_execution_receipt_must_bind_exact_artifact_bytes() -> None:
    miner = keypair(13)
    validator = keypair(14)
    task, _, hidden, artifact, artifact_raw = good_artifact(miner, validator)
    receipt_body = make_unsigned_receipt(
        network="finney",
        netuid=39,
        source_epoch=task.source_epoch,
        validator_hotkey=validator.ss58_address,
        miner_hotkey=miner.ss58_address,
        nonce=b"r" * 32,
        issued_at=ISSUED,
        completed_at=CREATED,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        model_id="cathedral/reference-policy-miner-v1",
        weights_digest=sha256_digest(b"reference-miner"),
        tokenizer_digest=None,
        image_digest=sha256_digest(b"runtime-image"),
        runner_digest=sha256_digest(b"runner"),
        input_commitment=commit_payload("input", task.task_id.encode()),
        parameters_commitment=commit_payload("parameters", b"{}"),
        output_commitment=commit_payload("output", artifact_raw),
        input_tokens=0,
        output_tokens=0,
        latency_ms="1",
        work_units="1",
    )
    receipt_raw = sign_receipt(receipt_body, miner)
    receipt = verify_receipt(
        receipt_raw,
        ReceiptPolicy(
            network="finney",
            netuid=39,
            current_block=BLOCK,
            require_attestation=False,
        ),
        now=NOW,
        output_reveal=artifact_raw,
    )
    evaluation = evaluate_policy(
        task, artifact, hidden, evaluated_at=EVALUATED, receipt=receipt
    )
    assert evaluation["execution"]["output_commitment_bound"] is True
    assert evaluation["execution"]["attestation_verified"] is False

    wrong = dict(receipt.document)
    wrong["result"] = dict(wrong["result"])
    wrong["result"]["output_commitment"] = commit_payload("output", b"other")
    object.__setattr__(receipt, "document", wrong)
    with pytest.raises(ThinSubnetError, match="does not bind"):
        evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED, receipt=receipt)


def _policy_document(public_key: bytes) -> dict:
    allocations = {
        "policy_fidelity": "0.4",
        "rare_case_retention": "0.3",
        "evidence_faithfulness": "0.2",
        "policy_compactness": "0.1",
    }
    metrics = {
        "policy_fidelity": "policy_balanced_accuracy",
        "rare_case_retention": "policy_rare_recall",
        "evidence_faithfulness": "policy_evidence_faithfulness",
        "policy_compactness": "policy_compactness",
    }
    return {
        "schema": "cathedral_score_policy_v1",
        "network": "finney",
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
                    "policy-score-key": base64.b64encode(public_key).decode("ascii")
                },
            }
            for class_id in sorted(allocations)
        ],
    }


def _evaluation_for(
    miner: Keypair,
    validator: Keypair,
    *,
    good: bool,
    nonce_byte: int,
):
    task, _, hidden = verified_task(miner, validator, nonce_byte=nonce_byte)
    artifact_body = (
        induce_policy(task, created_at=CREATED)
        if good
        else make_artifact(task, rules=[], default_label="allow", created_at=CREATED)
    )
    artifact = verify_artifact(sign_artifact(artifact_body, miner, task), task)
    evaluation_body = evaluate_policy(task, artifact, hidden, evaluated_at=EVALUATED)
    return verify_evaluation(
        sign_evaluation(evaluation_body, validator, task, artifact),
        task,
        artifact,
        hidden,
    )


def test_score_report_cannot_repackage_cross_network_epoch_or_block_evidence() -> None:
    validator = keypair(35)
    miner = keypair(36)
    evaluation = _evaluation_for(miner, validator, good=True, nonce_byte=44)
    common = {
        "evaluations": [evaluation],
        "network": "finney",
        "netuid": 39,
        "class_id": "policy_fidelity",
        "source_id": "cathedral_verified_agent_work",
        "source_epoch": 9,
        "generated_at": EVALUATED,
        "valid_until": "2026-07-19T15:09:20.000000Z",
        "valid_from_block": BLOCK - 5,
        "valid_until_block": BLOCK + 5,
        "signing_key_id": "policy-score-key",
        "policy_digest": sha256_digest(b"source-policy"),
        "score_verifier_digest": sha256_digest(b"verified-policy-v1"),
    }
    with pytest.raises(ThinSubnetError, match="network or netuid"):
        score_report_body_from_evaluations(**{**common, "network": "test"})
    with pytest.raises(ThinSubnetError, match="source epoch"):
        score_report_body_from_evaluations(**{**common, "source_epoch": 10})
    with pytest.raises(ThinSubnetError, match="task window"):
        score_report_body_from_evaluations(
            **{
                **common,
                "valid_from_block": BLOCK + 5,
                "valid_until_block": BLOCK + 6,
            }
        )


def test_end_to_end_measurements_become_validator_owned_class_weights(tmp_path) -> None:
    validator = keypair(15)
    good_miner = keypair(16)
    weak_miner = keypair(17)
    evaluations = [
        _evaluation_for(good_miner, validator, good=True, nonce_byte=20),
        _evaluation_for(weak_miner, validator, good=False, nonce_byte=30),
    ]
    report_key = Ed25519PrivateKey.generate()
    public_key = report_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(canonical_json(_policy_document(public_key)))
    policy = load_score_policy(policy_path, network="finney", netuid=39)
    decisions = []
    for class_policy in policy.external_classes:
        body = score_report_body_from_evaluations(
            evaluations,
            network="finney",
            netuid=39,
            class_id=class_policy.class_id,
            source_id=class_policy.source_id,
            source_epoch=9,
            generated_at=EVALUATED,
            valid_until="2026-07-19T15:09:20.000000Z",
            valid_from_block=BLOCK - 5,
            valid_until_block=BLOCK + 5,
            signing_key_id="policy-score-key",
            policy_digest=sha256_digest(b"source-policy"),
            score_verifier_digest=sha256_digest(b"verified-policy-v1"),
        )
        verified = verify_report(
            sign_report(body, report_key),
            class_policy,
            network="finney",
            netuid=39,
            current_block=BLOCK,
            now=NOW,
        )
        decisions.append(
            external_class_decision(
                class_policy,
                verified,
                coldkey_of={
                    good_miner.ss58_address: "cold-good",
                    weak_miner.ss58_address: "cold-weak",
                },
            )
        )
    weights = compose_class_decisions(policy, decisions)
    assert weights[good_miner.ss58_address] > 0.85
    assert weights[weak_miner.ss58_address] < 0.15
    assert sum(weights.values()) == pytest.approx(1.0)


def test_operator_demo_builds_uid_aligned_weights_without_chain_write() -> None:
    evidence = run_demo()
    assert evidence["ok"] is True
    assert evidence["owner_hosted_services"] == 0
    assert evidence["chain_write_submitted"] is False
    assert len(evidence["onchain_vector"]) == 2
    assert sum(row["weight"] for row in evidence["onchain_vector"]) == pytest.approx(1)
    assert canonical_json(evidence) == canonical_json(run_demo())
