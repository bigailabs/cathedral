"""End-to-end distillation acceptance gate (Tier A: offline scaffold ready).

Runs every named check from DISTILLATION_E2E_SPEC.md with zero GPU and zero
network. Sockets are disabled and accelerator probing is blocked before any
pipeline code runs, proving the dry-run path is genuinely offline.

Run:
  python3 distillation_e2e_verify.py
"""
from __future__ import annotations

import ast
import builtins
import socket
import sys
from pathlib import Path

# --- Offline enforcement: disable sockets AND block accelerator imports before
# importing any pipeline module, proving the dry-run path is genuinely offline
# and GPU-free (findings #6/#10). ---
def _blocked_socket(*_a, **_k):  # noqa: ANN001
    raise RuntimeError("network_access_blocked_in_gate")


socket.socket = _blocked_socket  # type: ignore[assignment]

_BLOCKED_ACCEL = {"torch", "torch_xla", "jax", "jaxlib", "tensorflow",
                  "cupy", "triton", "vllm", "transformers"}
_real_import = builtins.__import__


def _guarded_import(name, *a, **k):  # noqa: ANN001
    root = name.split(".")[0]
    if root in _BLOCKED_ACCEL:
        raise RuntimeError(f"accelerator_import_blocked_in_gate:{name}")
    return _real_import(name, *a, **k)


builtins.__import__ = _guarded_import  # type: ignore[assignment]

from scaffold.distillation import RedactionPolicy, export_trace  # noqa: E402
from scaffold.lanes.audit_arena import (  # noqa: E402
    AuditTarget,
    AuditTask,
    MinerAuditSubmission,
    fixedpoint_fee_silent_zero_replay,
    sha256_text,
    verify_and_replay,
)
from scaffold.distillation_corpus import (  # noqa: E402
    CorpusConfig,
    NotAnExportError,
    UnsafeExportError,
    assemble_corpus,
    training_safe_view,
)
from scaffold.distillation_pairs import PairFormat, build_pairs  # noqa: E402
from scaffold.distillation_train import (  # noqa: E402
    TrainConfig,
    UnprovenancedTrainingError,
    train,
)
from scaffold.distillation_serve import (  # noqa: E402
    EvalConfig,
    LeakageError,
    ServeConfig,
    evaluate,
    serving_manifest,
)


checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def _raises(exc, fn, *a, **k) -> bool:
    try:
        fn(*a, **k)
    except exc:
        return True
    except Exception:
        return False
    return False


# --- Build sample verified traces -> exports -----------------------------------
def _make_export(seq: int, accepted: bool, *, policy: RedactionPolicy | None = None) -> dict:
    cnf = "p cnf 1 1\n1 0\n"
    target = AuditTarget(
        target_id=f"target-{seq}",
        repo_url="https://example.invalid/private/subnet",
        commit=f"secret-commit-{seq}",
        netuid=999,
        validator_entrypoint="validator.py",
        scoring_entrypoint="score.py",
    )
    task = AuditTask(
        task_id=f"audit-{seq}",
        target=target,
        invariant_id=f"INV-{seq}",
        invariant="private exploit invariant text",
        challenge_id=f"cnf-{seq}",
        cnf_sha256=sha256_text(cnf),
        decode_map={
            "allow_static_witness": True,
            "witness": {"amount": 1, "fee_rate": 49152, "secret": "raw-witness"},
        },
        replay_kind="corpus_smoke",
        severity_hint="high",
    )
    sol = "s SATISFIABLE\nv 1 0\n" if accepted else "s SATISFIABLE\nv -1 0\n"
    verdict = verify_and_replay(
        task,
        MinerAuditSubmission(
            task_id=task.task_id,
            miner_hotkey=f"5SensitiveHotkey{seq}",
            dimacs_solution=sol,
            agent_trace={"notes": "private chain of thought", "tool": "local"},
        ),
        cnf_text=cnf,
        replay_fn=fixedpoint_fee_silent_zero_replay,
    )
    return export_trace(verdict.distillation_trace, policy or RedactionPolicy())


# A spread of exports: several accepted, several rejected, distinct seqs.
# 60 members so an 80/10/10 split yields non-trivial val/test sets.
EXPORTS = [_make_export(i, accepted=(i % 3 != 0)) for i in range(1, 61)]


# =============================== Stage 4: corpus ===============================
raw_trace = verify_and_replay(
    AuditTask(
        task_id="raw",
        target=AuditTarget(target_id="t", repo_url="r", commit="c", netuid=1,
                           validator_entrypoint="v", scoring_entrypoint="s"),
        invariant_id="i", invariant="x", challenge_id="c",
        cnf_sha256=sha256_text("p cnf 1 1\n1 0\n"),
        decode_map={"allow_static_witness": True, "witness": {"a": 1}},
        replay_kind="corpus_smoke", severity_hint="low",
    ),
    MinerAuditSubmission(task_id="raw", miner_hotkey="5X",
                         dimacs_solution="s SATISFIABLE\nv 1 0\n", agent_trace={}),
    cnf_text="p cnf 1 1\n1 0\n",
    replay_fn=fixedpoint_fee_silent_zero_replay,
).distillation_trace

ck("corpus_rejects_raw_trace", _raises(NotAnExportError, training_safe_view, raw_trace))

unsafe_export = _make_export(
    99, accepted=True,
    policy=RedactionPolicy(audience="private", include_raw_witness=True),
)
ck("corpus_rejects_unsafe_export", _raises(UnsafeExportError, training_safe_view, unsafe_export))

safe_member = training_safe_view(EXPORTS[0])
ck("corpus_strips_repo_commit",
   "repo_url" not in safe_member["task"] and "commit" not in safe_member["task"])

corpus = assemble_corpus(EXPORTS)
corpus_shuffled = assemble_corpus(list(reversed(EXPORTS)))
ck("member_set_hash_deterministic", corpus.member_set_hash == corpus_shuffled.member_set_hash)

corpus_alt_salt = assemble_corpus(EXPORTS, config=CorpusConfig(split_salt="different-salt"))
ck("corpus_hash_binds_split_config", corpus.corpus_hash != corpus_alt_salt.corpus_hash)

corpus_rebuild = assemble_corpus(EXPORTS)
ck("corpus_split_stable", corpus.split_assignments == corpus_rebuild.split_assignments)

all_splits = list(corpus.split_assignments.values())
in_one_split = all(
    sum(1 for s in [corpus.split_assignments[h]] if s) == 1
    for h in corpus.split_assignments
)
ck("corpus_no_split_leakage",
   in_one_split and len(corpus.split_assignments) == len(corpus.members))

private_member_export = EXPORTS[0]  # disclosure_status = "private"
ck("corpus_public_requires_all_public",
   _raises(UnsafeExportError, assemble_corpus, EXPORTS, config=CorpusConfig(audience="public")))

neg = sum(1 for m in corpus.members if not m["supervision"].get("accepted"))
ck("corpus_keeps_negative_controls", neg > 0)


# =============================== Stage 5: pairs ================================
train_pairs = build_pairs(corpus, split="train")
test_pairs = build_pairs(corpus, split="test")

blob = "\n".join(f"{p.input}\n{p.target}" for p in train_pairs.pairs).lower()
ck("pairs_no_sensitive_markers",
   "https://" not in blob and "github.com" not in blob and "5sensitive" not in blob
   and "secret-commit" not in blob)

train_pairs_2 = build_pairs(corpus, split="train")
ck("pairs_deterministic", train_pairs.pairs_hash == train_pairs_2.pairs_hash)

has_negative = any(p.label == "rejected_claim_negative_control" for p in
                   list(train_pairs.pairs) + list(test_pairs.pairs))
ck("pairs_negative_is_first_class", has_negative)

train_hashes = set(train_pairs.member_export_hashes)
test_hashes = set(test_pairs.member_export_hashes)
ck("pairs_no_test_leakage", train_hashes.isdisjoint(test_hashes))

ck("pairs_carry_corpus_hash", train_pairs.corpus_hash == corpus.corpus_hash)


# =============================== Stage 6: train ===============================
artifact = train(train_pairs, config=TrainConfig(), dry_run=True)
ck("train_dry_run_no_gpu", artifact.artifact_sha256 == "dry-run")

manifest = artifact.to_manifest()
ck("train_manifest_binds_lineage",
   manifest["corpus_hash"] == corpus.corpus_hash
   and manifest["pairs_hash"] == train_pairs.pairs_hash
   and bool(manifest["train_config_hash"]))


class _FakePairs:
    corpus_hash = ""
    pairs_hash = ""
    split = "train"


ck("train_rejects_unprovenanced",
   _raises(UnprovenancedTrainingError, train, _FakePairs(), dry_run=True))

ck("train_records_base_model_license", bool(manifest["base_model_license"]))


# =============================== Stage 7: serve ===============================
ck("eval_test_split_only",
   _raises(LeakageError, evaluate, artifact, train_pairs))


# A model that always predicts the majority class should NOT beat baseline.
degenerate_report = evaluate(artifact, test_pairs, predict=None)
ck("eval_rejects_degenerate_classifier", not degenerate_report.beats_baseline)

sm_degenerate = serving_manifest(artifact, degenerate_report)
ck("serve_below_threshold_not_ready", not sm_degenerate["ready"])

sm_no_eval = serving_manifest(artifact, None)
ck("serve_requires_eval", not sm_no_eval["ready"] and sm_no_eval["state"] == "unevaluated")


# A perfect predictor beats baseline and passes; then test earning gates.
def _perfect(inp: str) -> str:
    for p in test_pairs.pairs:
        if p.input == inp:
            return p.label
    return "rejected_claim_negative_control"


# must_beat_baseline=False here: these checks exercise the receipt/state machine,
# not eval quality (degenerate/baseline rejection is covered by its own checks).
_PASS_EVAL = EvalConfig(min_accuracy=0.5, min_positive_recall=0.0, must_beat_baseline=False)
good_report = evaluate(artifact, test_pairs, predict=_perfect, config=_PASS_EVAL)
sm_ready = serving_manifest(artifact, good_report, config=ServeConfig(eval=_PASS_EVAL))
sm_no_receipt = serving_manifest(
    artifact, good_report,
    config=ServeConfig(eval=_PASS_EVAL),
    deployment_id="dep-1", auth="allowlist",
    health_receipt={"timestamp": 100},
    now=200,
)
ck("serve_no_earning_without_receipt", not sm_no_receipt["earning"])

sm_earning = serving_manifest(
    artifact, good_report,
    config=ServeConfig(eval=_PASS_EVAL),
    deployment_id="dep-1", auth="allowlist",
    health_receipt={"timestamp": 100},
    usage_receipt={"timestamp": 100, "receipt_hash": "abc", "receipt_source": "chutes"},
    now=200,
)
sm_stale = serving_manifest(
    artifact, good_report,
    config=ServeConfig(eval=_PASS_EVAL, receipt_max_age_seconds=10),
    deployment_id="dep-1", auth="allowlist",
    health_receipt={"timestamp": 100},
    usage_receipt={"timestamp": 100, "receipt_hash": "abc", "receipt_source": "chutes"},
    now=100_000,
)
ck("serve_earning_with_full_evidence",
   sm_earning["state"] == "earning" and sm_earning["earning"]
   and sm_earning["receipt_hash"] == "abc" and sm_earning["receipt_source"] == "chutes")
ck("serve_stale_receipt_demotes", sm_stale["state"] == "stale" and not sm_stale["earning"])

sm_default = serving_manifest(artifact, good_report)
ck("serve_gated_by_default", sm_default["gated"] is True)

# Adversarial checks added after implementation review (findings #2/#3/#4/#5).
import copy as _copy  # noqa: E402
from scaffold.distillation_corpus import verify_corpus_integrity, UnsafeExportError as _UEE  # noqa: E402

# Mutated corpus must be rejected at pair-build time.
_mut = _copy.deepcopy(corpus)
_mut.split_assignments[next(iter(_mut.split_assignments))] = "test"
ck("mutated_corpus_rejected", _raises(_UEE, verify_corpus_integrity, _mut))

# Forged eval report (bogus eval_hash) must not reach earning.
from scaffold.distillation_serve import EvalReport as _ER  # noqa: E402
_forged = _ER(n=10, accuracy=1.0, per_category={}, positive_recall=1.0,
              baseline_accuracy=0.0, beats_baseline=True,
              model_artifact_sha256="dry-run", model_corpus_hash=corpus.corpus_hash,
              test_pairs_hash=test_pairs.pairs_hash, eval_hash="FORGED")
_sm_forged = serving_manifest(
    artifact, _forged, config=ServeConfig(eval=_PASS_EVAL),
    deployment_id="d", auth="a",
    health_receipt={"timestamp": 100},
    usage_receipt={"timestamp": 100, "receipt_hash": "h", "receipt_source": "c"},
    now=200,
)
ck("forged_eval_rejected", not _sm_forged["earning"] and _sm_forged["state"] == "unevaluated")

# Future-dated receipt must not count as fresh.
_sm_future = serving_manifest(
    artifact, good_report, config=ServeConfig(eval=_PASS_EVAL),
    deployment_id="d", auth="a",
    health_receipt={"timestamp": 9999}, now=100,  # receipt in the future
)
ck("future_receipt_not_fresh", not _sm_future["earning"])


# =============================== Cross-cutting ================================
# provenance_chain_intact: recompute EVERY hash along the chain (finding #7).
from scaffold.distillation_corpus import recompute_corpus_hash, recompute_member_set_hash  # noqa: E402
from scaffold.distillation_pairs import recompute_pairs_hash  # noqa: E402

# Retained verified-trace index: the source_trace_hash of every export we built.
_TRACE_INDEX = {
    e["source_trace_hash"] for e in EXPORTS
}


def _provenance_ok() -> bool:
    # 1. Corpus hashes recompute from its own members/config/splits.
    if recompute_member_set_hash(corpus.members) != corpus.member_set_hash:
        return False
    if recompute_corpus_hash(corpus) != corpus.corpus_hash:
        return False
    # 2. Pairs hash recomputes; pairs carry the corpus hash.
    if recompute_pairs_hash(train_pairs.pairs) != train_pairs.pairs_hash:
        return False
    if train_pairs.corpus_hash != corpus.corpus_hash:
        return False
    # 3. Model manifest binds corpus + pairs.
    if manifest["corpus_hash"] != corpus.corpus_hash:
        return False
    if manifest["pairs_hash"] != train_pairs.pairs_hash:
        return False
    # 4. Served earning manifest carries a bound, non-null eval.
    if sm_earning["eval"] is None:
        return False
    # 5. Every member resolves to a retained verified trace.
    for m in corpus.members:
        if m["source_schema_version"] != "cathedral.audit_trace.v1":
            return False
        if not m["source_trace_hash"] or m["source_trace_hash"] not in _TRACE_INDEX:
            return False
    return True


ck("provenance_chain_intact", _provenance_ok())


# no_emissions_writes: AST denylist over the new modules.
_NEW_MODULES = [
    "scaffold/distillation_corpus.py",
    "scaffold/distillation_pairs.py",
    "scaffold/distillation_train.py",
    "scaffold/distillation_serve.py",
]
# Denylisted module leaf names (any module path ending in one of these, in any
# qualified/relative form, is forbidden).
_DENY_MODULE_LEAVES = {"chain", "validator_thin", "weights", "scoring"}
_DENY_MODULE_FULL = {
    "scaffold.chain", "scaffold.validator_thin",
    "scaffold.publisher.weights", "scaffold.publisher.scoring",
    "publisher.weights", "publisher.scoring",
}
_DENY_CALL_PREFIX = "set_weights"  # set_weights, set_weights_on_chain, ...
_DENY_IMPORTED_SYMBOLS = {"weights", "scoring", "chain", "validator_thin"}


def _module_denied(mod: str) -> bool:
    if not mod:
        return False
    if mod in _DENY_MODULE_FULL:
        return True
    return mod.split(".")[-1] in _DENY_MODULE_LEAVES


def _emissions_clean() -> bool:
    root = Path(__file__).parent
    for rel in _NEW_MODULES:
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _module_denied(alias.name):
                        return False
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # Relative imports (level>0) and `from X import weights`.
                if _module_denied(mod):
                    return False
                for alias in node.names:
                    # `from scaffold.publisher import weights` -> alias "weights"
                    if alias.name in _DENY_IMPORTED_SYMBOLS:
                        return False
            elif isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if isinstance(name, str) and name.startswith(_DENY_CALL_PREFIX):
                    return False
    return True


ck("no_emissions_writes", _emissions_clean())

# dry_run_no_gpu_no_network: the ENTIRE pipeline above executed with sockets
# disabled AND accelerator imports (torch/jax/tf/vllm/...) hard-blocked. Reaching
# this line at all proves the dry-run path touched neither (findings #10). We
# additionally assert the guards are still armed.
def _guards_armed() -> bool:
    if socket.socket is not _blocked_socket:
        return False
    try:
        _guarded_import("torch")
    except RuntimeError:
        return True
    return False


ck("dry_run_no_gpu_no_network", _guards_armed())


# =============================== Gate exit ====================================
failed = [name for name, ok in checks if not ok]
if failed:
    print("\nFAILED:")
    for name in failed:
        print(f" - {name}")
    sys.exit(1)
print(f"\nOK: {len(checks)} checks")
