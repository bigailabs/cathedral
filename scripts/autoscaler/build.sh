#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

IMAGE_NAME="basilica/basilica-autoscaler"
IMAGE_TAG="latest"
RELEASE_MODE=true

while [[ $# -gt 0 ]]; do
    case $1 in
        --image-name)
            IMAGE_NAME="$2"
            shift 2
            ;;
        --image-tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --debug)
            RELEASE_MODE=false
            shift
            ;;
        --help)
            echo "Usage: $0 [--image-name NAME] [--image-tag TAG] [--debug]"
            echo ""
            echo "Options:"
            echo "  --image-name NAME     Docker image name (default: basilica/basilica-autoscaler)"
            echo "  --image-tag TAG       Docker image tag (default: latest)"
            echo "  --debug               Build in debug mode"
            echo "  --help                Show this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

cd "$PROJECT_ROOT"

BUILD_ARGS=""
if [[ "$RELEASE_MODE" == "true" ]]; then
    BUILD_ARGS="--build-arg BUILD_MODE=release"
else
    BUILD_ARGS="--build-arg BUILD_MODE=debug"
fi

echo "Building Docker image: ${IMAGE_NAME}:${IMAGE_TAG}"
docker build \
    --platform linux/amd64 \
    $BUILD_ARGS \
    -f scripts/autoscaler/Dockerfile \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    .
echo "Docker image built successfully"
