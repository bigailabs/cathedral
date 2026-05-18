# synthetic_boolean_v1 -- data collection integration

How the SAT lane reuses existing Cathedral plumbing for Hermes traces,
bundle publishing, dataset catalog, and private sidecars. **This PR does
not implement the wiring** -- it documents the integration seam so the
next PR can land without re-inventing v3 infrastructure.

## Goal

The lane scoring contract (`generate -> verify -> score`) is pure and
binary 1.0/0.0. Everything beyond binary scoring -- the prompt, the
Hermes agent trace, the miner stdout, the request dump -- is collected
as a **sidecar** for provenance, debugging, and downstream training
dataset value. The sidecar must never affect the v1 score; that gate
stays in `verify`/`score`.

## What gets collected per run

| field                    | source                                     | destination          |
| ------------------------ | ------------------------------------------ | -------------------- |
| `task_id`                | `PublicProblem.task_id`                    | wire + sidecar       |
| `task_family`            | constant `"synthetic_boolean_v1"`          | wire + sidecar       |
| `difficulty_tier`        | `PublicProblem.difficulty_tier`            | wire + sidecar       |
| `weighted_score`         | `ScoreResult.weighted_score`               | wire + sidecar       |
| `rejection_reason`       | `ScoreResult.rejection_reason`             | wire + sidecar       |
| `prompt_sent_to_miner`   | publisher orchestrator                     | sidecar only         |
| `miner_stdout`           | `TraceBundle.hermes_stdout`                | sidecar only         |
| `solver_output_excerpt`  | parsed from miner answer (truncated)       | wire (excerpt) + sidecar (full) |
| `hermes_trace_bundle`    | `SshHermesRunner.run()` -> `TraceBundle`   | sidecar only (encrypted) |
| `manifest_url`           | `EvalArtifactPublisher` -> manifest hash   | sidecar only         |
| `bundle_blake3`          | `TraceBundle.bundle_blake3`                | wire + sidecar       |
| `hidden_metadata`        | `HiddenMetadata` (planted assignment)      | sidecar only, PRIVATE storage |
| `lifecycle_state`        | `ChallengeRecord.state`                    | wire + sidecar       |
| `dataset_catalog_row_id` | `cathedral.v3.datasets.catalog`            | sidecar              |

**Wire row** is what the validator pulls from the publisher feed -- it
never carries hidden metadata or the full trace. **Sidecar** lives in
private operator storage (Hippius/S3) under publisher-only credentials.

## Plumbing reuse

### SSH Hermes runner

`src/cathedral/eval/ssh_hermes_runner.py` already captures everything
we need:

* `_invoke_hermes(...)` returns `(card_json, hermes_stdout)`
* `_collect_bundle(...)` builds a `TraceBundle` with `state.db`,
  `sessions/`, `memories/`, `skills/`, `logs/`, `hermes_stdout.txt`,
  manifest, and `bundle_blake3`
* `TraceBundle.bundle_blake3` is the integrity anchor

For SAT, the publisher orchestrator hands the runner a prompt built
from `PublicProblem.public_input["dimacs"]` plus the instructions
field. The runner returns a `TraceBundle` exactly the same shape as
the bug-isolation lane uses today.

**Reuse, do not rewrite.** The runner is generic over the prompt; it
does not need to know the prompt is DIMACS.

### Bundle publisher

`src/cathedral/eval/bundle_publisher.py` defines `EvalArtifactPublisher`
and `canonical_manifest_bytes` / `blake3_hex`. The SAT lane uses these
unchanged:

```python
artifact = publisher.publish_bundle(trace_bundle)
sidecar["manifest_url"] = artifact.manifest_url
sidecar["bundle_blake3"] = artifact.bundle_blake3
```

### Private sidecar pattern

`src/cathedral/v3/score_sidecar.py` is the existing template. The SAT
sidecar follows the same shape:

```python
{
  "schema": "cathedral.lanes.synthetic_boolean_v1.score_record/1",
  "eval_run_id": str,
  "miner_hotkey": str,
  "task_family": "synthetic_boolean_v1",
  "task_id": str,
  "difficulty_tier": int,
  "scorer_version": "synthetic_boolean_v1/dimacs-sat/planted/1",
  "cathedral_commit": str,
  "ran_at": str,  # ISO timestamp
  "duration_ms": int,
  "weighted_score": float,
  "rejection_reason": str | None,
  "score_parts": dict,
  "hidden_metadata": {                  # PRIVATE -- never on wire
    "planted_assignment": {...},
    "num_vars": int,
    "num_clauses": int
  },
  "trace_bundle": {                     # PRIVATE -- never on wire
    "bundle_blake3": str,
    "manifest_url": str,
    "hermes_stdout_excerpt": str        # truncated
  },
  "lifecycle_state": "scored" | "retired" | "revealed"
}
```

Write it through the same Hippius client v3 uses
(`cathedral.storage.HippiusClient`). Failure to upload the sidecar must
not break scoring -- wrap in try/except as `cathedral.v3.publisher`
already does.

### Dataset catalog

`src/cathedral/v3/datasets/catalog.py` and `export.py` ingest sidecars
into the training-data catalog. The SAT sidecar shape above matches the
catalog's row template; the catalog ingestor needs a small dispatch
addition to route `task_family == "synthetic_boolean_v1"` to a SAT
exporter (`v3/datasets/export.py` already has a per-task switch).

This is **deferred to the wiring PR** -- the v1 lane PR just produces
the sidecar; the catalog reads it later.

## Integration seam (next PR)

The publisher's `score_and_sign` dispatch (today gated by
`CATHEDRAL_SCORER=v2` / `CATHEDRAL_V3_FEED_ENABLED`) needs a new branch:

```python
if task_family == "synthetic_boolean_v1":
    lane = registry.lookup("synthetic_boolean_v1")
    public, hidden = lane.generate(GenerateCtx(...))
    # prompt = build_dimacs_prompt(public)
    # trace = await ssh_hermes_runner.run(prompt)
    # submission = parse_submission_from_trace(trace)
    # verifier_result = lane.verify(public, hidden, submission)
    # score = lane.score(public, verifier_result)
    # write_sidecar(public, hidden, trace, score)
    # sign + persist wire row
```

The hooks are all there. The wiring PR's only real work is:

1. A small `build_dimacs_prompt(public_input)` helper (~20 lines).
2. A small `parse_submission_from_trace(trace)` helper (~20 lines) that
   pulls the fenced JSON block out of `hermes_stdout` like
   `cathedral.eval.scorer_v2_publisher.parse_claim_v2` does today.
3. A `score_and_sign` branch behind a `CATHEDRAL_SAT_LANE_ENABLED` env
   gate (default off) that calls the lane + persists the sidecar.
4. A weight allocation entry (still 0% on mainnet at first).

## What does NOT need to happen

* SSH Hermes is not rewritten.
* The bundle publisher is not rewritten.
* The signing pipeline (`cathedral.v3.sign` / `cathedral.v4.sign`) is
  not rewritten.
* The validator pull loop is not touched -- it pulls signed rows
  generically; a new `task_family` is just another row type.

## Privacy invariants

1. Hidden metadata never leaves the publisher's process boundary
   except through the sidecar upload to **private** storage.
2. Trace bundles are encrypted at rest and never linked from the
   public feed.
3. The public wire row carries only fields in
   `ChallengeRecord.to_public_payload()` plus the signed score.
4. The `lifecycle_state` on the public row goes
   `active -> retired -> revealed`. Reveal is operator-gated; it never
   fires automatically.
