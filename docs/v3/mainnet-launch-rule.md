# v3 bug_isolation_v1: mainnet launch rule

The mainnet v3 feed flag (`CATHEDRAL_V3_FEED_ENABLED`) may turn on under the following conditions, and only under these conditions.

## Permitted on mainnet ONLY when ALL of these hold

1. The full testnet E2E checklist (`docs/v3/testnet-e2e-checklist.md`) has passed end-to-end.
2. Mainnet burn stays high: `forced_burn_percentage = 95.0` in `config/mainnet.toml`.
3. Mainnet v3 weight stays zero: `v3_bug_isolation_weight = 0.0` in `config/mainnet.toml`.
4. No code path raises v3 weight above 0 on mainnet under any env var.

When all four hold, mainnet may flip `CATHEDRAL_V3_FEED_ENABLED=true` to begin collecting packages from real miners. No emissions move to v3 yet.

## Meaningful v3 emissions remain blocked until

- A harder private corpus is in place (current corpus is too small / too easy to separate miners on).
- 24 hours of clean package collection on mainnet: every active miner has at least one full Hermes package + score sidecar in private storage, no upload failures, no public-feed leaks observed.

## Rollback

If a public-feed leak is observed, an oracle value appears in any public surface, or the score sidecar upload fails on more than 5% of evals over a 1-hour window:

1. Set `CATHEDRAL_V3_FEED_ENABLED=false` on mainnet publisher.
2. Restart publisher.
3. File an incident.
4. Do not re-enable until the root cause has a fix and a regression test.
