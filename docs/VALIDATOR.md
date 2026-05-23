# Validator Mechanism

Day-2 commands live in [validator/RUNBOOK.md](validator/RUNBOOK.md).

Validators verify signed Cathedral data, map hotkeys to UIDs, and set Bittensor weights.

## Default Mode

1. Poll `GET /v1/leaderboard/recent`.
2. Verify rows with `CATHEDRAL_PUBLIC_KEY_HEX`.
3. Store accepted rows locally.
4. Map hotkeys to current UIDs.
5. Compute weights locally.
6. Apply burn policy.
7. Call `set_weights`.

If `CATHEDRAL_PUBLIC_KEY_HEX` is missing, the validator logs `pull_loop_disabled`.

## Remote Weight Mode

Remote signed weights are opt-in.

1. Fetch `/v1/validator/weights/next`.
2. Verify signature with `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX`.
3. Check `key_id`, network, netuid, expiry, rollback, and burn policy.
4. Cache the last accepted vector.
5. Relay the vector in the weight loop.

If the pinned key is missing or invalid, startup fails closed.

## SAT Rows

SAT rows use `eval_output_schema_version = 5`.

Allowed public fields:

- miner hotkey
- task id hash
- answer hash
- verifier details hash
- score
- rejection reason
- timestamp
- Cathedral signature

Forbidden public fields:

- raw CNF
- DIMACS solution
- hidden metadata
- private challenge material
- trace URL
- manifest URL
- private score material

Validators verify the signature. They do not receive raw formulas in v1.

## Required Inputs

Default mode:

- validator wallet
- `CATHEDRAL_BEARER`
- `CATHEDRAL_PUBLIC_KEY_HEX`
- `polaris.public_key_hex`

Remote mode also needs:

- `[remote_weight_source].enabled = true`
- `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX`

No GPU is required.
