# Validator Runbook

This is the operator runbook for Cathedral validators during the SAT launch transition.

## Status

The codebase includes the SAT lane and remote signed-weight path. Mainnet SAT remains disabled until the publisher is deployed with the SAT feed and validators opt in to remote signed weight vectors.

Until remote weights are enabled, validators keep using the existing local scoring and weight path.

## What The Validator Does

Default path:

1. Poll `GET /v1/leaderboard/recent`.
2. Verify each signed eval row with `CATHEDRAL_PUBLIC_KEY_HEX`.
3. Store accepted rows locally.
4. Blend local score rows into a weight vector.
5. Apply burn policy.
6. Call `subtensor.set_weights`.

Remote-weight path after the SAT release:

1. Poll `GET /v1/validator/weights/next`.
2. Verify the signed weight vector with the pinned weight-policy public key.
3. Reject wrong network, wrong netuid, wrong key id, expired vectors, invalid weights, and policy rollbacks.
4. Map hotkeys to local metagraph uids.
5. Apply the signed burn snapshot.
6. Call `subtensor.set_weights` on the validator's normal cadence.

Remote weights are explicit opt-in. If the key is missing while remote mode is enabled, startup fails instead of silently falling back.

## Prerequisites

- Linux host.
- Python 3.11 or 3.12.
- Bittensor coldkey and hotkey registered on the target subnet.
- Outbound HTTPS to the publisher and subtensor endpoint.
- Local bearer token for the validator's own `/v1/claim` endpoint.
- Cathedral eval-signing public key pinned in env.
- Polaris runtime-attestation public key pinned in TOML while the legacy worker still boots.

No GPU is required. No inbound public port is required.

## Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs `cathedral`, `cathedral-validator`, and `cathedral-miner`.

## Configure

Use `config/mainnet.toml` for SN39 or the testnet config only for protocol development.

Edit:

- `network.name`: subtensor network name.
- `network.netuid`: subnet id.
- `network.validator_hotkey`: local wallet hotkey name, not ss58.
- `network.wallet_name`: local coldkey wallet name.
- `polaris.public_key_hex`: Polaris runtime-attestation public key.
- `publisher.url`: publisher base URL.
- `publisher.public_key_env`: env var containing the Cathedral eval-signing public key.
- `weights.task_family_weights`: keep `synthetic_boolean_v1 = 0.0` unless you are intentionally testing local schema-5 blending.

Required env:

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
export CATHEDRAL_PUBLIC_KEY_HEX=<cathedral-eval-signing-public-key>
```

Optional publisher-read token:

```bash
export CATHEDRAL_PUBLISHER_TOKEN=<token-if-issued>
```

The publisher does not currently require this token for public recent rows.

## Start

```bash
cathedral-validator migrate --config config/mainnet.toml
cathedral chain-check --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```

Production hosts usually run the validator under PM2:

```bash
sudo -u cathedral pm2 status
sudo -u cathedral pm2 logs cathedral-validator --lines 200
sudo -u cathedral pm2 restart cathedral-validator
```

## Enable Remote Weights After The SAT Release

Do this only after the remote-weight release is deployed and the operator has published the weight-policy public key.

The shipped `config/mainnet.toml` and `config/testnet.toml` templates already include a disabled block. Change only `enabled` unless the release notes say otherwise:

```toml
[remote_weight_source]
enabled = true
url = "https://api.cathedral.computer"
key_id = "cathedral-weight-policy"
public_key_env = "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX"
poll_interval_secs = 60.0
request_timeout_secs = 10.0
```

Set the pinned key:

```bash
export CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX=<cathedral-weight-policy-public-key>
```

Then restart:

```bash
cathedral-validator migrate --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```

Expected behavior:

- Startup refuses to run if `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX` is missing or invalid.
- `503` from `/v1/validator/weights/next` means the publisher is up but has no vector yet.
- The validator keeps last accepted remote state and does not apply a rollback.
- If remote mode is disabled, the validator uses local scoring and weight computation.

## Health

```bash
curl -s http://127.0.0.1:9333/health | jq
cathedral health
cathedral weights
cathedral registration
```

Watch:

- `registered`: validator hotkey is on the metagraph.
- `current_block`: chain reads are moving.
- `last_evidence_pass_at`: publisher pull loop is healthy.
- `last_weight_set_at`: most recent weight attempt.
- `weight_status`: `healthy`, `blocked_by_stake`, `blocked_by_transaction_error`, or `disabled`.
- `stalled`: background loops stopped making progress.

## Logs

```bash
sudo -u cathedral pm2 logs cathedral-validator --lines 200
tail -f /var/log/cathedral/validator.out.log
tail -f /var/log/cathedral/validator.err.log
```

Useful log lines:

- `pull_loop_tick fetched=N persisted=M`: signed eval rows are being pulled.
- `weights_pre_burn`: local vector before burn.
- `weights_set`: chain weight call completed or was blocked.
- `remote_weight_no_vector_yet`: remote mode is enabled but the publisher has no vector.
- `remote_weight_signature_invalid`: remote vector failed signature or policy checks.
- `remote_weight_source_enabled_but_key_missing`: remote mode was enabled without the pinned key.
- `pull_loop_disabled`: `CATHEDRAL_PUBLIC_KEY_HEX` is not set, so signed eval rows are not pulled.

## Troubleshooting

| Symptom | First check |
|---|---|
| `pull_loop_disabled` | Set `CATHEDRAL_PUBLIC_KEY_HEX` and restart. |
| `pull_eval_signature_invalid` | Re-pin the Cathedral eval-signing public key from the trusted operator source. |
| Remote startup fails | Set `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX` or disable `[remote_weight_source]`. |
| Remote fetch returns 503 | Wait for the publisher to produce a vector. Do not switch keys. |
| `blocked_by_stake` | Add stake or wait for permit. |
| `blocked_by_transaction_error` | Check subtensor endpoint, rate limit, and logs. |
| `stalled: true` | Restart the validator and inspect logs before it stalled. |
| SAT rows accepted but no SAT weight | Confirm remote mode is enabled after release, or local task-family weight is intentionally nonzero for testing. |

## Public Feed Checks

SAT public rows must be hash-only. They may include task id hash, answer hash, verifier details hash, score fields, schema version, and Cathedral signature.

They must not include raw CNF, DIMACS solutions, hidden metadata, private corpus material, trace bundle URLs, manifest URLs, or private score material.

Quick check:

```bash
curl -s "https://api.cathedral.computer/v1/leaderboard/recent?limit=20" | jq
```

Do not treat website pages, static text, or demo fixtures as live SAT metrics. Live claims need deployed `state.json` or publisher state.

## Handoff

An incoming validator operator needs:

- This runbook.
- Validator config file.
- Wallet files.
- Local `CATHEDRAL_BEARER`.
- Pinned Cathedral eval-signing public key.
- Pinned Polaris runtime-attestation public key while the legacy worker remains enabled.
- Pinned weight-policy public key only if remote weights are enabled.
