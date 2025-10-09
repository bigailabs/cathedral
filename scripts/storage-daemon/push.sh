#!/usr/bin/env bash
# Push the storage daemon Docker image to registry

set -euo pipefail

# Defaults
SOURCE_IMAGE="${SOURCE_IMAGE:-basilica/storage-daemon}"
TARGET_IMAGE="${TARGET_IMAGE:-basilica/storage-daemon}"
TAG="${TAG:-latest}"

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --source-image)
      SOURCE_IMAGE="$2"
      shift 2
      ;;
    --target-image)
      TARGET_IMAGE="$2"
      shift 2
      ;;
    --tag)
      TAG="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

SOURCE="${SOURCE_IMAGE}:${TAG}"
TARGET="${TARGET_IMAGE}:${TAG}"

echo "Pushing storage daemon image: ${SOURCE} -> ${TARGET}"

# Tag if source != target
if [[ "$SOURCE" != "$TARGET" ]]; then
  echo "Tagging ${SOURCE} as ${TARGET}"
  docker tag "$SOURCE" "$TARGET"
fi

docker push "$TARGET"

echo "✓ Pushed ${TARGET}"
