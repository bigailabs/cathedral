#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${CATHEDRAL_UPDATER_REPO_DIR:-/opt/cathedral/source}"
INSTALL_PREFIX="${CATHEDRAL_INSTALL_PREFIX:-/opt/cathedral}"
ALLOWED_SIGNERS="${CATHEDRAL_ALLOWED_SIGNERS:-${INSTALL_PREFIX}/allowed_signers}"
VALIDATOR_ENV="${CATHEDRAL_VALIDATOR_ENV:-/etc/cathedral/validator.env}"
ETC_DIR="${CATHEDRAL_ETC_DIR:-/etc/cathedral}"
TAG_PREFIX="${CATHEDRAL_UPDATER_TAG_PREFIX:-v}"
POLL_SECS="${CATHEDRAL_UPDATER_POLL_SECS:-600}"
RUN_ONCE="${CATHEDRAL_UPDATER_RUN_ONCE:-0}"
PIP_BIN="${CATHEDRAL_UPDATER_PIP_BIN:-${INSTALL_PREFIX}/.venv/bin/pip}"
VALIDATOR_BIN="${CATHEDRAL_UPDATER_VALIDATOR_BIN:-${INSTALL_PREFIX}/.venv/bin/cathedral-validator}"
PM2_BIN="${CATHEDRAL_UPDATER_PM2_BIN:-pm2}"
ECOSYSTEM_PATH="${CATHEDRAL_ECOSYSTEM_PATH:-${INSTALL_PREFIX}/ecosystem.config.cjs}"
EXPECTED_REMOTE_URL="${CATHEDRAL_UPDATER_EXPECTED_REMOTE_URL:-}"
VALIDATOR_STATE_DIR="${CATHEDRAL_VALIDATOR_STATE_DIR:-/var/lib/cathedral}"

sleep_or_exit() {
  local rc="${1:-0}"
  if [[ "$RUN_ONCE" == "1" ]]; then
    exit "$rc"
  fi
  sleep "$POLL_SECS"
}

# verify_tag <tag>
#
# Verifies a signed git tag using the SSH allowed-signers file at
# /opt/cathedral/allowed_signers. Returns 0 if the signature is valid,
# non-zero otherwise. Logs git's stderr so operators can diagnose failures
# (missing allowed_signers file, unknown signer, untrusted key, etc.).
#
# We rely on `git tag -v` exit code, not a substring of its output: the
# previous implementation grepped for "Good signature" which is the
# GPG-specific phrasing. SSH-signed tags use different output and the grep
# never matched, so the fleet never auto-updated.
verify_tag() {
  local tag="$1"
  local output rc=0
  # Capture combined stdout+stderr in `output`; check `git tag -v` exit code.
  # SSH and GPG produce different "good signature" phrasing, so we do not
  # grep -- we trust the exit code (0 = valid signature + principal matched,
  # non-zero = bad signature, untrusted signer, or missing allowed_signers).
  output=$(git -c "gpg.ssh.allowedSignersFile=${ALLOWED_SIGNERS}" \
    tag -v "$tag" 2>&1) || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi
  echo "$(date -u +%FT%TZ) updater: verify failed for $tag (exit=$rc): $output"
  return "$rc"
}

ensure_validator_state_dir() {
  local requested="$1"
  local fallback="${INSTALL_PREFIX}/state"

  # Legacy managed hosts can update before a root provisioner has created
  # /var/lib/cathedral. The updater runs as the unprivileged cathedral user,
  # so fall back to install-owned state and let config rendering point SQLite
  # there instead of failing later during `cathedral-validator migrate`.
  if install -d -m 0750 "$requested" 2>/dev/null && [[ -w "$requested" ]]; then
    printf '%s\n' "$requested"
    return 0
  fi

  echo "$(date -u +%FT%TZ) updater: cannot use validator state dir $requested; using $fallback" >&2
  install -d -m 0750 "$fallback"
  printf '%s\n' "$fallback"
}

cd "$REPO_DIR"

while true; do
  git fetch --tags --quiet origin || { sleep_or_exit 1; continue; }

  if [[ -n "$EXPECTED_REMOTE_URL" ]]; then
    actual_remote_url=$(git remote get-url origin)
    if [[ "$actual_remote_url" != "$EXPECTED_REMOTE_URL" ]]; then
      echo "$(date -u +%FT%TZ) updater: remote mismatch origin=$actual_remote_url expected=$EXPECTED_REMOTE_URL"
      sleep_or_exit 1
      continue
    fi
  fi

  current=$(git describe --tags --exact-match HEAD 2>/dev/null || echo "none")
  latest=$(git tag -l "${TAG_PREFIX}*" --sort=-version:refname | head -1)

  if [[ -n "$latest" && "$current" != "$latest" ]]; then
    echo "$(date -u +%FT%TZ) updater: current=$current latest=$latest - verifying signature"

    if [[ "$current" != "none" ]] && ! git merge-base --is-ancestor "$current" "$latest"; then
      echo "$(date -u +%FT%TZ) updater: $latest is not a descendant of current tag $current - refusing to update"
      sleep_or_exit 1
      continue
    fi

    if ! verify_tag "$latest"; then
      echo "$(date -u +%FT%TZ) updater: bad signature on $latest - refusing to update"
      sleep_or_exit 1
      continue
    fi

    echo "$(date -u +%FT%TZ) updater: checkout $latest + reinstall"
    git checkout --quiet "$latest"
    "$PIP_BIN" install --quiet -e .

    if [[ -f "$REPO_DIR/scripts/ecosystem.config.cjs" ]]; then
      install -m 0644 "$REPO_DIR/scripts/ecosystem.config.cjs" \
        "$ECOSYSTEM_PATH"
    fi

    config_path=""
    if [[ -f "$VALIDATOR_ENV" ]]; then
      config_path=$(awk -F= '$1 == "CATHEDRAL_CONFIG_PATH" {print $2}' "$VALIDATOR_ENV" | tail -1)
    fi
    if [[ -z "$config_path" && -f "$ETC_DIR/testnet.toml" ]]; then
      config_path="$ETC_DIR/testnet.toml"
    fi
    if [[ -z "$config_path" ]]; then
      config_path="$ETC_DIR/mainnet.toml"
    fi

    validator_state_dir="$(ensure_validator_state_dir "$VALIDATOR_STATE_DIR")"
    export CATHEDRAL_VALIDATOR_STATE_DIR="$validator_state_dir"

    echo "$(date -u +%FT%TZ) updater: migrate validator config $config_path"
    "$VALIDATOR_BIN" migrate --config "$config_path"

    echo "$(date -u +%FT%TZ) updater: restart validator"
    "$PM2_BIN" startOrReload "$ECOSYSTEM_PATH" --only cathedral-validator --update-env

    # The validator is now running the new code. The updater process,
    # however, is still executing the OLD bash loop from before the
    # checkout. Exit so PM2's autorestart (60s delay) respawns us from
    # the freshly-installed bin/updater.sh on disk. Without this, an
    # updater bug fix would never take effect on a live host.
    echo "$(date -u +%FT%TZ) updater: exiting to let PM2 respawn from new on-disk script"
    exit 0
  fi

  sleep_or_exit 0
done
