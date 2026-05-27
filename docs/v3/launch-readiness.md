# v3 launch readiness

The goal of this milestone is to start collecting full task-scoped Hermes packages from `bug_isolation_v1` evals on mainnet, with v3 weight = 0 and burn high (85%), so we accumulate trajectory data before any meaningful v3 payouts.

## What ships in this milestone

1. Runner captures the full Hermes package per v3 challenge (state.db slice, sessions, request dumps, memories, skills, logs, prompt, stdout, repair stdout if any).
2. Publisher writes a private `score_record.json` sidecar alongside the package, schema `cathedral.v3.score_record/1`, carrying score parts, oracle, parsed claim, signatures, and bundle hashes.
3. Catalog skeleton (`src/cathedral/v3/datasets/`) for later training export.
4. Public feed remains minimal: no oracle leakage, no bundle URIs, no score record URIs.

## What does NOT ship

- v3 mainnet emissions. `forced_burn_percentage` stays 85%, `v3_bug_isolation_weight` stays 0.0 on mainnet.
- Tokenized SFT/DPO/RM datasets. Export skeleton is real schema, placeholder content.
- Public feed leakage of any oracle / package content. Feed stays minimal.

## Read next

- `docs/v3/testnet-e2e-checklist.md` for the testnet rollout steps.
- `docs/v3/mainnet-launch-rule.md` for when the mainnet feed flag may flip.
- `docs/v3/datasets.md` for the catalog and training export schemas.
