# Validator Runbook

Run a Cathedral validator on SN39.

## Install

```bash
git clone https://github.com/cathedralai/cathedral
cd cathedral
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Configure

Use `config/mainnet.toml`.

Set:

- `network.validator_hotkey`
- `network.wallet_name`
- `polaris.public_key_hex`
- `publisher.public_key_env`

Keep:

```toml
task_family_weights = { synthetic_boolean_v1 = 0.0 }
```

Env:

```bash
export CATHEDRAL_BEARER=$(openssl rand -hex 32)
export CATHEDRAL_PUBLIC_KEY_HEX=<cathedral-eval-signing-public-key>
```

## Start

```bash
cathedral-validator migrate --config config/mainnet.toml
cathedral chain-check --config config/mainnet.toml
cathedral-validator serve --config config/mainnet.toml
```

PM2:

```bash
sudo -u cathedral pm2 status
sudo -u cathedral pm2 logs cathedral-validator --lines 200
sudo -u cathedral pm2 restart cathedral-validator --update-env
```

## Remote Signed Weights

Remote signed weights are opt-in. Enable only after release notice.

```toml
[remote_weight_source]
enabled = true
url = "https://api.cathedral.computer"
key_id = "cathedral-weight-policy"
public_key_env = "CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX"
poll_interval_secs = 60.0
request_timeout_secs = 10.0
```

```bash
export CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX=8d74453ac008cc7be3f0609b43d31aa4096ab4a6ded32b9e754a5c48360938fd
cathedral-validator verify-remote-weight-vector --config config/mainnet.toml
```

Missing or invalid key fails startup. There is no silent local fallback.

## SAT Preflight

Before SAT has nonzero weight, run:

```bash
cathedral-validator sat-launch-preflight --config config/mainnet.toml
cathedral-validator chain-launch-preflight --config config/mainnet.toml
cathedral-validator verify-remote-weight-vector --config config/mainnet.toml
```

Shadow only:

```bash
cathedral-validator sat-launch-preflight \
  --config config/mainnet.toml \
  --allow-local-weight-source
```

## Health

```bash
curl -s http://127.0.0.1:9333/health | jq
cathedral health
cathedral weights
cathedral registration
```

Fields:

- `registered`
- `current_block`
- `last_evidence_pass_at`
- `last_weight_set_at`
- `weight_status`
- `stalled`

## Logs

```bash
sudo -u cathedral pm2 logs cathedral-validator --lines 200
tail -f /var/log/cathedral/validator.out.log
tail -f /var/log/cathedral/validator.err.log
```

Look for:

- `pull_loop_tick fetched=N persisted=M`
- `pull_loop_disabled`
- `remote_weight_vector_cached`
- `remote_weight_mapped`
- `remote_weight_relayed`
- `blocked_by_transaction_error`
- `blocked_by_stake`

## Troubleshooting

| Symptom | Check |
|---|---|
| `pull_loop_disabled` | Set `CATHEDRAL_PUBLIC_KEY_HEX`. |
| `pull_eval_signature_invalid` | Re-pin Cathedral eval public key. |
| remote key startup failure | Set `CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX`. |
| `remote_weight_fetch_error` | Check publisher reachability. |
| `remote_weight_signature_invalid` | Stop and re-pin weight key. |
| `blocked_by_stake` | Stake or permit. |
| `blocked_by_transaction_error` | Rate limit, endpoint, chain logs. |
| SAT rows but no SAT weight | Expected while policy weight is `0.0`. |
