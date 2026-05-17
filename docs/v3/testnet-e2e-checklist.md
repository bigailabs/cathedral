# v3 bug_isolation_v1: testnet E2E checklist

Run end-to-end on testnet (SN292, not mainnet) before considering the v3 feed flag on mainnet.

## Prerequisites

- [ ] Controlled testnet miner registered on SN292
- [ ] SSH probe key installed on the miner box
- [ ] Hermes installed on the miner box (`hermes --version` returns)
- [ ] LLM inference API key wired into Hermes (HERMES_INFERENCE_PROVIDER + HERMES_INFERENCE_MODEL)
- [ ] Private corpus mounted on the publisher host
- [ ] `CATHEDRAL_V3_CORPUS_PATH` set on the publisher to the mounted corpus
- [ ] `CATHEDRAL_V3_FEED_ENABLED=true` on testnet publisher ONLY (do not set on mainnet)
- [ ] `CATHEDRAL_V3_BUG_ISOLATION_WEIGHT=0.05` on testnet validator ONLY (do not set on mainnet)
- [ ] `CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true` so the trace bundle actually uploads to private storage

## Rollout

- [ ] Run one v3 eval round against the controlled miner
- [ ] Verify exactly one signed v3 row landed in publisher DB
- [ ] Verify the validator pull loop accepts that v3 row (re-canonicalize + signature check passes)
- [ ] Verify the package was uploaded to private storage (Hippius eval-artifacts/ key exists)
- [ ] Verify the score sidecar JSON was uploaded (eval-artifacts/<eval_id>.score_record.json)
- [ ] Verify the public feed `/v1/leaderboard/recent` does NOT include `culprit_file`, `culprit_symbol`, `line_range`, `required_failure_keywords`, `bundle_url`, `manifest_url`, `score_record_url`, or `package_blake3` for the v3 row

## Public feed leak check

```
curl https://testnet-publisher/v1/leaderboard/recent | jq '.[] | select(.task_type == "bug_isolation_v1")'
```

The response must contain only: `task_type`, `challenge_id_public`, `claim` (miner's submitted answer), `failure_reason`, `worker_owner_hotkey`, signed-subset fields, signature, schema_version.

## Bundle integrity check

Pull the bundle by manifest URI, re-hash, confirm `blake3` matches the value embedded in the manifest body.

## If any step fails

Do not proceed to the mainnet launch rule. Fix the issue, re-run from the top.
