#!/usr/bin/env bash
#
# Cathedral validator provisioner - Stage A.7
#
# Bootstraps a fresh Polaris CPU box (Ubuntu 22.04+) into a fully running
# Cathedral validator under PM2. Idempotent: safe to re-run.
#
# ASSUMPTIONS
# -----------
# The bittensor wallet is already generated and the hotkey is registered on
# the target netuid BEFORE this script runs. Specifically, the operator has
# already done (on this box or elsewhere):
#
#   btcli w new_coldkey  --wallet.name "$BT_WALLET_NAME"
#   btcli w new_hotkey   --wallet.name "$BT_WALLET_NAME" --wallet.hotkey "$BT_WALLET_HOTKEY"
#   btcli subnet register --wallet.name "$BT_WALLET_NAME" \
#                         --wallet.hotkey "$BT_WALLET_HOTKEY" \
#                         --netuid "$BT_NETUID" \
#                         --subtensor.network "$BT_NETWORK"
#
# This script does NOT touch wallets. It does NOT generate or import keys.
# The local wallet hotkey NAME (e.g. "default") is what gets wired into
# /etc/cathedral/<network>.toml as `network.validator_hotkey`. The
# bittensor SDK opens the wallet by name and reads the ss58 off disk; the
# ss58 is not passed in here. Use `cathedral chain-check` afterwards to
# confirm the wallet's ss58 is registered on the subnet.

set -euo pipefail

# --- Inputs -----------------------------------------------------------------
#
# CATHEDRAL_BEARER          local validator bearer for its own /v1/claim endpoint
#                           (NOT publisher-read auth). Generate locally; do not
#                           send to anyone.
# CATHEDRAL_PUBLIC_KEY_HEX  Cathedral eval-signing pubkey (kid=cathedral-eval-signing
#                           in /.well-known/cathedral-jwks.json). Gates the pull
#                           loop; if unset, the loop is not spawned.
# CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX
#                           Optional publisher weight-policy pubkey. Required
#                           only when [remote_weight_source].enabled is true.
# POLARIS_PUBLIC_KEY_HEX    Polaris runtime-attestation pubkey (kid=polaris-runtime-attestation
#                           in the same JWKS document). Goes into TOML
#                           polaris.public_key_hex; required because
#                           `cathedral-validator serve` still constructs the
#                           legacy /v1/claim worker.
# CATHEDRAL_NETWORK         logical network name; selects which config template
#                           is rendered. Defaults to `mainnet` (SN39 finney);
#                           set to `testnet` for SN292 protocol-dev runs.
# BT_NETWORK                bittensor subtensor network. Defaults to `finney`.
#                           For testnet, set to `test`.
# BT_NETUID                 subnet uid. Defaults to 39 (mainnet). For testnet,
#                           set to 292.
# BT_WALLET_NAME            local bittensor wallet (coldkey) name. Default
#                           `cathedral-validator`.
# BT_WALLET_HOTKEY          local bittensor wallet hotkey NAME (not ss58).
#                           This is the value of `--wallet.hotkey` in btcli;
#                           it is what gets written to `network.validator_hotkey`
#                           in the rendered TOML. Default `default`.

: "${CATHEDRAL_RELEASE_TAG:=main}"
: "${CATHEDRAL_BEARER:?CATHEDRAL_BEARER is required (local validator bearer)}"
: "${CATHEDRAL_PUBLIC_KEY_HEX:?CATHEDRAL_PUBLIC_KEY_HEX is required (JWKS kid=cathedral-eval-signing)}"
: "${CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX:=}"
: "${POLARIS_PUBLIC_KEY_HEX:?POLARIS_PUBLIC_KEY_HEX is required (JWKS kid=polaris-runtime-attestation)}"
: "${BT_WALLET_NAME:=cathedral-validator}"
: "${BT_WALLET_HOTKEY:=default}"
: "${BT_NETWORK:=finney}"
: "${BT_NETUID:=39}"
: "${CATHEDRAL_NETWORK:=mainnet}"

# Sanity-check the network selector. Only `mainnet` and `testnet` ship with
# a config template; anything else is an operator typo.
case "$CATHEDRAL_NETWORK" in
  mainnet|testnet) ;;
  *)
    echo "ERROR: CATHEDRAL_NETWORK must be 'mainnet' or 'testnet' (got: $CATHEDRAL_NETWORK)" >&2
    exit 1
    ;;
esac

REPO_URL="https://github.com/cathedralai/cathedral"
SRC_DIR="/opt/cathedral/source"
VENV_DIR="/opt/cathedral/.venv"
ETC_DIR="/etc/cathedral"
LOG_DIR="/var/log/cathedral"
STATE_DIR="/var/lib/cathedral"
ECOSYSTEM_DST="/opt/cathedral/ecosystem.config.cjs"

echo "==> step 1: validate inputs"
echo "    release_tag=${CATHEDRAL_RELEASE_TAG}"
echo "    wallet=${BT_WALLET_NAME}/${BT_WALLET_HOTKEY}"
echo "    network=${BT_NETWORK} netuid=${BT_NETUID}"
echo "    config_target=${CATHEDRAL_NETWORK}.toml (validator_hotkey will be set to wallet hotkey name '${BT_WALLET_HOTKEY}')"

# --- Step 2: apt deps -------------------------------------------------------

echo "==> step 2: install system packages"
# Pick the Python interpreter. Cathedral requires >=3.11. On Ubuntu 22.04 the
# default is 3.10 and python3.11 must come from apt; on 24.04 the default is
# already 3.12. We auto-detect rather than hardcoding 3.11.
PYTHON_BIN=""
for cand in python3.13 python3.12 python3.11; do
  if command -v "$cand" >/dev/null 2>&1; then
    PYTHON_BIN="$cand"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  sudo apt-get update
  sudo apt-get install -y python3.11 python3.11-venv
  PYTHON_BIN="python3.11"
fi
echo "    python: $PYTHON_BIN ($($PYTHON_BIN --version))"

VENV_PKG="${PYTHON_BIN}-venv"
if ! dpkg -s "$VENV_PKG" >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y "$VENV_PKG"
fi

APT_PKGS=(git curl gnupg nodejs npm)
MISSING_PKGS=()
for pkg in "${APT_PKGS[@]}"; do
  if ! dpkg -s "$pkg" >/dev/null 2>&1; then
    MISSING_PKGS+=("$pkg")
  fi
done
if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
  echo "    missing: ${MISSING_PKGS[*]}"
  sudo apt-get update
  sudo apt-get install -y "${MISSING_PKGS[@]}"
else
  echo "    all system packages already installed"
fi

# --- Step 3: cathedral user -------------------------------------------------

echo "==> step 3: ensure cathedral user"
if id cathedral >/dev/null 2>&1; then
  echo "    user already exists"
else
  sudo useradd -m -s /bin/bash cathedral
fi

# --- Step 4: directory layout ----------------------------------------------

echo "==> step 4: create directories"
sudo install -d -o cathedral -g cathedral /opt/cathedral "$ETC_DIR" "$LOG_DIR" "$STATE_DIR"

# --- Step 5: clone or update source ----------------------------------------

echo "==> step 5: fetch source"
if [[ -d "$SRC_DIR/.git" ]]; then
  echo "    source exists; fetching"
  sudo -u cathedral git -C "$SRC_DIR" remote set-url origin "$REPO_URL"
  sudo -u cathedral git -C "$SRC_DIR" fetch --tags --prune origin
else
  echo "    cloning fresh"
  sudo -u cathedral git clone "$REPO_URL" "$SRC_DIR"
fi

# --- Step 6: checkout release tag ------------------------------------------

echo "==> step 6: checkout ${CATHEDRAL_RELEASE_TAG}"
# Use -C and pass the ref through detached checkout so tags, branches, and
# explicit shas all work the same way.
sudo -u cathedral git -C "$SRC_DIR" checkout --quiet "$CATHEDRAL_RELEASE_TAG"
# If the ref is a branch, fast-forward; if it's a tag/sha this is a no-op.
sudo -u cathedral git -C "$SRC_DIR" pull --ff-only --quiet origin "$CATHEDRAL_RELEASE_TAG" \
  || echo "    (ref is detached tag or sha; skipping pull)"

# --- Step 7: venv + install -------------------------------------------------

echo "==> step 7: python venv + install"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  sudo -u cathedral "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
sudo -u cathedral "$VENV_DIR/bin/pip" install --upgrade --quiet pip
sudo -u cathedral "$VENV_DIR/bin/pip" install --quiet -e "$SRC_DIR"

# --- Step 7b: install allowed_signers for updater tag verification ---------

echo "==> step 7b: install /opt/cathedral/allowed_signers"
ALLOWED_SIGNERS_SRC="$SRC_DIR/etc/cathedral/allowed_signers"
ALLOWED_SIGNERS_DST="/opt/cathedral/allowed_signers"
if [[ ! -f "$ALLOWED_SIGNERS_SRC" ]]; then
  echo "ERROR: allowed_signers not found at $ALLOWED_SIGNERS_SRC" >&2
  exit 1
fi
sudo install -o cathedral -g cathedral -m 0644 "$ALLOWED_SIGNERS_SRC" "$ALLOWED_SIGNERS_DST"

# --- Step 8: render <network>.toml -----------------------------------------

CONFIG_NAME="${CATHEDRAL_NETWORK}.toml"
CONFIG_DST="$ETC_DIR/$CONFIG_NAME"
echo "==> step 8: render ${CONFIG_DST}"
TEMPLATE="$SRC_DIR/config/${CONFIG_NAME}"
if [[ ! -f "$TEMPLATE" ]]; then
  echo "ERROR: template not found at $TEMPLATE" >&2
  exit 1
fi
# Render to a tmp file under the cathedral user, then move into /etc.
# Done in two sed passes so the substitution stays exact (no regex escaping
# of user-provided hex strings). `validator_hotkey` in the rendered TOML is
# the local wallet hotkey NAME (e.g. "default"); the bittensor SDK uses it
# to open the wallet on disk and reads the ss58 from the key file itself.
TMP_TOML="$(mktemp)"
trap 'rm -f "$TMP_TOML"' EXIT
sed \
  -e "s|validator_hotkey = \"REPLACE_ME\"|validator_hotkey = \"${BT_WALLET_HOTKEY}\"|" \
  -e "s|wallet_name = \"cathedral-validator\"|wallet_name = \"${BT_WALLET_NAME}\"|" \
  -e "s|public_key_hex = \"REPLACE_WITH_POLARIS_ED25519_PUBLIC_KEY_HEX\"|public_key_hex = \"${POLARIS_PUBLIC_KEY_HEX}\"|" \
  "$TEMPLATE" > "$TMP_TOML"
sudo install -o cathedral -g cathedral -m 0644 "$TMP_TOML" "$CONFIG_DST"

# --- Step 9: render validator.env ------------------------------------------

echo "==> step 9: render ${ETC_DIR}/validator.env"
TMP_ENV="$(mktemp)"
trap 'rm -f "$TMP_TOML" "$TMP_ENV"' EXIT
cat > "$TMP_ENV" <<EOF
CATHEDRAL_BEARER=${CATHEDRAL_BEARER}
CATHEDRAL_PUBLIC_KEY_HEX=${CATHEDRAL_PUBLIC_KEY_HEX}
CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX=${CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY_HEX}
CATHEDRAL_PUBLISHER_TOKEN=
CATHEDRAL_NETWORK=${CATHEDRAL_NETWORK}
CATHEDRAL_CONFIG_PATH=${CONFIG_DST}
CATHEDRAL_UPDATER_EXPECTED_REMOTE_URL=${REPO_URL}
EOF
sudo install -o cathedral -g cathedral -m 0600 "$TMP_ENV" "$ETC_DIR/validator.env"

# --- Step 10: pm2 -----------------------------------------------------------

echo "==> step 10: install pm2"
if ! command -v pm2 >/dev/null 2>&1; then
  sudo npm install -g pm2
else
  echo "    pm2 already installed"
fi

# --- Step 11: copy ecosystem config ----------------------------------------

echo "==> step 11: copy ecosystem.config.cjs"
ECOSYSTEM_SRC="$SRC_DIR/scripts/ecosystem.config.cjs"
if [[ ! -f "$ECOSYSTEM_SRC" ]]; then
  echo "ERROR: ecosystem.config.cjs not found at $ECOSYSTEM_SRC" >&2
  echo "       (Stage A.4 must land before this script can complete.)" >&2
  exit 1
fi
sudo install -o cathedral -g cathedral -m 0644 "$ECOSYSTEM_SRC" "$ECOSYSTEM_DST"

# --- Step 12: pm2 start -----------------------------------------------------

echo "==> step 12: pm2 start"
# Use `sudo -iu cathedral` (login shell) rather than plain `sudo -u`. PM2's
# daemon spawn fails with `spawn /usr/bin/node EACCES` when the env isn't a
# clean login shell (the parent shell inherits stdio fds that PM2's child
# can't open). Login shell resets HOME / fds and pm2 starts cleanly.
# `pm2 start` on an already-running ecosystem reloads in place; idempotent.
sudo -iu cathedral pm2 start "$ECOSYSTEM_DST"

# --- Step 13: pm2 save ------------------------------------------------------

echo "==> step 13: pm2 save"
sudo -iu cathedral pm2 save

# --- Step 14: pm2 startup --------------------------------------------------

echo "==> step 14: pm2 systemd boot integration"
if systemctl is-enabled pm2-cathedral >/dev/null 2>&1; then
  echo "    pm2-cathedral systemd unit already enabled"
else
  sudo env "PATH=$PATH:/usr/bin" pm2 startup systemd -u cathedral --hp /home/cathedral
fi

# --- Step 15: migrate db ---------------------------------------------------

echo "==> step 15: run validator migrations"
sudo -iu cathedral "$VENV_DIR/bin/cathedral-validator" migrate --config "$CONFIG_DST"

echo ""
echo "==> done. pm2 status:"
sudo -iu cathedral pm2 status
