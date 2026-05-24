# SAT Operations Runbook

Cathedral operations runbook for `synthetic_boolean_v1`.

No nonzero SAT weight before full E2E.

## Rule

One active formula.

1. Operator activates CNF.
2. Eligible miners race it.
3. Cathedral runs miners through SSH and Hermes.
4. Miners return `dimacs_solution`.
5. Receipt time is recorded when stdout returns.
6. Cathedral verifies every clause.
7. First submitted valid receipt wins.
8. Later answers score `0.0`.
9. Operator advances the formula.

This is first submitted valid, not first verified.

## Public Boundary

Allowed:

```text
miner_hotkey
task_type
task_id_public
difficulty_tier
weighted_score
score_parts
answer_hash
verifier_details_hash
rejection_reason
ran_at
eval_output_schema_version
cathedral_signature
```

Forbidden:

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

Public SAT rows are hash-only.

## Publisher Gates

```bash
CATHEDRAL_EVAL_MODE=ssh-probe
CATHEDRAL_PROBER_VERSION=v2
CATHEDRAL_TASK_FAMILY_FEED_ENABLED=true
CATHEDRAL_TASK_FAMILY_IDS=synthetic_boolean_v1
CATHEDRAL_PUBLIC_BASE_URL=https://api.cathedral.computer
```

For file-backed formulas:

```bash
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE=file
cathedral sat-seed-challenge --cnf-path /private/active.cnf --storage-mode file --activate
```

Preflight:

```bash
cathedral-publisher sat-launch-preflight
cathedral-publisher sat-active-cnf-probe \
  --db data/publisher.db \
  --public-base-url https://api.cathedral.computer
```

Shadow only:

```bash
cathedral-publisher sat-launch-preflight --no-require-weight-signing-key
```

## Validator Gates

Keep local SAT blending zero until launch:

```bash
CATHEDRAL_TASK_FAMILY_WEIGHTS_JSON='{"synthetic_boolean_v1": 0.0}'
CATHEDRAL_SYNTHETIC_BOOLEAN_V1_WEIGHT=0.0
```

### SAT-only cutover toggle (publisher)

When SAT lane goes live with nonzero weight and legacy v1 ranked
submissions should stop contributing immediately, set on the publisher
(not the validator):

```bash
CATHEDRAL_WEIGHT_POLICY_DISABLE_LEGACY_BASE_SCORES=true
```

This makes `latest_policy_scores_by_hotkey` skip the
`agent_submissions` ranked-score query entirely. Only schema-5 Task
Family rows feed the signed vector. Validators read the resulting
weights as normal; the toggle only changes what the publisher signs.

Verify after restart by inspecting `policy_metadata.score_source` on
the next signed vector returned from `/v1/validator/weights/next`:

* `agent_submissions.current_score+configured_task_family_rows` — flag off (default)
* `configured_task_family_rows` — flag on

Remote weight opt-in:

```toml
[remote_weight_source]
enabled = true
url = "https://api.cathedral.computer"
key_id = "cathedral-weight-policy"
public_key_env = "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX"
```

Pinned key:

```bash
export CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX=8d74453ac008cc7be3f0609b43d31aa4096ab4a6ded32b9e754a5c48360938fd
```

Preflight:

```bash
cathedral-publisher remote-weight-vector-preflight --db data/publisher.db
cathedral-validator sat-launch-preflight --config config/mainnet.toml
cathedral-validator chain-launch-preflight --config config/mainnet.toml
cathedral-validator verify-remote-weight-vector --config config/mainnet.toml
```

## E2E Gate

Do not raise SAT weight until this passes:

- CNF URL serves through the public path.
- Miner verifies CNF SHA-256.
- Miner returns exact `FINAL_ANSWER`.
- Publisher records receipt before trace collection.
- Publisher signs schema-5 row.
- Validator verifies signed row.
- Remote vector verifies.
- Chain preflight passes.
- Public feed is hash-only.

## Leak Check

```bash
find . -name '*.cnf' -o -name '*.dimacs' -o -name '*.sol'
rg -n "dimacs_solution|public_input|hidden_metadata|bundle_url|manifest_url|score_record_url" README.md docs src/cathedral/publisher/skill_md.py
```

Expected:

- no real formula files
- no raw solutions in public docs
- forbidden fields only appear as forbidden
