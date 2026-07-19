from __future__ import annotations

import base64
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
import sys
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from bittensor_wallet import Keypair

from cathedral_thin.core import ThinSubnetError
from cathedral_thin.ml_receipts import (
    AttestationVerification,
    BUNDLE_SCHEMA,
    BundleCheckpoint,
    ReceiptPolicy,
    aggregate_receipts,
    apply_request_authorization,
    build_bundle,
    bundle_checkpoint_bytes,
    commit_payload,
    expected_report_data,
    make_unsigned_receipt,
    score_report_body_from_bundle,
    sha256_digest,
    sign_request_authorization,
    sign_receipt,
    subprocess_attestation_verifier,
    verify_bundle,
    verify_receipt,
)
from cathedral_thin.score_classes import (
    canonical_json,
    compose_class_decisions,
    external_class_decision,
    load_score_policy,
    sign_report,
    verify_report,
)


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
ISSUED = "2026-07-19T11:59:58.000000Z"
COMPLETED = "2026-07-19T11:59:59.000000Z"
VALID_UNTIL = "2026-07-19T12:10:00.000000Z"
NETWORK = "finney"
NETUID = 39
BLOCK = 1_000
MODEL_DIGEST = sha256_digest(b"model-weights")
RUNTIME_DIGEST = sha256_digest(b"runner-image")
RUNNER_DIGEST = sha256_digest(b"runner-binary")
POLICY_DIGEST = sha256_digest(b"tdx-policy")
VERIFIER_DIGEST = sha256_digest(b"quote-verifier")
MEASUREMENT_DIGEST = sha256_digest(b"tdx-measurement")


def keypair(seed: int) -> Keypair:
    return Keypair.create_from_seed(bytes([seed]).hex() * 32)


def unsigned_receipt(
    miner: Keypair,
    *,
    validator: Keypair | None = None,
    source_epoch: int = 1,
    nonce_byte: int = 7,
    attested: bool = False,
    authorize: bool = True,
    evidence: bytes = b"genuine-quote",
) -> dict:
    validator = validator or keypair(9)
    document = make_unsigned_receipt(
        network=NETWORK,
        netuid=NETUID,
        source_epoch=source_epoch,
        validator_hotkey=validator.ss58_address,
        miner_hotkey=miner.ss58_address,
        nonce=bytes([nonce_byte]) * 32,
        issued_at=ISSUED,
        completed_at=COMPLETED,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        model_id="HuggingFaceTB/SmolLM2-135M-Instruct",
        weights_digest=MODEL_DIGEST,
        tokenizer_digest=sha256_digest(b"tokenizer"),
        image_digest=RUNTIME_DIGEST,
        runner_digest=RUNNER_DIGEST,
        input_commitment=commit_payload("input", b"What is 2+2?"),
        parameters_commitment=commit_payload(
            "parameters", b'{"temperature":"0","max_tokens":8}'
        ),
        output_commitment=commit_payload("output", b"4"),
        input_tokens=6,
        output_tokens=1,
        latency_ms="42.125",
        work_units="1.25",
        attestation_kind="tdx" if attested else "none",
        attestation_evidence_digest=(sha256_digest(evidence) if attested else None),
        attestation_evidence_uri=(
            "https://receipts.example/quote.bin" if attested else None
        ),
        attestation_policy_digest=POLICY_DIGEST if attested else None,
    )
    if attested and authorize:
        document = apply_request_authorization(
            document, sign_request_authorization(document, validator)
        )
    return document


def signed_receipt(
    miner: Keypair,
    *,
    validator: Keypair | None = None,
    source_epoch: int = 1,
    nonce_byte: int = 7,
    attested: bool = False,
    evidence: bytes = b"genuine-quote",
) -> bytes:
    return sign_receipt(
        unsigned_receipt(
            miner,
            validator=validator,
            source_epoch=source_epoch,
            nonce_byte=nonce_byte,
            attested=attested,
            evidence=evidence,
        ),
        miner,
    )


def receipt_policy(*, require_attestation: bool) -> ReceiptPolicy:
    return ReceiptPolicy(
        network=NETWORK,
        netuid=NETUID,
        current_block=BLOCK,
        max_age_seconds=60,
        max_future_seconds=5,
        max_block_span=20,
        require_attestation=require_attestation,
        expected_validator_hotkey=(
            keypair(9).ss58_address if require_attestation else None
        ),
        allowed_model_digests=frozenset({MODEL_DIGEST}),
        allowed_image_digests=frozenset({RUNTIME_DIGEST}),
        allowed_runner_digests=frozenset({RUNNER_DIGEST}),
        allowed_attestation_policy_digests=frozenset({POLICY_DIGEST}),
        allowed_verifier_digests=frozenset({VERIFIER_DIGEST}),
    )


def good_verifier(
    evidence: bytes, report_data: bytes, policy_digest: str
) -> AttestationVerification:
    assert evidence == b"genuine-quote"
    assert len(report_data) == 64
    assert policy_digest == POLICY_DIGEST
    return AttestationVerification(
        True, VERIFIER_DIGEST, MEASUREMENT_DIGEST, "verified"
    )


def test_unattested_receipt_is_signed_provenance_not_verified_execution() -> None:
    miner = keypair(1)
    raw = signed_receipt(miner)
    receipt = verify_receipt(
        raw,
        receipt_policy(require_attestation=False),
        now=NOW,
        input_reveal=b"What is 2+2?",
        parameters_reveal=b'{"temperature":"0","max_tokens":8}',
        output_reveal=b"4",
    )
    assert receipt.miner_hotkey == miner.ss58_address
    assert receipt.work_units == Decimal("1.25")
    assert receipt.attestation_verified is False
    assert (
        verify_receipt(
            raw + b"\n", receipt_policy(require_attestation=False), now=NOW
        ).receipt_id
        == receipt.receipt_id
    )

    with pytest.raises(ThinSubnetError, match="requires verified attestation"):
        verify_receipt(raw, receipt_policy(require_attestation=True), now=NOW)


def test_tdx_receipt_requires_evidence_and_validator_pinned_verifier() -> None:
    miner = keypair(2)
    raw = signed_receipt(miner, attested=True)
    receipt = verify_receipt(
        raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        attestation_evidence=b"genuine-quote",
        attestation_verifier=good_verifier,
    )
    assert receipt.attestation_verified is True
    assert receipt.verifier_digest == VERIFIER_DIGEST
    assert receipt.measurement_digest == MEASUREMENT_DIGEST
    assert bytes.fromhex(receipt.document["attestation"]["report_data_hex"]) == (
        expected_report_data(receipt.document)
    )

    with pytest.raises(ThinSubnetError, match="evidence digest mismatch"):
        verify_receipt(
            raw,
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"different-quote",
            attestation_verifier=good_verifier,
        )
    with pytest.raises(ThinSubnetError, match="must not be empty"):
        verify_receipt(
            raw,
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"",
            attestation_verifier=good_verifier,
        )

    def unexpected_verifier(_evidence, _report_data, _policy):
        raise RuntimeError("adapter bug")

    with pytest.raises(ThinSubnetError, match="unexpected error"):
        verify_receipt(
            raw,
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"genuine-quote",
            attestation_verifier=unexpected_verifier,
        )

    query_document = unsigned_receipt(miner, attested=True)
    query_document["attestation"]["evidence_uri"] = (
        "https://receipts.example/quote.bin?token=secret"
    )
    with pytest.raises(ThinSubnetError, match="credential-free"):
        verify_receipt(
            sign_receipt(query_document, miner),
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"genuine-quote",
            attestation_verifier=good_verifier,
        )


def test_attested_receipt_requires_validator_signed_request() -> None:
    miner = keypair(18)
    document = unsigned_receipt(miner, attested=True, authorize=False)
    raw = sign_receipt(document, miner)
    with pytest.raises(ThinSubnetError, match="validator-signed request"):
        verify_receipt(
            raw,
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"genuine-quote",
            attestation_verifier=good_verifier,
        )


def test_attested_policy_requires_every_trust_root_and_separate_runtime_pins() -> None:
    miner = keypair(12)
    raw = signed_receipt(miner, attested=True)
    missing_roots = ReceiptPolicy(
        network=NETWORK,
        netuid=NETUID,
        current_block=BLOCK,
        max_age_seconds=60,
        max_future_seconds=5,
        max_block_span=20,
        require_attestation=True,
    )
    with pytest.raises(ThinSubnetError, match="must separately pin model"):
        verify_receipt(raw, missing_roots, now=NOW)

    wrong_runner = ReceiptPolicy(
        **{
            **receipt_policy(require_attestation=True).__dict__,
            "allowed_runner_digests": frozenset({sha256_digest(b"wrong-runner")}),
        }
    )
    with pytest.raises(ThinSubnetError, match="runner digest is not allowed"):
        verify_receipt(
            raw,
            wrong_runner,
            now=NOW,
            attestation_evidence=b"genuine-quote",
            attestation_verifier=good_verifier,
        )


def test_tdx_report_data_binds_completion_time() -> None:
    miner = keypair(13)
    document = unsigned_receipt(miner, attested=True)
    original_report_data = document["attestation"]["report_data_hex"]
    document["completed_at"] = "2026-07-19T12:00:00.000000Z"
    assert document["attestation"]["report_data_hex"] == original_report_data
    replayed = sign_receipt(document, miner)
    with pytest.raises(ThinSubnetError, match="report data binding mismatch"):
        verify_receipt(
            replayed,
            receipt_policy(require_attestation=True),
            now=NOW,
            attestation_evidence=b"genuine-quote",
            attestation_verifier=good_verifier,
        )


def test_receipt_rejects_tampering_replay_and_policy_drift() -> None:
    miner = keypair(3)
    raw = signed_receipt(miner)
    document = json.loads(raw)
    document["result"]["work_units"] = "999"
    tampered = canonical_json(document)
    with pytest.raises(ThinSubnetError, match="receipt id"):
        verify_receipt(tampered, receipt_policy(require_attestation=False), now=NOW)

    wrong_policy = ReceiptPolicy(
        **{
            **receipt_policy(require_attestation=False).__dict__,
            "allowed_model_digests": frozenset({sha256_digest(b"other-model")}),
        }
    )
    with pytest.raises(ThinSubnetError, match="model weights digest"):
        verify_receipt(raw, wrong_policy, now=NOW)

    with pytest.raises(ThinSubnetError, match="output reveal"):
        verify_receipt(
            raw,
            receipt_policy(require_attestation=False),
            now=NOW,
            output_reveal=b"5",
        )


def test_receipt_work_units_are_bounded_before_aggregation() -> None:
    miner = keypair(19)
    document = unsigned_receipt(miner)
    document["result"]["work_units"] = "1000000000001"
    with pytest.raises(ThinSubnetError, match="protocol limit"):
        verify_receipt(
            sign_receipt(document, miner),
            receipt_policy(require_attestation=False),
            now=NOW,
        )


def test_bundle_is_content_addressed_replay_safe_and_aggregates_by_miner() -> None:
    miner_a = keypair(4)
    miner_b = keypair(5)
    receipts = [
        signed_receipt(miner_b, source_epoch=7, nonce_byte=3, attested=True),
        signed_receipt(miner_a, source_epoch=7, nonce_byte=1, attested=True),
        signed_receipt(miner_a, source_epoch=7, nonce_byte=2, attested=True),
    ]
    bundle_raw = build_bundle(
        receipts,
        network=NETWORK,
        netuid=NETUID,
        source_epoch=7,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    bundle, checkpoint = verify_bundle(
        bundle_raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
        attestation_verifier=good_verifier,
    )
    assert checkpoint == BundleCheckpoint(
        NETWORK,
        NETUID,
        7,
        bundle.bundle_id,
        datetime(2026, 7, 19, 11, 59, 59, tzinfo=UTC),
    )
    assert bundle_checkpoint_bytes(checkpoint).startswith(b'{"bundle_id":')
    assert [item.receipt_id for item in bundle.receipts] == sorted(
        item.receipt_id for item in bundle.receipts
    )
    aggregate = aggregate_receipts(bundle.receipts)
    assert aggregate[miner_a.ss58_address]["verified_work_units"] == "2.5"
    assert aggregate[miner_a.ss58_address]["verified_requests"] == 2
    assert aggregate[miner_b.ss58_address]["verified_work_units"] == "1.25"

    same, accepted = verify_bundle(
        bundle_raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        checkpoint=checkpoint,
        evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
        attestation_verifier=good_verifier,
    )
    assert same.bundle_id == bundle.bundle_id
    assert accepted == checkpoint

    wrong_network = BundleCheckpoint(
        "test",
        NETUID,
        checkpoint.source_epoch,
        checkpoint.bundle_id,
        checkpoint.generated_at,
    )
    with pytest.raises(ThinSubnetError, match="checkpoint network mismatch"):
        verify_bundle(
            bundle_raw,
            receipt_policy(require_attestation=True),
            now=NOW,
            checkpoint=wrong_network,
            evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
            attestation_verifier=good_verifier,
        )


def test_bundle_isolates_invalid_duplicate_without_zeroing_valid_work() -> None:
    miner = keypair(6)
    first = json.loads(signed_receipt(miner))
    second = dict(first)
    second["signature"] = dict(first["signature"])
    # A second canonical object with the same request must not be counted twice.
    second["receipt_id"] = sha256_digest(b"different-id")
    bundle_raw = build_bundle(
        [canonical_json(first), canonical_json(second)],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=1,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    bundle, _ = verify_bundle(
        bundle_raw,
        receipt_policy(require_attestation=False),
        now=NOW,
    )
    assert len(bundle.receipts) == 1
    assert len(bundle.rejections) == 1


def test_bundle_cannot_skip_the_accepted_epoch() -> None:
    miner = keypair(14)
    first_raw = build_bundle(
        [signed_receipt(miner, nonce_byte=1)],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=1,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    first, checkpoint = verify_bundle(
        first_raw,
        receipt_policy(require_attestation=False),
        now=NOW,
    )
    skipped = build_bundle(
        [signed_receipt(miner, source_epoch=3, nonce_byte=2)],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=3,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        previous_bundle_id=first.bundle_id,
    )
    with pytest.raises(ThinSubnetError, match="skipped"):
        verify_bundle(
            skipped,
            receipt_policy(require_attestation=False),
            now=NOW,
            checkpoint=checkpoint,
        )


def test_bundle_rejects_cross_epoch_receipt_replay_with_constant_checkpoint() -> None:
    miner = keypair(15)
    first_document = unsigned_receipt(miner, attested=True)
    receipt = sign_receipt(first_document, miner)
    first_raw = build_bundle(
        [receipt],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=1,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    first, checkpoint = verify_bundle(
        first_raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
        attestation_verifier=good_verifier,
    )
    rerun_document = json.loads(canonical_json(first_document))
    rerun_document["completed_at"] = "2026-07-19T12:00:01.000000Z"
    rerun_document["result"]["output_commitment"] = commit_payload(
        "output", b"fresh-rerun-output"
    )
    rerun_document["attestation"]["report_data_hex"] = expected_report_data(
        rerun_document
    ).hex()
    rerun_receipt = sign_receipt(rerun_document, miner)
    assert json.loads(rerun_receipt)["receipt_id"] != json.loads(receipt)["receipt_id"]
    assert json.loads(rerun_receipt)["request_id"] == json.loads(receipt)["request_id"]
    second_raw = build_bundle(
        [rerun_receipt],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=2,
        generated_at="2026-07-19T12:00:01.000000Z",
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        previous_bundle_id=first.bundle_id,
    )
    with pytest.raises(ThinSubnetError, match="different source epoch"):
        verify_bundle(
            second_raw,
            receipt_policy(require_attestation=True),
            now=datetime(2026, 7, 19, 12, 0, 2, tzinfo=UTC),
            checkpoint=checkpoint,
            evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
            attestation_verifier=good_verifier,
        )


def test_bad_receipt_does_not_zero_other_miners_in_bundle() -> None:
    good = signed_receipt(keypair(16), nonce_byte=1, attested=True)
    bad = signed_receipt(keypair(17), nonce_byte=2, attested=False)
    raw = build_bundle(
        [good, bad],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=1,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    bundle, _ = verify_bundle(
        raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
        attestation_verifier=good_verifier,
    )
    assert [item.miner_hotkey for item in bundle.receipts] == [keypair(16).ss58_address]
    assert len(bundle.rejections) == 1
    assert "unattested receipt" in bundle.rejections[0].reason


def test_verified_bundle_becomes_validator_assigned_score_report(tmp_path) -> None:
    miner = keypair(7)
    bundle_raw = build_bundle(
        [signed_receipt(miner, source_epoch=4, attested=True)],
        network=NETWORK,
        netuid=NETUID,
        source_epoch=4,
        generated_at=COMPLETED,
        valid_until=VALID_UNTIL,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
    )
    bundle, _ = verify_bundle(
        bundle_raw,
        receipt_policy(require_attestation=True),
        now=NOW,
        evidence_by_digest={sha256_digest(b"genuine-quote"): b"genuine-quote"},
        attestation_verifier=good_verifier,
    )

    report_key = Ed25519PrivateKey.generate()
    public = report_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    class_policy_document = {
        "schema": "cathedral_score_policy_v1",
        "network": NETWORK,
        "netuid": NETUID,
        "classes": [
            {
                "class_id": "verified_inference",
                "kind": "external",
                "allocation": "1",
                "source_id": "independent_receipt_verifier",
                "locations": [str(tmp_path / "report.json")],
                "trusted_keys": {
                    "verifyml-key-1": base64.b64encode(public).decode("ascii")
                },
                "max_age_seconds": 60,
                "max_future_seconds": 5,
                "max_block_span": 20,
                "require_evidence": True,
                "assignment": {
                    "mode": "metric",
                    "metric": "verified_work_units",
                    "transform": "linear",
                    "cap": "10",
                    "required_reason_codes": [
                        "attestation_verified",
                        "model_identity_bound",
                        "receipt_signature_verified",
                    ],
                    "required_evidence_kinds": [BUNDLE_SCHEMA],
                },
            }
        ],
    }
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(canonical_json(class_policy_document))
    score_policy = load_score_policy(policy_path, network=NETWORK, netuid=NETUID)
    body = score_report_body_from_bundle(
        bundle,
        class_id="verified_inference",
        source_id="independent_receipt_verifier",
        signing_key_id="verifyml-key-1",
        policy_digest=sha256_digest(b"receipt-admission-policy"),
        verifier_digest=VERIFIER_DIGEST,
        previous_report_id=None,
        evidence_uri="https://receipts.example/epoch-4.json",
    )
    with pytest.raises(ThinSubnetError, match="does not match every admitted"):
        score_report_body_from_bundle(
            bundle,
            class_id="verified_inference",
            source_id="independent_receipt_verifier",
            signing_key_id="verifyml-key-1",
            policy_digest=sha256_digest(b"receipt-admission-policy"),
            verifier_digest=sha256_digest(b"operator-asserted-wrong-verifier"),
            previous_report_id=None,
            evidence_uri=None,
        )
    signed = sign_report(body, report_key)
    report = verify_report(
        signed,
        score_policy.external_classes[0],
        network=NETWORK,
        netuid=NETUID,
        current_block=BLOCK,
        now=NOW,
    )
    assert report.entries[0].metrics["verified_work_units"] == Decimal("1.25")
    assert report.entries[0].evidence[0].kind == BUNDLE_SCHEMA
    decision = external_class_decision(
        score_policy.external_classes[0],
        report,
        coldkey_of={miner.ss58_address: "coldkey-a"},
    )
    assert decision.raw_scores == {miner.ss58_address: 1.25}
    assert compose_class_decisions(score_policy, [decision]) == {
        miner.ss58_address: 1.0
    }


def test_unattested_receipts_never_produce_verified_work_units() -> None:
    miner = keypair(8)
    receipt = verify_receipt(
        signed_receipt(miner),
        receipt_policy(require_attestation=False),
        now=NOW,
    )
    assert aggregate_receipts([receipt]) == {}


def test_subprocess_attestation_verifier_is_executable_pinned(tmp_path) -> None:
    verifier_path = tmp_path / "quote-verifier.py"
    verifier_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "_, evidence, report_data, policy, result = sys.argv\n"
        'body = {"ok": True, "report_data_match": len(report_data) == 128, '
        '"policy_digest": policy, "measurement_digest": '
        f'"{MEASUREMENT_DIGEST}", "reason": "verified"}}\n'
        "with open(result, 'w', encoding='utf-8') as handle: "
        "json.dump(body, handle, sort_keys=True, separators=(',', ':'))\n",
        encoding="utf-8",
    )
    os.chmod(verifier_path, 0o700)
    verifier = subprocess_attestation_verifier(
        [
            str(verifier_path),
            "{evidence_path}",
            "{report_data_hex}",
            "{policy_digest}",
            "{result_path}",
        ],
        expected_verifier_digest=sha256_digest(verifier_path.read_bytes()),
    )
    result = verifier(b"quote", bytes(64), POLICY_DIGEST)
    assert result.ok is True
    assert result.measurement_digest == MEASUREMENT_DIGEST

    verifier_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    with pytest.raises(ThinSubnetError, match="changed after pinning"):
        verifier(b"quote", bytes(64), POLICY_DIGEST)

    with pytest.raises(ThinSubnetError, match="executable digest"):
        subprocess_attestation_verifier(
            [
                str(verifier_path),
                "{evidence_path}",
                "{report_data_hex}",
                "{policy_digest}",
                "{result_path}",
            ],
            expected_verifier_digest=sha256_digest(b"different"),
        )


def test_run_local_cli_creates_noncreditable_receipt(
    tmp_path, monkeypatch, capsys
) -> None:
    from cathedral_thin import verifyml_cli

    miner = keypair(10)
    monkeypatch.setattr(
        verifyml_cli, "_wallet", lambda _args: SimpleNamespace(hotkey=miner)
    )
    input_path = tmp_path / "input.txt"
    parameters_path = tmp_path / "parameters.json"
    weights_path = tmp_path / "model.weights"
    input_path.write_bytes(b"prompt")
    parameters_path.write_bytes(b'{"temperature":"0"}')
    weights_path.write_bytes(b"real-model-placeholder")
    output_path = tmp_path / "output.txt"
    receipt_path = tmp_path / "receipt.json"
    rc = verifyml_cli.main(
        [
            "run-local",
            "--validator-hotkey",
            keypair(11).ss58_address,
            "--source-epoch",
            "1",
            "--valid-from-block",
            str(BLOCK - 1),
            "--valid-until-block",
            str(BLOCK + 1),
            "--issued-at",
            ISSUED,
            "--completed-at",
            COMPLETED,
            "--model-id",
            "test/model",
            "--weights-file",
            str(weights_path),
            "--image-digest",
            RUNTIME_DIGEST,
            "--runner-file",
            sys.executable,
            "--input-file",
            str(input_path),
            "--parameters-file",
            str(parameters_path),
            "--output-file",
            str(output_path),
            "--receipt",
            str(receipt_path),
            "--",
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'model-output')",
        ]
    )
    assert rc == 0
    assert output_path.read_bytes() == b"model-output"
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["creditable_as_verified_work"] is False
    receipt = verify_receipt(
        receipt_path.read_bytes(),
        ReceiptPolicy(
            network=NETWORK,
            netuid=NETUID,
            current_block=BLOCK,
            max_age_seconds=60,
            max_future_seconds=5,
            max_block_span=20,
            require_attestation=False,
        ),
        now=NOW,
        input_reveal=b"prompt",
        parameters_reveal=b'{"temperature":"0"}',
        output_reveal=b"model-output",
    )
    assert receipt.attestation_verified is False


def test_authorize_cli_signs_exact_pre_inference_request(
    tmp_path, monkeypatch, capsys
) -> None:
    from cathedral_thin import verifyml_cli

    validator = keypair(20)
    miner = keypair(21)
    monkeypatch.setattr(
        verifyml_cli, "_wallet", lambda _args: SimpleNamespace(hotkey=validator)
    )
    input_path = tmp_path / "input.bin"
    parameters_path = tmp_path / "parameters.json"
    authorization_path = tmp_path / "request-authorization.json"
    input_path.write_bytes(b"validator-selected-prompt")
    parameters_path.write_bytes(b'{"temperature":"0"}')
    nonce = bytes([22]) * 32
    assert (
        verifyml_cli.main(
            [
                "authorize",
                "--source-epoch",
                "1",
                "--miner-hotkey",
                miner.ss58_address,
                "--valid-from-block",
                str(BLOCK - 5),
                "--valid-until-block",
                str(BLOCK + 5),
                "--issued-at",
                ISSUED,
                "--nonce-base64",
                base64.b64encode(nonce).decode("ascii"),
                "--model-id",
                "test/model",
                "--weights-digest",
                MODEL_DIGEST,
                "--image-digest",
                RUNTIME_DIGEST,
                "--runner-digest",
                RUNNER_DIGEST,
                "--input-file",
                str(input_path),
                "--parameters-file",
                str(parameters_path),
                "--output",
                str(authorization_path),
            ]
        )
        == 0
    )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["validator_hotkey"] == validator.ss58_address
    template = make_unsigned_receipt(
        network=NETWORK,
        netuid=NETUID,
        source_epoch=1,
        validator_hotkey=validator.ss58_address,
        miner_hotkey=miner.ss58_address,
        nonce=nonce,
        issued_at=ISSUED,
        completed_at=COMPLETED,
        valid_from_block=BLOCK - 5,
        valid_until_block=BLOCK + 5,
        model_id="test/model",
        weights_digest=MODEL_DIGEST,
        tokenizer_digest=None,
        image_digest=RUNTIME_DIGEST,
        runner_digest=RUNNER_DIGEST,
        input_commitment=commit_payload("input", input_path.read_bytes()),
        parameters_commitment=commit_payload(
            "parameters", parameters_path.read_bytes()
        ),
        output_commitment=commit_payload("output", b"later-output"),
        input_tokens=1,
        output_tokens=1,
        latency_ms="1",
        work_units="1",
    )
    authorized = apply_request_authorization(template, authorization_path.read_bytes())
    assert authorized["request_authorization"]["algorithm"] == "sr25519"


def test_verify_bundle_cli_persists_and_reloads_checkpoint(
    tmp_path, monkeypatch, capsys
) -> None:
    from cathedral_thin import verifyml_cli

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_bytes(b"{}")
    checkpoint_path = tmp_path / "checkpoint.json"
    expected = BundleCheckpoint(
        network=NETWORK,
        netuid=NETUID,
        source_epoch=4,
        bundle_id=sha256_digest(b"bundle-4"),
        generated_at=NOW,
    )
    observed = []

    def fake_verify_bundle(_raw, _policy, **kwargs):
        observed.append(kwargs["checkpoint"])
        return (
            SimpleNamespace(
                bundle_id=expected.bundle_id,
                source_epoch=expected.source_epoch,
                receipts=(),
                rejections=(),
            ),
            expected,
        )

    monkeypatch.setattr(verifyml_cli, "verify_bundle", fake_verify_bundle)
    arguments = [
        "verify-bundle",
        "--current-block",
        str(BLOCK),
        "--allow-unattested",
        "--bundle",
        str(bundle_path),
        "--checkpoint",
        str(checkpoint_path),
    ]
    assert verifyml_cli.main(arguments) == 0
    assert checkpoint_path.read_bytes() == bundle_checkpoint_bytes(expected)
    capsys.readouterr()
    assert verifyml_cli.main(arguments) == 0
    assert observed == [None, expected]
