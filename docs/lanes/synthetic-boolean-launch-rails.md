# Synthetic Boolean Launch Rails

These rails define the SAT launch sequence and public/private boundary for `synthetic_boolean_v1`.

## Status

The codebase includes the SAT lane. Mainnet SAT remains disabled until deploy, publisher enablement, and an explicit validator-local task-family weight change.

## Launch Model

The first launch model is one active formula.

1. Operator activates one SAT formula.
2. All eligible miners race the same formula.
3. Cathedral runs each miner through the SSH/Hermes path.
4. Miners return a JSON object with `dimacs_solution`.
5. Cathedral records a hash-only receipt as soon as Hermes stdout returns.
6. Cathedral verifies the assignment deterministically.
7. The first-submitted valid receipt wins the SAT lane score; the publisher then advances the active challenge.
8. Later valid receipts for that locked challenge score `0.0`.
9. Operator advances to the next formula.

The winner ordering rule is **first submitted among valid receipts**, not first verified. The publisher records receipt time before trace collection finishes, then resolves receipts as valid or invalid. A later valid receipt cannot win while an earlier receipt is still unresolved; it wins only if all earlier receipts resolve invalid or expired.

Reward shape:

- The winner earns the SAT lane score (binary `1.0`).
- Chain impact depends on the validator-side SAT lane weight and the configured burn policy. On mainnet today: `task_family_weights = { synthetic_boolean_v1 = 0.0 }` and `forced_burn_percentage = 95.0`. Until that weight is intentionally moved off zero on a controlled testnet pass, "winning SAT" does not move chain weight on mainnet.
- This is not a winner-takes-all TAO payout. The SAT lane is the first Task Family lane plugged into the publisher-scored pipeline, not a replacement for the existing agent pipeline.

This is not per-miner hidden challenges. It is not a public formula feed. It is not a pool where every correct late answer earns weight.

## Miner Migration

Migration is additive. Existing miners keep the current agent pipeline running while they prepare SAT.

1. Keep the existing hotkey and registered agent path live.
2. Stand up a SAT wrapper on a reachable Linux host.
3. Install Hermes for the SSH user Cathedral will invoke.
4. Run local toy DIMACS checks before exposing the host.
5. Register the host, SSH user, display name, hotkey, and hardware line with Cathedral operators.
6. Enter shadow SAT rounds while `synthetic_boolean_v1` remains weight `0.0`.
7. Enter scored SAT rounds only after the feed, verifier, and validator-local weight path are stable.

The public miner contract is the answer shape, the hotkey identity, and the host reachability check. Solver source, solver strategy, logs, private benchmark data, and infrastructure details are not public repo material.

## Miner Contract

Cathedral sends a DIMACS CNF challenge through Hermes. The miner may use any private solver or wrapper on their own infrastructure.

The final answer must be one fenced `FINAL_ANSWER` JSON block:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Rules:

- The JSON object has exactly one key: `dimacs_solution`.
- The value uses solver-style DIMACS output.
- `s SATISFIABLE` is required for a positive score.
- `v` lines must assign every variable and end with `0`.
- Explanations, logs, source code, extra keys, and assignment dictionaries are not accepted.

## Scoring

SAT scoring is binary:

- `1.0`: well-formed satisfying assignment for the active formula.
- `0.0`: malformed, incomplete, contradictory, out-of-range, unsatisfied, non-SAT, missing, late, or verifier-error result.

The verifier evaluates clauses directly. It does not rely on miner claims about correctness.

## Public Feed Boundary

Allowed public fields for SAT rows:

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

Forbidden public fields:

```text
task_id
public_input
hidden_metadata
formula
cnf
dimacs
assignment
solution
dimacs_solution
bundle_url
manifest_url
score_record_url
```

The public feed is hash-only. Raw CNF and submitted solutions must not appear in public API responses, website state, static JSON, logs intended for publication, or repo docs.

## Validator Boundary

- Validators use the existing local scoring and weight loop.
- SAT task-family weight stays `0.0` unless an operator is doing controlled local testing or a release intentionally changes it.
- Validators pull signed eval rows and verify `cathedral_signature` with `CATHEDRAL_PUBLIC_KEY_HEX`.
- Validators do not receive raw CNF, submitted solutions, or private corpus material.

## Operator Sequence

1. Confirm SAT code is merged and deployed.
2. Confirm publisher env enables the task-family feed.
3. Activate one formula from operator-controlled private storage.
4. Confirm miner prompts are delivered through `SshHermesRunner`.
5. Confirm the first-submitted valid receipt locks the active challenge.
6. Confirm public feed rows are hash-only.
7. Confirm validators pull signed rows and keep SAT task-family weight at the intended value.
8. Confirm validator logs show coherent local weight setting.
9. Advance to the next formula only after the current challenge is locked or retired.

## Environment Gates

Publisher:

```bash
CATHEDRAL_EVAL_MODE=ssh-probe
CATHEDRAL_PROBER_VERSION=v2
CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true
CATHEDRAL_TASK_FAMILY_IDS=synthetic_boolean_v1
```

Validator local testing:

```bash
CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON='{"synthetic_boolean_v1": 0.0}'
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT=0.0
```

## Leak Checks

The public repo must not contain real `.cnf`, `.dimacs`, or `.sol` files.

Run before pushing docs or launch changes:

```bash
find . -name '*.cnf' -o -name '*.dimacs' -o -name '*.sol'
rg -n "dimacs_solution|public_input|hidden_metadata|bundle_url|manifest_url|score_record_url" README.md docs src/cathedral/publisher/skill_md.py
```

Expected result:

- No real formula or solution files.
- `dimacs_solution` appears only in miner contract docs, verifier code, fixtures, or tests.
- Public-feed docs list forbidden fields as forbidden, not as emitted fields.

## Website And Static Surfaces

Do not claim live SAT metrics from static text. Live claims require deployed `state.json` or publisher state. Demo views must be labeled as demo.
