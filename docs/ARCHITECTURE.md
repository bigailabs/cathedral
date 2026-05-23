# Architecture

Cathedral has three actors.

- Miners run private SAT-solving agents.
- The publisher issues challenges, verifies answers, and signs score rows.
- Validators verify signatures, map hotkeys to UIDs, and set weights.

## SAT Flow

1. The publisher activates one private DIMACS CNF.
2. Eligible miners receive a token-gated CNF URL and SHA-256 hash.
3. Each miner runs its own Hermes-driven solver stack.
4. The miner returns one `dimacs_solution`.
5. The publisher records receipt time when stdout returns.
6. The publisher parses DIMACS and checks every clause.
7. The first submitted valid receipt wins the active challenge.
8. The publisher signs a hash-only score row.
9. Validators pull and verify the signed row.
10. Validators submit weights to Bittensor.

## Data Boundary

Public rows are hash-only.

Public:

- miner hotkey
- task type
- task id hash
- answer hash
- verifier details hash
- score
- rejection reason
- timestamp
- Cathedral signature

Private:

- private challenge material
- fetch tokens
- raw DIMACS answers
- execution traces
- private score material
- signing keys

Validators do not receive raw formulas in v1. They verify Cathedral signatures.

## Publisher

The publisher owns challenge selection and answer verification.

Core responsibilities:

- serve authorized CNF downloads
- run miners through SSH and Hermes
- verify DIMACS assignments
- sign public eval rows
- produce optional signed weight vectors

The publisher is verifier-of-record for private SAT challenges.

## Validator

Validators have two weight paths.

Default path:

1. Pull `GET /v1/leaderboard/recent`.
2. Verify each row with `CATHEDRAL_PUBLIC_KEY_HEX`.
3. Store rows locally.
4. Map hotkeys to metagraph UIDs.
5. Compute local weights.
6. Apply burn policy.
7. Call `set_weights`.

Remote signed-weight path:

1. Fetch `/v1/validator/weights/next`.
2. Verify with `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX`.
3. Check key id, network, netuid, expiry, rollback, and burn policy.
4. Relay the cached signed vector.

Remote mode is opt-in. Missing or invalid weight-policy key fails closed.

## Miner

A SAT miner provides:

- registered Bittensor hotkey
- reachable Linux SSH host
- Hermes on `PATH`
- private solver or wrapper

The miner returns only:

````text
```FINAL_ANSWER
{
  "dimacs_solution": "s SATISFIABLE\nv 1 -2 3 0\n"
}
```
````

Solver source, logs, raw CNFs, and raw solutions stay private.

## Persistence

The validator uses SQLite with WAL mode.

Important tables:

- `pulled_eval_runs`: signed publisher rows
- `scores`: legacy local claim scores
- `pull_loop_meta`: backfill cursor state
- `validator_remote_weight_state`: last accepted remote vector

The publisher stores challenge state and signed eval rows. Private challenge material is not part of public API output.

## Trust Model

Cathedral signs what validators consume.

- Eval rows are signed with the Cathedral eval key.
- Remote weight vectors are signed with the Cathedral weight-policy key.
- Validators pin the public keys.
- Unsigned publisher data is not accepted.

This is the v1 trust model. A later public-verification design would be a different lane, not a hidden assumption in this one.

## Omitted By Design

This repo does not publish:

- private challenge material
- solver strategy
- miner execution logs
- raw solution archives
- private scoring material
