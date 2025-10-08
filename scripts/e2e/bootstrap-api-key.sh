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
#   - cargo (to build gen_api_key binary)
#   - kubectl with access to cluster
#   - Postgres deployed in cluster

USER="e2e-test"
KEY_NAME="smoke-test"
SCOPES="rentals:* jobs:*"
NAMESPACE="${POSTGRES_NAMESPACE:-basilica-system}"
POSTGRES_SVC="${POSTGRES_SERVICE:-basilica-postgres}"
POSTGRES_USER="${POSTGRES_USER:-basilica}"
POSTGRES_DB="${POSTGRES_DB:-basilica}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-devpassword}"

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
      echo "  POSTGRES_NAMESPACE  Namespace for Postgres (default: basilica-system)"
      echo "  POSTGRES_SERVICE    Postgres service name (default: basilica-postgres)"
      echo "  POSTGRES_USER       Postgres username (default: basilica)"
      echo "  POSTGRES_DB         Postgres database (default: basilica)"
      echo "  POSTGRES_PASSWORD   Postgres password (default: devpassword)"
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
if [ ! -f target/release/gen_api_key ]; then
  echo "[bootstrap] Building gen_api_key binary..." >&2
  cargo build -p basilica-api --bin gen_api_key --release --quiet
fi

# Run gen-api-key with scopes
# The binary outputs:
#   API Key Details:
#   Token: <token>
#   Key ID: <uuid>
#
#   INSERT INTO api_keys ...;
echo "[bootstrap] Running gen_api_key..." >&2

# Convert scopes to --scopes arguments
SCOPE_ARGS=""
for scope in $SCOPES; do
  SCOPE_ARGS="$SCOPE_ARGS --scopes $scope"
done

OUTPUT=$(cargo run -p basilica-api --bin gen_api_key --release --quiet -- \
  --user "$USER" \
  --name "$KEY_NAME" \
  $SCOPE_ARGS 2>&1)

# Extract token (line starting with "Token:")
TOKEN=$(echo "$OUTPUT" | grep "^Token:" | awk '{print $2}' || echo "")

if [ -z "$TOKEN" ]; then
  echo "[bootstrap] ERROR: Failed to extract token from gen_api_key output" >&2
  echo "[bootstrap] Output was:" >&2
  echo "$OUTPUT" >&2
  exit 1
fi

# Extract SQL (all lines from INSERT to semicolon)
SQL=$(echo "$OUTPUT" | sed -n '/^INSERT INTO/,/;$/p')

if [ -z "$SQL" ]; then
  echo "[bootstrap] ERROR: Failed to extract SQL from gen_api_key output" >&2
  echo "[bootstrap] Output was:" >&2
  echo "$OUTPUT" >&2
  exit 1
fi

echo "[bootstrap] Token: $TOKEN" >&2
echo "[bootstrap] SQL:" >&2
echo "$SQL" >&2

# Insert into database
echo "[bootstrap] Connecting to Postgres..." >&2

# Check if we can reach Postgres service
if ! kubectl get svc -n "$NAMESPACE" "$POSTGRES_SVC" &>/dev/null; then
  echo "[bootstrap] WARNING: Postgres service $POSTGRES_SVC not found in namespace $NAMESPACE" >&2
  echo "[bootstrap] Skipping database insertion. Run SQL manually:" >&2
  echo "$SQL" >&2
else
  # Port-forward Postgres
  echo "[bootstrap] Port-forwarding Postgres on 5432..." >&2
  kubectl port-forward -n "$NAMESPACE" "svc/$POSTGRES_SVC" 5432:5432 >/dev/null 2>&1 &
  PF_PID=$!

  # Wait for port-forward to be ready
  sleep 3

  # Insert SQL
  echo "[bootstrap] Inserting API key into database..." >&2
  export PGPASSWORD="$POSTGRES_PASSWORD"

  if echo "$SQL" | psql -h localhost -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" 2>&1 | grep -q "ERROR"; then
    echo "[bootstrap] WARNING: SQL insertion may have failed. Check manually." >&2
    # Don't exit on error - key might already exist
  else
    echo "[bootstrap] ✅ API key inserted successfully" >&2
  fi

  # Kill port-forward
  kill $PF_PID 2>/dev/null || true
  wait $PF_PID 2>/dev/null || true
fi

# Output token for easy export
echo "[bootstrap] Use this token in tests:" >&2
echo "export BASILICA_API_TOKEN=$TOKEN"
echo "" >&2
echo "[bootstrap] Or source directly:" >&2
echo "  source <(./scripts/e2e/bootstrap-api-key.sh | grep '^export')" >&2

# Also output just the token for easy capture
echo "Token for programmatic use: $TOKEN" >&2
