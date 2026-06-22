"""Smoke checks for distillation trace redaction/export.

Run:
  python3 distillation_verify.py
"""
from __future__ import annotations

import json
import sys

from scaffold.distillation import RedactionPolicy, export_trace
from scaffold.lanes.audit_arena import (
    AuditTarget,
    AuditTask,
    MinerAuditSubmission,
    fixedpoint_fee_silent_zero_replay,
    sha256_text,
    verify_and_replay,
)


checks: list[tuple[str, bool]] = []
STRONG_PUBLIC_SECRET = "public-redaction-secret-2026-06-20-v0"


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def _contains_text(value, needle: str) -> bool:
    return needle in json.dumps(value, sort_keys=True, default=str)


def _sample_trace() -> dict:
    cnf = "p cnf 1 1\n1 0\n"
    target = AuditTarget(
        target_id="private-subnet-target",
        repo_url="https://example.invalid/private/subnet",
        commit="secret-commit",
        netuid=999,
        validator_entrypoint="validator.py",
        scoring_entrypoint="score.py",
    )
    task = AuditTask(
        task_id="audit-redaction-smoke",
        target=target,
        invariant_id="INV-PRIVATE",
        invariant="private exploit invariant text",
        challenge_id="audit-redaction-cnf",
        cnf_sha256=sha256_text(cnf),
        decode_map={
            "allow_static_witness": True,
            "witness": {"amount": 1, "fee_rate": 49152, "secret": "raw-witness"},
        },
        replay_kind="corpus_smoke",
        severity_hint="high",
    )
    verdict = verify_and_replay(
        task,
        MinerAuditSubmission(
            task_id=task.task_id,
            miner_hotkey="5SensitiveMinerHotkey",
            dimacs_solution="s SATISFIABLE\nv 1 0\n",
            agent_trace={"notes": "private chain of thought", "tool": "local"},
        ),
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    assert verdict.accepted
    return verdict.distillation_trace


def _sample_rejected_trace() -> dict:
    cnf = "p cnf 1 1\n1 0\n"
    target = AuditTarget(
        target_id="private-subnet-target",
        repo_url="https://example.invalid/private/subnet",
        commit="secret-commit",
        netuid=999,
        validator_entrypoint="validator.py",
        scoring_entrypoint="score.py",
    )
    task = AuditTask(
        task_id="audit-redaction-rejected-smoke",
        target=target,
        invariant_id="INV-PRIVATE",
        invariant="private exploit invariant text",
        challenge_id="audit-redaction-cnf",
        cnf_sha256=sha256_text(cnf),
        decode_map={
            "allow_static_witness": True,
            "witness": {"amount": 1, "fee_rate": 49152, "secret": "raw-witness"},
        },
        replay_kind="corpus_smoke",
        severity_hint="high",
    )
    verdict = verify_and_replay(
        task,
        MinerAuditSubmission(
            task_id=task.task_id,
            miner_hotkey="5SensitiveMinerHotkey",
            dimacs_solution="s SATISFIABLE\nv -1 0\n",
            agent_trace={"notes": "bad private chain of thought", "tool": "local"},
        ),
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    assert not verdict.accepted
    return verdict.distillation_trace


trace = _sample_trace()

default_export = export_trace(trace)
ck("export_trace defaults to private export",
   default_export["private"] is True and default_export["audience"] == "private")

private_export = export_trace(trace, RedactionPolicy(audience="private", salt="test"))
ck("private export keeps export schema and source hash",
   private_export["schema_version"] == "cathedral.audit_trace.export.v1"
   and private_export["source_trace_hash"] == trace["trace_hash"])
ck("private export hashes miner identity",
   private_export["submission"]["miner_hotkey_hash"]
   and not _contains_text(private_export, "5SensitiveMinerHotkey"))
ck("private export omits raw agent trace by default",
   "agent_trace_hash" in private_export["submission"]
   and not _contains_text(private_export, "private chain of thought"))
ck("private export omits raw witness by default",
   "decoded_witness_hash" in private_export["verdict"]
   and not _contains_text(private_export, "raw-witness"))

private_full = export_trace(
    trace,
    RedactionPolicy(
        audience="private",
        salt="test",
        include_agent_trace=True,
        include_raw_witness=True,
        include_replay_artifacts=True,
    ),
)
ck("private operator export can include raw witness when explicitly requested",
   _contains_text(private_full, "raw-witness")
   and _contains_text(private_full, "private chain of thought"))

try:
    export_trace(trace, RedactionPolicy(audience="public", disclosure_status="private"))
    ck("public export rejects undisclosed targets", False)
except ValueError as e:
    ck("public export rejects undisclosed targets",
       str(e) == "public_export_requires_fixed_public_opt_in_or_cathedral_owned")

try:
    export_trace(trace, RedactionPolicy(audience="public", disclosure_status="fixed"))
    ck("public export requires a redaction salt", False)
except ValueError as e:
    ck("public export requires a redaction salt",
       str(e) == "public_export_requires_non_empty_redaction_salt")

try:
    export_trace(trace, RedactionPolicy(audience="public", disclosure_status="fixed", salt="test"))
    ck("public export rejects weak redaction salt", False)
except ValueError as e:
    ck("public export rejects weak redaction salt",
       str(e) == "public_export_requires_strong_salt_or_hmac_secret")

public_export = export_trace(
    trace,
    RedactionPolicy(audience="public", disclosure_status="fixed", salt=STRONG_PUBLIC_SECRET),
)
ck("public export is marked public",
   public_export["private"] is False and public_export["audience"] == "public")
ck("public export records HMAC redaction guidance",
   public_export["redaction"]["hash_algorithm"] == "hmac-sha256"
   and public_export["redaction"]["public_hash_secret_required"] is True
   and "HMAC" in public_export["redaction"]["public_hash_guidance"])
ck("public export strips repo and commit",
   public_export["task"]["repo_url"] == ""
   and public_export["task"]["commit"] == ""
   and public_export["task"]["netuid"] is None)
ck("public export does not leak raw witness, miner hotkey, or repo url",
   not _contains_text(public_export, "raw-witness")
   and not _contains_text(public_export, "5SensitiveMinerHotkey")
   and not _contains_text(public_export, "https://example.invalid/private/subnet"))
ck("public export keeps verification supervision",
   public_export["supervision"]["label"] == "reproduced_witness"
   and public_export["verdict"]["accepted"] is True)

rejected_export = export_trace(
    _sample_rejected_trace(),
    RedactionPolicy(audience="private", salt="test"),
)
ck("rejected traces are retained as valuable negative controls",
   rejected_export["dataset"]["category"] == "rejected_claim_negative_control"
   and rejected_export["dataset"]["retention_value"] == "valuable_negative_control"
   and rejected_export["supervision"]["training_value"] == "valuable_negative_control")


failed = [name for name, ok in checks if not ok]
if failed:
    print("\nFAILED:")
    for name in failed:
        print(f" - {name}")
    sys.exit(1)
print(f"\nOK: {len(checks)} checks")
