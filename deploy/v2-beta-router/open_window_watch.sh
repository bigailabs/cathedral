#!/usr/bin/env bash
# Open-window watch for the V2 all-miner gate (runbook section 6).
# Samples every 30s: edge+local readiness, verifier drain, origin FD count,
# accept-queue depth, and shed counters. One line per sample so an operator
# can eyeball trends.
#
# AUTO-ABORT: after CATHEDRAL_WATCH_ABORT_AFTER consecutive edge readiness
# failures (default 3, 0 disables) the script deploys V2_GATE_MODE:staged
# itself and exits 2. Added after the 2026-07-08 15:10Z open window, where
# the gate stayed open for 20+ minutes against a wedged origin because the
# driving session stalled and no watcher could abort.
# Usage: ./open_window_watch.sh [samples] [interval_secs]
set -u

SSH_KEY="${CATHEDRAL_PREFLIGHT_SSH_KEY:-$HOME/.ssh/polaris_rsa}"
SSH_TARGET="${CATHEDRAL_PREFLIGHT_SSH:-polaris@34.71.88.140}"
EDGE_BASE="https://v2-beta.cathedral.computer"
SAMPLES="${1:-30}"
INTERVAL="${2:-30}"
ABORT_AFTER="${CATHEDRAL_WATCH_ABORT_AFTER:-3}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
consec_fail=0

for i in $(seq 1 "$SAMPLES"); do
  ts=$(date -u '+%H:%M:%S')
  edge_ready=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$EDGE_BASE/health/ready" || echo ERR)
  remote=$(ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=8 "$SSH_TARGET" \
    'cd /home/polaris/cathedral && set -a && . ./.env.sh >/dev/null 2>&1 && set +a && python3 deploy/v2-beta-router/open_window_sample_remote.py' 2>/dev/null)
  echo "$ts edge_ready=$edge_ready $remote"
  if [ "$edge_ready" = "200" ]; then
    consec_fail=0
  else
    consec_fail=$((consec_fail + 1))
  fi
  if [ "$ABORT_AFTER" -gt 0 ] && [ "$consec_fail" -ge "$ABORT_AFTER" ]; then
    echo "$ts AUTO_ABORT edge readiness failed ${consec_fail}x consecutively - deploying V2_GATE_MODE:staged"
    (cd "$SCRIPT_DIR" && npx wrangler deploy --var V2_GATE_MODE:staged)
    echo "$ts AUTO_ABORT complete - gate is STAGED, origin may still need a restart"
    exit 2
  fi
  if [ "$i" -lt "$SAMPLES" ]; then sleep "$INTERVAL"; fi
done
exit 0
