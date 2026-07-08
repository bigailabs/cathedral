#!/usr/bin/env bash
# Open-window watch for the V2 all-miner gate (runbook section 6).
# Samples every 30s: edge+local readiness, verifier drain, origin FD count,
# and shed counters. One line per sample so an operator can eyeball trends.
# Usage: ./open_window_watch.sh [samples] [interval_secs]
set -u

SSH_KEY="${CATHEDRAL_PREFLIGHT_SSH_KEY:-$HOME/.ssh/polaris_rsa}"
SSH_TARGET="${CATHEDRAL_PREFLIGHT_SSH:-polaris@34.71.88.140}"
EDGE_BASE="https://v2-beta.cathedral.computer"
SAMPLES="${1:-30}"
INTERVAL="${2:-30}"

for i in $(seq 1 "$SAMPLES"); do
  ts=$(date -u '+%H:%M:%S')
  edge_ready=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$EDGE_BASE/health/ready" || echo ERR)
  remote=$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 "$SSH_TARGET" \
    'cd /home/polaris/cathedral && set -a && . ./.env.sh >/dev/null 2>&1 && set +a && python3 deploy/v2-beta-router/open_window_sample_remote.py' 2>/dev/null)
  echo "$ts edge_ready=$edge_ready $remote"
  if [ "$i" -lt "$SAMPLES" ]; then sleep "$INTERVAL"; fi
done
exit 0
