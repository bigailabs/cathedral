#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/basilica/disk_cleanup.log"
DATE=$(date '+%Y-%m-%d %H:%M:%S')

log() {
    echo "[$DATE] $1" | tee -a "$LOG_FILE"
}

log "Starting disk cleanup process..."

DISK_USAGE=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
log "Current disk usage: ${DISK_USAGE}%"

if [ "$DISK_USAGE" -lt 70 ]; then
    log "Disk usage is below threshold (70%), no cleanup needed"
    exit 0
fi

log "Disk usage is above threshold, starting cleanup..."

log "Cleaning Docker system..."
docker system prune -f --volumes --filter "until=168h" 2>&1 | tee -a "$LOG_FILE"

log "Cleaning journal logs older than 7 days..."
journalctl --vacuum-time=7d 2>&1 | tee -a "$LOG_FILE"

log "Cleaning apt cache..."
apt-get clean 2>&1 | tee -a "$LOG_FILE"

log "Removing old log files..."
find /var/log -name "*.log" -type f -mtime +30 -delete 2>&1 | tee -a "$LOG_FILE"
find /var/log -name "*.gz" -type f -mtime +30 -delete 2>&1 | tee -a "$LOG_FILE"

log "Cleaning Prometheus old WAL segments..."
find /var/lib/docker/volumes/prometheus_data/_data/wal -name "*.tmp" -type f -mtime +7 -delete 2>&1 | tee -a "$LOG_FILE" || true

log "Cleaning Loki chunks older than 14 days..."
find /var/lib/docker/volumes/loki_data/_data/chunks -type f -mtime +14 -delete 2>&1 | tee -a "$LOG_FILE" || true

DISK_USAGE_AFTER=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
log "Disk usage after cleanup: ${DISK_USAGE_AFTER}%"
log "Freed space: $((DISK_USAGE - DISK_USAGE_AFTER))%"

if [ "$DISK_USAGE_AFTER" -gt 85 ]; then
    log "WARNING: Disk usage still high after cleanup! Manual intervention may be required."
    exit 1
fi

log "Disk cleanup completed successfully"
