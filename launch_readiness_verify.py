"""Smoke checks for Cathedral launch-readiness scorecard.

Run:
  python launch_readiness_verify.py
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from scaffold import launch_readiness as lr
from scaffold.publisher.store import Store
from scaffold.publisher import refill, tee_gpu


checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


@contextmanager
def _clean_env():
    keys = [
        "DATABASE_URL",
        "CATHEDRAL_TEE_GPU_ENABLED",
        "CATHEDRAL_TEE_GPU_REQUIRE_EVIDENCE",
        "CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE",
        "CATHEDRAL_TEE_GPU_VERIFY_CMD",
        "CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS",
        "CATHEDRAL_TEE_GPU_VERIFY_TIMEOUT_SECS",
        "CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE",
        "CATHEDRAL_TEE_GPU_INTAKE_CODE",
        "CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST",
        "CATHEDRAL_REFILL_ENABLED",
        "CATHEDRAL_REFILL_INTERVAL_SECONDS",
        "CATHEDRAL_REFILL_MAX_MINTS",
        "CATHEDRAL_PREGEN_QUEUE_SIZE",
        "CATHEDRAL_REFILL_TARGET_T1",
        "CATHEDRAL_REFILL_TARGET_T2",
        "CATHEDRAL_REFILL_METHOD_T1",
        "CATHEDRAL_REFILL_METHOD_T2",
        "CATHEDRAL_REFILL_NVARS_T2",
        "CATHEDRAL_REFILL_NCLAUSES_T2",
        "CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_DISTINCT_SOLVERS",
        "CATHEDRAL_OPEN_WINDOW_RETIRE_AFTER_SECONDS",
    ]
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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


all_gate_ids = {gate.id for gate in lr.gates()}
local = lr.local_scaffold_evidence()
local_report = lr.evaluate(local)

ck("launch scorecard totals exactly 100 points", lr.total_points() == 100.0)
ck("local evidence names only known gates", set(local) <= all_gate_ids)
ck("local scaffold is not falsely marked 100% launch-ready", local_report["ready"] is False)
ck("local scaffold score reflects remaining real-world blockers", local_report["score"] == 91.0)
with _clean_env():
    generator = refill.generator_config()
    generator_tiers = {int(t["tier"]): t for t in generator["tiers"]}
    ck("SAT refill generator is wired with launch tier policy",
       generator["kind"] == "local_refill"
       and generator_tiers[1]["method"] == "biased"
       and generator_tiers[2]["method"] == "ajm"
       and generator_tiers[1]["target_active"] == 25
       and generator_tiers[2]["target_active"] == 25)
    ck("SAT refill generator has enough burst supply for live miners",
       generator["interval_seconds"] <= 20
       and generator["mint_cap"] >= 4
       and generator["pregen_queue_size"] >= 8
       and generator["retire_after_distinct_solvers"] >= 256)
    ck("external SAT generator is disabled by default",
       generator["external"]["enabled"] is False
       and generator["external"]["requested"] is False
       and generator["external"]["configured"] is False
       and generator["external"]["local_fallback"] is False)
    os.environ["CATHEDRAL_SAT_GENERATOR_ENABLED"] = "true"
    partial_generator = refill.generator_config()
    ck("external SAT generator partial config is visible and not silently live",
       partial_generator["external"]["requested"] is True
       and partial_generator["external"]["enabled"] is False
       and partial_generator["external"]["configured"] is False
       and partial_generator["external"]["configuration_error"] == "missing_url_or_token")
    partial_store = Store(":memory:")
    try:
        partial_failed_closed = False
        try:
            refill.refill_tier(partial_store, 1, seed_input="partial-generator-config")
        except RuntimeError as exc:
            partial_failed_closed = "missing_url_or_token" in str(exc)
        ck("external SAT generator partial config fails closed before local mint",
           partial_failed_closed
           and partial_store.query("SELECT COUNT(*) AS n FROM lane_challenges")[0]["n"] == 0)
    finally:
        partial_store.close()
    os.environ["CATHEDRAL_SAT_GENERATOR_ENABLED"] = "true"
    os.environ["CATHEDRAL_SAT_GENERATOR_URL"] = "https://generator.example"
    os.environ["CATHEDRAL_SAT_GENERATOR_TOKEN"] = "test-token"
    os.environ["CATHEDRAL_SAT_GENERATOR_MAX_MINTS"] = "40"
    external_generator = refill.generator_config()
    ck("external SAT generator mode reports explicit lease source",
       external_generator["kind"] == "external_sat_generator"
       and external_generator["external"]["enabled"] is True
       and external_generator["external"]["configured"] is True
       and external_generator["mint_cap"] == 40)
    os.environ.pop("CATHEDRAL_SAT_GENERATOR_ENABLED", None)
    os.environ.pop("CATHEDRAL_SAT_GENERATOR_URL", None)
    os.environ.pop("CATHEDRAL_SAT_GENERATOR_TOKEN", None)
    os.environ.pop("CATHEDRAL_SAT_GENERATOR_MAX_MINTS", None)
    os.environ["CATHEDRAL_REFILL_ENABLED"] = "true"
    ck("SAT refill generator launch env enables mint loop",
       refill.generator_config()["enabled"] is True)

    gate_status = tee_gpu.intake_gate_status()
    ck("compute intake gate defaults to fail-closed until configured",
       gate_status["required"] is True
       and gate_status["configured"] is False
       and gate_status["accepting_invited_miners"] is False)
    try:
        tee_gpu.require_miner_intake_gate("5LaunchHotkey", {})
        fail_closed = False
    except tee_gpu.HTTPException as e:
        fail_closed = e.status_code == 503 and e.detail == "tee_gpu_intake_gate_not_configured"
    ck("unconfigured compute intake rejects public miner entry", fail_closed)
ck(
    "real hardware/provider/revenue gates remain blockers",
    {
        "compute_real_verifier_tested",
        "compute_provider_listing_verified",
        "compute_health_and_revenue_verified",
    } <= {b["id"] for b in local_report["blockers"]},
)
controlled_report = lr.evaluate(local, profile="controlled-v0")
ck("controlled v0 profile is ready while deferring real compute gates",
   controlled_report["ready"] is True
   and controlled_report["score"] == 91.0
   and {
       "compute_real_verifier_tested",
       "compute_provider_listing_verified",
       "compute_health_and_revenue_verified",
   } == {g["id"] for g in controlled_report["deferred"]})
no_hardware_report = lr.evaluate(local, profile="no-hardware-v0")
ck("no-hardware v0 aliases to controlled intake readiness",
   no_hardware_report["profile"] == "controlled-v0"
   and no_hardware_report["ready"] is True
   and {g["id"] for g in no_hardware_report["deferred"]}
   == {g["id"] for g in controlled_report["deferred"]})

complete = {gate.id: True for gate in lr.gates()}
complete_report = lr.evaluate(complete)
ck("all gate evidence reaches 100%", complete_report["score"] == 100.0)
ck("all gate evidence is launch-ready", complete_report["ready"] is True)

missing_one = dict(complete)
missing_one["signed_weight_vector_verified"] = False
missing_report = lr.evaluate(missing_one)
ck("one missing blocker prevents readiness", missing_report["ready"] is False)
ck("missing blocker is surfaced with needed evidence",
   missing_report["blockers"][0]["id"] == "signed_weight_vector_verified"
   and "needed_evidence" in missing_report["blockers"][0])

report_script = Path(__file__).with_name("launch_readiness_report.py")
report_run = subprocess.run(
    [sys.executable, str(report_script), "--show-gates"],
    capture_output=True,
    text=True,
    check=False,
)
ck("readiness report prints evidence caveats",
   report_run.returncode == 0 and "Evidence caveats:" in report_run.stdout)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
    f.write(
        '{"gates":{"compute_real_verifier_tested":"false",'
        '"compute_provider_listing_verified":"false",'
        '"compute_health_and_revenue_verified":"false"}}'
    )
    string_evidence_path = f.name
string_evidence_run = subprocess.run(
    [
        sys.executable,
        str(report_script),
        "--evidence-json",
        string_evidence_path,
        "--require-ready",
    ],
    capture_output=True,
    text=True,
    check=False,
)
ck("readiness report rejects stringly boolean evidence JSON",
   string_evidence_run.returncode != 0
   and "must be a JSON boolean" in (string_evidence_run.stderr + string_evidence_run.stdout))
require_ready_run = subprocess.run(
    [sys.executable, str(report_script), "--require-ready"],
    capture_output=True,
    text=True,
    check=False,
)
ck("readiness report refuses local scaffold under --require-ready",
   require_ready_run.returncode != 0 and "NOT READY" in require_ready_run.stdout)
controlled_require_ready_run = subprocess.run(
    [sys.executable, str(report_script), "--profile", "controlled-v0", "--require-ready"],
    capture_output=True,
    text=True,
    check=False,
)
ck("readiness report allows controlled-v0 profile without real compute proof",
   controlled_require_ready_run.returncode == 0
   and "READY" in controlled_require_ready_run.stdout
   and "Deferred gates:" in controlled_require_ready_run.stdout)
no_hardware_require_ready_run = subprocess.run(
    [sys.executable, str(report_script), "--profile", "no-hardware-v0", "--require-ready"],
    capture_output=True,
    text=True,
    check=False,
)
ck("readiness report allows explicit no-hardware-v0 profile",
   no_hardware_require_ready_run.returncode == 0
   and "Profile: controlled-v0" in no_hardware_require_ready_run.stdout
   and "Secure Compute is gated live intake plus operator review only" in no_hardware_require_ready_run.stdout)

with _clean_env():
    os.environ["CATHEDRAL_TEE_GPU_ENABLED"] = "1"
    os.environ["CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE"] = "1"
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = Store(db_path)
    request = tee_gpu.create_evidence_request(
        store,
        owner_hotkey="5LaunchHotkey",
        node_id="launch-h200-0",
        actor="verify",
        ttl_secs=60,
    )
    capacity = tee_gpu.create_capacity(
        store,
        {
            "node_id": "launch-h200-0",
            "gpu_short_ref": "h200",
            "gpu_count": 8,
            "hourly_cost": 2.75,
            "agent_api": "http://203.0.113.55:32000",
            "tee_kind": "intel_tdx",
            "tdx_claimed": True,
            "gpu_cc_claimed": True,
            "operator_use_authorized": True,
            "attestation": {
                "evidence_request_id": request["request_id"],
                "tdx_quote_b64": "fixture-quote",
                "gpu_evidence_json": {"cc_mode": "on"},
            },
        },
        owner_hotkey="5LaunchHotkey",
        actor="verify",
        event_type="launch_fixture_created",
    )
    negative_verifier_cmd = f"{sys.executable} {_write_fixture_verifier(ok=True, missing_checks=True)}"
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = negative_verifier_cmd
    try:
        tee_gpu.verify_capacity_evidence(store, capacity["capacity_id"])
        raise AssertionError("missing-check verifier unexpectedly passed")
    except tee_gpu.HTTPException:
        pass
    success_verifier_cmd = f"{sys.executable} {_write_fixture_verifier(ok=True)}"
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = success_verifier_cmd
    tee_gpu.verify_capacity_evidence(store, capacity["capacity_id"])
    tee_gpu.record_provider_status(
        store,
        capacity["capacity_id"],
        {
            "provider_status": "listed",
            "server_id": "provider-server-1",
            "server_name": "launch-h200-0",
        },
    )
    tee_gpu.record_health_receipt(
        store,
        capacity["capacity_id"],
        {"ok": True, "response_digest": "sha256:fixture-health"},
    )
    tee_gpu.record_usage_receipt(
        store,
        capacity["capacity_id"],
        {"receipt_id": "usage-1", "usage_seconds": 120},
    )
    runtime = lr.tee_gpu_runtime_evidence(store)
    store_report = lr.evaluate(lr.evidence_from_store(store))
    ck("fixture verifier is not real launch evidence unless allowlisted",
       runtime["compute_real_verifier_tested"] is False
       and store_report["ready"] is False)
    ck("TEE runtime ignores provider listing before verifier digest is allowlisted",
       runtime["compute_provider_listing_verified"] is False)
    ck("TEE runtime ignores health plus usage before verifier digest is allowlisted",
       runtime["compute_health_and_revenue_verified"] is False)
    verifier_digests = [
        hashlib.sha256(negative_verifier_cmd.encode("utf-8")).hexdigest(),
        hashlib.sha256(success_verifier_cmd.encode("utf-8")).hexdigest(),
    ]
    os.environ["CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS"] = ",".join(verifier_digests)
    allowlisted_runtime = lr.tee_gpu_runtime_evidence(store)
    allowlisted_report = lr.evaluate(lr.evidence_from_store(store))
    ck("allowlisted verifier digest completes real verifier gate",
       allowlisted_runtime["compute_real_verifier_tested"] is True)
    ck("allowlisted verifier digest unlocks trusted provider listing",
       allowlisted_runtime["compute_provider_listing_verified"] is True)
    ck("allowlisted verifier digest unlocks trusted health plus usage",
       allowlisted_runtime["compute_health_and_revenue_verified"] is True)
    ck("fully evidenced allowlisted publisher DB reaches 100%",
       allowlisted_report["score"] == 100.0)
    ck("fully evidenced allowlisted publisher DB is launch-ready",
       allowlisted_report["ready"] is True)

with _clean_env():
    os.environ["CATHEDRAL_TEE_GPU_ENABLED"] = "1"
    os.environ["CATHEDRAL_TEE_GPU_REQUIRE_CRYPTO_EVIDENCE"] = "1"
    split_store = Store(":memory:")
    trusted_request = tee_gpu.create_evidence_request(
        split_store,
        owner_hotkey="5TrustedVerifierHotkey",
        node_id="trusted-verifier-only",
        actor="verify",
        ttl_secs=60,
    )
    trusted_capacity = tee_gpu.create_capacity(
        split_store,
        {
            "node_id": "trusted-verifier-only",
            "gpu_short_ref": "h200",
            "gpu_count": 8,
            "hourly_cost": 2.75,
            "agent_api": "http://203.0.113.56:32000",
            "tee_kind": "intel_tdx",
            "tdx_claimed": True,
            "gpu_cc_claimed": True,
            "operator_use_authorized": True,
            "attestation": {
                "evidence_request_id": trusted_request["request_id"],
                "tdx_quote_b64": "trusted-fixture-quote",
                "gpu_evidence_json": {"cc_mode": "on"},
            },
        },
        owner_hotkey="5TrustedVerifierHotkey",
        actor="verify",
        event_type="launch_split_trusted_created",
    )
    trusted_negative_cmd = f"{sys.executable} {_write_fixture_verifier(ok=True, missing_checks=True)}"
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = trusted_negative_cmd
    try:
        tee_gpu.verify_capacity_evidence(split_store, trusted_capacity["capacity_id"])
        raise AssertionError("trusted negative verifier unexpectedly passed")
    except tee_gpu.HTTPException:
        pass
    trusted_success_cmd = f"{sys.executable} {_write_fixture_verifier(ok=True)}"
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = trusted_success_cmd
    tee_gpu.verify_capacity_evidence(split_store, trusted_capacity["capacity_id"])

    untrusted_request = tee_gpu.create_evidence_request(
        split_store,
        owner_hotkey="5UntrustedProviderHotkey",
        node_id="untrusted-provider-ready",
        actor="verify",
        ttl_secs=60,
    )
    untrusted_capacity = tee_gpu.create_capacity(
        split_store,
        {
            "node_id": "untrusted-provider-ready",
            "gpu_short_ref": "h200",
            "gpu_count": 8,
            "hourly_cost": 2.75,
            "agent_api": "http://203.0.113.57:32000",
            "tee_kind": "intel_tdx",
            "tdx_claimed": True,
            "gpu_cc_claimed": True,
            "operator_use_authorized": True,
            "attestation": {
                "evidence_request_id": untrusted_request["request_id"],
                "tdx_quote_b64": "untrusted-fixture-quote",
                "gpu_evidence_json": {"cc_mode": "on"},
            },
        },
        owner_hotkey="5UntrustedProviderHotkey",
        actor="verify",
        event_type="launch_split_untrusted_created",
    )
    untrusted_success_cmd = f"{sys.executable} {_write_fixture_verifier(ok=True)}"
    os.environ["CATHEDRAL_TEE_GPU_VERIFY_CMD"] = untrusted_success_cmd
    tee_gpu.verify_capacity_evidence(split_store, untrusted_capacity["capacity_id"])
    tee_gpu.record_provider_status(
        split_store,
        untrusted_capacity["capacity_id"],
        {
            "provider_status": "listed",
            "server_id": "provider-server-untrusted",
            "server_name": "untrusted-provider-ready",
        },
    )
    tee_gpu.record_health_receipt(
        split_store,
        untrusted_capacity["capacity_id"],
        {"ok": True, "response_digest": "sha256:untrusted-health"},
    )
    tee_gpu.record_usage_receipt(
        split_store,
        untrusted_capacity["capacity_id"],
        {"receipt_id": "usage-untrusted", "usage_seconds": 120},
    )
    os.environ["CATHEDRAL_TEE_GPU_REAL_VERIFIER_DIGESTS"] = ",".join([
        hashlib.sha256(trusted_negative_cmd.encode("utf-8")).hexdigest(),
        hashlib.sha256(trusted_success_cmd.encode("utf-8")).hexdigest(),
    ])
    split_runtime = lr.tee_gpu_runtime_evidence(split_store)
    split_report = lr.evaluate(lr.evidence_from_store(split_store))
    ck("full readiness does not combine trusted verifier with untrusted provider row",
       split_runtime["compute_real_verifier_tested"] is True
       and split_runtime["compute_provider_listing_verified"] is False
       and split_runtime["compute_health_and_revenue_verified"] is False
       and split_report["ready"] is False)


failed = [name for name, ok in checks if not ok]
if failed:
    print("\nFAILED:")
    for name in failed:
        print(f" - {name}")
    sys.exit(1)
print(f"\nOK: {len(checks)} checks")
