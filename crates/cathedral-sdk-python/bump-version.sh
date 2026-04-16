#!/bin/bash
set -e

if [ -z "$1" ]; then
  echo "Usage: ./bump-version.sh <version>"
  echo "Example: ./bump-version.sh 0.2.0"
  exit 1
fi

NEW_VERSION="$1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Bumping Python SDK version to $NEW_VERSION..."

# Update pyproject.toml
sed -i "s/^version = \".*\"/version = \"$NEW_VERSION\"/" "$SCRIPT_DIR/pyproject.toml"
echo "✓ Updated pyproject.toml"

# Update Cargo.toml
sed -i "s/^version = \".*\"/version = \"$NEW_VERSION\"/" "$SCRIPT_DIR/Cargo.toml"
echo "✓ Updated Cargo.toml"

echo ""
echo "Version bumped to $NEW_VERSION"
echo ""
echo "Next steps:"
echo "1. Update CHANGELOG.md with release notes for v$NEW_VERSION"
echo "2. Review the changes: git diff"
echo "3. Commit: git commit -am \"Bump Python SDK to v$NEW_VERSION\""
echo "4. Create tag: git tag basilica-sdk-python-v$NEW_VERSION"
echo "5. Push: git push origin main && git push origin basilica-sdk-python-v$NEW_VERSION"
