#!/bin/bash
# WireGuard Connectivity Watchdog
# Monitors connectivity to K3s servers and restarts WireGuard if all are unreachable
# Implements: parallel pings, handshake verification, graduated recovery, startup jitter
# K8s integration: taints node during WireGuard failures to prevent pod scheduling
#
# Deployed by Ansible/Basilica - Do not edit manually

set -euo pipefail

# Configuration (can be overridden via environment)
SERVERS="${WATCHDOG_SERVERS:-10.200.0.1 10.200.0.2 10.200.0.3}"
INTERFACE="${WATCHDOG_INTERFACE:-wg0}"
FAILURE_THRESHOLD="${WATCHDOG_FAILURE_THRESHOLD:-3}"
CHECK_INTERVAL="${WATCHDOG_CHECK_INTERVAL:-30}"
PING_TIMEOUT="${WATCHDOG_PING_TIMEOUT:-3}"
STARTUP_JITTER="${WATCHDOG_STARTUP_JITTER:-5}"
HANDSHAKE_MAX_AGE="${WATCHDOG_HANDSHAKE_MAX_AGE:-150}"

# K8s tainting configuration
K8S_TAINTING="${WATCHDOG_K8S_TAINTING:-true}"
TAINT_KEY="${WATCHDOG_TAINT_KEY:-basilica.ai/wireguard-failure}"
TAINT_VALUE="${WATCHDOG_TAINT_VALUE:-true}"
TAINT_EFFECT="${WATCHDOG_TAINT_EFFECT:-NoExecute}"
KUBECTL="${WATCHDOG_KUBECTL:-/usr/local/bin/k3s kubectl}"

# Metrics configuration
METRICS_ENABLED="${WATCHDOG_METRICS_ENABLED:-true}"
METRICS_DIR="${WATCHDOG_METRICS_DIR:-/var/lib/node-exporter/textfile}"

# Convert space-separated servers to array
IFS=' ' read -ra SERVER_ARRAY <<< "$SERVERS"

# State
failure_count=0
node_tainted=false

log() {
    logger -t wireguard-watchdog "$1"
    echo "$(date -Iseconds) $1"
}

# Collect VXLAN health metrics for Prometheus node-exporter textfile collector
collect_vxlan_metrics() {
    if [[ "$METRICS_ENABLED" != "true" ]]; then
        return 0
    fi

    # Ensure metrics directory exists
    mkdir -p "$METRICS_DIR" 2>/dev/null || return 0

    local metrics_file="${METRICS_DIR}/vxlan_health.prom"
    local temp_file="${metrics_file}.tmp"

    # Count FDB entries for flannel.1
    local fdb_count=0
    if ip link show flannel.1 &>/dev/null; then
        fdb_count=$(bridge fdb show dev flannel.1 2>/dev/null | wc -l)
    fi

    # Count neighbor entries for flannel.1
    local neigh_count=0
    local stale_count=0
    if ip link show flannel.1 &>/dev/null; then
        neigh_count=$(ip neigh show dev flannel.1 2>/dev/null | wc -l)
        stale_count=$(ip neigh show dev flannel.1 2>/dev/null | grep -c STALE || echo 0)
    fi

    # Count routes via wg0 that should be via flannel.1 (pod CIDRs 10.42.x.x)
    local wg0_route_count=0
    wg0_route_count=$(ip route show 2>/dev/null | grep -c "10\.42\..* dev wg0" || echo 0)

    # Get flannel.1 MAC for duplicate detection
    local vtep_mac=""
    if ip link show flannel.1 &>/dev/null; then
        vtep_mac=$(ip link show flannel.1 2>/dev/null | grep -oP 'link/ether \K[0-9a-f:]+' || echo "")
    fi

    # Write metrics in Prometheus format
    cat > "$temp_file" <<EOF
# HELP vxlan_fdb_entries_total Number of FDB entries on flannel.1 interface
# TYPE vxlan_fdb_entries_total gauge
vxlan_fdb_entries_total $fdb_count

# HELP vxlan_neighbor_entries_total Number of neighbor entries on flannel.1 interface
# TYPE vxlan_neighbor_entries_total gauge
vxlan_neighbor_entries_total $neigh_count

# HELP vxlan_stale_neighbor_entries Number of stale neighbor entries on flannel.1
# TYPE vxlan_stale_neighbor_entries gauge
vxlan_stale_neighbor_entries $stale_count

# HELP flannel_route_via_wg0 Number of pod CIDR routes incorrectly via wg0
# TYPE flannel_route_via_wg0 gauge
flannel_route_via_wg0 $wg0_route_count

# HELP flannel_vtep_mac_info VTEP MAC address info for duplicate detection
# TYPE flannel_vtep_mac_info gauge
flannel_vtep_mac_info{vtep_mac="$vtep_mac"} 1
EOF

    # Atomic move to avoid partial reads
    mv "$temp_file" "$metrics_file" 2>/dev/null || rm -f "$temp_file"
}

# Get current node name from K3s agent
get_node_name() {
    local node_name
    node_name=$(hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo "")
    if [[ -z "$node_name" ]]; then
        log "ERROR: Cannot determine node name"
        return 1
    fi
    echo "$node_name"
}

# Add taint to node to prevent pod scheduling during WireGuard failure
taint_node() {
    if [[ "$K8S_TAINTING" != "true" ]]; then
        return 0
    fi

    if [[ "$node_tainted" == "true" ]]; then
        return 0
    fi

    local node_name
    node_name=$(get_node_name) || return 1

    log "Adding taint ${TAINT_KEY}=${TAINT_VALUE}:${TAINT_EFFECT} to node ${node_name}"

    if $KUBECTL taint nodes "$node_name" "${TAINT_KEY}=${TAINT_VALUE}:${TAINT_EFFECT}" --overwrite 2>/dev/null; then
        node_tainted=true
        log "Node ${node_name} tainted successfully"
        return 0
    else
        log "WARNING: Failed to taint node ${node_name} (may not have API access)"
        return 1
    fi
}

# Remove taint from node when connectivity is restored
untaint_node() {
    if [[ "$K8S_TAINTING" != "true" ]]; then
        return 0
    fi

    if [[ "$node_tainted" != "true" ]]; then
        return 0
    fi

    local node_name
    node_name=$(get_node_name) || return 1

    log "Removing taint ${TAINT_KEY} from node ${node_name}"

    if $KUBECTL taint nodes "$node_name" "${TAINT_KEY}-" 2>/dev/null; then
        node_tainted=false
        log "Node ${node_name} untainted successfully"
        return 0
    else
        log "WARNING: Failed to untaint node ${node_name}"
        return 1
    fi
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
    log "Starting WireGuard watchdog v2.1"
    log "Servers: ${SERVER_ARRAY[*]}"
    log "Config: threshold=$FAILURE_THRESHOLD, interval=${CHECK_INTERVAL}s, ping_timeout=${PING_TIMEOUT}s"
    log "Handshake max age: ${HANDSHAKE_MAX_AGE}s"
    log "K8s tainting: ${K8S_TAINTING} (${TAINT_KEY}:${TAINT_EFFECT})"

    # Apply startup jitter to prevent fleet synchronization
    if [[ $STARTUP_JITTER -gt 0 ]]; then
        local jitter=$((RANDOM % (STARTUP_JITTER + 1)))
        log "Applying startup jitter: ${jitter}s"
        sleep "$jitter"
    fi

    while true; do
        # Collect VXLAN metrics on each iteration
        collect_vxlan_metrics

        if check_connectivity; then
            if [[ $failure_count -gt 0 ]]; then
                log "Connectivity restored (was failing for $failure_count checks)"
                untaint_node
            fi
            failure_count=0
        else
            ((failure_count++)) || true
            log "Connectivity check failed ($failure_count/$FAILURE_THRESHOLD)"

            if [[ $failure_count -ge $FAILURE_THRESHOLD ]]; then
                taint_node
                recover_wireguard
                if check_connectivity; then
                    untaint_node
                fi
                failure_count=0
                sleep 10
            fi
        fi

        # Add jitter to check interval to prevent fleet sync
        local sleep_jitter=$((RANDOM % 3))
        sleep $((CHECK_INTERVAL + sleep_jitter))
    done
}

cleanup() {
    log "Received signal, shutting down"
    if [[ "$node_tainted" == "true" ]]; then
        log "Removing taint before shutdown"
        untaint_node
    fi
    exit 0
}

trap cleanup SIGTERM SIGINT

main
