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

# Generate SSH keys if they don't exist
SSH_KEY_DIR="${SCRIPT_DIR}/ssh-keys"
mkdir -p "${SSH_KEY_DIR}"
MINER_KEY="${SSH_KEY_DIR}/miner_node_key"
if [ ! -f "${MINER_KEY}" ]; then
    echo "Generating miner SSH key..."
    ssh-keygen -t ed25519 -f "${MINER_KEY}" -N "" -C "basilica-miner-localnet"
    chmod 600 "${MINER_KEY}"
    chmod 644 "${MINER_KEY}.pub"
    echo ""
fi

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

# Function to wait for a TCP port (for gRPC services without HTTP endpoints)
wait_for_port() {
    local name=$1
    local host=$2
    local port=$3
    local max_attempts=${4:-30}
    local attempt=1

    echo -n "  Waiting for ${name}..."
    while [ $attempt -le $max_attempts ]; do
        if nc -z "${host}" "${port}" 2>/dev/null; then
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

# Function to initialize wallets and subnet
init_subnet_if_needed() {
    local wallets_dir="${SCRIPT_DIR}/wallets"

    # Check if wallets already exist
    if [ -f "${wallets_dir}/validator/hotkeys/default" ] && [ -f "${wallets_dir}/miner_1/hotkeys/default" ]; then
        echo "  Wallets already exist, skipping initialization"
        return 0
    fi

    echo "  Initializing subnet (creating wallets, funding, registering)..."

    # Check prerequisites
    if ! command -v uvx &> /dev/null; then
        echo "  ERROR: uv is not installed (required for btcli)"
        echo "  Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        return 1
    fi

    if ! command -v jq &> /dev/null; then
        echo "  ERROR: jq is not installed"
        echo "  Install: brew install jq (macOS) or apt install jq (Linux)"
        return 1
    fi

    # Run init-subnet.sh
    "${SCRIPT_DIR}/init-subnet.sh"
}

# Two-phase startup: Start subtensor first, init wallets, then start remaining services
# This prevents race conditions where validator starts before wallets exist

echo "[1/4] Starting subtensor (network profile)..."
docker compose --profile network up -d ${BUILD_FLAG}

echo ""
echo "[2/4] Waiting for Subtensor..."
wait_for_service "Subtensor" "http://localhost:9944/health"

echo ""
echo "[3/4] Initializing subnet..."
init_subnet_if_needed

# For network-only profile, we're done
if [ "${PROFILE}" = "network" ]; then
    echo ""
    echo "[4/4] Network profile complete."
else
    echo ""
    echo "[4/4] Starting remaining services for profile: ${PROFILE}..."

    # Start the remaining services for the requested profile
    if [ "${PROFILE}" = "all" ]; then
        docker compose up -d ${BUILD_FLAG}
    else
        docker compose --profile "${PROFILE}" up -d ${BUILD_FLAG}
    fi

    echo ""
    echo "Waiting for services..."

    # Wait for services based on profile
    case "${PROFILE}" in
        validator)
            wait_for_service "Validator" "http://localhost:8080/health" 60
            ;;
        miner)
            wait_for_service "Validator" "http://localhost:8080/health" 60
            wait_for_port "Miner" "localhost" 8092 60
            ;;
        monitoring|all)
            wait_for_service "Validator" "http://localhost:8080/health" 60
            wait_for_port "Miner" "localhost" 8092 60
            wait_for_service "Prometheus" "http://localhost:9099/-/healthy"
            wait_for_service "Grafana" "http://localhost:3000/api/health"
            ;;
    esac
fi

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
