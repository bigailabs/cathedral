#!/usr/bin/env bash
set -euo pipefail

# Bootstrap an API key for E2E testing
# Generates key via gen-api-key and inserts into Postgres
#
# Usage:
#   ./scripts/e2e/bootstrap-api-key.sh [--user USER] [--name NAME] [--scopes SCOPES]
#
# Outputs:
#   - Prints token and SQL to stdout
#   - Exports BASILICA_API_TOKEN for use in scripts
#
# Prerequisites:
#   - cargo (to build gen-api-key binary)
#   - kubectl with access to cluster
#   - Postgres deployed in cluster

USER="test"
KEY_NAME="smoke-test"
SCOPES="rentals:* jobs:*"

# Local API Postgres (Docker Compose)
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-api}"
POSTGRES_DB="${POSTGRES_DB:-basilica_api}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-api_dev_password}"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      USER="$2"
      shift 2
      ;;
    --name)
      KEY_NAME="$2"
      shift 2
      ;;
    --scopes)
      SCOPES="$2"
      shift 2
      ;;
    --help)
      echo "Usage: $0 [--user USER] [--name NAME] [--scopes SCOPES]"
      echo ""
      echo "Options:"
      echo "  --user USER      User ID for the API key (default: e2e-test)"
      echo "  --name NAME      Name for the API key (default: smoke-test)"
      echo "  --scopes SCOPES  Space-separated scopes (default: 'rentals:* jobs:*')"
      echo ""
      echo "Environment Variables:"
      echo "  POSTGRES_HOST       Postgres host (default: localhost)"
      echo "  POSTGRES_PORT       Postgres port (default: 5432)"
      echo "  POSTGRES_USER       Postgres username (default: api)"
      echo "  POSTGRES_DB         Postgres database (default: basilica_api)"
      echo "  POSTGRES_PASSWORD   Postgres password (default: api_dev_password)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

echo "[bootstrap] Generating API key for user='$USER' name='$KEY_NAME' scopes='$SCOPES'" >&2

# Build gen-api-key if not already built
if [ ! -f target/release/gen-api-key ]; then
  echo "[bootstrap] Building gen-api-key binary..." >&2
  cargo build -p basilica-api --bin gen-api-key --release
else
  echo "[bootstrap] Using existing gen-api-key binary" >&2
fi

# Run gen-api-key with scopes
# The binary outputs:
#   API Key Details:
#   Token: <token>
#   Key ID: <uuid>
#
#   INSERT INTO api_keys ...;
echo "[bootstrap] Running gen-api-key..." >&2

# Convert scopes to --scopes arguments
SCOPE_ARGS=""
for scope in $SCOPES; do
  SCOPE_ARGS="$SCOPE_ARGS --scopes $scope"
done

OUTPUT=$(timeout 30 ./target/release/gen-api-key \
  --user "$USER" \
  --name "$KEY_NAME" \
  $SCOPE_ARGS 2>&1 || {
    echo "[bootstrap] ERROR: gen-api-key timed out or failed" >&2
    exit 1
  })

# Extract token (line containing "Token (Authorization): Bearer")
TOKEN=$(echo "$OUTPUT" | grep "Token (Authorization):" | sed 's/.*Bearer //' | tr -d ' ' || echo "")

if [ -z "$TOKEN" ]; then
  echo "[bootstrap] ERROR: Failed to extract token from gen-api-key output" >&2
  echo "[bootstrap] Output was:" >&2
  echo "$OUTPUT" >&2
  exit 1
fi

# Extract SQL (line starting with INSERT INTO and ending with semicolon)
SQL=$(echo "$OUTPUT" | grep "^INSERT INTO" || echo "")

if [ -z "$SQL" ]; then
  echo "[bootstrap] ERROR: Failed to extract SQL from gen-api-key output" >&2
  echo "[bootstrap] Output was:" >&2
  echo "$OUTPUT" >&2
  exit 1
fi

echo "[bootstrap] Token: $TOKEN" >&2
echo "[bootstrap] SQL:" >&2
echo "$SQL" >&2

# Insert into database
echo "[bootstrap] Connecting to local Postgres..." >&2

# Find the postgres container from the API docker compose
POSTGRES_CONTAINER=$(docker ps --filter "name=postgres" --format "{{.Names}}" | head -1)

if [ -z "$POSTGRES_CONTAINER" ]; then
  echo "[bootstrap] WARNING: Postgres container not found." >&2
  echo "[bootstrap] Make sure local API is running: just local-api-up" >&2
  echo "[bootstrap] Run SQL manually:" >&2
  echo "$SQL" >&2
else
  # Delete existing key with same user_id and name (if exists)
  echo "[bootstrap] Using container: $POSTGRES_CONTAINER" >&2
  echo "[bootstrap] Deleting existing key (if any)..." >&2
  docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
    "DELETE FROM api_keys WHERE user_id = '$USER' AND name = '$KEY_NAME';" > /dev/null 2>&1 || true

  # Insert new key
  echo "[bootstrap] Inserting API key into database..." >&2
  if docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$SQL" 2>&1 | grep -q "ERROR"; then
    echo "[bootstrap] ERROR: SQL insertion failed" >&2
    exit 1
  else
    echo "[bootstrap] ✅ API key inserted successfully" >&2
  fi
fi

# Output token for easy export
echo "[bootstrap] Use this token in tests:" >&2
echo "export BASILICA_API_TOKEN=$TOKEN"
echo "" >&2
echo "[bootstrap] Or source directly:" >&2
echo "  source <(./scripts/e2e/bootstrap-api-key.sh | grep '^export')" >&2

# Also output just the token for easy capture
echo "Token for programmatic use: $TOKEN" >&2
