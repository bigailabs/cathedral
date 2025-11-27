#!/bin/bash
# WireGuard Connectivity Watchdog
# Monitors connectivity to K3s servers and restarts WireGuard if all are unreachable
# Implements: parallel pings, handshake verification, graduated recovery, startup jitter
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
