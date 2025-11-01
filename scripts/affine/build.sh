#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVICE_DIR="$PROJECT_ROOT/services/affine"

REGISTRY="${REGISTRY:-ghcr.io/one-covenant}"
TAG="${TAG:-latest}"
PLATFORM="${PLATFORM:-linux/amd64}"

IMAGE_NAME="${REGISTRY}/basilica-affine:${TAG}"

echo "Building Affine Evaluation Service Docker image"
echo "Image: $IMAGE_NAME"
echo "Platform: $PLATFORM"
echo "Service directory: $SERVICE_DIR"
echo "----------------------------------------"

if [ ! -d "$SERVICE_DIR" ]; then
  echo "Error: Service directory not found: $SERVICE_DIR"
  exit 1
fi

if [ ! -f "$SERVICE_DIR/Dockerfile" ]; then
  echo "Error: Dockerfile not found: $SERVICE_DIR/Dockerfile"
  exit 1
fi

cd "$SERVICE_DIR"

echo ""
echo "Building image..."
docker build \
  --platform "$PLATFORM" \
  -f Dockerfile \
  -t "$IMAGE_NAME" \
  .

echo ""
echo "Build completed successfully!"
echo "Image: $IMAGE_NAME"
echo ""
echo "To run locally:"
echo "  docker run -p 8000:8000 -e CHUTES_API_KEY=your-key $IMAGE_NAME"
echo ""
echo "To test:"
echo "  curl http://localhost:8000/health"
echo ""
echo "To push to registry:"
echo "  docker push $IMAGE_NAME"
