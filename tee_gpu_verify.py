"""Smoke checks for the default-off TEE GPU capacity lane.

Run with the publisher extra installed:

    python tee_gpu_verify.py

This verifies the storage and preflight path only. It does not contact Chutes and
does not prove hardware attestation.
"""
from __future__ import annotations

import os
import hashlib
import json
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from scaffold.publisher.store import Store
from scaffold.publisher import tee_gpu


@contextmanager
def _clean_env():
    keys = [
        "DATABASE_URL",
        "CATHEDRAL_TEE_GPU_ENABLED",
        "CATHEDRAL_TEE_GPU_ADMIN_TOKEN",
        "CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED",
        "CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE",
        "CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE",
        "CATHEDRAL_TEE_GPU_VERIFY_CMD",
        "CATHEDRAL_TEE_GPU_VERIFY_TIMEOUT_SECS",
        "CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH",
        "CATHEDRAL_TEE_GPU_CHUTES_CLI",
        "CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED",
        "CATHEDRAL_TEE_GPU_CHUTES_TIMEOUT_SECS",
        "CATHEDRAL_TEE_GPU_LISTING_STALE_SECS",
        "CATHEDRAL_TEE_GPU_EVIDENCE_REQUEST_TTL_SECS",
        "CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE",
        "CATHEDRAL_TEE_GPU_INTAKE_CODE",
        "CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST",
    ]
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _write_fixture_verifier(*, ok: bool = True, missing_checks: bool = False) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    checks = {
        "tdx_verified": ok and not missing_checks,
        "gpu_verified": ok and not missing_checks,
        "gpu_claims_match": ok and not missing_checks,
        "report_data_match": ok and not missing_checks,
        "debug_disabled": ok and not missing_checks,
    }
    f.write(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if len(args) == 3:\n"
        "    evidence_path, request_path, result_path = args\n"
        "    capacity_path = None\n"
        "else:\n"
        "    evidence_path, request_path, capacity_path, result_path = args[:4]\n"
        "evidence = json.load(open(evidence_path, encoding='utf-8'))\n"
        "request = json.load(open(request_path, encoding='utf-8'))\n"
        "capacity = json.load(open(capacity_path, encoding='utf-8')) if capacity_path else {}\n"
        "request_ok = evidence.get('evidence_request_id') == request.get('request_id')\n"
        "capacity_ok = not capacity or (capacity.get('gpu_short_ref') == 'h200' and capacity.get('gpu_count') == 8)\n"
        f"ok = {bool(ok)!r} and request_ok\n"
        f"checks = {checks!r}\n"
        "checks['gpu_claims_match'] = checks.get('gpu_claims_match') and capacity_ok\n"
        "result = {'ok': ok, 'verified': ok, 'verifier': 'fixture-tdx-gpu', "
        "'proof': 'fixture_dcap_nvidia', 'reason': 'fixture'}\n"
        "if ok:\n"
        "    result.update(checks)\n"
        "with open(result_path, 'w', encoding='utf-8') as out:\n"
        "    json.dump(result, out)\n"
        "sys.exit(0)\n"
    )
    f.close()
    return f.name


def main() -> None:
    assert tee_gpu._parse_iso("2026-06-20T12:00:00") == tee_gpu._parse_iso(
        "2026-06-20T12:00:00Z")
    try:
        tee_gpu._evidence_verifier_args(
            'verify {0} {evidence_path}',
            evidence_path="evidence.json",
            request_path="request.json",
            capacity_path="capacity.json",
            result_path="result.json",
        )
        raise AssertionError("malformed verifier command template accepted")
    except tee_gpu.HTTPException as e:
        assert e.status_code == 400
        assert e.detail["detail"] == "tee_gpu_evidence_verifier_command_invalid"
    with _clean_env():
        os.environ["CATHEDRAL_TEE_GPU_ENABLED"] = "1"
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        store = Store(db_path)
        try:
            record = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.10:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                    "region": "test",
                    "status": "active",
                    "admin_note": "should_not_stick",
                    "chutes_validator_hotkey": "attacker-validator",
                    "authorization": {
                        "operator_use_authorized": True,
                        "intake_code": "must-not-persist",
                    },
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_created",
                preserve_admin_fields=True,
            )
            assert record["preflight_status"] == "eligible", record
            assert record["status"] == "pending"
            assert record["admin_note"] == ""
            assert record["chutes_validator_hotkey"] != "attacker-validator"
            assert record["operator_use_authorized"] == 1
            assert record["emissions_eligible"] == 0
            authorization = json.loads(record["authorization_json"])
            assert "intake_code" not in authorization["supplied"]

            single_h200 = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-h200-single",
                    "gpu_short_ref": "h200",
                    "gpu_count": 1,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.11:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_single_h200_created",
            )
            assert single_h200["preflight_status"] == "blocked"
            assert "gpu_count_not_tee_measurement_profile" in json.loads(
                single_h200["preflight_json"]
            )["reasons"]

            rows = tee_gpu.list_capacity(store, status="pending")
            assert len(rows) == 2
            assert any(row["capacity_id"] == record["capacity_id"] for row in rows)

            metrics = tee_gpu.capacity_metrics(store)
            assert metrics["count"] == 2
            assert metrics["active_gpus"] == 0
            assert metrics["active_listed_hourly_cost"] == 0.0
            assert metrics["emissions_eligible"] is False
            assert metrics["intake_gate"]["required"] is True
            assert metrics["intake_gate"]["configured"] is False

            try:
                tee_gpu.require_miner_intake_gate("5OpenHotkey", {})
                raise AssertionError("unconfigured intake gate did not fail closed")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 503
                assert e.detail == "tee_gpu_intake_gate_not_configured"
            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE"] = "1"
            try:
                tee_gpu.require_miner_intake_gate("5OpenHotkey", {})
                raise AssertionError("missing configured intake code did not fail closed")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 503
                assert e.detail == "tee_gpu_intake_code_not_configured"
            os.environ["CATHEDRAL_TEE_GPU_INTAKE_CODE"] = "launch-secret"
            try:
                tee_gpu.require_miner_intake_gate("5OpenHotkey", {"intake_code": "wrong"})
                raise AssertionError("bad intake code accepted")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 403
                assert e.detail == "invalid_tee_gpu_intake_code"
            tee_gpu.require_miner_intake_gate("5OpenHotkey", {"intake_code": "launch-secret"})
            tee_gpu.require_miner_intake_gate("5OpenHotkey", {"invite_code": "launch-secret"})
            os.environ["CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST"] = "5AllowedHotkey, 5Other"
            tee_gpu.require_miner_intake_gate("5AllowedHotkey", {})
            gated_metrics = tee_gpu.capacity_metrics(store)
            assert gated_metrics["intake_gate"]["required"] is True
            assert gated_metrics["intake_gate"]["code_configured"] is True
            assert gated_metrics["intake_gate"]["configured"] is True
            assert gated_metrics["intake_gate"]["allowlist_count"] == 2
            os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_INTAKE_CODE", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST", None)

            h100 = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-h100-0",
                    "gpu_short_ref": "h100",
                    "gpu_count": 1,
                    "hourly_cost": 1.79,
                    "agent_api": "http://203.0.113.14:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_h100_created",
            )
            assert h100["preflight_status"] == "blocked"
            assert "gpu_short_ref_not_tee_candidate" in json.loads(
                h100["preflight_json"]
            )["reasons"]

            tpu = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-tpu-v5e-0",
                    "gpu_short_ref": "tpu_v5e",
                    "gpu_count": 8,
                    "hourly_cost": 1.25,
                    "agent_api": "https://tpu-worker.example.invalid",
                    "tee_kind": "google_tpu",
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_tpu_created",
            )
            tpu_preflight = json.loads(tpu["preflight_json"])
            assert tpu["preflight_status"] == "exploratory"
            assert tpu_preflight["capacity_kind"] == "google_tpu"
            assert "google_tpu_exploratory_intake_only" in tpu_preflight["warnings"]
            tpu_manifest = tee_gpu.chutes_manifest_item(tpu)
            assert not tpu_manifest["ready"]
            assert "google_tpu_not_chutes_listable" in tpu_manifest["missing"]
            assert tee_gpu.public_record(tpu)["emissions_eligible"] is False

            evidence_request = tee_gpu.create_evidence_request(
                store,
                owner_hotkey="5VerifyHotkey",
                node_id="verify-evidence-h200-0",
                actor="verify",
                ttl_secs=30,
            )
            assert evidence_request["ttl_secs"] == 60
            assert evidence_request["capacity_id"].startswith("tee-")
            assert evidence_request["evidence_request_binding"]["sha256_hex"]
            events = store.query(
                "SELECT COUNT(*) AS n FROM tee_gpu_capacity_events "
                "WHERE capacity_id=? AND event_type='evidence_request_created'",
                (evidence_request["capacity_id"],),
            )
            assert events[0]["n"] == 1

            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE"] = "1"
            spoofed_review = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-spoofed-review-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.16:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                    "attestation": {
                        "cathedral_review": {"status": "operator_reviewed"},
                    },
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_spoofed_review_created",
            )
            spoofed_preflight = json.loads(spoofed_review["preflight_json"])
            assert spoofed_review["preflight_status"] == "blocked"
            assert "attestation_evidence_required" in spoofed_preflight["reasons"]
            assert tee_gpu.miner_record(spoofed_review)["evidence"]["status"] == "missing"
            try:
                tee_gpu.review_capacity_evidence(
                    store,
                    spoofed_review["capacity_id"],
                    {"status": "operator_reviewed", "reason": "empty evidence"},
                )
                raise AssertionError("empty evidence accepted as operator-reviewed")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400

            evidence_pending = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-evidence-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.15:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                    "attestation": {
                        "evidence_request_id": evidence_request["request_id"],
                        "tdx_quote_b64": "not-a-real-quote",
                        "gpu_evidence_json": {"cc_mode": "claimed"},
                    },
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_evidence_created",
            )
            pending_preflight = json.loads(evidence_pending["preflight_json"])
            assert evidence_pending["preflight_status"] == "blocked"
            assert "attestation_evidence_review_required" in pending_preflight["reasons"]
            assert tee_gpu.admin_record(evidence_pending)["evidence"]["status"] == "submitted"

            missing_request = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-missing-request-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.17:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                    "attestation": {
                        "evidence_request_id": "not-issued",
                        "tdx_quote_b64": "not-a-real-quote",
                    },
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_missing_request_created",
            )
            try:
                tee_gpu.review_capacity_evidence(
                    store,
                    missing_request["capacity_id"],
                    {"status": "operator_reviewed", "reason": "made-up request id"},
                )
                raise AssertionError("made-up evidence request accepted")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail == "evidence_request_not_found"

            admin_direct_spoof = tee_gpu.create_capacity(
                store,
                {
                    "owner_hotkey": "5VerifyHotkey",
                    "node_id": "verify-admin-direct-spoof-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.18:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                    "status": "active",
                    "attestation_json": {
                        "cathedral_review": {"status": "operator_reviewed"},
                    },
                },
                owner_hotkey="5VerifyHotkey",
                actor="admin",
                event_type="verify_admin_direct_spoof_created",
                allow_requested_status=True,
            )
            assert admin_direct_spoof["status"] == "pending"
            assert admin_direct_spoof["preflight_status"] == "blocked"
            assert tee_gpu.admin_record(admin_direct_spoof)["evidence"]["status"] == "missing"

            direct_patch = tee_gpu.update_capacity_admin(
                store,
                evidence_pending["capacity_id"],
                {
                    "attestation_json": {
                        "evidence_request_id": evidence_request["request_id"],
                        "tdx_quote_b64": "not-a-real-quote",
                        "cathedral_review": {"status": "operator_reviewed"},
                    },
                },
            )
            assert direct_patch is not None
            assert direct_patch["status"] == "pending"
            assert direct_patch["preflight_status"] == "blocked"
            assert tee_gpu.admin_record(direct_patch)["evidence"]["status"] == "submitted"

            reviewed = tee_gpu.review_capacity_evidence(
                store,
                evidence_pending["capacity_id"],
                {"status": "operator_reviewed", "reason": "manual fixture review"},
            )
            assert reviewed is not None
            assert reviewed["preflight_status"] == "eligible"
            reviewed_record = tee_gpu.admin_record(reviewed)
            assert reviewed_record["evidence"]["status"] == "operator_reviewed"
            assert reviewed_record["evidence"]["acceptable"] is True
            assert reviewed_record["evidence"]["proof"] == "operator_review_only"

            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE"] = "1"
            crypto_recheck = tee_gpu.update_capacity_admin(
                store,
                evidence_pending["capacity_id"],
                {"admin_note": "crypto evidence required"},
            )
            assert crypto_recheck is not None
            crypto_recheck_preflight = json.loads(crypto_recheck["preflight_json"])
            assert crypto_recheck["preflight_status"] == "blocked"
            assert "cryptographic_attestation_required" in crypto_recheck_preflight["reasons"]
            try:
                tee_gpu.verify_capacity_evidence(store, evidence_pending["capacity_id"])
                raise AssertionError("crypto evidence verification accepted without verifier")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 503
            try:
                tee_gpu.record_provider_status(
                    store,
                    evidence_pending["capacity_id"],
                    {
                        "provider_status": "listed",
                        "server_id": "fixture-server-before-crypto",
                    },
                )
                raise AssertionError("provider status accepted before crypto verification")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail == "cryptographic_attestation_required"
            missing_checks_verifier = _write_fixture_verifier(ok=True, missing_checks=True)
            os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = f"{sys.executable} {missing_checks_verifier}"
            try:
                tee_gpu.verify_capacity_evidence(store, evidence_pending["capacity_id"])
                raise AssertionError("verifier result with missing checks accepted")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail["detail"] == "tee_gpu_evidence_verifier_missing_required_checks"
                assert "gpu_claims_match" in e.detail["missing"]
            fixture_verifier = _write_fixture_verifier(ok=True)
            os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = f"{sys.executable} {fixture_verifier}"
            crypto_verified = tee_gpu.verify_capacity_evidence(store, evidence_pending["capacity_id"])
            assert crypto_verified is not None
            assert crypto_verified["preflight_status"] == "eligible"
            crypto_record = tee_gpu.admin_record(crypto_verified)
            assert crypto_record["evidence"]["status"] == "cryptographically_verified"
            assert crypto_record["evidence"]["acceptable"] is True
            assert crypto_record["evidence"]["cryptographic_proof"] is True
            assert crypto_record["evidence"]["gpu_claims_match"] is True
            assert crypto_record["evidence"]["verifier"] == "fixture-tdx-gpu"
            expected_verifier_digest = hashlib.sha256(
                os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"].encode("utf-8")
            ).hexdigest()
            assert crypto_record["evidence"]["verifier_command_digest"] == expected_verifier_digest
            verifier_events = store.query(
                "SELECT event_type, event_json FROM tee_gpu_capacity_events "
                "WHERE capacity_id=? AND event_type IN ("
                "'attestation_verification_failed', "
                "'attestation_verification_succeeded')",
                (evidence_pending["capacity_id"],),
            )
            verifier_payloads = [json.loads(row["event_json"]) for row in verifier_events]
            assert any(
                payload.get("counts_as_bad_evidence_rejection") is True
                and payload.get("verifier_command_digest")
                for payload in verifier_payloads
            )
            assert any(
                payload.get("verifier") == "fixture-tdx-gpu"
                and payload.get("verifier_command_digest") == expected_verifier_digest
                for payload in verifier_payloads
            )
            no_name_verified = tee_gpu._attestation_verified_json(
                {
                    "evidence_request_id": evidence_request["request_id"],
                    "tdx_quote_b64": "fixture-quote",
                },
                {
                    "ok": True,
                    "tdx_verified": True,
                    "gpu_verified": True,
                    "report_data_match": True,
                    "debug_disabled": True,
                },
                request_payload=evidence_request,
                reviewed_by="verify",
            )
            no_name_summary = tee_gpu.evidence_summary(no_name_verified)
            assert no_name_summary["verifier"] == "configured_verifier"
            assert os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] not in json.dumps(no_name_summary)
            listable_crypto = tee_gpu.update_capacity_admin(
                store,
                evidence_pending["capacity_id"],
                {
                    "status": "active",
                    "chutes_server_name": "worker-h200-crypto-0",
                    "chutes_status": "queued",
                },
            )
            assert listable_crypto is not None
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH"] = "/tmp/chutes.hotkey"
            manifest = tee_gpu.chutes_manifest_item(listable_crypto)
            assert manifest["ready"] is True
            assert manifest["command"] and "--name worker-h200-crypto-0" in manifest["command"]
            assert "--agent-api http://203.0.113.15:32000" in manifest["command"]
            dry = tee_gpu.list_capacity_on_chutes(store, listable_crypto["capacity_id"])
            assert dry["status"] == "dry_run"
            assert dry["executed"] is False
            assert "--agent-api" in dry["command"]
            try:
                tee_gpu.list_capacity_on_chutes(store, listable_crypto["capacity_id"], execute=True)
                raise AssertionError("Chutes execute accepted without enable env")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 403
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED"] = "1"
            with tempfile.TemporaryDirectory() as bad_cli:
                os.environ["CATHEDRAL_TEE_GPU_CHUTES_CLI"] = bad_cli
                bad_cli_result = tee_gpu.list_capacity_on_chutes(
                    store, listable_crypto["capacity_id"], execute=True)
                assert bad_cli_result["status"] == "list_failed"
                assert bad_cli_result["executed"] is False
                assert "cli_error" in bad_cli_result["error"]
            assert tee_gpu._claim_chutes_listing(
                store, listable_crypto["capacity_id"], {"status": "test-claim"}) == "claimed"
            assert tee_gpu._claim_chutes_listing(
                store, listable_crypto["capacity_id"], {"status": "test-claim"}) == "listing_in_progress"
            os.environ["CATHEDRAL_TEE_GPU_LISTING_STALE_SECS"] = "60"
            def _stale_listing(conn):
                conn.execute(
                    "UPDATE tee_gpu_capacity SET chutes_status=?, updated_at_iso=? WHERE capacity_id=?",
                    ("listing", "2026-01-01T00:00:00.000Z", listable_crypto["capacity_id"]),
                )
            store.write(_stale_listing)
            assert tee_gpu._claim_chutes_listing(
                store, listable_crypto["capacity_id"], {"status": "test-claim"}) == "claimed"
            os.environ.pop("CATHEDRAL_TEE_GPU_LISTING_STALE_SECS", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_CLI", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH", None)
            try:
                tee_gpu.record_usage_receipt(
                    store,
                    evidence_pending["capacity_id"],
                    {"receipt_id": "usage-before-provider", "usage_seconds": 1},
                )
                raise AssertionError("usage receipt accepted before provider listing")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail == "provider_listing_required"
            provider_seen = tee_gpu.record_provider_status(
                store,
                evidence_pending["capacity_id"],
                {
                    "provider_status": "listed",
                    "server_id": "fixture-provider-server",
                    "server_name": "fixture-h200-0",
                    "source": "fixture-import",
                    "receipt_id": "provider-receipt-1",
                },
            )
            assert provider_seen is not None
            launch_after_provider = tee_gpu.capacity_launch_evidence(
                store, evidence_pending["capacity_id"])
            assert launch_after_provider["provider_listing_verified"] is True
            assert launch_after_provider["health_verified"] is False
            try:
                tee_gpu.record_usage_receipt(
                    store,
                    evidence_pending["capacity_id"],
                    {"receipt_id": "usage-before-health", "usage_seconds": 1},
                )
                raise AssertionError("usage receipt accepted before health receipt")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail == "health_receipt_required"
            health_seen = tee_gpu.record_health_receipt(
                store,
                evidence_pending["capacity_id"],
                {
                    "ok": True,
                    "probe_url": "http://203.0.113.15:32000/health",
                    "latency_ms": 12.5,
                    "response_digest": "sha256:fixture-health",
                },
            )
            assert health_seen is not None
            usage_seen = tee_gpu.record_usage_receipt(
                store,
                evidence_pending["capacity_id"],
                {
                    "receipt_id": "usage-receipt-1",
                    "usage_seconds": 60,
                    "revenue_usd": 0.01,
                    "workload_count": 1,
                },
            )
            assert usage_seen is not None
            launch_ready = tee_gpu.capacity_launch_evidence(store, evidence_pending["capacity_id"])
            assert launch_ready["production_compute_ready"] is True
            admin_with_launch = tee_gpu.admin_record(usage_seen, store=store)
            assert admin_with_launch["launch_evidence"]["usage_or_revenue_verified"] is True
            ready_metrics = tee_gpu.capacity_metrics(store)
            assert ready_metrics["production_ready"] >= 1
            ready_gpu_count = int(usage_seen["gpu_count"])
            assert ready_metrics["active_gpus"] >= ready_gpu_count
            stale_ready = tee_gpu.update_capacity_admin(
                store,
                evidence_pending["capacity_id"],
                {"status": "pending"},
            )
            assert stale_ready is not None
            assert tee_gpu.capacity_launch_evidence(
                store, evidence_pending["capacity_id"])["production_compute_ready"] is False
            stale_metrics = tee_gpu.capacity_metrics(store)
            assert stale_metrics["active_gpus"] == ready_metrics["active_gpus"] - ready_gpu_count
            os.environ.pop("CATHEDRAL_TEE_GPU_VERIFY_CMD", None)

            rejected = tee_gpu.review_capacity_evidence(
                store,
                evidence_pending["capacity_id"],
                {"status": "rejected", "reason": "bad fixture"},
            )
            assert rejected is not None
            rejected_preflight = json.loads(rejected["preflight_json"])
            assert rejected["preflight_status"] == "blocked"
            assert "attestation_evidence_rejected" in rejected_preflight["reasons"]
            os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE", None)

            approved = tee_gpu.update_capacity_admin(
                store,
                record["capacity_id"],
                {
                    "status": "active",
                    "admin_note": "approved",
                    "chutes_server_name": "worker-h200-0",
                    "chutes_status": "queued",
                },
            )
            assert approved is not None
            assert approved["status"] == "active"
            assert approved["admin_note"] == "approved"

            resubmitted = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-h200-0",
                    "gpu_short_ref": "h200",
                    "gpu_count": 8,
                    "hourly_cost": 2.75,
                    "agent_api": "http://203.0.113.10:32000",
                    "tee_kind": "intel_tdx",
                    "tdx_claimed": True,
                    "gpu_cc_claimed": True,
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_resubmitted",
                preserve_admin_fields=True,
            )
            assert resubmitted["status"] == "active"
            assert resubmitted["admin_note"] == "approved"
            assert resubmitted["chutes_server_name"] == "worker-h200-0"
            active_metrics = tee_gpu.capacity_metrics(store)
            assert active_metrics["active_gpus"] == 0
            assert active_metrics["active_listed_hourly_cost"] == 0.0
            assert active_metrics["admin_active_candidate_gpus"] == 8
            assert active_metrics["admin_active_candidate_hourly_cost"] == 22.0

            os.environ["CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH"] = "/tmp/chutes.hotkey"
            manifest = tee_gpu.chutes_manifest_item(resubmitted)
            assert manifest["ready"] is False
            assert manifest["command"] is None
            assert "cryptographic_attestation_required" in manifest["missing"]
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED"] = "1"
            try:
                tee_gpu.list_capacity_on_chutes(store, resubmitted["capacity_id"], execute=True)
                raise AssertionError("Chutes command emitted without cryptographic evidence")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400
                assert e.detail["detail"] == "chutes_listing_not_ready"
                assert "cryptographic_attestation_required" in e.detail["blockers"]
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH", None)
            not_ready = tee_gpu.chutes_manifest_item(resubmitted)
            assert not_ready["ready"] is False
            assert not_ready["command"] is None

            downgraded = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-h200-0",
                    "gpu_short_ref": "a100",
                    "gpu_count": 1,
                    "hourly_cost": 1.79,
                    "agent_api": "http://203.0.113.10:32000",
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_downgraded",
                preserve_admin_fields=True,
            )
            assert downgraded["preflight_status"] == "blocked"
            assert downgraded["status"] == "pending"
            assert downgraded["chutes_server_name"] == "worker-h200-0"

            try:
                tee_gpu.create_capacity(
                    store,
                    {
                        "node_id": "verify-nan",
                        "gpu_short_ref": "h200",
                        "gpu_count": 8,
                        "hourly_cost": "NaN",
                        "agent_api": "http://203.0.113.12:32000",
                        "tee_kind": "intel_tdx",
                        "tdx_claimed": True,
                        "gpu_cc_claimed": True,
                        "operator_use_authorized": True,
                    },
                    owner_hotkey="5VerifyHotkey",
                    actor="verify",
                    event_type="verify_nan_created",
                )
                raise AssertionError("NaN hourly_cost accepted")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400

            try:
                tee_gpu.create_capacity(
                    store,
                    {
                        "node_id": "verify-no-consent",
                        "gpu_short_ref": "h200",
                        "gpu_count": 8,
                        "hourly_cost": 2.75,
                        "agent_api": "http://203.0.113.13:32000",
                        "tee_kind": "intel_tdx",
                        "tdx_claimed": True,
                        "gpu_cc_claimed": True,
                    },
                    owner_hotkey="5VerifyHotkey",
                    actor="verify",
                    event_type="verify_no_consent_created",
                )
                raise AssertionError("capacity accepted without operator authorization")
            except tee_gpu.HTTPException as e:
                assert e.status_code == 400

            eval_rows = store.query("SELECT COUNT(*) AS n FROM eval_runs")[0]["n"]
            solves = store.query("SELECT COUNT(*) AS n FROM lane_challenge_solves")[0]["n"]
            pm = store.query("SELECT COUNT(*) AS n FROM per_miner_solves")[0]["n"]
            assert eval_rows == solves == pm == 0

            bad = tee_gpu.create_capacity(
                store,
                {
                    "node_id": "verify-a100-0",
                    "gpu_short_ref": "a100",
                    "gpu_count": 1,
                    "hourly_cost": 1.20,
                    "agent_api": "http://203.0.113.11:32000",
                    "operator_use_authorized": True,
                },
                owner_hotkey="5VerifyHotkey",
                actor="verify",
                event_type="verify_bad_created",
            )
            assert bad["preflight_status"] == "blocked"
            _route_smoke()
            print("tee_gpu_verify: ok")
        finally:
            store.close()
            try:
                os.unlink(db_path)
            except OSError:
                pass


def _route_smoke() -> None:
    try:
        import resource  # noqa: F401
    except Exception:
        import types

        resource = types.ModuleType("resource")
        resource.RLIMIT_CPU = 0
        resource.RLIMIT_AS = 1
        resource.setrlimit = lambda *args, **kwargs: None
        sys.modules["resource"] = resource

    try:
        from fastapi.testclient import TestClient
        from scaffold.publisher import build_app
    except Exception as e:
        raise RuntimeError(f"tee_gpu route smoke requires FastAPI TestClient: {e}") from e

    class AcceptVerifier:
        def verify(self, ss58_address: str, message: bytes, signature_b64: str) -> bool:
            return signature_b64 == "ok"

    original = tee_gpu.default_verifier
    tee_gpu.default_verifier = lambda: AcceptVerifier()
    try:
        key_hex = "11" * 32
        headers = {
            "X-Cathedral-Hotkey": "5RouteHotkey",
            "X-Cathedral-Signature": "ok",
            "X-Cathedral-Submitted-At": _now_iso(),
        }
        os.environ.pop("CATHEDRAL_TEE_GPU_ENABLED", None)
        app = build_app(database_path=":memory:", signing_key_hex=key_hex)
        with TestClient(app) as client:
            assert client.post("/v1/tee-gpu/offers", json={}, headers=headers).status_code == 404
            assert client.post(
                "/v1/tee-gpu/evidence-request",
                json={"node_id": "route-disabled-h200-0"},
                headers=headers,
            ).status_code == 404

        os.environ["CATHEDRAL_TEE_GPU_ENABLED"] = "1"
        os.environ["CATHEDRAL_TEE_GPU_ADMIN_TOKEN"] = "secret"
        os.environ["CATHEDRAL_TEE_GPU_CHUTES_HOTKEY_PATH"] = "/tmp/chutes.hotkey"
        app = build_app(database_path=":memory:", signing_key_hex=key_hex)
        with TestClient(app) as client:
            assert client.get("/v1/admin/tee-gpu/metrics").status_code == 401
            assert client.get(
                "/v1/admin/tee-gpu/metrics",
                headers={"Authorization": "Bearer bad"},
            ).status_code == 401
            assert client.get("/v1/admin/tee-gpu/dashboard").status_code == 401
            assert client.get(
                "/v1/tee-gpu/capacity",
            ).status_code == 404

            assert client.post(
                "/v1/tee-gpu/nonce",
                json={"node_id": "route-evidence-h200-0"},
                headers=headers,
            ).status_code == 404
            closed_request = client.post(
                "/v1/tee-gpu/evidence-request",
                json={"node_id": "route-evidence-h200-0", "ttl_secs": 30},
                headers=headers,
            )
            assert closed_request.status_code == 503
            assert closed_request.json()["detail"] == "tee_gpu_intake_gate_not_configured"

            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE"] = "1"
            os.environ["CATHEDRAL_TEE_GPU_INTAKE_CODE"] = "route-secret"
            blocked_request = client.post(
                "/v1/tee-gpu/evidence-request",
                json={"node_id": "route-evidence-h200-0", "ttl_secs": 30},
                headers=headers,
            )
            assert blocked_request.status_code == 403
            assert blocked_request.json()["detail"] == "invalid_tee_gpu_intake_code"
            evidence_request = client.post(
                "/v1/tee-gpu/evidence-request",
                json={
                    "node_id": "route-evidence-h200-0",
                    "ttl_secs": 30,
                    "invite_code": "route-secret",
                },
                headers=headers,
            )
            assert evidence_request.status_code == 200, evidence_request.text
            assert evidence_request.json()["ttl_secs"] == 60
            assert evidence_request.json()["status"] == "issued"
            assert "not a single-use verifier nonce" in evidence_request.json()["note"]

            offer = {
                "node_id": "route-h200-0",
                "gpu_short_ref": "h200",
                "gpu_count": 8,
                "hourly_cost": 2.75,
                "agent_api": "http://203.0.113.20:32000",
                "tee_kind": "intel_tdx",
                "tdx_claimed": True,
                "gpu_cc_claimed": True,
                "operator_use_authorized": True,
                "status": "active",
                "chutes_validator_hotkey": "attacker-validator",
                "intake_code": "route-secret",
            }
            r = client.post("/v1/tee-gpu/offers", json=offer, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["capacity"]["status"] == "pending"
            assert "preflight" in r.json()["capacity"]
            assert r.json()["capacity"]["evidence"]["status"] == "missing"
            assert r.json()["capacity"]["operator_use_authorized"] is True
            assert "chutes_status" not in r.json()["capacity"]
            bad_sig = dict(headers)
            bad_sig["X-Cathedral-Signature"] = "bad"
            assert client.post("/v1/tee-gpu/offers", json=offer, headers=bad_sig).status_code == 401

            admin_headers = {"Authorization": "Bearer secret"}
            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE"] = "1"
            spoofed = dict(offer)
            spoofed["node_id"] = "route-spoofed-review-h200-0"
            spoofed["attestation"] = {
                "cathedral_review": {"status": "operator_reviewed"},
            }
            spoofed_offer = client.post("/v1/tee-gpu/offers", json=spoofed, headers=headers)
            assert spoofed_offer.status_code == 200, spoofed_offer.text
            spoofed_id = spoofed_offer.json()["capacity"]["capacity_id"]
            assert spoofed_offer.json()["capacity"]["preflight"]["status"] == "blocked"
            assert "attestation_evidence_required" in spoofed_offer.json()["capacity"]["preflight"]["reasons"]
            assert spoofed_offer.json()["capacity"]["evidence"]["status"] == "missing"
            empty_review = client.post(
                f"/v1/admin/tee-gpu/capacity/{spoofed_id}/attestation-review",
                json={"status": "operator_reviewed", "reason": "empty evidence"},
                headers=admin_headers,
            )
            assert empty_review.status_code == 400, empty_review.text

            evidence = dict(offer)
            evidence["node_id"] = "route-evidence-h200-0"
            evidence["attestation"] = {
                "evidence_request_id": evidence_request.json()["request_id"],
                "tdx_quote_b64": "not-a-real-quote",
                "gpu_evidence_json": {"cc_mode": "claimed"},
            }
            evidence_offer = client.post("/v1/tee-gpu/offers", json=evidence, headers=headers)
            assert evidence_offer.status_code == 200, evidence_offer.text
            evidence_id = evidence_offer.json()["capacity"]["capacity_id"]
            assert evidence_offer.json()["capacity"]["preflight"]["status"] == "blocked"
            assert "attestation_evidence_review_required" in evidence_offer.json()["capacity"]["preflight"]["reasons"]
            assert evidence_offer.json()["capacity"]["evidence"]["status"] == "submitted"
            reviewed = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/attestation-review",
                json={"status": "operator_reviewed", "reason": "manual fixture review"},
                headers=admin_headers,
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["capacity"]["preflight"]["status"] == "eligible"
            assert reviewed.json()["capacity"]["evidence"]["status"] == "operator_reviewed"
            assert reviewed.json()["capacity"]["evidence"]["acceptable"] is True
            os.environ["CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE"] = "1"
            crypto_recheck = client.patch(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}",
                json={"admin_note": "crypto required"},
                headers=admin_headers,
            )
            assert crypto_recheck.status_code == 200, crypto_recheck.text
            assert crypto_recheck.json()["capacity"]["preflight"]["status"] == "blocked"
            assert "cryptographic_attestation_required" in crypto_recheck.json()["capacity"]["preflight"]["reasons"]
            missing_verifier = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/verify-evidence",
                json={},
                headers=admin_headers,
            )
            assert missing_verifier.status_code == 503, missing_verifier.text
            with tempfile.TemporaryDirectory() as bad_verifier:
                os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = bad_verifier
                bad_verifier_resp = client.post(
                    f"/v1/admin/tee-gpu/capacity/{evidence_id}/verify-evidence",
                    json={},
                    headers=admin_headers,
                )
                assert bad_verifier_resp.status_code == 503, bad_verifier_resp.text
                assert bad_verifier_resp.json()["detail"]["detail"] == "tee_gpu_evidence_verifier_error"
            fixture_verifier = _write_fixture_verifier(ok=True)
            os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = f"{sys.executable} {fixture_verifier}"
            verified = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/verify-evidence",
                json={},
                headers=admin_headers,
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["capacity"]["preflight"]["status"] == "eligible"
            assert verified.json()["capacity"]["evidence"]["status"] == "cryptographically_verified"
            assert verified.json()["capacity"]["evidence"]["acceptable"] is True
            assert verified.json()["capacity"]["evidence"]["cryptographic_proof"] is True
            route_usage_too_early = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/usage-receipt",
                json={"receipt_id": "route-usage-too-early", "usage_seconds": 1},
                headers=admin_headers,
            )
            assert route_usage_too_early.status_code == 400, route_usage_too_early.text
            assert "provider_listing_required" in route_usage_too_early.text
            route_provider = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/provider-status",
                json={
                    "provider_status": "listed",
                    "server_id": "route-provider-server",
                    "server_name": "route-h200-provider-0",
                    "receipt_id": "route-provider-receipt",
                },
                headers=admin_headers,
            )
            assert route_provider.status_code == 200, route_provider.text
            assert route_provider.json()["capacity"]["launch_evidence"]["provider_listing_verified"] is True
            route_health = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/health-receipt",
                json={
                    "ok": True,
                    "probe_url": "http://203.0.113.20:32000/health",
                    "response_digest": "sha256:route-health",
                    "latency_ms": 9,
                },
                headers=admin_headers,
            )
            assert route_health.status_code == 200, route_health.text
            assert route_health.json()["capacity"]["launch_evidence"]["health_verified"] is True
            route_usage = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/usage-receipt",
                json={
                    "receipt_id": "route-usage-receipt",
                    "usage_seconds": 30,
                    "revenue_usd": 0.01,
                    "workload_count": 1,
                },
                headers=admin_headers,
            )
            assert route_usage.status_code == 200, route_usage.text
            assert route_usage.json()["capacity"]["launch_evidence"]["production_compute_ready"] is True
            os.environ.pop("CATHEDRAL_TEE_GPU_VERIFY_CMD", None)
            rejected_review = client.post(
                f"/v1/admin/tee-gpu/capacity/{evidence_id}/attestation-review",
                json={"status": "rejected", "reason": "bad fixture"},
                headers=admin_headers,
            )
            assert rejected_review.status_code == 200, rejected_review.text
            assert rejected_review.json()["capacity"]["preflight"]["status"] == "blocked"
            assert "attestation_evidence_rejected" in rejected_review.json()["capacity"]["preflight"]["reasons"]
            os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE", None)

            blocked = dict(offer)
            blocked["node_id"] = "route-a100-0"
            blocked["gpu_short_ref"] = "a100"
            blocked_offer = client.post("/v1/tee-gpu/offers", json=blocked, headers=headers)
            assert blocked_offer.status_code == 200
            blocked_id = blocked_offer.json()["capacity"]["capacity_id"]
            blocked_ready = client.patch(
                f"/v1/admin/tee-gpu/capacity/{blocked_id}",
                json={"chutes_server_name": "worker-route-a100-0"},
                headers=admin_headers,
            )
            assert blocked_ready.status_code == 200, blocked_ready.text
            blocked_manifest = client.get(
                "/v1/admin/tee-gpu/chutes-manifest?status=pending&include_blocked=true",
                headers=admin_headers,
            ).json()
            blocked_items = [
                item for item in blocked_manifest["items"]
                if item["capacity_id"] == blocked_id
            ]
            assert len(blocked_items) == 1
            assert blocked_items[0]["ready"] is False
            assert blocked_items[0]["command"] is None
            assert "preflight_not_eligible" in blocked_items[0]["missing"]
            blocked_list = client.post(
                f"/v1/admin/tee-gpu/capacity/{blocked_id}/chutes-list",
                json={"allow_blocked": True},
                headers=admin_headers,
            )
            assert blocked_list.status_code == 400, blocked_list.text
            assert "preflight_not_eligible" in blocked_list.text

            no_consent = dict(offer)
            no_consent.pop("operator_use_authorized")
            no_consent["node_id"] = "route-no-consent"
            assert client.post("/v1/tee-gpu/offers", json=no_consent, headers=headers).status_code == 400

            created = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5AdminHotkey",
                    "node_id": "route-admin-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-h200-0",
                },
                headers=admin_headers,
            )
            assert created.status_code == 200, created.text
            assert created.json()["capacity"]["status"] == "active"
            cap_id = created.json()["capacity"]["capacity_id"]

            reprice_seed = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5RouteHotkey",
                    "node_id": "route-reprice-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-reprice-h200-0",
                    "chutes_status": "listed",
                },
                headers=admin_headers,
            )
            assert reprice_seed.status_code == 200, reprice_seed.text
            repriced_offer = dict(offer)
            repriced_offer["node_id"] = "route-reprice-h200-0"
            repriced_offer["hourly_cost"] = 20.0
            repriced = client.post("/v1/tee-gpu/offers", json=repriced_offer, headers=headers)
            assert repriced.status_code == 200, repriced.text
            assert repriced.json()["capacity"]["status"] == "pending"
            repriced_id = repriced.json()["capacity"]["capacity_id"]
            repriced_admin = client.get(
                "/v1/admin/tee-gpu/capacity?status=pending",
                headers=admin_headers,
            ).json()
            repriced_rows = [
                item for item in repriced_admin["items"]
                if item["capacity_id"] == repriced_id
            ]
            assert len(repriced_rows) == 1
            assert repriced_rows[0]["chutes_status"] == "needs_relisting"
            repriced_manifest = client.get(
                "/v1/admin/tee-gpu/chutes-manifest?status=pending&include_blocked=true",
                headers=admin_headers,
            ).json()
            repriced_items = [
                item for item in repriced_manifest["items"]
                if item["capacity_id"] == repriced_id
            ]
            assert len(repriced_items) == 1
            assert repriced_items[0]["ready"] is False
            assert repriced_items[0]["command"] is None
            assert "status_not_listable" in repriced_items[0]["missing"]
            repriced_list = client.post(
                f"/v1/admin/tee-gpu/capacity/{repriced_id}/chutes-list",
                json={},
                headers=admin_headers,
            )
            assert repriced_list.status_code == 400, repriced_list.text
            assert "status_not_listable" in repriced_list.text

            paused_seed = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5RouteHotkey",
                    "node_id": "route-paused-reprice-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-paused-reprice-h200-0",
                    "chutes_status": "listed",
                },
                headers=admin_headers,
            )
            assert paused_seed.status_code == 200, paused_seed.text
            paused_id = paused_seed.json()["capacity"]["capacity_id"]
            paused = client.patch(
                f"/v1/admin/tee-gpu/capacity/{paused_id}",
                json={"status": "paused"},
                headers=admin_headers,
            )
            assert paused.status_code == 200, paused.text
            paused_offer = dict(offer)
            paused_offer["node_id"] = "route-paused-reprice-h200-0"
            paused_offer["agent_api"] = "http://203.0.113.21:32000"
            paused_changed = client.post("/v1/tee-gpu/offers", json=paused_offer, headers=headers)
            assert paused_changed.status_code == 200, paused_changed.text
            assert paused_changed.json()["capacity"]["status"] == "pending"
            paused_admin = client.get(
                "/v1/admin/tee-gpu/capacity?status=pending",
                headers=admin_headers,
            ).json()
            paused_rows = [
                item for item in paused_admin["items"]
                if item["capacity_id"] == paused_id
            ]
            assert len(paused_rows) == 1
            assert paused_rows[0]["chutes_status"] == "needs_relisting"

            patch_seed = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5AdminHotkey",
                    "node_id": "route-admin-patch-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-admin-patch-h200-0",
                    "chutes_status": "listed",
                },
                headers=admin_headers,
            )
            assert patch_seed.status_code == 200, patch_seed.text
            patch_id = patch_seed.json()["capacity"]["capacity_id"]
            patch_changed = client.patch(
                f"/v1/admin/tee-gpu/capacity/{patch_id}",
                json={"hourly_cost": 3.5},
                headers=admin_headers,
            )
            assert patch_changed.status_code == 200, patch_changed.text
            assert patch_changed.json()["capacity"]["status"] == "pending"
            assert patch_changed.json()["capacity"]["chutes_status"] == "needs_relisting"

            rejected = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5AdminHotkey",
                    "node_id": "route-rejected-h200-0",
                    "status": "rejected",
                    "chutes_server_name": "worker-route-rejected-h200-0",
                },
                headers=admin_headers,
            )
            assert rejected.status_code == 200, rejected.text
            rejected_id = rejected.json()["capacity"]["capacity_id"]
            rejected_manifest = client.get(
                "/v1/admin/tee-gpu/chutes-manifest?status=rejected&include_blocked=true",
                headers=admin_headers,
            ).json()
            rejected_items = [
                item for item in rejected_manifest["items"]
                if item["capacity_id"] == rejected_id
            ]
            assert len(rejected_items) == 1
            assert rejected_items[0]["ready"] is False
            assert rejected_items[0]["command"] is None
            assert "status_not_listable" in rejected_items[0]["missing"]
            rejected_list = client.post(
                f"/v1/admin/tee-gpu/capacity/{rejected_id}/chutes-list",
                json={},
                headers=admin_headers,
            )
            assert rejected_list.status_code == 400, rejected_list.text
            assert "status_not_listable" in rejected_list.text

            already = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5AdminHotkey",
                    "node_id": "route-already-listed-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-already-listed-h200-0",
                    "chutes_status": "listed",
                },
                headers=admin_headers,
            )
            assert already.status_code == 200, already.text
            already_id = already.json()["capacity"]["capacity_id"]
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED"] = "1"
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_CLI"] = "definitely-not-installed-cathedral-test-cli"
            already_listed = client.post(
                f"/v1/admin/tee-gpu/capacity/{already_id}/chutes-list",
                json={"execute": True},
                headers=admin_headers,
            )
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_CLI", None)
            assert already_listed.status_code == 400, already_listed.text
            assert already_listed.json()["detail"]["detail"] == "chutes_listing_not_ready"
            assert "cryptographic_attestation_required" in already_listed.json()["detail"]["blockers"]

            metrics = client.get("/v1/admin/tee-gpu/metrics", headers=admin_headers).json()
            assert metrics["emissions_eligible"] is False
            assert metrics["active_gpus"] == 0
            assert metrics["active_listed_hourly_cost"] == 0.0
            assert metrics["admin_active_candidate_gpus"] == 16
            assert metrics["admin_active_candidate_hourly_cost"] == 44.0

            demote = client.post(
                "/v1/admin/tee-gpu/capacity",
                json={
                    **offer,
                    "owner_hotkey": "5AdminHotkey",
                    "node_id": "route-admin-demote-h200-0",
                    "status": "active",
                    "chutes_server_name": "worker-route-demote-h200-0",
                },
                headers=admin_headers,
            )
            assert demote.status_code == 200, demote.text
            demote_id = demote.json()["capacity"]["capacity_id"]
            revoked = client.patch(
                f"/v1/admin/tee-gpu/capacity/{demote_id}",
                json={"operator_use_authorized": False},
                headers=admin_headers,
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["capacity"]["status"] == "pending"
            assert revoked.json()["capacity"]["preflight"]["status"] == "blocked"
            assert "operator_use_not_authorized" in revoked.json()["capacity"]["preflight"]["reasons"]

            manifest = client.get(
                "/v1/admin/tee-gpu/chutes-manifest?status=active",
                headers=admin_headers,
            ).json()
            manifest_items = [
                item for item in manifest["items"]
                if item["capacity_id"] == cap_id
            ]
            assert len(manifest_items) == 1
            assert manifest_items[0]["ready"] is False
            assert manifest_items[0]["command"] is None
            assert "cryptographic_attestation_required" in manifest_items[0]["missing"]
            dashboard = client.get("/v1/admin/tee-gpu/dashboard", headers=admin_headers)
            assert dashboard.status_code == 200, dashboard.text
            assert dashboard.headers["cache-control"] == "no-store"
            assert "List compute with Cathedral." in dashboard.text
            assert "offer -> review -> Chutes handoff" in dashboard.text
            assert "cryptographic_attestation_required" in dashboard.text
            assert "chutes-miner add-node --name worker-route-h200-0" not in dashboard.text
            assert "preflight_not_eligible" in dashboard.text
            assert "chutes-miner add-node --name worker-route-a100-0" not in dashboard.text
            assert "chutes-miner add-node --name worker-route-reprice-h200-0" not in dashboard.text
            assert "chutes-miner add-node --name worker-route-paused-reprice-h200-0" not in dashboard.text
            assert "chutes-miner add-node --name worker-route-admin-patch-h200-0" not in dashboard.text
            assert "chutes-miner add-node --name worker-route-rejected-h200-0" not in dashboard.text
            dry = client.post(
                f"/v1/admin/tee-gpu/capacity/{cap_id}/chutes-list",
                json={},
                headers=admin_headers,
            )
            assert dry.status_code == 400, dry.text
            assert dry.json()["detail"]["detail"] == "chutes_listing_not_ready"
            assert "cryptographic_attestation_required" in dry.json()["detail"]["blockers"]
            blocked_exec = client.post(
                f"/v1/admin/tee-gpu/capacity/{cap_id}/chutes-list",
                json={"execute": True},
                headers=admin_headers,
            )
            assert blocked_exec.status_code == 400
            assert "cryptographic_attestation_required" in blocked_exec.json()["detail"]["blockers"]
            pending_manifest = client.get(
                "/v1/admin/tee-gpu/chutes-manifest?status=pending",
                headers=admin_headers,
            ).json()
            pending_ids = {item["capacity_id"] for item in pending_manifest["items"]}
            assert pending_manifest["omitted_blocked"] >= 2
            assert blocked_id not in pending_ids
            assert demote_id not in pending_ids
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED"] = "1"
            os.environ["CATHEDRAL_TEE_GPU_CHUTES_CLI"] = "definitely-not-installed-cathedral-test-cli"
            for changed_id in (repriced_id, paused_id, patch_id):
                reapproved = client.patch(
                    f"/v1/admin/tee-gpu/capacity/{changed_id}",
                    json={"status": "active"},
                    headers=admin_headers,
                )
                assert reapproved.status_code == 200, reapproved.text
                assert reapproved.json()["capacity"]["status"] == "active"
                assert reapproved.json()["capacity"]["chutes_status"] == "needs_relisting"
                if changed_id == repriced_id:
                    second_offer = dict(offer)
                    second_offer["node_id"] = "route-reprice-h200-0"
                    second_offer["hourly_cost"] = 21.0
                    second = client.post("/v1/tee-gpu/offers", json=second_offer, headers=headers)
                    assert second.status_code == 200, second.text
                    assert second.json()["capacity"]["status"] == "pending"
                    second_admin = client.get(
                        "/v1/admin/tee-gpu/capacity?status=pending",
                        headers=admin_headers,
                    ).json()
                    second_rows = [
                        item for item in second_admin["items"]
                        if item["capacity_id"] == repriced_id
                    ]
                    assert len(second_rows) == 1
                    assert second_rows[0]["chutes_status"] == "needs_relisting"
                    reapproved = client.patch(
                        f"/v1/admin/tee-gpu/capacity/{repriced_id}",
                        json={"status": "active"},
                        headers=admin_headers,
                    )
                    assert reapproved.status_code == 200, reapproved.text
                    assert reapproved.json()["capacity"]["chutes_status"] == "needs_relisting"
                relist = client.post(
                    f"/v1/admin/tee-gpu/capacity/{changed_id}/chutes-list",
                    json={"execute": True},
                    headers=admin_headers,
                )
                assert relist.status_code == 400, relist.text
                assert relist.json()["detail"]["detail"] == "chutes_listing_not_ready"
                assert "cryptographic_attestation_required" in relist.json()["detail"]["blockers"]
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_EXECUTE_ENABLED", None)
            os.environ.pop("CATHEDRAL_TEE_GPU_CHUTES_CLI", None)
            os.environ["CATHEDRAL_TEE_GPU_PUBLIC_CATALOG_ENABLED"] = "1"
            assert client.get("/v1/tee-gpu/capacity").status_code == 200
    finally:
        tee_gpu.default_verifier = original


if __name__ == "__main__":
    main()
