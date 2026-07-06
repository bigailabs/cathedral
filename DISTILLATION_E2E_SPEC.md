# Distillation End-to-End Spec (v0 → live)

Status: design + acceptance gate. Extends `DISTILLATION_READINESS.md` (which
covers only trace redaction/export) with the full pipeline: corpus assembly →
training data build → model artifact → serving/revenue, plus the exact tests
that must pass before this lane is called ready to merge live.

This spec does not change validator scoring, does not touch emissions, and does
not enable any public corpus by default. Every new stage is private-by-default
and gated, matching the existing lane discipline.

## Where this picks up

The already-built foundation (do not re-implement):

- `scaffold/lanes/audit_arena.py` — `verify_and_replay(...)` returns an
  `AuditVerdict` whose `.distillation_trace` is a `cathedral.audit_trace.v1`
  record (label `reproduced_witness` | `rejected_claim`, `trace_hash`, task,
  submission, decoded_witness, replay, supervision).
- `scaffold/distillation.py` — `export_trace(trace, RedactionPolicy)` produces a
  `cathedral.audit_trace.export.v1` record. Private-by-default; public export is
  disclosure-gated and requires a strong hash secret. Emits `dataset.category`
  (`accepted_reproduced_witness` | `rejected_claim_negative_control`) and
  `dataset.retention_value`.
- `distillation_verify.py` — smoke gate proving redaction/export policy holds.

The pipeline below consumes `export_trace(...)` output. But note the critical
correction (Codex finding #1): **`export.v1` is NOT automatically training-safe.**
The exporter keeps `task.repo_url` / `task.commit` in PRIVATE exports
(`scaffold/distillation.py:97-98`), and a private-audience `RedactionPolicy` can
intentionally include the raw agent trace, raw decoded witness, and replay
artifacts (`scaffold/distillation.py:119, 149, 151`). The existing smoke gate
explicitly blesses that operator mode (`distillation_verify.py`).

Therefore Stage 4 does NOT trust `export.v1` blindly. It applies a
**training-safety gate** (`training_safe_view`) that:

- rejects any export whose `redaction.raw_witness_included`,
  `agent_trace_included`, or `replay_artifacts_included` is true, and
- strips/hashes `task.repo_url` and `task.commit` before the record can become a
  corpus member.

Only the training-safe view flows downstream. Redaction is enforced HERE, not
assumed from upstream.

`replay` in the source trace is nullable (rejections before replay build traces
with `replay=None`, `scaffold/lanes/audit_arena.py`), and traces also carry
`replay_provenance`. The pipeline handles both.

## Pipeline stages

```
[Lane 1 live]        Stage 4            Stage 5           Stage 6         Stage 7
audit_arena  ──▶  corpus assembly ──▶ training build ──▶ fine-tune  ──▶ serve/eval
 (traces)          (dedup, split,      (prompt/target     (adapter,     (gated API,
                    balance, hash)      pairs, format)     artifact)     revenue)
                        │                                                    │
                        └──────────── all private-by-default ───────────────┘
```

### Stage 4 — Corpus assembly (`scaffold/distillation_corpus.py`)

Turn a stream of `export_trace(...)` records into a deterministic, deduplicated,
split training corpus. No model, no training — just data curation.

Public functions:

- `training_safe_view(export: dict) -> dict` — applies the training-safety gate
  above. Raises `UnsafeExportError` if the export carries raw witness / agent
  trace / replay artifacts; otherwise returns a member with `repo_url`/`commit`
  stripped-or-hashed and `member_hash` set. Every corpus member passes through
  this first.
- `assemble_corpus(exports: Iterable[dict], *, config: CorpusConfig) -> Corpus`
- `Corpus.stats() -> dict` — counts per category, split sizes, dedup drops.
- `Corpus.to_manifest() -> dict` — schema `cathedral.distillation_corpus.v1`.
  Two distinct hashes (Codex finding #3):
  - `member_set_hash` = hash over sorted member `export_hash` values (identity of
    the data).
  - `corpus_hash` = hash over the canonical manifest: schema version + full
    `CorpusConfig` (including split ratios and salt) + the resolved per-member
    split assignments + `member_set_hash`. So a different split salt or ratio
    yields a different `corpus_hash` even on identical members.

`CorpusConfig` fields:

- `dedup_by`: `"export_hash"` (default) — drop exact-duplicate exports.
- `split`: `{"train": 0.8, "val": 0.1, "test": 0.1}` — deterministic assignment
  by `sha256(export_hash + salt) mod 1000`, so the same export always lands in
  the same split (no leakage across rebuilds).
- `balance`: cap on `rejected_claim_negative_control` : `accepted_reproduced_witness`
  ratio (default no cap; rejected traces are valuable, keep them).
- `min_members`: refuse to emit a corpus below N members (default 1 for smoke).
- `audience`: `"private"` (default) | `"public"`. A `"public"` corpus requires
  every member to be public-disclosure-gated (see hard rule 3).

Hard rules (enforced, tested):

1. Input must be `export.v1` records, never raw `audit_trace.v1`. Reject a raw
   trace (has `submission.agent_trace` unredacted, or no `redaction` block).
2. Training-safety gate (finding #1): reject any export with
   `redaction.raw_witness_included` / `agent_trace_included` /
   `replay_artifacts_included` == true; strip/hash `task.repo_url` and
   `task.commit`. No member retains raw repo/commit.
3. A corpus with `audience="public"` members must have every member already
   public-disclosure-gated (carry `disclosure_status` in allowed set). A single
   private member in a public corpus is a hard error.
4. `member_set_hash` is deterministic: same members → same hash, order-independent.
5. `corpus_hash` changes when split ratios or salt change (finding #3).
6. Split assignment is stable across rebuilds (seeded by member export_hash + salt).
7. No split leakage: an `export_hash` appears in exactly one split.

### Stage 5 — Training-pair build (`scaffold/distillation_pairs.py`)

Convert corpus members into model-ready supervised pairs. Format-only; still no
training.

Public functions (finding #2 — split is explicit, returns a manifest not a bare list):

- `build_pairs(corpus: Corpus, *, fmt: PairFormat, split: str) -> PairsManifest`
  where `split` ∈ `{"train","val","test"}`.
- `PairsManifest` = `{pairs, split, corpus_hash, pairs_hash, format_hash,
  member_export_hashes}`. `pairs_hash` is over the canonical serialized pairs;
  `corpus_hash` is carried through from the source corpus so Stage 6 can verify
  lineage without re-reading the corpus.
- `TrainingPair` = `{input, target, label, weight, provenance_hash}` where
  `provenance_hash` = source member `export_hash`.

Semantics:

- `accepted_reproduced_witness` → positive pair: input = redacted task/context,
  target = "this witness reproduces / is a valid exploit path", `weight=1.0`.
- `rejected_claim_negative_control` → negative pair: target = "this claim does
  not reproduce" + `rejection_reason` category, `weight` configurable (default
  1.0; negatives are first-class).
- `provenance_hash` = member `export_hash`, so any pair traces back to its
  source export (audit trail; never to a raw hotkey).

Hard rules (enforced, tested):

1. No pair may contain a raw miner hotkey, repo URL, commit, or raw witness.
   Pairs are built from training-safe corpus members only; a scan asserts none
   of these markers appear in any `input`/`target` string.
2. `build_pairs(..., split="train")` emits only train-split members; test-split
   members never appear. Same for val.
3. Deterministic: same corpus + same format + same split → byte-identical
   `pairs_hash`.
4. `PairsManifest.corpus_hash` equals the source corpus's `corpus_hash` (lineage
   carried forward for Stage 6).

### Stage 6 — Fine-tune (`scaffold/distillation_train.py`)

Produce a model artifact from training pairs. This is the only stage that needs
real compute (Kaggle TPU / rented GPU). It must be runnable in a **dry-run**
mode that produces a valid artifact manifest without a GPU, so CI can gate it.

Public functions (finding #2 — takes a PairsManifest so lineage is verifiable):

- `train(pairs_manifest: PairsManifest, *, config: TrainConfig, dry_run: bool=False) -> ModelArtifact`
- `ModelArtifact` = manifest `cathedral.distillation_model.v1`:
  `{base_model, base_model_license, adapter_kind, corpus_hash, pairs_hash,
    train_config_hash, artifact_sha256, eval: null, created_by}`. `corpus_hash`
  and `pairs_hash` are read FROM the manifest (not passed loosely), so a model
  cannot claim a lineage its inputs don't support.

Semantics:

- `dry_run=True` validates config, writes a manifest with
  `artifact_sha256="dry-run"` and `eval=null`, runs zero training steps. This is
  what the acceptance gate exercises (no GPU in CI).
- Real run: LoRA/adapter fine-tune (base model configurable; default a small
  open model that fits one TPU v5e-8 or a modest GPU). Records `artifact_sha256`
  of the produced weights.
- The manifest binds `corpus_hash` + `pairs_hash` + `train_config_hash` so a
  model is fully reproducible from its inputs (provenance chain: model → pairs →
  corpus → exports → verified traces).

Hard rules (enforced, tested):

1. Manifest must bind corpus_hash, pairs_hash, and train_config_hash. Missing
   any → hard error (no unprovenanced models).
2. `dry_run` produces a schema-valid manifest and never touches a GPU / network.
3. Refuse to train on a corpus whose members are not all present in the manifest
   lineage (no silent data drift between assembly and training).

### Stage 7 — Serve + eval + revenue gate (`scaffold/distillation_serve.py`)

Gate a model artifact for serving and record eval + revenue evidence. Mirrors
Lane 2's evidence discipline (finding #5): a model is not "live/earning" because
someone claims it works — it earns only when eval passes AND a healthy deployment
AND a usage/revenue receipt all exist, and it demotes when evidence goes stale.

Eval (finding #7 — per-category metrics + baseline, not raw accuracy):

- `evaluate(artifact, pairs_manifest, *, config: EvalConfig) -> EvalReport`.
  `pairs_manifest.split` MUST be `"test"` (leakage guard). `EvalReport` =
  `{n, accuracy, per_category: {precision, recall, f1}, positive_recall,
    baseline_accuracy, beats_baseline: bool, eval_hash}`.
- `EvalConfig` = `{min_accuracy, min_positive_recall, must_beat_baseline}`.
  `baseline_accuracy` is the majority-class classifier on the test set — this is
  what catches a degenerate model that always predicts "rejected" and scores high
  on an imbalanced set.

Serving/revenue state machine (finding #5 — parallels Lane 2 gates):

```
[*] -> evaluated : eval computed on test split
evaluated -> ready : eval passes EvalConfig thresholds
ready -> deployed : deployment_id + auth/allowlist recorded
deployed -> healthy : health_receipt recorded
healthy -> earning : usage_receipt (with receipt_hash + source) recorded
healthy -> stale : health/usage receipt ages out -> demote
earning -> stale : receipts age out -> demote
```

- `serving_manifest(artifact, eval, *, config: ServeConfig, gated=True) -> dict`
  — schema `cathedral.distillation_serving.v1`. Fields: `state`, `deployment_id`,
  `auth`, `gated`, `eval`, `health_receipt`, `usage_receipt`, `receipt_hash`,
  `receipt_source`, `updated_at`. Advances state only when the evidence for the
  next state is present.
- Revenue is off-chain (Lane 2 line 115). No emissions writes, ever.

Hard rules (enforced, tested):

1. `evaluate(...)` on a non-`test` split is a hard error (leakage guard).
2. `serving_manifest(..., eval=None)` → `state="evaluated"` is impossible;
   `ready` requires eval meeting all `EvalConfig` thresholds.
3. Eval must beat the majority-class baseline when `must_beat_baseline=True`;
   a degenerate always-one-class model is rejected.
4. `state="earning"` requires deployment + health_receipt + usage_receipt with a
   non-empty `receipt_hash` and `receipt_source`. Missing any → not earning.
5. Stale health/usage receipt demotes `earning`/`healthy` back to `stale`.
6. Default `gated=True`: serving is invite/allowlist gated until operator opens it.

## Provenance chain (the whole point)

```
verified trace ─▶ export (redacted) ─▶ corpus member ─▶ training pair ─▶ model ─▶ serving
   trace_hash        export_hash          member_set_hash   pairs_hash    artifact_sha256
   (source)          source_trace_hash    + corpus_hash
```

Canonical hash inputs (each hash excludes its own self-field, matching
`scaffold/distillation.py:85` where `export_hash` is computed over the body
BEFORE the field is attached):

- `export_hash` = `sha256(canonical_json(export_without_export_hash))`.
- `member_set_hash` = `sha256(canonical_json(sorted(member export_hash list)))`.
- `corpus_hash` = `sha256(canonical_json({schema, config, split_assignments, member_set_hash}))`.
- `pairs_hash` = `sha256(canonical_json(pairs_without_pairs_hash))`.
- `artifact_sha256` = hash of the actual weights (or `"dry-run"`).
- `eval_hash` = `sha256(canonical_json(eval_without_eval_hash))`.

Verification (finding #4 — the chain is recomputed, not assumed):

- Each export carries `source_schema_version` (must == `cathedral.audit_trace.v1`)
  and `source_trace_hash` (must be non-empty) — `scaffold/distillation.py:62-66`.
- `export_hash` is recomputable from the export body (`scaffold/distillation.py:85`);
  the gate recomputes it and asserts it matches.
- `corpus_hash` binds config+splits+`member_set_hash`; `pairs_hash` binds the
  serialized pairs; the model manifest binds `corpus_hash`+`pairs_hash`.
- `provenance_chain_intact` walks served manifest → model → pairs → corpus →
  member export_hashes, recomputing each hash and requiring each `source_trace_hash`
  to resolve against the retained verified-trace index.

Given a served model, you can prove exactly which verified, reproduced witnesses
trained it — without ever exposing a raw hotkey, repo, or witness. That
provenance is the sellable trust story and the audit defense.

## Acceptance gate — `distillation_e2e_verify.py`

Runnable with zero GPU and zero network (matches the house `*_verify.py`
pattern: build sample data, run each stage, assert, `sys.exit(1)` on any FAIL,
print `OK: N checks`).

Scope correction (finding #6): this gate proves the pipeline is **offline-scaffold
ready** — the data governance, provenance, and state-machine logic are correct and
safe. It does NOT prove a live revenue-generating model, because real GPU training
and serving-infra choice are out of scope for v0 (see below). "Merge live" is a
two-tier bar:

- **Tier A (this gate):** offline scaffold ready. `distillation_e2e_verify.py` +
  `distillation_verify.py` pass. Safe to merge the scaffold to main.
- **Tier B (real revenue, later):** one real fine-tune produces a licensed model
  artifact, deployed behind auth, with a real health receipt and a real
  usage/revenue receipt — the direct parallel to Lane 2's "Production Hardware
  Ask Gate." Do not claim live earning before Tier B.

The gate must prove every hard rule above. Concretely, these named checks:

Stage 4 (corpus):
- `corpus_rejects_raw_trace` — feeding a raw `audit_trace.v1` errors.
- `corpus_rejects_unsafe_export` — export with raw witness / agent trace /
  replay artifacts included is rejected (finding #1).
- `corpus_strips_repo_commit` — no member retains raw `repo_url`/`commit` (finding #1).
- `member_set_hash_deterministic` — same members, shuffled order → same hash.
- `corpus_hash_binds_split_config` — changing split ratio or salt changes
  `corpus_hash` on identical members (finding #3).
- `corpus_split_stable` — rebuild with same config → identical split assignment.
- `corpus_no_split_leakage` — every export_hash in exactly one split.
- `corpus_public_requires_all_public` — one private member in a public corpus errors.
- `corpus_keeps_negative_controls` — rejected traces retained, not dropped.

Stage 5 (pairs):
- `pairs_no_sensitive_markers` — no hotkey/repo/commit/raw-witness in any pair.
- `pairs_deterministic` — same corpus+format+split → identical `pairs_hash`.
- `pairs_negative_is_first_class` — a rejected trace yields a usable negative pair.
- `pairs_no_test_leakage` — train pairs exclude test-split members (finding #2).
- `pairs_carry_corpus_hash` — `PairsManifest.corpus_hash` == source corpus_hash.

Stage 6 (train):
- `train_dry_run_no_gpu` — dry-run produces schema-valid manifest, zero steps.
- `train_manifest_binds_lineage` — manifest binds corpus+pairs+config hashes
  read FROM the PairsManifest (finding #2).
- `train_rejects_unprovenanced` — PairsManifest missing lineage hash errors.
- `train_records_base_model_license` — manifest carries `base_model_license`.

Stage 7 (serve):
- `eval_test_split_only` — eval on train/val split errors (leakage guard).
- `eval_rejects_degenerate_classifier` — always-one-class model fails
  `must_beat_baseline` (finding #7).
- `serve_below_threshold_not_ready` — eval under `min_accuracy`/`min_positive_recall`
  → not `ready` (finding #7).
- `serve_requires_eval` — no eval → cannot reach `ready`.
- `serve_no_earning_without_receipt` — `earning` needs deployment + health +
  usage receipt with `receipt_hash`/`receipt_source` (finding #5).
- `serve_stale_receipt_demotes` — aged receipt demotes to `stale` (finding #5).
- `serve_gated_by_default` — default serving manifest is `gated=True`.

Cross-cutting:
- `provenance_chain_intact` — served manifest → model → pairs → corpus →
  export_hashes, recomputing each hash, requiring
  `source_schema_version == cathedral.audit_trace.v1` and non-empty
  `source_trace_hash` resolving against the retained trace index (finding #4).
- `no_emissions_writes` — AST/import denylist covering both fully-qualified and
  relative forms: `scaffold.chain`/`chain`, `scaffold.validator_thin`/`validator_thin`,
  `scaffold.publisher.weights`/`publisher.weights`,
  `scaffold.publisher.scoring`/`publisher.scoring`; plus a sentinel that any
  `set_weights` call fails (finding #8).
- `dry_run_no_gpu_no_network` — gate runs with sockets disabled and accelerator
  APIs monkeypatched to fail; the full pipeline still passes (finding #8).

## Tier A checklist — offline scaffold ready (this PR)

- [ ] `python3 distillation_verify.py` passes (existing redaction gate).
- [ ] `python3 distillation_e2e_verify.py` passes (all named checks above).
- [ ] `pytest` for the four new modules passes (unit-level, per-stage).
- [ ] No new module imports validator scoring/weights tables (AST denylist check).
- [ ] No public corpus enabled by default; public path stays disclosure-gated.
- [ ] Training-safety gate rejects unsafe exports; no member keeps raw repo/commit.
- [ ] `dry_run` training path works with sockets disabled + accelerators failing.
- [ ] Codex review clear (no blocking findings) against this spec + code.

## Tier B checklist — real revenue (later, do not claim before done)

- [ ] One real fine-tune produces a licensed `ModelArtifact` with real
      `artifact_sha256`. (`ModelArtifact.eval` stays `null`; eval lives in the
      `EvalReport` / serving manifest, not the artifact — see Stage 6/7.)
- [ ] Eval beats the majority-class baseline on the held-out test split.
- [ ] Model deployed behind auth/allowlist; `health_receipt` recorded.
- [ ] Real `usage_receipt` with `receipt_hash`/`receipt_source` recorded.
- [ ] Operator explicitly opens serving (`gated=False`).

## Explicitly out of scope for v0

- Real GPU training runs (design supports them; gate uses dry-run).
- A public corpus release (stays private; public path exists but off by default).
- Emissions/validator changes (revenue is off-chain, like Lane 2).
- Choice of the exact base model / serving infra (config-driven, not hardcoded).
