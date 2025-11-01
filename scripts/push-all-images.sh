#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

REGISTRY="${REGISTRY:-ghcr.io/one-covenant}"
TAG="${TAG:-latest}"

echo "Pushing Basilica Docker images to registry"
echo "Registry: $REGISTRY"
echo "Tag: $TAG"
echo "----------------------------------------"

# Check if logged in
if ! docker info 2>/dev/null | grep -q "Username"; then
  echo "⚠️  Not logged in to Docker registry"
  echo ""
  echo "Please login first:"
  echo "  echo \$GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin"
  echo ""
  exit 1
fi

# Components to push
COMPONENTS=(
  "operator"
  "api"
  "miner"
  "validator"
  "billing"
  "payments"
  "cli"
  "storage-daemon"
)

FAILED=()
SUCCEEDED=()

for component in "${COMPONENTS[@]}"; do
  image_name="${REGISTRY}/basilica-${component}:${TAG}"

  # Check if image exists locally
  if ! docker image inspect "$image_name" >/dev/null 2>&1; then
    echo "⚠️  Skipping $component - image not found locally"
    echo "    Run ./scripts/build-all-images.sh first"
    continue
  fi

  echo ""
  echo "📤 Pushing $component..."
  echo "   Image: $image_name"

  if docker push "$image_name"; then
    echo "✅ Successfully pushed $component"
    SUCCEEDED+=("$component")
  else
    echo "❌ Failed to push $component"
    FAILED+=("$component")
  fi
done

echo ""
echo "========================================"
echo "Push Summary"
echo "========================================"
echo "Succeeded: ${#SUCCEEDED[@]}"
for comp in "${SUCCEEDED[@]}"; do
  echo "  ✅ $comp"
  echo "     ${REGISTRY}/basilica-${comp}:${TAG}"
done

if [ ${#FAILED[@]} -gt 0 ]; then
  echo ""
  echo "Failed: ${#FAILED[@]}"
  for comp in "${FAILED[@]}"; do
    echo "  ❌ $comp"
  done
  exit 1
else
  echo ""
  echo "🎉 All images pushed successfully!"
  echo ""
  echo "View packages at:"
  echo "  https://github.com/orgs/one-covenant/packages"
fi
