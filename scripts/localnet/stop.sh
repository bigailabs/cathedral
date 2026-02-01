#!/bin/bash
# Basilica Localnet - Stop Services
# Usage: ./stop.sh [--clean]
#
# Options:
#   --clean   Remove all data (volumes + wallets) for fresh start

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Parse arguments
CLEAN=false

for arg in "$@"; do
    case $arg in
        --clean|--purge|--reset)
            CLEAN=true
            ;;
        -h|--help)
            echo "Usage: ./stop.sh [--clean]"
            echo ""
            echo "Options:"
            echo "  --clean   Remove all data (volumes + wallets) for fresh start"
            echo ""
            exit 0
            ;;
        -*)
            echo "Unknown option: $arg"
            echo "Use --help for usage"
            exit 1
            ;;
    esac
done

echo "========================================"
echo "  Stopping Basilica Localnet"
if [ "$CLEAN" = true ]; then
    echo "  Mode: Clean (remove all data)"
fi
echo "========================================"
echo ""

# Stop containers
echo "[1/4] Stopping containers..."
docker compose down 2>/dev/null || true
docker compose --profile network down 2>/dev/null || true
docker compose --profile validator down 2>/dev/null || true
docker compose --profile miner down 2>/dev/null || true
docker compose --profile monitoring down 2>/dev/null || true

if [ "$CLEAN" = true ]; then
    echo ""
    echo "[2/4] Removing Docker volumes..."
    docker compose down -v 2>/dev/null || true

    # Also remove any orphaned volumes from this project
    docker volume ls --filter "name=localnet" -q | xargs -r docker volume rm 2>/dev/null || true

    echo ""
    echo "[3/4] Removing wallets directory..."
    if [ -d "${SCRIPT_DIR}/wallets" ]; then
        rm -rf "${SCRIPT_DIR}/wallets"
        echo "  Removed: ${SCRIPT_DIR}/wallets"
    else
        echo "  No wallets directory found"
    fi

    echo ""
    echo "[4/4] Removing Docker network..."
    docker network rm localnet_basilica-localnet 2>/dev/null || true
else
    echo ""
    echo "[2/4] Skipping volume removal (use --clean to remove)"
    echo "[3/4] Skipping wallet removal (use --clean to remove)"
    echo "[4/4] Skipping network removal (use --clean to remove)"
fi

echo ""
echo "========================================"
echo "  Localnet Stopped"
echo "========================================"
echo ""

# Show status
RUNNING=$(docker compose ps -q 2>/dev/null | wc -l | tr -d ' ')
if [ "$RUNNING" -gt 0 ]; then
    echo "Warning: Some containers may still be running:"
    docker compose ps 2>/dev/null
else
    echo "All containers stopped."
fi

if [ "$CLEAN" = true ]; then
    echo ""
    echo "All data has been removed. Ready for fresh start:"
    echo "  1. ./start.sh network"
    echo "  2. ./init-subnet.sh"
else
    echo ""
    echo "Data preserved. To start again: ./start.sh"
    echo "To remove all data: ./stop.sh --clean"
fi
echo ""
