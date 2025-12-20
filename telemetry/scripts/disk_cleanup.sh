#!/bin/bash
set -euo pipefail

LOG_FILE="/var/log/basilica/disk_cleanup.log"
LOKI_CHUNKS_DIR="/var/lib/docker/volumes/telemetry_loki_data/_data/chunks"
LOKI_WAL_DIR="/var/lib/docker/volumes/telemetry_loki_data/_data/wal"
PROMETHEUS_WAL_DIR="/var/lib/docker/volumes/telemetry_prometheus_data/_data/wal"

THRESHOLD_WARNING=90
THRESHOLD_CRITICAL=95
RETENTION_DAYS=1

mkdir -p /var/log/basilica

log() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $1" | tee -a "$LOG_FILE"
}

get_disk_usage() {
    df / | tail -1 | awk '{print $5}' | sed 's/%//'
}

log "Starting disk cleanup check..."

DISK_USAGE=$(get_disk_usage)
log "Current disk usage: ${DISK_USAGE}%"

if [ "$DISK_USAGE" -lt "$THRESHOLD_WARNING" ]; then
    log "Disk usage below ${THRESHOLD_WARNING}% threshold, no cleanup needed"
    exit 0
fi

log "Disk usage at ${DISK_USAGE}% - above ${THRESHOLD_WARNING}% threshold, starting cleanup..."

if [ "$DISK_USAGE" -ge "$THRESHOLD_CRITICAL" ]; then
    log "CRITICAL: Disk usage at ${DISK_USAGE}% - aggressive cleanup required"
    RETENTION_DAYS=0
fi

log "Cleaning Loki chunks older than ${RETENTION_DAYS} days..."
if [ -d "$LOKI_CHUNKS_DIR" ]; then
    find "$LOKI_CHUNKS_DIR" -type f -mtime +"$RETENTION_DAYS" -delete 2>&1 | tee -a "$LOG_FILE" || true
    log "Loki chunks cleanup complete"
else
    log "Loki chunks directory not found: $LOKI_CHUNKS_DIR"
fi

if [ "$DISK_USAGE" -ge "$THRESHOLD_CRITICAL" ] && [ -d "$LOKI_WAL_DIR" ]; then
    log "Cleaning Loki WAL directory..."
    find "$LOKI_WAL_DIR" -type f -mtime +0 -delete 2>&1 | tee -a "$LOG_FILE" || true
fi

log "Cleaning Docker unused resources..."
docker system prune -f --filter "until=24h" 2>&1 | tee -a "$LOG_FILE" || true

log "Cleaning journal logs older than 3 days..."
journalctl --vacuum-time=3d 2>&1 | tee -a "$LOG_FILE" || true

log "Cleaning apt cache..."
apt-get clean 2>&1 | tee -a "$LOG_FILE" || true

log "Removing old log files..."
find /var/log -name "*.log" -type f -mtime +7 -delete 2>&1 | tee -a "$LOG_FILE" || true
find /var/log -name "*.gz" -type f -mtime +7 -delete 2>&1 | tee -a "$LOG_FILE" || true

if [ -d "$PROMETHEUS_WAL_DIR" ]; then
    log "Cleaning Prometheus old WAL segments..."
    find "$PROMETHEUS_WAL_DIR" -name "*.tmp" -type f -mtime +1 -delete 2>&1 | tee -a "$LOG_FILE" || true
fi

DISK_USAGE_AFTER=$(get_disk_usage)
log "Disk usage after cleanup: ${DISK_USAGE_AFTER}%"
log "Freed: $((DISK_USAGE - DISK_USAGE_AFTER))%"

if [ "$DISK_USAGE_AFTER" -ge "$THRESHOLD_CRITICAL" ]; then
    log "ERROR: Disk still critically full after cleanup - manual intervention required"
    exit 1
fi

log "Disk cleanup completed successfully"
