# Cathedral Infrastructure Runbook

> Operational record of how the Cathedral validator runs today, and the gotchas we've hit. Verified 2026-04-18 against the live VPS deployment. Update this doc any time infra changes.

---

## Current production deployment

| Component | Host | Status |
|---|---|---|
| cathedral-validator (UID 123) | Hetzner CPX VPS · `135.181.8.214` · Ubuntu 24.04 | systemd-supervised, reboot-safe |
| cathedral-axon-local (socat 8080→50052) | same VPS | systemd-supervised |
| cathedral-miner (UID 115) | **orphaned** — was on terminated Verda VM `65.109.75.36` | re-register against a new GPU node when available |
| cathedral.computer site | Cloudflare Workers, deployed from `bigailabs/cathedral-site` | static |
| Log streaming backend | `api.polaris.computer` (Railway) | **stale** — backend still points at dead `65.109.75.36`; site's log panes are empty until the env var flips |

**Validator hotkey:** `5DnvAgAVykQFmgTSwLXTHpzfmi6W32VtV8L1D9yxSmtThWPb` (wallet `iota1`, hotkey `default`)
**Coldkey:** `5Ci3vcyyduFkMi9WTBozHppvGpuSe4oNRCAE1QFkX4j6Dso6`
**Stake:** 131.82 α (far below SN39's ~14k α permit threshold — weights parked)
**On-chain axon:** `135.181.8.214:8080` (verified via metagraph 2026-04-18)

---

## Gotchas — verified, each has bitten us at least once

### 1. `serve_axon` does NOT bind a listening socket

**Symptom**: validator logs `Axon served successfully`, the chain shows `<our-ip>:8080`, but `ss -tlnp` shows nothing on 8080. External clients get connection refused or hang.

**Root cause**: `serve_axon` is strictly a chain announcement. The actual gRPC server binds `[bid_grpc].listen_address` (default `0.0.0.0:50052`). Port 8080 (the axon port) is never locally bound by the validator binary.

**Fix**: run a local `socat` forwarder bridging 8080 → 50052. Systemd unit `cathedral-axon-local.service` does this on the VPS.

**Verify**: `ss -tlnp | grep 8080` shows `socat` listening. `nc -zv <public-ip> 8080` from an outside box succeeds.

### 2. SN39 `ServingRateLimit` is 50 blocks (~10 minutes)

**Symptom**: `btcli axon set ...` or the validator's automatic `serve_axon` call fails with `SubstrateRequestException(Invalid Transaction)` · `Custom error: 12`.

**Root cause**: SN39 chain enforces a 50-block cooldown between axon updates per hotkey. Each validator restart triggers another `serve_axon` attempt, eating more of the rate-limit window.

**Fix**: do not panic-restart. Wait 10 minutes between attempts. If using `btcli axon set` manually, verify the txn landed by checking the metagraph (`bt.Subtensor('finney').metagraph(39).axons[123]`), because follow-up attempts may return `Priority too low` when the first is still in the mempool — that's not failure, it's the original txn waiting to land.

### 3. Systemd unit on the home box re-activated after being stopped

**Observed 2026-04-18**: the home-box's `cathedral-validator.service` came back `active` an hour after `systemctl stop && disable`. Root cause wasn't fully isolated (possibly another agent, a scheduled task, or WSL-systemd quirk), but the result was a dual-validator race on the same hotkey — both boxes fighting over the axon registration.

**Current state**: the unit file on the home box was deleted (`sudo rm /etc/systemd/system/cathedral-validator.service`). `systemctl daemon-reload` confirmed "Unit cathedral-validator.service could not be found." The home box is now pure miner / operator-console hardware; it should **not** host a validator.

**If you ever re-stage a validator unit on that box**: understand why the previous one was deleted before creating a new one.

### 4. protoc 25.1 install must include the `include/` directory

**Symptom**: `cargo build` fails with `google/protobuf/timestamp.proto: File not found`.

**Root cause**: the protoc binary alone isn't enough — cathedral's proto files import well-known types (`google/protobuf/timestamp.proto`, etc.). Apt's protoc is too old; the GitHub release ships include files alongside the binary but you need to install them too.

**Fix**: after unzipping the release, `cp -r include/google /usr/local/include/`. Verify `/usr/local/include/google/protobuf/timestamp.proto` exists.

### 5. Crate names and binary names diverge

The cargo workspace still uses `basilica-validator` / `basilica-miner` as crate names, but the binaries they produce are named `cathedral-validator` / `cathedral-miner`. This is intentional during the substrate → cathedral rename. Don't try to "fix" it without coordination.

Correct build command:
```bash
cargo build --release --package basilica-validator --package basilica-miner
```

Looks for the artifacts at `target/release/cathedral-{validator,miner}`.

---

## Things I was wrong about, documenting for posterity

### `api_endpoint` overriding `api.basilica.ai`

I claimed in an earlier draft that the validator *must* have `[validator].api_endpoint` set to a cathedral-controlled URL or else it fails with `BASILICA_API_AUTH_ERROR`. We observed this error on one home-box deployment.

**Verified on the VPS 2026-04-18**: the live VPS `validator.toml` does **not** set `api_endpoint`. The validator has run without auth errors since startup. So either:
1. The earlier auth failure was specific to a network condition on the home box, or
2. The code path that calls the basilica API isn't currently executing, or
3. There's a fallback we haven't traced.

**Action**: if you see `BASILICA_API_AUTH_ERROR` in the journal, setting `api_endpoint` in the config *may* help, but it's not a universal requirement. Don't preemptively set it based on this doc.

The hard-coded default is in `crates/basilica-validator/src/config/main_config.rs::default_api_endpoint()` if you need to trace further.

---

## Deployment from scratch

Target: fresh Ubuntu 22.04 or 24.04 VPS with root SSH, public IPv4, no CGNAT. 4 CPU / 16 GB RAM / 50 GB disk is enough (validator is CPU-bound, no GPU needed).

```bash
# 1. Toolchain
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential pkg-config libssl-dev git curl ca-certificates \
  sqlite3 tmux socat ufw python3-pip python3-venv unzip

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source /root/.cargo/env

# 2. protoc 25.1 WITH include files
cd /tmp
curl -sL https://github.com/protocolbuffers/protobuf/releases/download/v25.1/protoc-25.1-linux-x86_64.zip -o protoc.zip
unzip -q -o protoc.zip -d protoc25
install -m 755 protoc25/bin/protoc /usr/local/bin/protoc
mkdir -p /usr/local/include && cp -r protoc25/include/google /usr/local/include/
test -f /usr/local/include/google/protobuf/timestamp.proto || { echo "MISSING include files"; exit 1; }

# 3. Clone + build
cd /root
git clone https://github.com/bigailabs/cathedral.git
cd cathedral
cargo build --release --package basilica-validator --package basilica-miner

# 4. Wallet — scp from a trusted source
# scp -r <trusted-laptop>:.bittensor/wallets/iota1 /root/.bittensor/wallets/

# 5. Write validator.toml (template below), systemd units (below)
mkdir -p /root/cathedral-config /root/cathedral-data /root/cathedral-logs
# ... write /root/cathedral-config/validator.toml

# 6. Install systemd units
# /etc/systemd/system/cathedral-validator.service (template below)
# /etc/systemd/system/cathedral-axon-local.service (template below)
systemctl daemon-reload
systemctl enable --now cathedral-validator cathedral-axon-local

# 7. Firewall
ufw --force enable
ufw allow 22/tcp
ufw allow 8080/tcp
ufw allow 50052/tcp

# 8. Verify
systemctl is-active cathedral-validator cathedral-axon-local       # active active
ss -tlnp | grep -E '8080|50052|9090|9091'                          # 4 listeners
nc -zv <PUBLIC_IP> 8080                                            # open from outside
journalctl -u cathedral-validator --since '1 minute ago' -f        # clean startup
```

### `validator.toml` template (live config on the VPS)

```toml
[database]
url = "sqlite:/root/cathedral-data/validator.db?mode=rwc"
max_connections = 10
run_migrations = true

[server]
host = "0.0.0.0"
port = 8080
advertised_host = "<PUBLIC_IP>"
advertised_port = 8080
advertised_tls = false
max_connections = 1000
request_timeout = { secs = 30 }

[bittensor]
wallet_name = "iota1"
hotkey_name = "default"
network = "finney"
netuid = 39
chain_endpoint = "wss://entrypoint-finney.opentensor.ai:443"
weight_interval_secs = 300
axon_port = 8080
external_ip = "<PUBLIC_IP>"

[verification]
max_concurrent_verifications = 50
max_concurrent_full_validations = 25
min_score_threshold = 0.1
verification_interval = { secs = 600 }
challenge_timeout = { secs = 120 }
use_dynamic_discovery = true
discovery_timeout = { secs = 30 }
fallback_to_static = true
cache_miner_info_ttl = { secs = 300 }
enable_worker_queue = false

# NOTE: no [verification.binary_validation] block.
# Cathedral runs SSH-based GPU discovery only (cathedral PR #7).

[verification.docker_validation]
docker_image = "nvidia/cuda:12.8.0-runtime-ubuntu22.04"
pull_timeout_secs = 2400

[verification.storage_validation]
min_required_storage_bytes = 109951162777

[metrics]
enabled = true
retention_period = { secs = 604800 }
collection_interval = { secs = 30 }

[metrics.prometheus]
host = "0.0.0.0"
port = 9091
path = "/metrics"

[api]
max_body_size = 1048576
bind_address = "0.0.0.0:9090"

[storage]
data_dir = "/root/cathedral-data"

[ssh_session]
ssh_key_directory = "/root/cathedral-data/ssh_keys"
key_algorithm = "ed25519"
default_session_duration = 300
max_session_duration = 3600
rental_session_duration = 3600
key_cleanup_interval = { secs = 60, nanos = 0 }
enable_automated_sessions = true
max_concurrent_sessions = 5
session_rate_limit = 20
enable_audit_logging = true
audit_log_path = "/root/cathedral-data/ssh_audit.log"
ssh_connection_timeout = { secs = 30, nanos = 0 }
ssh_command_timeout = { secs = 60, nanos = 0 }
ssh_retry_attempts = 3
ssh_retry_delay = { secs = 2, nanos = 0 }

[emission]
# TODO: verify against cathedral policy.md. Carried from upstream, may be wrong.
forced_burn_percentage = 95.0
burn_uid = 204
weight_set_interval_blocks = 360
weight_version_key = 0

[bid_grpc]
listen_address = "0.0.0.0:50052"
```

### `cathedral-validator.service`

```ini
[Unit]
Description=Cathedral Validator (Bittensor SN39)
After=network-online.target
Wants=network-online.target
Requires=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/cathedral
ExecStart=/root/cathedral/target/release/cathedral-validator start --config /root/cathedral-config/validator.toml
Restart=always
RestartSec=10s
TimeoutStopSec=30
Environment=RUST_LOG=info
StandardOutput=journal
StandardError=journal
SyslogIdentifier=cathedral-validator

[Install]
WantedBy=multi-user.target
```

### `cathedral-axon-local.service`

```ini
[Unit]
Description=Cathedral axon local bind 8080 -> validator gRPC :50052
After=cathedral-validator.service
Requires=cathedral-validator.service

[Service]
Type=simple
ExecStart=/usr/bin/socat TCP4-LISTEN:8080,fork,reuseaddr TCP4:127.0.0.1:50052
Restart=always
RestartSec=3s

[Install]
WantedBy=multi-user.target
```

---

## Axon re-registration: two paths

### A. Let the validator do it on startup (preferred)

The validator reads `external_ip` from config, compares to the on-chain axon entry, and calls `serve_axon` if mismatched. This works when:
- `ServingRateLimit` hasn't been hit in the last 10 minutes
- The 5-attempt internal retry loop is enough to succeed

If it fails, the process exits and systemd restarts it — each restart eats more of the rate-limit window. Stop the service, wait, then start once cleanly.

### B. Manual `btcli axon set`

```bash
source ~/bittensor-venv/bin/activate
btcli axon set \
  --netuid 39 \
  --ip <PUBLIC_IP> \
  --port 8080 \
  --wallet-name iota1 \
  --wallet-hotkey default \
  --network finney \
  --no-prompt
```

Use this when:
- The validator can't bind the IP locally (forwarder sitting in front of a private host)
- Path A keeps thrashing rate limits and you want to stop the bleed

Costs ~0.001 TAO in extrinsic fees. Check `btcli wallet overview` first.

---

## Home-box role

Windows 11 + WSL Ubuntu 22.04 at tailnet `100.112.113.3`. Retained as:
1. **Operator access** — Tailscale admin, has the `polaris_rsa` SSH key, holds a wallet backup.
2. **Future mining node** — RTX 5060 will become a billable node once consumer GPU pricing lands (issue [#24](https://github.com/bigailabs/cathedral/issues/24)).

**NOT a validator.** Don't re-stage the validator unit there. Path bitten 2026-04-18.

---

## Known weaknesses — audit list

- **GPU allow-list is a string match on `nvidia-smi` output.** Spoofable. Accepted for Act 1. [#24](https://github.com/bigailabs/cathedral/issues/24).
- **Binary GPU attestation disabled.** Prover binaries not integrated yet. See cathedral PR #7 for the SSH-discovery fallback.
- **Site log streaming is stale.** Railway backend has `POLARIS_VALIDATOR_HOST=65.109.75.36` (the dead Verda VM). Needs a Railway env var flip to `135.181.8.214` plus the VPS SSH key, OR a push-based log stream from the VPS. Dashboard log panes are empty until this is fixed.
- **No automated chain-txn retry with rate-limit backoff** for `serve_axon`. Validator startup tries 5 times over ~15s then exits; systemd restart re-enters the rate limit. Fix: add a longer sleep-between-attempts that respects the 50-block limit.
- **Weights parked.** Stake 131.82 α << ~14k α SN39 permit threshold. CUs accrue locally; emissions will flow once stake unlocks.

---

## Who to ask / where to look

- Validator internals: `crates/basilica-validator/src/`
- Miner internals: `crates/basilica-miner/src/`
- Config shape: `crates/basilica-validator/src/config/main_config.rs`
- On-chain registration / serve_axon: `crates/bittensor-rs/src/registration.rs`
- For cathedral-specific questions: [bigailabs/cathedral/issues](https://github.com/bigailabs/cathedral/issues)

If a step here differs from what you actually need to do, update this doc first. The whole point is to not re-learn the same mistakes.
