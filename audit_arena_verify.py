"""Smoke checks for Audit Arena v0.

Run:

    python3 audit_arena_verify.py

On this Windows host, `python` may be the Microsoft Store alias. Use WSL
`python3`, `py` if installed, or the Codex bundled Python path.

This stays fully offline. It proves the vertical slice:

    DIMACS witness -> CNF verification -> witness decode -> deterministic replay
    -> private distillation trace
"""
from __future__ import annotations

from scaffold.lanes.audit_arena import (
    AUDIT_SUBMISSION_SCHEMA_VERSION,
    AuditTarget,
    AuditTask,
    MinerAuditSubmission,
    ReplayEvidence,
    decode_witness,
    fixedpoint_fee_silent_zero_replay,
    sha256_text,
    verify_and_replay,
)
from scaffold.lanes.subtensor_replay import (
    SUBTENSOR_REPLAY_SCHEMA_VERSION,
    SubtensorReplayPackage,
    make_subtensor_replay_adapter,
)


checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def main() -> None:
    print("AUDIT ARENA V0 - deterministic witness replay")

    target = AuditTarget(
        target_id="subtensor-amm",
        repo_url="https://github.com/opentensor/subtensor",
        commit="pinned-test-commit",
        netuid=0,
        validator_entrypoint="pallets/swap/src/swap_step.rs",
        scoring_entrypoint="recalc fee path",
    )

    # Tiny SAT CNF for the smoke gate. The audit witness is pre-decoded because
    # the current audit-hunter map files use that shape for known-answer cases.
    cnf = "p cnf 1 1\n1 0\n"
    task = AuditTask(
        task_id="audit-subtensor-amm-b2",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="amount>0 and fee_rate>0 must not floor to zero fee",
        challenge_id="audit-smoke-cnf",
        cnf_sha256=sha256_text(cnf),
        decode_map={
            "allow_static_witness": True,
            "witness": {"amount": 1, "fee_rate": 49152},
        },
        severity_hint="dust",
        replay_kind="corpus_smoke",
        source={"audit_hunter_cnf_id": "subtensor-amm__B2-fee-silent-zero__16f16__full"},
    )
    submission = MinerAuditSubmission(
        task_id=task.task_id,
        miner_hotkey="5AuditMiner",
        dimacs_solution="s SATISFIABLE\nv 1 0\n",
        agent_trace={"hypotheses_tested": 3},
    )
    verdict = verify_and_replay(
        task,
        submission,
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("valid SAT witness is accepted after deterministic replay", verdict.accepted)
    ck("decoded witness carries audit inputs",
       verdict.decoded_witness == {"amount": 1, "fee_rate": 49152})
    ck("replay artifact computes silent zero fee",
       verdict.replay is not None and verdict.replay.artifacts["computed_fee"] == 0)
    ck("distillation trace labels reproduced witness",
       verdict.distillation_trace["label"] == "reproduced_witness")
    ck("distillation trace is private by default", verdict.distillation_trace["private"] is True)
    ck("live earning policy defaults to shadow replay",
       verdict.distillation_trace["supervision"]["live_earning_policy"] == "shadow_replay_only")
    ck("trace hash is present", len(verdict.distillation_trace["trace_hash"]) == 64)
    ck("trace includes replay adapter identity",
       "replay_adapter_sha256" in verdict.replay.artifacts)
    ck("trace includes replay source provenance",
       "replay_code_sha256" in verdict.replay.artifacts)
    ck("trace includes decode map and audit package binding",
       "decode_map_sha256" in verdict.distillation_trace["task"]
       and "audit_package_sha256" in verdict.distillation_trace["task"])
    ck("submission schema is explicit in the private trace",
       verdict.distillation_trace["submission"]["schema_version"]
       == AUDIT_SUBMISSION_SCHEMA_VERSION)
    provenance = verdict.distillation_trace["replay_provenance"]
    ck("deterministic replay provenance binds code and challenge package",
       provenance["replay_code_sha256"] == verdict.replay.artifacts["replay_code_sha256"]
       and provenance["audit_package_sha256"]
       == verdict.distillation_trace["task"]["audit_package_sha256"]
       and provenance["target_commit"] == target.commit)

    static_without_opt_in = AuditTask(
        task_id="audit-static-blocked",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="static smoke witness must be explicitly allowed",
        challenge_id="audit-smoke-cnf",
        cnf_sha256=sha256_text(cnf),
        decode_map={"witness": {"amount": 1, "fee_rate": 49152}},
    )
    blocked_static = verify_and_replay(
        static_without_opt_in,
        MinerAuditSubmission(
            task_id=static_without_opt_in.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 0\n",
        ),
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("static decoded witnesses are blocked unless explicitly marked as smoke/corpus",
       not blocked_static.accepted
       and blocked_static.rejection_reason == "static_witness_decode_requires_allow_static_witness")

    decode_inputs_only = AuditTask(
        task_id="audit-decode-inputs-only",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="decode_inputs labels alone are not enough to replay",
        challenge_id="audit-smoke-cnf",
        cnf_sha256=sha256_text(cnf),
        decode_map={"decode_inputs": ["amount", "fee_rate"]},
    )
    blocked_decode_inputs = verify_and_replay(
        decode_inputs_only,
        MinerAuditSubmission(
            task_id=decode_inputs_only.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 0\n",
        ),
        cnf_text=cnf,
        replay_fn=lambda _decoded, _task: ReplayEvidence(reproduced=True),
    )
    ck("decode_inputs-only maps cannot produce accepted replay witnesses",
       not blocked_decode_inputs.accepted
       and blocked_decode_inputs.rejection_reason == "decode_map_missing_witness_or_fields")

    prod_cnf = "p cnf 2 2\n1 0\n2 0\n"
    prod_decode_map = {
        "required_fields": ["amount", "fee_rate"],
        "fields": {
            "amount": {"bits": [1]},
            "fee_rate": {"bits": [2]},
        },
    }
    prod_task = AuditTask(
        task_id="audit-production-bit-projection",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="production replay inputs must be SAT-bound bit projections",
        challenge_id="audit-prod-cnf",
        cnf_sha256=sha256_text(prod_cnf),
        decode_map=prod_decode_map,
    )
    prod_submission = MinerAuditSubmission(
        task_id=prod_task.task_id,
        miner_hotkey="5AuditMiner",
        dimacs_solution="s SATISFIABLE\nv 1 2 0\n",
    )
    prod_verdict = verify_and_replay(
        prod_task,
        prod_submission,
        cnf_text=prod_cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("production bit-projection maps with required replay fields are accepted",
       prod_verdict.accepted
       and prod_verdict.decoded_witness == {"amount": 1, "fee_rate": 1})

    prod_repeat = verify_and_replay(
        prod_task,
        prod_submission,
        cnf_text=prod_cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("trace hash is stable for identical deterministic replay",
       prod_verdict.distillation_trace["trace_hash"]
       == prod_repeat.distillation_trace["trace_hash"])

    prod_reordered_task = AuditTask(
        task_id=prod_task.task_id,
        target=target,
        invariant_id=prod_task.invariant_id,
        invariant=prod_task.invariant,
        challenge_id=prod_task.challenge_id,
        cnf_sha256=prod_task.cnf_sha256,
        decode_map={
            "fields": {
                "fee_rate": {"bits": [2]},
                "amount": {"bits": [1]},
            },
            "required_fields": ["amount", "fee_rate"],
        },
    )
    prod_reordered = verify_and_replay(
        prod_reordered_task,
        prod_submission,
        cnf_text=prod_cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("trace hash is stable across decode-map key order",
       prod_verdict.distillation_trace["trace_hash"]
       == prod_reordered.distillation_trace["trace_hash"])

    subtensor_package = SubtensorReplayPackage(
        schema_version=SUBTENSOR_REPLAY_SCHEMA_VERSION,
        target_commit=target.commit,
        runtime_sha256="a" * 64,
        clone_state_sha256="b" * 64,
        clone_block=2764,
        clone_state_root="0x" + "c" * 64,
        script_sha256="d" * 64,
        script_steps=[
            {
                "call": "Subtensor.synthetic_transfer",
                "args": {"amount": 1, "fee_rate": 1},
            }
        ],
        invariant_id="INV-SUBTENSOR-CONSERVATION",
        expected_witness={"amount": 1, "fee_rate": 1},
        checks=[
            {
                "id": "total-issuance-conserved",
                "kind": "numeric_delta",
                "before_path": "before.total_issuance",
                "after_path": "after.total_issuance",
                "operator": "delta_eq",
                "expected_delta": 0,
            }
        ],
        artifact_sha256={"replay_script": "d" * 64},
    )
    subtensor_task = AuditTask(
        task_id="audit-subtensor-clone-shadow",
        target=target,
        invariant_id="INV-SUBTENSOR-CONSERVATION",
        invariant="total issuance must be conserved across the replayed transaction sequence",
        challenge_id="audit-subtensor-clone-cnf",
        cnf_sha256=sha256_text(prod_cnf),
        decode_map=prod_decode_map,
        replay_kind="subtensor_clone_shadow",
        source={"subtensor_replay_package_sha256": subtensor_package.sha256()},
    )
    subtensor_submission = MinerAuditSubmission(
        task_id=subtensor_task.task_id,
        miner_hotkey="5AuditMiner",
        dimacs_solution="s SATISFIABLE\nv 1 2 0\n",
    )
    subtensor_observed = {
        "runtime_sha256": "a" * 64,
        "clone_state_sha256": "b" * 64,
        "script_sha256": "d" * 64,
        "artifact_sha256": {"replay_script": "d" * 64},
        "before": {"total_issuance": 100.0},
        "after": {"total_issuance": 101.0},
    }
    subtensor_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor clone shadow replay accepts SAT-bound invariant break",
       subtensor_replay.accepted
       and subtensor_replay.stage == "accepted"
       and subtensor_replay.distillation_trace["label"] == "reproduced_witness")
    ck("Subtensor clone replay artifacts bind package and target commit",
       subtensor_replay.replay is not None
       and len(subtensor_replay.replay.artifacts["subtensor_replay_package_sha256"]) == 64
       and subtensor_replay.replay.artifacts["target_commit"] == target.commit)

    bad_schema_package = SubtensorReplayPackage(
        schema_version="cathedral.subtensor_replay.v0",
        target_commit=target.commit,
        runtime_sha256="a" * 64,
        clone_state_sha256="b" * 64,
        script_sha256="d" * 64,
        script_steps=subtensor_package.script_steps,
        invariant_id=subtensor_package.invariant_id,
        expected_witness=subtensor_package.expected_witness,
        checks=subtensor_package.checks,
    )
    bad_schema_task = AuditTask(
        task_id=subtensor_task.task_id,
        target=target,
        invariant_id=subtensor_task.invariant_id,
        invariant=subtensor_task.invariant,
        challenge_id=subtensor_task.challenge_id,
        cnf_sha256=subtensor_task.cnf_sha256,
        decode_map=subtensor_task.decode_map,
        replay_kind=subtensor_task.replay_kind,
        source={"subtensor_replay_package_sha256": bad_schema_package.sha256()},
    )
    bad_schema_replay = verify_and_replay(
        bad_schema_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            bad_schema_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor replay rejects wrong package schema",
       not bad_schema_replay.accepted
       and bad_schema_replay.stage == "replay"
       and bad_schema_replay.rejection_reason == "subtensor_replay_schema_mismatch")

    bad_hash_observed = dict(subtensor_observed)
    bad_hash_observed["script_sha256"] = "e" * 64
    bad_hash_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=bad_hash_observed,
        ),
    )
    ck("Subtensor replay rejects observed script hash mismatch",
       not bad_hash_replay.accepted
       and bad_hash_replay.rejection_reason == "script_sha256_mismatch")

    unpinned_task = AuditTask(
        task_id=subtensor_task.task_id,
        target=target,
        invariant_id=subtensor_task.invariant_id,
        invariant=subtensor_task.invariant,
        challenge_id=subtensor_task.challenge_id,
        cnf_sha256=subtensor_task.cnf_sha256,
        decode_map=subtensor_task.decode_map,
        replay_kind=subtensor_task.replay_kind,
        source={},
    )
    unpinned_replay = verify_and_replay(
        unpinned_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor replay rejects packages not pinned by the task",
       not unpinned_replay.accepted
       and unpinned_replay.rejection_reason == "subtensor_replay_package_unpinned")

    pin_mismatch_task = AuditTask(
        task_id=subtensor_task.task_id,
        target=target,
        invariant_id=subtensor_task.invariant_id,
        invariant=subtensor_task.invariant,
        challenge_id=subtensor_task.challenge_id,
        cnf_sha256=subtensor_task.cnf_sha256,
        decode_map=subtensor_task.decode_map,
        replay_kind=subtensor_task.replay_kind,
        source={"subtensor_replay_package_sha256": "f" * 64},
    )
    pin_mismatch_replay = verify_and_replay(
        pin_mismatch_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor replay rejects package hash not pinned to task source",
       not pin_mismatch_replay.accepted
       and pin_mismatch_replay.rejection_reason
       == "subtensor_replay_package_sha256_mismatch")

    bad_artifact_observed = dict(subtensor_observed)
    bad_artifact_observed["artifact_sha256"] = {"replay_script": "f" * 64}
    bad_artifact_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=bad_artifact_observed,
        ),
    )
    ck("Subtensor replay rejects observed artifact hash mismatch",
       not bad_artifact_replay.accepted
       and bad_artifact_replay.rejection_reason == "artifact_sha256_mismatch:replay_script")

    no_break_observed = dict(subtensor_observed)
    no_break_observed["after"] = {"total_issuance": 100.0}
    no_break_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=no_break_observed,
        ),
    )
    ck("Subtensor replay rejects valid execution when invariant does not break",
       not no_break_replay.accepted
       and no_break_replay.rejection_reason == "invariant_not_violated"
       and no_break_replay.replay is not None
       and no_break_replay.replay.reproduced is False)

    mismatch_package = SubtensorReplayPackage(
        schema_version=SUBTENSOR_REPLAY_SCHEMA_VERSION,
        target_commit=target.commit,
        runtime_sha256="a" * 64,
        clone_state_sha256="b" * 64,
        script_sha256="d" * 64,
        script_steps=subtensor_package.script_steps,
        invariant_id=subtensor_package.invariant_id,
        expected_witness={"amount": 2, "fee_rate": 1},
        checks=subtensor_package.checks,
    )
    mismatch_task = AuditTask(
        task_id=subtensor_task.task_id,
        target=target,
        invariant_id=subtensor_task.invariant_id,
        invariant=subtensor_task.invariant,
        challenge_id=subtensor_task.challenge_id,
        cnf_sha256=subtensor_task.cnf_sha256,
        decode_map=subtensor_task.decode_map,
        replay_kind=subtensor_task.replay_kind,
        source={"subtensor_replay_package_sha256": mismatch_package.sha256()},
    )
    mismatch_replay = verify_and_replay(
        mismatch_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            mismatch_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor replay rejects package decoupled from decoded SAT witness",
       not mismatch_replay.accepted
       and mismatch_replay.rejection_reason == "witness_mismatch")

    missing_observation_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(subtensor_package),
    )
    ck("Subtensor replay rejects missing observation instead of executing implicitly",
       not missing_observation_replay.accepted
       and missing_observation_replay.rejection_reason == "subtensor_replay_missing_observation")

    no_required_check_package = SubtensorReplayPackage(
        schema_version=SUBTENSOR_REPLAY_SCHEMA_VERSION,
        target_commit=target.commit,
        runtime_sha256="a" * 64,
        clone_state_sha256="b" * 64,
        script_sha256="d" * 64,
        script_steps=subtensor_package.script_steps,
        invariant_id=subtensor_package.invariant_id,
        expected_witness=subtensor_package.expected_witness,
        checks=[
            {
                "id": "optional-observation",
                "kind": "numeric_delta",
                "before_path": "before.total_issuance",
                "after_path": "after.total_issuance",
                "operator": "delta_eq",
                "expected_delta": 0,
                "required": False,
            }
        ],
    )
    no_required_check_task = AuditTask(
        task_id=subtensor_task.task_id,
        target=target,
        invariant_id=subtensor_task.invariant_id,
        invariant=subtensor_task.invariant,
        challenge_id=subtensor_task.challenge_id,
        cnf_sha256=subtensor_task.cnf_sha256,
        decode_map=subtensor_task.decode_map,
        replay_kind=subtensor_task.replay_kind,
        source={"subtensor_replay_package_sha256": no_required_check_package.sha256()},
    )
    no_required_check_replay = verify_and_replay(
        no_required_check_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            no_required_check_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor replay rejects packages with no required invariant check",
       not no_required_check_replay.accepted
       and no_required_check_replay.rejection_reason == "invariant_required_check_missing")

    bad_number_observed = dict(subtensor_observed)
    bad_number_observed["after"] = {"total_issuance": "not-a-number"}
    bad_number_replay = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=bad_number_observed,
        ),
    )
    ck("Subtensor replay rejects non-numeric invariant observations cleanly",
       not bad_number_replay.accepted
       and bad_number_replay.rejection_reason == "invariant_number_must_be_finite")

    subtensor_replay_repeat = verify_and_replay(
        subtensor_task,
        subtensor_submission,
        cnf_text=prod_cnf,
        replay_fn=make_subtensor_replay_adapter(
            subtensor_package,
            observed_result=subtensor_observed,
        ),
    )
    ck("Subtensor clone shadow replay trace hash is deterministic",
       subtensor_replay.distillation_trace["trace_hash"]
       == subtensor_replay_repeat.distillation_trace["trace_hash"])

    sparse_prod_task = AuditTask(
        task_id="audit-sparse-production-map",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="production replay maps must declare their required fields",
        challenge_id="audit-prod-cnf",
        cnf_sha256=sha256_text(prod_cnf),
        decode_map={"fields": {"amount": {"bits": [1]}}},
    )
    sparse_prod = verify_and_replay(
        sparse_prod_task,
        MinerAuditSubmission(
            task_id=sparse_prod_task.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 2 0\n",
        ),
        cnf_text=prod_cnf,
        replay_fn=lambda _decoded, _task: ReplayEvidence(reproduced=True),
    )
    ck("production sparse bit maps without required_fields are rejected",
       not sparse_prod.accepted
       and sparse_prod.rejection_reason == "sparse_decode_requires_required_fields")

    flagged_sparse_prod_task = AuditTask(
        task_id="audit-flagged-sparse-production-map",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="sparse decode maps are smoke/corpus only",
        challenge_id="audit-prod-cnf",
        cnf_sha256=sha256_text(prod_cnf),
        decode_map={
            "allow_sparse_decode": True,
            "fields": {"amount": {"bits": [1]}},
        },
    )
    flagged_sparse_prod = verify_and_replay(
        flagged_sparse_prod_task,
        MinerAuditSubmission(
            task_id=flagged_sparse_prod_task.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 2 0\n",
        ),
        cnf_text=prod_cnf,
        replay_fn=lambda _decoded, _task: ReplayEvidence(reproduced=True),
    )
    ck("explicit sparse decode maps are rejected outside smoke/corpus mode",
       not flagged_sparse_prod.accepted
       and flagged_sparse_prod.rejection_reason == "sparse_decode_requires_smoke_or_corpus")

    sparse_smoke_task = AuditTask(
        task_id="audit-sparse-smoke-map",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="corpus maps may carry partial decoded fields for smoke tests",
        challenge_id="audit-prod-cnf",
        cnf_sha256=sha256_text(prod_cnf),
        decode_map={
            "allow_sparse_decode": True,
            "fields": {"amount": {"bits": [1]}},
        },
        replay_kind="corpus_smoke",
    )
    sparse_smoke = verify_and_replay(
        sparse_smoke_task,
        MinerAuditSubmission(
            task_id=sparse_smoke_task.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 2 0\n",
        ),
        cnf_text=prod_cnf,
        replay_fn=lambda decoded, _task: ReplayEvidence(
            reproduced=decoded == {"amount": 1},
            artifacts={"mode": "corpus_smoke"},
        ),
    )
    ck("sparse decode maps remain available for explicit corpus smoke tasks",
       sparse_smoke.accepted and sparse_smoke.decoded_witness == {"amount": 1})

    bad_solution = MinerAuditSubmission(
        task_id=task.task_id,
        miner_hotkey="5AuditMiner",
        dimacs_solution="s SATISFIABLE\nv -1 0\n",
    )
    bad = verify_and_replay(
        task,
        bad_solution,
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("bad SAT witness is rejected before replay",
       not bad.accepted and bad.stage == "sat_verify"
       and bad.rejection_reason == "solution_unsatisfied")

    def non_repro(decoded, _task):
        return ReplayEvidence(reproduced=False, reason="local_scorer_did_not_move")

    no_replay = verify_and_replay(task, submission, cnf_text=cnf, replay_fn=non_repro)
    ck("non-reproducing witness scores zero even when SAT is valid",
       not no_replay.accepted and no_replay.rejection_reason == "local_scorer_did_not_move")

    truthy_string_replay = verify_and_replay(
        task,
        submission,
        cnf_text=cnf,
        replay_fn=lambda _decoded, _task: {"reproduced": "false"},
    )
    ck("string replay booleans are rejected instead of treated as truthy",
       not truthy_string_replay.accepted
       and truthy_string_replay.rejection_reason == "replay_reproduced_must_be_boolean")

    bit_decoded = decode_witness(
        [1, -2, 3, -4, 5],
        {
            "required_fields": ["amount", "signed_delta"],
            "fields": {
                "amount": {"bits": [1, 2, 3]},           # 1 + 0 + 4 = 5
                "signed_delta": {"bits": [4, 5], "signed": True},  # 10b -> -2
            }
        },
    )
    ck("bit-projection decode reconstructs unsigned integers", bit_decoded["amount"] == 5)
    ck("bit-projection decode supports signed two's-complement fields",
       bit_decoded["signed_delta"] == -2)

    wrong_cnf = verify_and_replay(
        task,
        submission,
        cnf_text="p cnf 1 1\n-1 0\n",
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    ck("CNF hash mismatch blocks stale/replayed audit packages",
       not wrong_cnf.accepted and wrong_cnf.rejection_reason == "cnf_sha256_mismatch")

    no_hash_task = AuditTask(
        task_id="audit-no-cnf-hash",
        target=target,
        invariant_id="INV-FIXEDPOINT-BOUNDS",
        invariant="CNF hash is required",
        challenge_id="audit-smoke-cnf",
        cnf_sha256="",
        decode_map={"fields": {"x": {"bits": [1]}}},
    )
    no_hash = verify_and_replay(
        no_hash_task,
        MinerAuditSubmission(
            task_id=no_hash_task.task_id,
            miner_hotkey="5AuditMiner",
            dimacs_solution="s SATISFIABLE\nv 1 0\n",
        ),
        cnf_text=cnf,
        replay_fn=lambda _decoded, _task: ReplayEvidence(reproduced=True),
    )
    ck("empty CNF hash is rejected",
       not no_hash.accepted and no_hash.rejection_reason == "cnf_sha256_required")

    fails = [name for name, ok in checks if not ok]
    print(
        "\nAUDIT ARENA VERIFY: "
        + (f"PASS all {len(checks)} checks" if not fails else "FAIL " + repr(fails))
    )
    raise SystemExit(1 if fails else 0)


if __name__ == "__main__":
    main()
