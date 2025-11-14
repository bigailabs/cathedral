#!/usr/bin/env bash
#
# Basilica GPU Node Onboarding Script
#
# This script automatically onboards a GPU node to the Basilica network by:
#   1. Detecting GPU hardware (NVIDIA GPUs via nvidia-smi)
#   2. Registering the node with the Basilica API
#   3. Joining the K3s cluster as a worker node
#
# PREREQUISITES:
#   - Ubuntu 20.04+ or compatible Linux distribution
#   - NVIDIA drivers installed (nvidia-smi working)
#   - Root/sudo access
#   - curl and jq installed
#   - Internet connectivity to api.basilica.ai and get.k3s.io
#
# REQUIRED ENVIRONMENT VARIABLES:
#   BASILICA_DATACENTER_ID       Your datacenter identifier (from Basilica dashboard)
#   BASILICA_DATACENTER_API_KEY  Your API key (format: basilica_xxxx...)
#
# OPTIONAL ENVIRONMENT VARIABLES:
#   BASILICA_API_URL             API endpoint (default: https://api.basilica.ai)
#   BASILICA_NODE_ID             Custom node ID (default: hostname)
#
# HOW TO GET YOUR API KEY:
#   1. Log in to https://app.basilica.ai
#   2. Navigate to Settings > API Keys
#   3. Click "Create API Key"
#   4. Copy the key (starts with "basilica_")
#   5. Your DATACENTER_ID is your user ID from the dashboard
#
# USAGE:
#   # Basic usage (recommended for web UI copy-paste)
#   export BASILICA_DATACENTER_ID="your-datacenter-id"
#   export BASILICA_DATACENTER_API_KEY="basilica_xxxx..."
#   curl -fsSL https://onboard.basilica.ai/install.sh | sudo bash
#
#   # Or download and run locally
#   wget https://onboard.basilica.ai/install.sh -O onboard.sh
#   chmod +x onboard.sh
#   sudo BASILICA_DATACENTER_ID="your-dc-id" \
#        BASILICA_DATACENTER_API_KEY="basilica_xxx" \
#        ./onboard.sh
#
#   # Custom node ID
#   sudo BASILICA_NODE_ID="gpu-production-01" \
#        BASILICA_DATACENTER_ID="your-dc-id" \
#        BASILICA_DATACENTER_API_KEY="basilica_xxx" \
#        ./onboard.sh
#
# WHAT HAPPENS:
#   1. Validates root access and NVIDIA drivers
#   2. Checks connectivity to Basilica API
#   3. Auto-detects GPU model, count, memory, driver version, CUDA version
#   4. Registers node with Basilica API (creates/reuses K3s join token)
#   5. Installs K3s agent and joins the cluster
#   6. Node starts with taint "basilica.ai/unvalidated=true:NoSchedule"
#   7. After validation by network, taint is removed and node becomes schedulable
#
# TROUBLESHOOTING:
#   - "nvidia-smi not found": Install NVIDIA drivers first
#   - "Cannot reach Basilica API": Check firewall/network connectivity
#   - "Registration failed": Verify your API key and datacenter ID
#   - "K3s agent failed to start": Check logs with: journalctl -u k3s-agent -n 50
#
# UNINSTALL:
#   To remove this node from the cluster:
#   1. Run: /usr/local/bin/k3s-agent-uninstall.sh
#   2. Revoke the node token via API or dashboard
#
# VERSION: 1.0.0
# AUTHOR: Basilica Network
# LICENSE: MIT
#
set -euo pipefail

readonly BASILICA_API_URL="${BASILICA_API_URL:-https://api.basilica.ai}"
readonly SCRIPT_VERSION="1.0.0"

: "${BASILICA_DATACENTER_ID:?ERROR: BASILICA_DATACENTER_ID not set}"
: "${BASILICA_DATACENTER_API_KEY:?ERROR: BASILICA_DATACENTER_API_KEY not set}"

readonly NODE_ID="${BASILICA_NODE_ID:-$(hostname)}"

main() {
    log "Basilica GPU Node Join v${SCRIPT_VERSION}"
    log "Node ID: ${NODE_ID}"
    log "Datacenter: ${BASILICA_DATACENTER_ID}"

    check_root
    check_nvidia_driver
    check_connectivity

    log "Detecting GPU hardware..."
    detect_gpu_specs

    log "Registering node with Basilica..."
    register_node

    log "Joining K3s cluster..."
    join_k3s_cluster

    log "Successfully joined Basilica GPU cluster!"
    log "Check node status: kubectl get nodes ${K3S_NODE_NAME}"
}

check_root() {
    if [[ $EUID -ne 0 ]]; then
        die "This script must be run as root"
    fi
}

check_nvidia_driver() {
    if ! command -v nvidia-smi &> /dev/null; then
        die "nvidia-smi not found. Please install NVIDIA drivers first."
    fi

    if ! nvidia-smi &> /dev/null; then
        die "nvidia-smi failed. Check NVIDIA driver installation."
    fi

    log "NVIDIA driver detected"
}

check_connectivity() {
    if ! curl -f -m 10 "${BASILICA_API_URL}/health" &> /dev/null; then
        die "Cannot reach Basilica API at ${BASILICA_API_URL}"
    fi
    log "Basilica API reachable"
}

detect_gpu_specs() {
    GPU_COUNT=$(nvidia-smi --query-gpu=count --format=csv,noheader | wc -l)

    GPU_MODEL=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1 | xargs)

    GPU_MEMORY_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | xargs)
    GPU_MEMORY_GB=$((GPU_MEMORY_MB / 1024))

    DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | xargs)

    CUDA_VERSION=$(nvidia-smi | grep "CUDA Version" | awk '{print $9}' | xargs || echo "unknown")

    log "Detected GPU specs:"
    log "  Model: ${GPU_MODEL}"
    log "  Count: ${GPU_COUNT}"
    log "  Memory: ${GPU_MEMORY_GB}GB per GPU"
    log "  Driver: ${DRIVER_VERSION}"
    log "  CUDA: ${CUDA_VERSION}"
}

register_node() {
    local payload=$(cat <<EOF
{
  "node_id": "${NODE_ID}",
  "datacenter_id": "${BASILICA_DATACENTER_ID}",
  "gpu_specs": {
    "count": ${GPU_COUNT},
    "model": "${GPU_MODEL}",
    "memory_gb": ${GPU_MEMORY_GB},
    "driver_version": "${DRIVER_VERSION}",
    "cuda_version": "${CUDA_VERSION}"
  }
}
EOF
)

    local response=$(curl -f -X POST "${BASILICA_API_URL}/v1/gpu-nodes/register" \
        -H "Authorization: Bearer ${BASILICA_DATACENTER_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "${payload}" 2>&1) || {
        die "Registration failed. API response: ${response}"
    }

    K3S_URL=$(echo "$response" | jq -r '.k3s_url')
    K3S_TOKEN=$(echo "$response" | jq -r '.k3s_token')
    K3S_NODE_NAME=$(echo "$response" | jq -r '.node_id')

    NODE_LABELS=$(echo "$response" | jq -r '.node_labels | to_entries | map("--node-label \(.key)=\(.value)") | join(" ")')

    log "Registration approved"
    log "  K3s URL: ${K3S_URL}"
    log "  Node name: ${K3S_NODE_NAME}"
}

join_k3s_cluster() {
    log "Installing K3s agent..."

    local TAINTS="--kubelet-arg=register-with-taints=basilica.ai/unvalidated=true:NoSchedule"

    curl -sfL https://get.k3s.io | \
        K3S_URL="${K3S_URL}" \
        K3S_TOKEN="${K3S_TOKEN}" \
        K3S_NODE_NAME="${K3S_NODE_NAME}" \
        INSTALL_K3S_EXEC="agent ${NODE_LABELS} ${TAINTS}" \
        sh -

    log "Waiting for node to be Ready..."
    local retries=30
    while [[ $retries -gt 0 ]]; do
        if systemctl is-active --quiet k3s-agent; then
            log "K3s agent is running"
            log "  Node will be schedulable after validation completes"
            return 0
        fi
        sleep 2
        retries=$((retries - 1))
    done

    die "K3s agent failed to start. Check: journalctl -u k3s-agent -n 50"
}

log() {
    echo "[$(date +'%Y-%m-%d %H:%M:%S')] $*" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

main "$@"
