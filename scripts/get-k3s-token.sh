#!/bin/bash
# Securely fetch K3s node token from the server
#
# This script retrieves the K3s node token needed for validator node onboarding
# without writing it to disk.
#
# Usage:
#   export BASILICA_K3S_TOKEN=$(./scripts/get-k3s-token.sh)
#   echo "Token length: ${#BASILICA_K3S_TOKEN}"

set -euo pipefail

INVENTORY="${INVENTORY:-scripts/ansible/inventories/example.ini}"

# Check if ansible is available
if ! command -v ansible &> /dev/null; then
    echo "Error: ansible not found. Please install ansible." >&2
    exit 1
fi

# Check if inventory file exists
if [ ! -f "$INVENTORY" ]; then
    echo "Error: Inventory file not found: $INVENTORY" >&2
    echo "Set INVENTORY env var or use default: scripts/ansible/inventories/example.ini" >&2
    exit 1
fi

# Fetch token from K3s server
ansible k3s_server -i "$INVENTORY" \
  -m slurp \
  -a "src=/var/lib/rancher/k3s/server/node-token" \
  --become \
  2>/dev/null \
  | python3 -c '
import sys
import json
import base64

try:
    for line in sys.stdin:
        if "\"content\":" in line:
            # Parse the JSON from ansible output
            data = json.loads(line.strip().rstrip(","))
            if "content" in data:
                token = base64.b64decode(data["content"]).decode("utf-8").strip()
                print(token)
                sys.exit(0)
    # If we get here, content was not found
    sys.stderr.write("Error: Could not parse token from ansible output\n")
    sys.exit(1)
except Exception as e:
    sys.stderr.write(f"Error: {e}\n")
    sys.exit(1)
'

# Check if token was successfully fetched
if [ $? -ne 0 ]; then
    echo "" >&2
    echo "Failed to fetch K3s token. Try manually:" >&2
    echo "  ssh <k3s-server> 'sudo cat /var/lib/rancher/k3s/server/node-token'" >&2
    exit 1
fi
