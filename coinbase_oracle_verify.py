"""Focused gate for the canonical coinbase conservation SAT oracle."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import sys
import tempfile

from game.arena.coinbase_encoder_agent import SCHEMA_PACKET, run_encoder_agent
from scaffold.dimacs import verify_witness
from scaffold.lanes.coinbase_oracle import (
    CANONICAL_INVARIANT_ID,
    attestation_report_data,
    build_coinbase_challenge,
    run_childkey_split,
    verify_coinbase_sat_assignment,
    verify_coinbase_unsat_proof,
)
from scaffold.lanes.verifiable_sat_pipeline import verify_coinbase_pipeline
from scaffold.publisher.solver_artifacts import (
    SOLVER_ARTIFACT_SCHEMA,
    SolverArtifact,
    verify_solver_artifact,
)
from scaffold.publisher.auth import canonical_claim_bytes, sha256_hex
from scaffold.solve_real import solve_cnf_real


def ck(label: str, cond: bool, failures: list[str]) -> None:
    if not cond:
        failures.append(label)


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def solver_claim_hash(artifact: SolverArtifact) -> str:
    return sha256_hex(json.dumps(
        artifact.hashes(),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ))


def real_sat_assignment(cnf_text: str, label: str, failures: list[str]) -> list[int] | None:
    result = solve_cnf_real(cnf_text, timeout=30)
    if result.get("status") == "UNAVAILABLE":
        ck(f"{label}: real solver is installed", False, failures)
        return None
    ck(f"{label}: real solver returns SAT", result.get("status") == "SAT", failures)
    model = result.get("model") if result.get("status") == "SAT" else None
    return list(model or []) if isinstance(model, list) else None


def run_publisher_smoke(failures: list[str]) -> None:
    from bittensor_wallet import Keypair
    from fastapi.testclient import TestClient

    from scaffold.publisher.app import (
        _VERIFIABLE_SAT_CARD,
        _empty_bundle_hash,
        build_app,
    )
    from scaffold.publisher.keys import generate_test_key

    old_enabled = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_ENABLED")
    old_vsat_attest = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_REQUIRE_ATTESTATION")
    old_vsat_report_only = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_ALLOW_REPORT_DATA_ONLY")
    old_vsat_replay = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY")
    old_vsat_replay_cmd = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_REPLAY_CMD")
    old_vsat_replay_runners = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_REPLAY_ALLOWED_RUNNERS")
    old_vsat_digests = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_AGENT_IMAGE_DIGESTS")
    old_min_width = os.environ.get("CATHEDRAL_VERIFIABLE_SAT_MIN_PAYMENT_WIDTH")
    old_attest = os.environ.get("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION")
    tmpdir = tempfile.TemporaryDirectory()
    try:
        replay_script = os.path.join(tmpdir.name, "shadow_clone_replay.py")
        with open(replay_script, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys\n"
                "req = json.load(sys.stdin)\n"
                "ch = req['challenge']\n"
                "receipt = {\n"
                "  'schema_version': 'cathedral.subtensor_clone_replay_receipt.v1',\n"
                "  'runner_kind': 'subtensor_clone_shadow_v1',\n"
                "  'target_commit': 'shadow-fixture-commit',\n"
                "  'invariant_id': req['invariant_id'],\n"
                "  'source_target': req['source_target'],\n"
                "  'challenge_artifact_sha256': ch['artifact_sha256'],\n"
                "  'cnf_sha256': ch['cnf_sha256'],\n"
                "  'decode_map_sha256': ch['decode_map_sha256'],\n"
                "  'clause_source_map_sha256': ch['clause_source_map_sha256'],\n"
                "  'decoded': req['decoded'],\n"
                "  'observed': req['observed'],\n"
                "  'accepted': True,\n"
                "  'invariant_broken': True,\n"
                "}\n"
                "print(json.dumps(receipt, sort_keys=True))\n"
            )
        os.environ["CATHEDRAL_VERIFIABLE_SAT_ENABLED"] = "1"
        os.environ["CATHEDRAL_VERIFIABLE_SAT_REQUIRE_ATTESTATION"] = "1"
        # Shadow-only smoke path: this proves report_data binding and payment
        # plumbing without a live TDX quote. Production must leave this unset
        # and configure a real DCAP verifier plus a real clone replay runner.
        os.environ["CATHEDRAL_VERIFIABLE_SAT_ALLOW_REPORT_DATA_ONLY"] = "1"
        os.environ["CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY"] = "1"
        os.environ["CATHEDRAL_VERIFIABLE_SAT_REPLAY_ALLOWED_RUNNERS"] = "subtensor_clone_shadow_v1"
        os.environ["CATHEDRAL_VERIFIABLE_SAT_REPLAY_CMD"] = f"{sys.executable} {replay_script}"
        os.environ["CATHEDRAL_VERIFIABLE_SAT_MIN_PAYMENT_WIDTH"] = "5"
        allowed_digest = "sha256:" + "c" * 64
        os.environ["CATHEDRAL_VERIFIABLE_SAT_AGENT_IMAGE_DIGESTS"] = allowed_digest
        os.environ.pop("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION", None)
        app = build_app(database_path=":memory:", signing_key_hex=generate_test_key())
        keypair = Keypair.create_from_uri("//CoinbaseOracleSmoke")
        agent_id = "hermes-coinbase-encoder-v1"

        def issue_headers(width: int, *, agent_id_value: str = agent_id) -> dict[str, str]:
            issue_at = now_iso()
            issue_basis = {
                "ckb_enabled": True,
                "width": width,
                "agent_image_digest": allowed_digest,
                "agent_id": agent_id_value,
            }
            issue_digest = sha256_hex(json.dumps(issue_basis, sort_keys=True, separators=(",", ":")))
            issue_msg = canonical_claim_bytes(
                bundle_hash=_empty_bundle_hash(),
                card_id=_VERIFIABLE_SAT_CARD,
                miner_hotkey=keypair.ss58_address,
                submitted_at=issue_at,
                challenge_id=f"issue:{issue_digest}",
                dimacs_solution_sha256=issue_digest,
            )
            return {
                "X-Cathedral-Hotkey": keypair.ss58_address,
                "X-Cathedral-Submitted-At": issue_at,
                "X-Cathedral-Signature": base64.b64encode(keypair.sign(issue_msg)).decode("ascii"),
            }

        with TestClient(app) as client:
            status = client.get("/v1/verifiable-sat/coinbase/status")
            ck("publisher exposes verifiable SAT status", status.status_code == 200 and status.json()["enabled"], failures)
            bad_agent = client.get(
                "/v1/verifiable-sat/coinbase/challenge",
                params={
                    "ckb_enabled": "true",
                    "width": "5",
                    "agent_image_digest": allowed_digest,
                    "agent_id": "hermes-coinbase-encoder-v1-copy",
                },
                headers=issue_headers(5, agent_id_value="hermes-coinbase-encoder-v1-copy"),
            )
            ck("publisher rejects unallowlisted verifiable SAT agent_id", bad_agent.status_code == 403, failures)
            challenge_response = client.get(
                "/v1/verifiable-sat/coinbase/challenge",
                params={"ckb_enabled": "true", "width": "5", "agent_image_digest": allowed_digest},
                headers=issue_headers(5),
            )
            ck("publisher emits coinbase challenge", challenge_response.status_code == 200, failures)
            challenge_json = challenge_response.json() if challenge_response.status_code == 200 else {}
            body_base = {
                "ckb_enabled": True,
                "width": 5,
                "agent_image_digest": allowed_digest,
                "agent_id": agent_id,
                "work_nonce": str(challenge_json.get("work_nonce") or ""),
            }
            challenge = build_coinbase_challenge(
                ckb_enabled=True,
                width=5,
                agent_image_digest=body_base["agent_image_digest"],
                agent_id=body_base["agent_id"],
                work_nonce=body_base["work_nonce"],
            )
            assignment = real_sat_assignment(challenge.cnf_text, "publisher smoke challenge", failures)
            ck("publisher smoke challenge is solvable", assignment is not None, failures)
            if assignment is None:
                return
            artifact = SolverArtifact(
                outcome="SAT",
                dimacs_solution="s SATISFIABLE\nv " + " ".join(str(lit) for lit in assignment) + " 0\n",
            )
            submitted_at = now_iso()
            msg = canonical_claim_bytes(
                bundle_hash=_empty_bundle_hash(),
                card_id=_VERIFIABLE_SAT_CARD,
                miner_hotkey=keypair.ss58_address,
                submitted_at=submitted_at,
                challenge_id=challenge.artifact_sha256,
                dimacs_solution_sha256=solver_claim_hash(artifact),
            )
            headers = {
                "X-Cathedral-Hotkey": keypair.ss58_address,
                "X-Cathedral-Submitted-At": submitted_at,
                "X-Cathedral-Signature": base64.b64encode(keypair.sign(msg)).decode("ascii"),
            }
            body = {
                **body_base,
                "tdx_report_data_hex": attestation_report_data(
                    challenge,
                    miner_hotkey=keypair.ss58_address,
                    solver_artifact_hash=solver_claim_hash(artifact),
                ),
                "solver_artifact": {
                    "schema_version": SOLVER_ARTIFACT_SCHEMA,
                    "outcome": "SAT",
                    "dimacs_solution": artifact.dimacs_solution,
                },
            }
            missing_attest = dict(body)
            missing_attest.pop("tdx_report_data_hex", None)
            rejected = client.post("/v1/verifiable-sat/coinbase/verify", json=missing_attest, headers=headers)
            rejected_json = rejected.json() if rejected.status_code == 200 else {}
            ck(
                "publisher rejects missing TDX report_data when required",
                rejected.status_code == 200 and rejected_json.get("accepted") is False,
                failures,
            )
            verify = client.post("/v1/verifiable-sat/coinbase/verify", json=body, headers=headers)
            ck("publisher verifies signed solver artifact", verify.status_code == 200 and verify.json()["accepted"], failures)
            submit = client.post("/v1/verifiable-sat/coinbase/submit", json=body, headers=headers)
            submit_json = submit.json() if submit.status_code == 200 else {}
            ck(
                "publisher submit emits payment row",
                submit.status_code == 200
                and submit_json.get("accepted") is True
                and submit_json.get("payment_row_emitted") is True,
                failures,
            )
            ck(
                "publisher payment metadata shows attestation required",
                bool(submit_json.get("payment", {}).get("attestation_required")) is True,
                failures,
            )
            ck(
                "publisher payment metadata marks report-data-only as shadow",
                bool(submit_json.get("payment", {}).get("attestation_report_data_only")) is True,
                failures,
            )
            ck(
                "publisher payment metadata shows system replay required",
                bool(submit_json.get("payment", {}).get("system_replay_required")) is True
                and bool(submit_json.get("payment", {}).get("system_replay_configured")) is True,
                failures,
            )
            ck(
                "publisher submit gates on clone replay receipt",
                bool(submit_json.get("gates", {}).get("system_replay_verified")) is True,
                failures,
            )
            duplicate_submit = client.post("/v1/verifiable-sat/coinbase/submit", json=body, headers=headers)
            ck(
                "publisher consumes paid verifiable SAT challenge after first payment",
                duplicate_submit.status_code == 409,
                failures,
            )
            attested_rows = app.state.store.query(
                "SELECT attested FROM eval_runs WHERE id=?",
                (submit_json.get("eval_run_id", ""),),
            )
            ck(
                "publisher does not mark report-data-only shadow rows as attested",
                bool(attested_rows) and int(attested_rows[0]["attested"]) == 0,
                failures,
            )
            tiny_base = dict(body_base)
            tiny_base["width"] = 4
            tiny_challenge_response = client.get(
                "/v1/verifiable-sat/coinbase/challenge",
                params={"ckb_enabled": "true", "width": "4", "agent_image_digest": allowed_digest},
                headers=issue_headers(4),
            )
            tiny_json_issued = tiny_challenge_response.json() if tiny_challenge_response.status_code == 200 else {}
            tiny_base["work_nonce"] = str(tiny_json_issued.get("work_nonce") or "")
            tiny_challenge = build_coinbase_challenge(
                ckb_enabled=True,
                width=4,
                agent_image_digest=tiny_base["agent_image_digest"],
                agent_id=tiny_base["agent_id"],
                work_nonce=tiny_base["work_nonce"],
            )
            tiny_assignment = real_sat_assignment(tiny_challenge.cnf_text, "tiny publisher smoke challenge", failures)
            ck("tiny publisher smoke challenge is solvable", tiny_assignment is not None, failures)
            if tiny_assignment is not None:
                tiny_artifact = SolverArtifact(
                    outcome="SAT",
                    dimacs_solution="s SATISFIABLE\nv " + " ".join(str(lit) for lit in tiny_assignment) + " 0\n",
                )
                tiny_submitted_at = now_iso()
                tiny_msg = canonical_claim_bytes(
                    bundle_hash=_empty_bundle_hash(),
                    card_id=_VERIFIABLE_SAT_CARD,
                    miner_hotkey=keypair.ss58_address,
                    submitted_at=tiny_submitted_at,
                    challenge_id=tiny_challenge.artifact_sha256,
                    dimacs_solution_sha256=solver_claim_hash(tiny_artifact),
                )
                tiny_headers = {
                    "X-Cathedral-Hotkey": keypair.ss58_address,
                    "X-Cathedral-Submitted-At": tiny_submitted_at,
                    "X-Cathedral-Signature": base64.b64encode(keypair.sign(tiny_msg)).decode("ascii"),
                }
                tiny_body = {
                    **tiny_base,
                    "tdx_report_data_hex": attestation_report_data(
                        tiny_challenge,
                        miner_hotkey=keypair.ss58_address,
                        solver_artifact_hash=solver_claim_hash(tiny_artifact),
                    ),
                    "solver_artifact": {
                        "schema_version": SOLVER_ARTIFACT_SCHEMA,
                        "outcome": "SAT",
                        "dimacs_solution": tiny_artifact.dimacs_solution,
                    },
                }
                tiny_submit = client.post("/v1/verifiable-sat/coinbase/submit", json=tiny_body, headers=tiny_headers)
                tiny_json = tiny_submit.json() if tiny_submit.status_code == 200 else {}
                ck(
                    "publisher verifies but does not pay below min width",
                    tiny_submit.status_code == 200
                    and tiny_json.get("accepted") is True
                    and tiny_json.get("payment_row_emitted") is False,
                    failures,
                )
            weights = client.get("/v1/validator/weights/next")
            weights_json = weights.json() if weights.status_code == 200 else {}
            serialized = json.dumps(weights_json, sort_keys=True, default=str)
            ck(
                "report-data-only verifiable SAT row does not reach signed weights",
                weights.status_code == 200 and keypair.ss58_address not in serialized,
                failures,
            )
    finally:
        if old_enabled is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_ENABLED", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_ENABLED"] = old_enabled
        if old_vsat_attest is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_REQUIRE_ATTESTATION", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_REQUIRE_ATTESTATION"] = old_vsat_attest
        if old_vsat_report_only is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_ALLOW_REPORT_DATA_ONLY", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_ALLOW_REPORT_DATA_ONLY"] = old_vsat_report_only
        if old_vsat_replay is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_REQUIRE_SYSTEM_REPLAY"] = old_vsat_replay
        if old_vsat_replay_cmd is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_REPLAY_CMD", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_REPLAY_CMD"] = old_vsat_replay_cmd
        if old_vsat_replay_runners is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_REPLAY_ALLOWED_RUNNERS", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_REPLAY_ALLOWED_RUNNERS"] = old_vsat_replay_runners
        if old_vsat_digests is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_AGENT_IMAGE_DIGESTS", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_AGENT_IMAGE_DIGESTS"] = old_vsat_digests
        if old_min_width is None:
            os.environ.pop("CATHEDRAL_VERIFIABLE_SAT_MIN_PAYMENT_WIDTH", None)
        else:
            os.environ["CATHEDRAL_VERIFIABLE_SAT_MIN_PAYMENT_WIDTH"] = old_min_width
        if old_attest is None:
            os.environ.pop("CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION", None)
        else:
            os.environ["CATHEDRAL_AUDIT_SCANNER_REQUIRE_ATTESTATION"] = old_attest
        tmpdir.cleanup()


def main() -> int:
    failures: list[str] = []

    sat = build_coinbase_challenge(
        ckb_enabled=True,
        width=4,
        agent_image_digest="sha256:" + "a" * 64,
        work_nonce="coinbase-oracle-smoke",
    )
    ck("SAT challenge uses canonical invariant", sat.invariant_id == CANONICAL_INVARIANT_ID, failures)
    ck("SAT challenge emits DIMACS", sat.cnf_text.startswith("c schema:"), failures)
    ck("SAT challenge emits clause-source map", bool(sat.clause_source_map["sections"]), failures)
    ck("SAT challenge emits decode map", "parent_emission" in sat.decode_map["fields"], failures)
    ck("SAT attestation report_data binds 64 bytes", len(attestation_report_data(sat)) == 128, failures)
    agent_out = run_encoder_agent({
        "schema_version": SCHEMA_PACKET,
        "agent_id": "hermes-coinbase-encoder-v1",
        "agent_image_digest": "sha256:" + "a" * 64,
        "work_nonce": "coinbase-oracle-smoke",
        "ckb_enabled": True,
        "width": 4,
    })
    ck("Hermes encoder agent emits matching artifact",
       agent_out["artifact_sha256"] == sat.artifact_sha256
       and agent_out["tdx_report_data_hex"] == attestation_report_data(sat),
       failures)

    assignment = real_sat_assignment(sat.cnf_text, "CKBurn>0 oracle", failures)
    ck("CKBurn>0 oracle is SAT", assignment is not None, failures)
    if assignment is not None:
        ck("SAT assignment satisfies CNF", verify_witness(sat.cnf_text, assignment), failures)
        artifact = SolverArtifact(
            outcome="SAT",
            dimacs_solution="s SATISFIABLE\nv " + " ".join(str(lit) for lit in assignment) + " 0\n",
        )
        artifact_check = verify_solver_artifact(sat.cnf_text, artifact)
        ck("solver artifact accepts SAT assignment", artifact_check.ok, failures)
        ck("solver artifact carries hashes", bool(artifact_check.artifact_hashes["dimacs_solution_sha256"]), failures)
        verdict = verify_coinbase_sat_assignment(sat, assignment)
        ck("SAT assignment replays as real conservation break", verdict.ok, failures)
        ck("SAT replay reports positive excess", int(verdict.observed.get("excess", 0)) > 0, failures)
        pipeline = verify_coinbase_pipeline(sat, artifact)
        ck("pipeline accepts SAT artifact after real replay", pipeline.accepted and pipeline.rewardable, failures)
        pipeline_missing_clone = verify_coinbase_pipeline(
            sat,
            artifact,
            require_system_replay=True,
            system_replay_command="",
            allowed_replay_runner_kinds={"subtensor_clone_rust_v1"},
        )
        ck("pipeline rejects SAT payout without system clone replay", not pipeline_missing_clone.accepted, failures)
        pipeline_attested = verify_coinbase_pipeline(
            sat,
            artifact,
            require_attestation=True,
            observed_report_data_hex=attestation_report_data(sat),
        )
        ck("pipeline accepts correctly bound attestation", pipeline_attested.accepted, failures)
        pipeline_bad_attest = verify_coinbase_pipeline(
            sat,
            artifact,
            require_attestation=True,
            observed_report_data_hex="00" * 64,
        )
        ck("pipeline rejects mismatched attestation binding", not pipeline_bad_attest.accepted, failures)
    real_sat = solve_cnf_real(sat.cnf_text, timeout=30)
    if real_sat["status"] == "UNAVAILABLE":
        ck("real SAT solver is installed for oracle gate", False, failures)
    else:
        ck("real solver returns SAT for CKBurn>0 oracle", real_sat.get("status") == "SAT", failures)
        if real_sat.get("status") == "SAT":
            real_sat_verdict = verify_coinbase_sat_assignment(sat, real_sat.get("model", []))
            ck("real solver SAT model replays as conservation break", real_sat_verdict.ok, failures)

    safe = build_coinbase_challenge(
        ckb_enabled=False,
        width=4,
        agent_image_digest="sha256:" + "b" * 64,
        work_nonce="coinbase-oracle-safe",
    )
    safe_quick = solve_cnf_real(safe.cnf_text, timeout=30)
    ck("CKBurn=0 oracle is UNSAT by real solver", safe_quick.get("status") == "UNSAT", failures)

    real_safe = run_childkey_split(
        validating_emission=15,
        parent_factor=15,
        ck_burn_rate=0,
        child_take_rate=2,
        width=4,
    )
    ck("CKBurn=0 real replay conserves at boundary", real_safe["violation"] is False, failures)
    real_bug = run_childkey_split(
        validating_emission=15,
        parent_factor=15,
        ck_burn_rate=15,
        child_take_rate=2,
        width=4,
    )
    ck("saturating-sub underflow bug replays", real_bug["violation"] is True and real_bug["excess"] == 2, failures)

    # This is the launch-critical external proof gate: the generated safe CNF
    # must be solved UNSAT by a real solver, and the returned DRAT proof must be
    # accepted by drat-trim. No shape-only or exit-code-only proof checks.
    real_unsat = solve_cnf_real(safe.cnf_text, want_drat=True, timeout=30)
    if real_unsat["status"] == "UNAVAILABLE":
        ck("real UNSAT proof solver is installed for oracle gate", False, failures)
    else:
        ck("real solver returns UNSAT for CKBurn=0 oracle", real_unsat.get("status") == "UNSAT", failures)
        drat_text = str(real_unsat.get("drat") or "")
        ck("real UNSAT solver emits DRAT proof", bool(drat_text.strip()), failures)
        unsat = verify_coinbase_unsat_proof(safe, drat_text)
        ck("drat-trim accepts generated CKBurn=0 proof", unsat.ok and not unsat.stub, failures)
        bogus = verify_coinbase_unsat_proof(safe, "0\n")
        ck("drat-trim rejects bogus UNSAT proof", not bogus.ok and not bogus.stub, failures)

    unsat_artifact = verify_solver_artifact(safe.cnf_text, {
        "schema_version": SOLVER_ARTIFACT_SCHEMA,
        "outcome": "UNSAT",
        "drat_proof": "",
    })
    ck("UNSAT artifact without proof is rejected", not unsat_artifact.ok, failures)

    run_publisher_smoke(failures)

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1
    print("PASS coinbase oracle checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
