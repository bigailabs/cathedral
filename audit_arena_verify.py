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
