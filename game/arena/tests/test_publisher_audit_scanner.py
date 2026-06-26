from __future__ import annotations

import base64
from datetime import datetime, timezone
from dataclasses import replace

from fastapi.testclient import TestClient
from bittensor_wallet import Keypair

from game.arena import audit_scanner_smoke
from game.arena import scanner
from scaffold.publisher import build_app
from scaffold.publisher.app import _AUDIT_SCANNER_CARD, _empty_bundle_hash
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher import weights as weights_mod


def _now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _payload_and_headers(sub: scanner.ScannerSubmission, keypair: Keypair) -> tuple[dict, dict]:
    payload = sub.as_artifact()
    payload["report"] = sub.report
    submitted_at = _now_iso()
    artifact_sha = scanner._sha(sub.as_artifact())
    msg = canonical_claim_bytes(
        bundle_hash=_empty_bundle_hash(),
        card_id=_AUDIT_SCANNER_CARD,
        miner_hotkey=keypair.ss58_address,
        submitted_at=submitted_at,
        challenge_id=sub.task_id,
        dimacs_solution_sha256=artifact_sha,
    )
    headers = {
        "X-Cathedral-Hotkey": keypair.ss58_address,
        "X-Cathedral-Submitted-At": submitted_at,
        "X-Cathedral-Signature": base64.b64encode(keypair.sign(msg)).decode("ascii"),
    }
    return payload, headers


def _miner_weight(vector: dict, hotkey: str) -> float:
    for row in vector.get("weights", []):
        if row.get("miner_hotkey") == hotkey:
            return float(row.get("weight") or 0.0)
    return 0.0


def test_audit_scanner_bridge_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_ENABLED", "0")
    weights_mod._reset_vector_cache()
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())

    with TestClient(app) as client:
        status = client.get("/v1/audit-scanner/status").json()
        assert status["enabled"] is False
        assert status["payment_weights"] is False
        assert status["card_id"] == _AUDIT_SCANNER_CARD
        assert client.get("/v1/audit-scanner/catalog").status_code == 404


def test_audit_scanner_bridge_scores_only_signed_replayable_proof(monkeypatch, tmp_path):
    monkeypatch.delenv("CATHEDRAL_AUDIT_SCANNER_ENABLED", raising=False)
    monkeypatch.delenv("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION", raising=False)
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH", str(tmp_path / "audit_scanner.jsonl"))
    monkeypatch.setenv("CATHEDRAL_AUDIT_REPLAY_BONUS_MULT", "0.25")
    weights_mod._reset_vector_cache()
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
    miner = Keypair.create_from_uri("//AuditScannerMiner")
    reporter = Keypair.create_from_uri("//AuditScannerReporter")
    task = scanner.issue_task(0)

    with TestClient(app) as client:
        assert client.get("/v1/audit-scanner/status").json()["enabled"] is True
        assert client.get("/v1/audit-scanner/status").json()["payment_weights"] is True
        assert client.get("/v1/audit-scanner/catalog?limit=2").json()["count"] == 2
        differential = client.get("/v1/audit-scanner/differential").json()
        assert differential["schema"] == "cathedral.arena.replay_differential.v1"
        assert differential["all_real"] is True
        assert differential["payment_weights"] is True
        assert differential["scoring"] == "verifier_quality_gate_only"
        intake = client.post("/v1/audit-scanner/request", json={"repo": "https://example/repo", "max_tasks": 1}).json()
        assert intake["scored"] is False and intake["routed_count"] == 1

        good = scanner.example_accepted_submission(task, miner_hotkey=miner.ss58_address)
        good_payload, good_headers = _payload_and_headers(good, miner)
        replay = client.post("/v1/audit-scanner/replay", json=good_payload, headers=good_headers).json()
        assert replay["accepted"] is True
        assert replay["scored"] is False
        assert replay["ledger_written"] is False

        submit = client.post("/v1/audit-scanner/submit", json=good_payload, headers=good_headers).json()
        assert submit["accepted"] is True
        assert submit["scored"] is True
        assert submit["payment_weights"] is True
        assert submit["eval_run_id"]
        assert submit["payment_row_emitted"] is True
        assert submit["payment"]["weighted_score"] > 0.0
        assert submit["attestation_required"] is False
        weights_mod._reset_vector_cache()
        vector = client.get("/v1/validator/weights/next").json()
        assert _miner_weight(vector, miner.ss58_address) == 1.0
        assert vector["policy_metadata"]["audit_replay"]["bonus_multiplier"] == 0.25

        duplicate_payload, duplicate_headers = _payload_and_headers(good, miner)
        duplicate = client.post(
            "/v1/audit-scanner/submit",
            json=duplicate_payload,
            headers=duplicate_headers,
        ).json()
        assert duplicate["accepted"] is False
        assert duplicate["payment_row_emitted"] is False
        assert duplicate["eval_run_id"] is None

        report_only = replace(
            scanner.example_accepted_submission(task, miner_hotkey=reporter.ss58_address),
            witness=None,
            report="Correct category, no replayable witness.",
        )
        report_payload, report_headers = _payload_and_headers(report_only, reporter)
        report = client.post("/v1/audit-scanner/submit", json=report_payload, headers=report_headers).json()
        assert report["accepted"] is False
        assert report["score"] == 0.0
        assert report["gates"]["decode_map_present"] is False

        board = client.get("/v1/audit-scanner/leaderboard").json()
        killer = next(m for m in board["miners"] if m["miner_hotkey"] == miner.ss58_address)
        assert killer["kills"] == 1
        assert killer["score"] > 0
        submissions = client.get("/v1/audit-scanner/submissions?limit=1").json()
        assert submissions["schema"] == "cathedral.audit_scanner.submissions.v1"
        assert submissions["count"] == 1
        assert submissions["total"] == 3
        assert submissions["entries"][0]["miner_hotkey"] == reporter.ss58_address
        assert submissions["contains_witnesses"] is False
        assert submissions["payment_weights"] is True


def test_audit_scanner_attestation_required_withholds_unattested_rows(monkeypatch, tmp_path):
    monkeypatch.delenv("CATHEDRAL_AUDIT_SCANNER_ENABLED", raising=False)
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION", "1")
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH", str(tmp_path / "audit_scanner.jsonl"))
    weights_mod._reset_vector_cache()
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
    miner = Keypair.create_from_uri("//AuditScannerAttest")
    task = scanner.issue_task(0)

    with TestClient(app) as client:
        good = scanner.example_accepted_submission(task, miner_hotkey=miner.ss58_address)
        payload, headers = _payload_and_headers(good, miner)
        submit = client.post("/v1/audit-scanner/submit", json=payload, headers=headers).json()
        assert submit["accepted"] is True
        assert submit["payment_weights"] is True
        assert submit["eval_run_id"]
        assert submit["payment_row_emitted"] is True
        assert submit["attestation_required"] is True
        assert submit["attestation_status"] == "pending"
        weights_mod._reset_vector_cache()
        vector = client.get("/v1/validator/weights/next").json()
        assert _miner_weight(vector, miner.ss58_address) == 0.0
        assert vector["policy_metadata"]["audit_replay"]["require_attestation"] is True


def test_audit_scanner_payment_fails_closed_when_task_type_not_scored(monkeypatch, tmp_path):
    monkeypatch.delenv("CATHEDRAL_AUDIT_SCANNER_ENABLED", raising=False)
    monkeypatch.delenv("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION", raising=False)
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_TASK_TYPE", "audit_custom_v1")
    monkeypatch.setenv("CATHEDRAL_AUDIT_REPLAY_TASK_TYPES", "audit_replay_v1")
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH", str(tmp_path / "audit_scanner.jsonl"))
    weights_mod._reset_vector_cache()
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
    miner = Keypair.create_from_uri("//AuditScannerMismatch")
    task = scanner.issue_task(0)

    with TestClient(app) as client:
        status = client.get("/v1/audit-scanner/status").json()
        assert status["enabled"] is True
        assert status["payment_weights"] is False
        assert status["payment"]["degraded_reason"] == "audit_scanner_task_type_not_in_weight_policy"
        good = scanner.example_accepted_submission(task, miner_hotkey=miner.ss58_address)
        payload, headers = _payload_and_headers(good, miner)
        submit = client.post("/v1/audit-scanner/submit", json=payload, headers=headers).json()
        assert submit["accepted"] is True
        assert submit["payment_weights"] is False
        assert submit["payment_row_emitted"] is False
        assert submit["eval_run_id"] is None
        weights_mod._reset_vector_cache()
        vector = client.get("/v1/validator/weights/next").json()
        assert _miner_weight(vector, miner.ss58_address) == 0.0


def test_audit_scanner_signature_binds_artifact_body(monkeypatch, tmp_path):
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH", str(tmp_path / "audit_scanner.jsonl"))
    app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
    miner = Keypair.create_from_uri("//AuditScannerTamper")
    task = scanner.issue_task(0)
    sub = scanner.example_accepted_submission(task, miner_hotkey=miner.ss58_address)
    payload, headers = _payload_and_headers(sub, miner)
    payload["witness"] = {k: 0 for k in task.required_fields}

    with TestClient(app) as client:
        response = client.post("/v1/audit-scanner/submit", json=payload, headers=headers)
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid hotkey signature"


def test_audit_scanner_smoke_cli_exercises_signed_bridge(tmp_path, capsys):
    rc = audit_scanner_smoke.main([
        "--ledger-path",
        str(tmp_path / "audit_scanner_smoke.jsonl"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "AUDIT SCANNER SMOKE: PASS" in out
    assert "submissions: total=1 contains_witnesses=false" in out
    assert "payment_weights: true" in out
