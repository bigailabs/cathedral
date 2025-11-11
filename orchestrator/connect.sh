#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/cloud"

NODE="${1:-}"

if [[ -z "$NODE" ]]; then
    echo "Usage: $0 <node-name>"
    echo ""
    echo "Examples:"
    echo "  $0 k3s-server-1"
    echo "  $0 server-1"
    echo "  $0 server1"
    echo "  $0 k3s-agent-2"
    echo "  $0 agent-2"
    echo "  $0 agent2"
    echo ""
    echo "Available nodes:"
    echo ""

    # Get server count
    SERVER_IPS=$(terraform output -json k3s_server_public_ips 2>/dev/null | jq -r '.[]' 2>/dev/null || echo "")
    if [[ -n "$SERVER_IPS" ]]; then
        echo "Servers:"
        idx=1
        while IFS= read -r ip; do
            if [[ -n "$ip" ]]; then
                echo "  - k3s-server-$idx ($ip)"
                ((idx++)) || true
            fi
        done <<< "$SERVER_IPS"
    fi

    # Get agent count
    AGENT_IPS=$(terraform output -json k3s_agent_public_ips 2>/dev/null | jq -r '.[]' 2>/dev/null || echo "")
    if [[ -n "$AGENT_IPS" ]]; then
        echo ""
        echo "Agents:"
        idx=1
        while IFS= read -r ip; do
            if [[ -n "$ip" ]]; then
                echo "  - k3s-agent-$idx ($ip)"
                ((idx++)) || true
            fi
        done <<< "$AGENT_IPS"
    fi

    if [[ -z "$SERVER_IPS" ]] && [[ -z "$AGENT_IPS" ]]; then
        echo "No nodes found. Make sure Terraform has been applied successfully."
        echo ""
        echo "Run: terraform output"
    fi

    exit 1
fi

# Normalize node name (support multiple formats)
# k3s-server-1, server-1, server1 -> server 1
NODE_LOWER=$(echo "$NODE" | tr '[:upper:]' '[:lower:]')

if [[ "$NODE_LOWER" =~ ^(k3s-)?server-?([0-9]+)$ ]]; then
    NODE_TYPE="server"
    NODE_INDEX="${BASH_REMATCH[2]}"
elif [[ "$NODE_LOWER" =~ ^(k3s-)?agent-?([0-9]+)$ ]]; then
    NODE_TYPE="agent"
    NODE_INDEX="${BASH_REMATCH[2]}"
else
    echo "Error: Invalid node name format: $NODE"
    echo ""
    echo "Valid formats:"
    echo "  - k3s-server-1, server-1, server1"
    echo "  - k3s-agent-2, agent-2, agent2"
    exit 1
fi

# Get SSH key path
SSH_KEY=$(terraform output -raw ssh_private_key_file 2>/dev/null || echo "")
if [[ -z "$SSH_KEY" ]]; then
    echo "Error: Could not get SSH key from Terraform outputs"
    exit 1
fi

# Extract actual key file path (terraform output might have description)
SSH_KEY_FILE=$(echo "$SSH_KEY" | awk '{print $1}')

if [[ ! -f "$SSH_KEY_FILE" ]]; then
    echo "Error: SSH key file not found: $SSH_KEY_FILE"
    exit 1
fi

# Get node IP address
if [[ "$NODE_TYPE" == "server" ]]; then
    NODE_IP=$(terraform output -json k3s_server_public_ips 2>/dev/null | jq -r ".[$((NODE_INDEX - 1))]" 2>/dev/null || echo "null")
    FRIENDLY_NAME="k3s-server-$NODE_INDEX"
else
    NODE_IP=$(terraform output -json k3s_agent_public_ips 2>/dev/null | jq -r ".[$((NODE_INDEX - 1))]" 2>/dev/null || echo "null")
    FRIENDLY_NAME="k3s-agent-$NODE_INDEX"
fi

if [[ "$NODE_IP" == "null" ]] || [[ -z "$NODE_IP" ]]; then
    echo "Error: Node not found: $FRIENDLY_NAME"
    echo ""
    echo "Available nodes:"
    "$0"  # Recursive call to show list
    exit 1
fi

echo "Connecting to $FRIENDLY_NAME ($NODE_IP)..."
echo ""

# Connect via SSH
ssh -i "$SSH_KEY_FILE" \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    ubuntu@"$NODE_IP"
