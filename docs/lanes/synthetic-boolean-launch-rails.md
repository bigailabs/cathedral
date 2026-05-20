# Synthetic Boolean Launch Rails

These rails define the public SAT launch boundary for `synthetic_boolean_v1`.

## Status

The public repo includes the SAT lane, toy fixtures, token-gated CNF URL
transport, and a durable first-verified lock. Mainnet SAT remains disabled:
validator SAT weight defaults to `0.0`, no production CNFs ship in this repo,
and operators must not enable a nonzero mainnet SAT weight from this branch.

The rules are public. Actual challenge CNFs are private until announcement.
When a challenge is announced, eligible miners receive a `cnf_url` with an
embedded unguessable token and a `cnf_sha256` integrity hash. The public feed
still remains hash-only.

## Launch Model

The first launch model is one active formula.

1. Operator activates one SAT formula from private publisher storage.
2. All eligible miners race the same active formula.
3. Cathedral sends challenge metadata through the SSH/Hermes path.
4. Miners fetch the token-gated CNF URL and verify `cnf_sha256`.
5. Miners return a JSON object with `dimacs_solution`.
6. Cathedral verifies the assignment deterministically.
7. The first answer Cathedral verifies and locks wins the SAT lane score.
8. Later submissions for that locked challenge score `0.0`.
9. The publisher advances to the next pending challenge.

The winner ordering rule is first verified and locked. This branch does not
change that rule into true first-submitted ordering.

This is not per-miner hidden challenges. It is not a public formula feed. It is
not a pool where every correct late answer earns weight.

## Miner Contract

Cathedral sends challenge metadata through Hermes. Under the URL transport,
`public_input` contains:

```json
{
  "format": "dimacs",
  "cnf_url": "https://api.cathedral.computer/v1/challenges/<id>/cnf?t=<token>",
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
- Miners must not log the token-bearing CNF URL.

## CNF URL Transport

The CNF route is:

```text
GET /v1/challenges/{challenge_id}/cnf?t={token}
GET /api/cathedral/v1/challenges/{challenge_id}/cnf?t={token}
```

The endpoint returns `200 text/plain` only when:

- a fetch-token row exists for the challenge
- the query token matches in constant time
- the challenge is active, or locked and still inside the short grace window

Every miss path returns the same `404 {"detail": "challenge_not_found"}` body.
Unknown IDs, missing tokens, wrong tokens, pending rows, retired rows, and
locked rows past grace are intentionally indistinguishable.

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
fetch_token
bundle_url
manifest_url
score_record_url
```

Raw CNF, token-bearing URLs, submitted solutions, hidden metadata, generator
details, private corpus names, and private filenames must not appear in public
API responses, website state, static JSON, logs intended for publication, or
repo docs.

## Environment Gates

Publisher smoke path:

```bash
CATHEDRAL_EVAL_MODE=ssh-probe
CATHEDRAL_PROBER_VERSION=v2
CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true
CATHEDRAL_TASK_FAMILY_IDS=synthetic_boolean_v1
CATHEDRAL_PUBLIC_BASE_URL=https://api.cathedral.computer
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH=/path/to/operator-mounted.cnf
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
5. Confirm token-gated fetch succeeds for eligible miners.
6. Confirm the first verified solution locks the active challenge.
7. Confirm later correct submissions for the same challenge score `0.0`.
8. Confirm public feed rows are hash-only.
9. Keep validator SAT weight at `0.0` until a separate release intentionally changes it.

## Leak Checks

Run before pushing SAT changes:

```bash
find . -name '*.cnf' -o -name '*.dimacs' -o -name '*.sol'
rg -n "cnf_url|cnf_sha256|fetch_token|dimacs_solution|public_input|hidden_metadata|bundle_url|manifest_url|score_record_url" README.md docs src tests
```

Expected result:

- No real formula or solution files.
- `dimacs_solution` appears only in miner contract docs, verifier code, fixtures, or tests.
- `cnf_url`, `cnf_sha256`, and `fetch_token` appear only in transport docs, prompt code, endpoint code, or tests.
- Public-feed docs list forbidden fields as forbidden, not as emitted fields.
