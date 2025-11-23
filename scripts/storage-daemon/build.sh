#!/usr/bin/env bash
# Build the storage daemon Docker image

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Defaults
IMAGE_NAME="${IMAGE_NAME:-basilica/storage-daemon}"
TAG="${TAG:-latest}"
BUILD_MODE="${BUILD_MODE:-release}"

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --image-name)
      IMAGE_NAME="$2"
      shift 2
      ;;
    --image-tag)
      TAG="$2"
      shift 2
      ;;
    --build-mode)
      BUILD_MODE="$2"
      shift 2
      ;;
    --no-extract)
      # Ignored for compatibility with other build scripts
      shift
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "Building storage daemon image: ${IMAGE_NAME}:${TAG}"
echo "Build mode: ${BUILD_MODE}"

cd "$REPO_ROOT"

docker build \
    --build-arg BUILD_MODE="$BUILD_MODE" \
    -f scripts/storage-daemon/Dockerfile \
    -t "${IMAGE_NAME}:${TAG}" \
    .

echo "✓ Built ${IMAGE_NAME}:${TAG}"
