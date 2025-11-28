#!/usr/bin/env bash
#
# Basilica GPU Node Onboarding Script
#
# This script automatically onboards a GPU node to the Basilica network by:
#   1. Detecting GPU hardware (NVIDIA GPUs via nvidia-smi)
#   2. Registering the node with the Basilica API
#   3. Setting up WireGuard VPN (if configured by cluster)
#   4. Joining the K3s cluster as a worker node
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
#   5. If WireGuard is required: sets up VPN tunnel to cluster
#   6. Installs K3s agent and joins the cluster (using WireGuard IP if applicable)
#   7. Node starts with taint "basilica.ai/unvalidated=true:NoSchedule"
#   8. After validation by network, taint is removed and node becomes schedulable
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
# VERSION: 1.6.0
# AUTHOR: Basilica Network
# LICENSE: MIT
#
set -euo pipefail

readonly BASILICA_API_URL="${BASILICA_API_URL:-https://api.basilica.ai}"
readonly SCRIPT_VERSION="1.6.0"
readonly WIREGUARD_INTERFACE="wg0"

: "${BASILICA_DATACENTER_ID:?ERROR: BASILICA_DATACENTER_ID not set}"
: "${BASILICA_DATACENTER_API_KEY:?ERROR: BASILICA_DATACENTER_API_KEY not set}"

readonly NODE_ID="${BASILICA_NODE_ID:-$(hostname)}"

# WireGuard variables (set by register_node if needed)
WIREGUARD_ENABLED="false"
WG_NODE_IP=""
WG_KEEPALIVE=""
WG_PEERS_JSON=""

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

    log "Applying performance tuning..."
    setup_performance_tuning

    if [ "${WIREGUARD_ENABLED}" = "true" ]; then
        log "Setting up WireGuard VPN..."
        setup_wireguard

        log "Deploying WireGuard watchdog..."
        deploy_wireguard_watchdog
    fi

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

setup_performance_tuning() {
    log "Loading kernel modules for performance tuning..."

    # Load BBR congestion control module
    if modprobe tcp_bbr 2>/dev/null; then
        log "  BBR module loaded"
        echo "tcp_bbr" > /etc/modules-load.d/bbr.conf
    else
        log "  BBR module not available (kernel may be too old)"
    fi

    # Load conntrack module for connection tracking tuning
    if modprobe nf_conntrack 2>/dev/null; then
        log "  nf_conntrack module loaded"
        echo "nf_conntrack" > /etc/modules-load.d/conntrack.conf
    fi

    # Load br_netfilter module (required for bridge-nf-call settings)
    if modprobe br_netfilter 2>/dev/null; then
        log "  br_netfilter module loaded"
        echo "br_netfilter" > /etc/modules-load.d/br_netfilter.conf
    fi

    log "Deploying sysctl performance configuration..."
    cat > /etc/sysctl.d/99-wireguard-performance.conf <<'SYSCTL_EOF'
# WireGuard and Network Performance Tuning for K3s GPU Clusters
# Deployed by Basilica onboard.sh - Do not edit manually
# Architecture: WireGuard (MTU 1420) -> Flannel VXLAN (MTU ~1370) -> Pods

# IP forwarding and routing (mandatory for K3s/Flannel)
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
net.ipv4.conf.all.rp_filter = 2
net.ipv4.conf.default.rp_filter = 2

# Bridge netfilter (mandatory for Flannel/kube-proxy)
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1

# Socket buffer sizing (64MB max for high-throughput GPU workloads)
net.core.rmem_max = 67108864
net.core.wmem_max = 67108864
net.core.rmem_default = 16777216
net.core.wmem_default = 16777216
net.ipv4.tcp_rmem = 4096 1048576 67108864
net.ipv4.tcp_wmem = 4096 1048576 67108864
net.ipv4.udp_rmem_min = 16384
net.ipv4.udp_wmem_min = 16384

# Network device tuning (increased for 10Gbps line-rate)
net.core.netdev_max_backlog = 50000
net.core.netdev_budget = 3000
net.core.netdev_budget_usecs = 8000
net.core.somaxconn = 65535

# BBR congestion control (ideal for WireGuard tunnels)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_notsent_lowat = 16384

# Connection tracking (1M entries, tuned timeouts for K8s)
net.netfilter.nf_conntrack_max = 1048576
net.netfilter.nf_conntrack_buckets = 262144
net.netfilter.nf_conntrack_tcp_timeout_established = 7200
net.netfilter.nf_conntrack_tcp_timeout_time_wait = 30
net.netfilter.nf_conntrack_udp_timeout = 120
net.netfilter.nf_conntrack_udp_timeout_stream = 180

# TCP optimizations
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_max_orphans = 65536
net.ipv4.tcp_max_syn_backlog = 65536
net.ipv4.tcp_window_scaling = 1
net.ipv4.tcp_timestamps = 1
net.ipv4.tcp_sack = 1
net.ipv4.tcp_slow_start_after_idle = 0

# Path MTU Discovery (critical for nested encapsulation)
net.ipv4.ip_no_pmtu_disc = 0
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_base_mss = 1280

# ARP cache tuning (for large clusters)
net.ipv4.neigh.default.gc_thresh1 = 8192
net.ipv4.neigh.default.gc_thresh2 = 32768
net.ipv4.neigh.default.gc_thresh3 = 65536

# Inotify limits (for kubelet/containerd)
fs.inotify.max_user_instances = 8192
fs.inotify.max_user_watches = 524288

# File descriptor limits
fs.file-max = 2097152

# ICMP rate limiting (security + PMTUD)
net.ipv4.icmp_ratelimit = 1000
net.ipv4.icmp_msgs_per_sec = 1000
SYSCTL_EOF

    chmod 644 /etc/sysctl.d/99-wireguard-performance.conf

    log "Applying sysctl settings..."
    if sysctl --system > /dev/null 2>&1; then
        log "  Performance tuning applied successfully"
    else
        log "  Performance tuning applied (some settings may require reboot)"
    fi

    # Verify critical settings
    local bbr_status ip_forward netdev_budget udp_timeout
    bbr_status=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo "unknown")
    ip_forward=$(sysctl -n net.ipv4.ip_forward 2>/dev/null || echo "unknown")
    netdev_budget=$(sysctl -n net.core.netdev_budget 2>/dev/null || echo "unknown")
    udp_timeout=$(sysctl -n net.netfilter.nf_conntrack_udp_timeout 2>/dev/null || echo "unknown")

    log "  Congestion control: ${bbr_status}"
    log "  IP forwarding: ${ip_forward}"
    log "  Netdev budget: ${netdev_budget}"
    log "  UDP conntrack timeout: ${udp_timeout}s"
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

    response=$(curl -sSf -X POST "${BASILICA_API_URL}/v1/gpu-nodes/register" \
        -H "Authorization: Bearer ${BASILICA_DATACENTER_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "${payload}")

    K3S_URL=$(echo "$response" | jq -r '.k3s_url')
    K3S_TOKEN=$(echo "$response" | jq -r '.k3s_token')
    K3S_NODE_NAME=$(echo "$response" | jq -r '.node_id')
    NODE_PASSWORD=$(echo "$response" | jq -r '.node_password // empty')
    NODE_LABELS=$(echo "$response" | jq -r '.node_labels | to_entries | map("--node-label \(.key)=\(.value)") | join(" ")')

    # Parse WireGuard configuration if present
    WIREGUARD_ENABLED=$(echo "$response" | jq -r '.wireguard.enabled // "false"')
    if [ "${WIREGUARD_ENABLED}" = "true" ]; then
        WG_NODE_IP=$(echo "$response" | jq -r '.wireguard.node_ip // empty')
        WG_KEEPALIVE=$(echo "$response" | jq -r '.wireguard.persistent_keepalive // empty')
        WG_PEERS_JSON=$(echo "$response" | jq -c '.wireguard.peers // []')
        local peer_count
        peer_count=$(echo "$WG_PEERS_JSON" | jq 'length')

        # Validate required WireGuard configuration
        if [ -z "${WG_NODE_IP}" ]; then
            die "WireGuard enabled but node_ip is missing from API response"
        fi
        if [ "${peer_count}" -eq 0 ]; then
            die "WireGuard enabled but no peers configured in API response"
        fi

        log "WireGuard VPN required"
        log "  Node IP: ${WG_NODE_IP}"
        log "  Server peers: ${peer_count}"
    fi

    if [ -n "$NODE_PASSWORD" ]; then
        log "Setting up node password for K3s authentication"
        mkdir -p /etc/rancher/node
        chmod 755 /etc/rancher/node
        echo -n "$NODE_PASSWORD" > /etc/rancher/node/password
        chown root:root /etc/rancher/node/password
        chmod 400 /etc/rancher/node/password
    fi

    log "Registration approved"
    log "  K3s URL: ${K3S_URL}"
    log "  Node name: ${K3S_NODE_NAME}"
}

setup_wireguard() {
    # Guard: ensure WireGuard configuration is present
    if [ -z "${WG_NODE_IP}" ] || [ -z "${WG_PEERS_JSON}" ]; then
        die "setup_wireguard called without valid WireGuard configuration"
    fi

    log "Installing WireGuard..."
    if command -v apt-get &> /dev/null; then
        apt-get update -qq
        apt-get install -y -qq wireguard wireguard-tools
    elif command -v yum &> /dev/null; then
        yum install -y epel-release
        yum install -y wireguard-tools
    else
        die "Unsupported package manager. Please install WireGuard manually."
    fi

    umask 077
    mkdir -p /etc/wireguard

    # Preserve existing keys if they exist and are valid
    if [ -f /etc/wireguard/private.key ] && [ -f /etc/wireguard/public.key ]; then
        local existing_priv existing_pub
        existing_priv=$(cat /etc/wireguard/private.key 2>/dev/null)
        existing_pub=$(cat /etc/wireguard/public.key 2>/dev/null)

        # Validate keys are non-empty and properly formatted (44 chars base64)
        if [ -n "$existing_priv" ] && [ -n "$existing_pub" ] && \
           [ ${#existing_pub} -eq 44 ]; then
            log "Using existing WireGuard keypair (public key: ${existing_pub:0:8}...)"
            WG_PRIVATE_KEY="$existing_priv"
            WG_PUBLIC_KEY="$existing_pub"
        else
            log "Existing keys invalid, generating new WireGuard keypair..."
            wg genkey > /etc/wireguard/private.key
            wg pubkey < /etc/wireguard/private.key > /etc/wireguard/public.key
            WG_PRIVATE_KEY=$(cat /etc/wireguard/private.key)
            WG_PUBLIC_KEY=$(cat /etc/wireguard/public.key)
        fi
    else
        log "Generating WireGuard keypair..."
        wg genkey > /etc/wireguard/private.key
        wg pubkey < /etc/wireguard/private.key > /etc/wireguard/public.key
        WG_PRIVATE_KEY=$(cat /etc/wireguard/private.key)
        WG_PUBLIC_KEY=$(cat /etc/wireguard/public.key)
    fi

    log "Creating WireGuard configuration with multiple peers..."

    # Start config with interface section
    # MTU 1420 accounts for WireGuard overhead (~80 bytes) to allow room for
    # Flannel VXLAN encapsulation (~50 bytes) on top
    cat > /etc/wireguard/${WIREGUARD_INTERFACE}.conf <<EOF
[Interface]
Address = ${WG_NODE_IP}/16
PrivateKey = ${WG_PRIVATE_KEY}
MTU = 1420
EOF

    # Add each peer from the JSON array
    local peer_count
    peer_count=$(echo "$WG_PEERS_JSON" | jq 'length')

    for i in $(seq 0 $((peer_count - 1))); do
        local endpoint public_key wireguard_ip vpc_subnet route_pod_network
        endpoint=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].endpoint")
        public_key=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].public_key")
        wireguard_ip=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].wireguard_ip")
        vpc_subnet=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].vpc_subnet")
        route_pod_network=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].route_pod_network")

        # Build AllowedIPs: WireGuard IP + VPC subnet + pod network (if designated)
        # NOTE: Service network (10.43.0.0/16) should NOT be routed via WireGuard
        # because ClusterIP services are virtual IPs handled locally by kube-proxy
        local allowed_ips="${wireguard_ip}/32,${vpc_subnet}"
        if [ "$route_pod_network" = "true" ]; then
            allowed_ips="${allowed_ips},10.42.0.0/16"
        fi

        log "  Adding peer: ${endpoint} (WG: ${wireguard_ip}, VPC: ${vpc_subnet})"

        cat >> /etc/wireguard/${WIREGUARD_INTERFACE}.conf <<EOF

[Peer]
PublicKey = ${public_key}
Endpoint = ${endpoint}
AllowedIPs = ${allowed_ips}
PersistentKeepalive = ${WG_KEEPALIVE}
EOF
    done

    chmod 600 /etc/wireguard/${WIREGUARD_INTERFACE}.conf

    log "Registering public key with Basilica API..."
    local key_response
    key_response=$(curl -sSf -X POST "${BASILICA_API_URL}/v1/gpu-nodes/${NODE_ID}/wireguard-key" \
        -H "Authorization: Bearer ${BASILICA_DATACENTER_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "{\"public_key\": \"${WG_PUBLIC_KEY}\"}")

    local status
    status=$(echo "$key_response" | jq -r '.status')
    if [ "$status" != "peer_added" ]; then
        die "Failed to register WireGuard public key: $key_response"
    fi

    log "Starting WireGuard interface..."
    systemctl enable wg-quick@${WIREGUARD_INTERFACE}
    systemctl start wg-quick@${WIREGUARD_INTERFACE}

    sleep 2
    if ! wg show ${WIREGUARD_INTERFACE} &> /dev/null; then
        die "WireGuard interface failed to start. Check: journalctl -u wg-quick@${WIREGUARD_INTERFACE}"
    fi

    log "WireGuard VPN established"
    log "  Interface: ${WIREGUARD_INTERFACE}"
    log "  Node IP: ${WG_NODE_IP}"
    log "  Peers: ${peer_count}"
    wg show ${WIREGUARD_INTERFACE}
}

deploy_wireguard_watchdog() {
    log "Deploying WireGuard watchdog service..."

    # Extract server IPs from WG_PEERS_JSON for watchdog
    local watchdog_servers=""
    local peer_count
    peer_count=$(echo "$WG_PEERS_JSON" | jq 'length')

    for i in $(seq 0 $((peer_count - 1))); do
        local wireguard_ip
        wireguard_ip=$(echo "$WG_PEERS_JSON" | jq -r ".[$i].wireguard_ip")
        if [ -n "$watchdog_servers" ]; then
            watchdog_servers="${watchdog_servers} ${wireguard_ip}"
        else
            watchdog_servers="${wireguard_ip}"
        fi
    done

    log "  Watchdog will monitor: ${watchdog_servers}"

    # Create watchdog script with network engineer recommendations:
    # - Parallel ping checks
    # - WireGuard handshake age verification
    # - Graduated recovery (syncconf -> reload -> restart)
    # - Startup jitter to prevent fleet synchronization
    # - Interface state polling
    cat > /usr/local/bin/wireguard-watchdog.sh <<'WATCHDOG_SCRIPT'
#!/bin/bash
# WireGuard Connectivity Watchdog
# Monitors connectivity to K3s servers and restarts WireGuard if all are unreachable
# Implements: parallel pings, handshake verification, graduated recovery, startup jitter
#
# Deployed by Basilica onboard.sh - Do not edit manually

set -euo pipefail

# Configuration (can be overridden via environment)
SERVERS="${WATCHDOG_SERVERS:-10.200.0.1 10.200.0.2 10.200.0.3}"
INTERFACE="${WATCHDOG_INTERFACE:-wg0}"
FAILURE_THRESHOLD="${WATCHDOG_FAILURE_THRESHOLD:-3}"
CHECK_INTERVAL="${WATCHDOG_CHECK_INTERVAL:-30}"
PING_TIMEOUT="${WATCHDOG_PING_TIMEOUT:-3}"
STARTUP_JITTER="${WATCHDOG_STARTUP_JITTER:-5}"
HANDSHAKE_MAX_AGE="${WATCHDOG_HANDSHAKE_MAX_AGE:-150}"

# Convert space-separated servers to array
IFS=' ' read -ra SERVER_ARRAY <<< "$SERVERS"

# State
failure_count=0

log() {
    logger -t wireguard-watchdog "$1"
    echo "$(date -Iseconds) $1"
}

# Check if WireGuard handshake is fresh (not stale)
check_handshake_age() {
    local latest_handshake
    latest_handshake=$(wg show "$INTERFACE" latest-handshakes 2>/dev/null | awk '{print $2}' | sort -rn | head -1)

    if [[ -z "$latest_handshake" || "$latest_handshake" == "0" ]]; then
        log "WARNING: No WireGuard handshake recorded yet"
        return 1
    fi

    local current_time
    current_time=$(date +%s)
    local age=$((current_time - latest_handshake))

    if [[ $age -gt $HANDSHAKE_MAX_AGE ]]; then
        log "WARNING: WireGuard handshake stale (${age}s > ${HANDSHAKE_MAX_AGE}s)"
        return 1
    fi

    return 0
}

# Parallel ping check - returns 0 if any server is reachable
check_ping_parallel() {
    local temp_dir
    temp_dir=$(mktemp -d)
    local pids=()

    # Launch parallel pings
    for i in "${!SERVER_ARRAY[@]}"; do
        (ping -c 1 -W "$PING_TIMEOUT" "${SERVER_ARRAY[$i]}" >/dev/null 2>&1 && touch "$temp_dir/success_$i") &
        pids+=($!)
    done

    # Wait for all pings to complete (with timeout safety)
    local wait_start
    wait_start=$(date +%s)
    local max_wait=$((PING_TIMEOUT + 2))

    while [[ ${#pids[@]} -gt 0 ]]; do
        for i in "${!pids[@]}"; do
            if ! kill -0 "${pids[$i]}" 2>/dev/null; then
                unset 'pids[$i]'
            fi
        done
        pids=("${pids[@]}")

        local elapsed=$(($(date +%s) - wait_start))
        if [[ $elapsed -gt $max_wait ]]; then
            for pid in "${pids[@]}"; do
                kill -9 "$pid" 2>/dev/null || true
            done
            break
        fi
        sleep 0.1
    done

    # Check results
    local success_count
    success_count=$(find "$temp_dir" -maxdepth 1 -name 'success_*' 2>/dev/null | wc -l)
    rm -rf "$temp_dir"

    [[ $success_count -gt 0 ]]
}

# Combined connectivity check: parallel pings + handshake verification
check_connectivity() {
    if ! check_ping_parallel; then
        return 1
    fi

    if ! check_handshake_age; then
        return 1
    fi

    return 0
}

# Wait for interface to reach UP state
wait_for_interface_up() {
    local max_retries=20
    local retry=0

    while [[ $retry -lt $max_retries ]]; do
        if ip link show "$INTERFACE" 2>/dev/null | grep -q "state UP\|state UNKNOWN"; then
            return 0
        fi
        sleep 0.5
        ((retry++))
    done

    log "WARNING: Interface $INTERFACE did not reach UP state within ${max_retries}s"
    return 1
}

# Graduated recovery: syncconf -> down/up -> systemctl restart
recover_wireguard() {
    log "Attempting WireGuard recovery for $INTERFACE"

    # Step 1: Try soft recovery via wg syncconf (preserves active connections)
    local wg_conf="/etc/wireguard/${INTERFACE}.conf"
    if [[ -f "$wg_conf" ]]; then
        log "Step 1: Attempting soft recovery via wg syncconf"
        if wg syncconf "$INTERFACE" <(wg-quick strip "$INTERFACE" 2>/dev/null) 2>/dev/null; then
            sleep 3
            if check_ping_parallel; then
                log "Connectivity restored via soft recovery (syncconf)"
                return 0
            fi
            log "Soft recovery did not restore connectivity"
        else
            log "Soft recovery (syncconf) failed, escalating"
        fi
    fi

    # Step 2: Try interface reload (down + up)
    log "Step 2: Attempting interface reload (down/up)"
    if wg-quick down "$INTERFACE" 2>/dev/null; then
        sleep 1
        if wg-quick up "$INTERFACE" 2>/dev/null; then
            wait_for_interface_up
            sleep 3
            if check_ping_parallel; then
                log "Connectivity restored via interface reload"
                return 0
            fi
            log "Interface reload did not restore connectivity"
        else
            log "wg-quick up failed after down"
        fi
    else
        log "wg-quick down failed, escalating to systemctl"
    fi

    # Step 3: Full systemctl restart as last resort
    log "Step 3: Performing full systemctl restart"
    if systemctl restart "wg-quick@${INTERFACE}"; then
        wait_for_interface_up
        sleep 5

        if check_ping_parallel; then
            log "Connectivity restored via systemctl restart"
            return 0
        fi
        log "ERROR: Connectivity not restored after full restart"
        return 1
    fi

    log "ERROR: systemctl restart failed"
    return 1
}

main() {
    log "Starting WireGuard watchdog v2.0"
    log "Servers: ${SERVER_ARRAY[*]}"
    log "Config: threshold=$FAILURE_THRESHOLD, interval=${CHECK_INTERVAL}s, ping_timeout=${PING_TIMEOUT}s"
    log "Handshake max age: ${HANDSHAKE_MAX_AGE}s"

    # Apply startup jitter to prevent fleet synchronization
    if [[ $STARTUP_JITTER -gt 0 ]]; then
        local jitter=$((RANDOM % (STARTUP_JITTER + 1)))
        log "Applying startup jitter: ${jitter}s"
        sleep "$jitter"
    fi

    while true; do
        if check_connectivity; then
            if [[ $failure_count -gt 0 ]]; then
                log "Connectivity restored (was failing for $failure_count checks)"
            fi
            failure_count=0
        else
            ((failure_count++)) || true
            log "Connectivity check failed ($failure_count/$FAILURE_THRESHOLD)"

            if [[ $failure_count -ge $FAILURE_THRESHOLD ]]; then
                recover_wireguard
                failure_count=0
                sleep 10
            fi
        fi

        # Add jitter to check interval to prevent fleet sync
        local sleep_jitter=$((RANDOM % 3))
        sleep $((CHECK_INTERVAL + sleep_jitter))
    done
}

trap 'log "Received signal, shutting down"; exit 0' SIGTERM SIGINT

main
WATCHDOG_SCRIPT

    chmod 755 /usr/local/bin/wireguard-watchdog.sh

    # Create systemd service
    cat > /etc/systemd/system/wireguard-watchdog.service <<EOF
[Unit]
Description=WireGuard Connectivity Watchdog
Documentation=https://github.com/basilica-ai/basilica
After=network-online.target wg-quick@${WIREGUARD_INTERFACE}.service
Wants=network-online.target
Requires=wg-quick@${WIREGUARD_INTERFACE}.service

[Service]
Type=simple
User=root
Environment="WATCHDOG_SERVERS=${watchdog_servers}"
Environment="WATCHDOG_INTERFACE=${WIREGUARD_INTERFACE}"
Environment="WATCHDOG_FAILURE_THRESHOLD=3"
Environment="WATCHDOG_CHECK_INTERVAL=30"
Environment="WATCHDOG_PING_TIMEOUT=3"
Environment="WATCHDOG_STARTUP_JITTER=5"
Environment="WATCHDOG_HANDSHAKE_MAX_AGE=150"
ExecStart=/usr/local/bin/wireguard-watchdog.sh
Restart=always
RestartSec=30
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
MemoryMax=50M
CPUQuota=5%

[Install]
WantedBy=multi-user.target
EOF

    # Enable and start watchdog
    systemctl daemon-reload
    systemctl enable wireguard-watchdog
    systemctl start wireguard-watchdog

    # Verify watchdog is running
    if systemctl is-active --quiet wireguard-watchdog; then
        log "  WireGuard watchdog deployed and running"
    else
        log "  WARNING: WireGuard watchdog failed to start"
    fi
}

join_k3s_cluster() {
    log "Installing K3s agent..."

    local TAINTS="--kubelet-arg=register-with-taints=basilica.ai/unvalidated=true:NoSchedule"
    local NODE_IP_FLAG=""
    local FLANNEL_IFACE_FLAG=""

    # Use WireGuard IP for kubelet and flannel if VPN is enabled
    if [ "${WIREGUARD_ENABLED}" = "true" ]; then
        NODE_IP_FLAG="--node-ip ${WG_NODE_IP}"
        FLANNEL_IFACE_FLAG="--flannel-iface ${WIREGUARD_INTERFACE}"
        # Add WireGuard-specific labels for scheduling and affinity rules
        NODE_LABELS="${NODE_LABELS} --node-label basilica.ai/wireguard=true --node-label basilica.ai/network=remote"
        log "Using WireGuard IP for kubelet: ${WG_NODE_IP}"
        log "Using WireGuard interface for Flannel VXLAN: ${WIREGUARD_INTERFACE}"
        log "WireGuard MTU: 1420 (configured in wg0.conf)"
    fi

    curl -sfL https://get.k3s.io | \
        INSTALL_K3S_VERSION="v1.31.1+k3s1" \
        K3S_URL="${K3S_URL}" \
        K3S_TOKEN="${K3S_TOKEN}" \
        K3S_NODE_NAME="${K3S_NODE_NAME}" \
        INSTALL_K3S_EXEC="agent ${NODE_LABELS} ${TAINTS} ${NODE_IP_FLAG} ${FLANNEL_IFACE_FLAG}" \
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
