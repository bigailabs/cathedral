#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

INVENTORY="${INVENTORY:-inventories/production.ini}"
PLAYBOOK="${PLAYBOOK:-playbooks/k3s-setup.yml}"
TAGS="${TAGS:-all}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

echo "Provisioning K3s cluster..."
echo "Inventory: $INVENTORY"
echo "Playbook: $PLAYBOOK"
echo "Tags: $TAGS"

ansible-playbook \
    -i "$INVENTORY" \
    "$PLAYBOOK" \
    --tags "$TAGS" \
    $EXTRA_ARGS

echo "Provisioning complete."
