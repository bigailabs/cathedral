# synthetic_boolean_v1 launch rails

This lane is the first Task Family target for boolean work. The platform
surface is ready for a lane implementation, but the mainnet feed and
validator weight stay off until the lane author lands `generate`, `verify`,
`score`, fixtures, and registration.

## Product posture

- Miners still run Hermes. Cathedral SSHs into the miner box, starts a
  task-scoped Hermes profile, sends the boolean prompt, captures stdout,
  and bundles the Hermes trace.
- Miners are paid for verified solutions. The trace is sidecar data for
  provenance, anomaly review, and later training export. It is not a scoring
  input unless a lane explicitly defines a deterministic trace requirement.
- Public feeds do not expose raw formulas, hidden metadata, or submitted
  answers. Public rows carry only `task_id_public`, `answer_hash`,
  `verifier_details_hash`, score fields, signature, and schema version.
- The generator, seeds, hidden metadata, unreleased formulas, and raw
  solutions stay publisher-private until an explicit reveal or export path
  is added.

## Current code path

1. `EvalOrchestrator.evaluate_one` runs the normal v1 card eval.
2. If `CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true`, the orchestrator calls
   `_maybe_run_task_family_lanes`.
3. The lane is looked up in `cathedral.lanes.registry`.
4. `lane.generate(ctx)` produces a `PublicProblem` and `HiddenMetadata`.
5. `SshHermesRunner.run_task_family_challenge` sends the prompt to the
   miner's Hermes process and returns stdout plus the trace bundle.
6. `cathedral.lanes.publisher.score_and_sign_task_family_stdout` extracts
   the final JSON answer, calls `lane.verify`, calls `lane.score`, and signs
   a schema 5 row.
7. `persist_task_family_result` stores the private task material and trace
   sidecar in the publisher DB while publishing only hash-backed fields.
8. Validators pull schema 5 rows and blend them only if a nonzero
   task-family weight is configured.

## Environment gates

Publisher:

```bash
CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true
CATHEDRAL_TASK_FAMILY_IDS=synthetic_boolean_v1
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER=0
```

Validator:

```bash
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT=0.0
```

Alternative validator weight override:

```bash
CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON='{"synthetic_boolean_v1": 0.05}'
```

Mainnet default remains weight `0.0`. A nonzero weight should first be
tested on SN292 with one controlled miner and one signed schema 5 row.

## Launch checklist

- [ ] `src/cathedral/lanes/synthetic_boolean_v1/` implements the contract.
- [ ] `PYTHONPATH=src pytest tests/lanes/test_contract.py -k synthetic_boolean_v1 -v`
      is green with no behavioural skips for the lane.
- [ ] The lane is registered in `src/cathedral/lanes/registry.py`.
- [ ] A controlled miner has Hermes installed and the Cathedral SSH key
      configured.
- [ ] Publisher feed gate is enabled only on testnet.
- [ ] Validator weight is `0.0` for first smoke, then optionally `0.05` on
      testnet after signature pull succeeds.
- [ ] `/v1/leaderboard/recent` contains no raw formula, raw assignment,
      hidden metadata, generator seed, bundle URL, manifest URL, or private
      score material for schema 5 rows.
- [ ] Validator logs show the schema 5 row was accepted and `weights_pre_burn`
      remains coherent.

## Public leak check

```bash
curl https://testnet-publisher/v1/leaderboard/recent \
  | jq '.items[] | select(.task_type == "synthetic_boolean_v1")'
```

Allowed fields:

```text
id
agent_id
agent_display_name
miner_hotkey
task_type
task_id_public
epoch_salt
difficulty_tier
weighted_score
score_parts
answer_hash
verifier_details_hash
rejection_reason
ran_at
eval_output_schema_version
cathedral_signature
merkle_epoch
```

Forbidden fields:

```text
task_id
public_input
hidden_metadata
seed
generator_state
formula
assignment
solution
bundle_url
manifest_url
score_record_url
```

## Failure posture

- Unregistered lane: log `task_family_skipped reason=unregistered`.
- Stub lane: log `task_family_skipped reason=stub`.
- Malformed miner stdout: signed zero row with `rejection_reason=malformed_answer`.
- Verifier exception: signed zero row with `rejection_reason=verifier_error`.
- Scorer exception: signed zero row with `rejection_reason=scorer_error`.
- Validator unknown schema version: row rejected.

## What the lane author still owns

- Exact boolean formulation.
- Problem schema and answer schema.
- Deterministic generator or private corpus.
- Deterministic verifier.
- Binary or partial scoring policy.
- Golden and adversarial fixtures.
- Difficulty calibration.

Validator wiring, chain weights, Hermes transport, signing, public feed shape,
and private trace capture are platform-owned.
