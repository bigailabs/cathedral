#!/bin/bash
# Disk cleanup script for K3s nodes
# Logs to /var/log/disk-cleanup.log

set -euo pipefail

LOG_FILE="/var/log/disk-cleanup.log"
DRY_RUN=${DRY_RUN:-false}

log() {
    echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')] $*" | tee -a "$LOG_FILE"
}

get_disk_usage() {
    df / | tail -1 | awk '{print $5}' | sed 's/%//'
}

cleanup_containerd_images() {
    log "INFO: Cleaning up unused container images"

    if [ "$DRY_RUN" = "false" ]; then
        crictl rmi --prune 2>&1 | tee -a "$LOG_FILE" || true
    else
        log "INFO: [DRY RUN] Would run: crictl rmi --prune"
    fi
}

cleanup_pod_logs() {
    log "INFO: Cleaning up pod logs older than 7 days"

    LOG_DIR="/var/log/pods"
    if [ -d "$LOG_DIR" ]; then
        if [ "$DRY_RUN" = "false" ]; then
            find "$LOG_DIR" -type f -name "*.log" -mtime +7 -delete 2>&1 | tee -a "$LOG_FILE"
        else
            count=$(find "$LOG_DIR" -type f -name "*.log" -mtime +7 2>/dev/null | wc -l)
            log "INFO: [DRY RUN] Would delete $count log files"
        fi
    fi
}

cleanup_journal_logs() {
    log "INFO: Vacuuming journal logs (keep last 7 days)"

    if [ "$DRY_RUN" = "false" ]; then
        journalctl --vacuum-time=7d 2>&1 | tee -a "$LOG_FILE"
    else
        log "INFO: [DRY RUN] Would run: journalctl --vacuum-time=7d"
    fi
}

cleanup_tmp_files() {
    log "INFO: Cleaning /tmp and /var/tmp files older than 7 days"

    for dir in /tmp /var/tmp; do
        if [ "$DRY_RUN" = "false" ]; then
            find "$dir" -type f -atime +7 -delete 2>&1 | tee -a "$LOG_FILE" || true
        else
            count=$(find "$dir" -type f -atime +7 2>/dev/null | wc -l)
            log "INFO: [DRY RUN] Would delete $count files from $dir"
        fi
    done
}

cleanup_evicted_pods() {
    log "INFO: Removing evicted pods"

    if ! command -v kubectl &> /dev/null; then
        log "WARN: kubectl not found, skipping evicted pod cleanup"
        return
    fi

    if [ "$DRY_RUN" = "false" ]; then
        kubectl get pods --all-namespaces --field-selector=status.phase=Failed -o json 2>/dev/null | \
            jq -r '.items[] | select(.status.reason=="Evicted") | "\(.metadata.namespace) \(.metadata.name)"' 2>/dev/null | \
            while read -r namespace pod; do
                if [ -n "$namespace" ] && [ -n "$pod" ]; then
                    log "INFO: Deleting evicted pod $namespace/$pod"
                    kubectl delete pod "$pod" -n "$namespace" 2>&1 | tee -a "$LOG_FILE"
                fi
            done || true
    else
        count=$(kubectl get pods --all-namespaces --field-selector=status.phase=Failed -o json 2>/dev/null | \
            jq '[.items[] | select(.status.reason=="Evicted")] | length' 2>/dev/null || echo "0")
        log "INFO: [DRY RUN] Would delete $count evicted pods"
    fi
}

main() {
    log "========================================"
    log "INFO: Starting disk cleanup"
    log "INFO: Current disk usage: $(get_disk_usage)%"

    USAGE=$(get_disk_usage)
    THRESHOLD=85

    if [ "$USAGE" -ge "$THRESHOLD" ]; then
        log "WARN: Disk usage at ${USAGE}%, exceeds threshold ${THRESHOLD}%"

        cleanup_containerd_images
        cleanup_pod_logs
        cleanup_evicted_pods
        cleanup_journal_logs
        cleanup_tmp_files

        log "INFO: Cleanup complete. New disk usage: $(get_disk_usage)%"
    else
        log "INFO: Disk usage at ${USAGE}%, below threshold ${THRESHOLD}%"
    fi

    log "========================================"
}

main "$@"
