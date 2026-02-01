#!/bin/bash
# Basilica Localnet - Start Services
# Usage: ./start.sh [profile] [--build]
#
# Profiles:
#   network     - Subtensor only
#   validator   - Subtensor + Validator
#   miner       - Above + Miner
#   monitoring  - All + Prometheus + Grafana
#   all         - Everything (default)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Parse arguments
PROFILE="${1:-all}"
BUILD_FLAG=""

for arg in "$@"; do
    case $arg in
        --build)
            BUILD_FLAG="--build"
            shift
            ;;
        -*)
            echo "Unknown option: $arg"
            exit 1
            ;;
    esac
done

# Normalize profile name
case "${PROFILE}" in
    network|subtensor)
        PROFILE="network"
        ;;
    validator|val)
        PROFILE="validator"
        ;;
    miner|min)
        PROFILE="miner"
        ;;
    monitoring|monitor|mon)
        PROFILE="monitoring"
        ;;
    all|"")
        PROFILE="all"
        ;;
    *)
        echo "Unknown profile: ${PROFILE}"
        echo ""
        echo "Available profiles:"
        echo "  network     - Subtensor only"
        echo "  validator   - Subtensor + Validator"
        echo "  miner       - Above + Miner"
        echo "  monitoring  - All + Prometheus + Grafana"
        echo "  all         - Everything (default)"
        exit 1
        ;;
esac

echo "========================================"
echo "  Starting Basilica Localnet"
echo "  Profile: ${PROFILE}"
echo "========================================"
echo ""

# Function to wait for a service
wait_for_service() {
    local name=$1
    local url=$2
    local max_attempts=${3:-30}
    local attempt=1

    echo -n "  Waiting for ${name}..."
    while [ $attempt -le $max_attempts ]; do
        if curl -sf "${url}" > /dev/null 2>&1; then
            echo " Ready!"
            return 0
        fi
        echo -n "."
        sleep 2
        ((attempt++))
    done
    echo " Timeout!"
    return 1
}

# Start services based on profile
if [ "${PROFILE}" = "all" ]; then
    echo "[1/2] Starting all services..."
    docker compose up -d ${BUILD_FLAG}
else
    echo "[1/2] Starting services with profile: ${PROFILE}..."
    docker compose --profile "${PROFILE}" up -d ${BUILD_FLAG}
fi

echo ""
echo "[2/2] Waiting for services..."

# Wait for services based on profile
case "${PROFILE}" in
    network)
        wait_for_service "Subtensor" "http://localhost:9944/health"
        ;;
    validator)
        wait_for_service "Subtensor" "http://localhost:9944/health"
        wait_for_service "Validator" "http://localhost:8080/health" 60
        ;;
    miner)
        wait_for_service "Subtensor" "http://localhost:9944/health"
        wait_for_service "Validator" "http://localhost:8080/health" 60
        wait_for_service "Miner" "http://localhost:9091/metrics" 60
        ;;
    monitoring|all)
        wait_for_service "Subtensor" "http://localhost:9944/health"
        wait_for_service "Validator" "http://localhost:8080/health" 60
        wait_for_service "Miner" "http://localhost:9091/metrics" 60
        wait_for_service "Prometheus" "http://localhost:9099/-/healthy"
        wait_for_service "Grafana" "http://localhost:3000/api/health"
        ;;
esac

echo ""
echo "========================================"
echo "  Services Started!"
echo "========================================"
echo ""

# Display running services
echo "Running services:"
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null | head -20

echo ""
echo "Endpoints:"
case "${PROFILE}" in
    network)
        echo "  Subtensor:   ws://localhost:9944"
        ;;
    validator)
        echo "  Subtensor:   ws://localhost:9944"
        echo "  Validator:   http://localhost:8080 (API)"
        echo "               http://localhost:9090/metrics"
        ;;
    miner)
        echo "  Subtensor:   ws://localhost:9944"
        echo "  Validator:   http://localhost:8080 (API)"
        echo "               http://localhost:9090/metrics"
        echo "  Miner:       localhost:8092 (gRPC)"
        echo "               http://localhost:9091/metrics"
        ;;
    monitoring|all)
        echo "  Subtensor:   ws://localhost:9944"
        echo "  Validator:   http://localhost:8080 (API)"
        echo "               http://localhost:9090/metrics"
        echo "  Miner:       localhost:8092 (gRPC)"
        echo "               http://localhost:9091/metrics"
        echo "  Prometheus:  http://localhost:9099"
        echo "  Grafana:     http://localhost:3000 (admin/admin)"
        ;;
esac

echo ""
echo "Commands:"
echo "  Check health:  ./test.sh"
echo "  View logs:     docker compose logs -f [service]"
echo "  Stop:          docker compose down"
echo ""
