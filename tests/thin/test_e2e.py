from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cathedral_thin.e2e import run_e2e
from cathedral_thin.score_classes import format_time, sign_report


def test_complete_local_subnet_loop():
    evidence = asyncio.run(run_e2e())
    assert evidence["ok"]
    assert evidence["owner_hosted_services"] == 0
    assert evidence["sybil_no_multiplier"]
    assert evidence["historical_offline_gated"]
    assert evidence["confirmed_after_retry"]


def test_local_validator_accepts_external_report_bytes_through_normal_verifier():
    issued = datetime.now(UTC)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    report = sign_report(
        {
            "schema": "cathedral_score_class_report_v1",
            "network": "local",
            "netuid": 1,
            "class_id": "confidential_compute",
            "source_id": "cathedralconfidential",
            "source_epoch": 7,
            "generated_at": format_time(issued),
            "valid_until": format_time(issued + timedelta(minutes=5)),
            "valid_from_block": 70,
            "valid_until_block": 80,
            "complete": True,
            "policy_digest": "sha256:" + "11" * 32,
            "verifier_digest": "sha256:" + "22" * 32,
            "previous_report_id": None,
            "entries": [
                {
                    "miner_hotkey": hotkey,
                    "metrics": {"verified_work_units": units},
                    "asserted_score": None,
                    "reason_codes": ["receipt_verified", "work_verified"],
                    "evidence": [
                        {
                            "kind": "cathedral_assurance_receipt_v2",
                            "id": "receipt-sha256:" + digest,
                            "digest": "sha256:" + digest,
                            "uri": None,
                        }
                    ],
                }
                for hotkey, units, digest in (
                    ("honest-a", "1", "1" * 64),
                    ("honest-a2", "1", "2" * 64),
                    ("honest-b", "2", "3" * 64),
                )
            ],
            "signing_key_id": "e2e-score-key",
        },
        key,
    )

    evidence = asyncio.run(
        run_e2e(external_report_raw=report, external_public_key=public)
    )

    assert evidence["ok"]
    assert evidence["score_classes"]["report_origin"] == "external_report_bytes"
    assert evidence["score_classes"]["external_hotkeys"] == [
        "honest-a",
        "honest-a2",
        "honest-b",
    ]
