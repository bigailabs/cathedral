# v3 datasets: private catalog and training-export skeleton

## Purpose

We collect full Hermes packages from v3 `bug_isolation_v1` evals. This doc covers the private catalog format and the training-export skeleton that turns those packages into SFT, DPO, and RM training rows. Nothing here goes near the public feed.

## Catalog row schema

One catalog row per eval_run. Rows are append-only JSONL.

```
schema:               "cathedral.v3.catalog/1"
eval_run_id:          str
bundle_uri:           str | None     # s3://... encrypted Hermes TraceBundle
manifest_uri:         str | None     # s3://... signed bundle manifest
score_uri:            str            # s3://... private score_record
task_type:            str            # e.g. "bug_isolation_v1"
weighted_score:       float
split:                "train" | "val" | "test"
distillation_ready:   bool           # weighted_score >= 0.75 AND bundle_uri set
tokenized_uri:        str | None     # always null at skeleton stage
```

Split assignment is deterministic: `sha256(eval_run_id)`, first byte buckets to train (~80%), val (~10%), test (~10%).

`corpus_row_id`, `hidden_oracle`, raw `challenge_id`, and `cathedral_signature` NEVER appear in catalog rows. Only `challenge_id_public` (read through to the score_record when needed) is safe to surface. Source artifact hashes (`package_blake3`, `cathedral_signature`) stay on the score_record itself, reachable via `score_uri`.

## Training export schemas

Three output JSONL files, one per training format.

### `sft_success.jsonl`: `cathedral.v3.sft_success/1`

```
schema:                "cathedral.v3.sft_success/1"
eval_run_id:           str
task_type:             str
prompt:                str           # placeholder until bundle parser lands
completion:            str           # placeholder until bundle parser lands
source_bundle_uri:     str | None
source_score_uri:      str
source_manifest_uri:   str | None
weighted_score:        float
```

Only rows with `distillation_ready=true` end up here. Hidden oracle fields MUST NOT appear in this file.

### `failure_analysis.jsonl`: `cathedral.v3.failure_analysis/1`

**PRIVATE: contains hidden_oracle. Never share outside private storage.**

```
schema:                "cathedral.v3.failure_analysis/1"
eval_run_id:           str
task_type:             str
failure_reason:        str | null
claim:                 dict | null
hidden_oracle:         dict          # PRIVATE: culprit + line range + keywords
source_bundle_uri:     str | None
source_score_uri:      str
weighted_score:        float
```

Used by Cathedral operators to triage failure modes. The oracle is included so the analyst can compare the miner's claim against ground truth. This file is the only export that carries oracle data.

### `rm_pairs.jsonl`: `cathedral.v3.rm_pairs/1`

```
schema:                "cathedral.v3.rm_pairs/1"
challenge_id_public:   str
chosen:                { eval_run_id, weighted_score, source_score_uri }
rejected:              { eval_run_id, weighted_score, source_score_uri }
```

Pairs are formed within a `challenge_id_public` group, where two eval_runs scored on the same challenge. A pair is emitted only when `chosen.weighted_score - rejected.weighted_score >= 0.25`. Hidden oracle fields MUST NOT appear in this file.

## Scanner usage

```
python scripts/v3_catalog_scan.py \
    --score-dir /path/to/score_records \
    --out /path/to/catalog.jsonl \
    [--score-uri-prefix s3://bucket/score-records] \
    [--bundle-uri-prefix s3://bucket/eval-artifacts]
```

The scanner reads every `*.score_record.json` under `--score-dir`, builds one catalog row per record, and appends to `--out`. Records that fail validation are skipped with a warning on stderr and counted in the final summary. Exit code is non-zero if zero records were written.

The scanner is intended for manual / batch use by operators. It is not wired into CI and does not run automatically.

## What this is NOT

- Not yet wired to a tokenizer. `prompt` and `completion` in `sft_success.jsonl` are placeholder sentinels (`<<PROMPT_NOT_YET_PARSED>>`) until a Hermes-bundle parser lands.
- Not a DPO export. RM pairs are the closest equivalent right now; DPO needs preference-ranked completions that the bundle parser will surface.
- Real corpus rows are not in this repo. The catalog references them by id only.
- Not gated on the public v3 feed flag. v3 weight stays 0 on mainnet; this whole skeleton is private-only.
