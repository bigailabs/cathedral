#!/usr/bin/env bash
set -euo pipefail

# This script initializes a local Subtensor devnet by:
# - Ensuring btcli is installed
# - Creating wallets (Alice coldkey + default/M1/M2 hotkeys)
# - Creating subnet (netuid=2), registering M1/M2, and starting emission

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
CHAIN_ENDPOINT="${CHAIN_ENDPOINT:-ws://127.0.0.1:19944}"
NETUID="${NETUID:-2}"
WALLET_PATH="${WALLET_PATH:-$HOME/.bittensor/wallets}"

if [[ "$CHAIN_ENDPOINT" =~ ^ws:// ]]; then
  unset SSL_CERT_FILE
else
  # Ensure TLS trust for WSS endpoints if not provided by the environment
  export SSL_CERT_FILE="${SSL_CERT_FILE:-$SCRIPT_DIR/tls/ca.crt}"
fi
echo "[init] Using CHAIN_ENDPOINT=$CHAIN_ENDPOINT NETUID=$NETUID WALLET_PATH=$WALLET_PATH"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 required for pip install of bittensor-cli" >&2
  exit 1
fi

# Install bittensor-cli if missing
if ! command -v btcli >/dev/null 2>&1; then
  python3 -m pip install --no-cache-dir --upgrade pip >/dev/null 2>&1 || true
  python3 -m pip install --no-cache-dir bittensor-cli >/dev/null 2>&1
fi

if ! command -v btcli >/dev/null 2>&1; then
  echo "btcli not found after install" >&2
  exit 1
fi

mkdir -p "$WALLET_PATH/Alice/hotkeys"

echo "[init] Waiting for Subtensor endpoint at $CHAIN_ENDPOINT..."
host_port="${CHAIN_ENDPOINT#*//}"
host="${host_port%%:*}"
port="${host_port##*:}"
ready=false
for i in $(seq 1 60); do
  if bash -c "</dev/tcp/${host}/${port}" 2>/dev/null; then
    ready=true; echo "[init] chain ready"; break
  fi
  sleep 2
done

if [ "$ready" != true ]; then
  echo "[init] Primary endpoint not reachable: $CHAIN_ENDPOINT"
  # Try sensible fallback between WS<->WSS for local setup
  case "$CHAIN_ENDPOINT" in
    wss://localhost:9944|wss://127.0.0.1:9944)
      ALT="ws://127.0.0.1:19944" ;;
    ws://127.0.0.1:19944|ws://localhost:19944)
      ALT="wss://localhost:9944" ;;
    *) ALT="" ;;
  esac

  if [ -n "$ALT" ]; then
    echo "[init] Trying fallback endpoint: $ALT"
    host_port="${ALT#*//}"; host="${host_port%%:*}"; port="${host_port##*:}"
    for i in $(seq 1 60); do
      if bash -c "</dev/tcp/${host}/${port}" 2>/dev/null; then
        CHAIN_ENDPOINT="$ALT"
        # Update TLS env for WSS, or unset for WS
        if [[ "$CHAIN_ENDPOINT" =~ ^ws:// ]]; then
          unset SSL_CERT_FILE
        else
          export SSL_CERT_FILE="${SSL_CERT_FILE:-$SCRIPT_DIR/tls/ca.crt}"
        fi
        echo "[init] Fallback endpoint ready: $CHAIN_ENDPOINT"; ready=true; break
      fi
      sleep 2
    done
  fi
fi

if [ "$ready" != true ]; then
  echo "[init] ERROR: Could not reach any local Subtensor endpoint (tried $CHAIN_ENDPOINT${ALT:+, $ALT})" >&2
  exit 1
fi

# Export env vars for btcli (some versions honor these)
export SUBTENSOR_CHAIN_ENDPOINT="$CHAIN_ENDPOINT"

echo "[init] btcli version: $(btcli --version 2>/dev/null || echo unknown)"
echo "[init] Verifying chain connectivity via btcli..."
for i in $(seq 1 30); do
  if btcli subnet list --subtensor.chain_endpoint "$CHAIN_ENDPOINT" >/dev/null 2>&1; then
    echo "[init] btcli connectivity OK"
    break
  fi
  echo "[init] btcli not ready yet (attempt $i/30). Waiting..."
  sleep 2
done

echo "[init] Creating Alice wallet (coldkey) if missing"
if [ ! -f "$WALLET_PATH/Alice/coldkey" ]; then
  btcli wallet regen_coldkey --wallet.path "$WALLET_PATH" --wallet.name Alice \
    --seed "0xe5be9a5092b81bca64be81d212e7f2f9eba183bb7a90954f7b76361f6edb5c0a" \
    --no-use-password || true
fi

[ -f "$WALLET_PATH/Alice/hotkeys/default" ] || btcli wallet new_hotkey --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey default --n_words 12 --no-use-password
[ -f "$WALLET_PATH/Alice/hotkeys/M1" ] || btcli wallet new_hotkey --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey M1 --n_words 12 --no-use-password
[ -f "$WALLET_PATH/Alice/hotkeys/M2" ] || btcli wallet new_hotkey --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey M2 --n_words 12 --no-use-password

echo "[init] Creating subnet (may already exist)"
create_subnet() {
  printf "\n\n\n\n\n\n\n" | btcli subnet create \
    --subtensor.chain_endpoint "$CHAIN_ENDPOINT" \
    --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey default \
    --subnet-name "grail-local" --no_prompt
}

n=0; until [ $n -ge 10 ]; do
  if create_subnet; then
    break
  fi
  echo "[init] subnet create failed (attempt $((n+1)) of 10). Waiting before retry..."
  sleep 6
  n=$((n+1))
done || true

register() {
  local hk="$1"; local n=0
  until [ $n -ge 5 ]; do
    if yes | btcli subnet register --netuid "$NETUID" \
      --subtensor.chain_endpoint "$CHAIN_ENDPOINT" \
      --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey "$hk" --no_prompt 2>&1 | tee "/tmp/register_$hk.log" | grep -q "✅ Registered"; then
      echo "[init] Registered $hk"; return 0; fi
    grep -q "already registered" "/tmp/register_$hk.log" && { echo "[init] $hk already registered"; return 0; }
    sleep 5; n=$((n+1))
  done
  return 0
}

echo "[init] Registering hotkeys M1/M2"
register M1 || true
register M2 || true

echo "[init] Starting emission (may already be started)"
start_emission() {
  btcli subnet start --netuid "$NETUID" \
    --subtensor.chain_endpoint "$CHAIN_ENDPOINT" \
    --wallet.path "$WALLET_PATH" --wallet.name Alice --wallet.hotkey default --no_prompt
}

n=0; until [ $n -ge 10 ]; do
  if start_emission; then
    break
  fi
  echo "[init] subnet start failed (attempt $((n+1)) of 10). Waiting before retry..."
  sleep 6
  n=$((n+1))
done || true

echo "[init] Complete. Alice SS58 hotkey: 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
