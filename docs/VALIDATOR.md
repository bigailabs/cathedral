# Validator Notes

This file explains the validator mechanism. Day-2 commands live in [validator/RUNBOOK.md](validator/RUNBOOK.md).

## Status

The codebase includes the SAT lane. Mainnet SAT remains disabled by config until the publisher is deployed with the SAT feed and operators move validator-local task-family weight above `0.0`.

## Current Default Path

`cathedral-validator serve` runs the existing validator process:

1. Legacy `/v1/claim` worker still boots.
2. Pull loop polls `GET /v1/leaderboard/recent`.
3. Pull loop verifies Cathedral signatures with `CATHEDRAL_PUBLIC_KEY_HEX`.
4. Accepted rows are written to the local validator database.
5. Weight loop computes the local vector, applies burn policy, and calls `set_weights`.
6. Stall watchdog reports stuck background loops.

If `CATHEDRAL_PUBLIC_KEY_HEX` is absent, startup logs `pull_loop_disabled` and the pull loop is skipped.

## SAT Rows

SAT task-family rows use `eval_output_schema_version = 5`.

The signed public row is hash-only. It authenticates:

- Miner hotkey.
- Public task id hash.
- Answer hash.
- Verifier details hash.
- Binary score.
- Rejection reason, if any.
- Timestamp.

It must not expose raw CNF, submitted DIMACS solution, hidden metadata, private corpus material, trace bundle URL, manifest URL, or private score material.

Validators verify schema-aware signed rows through the same pull-loop dispatcher used for other eval rows. Local task-family blending remains weight `0.0` unless an operator intentionally changes config for testing.

## Required Operator Inputs

- Registered validator hotkey.
- `CATHEDRAL_BEARER` for the local `/v1/claim` endpoint.
- `CATHEDRAL_PUBLIC_KEY_HEX` for signed eval-row verification.
- `polaris.public_key_hex` in TOML while the legacy worker still boots.

Validators do not need GPUs, solver infrastructure, raw SAT formulas, submitted solutions, or publisher storage credentials.

## Known Limits

- The publisher remains the source of signed eval rows.
- SAT mainnet is gated until deploy and explicit validator-local weight config.
- Static website text is not evidence of live SAT metrics.
- Losing the validator hotkey or local wallet files is outside Cathedral recovery.

## Verification Checklist

For a SAT launch candidate:

1. Confirm schema-5 rows verify with the pinned Cathedral eval key.
2. Confirm public rows are hash-only.
3. Confirm the first verified solution locks the active challenge (the lock fires after the publisher-side verifier runs, not on submission timestamp).
4. Confirm late solutions do not earn weight for the locked challenge.
5. Confirm validator-local SAT task-family weight is still `0.0` unless the release intentionally changes it.
6. Confirm no live website metric is shown unless backed by deployed `state.json` or marked demo.
