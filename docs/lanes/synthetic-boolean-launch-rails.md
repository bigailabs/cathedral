# Synthetic Boolean Launch Rails

These rails define the public SAT launch boundary for `synthetic_boolean_v1`.

## Status

The public repo includes the SAT lane, toy fixtures, authorized CNF URL
transport, and durable first-submitted receipt ordering. Mainnet SAT remains
disabled: validator SAT weight defaults to `0.0`, no production CNFs ship in
this repo, and operators must not enable a nonzero mainnet SAT weight from
this branch.

The rules are public. Actual challenge CNFs are private until announcement.
When a challenge is announced, eligible miners receive a `cnf_url` exactly as
it should be fetched and a `cnf_sha256` integrity hash. The public feed still
remains hash-only.

## Launch Model

The first launch model is one active formula.

1. Operator activates one SAT formula from private storage.
2. All eligible miners race the same active formula.
3. Cathedral sends challenge metadata through the SSH/Hermes path.
4. Miners fetch the CNF URL exactly as given and verify `cnf_sha256`.
5. Miners return a JSON object with `dimacs_solution`.
6. Cathedral records a publisher receipt timestamp when Hermes stdout returns.
7. Cathedral verifies the assignment deterministically.
8. The earliest valid receipt wins the SAT lane score.
9. Later valid receipts for that challenge score `0.0`.
10. Earlier invalid or expired receipts do not block a later valid receipt.
11. The publisher advances to the next pending challenge.

The winner ordering rule is first submitted by publisher receipt time, after
verification proves the answer valid. A later receipt that verifies faster must
wait while any earlier receipt is still `unverified` or `verifying`. The
selector can finalize only when the earliest unresolved receipts are invalid or
expired, or when the earliest unresolved receipt becomes valid and wins.

This is not per-miner hidden challenges. It is not a public formula feed. It is
not a pool where every correct late answer earns weight.

## Miner Contract

Cathedral sends challenge metadata through Hermes. Under the URL transport,
`public_input` contains:

```json
{
  "format": "dimacs",
  "cnf_url": "<authorized HTTPS URL>",
  "cnf_sha256": "<sha256>",
  "num_vars": 3,
  "num_clauses": 2
}
```

The miner may use any private solver or wrapper on their own infrastructure.
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
- Miners must not log the authorized CNF URL.

## CNF URL Transport

Miners fetch `public_input.cnf_url` exactly as given. The transport returns
`200 text/plain` only for an authorized, currently available challenge. Every
miss path returns the same `404 {"detail": "challenge_not_found"}` body.
Unknown IDs, missing authorization, unavailable rows, and expired rows are
intentionally indistinguishable.

## Scoring

SAT scoring is binary:

- `1.0`: well-formed satisfying assignment for the active formula.
- `0.0`: malformed, incomplete, contradictory, out-of-range, unsatisfied, non-SAT, missing, late, or verifier-error result.

The verifier evaluates clauses directly. It does not rely on miner claims about
correctness.

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
cnf_url
cnf_sha256
bundle_url
manifest_url
score_record_url
```

Raw CNF, authorized fetch URLs, submitted solutions, hidden metadata, private
construction details, private corpus names, and private filenames must not
appear in public API responses, website state, static JSON, logs intended for
publication, or repo docs.

## Environment Gates

Publisher smoke path:

```bash
CATHEDRAL_EVAL_MODE=ssh-probe
CATHEDRAL_PROBER_VERSION=v2
CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true
CATHEDRAL_TASK_FAMILY_IDS=synthetic_boolean_v1
CATHEDRAL_PUBLIC_BASE_URL=https://api.cathedral.computer
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH=/path/to/private-input
```

Validator:

```bash
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT=0.0
```

Alternative validator weight override for local tests only:

```bash
CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON='{"synthetic_boolean_v1": 0.0}'
```

## Operator Sequence

1. Confirm SAT code is merged and deployed.
2. Confirm publisher env enables the task-family feed on a controlled surface.
3. Activate one formula from operator-controlled private storage.
4. Confirm miner prompts contain `cnf_url` and `cnf_sha256`, not inline CNF.
5. Confirm authorized fetch succeeds for eligible miners.
6. Confirm the earliest valid receipt locks the active challenge, even if a
   later receipt verifies first.
7. Confirm later correct submissions for the same challenge score `0.0`.
8. Confirm public feed rows are hash-only.
9. Keep validator SAT weight at `0.0` until a separate release intentionally changes it.

## Leak Checks

Run before pushing SAT changes:

```bash
find . -name '*.cnf' -o -name '*.dimacs' -o -name '*.sol'
rg -n "cnf_url|cnf_sha256|dimacs_solution|public_input|hidden_metadata|bundle_url|manifest_url|score_record_url" README.md docs src tests
```

Expected result:

- No real formula or solution files.
- `dimacs_solution` appears only in miner contract docs, verifier code, fixtures, or tests.
- `cnf_url` and `cnf_sha256` appear only in transport docs, prompt code, endpoint code, or tests.
- Public-feed docs list forbidden fields as forbidden, not as emitted fields.
