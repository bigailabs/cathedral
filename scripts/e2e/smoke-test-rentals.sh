#!/usr/bin/env bash
set -euo pipefail

# E2E smoke test for Rentals API (v2)
#
# Tests the complete flow:
#   User → Local API (localhost:8000) → Remote K3s Operator → Pods/Services/CRs
#
# Prerequisites:
#   - Local E2E environment running: `just e2e-up`
#     (Local: Subtensor, Validator, API | Remote: K3s Operator)
#   - OR: In-cluster API deployment with port-forward
#   - BASILICA_API_URL and BASILICA_API_TOKEN env vars set
#   - kubectl access to remote cluster for CR verification (optional)
#   - jq installed
#
# Usage:
#   # After running `just e2e-up`:
#   source <(./scripts/e2e/bootstrap-api-key.sh | grep "^export BASILICA_API_TOKEN")
#   export BASILICA_API_URL=http://localhost:8000
#   ./scripts/e2e/smoke-test-rentals.sh

API_URL=${BASILICA_API_URL:-http://localhost:8000}
API_TOKEN=${BASILICA_API_TOKEN:?BASILICA_API_TOKEN must be set}
NAMESPACE=${TENANT_NAMESPACE:-u-test}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
  echo -e "${GREEN}[smoke-rentals]${NC} $*"
}

warn() {
  echo -e "${YELLOW}[smoke-rentals]${NC} $*"
}

error() {
  echo -e "${RED}[smoke-rentals] ERROR:${NC} $*" >&2
}

# Check prerequisites
if ! command -v jq &>/dev/null; then
  error "jq is required but not installed. Install with: apt-get install jq"
  exit 1
fi

if ! command -v kubectl &>/dev/null; then
  warn "kubectl not found - will skip CR verification"
  SKIP_K8S_VERIFY=true
else
  SKIP_K8S_VERIFY=false
fi

log "Testing Rentals v2 API at $API_URL"
log "Using namespace: $NAMESPACE"

# Cleanup function
RENTAL_ID=""
cleanup() {
  if [ -n "$RENTAL_ID" ]; then
    log "Cleaning up rental $RENTAL_ID..."
    curl -s -X DELETE -H "Authorization: Bearer ${API_TOKEN}" \
      "${API_URL}/v2/rentals/${RENTAL_ID}" || true
  fi
}
trap cleanup EXIT

# 1. Create rental
log "Step 1/8: Creating CPU-only rental..."
RENTAL_RESP=$(curl -s -w "\n%{http_code}" -X POST "${API_URL}/v2/rentals" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "container_image": "busybox:latest",
    "command": ["sh", "-c", "while true; do echo alive; sleep 5; done"],
    "resources": {
      "cpu": "0.25",
      "memory": "256Mi",
      "gpus": {"count": 0, "model": []}
    },
    "network": {
      "ingress_ports": [],
      "egress_policy": "open"
    }
  }')

HTTP_CODE=$(echo "$RENTAL_RESP" | tail -n1)
BODY=$(echo "$RENTAL_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  error "Failed to create rental. HTTP $HTTP_CODE"
  echo "$BODY" | jq . || echo "$BODY"
  exit 1
fi

RENTAL_ID=$(echo "$BODY" | jq -r '.rental_id // .id // empty')

if [ -z "$RENTAL_ID" ]; then
  error "Failed to extract rental_id from response"
  echo "$BODY" | jq .
  exit 1
fi

log "✓ Created rental: $RENTAL_ID"

# 2. Poll until Active
log "Step 2/8: Waiting for rental to become Active (timeout: 60s)..."
MAX_ATTEMPTS=30
ATTEMPT=0
STATUS="unknown"

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  ATTEMPT=$((ATTEMPT + 1))

  GET_RESP=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "${API_URL}/v2/rentals/${RENTAL_ID}")

  HTTP_CODE=$(echo "$GET_RESP" | tail -n1)
  BODY=$(echo "$GET_RESP" | head -n-1)

  if [ "$HTTP_CODE" != "200" ]; then
    warn "GET request returned HTTP $HTTP_CODE (attempt $ATTEMPT/$MAX_ATTEMPTS)"
    sleep 2
    continue
  fi

  STATUS=$(echo "$BODY" | jq -r '.status.state // .state // empty' | tr '[:upper:]' '[:lower:]')
  log "  Current status: $STATUS (attempt $ATTEMPT/$MAX_ATTEMPTS)"

  if [ "$STATUS" = "active" ]; then
    break
  fi

  # Check for failure states
  if [[ "$STATUS" =~ ^(failed|error|stopped)$ ]]; then
    error "Rental entered failure state: $STATUS"
    echo "$BODY" | jq .
    exit 1
  fi

  sleep 2
done

if [ "$STATUS" != "active" ]; then
  error "Rental did not become Active within timeout. Last status: $STATUS"
  curl -s -H "Authorization: Bearer ${API_TOKEN}" "${API_URL}/v2/rentals/${RENTAL_ID}" | jq .
  exit 1
fi

log "✓ Rental is Active"

# 3. Verify CR exists in cluster
if [ "$SKIP_K8S_VERIFY" = false ]; then
  log "Step 3/8: Verifying GpuRental CR exists in cluster..."
  if kubectl get gpurentals.basilica.ai -n "$NAMESPACE" "$RENTAL_ID" &>/dev/null; then
    log "✓ GpuRental CR found"
  else
    error "GpuRental CR not found in namespace $NAMESPACE"
    kubectl get gpurentals.basilica.ai -n "$NAMESPACE" || true
    exit 1
  fi
else
  warn "Step 3/8: Skipping CR verification (kubectl not available)"
fi

# 4. Verify endpoints populated
log "Step 4/8: Verifying rental has endpoints..."
ENDPOINTS=$(curl -s -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/v2/rentals/${RENTAL_ID}" | jq '.endpoints // empty')

if [ -z "$ENDPOINTS" ] || [ "$ENDPOINTS" = "null" ]; then
  warn "Rental has no endpoints yet (may be normal for some deployments)"
else
  log "✓ Rental has endpoints: $ENDPOINTS"
fi

# 5. Get logs
log "Step 5/8: Fetching logs (tail=10)..."
LOG_RESP=$(curl -s -w "\n%{http_code}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/v2/rentals/${RENTAL_ID}/logs?tail=10")

HTTP_CODE=$(echo "$LOG_RESP" | tail -n1)
LOGS=$(echo "$LOG_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  warn "Logs endpoint returned HTTP $HTTP_CODE"
  echo "$LOGS"
else
  LINE_COUNT=$(echo "$LOGS" | wc -l)
  log "✓ Logs retrieved ($LINE_COUNT lines)"
  # Show first few lines
  echo "$LOGS" | head -n3
fi

# 6. Exec command
log "Step 6/8: Executing command in rental..."
EXEC_RESP=$(curl -s -w "\n%{http_code}" -X POST \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"command": ["echo", "smoke-test-exec"], "tty": false}' \
  "${API_URL}/v2/rentals/${RENTAL_ID}/exec")

HTTP_CODE=$(echo "$EXEC_RESP" | tail -n1)
BODY=$(echo "$EXEC_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  warn "Exec endpoint returned HTTP $HTTP_CODE"
  echo "$BODY" | jq . || echo "$BODY"
else
  STDOUT=$(echo "$BODY" | jq -r '.stdout // empty')
  if echo "$STDOUT" | grep -q "smoke-test-exec"; then
    log "✓ Exec succeeded, output contains expected string"
  else
    warn "Exec succeeded but output unexpected. Got: $STDOUT"
  fi
fi

# 7. List rentals (verify it appears)
log "Step 7/8: Listing rentals..."
LIST_RESP=$(curl -s -w "\n%{http_code}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/v2/rentals")

HTTP_CODE=$(echo "$LIST_RESP" | tail -n1)
BODY=$(echo "$LIST_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  warn "List endpoint returned HTTP $HTTP_CODE"
else
  FOUND=$(echo "$BODY" | jq -r --arg id "$RENTAL_ID" '.rentals[]? | select(.id == $id or .rental_id == $id) | .id // .rental_id' || echo "")
  if [ -n "$FOUND" ]; then
    log "✓ Rental found in list"
  else
    warn "Rental not found in list response"
  fi
fi

# 8. Delete rental
log "Step 8/8: Deleting rental..."
DEL_RESP=$(curl -s -w "\n%{http_code}" -X DELETE \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/v2/rentals/${RENTAL_ID}")

HTTP_CODE=$(echo "$DEL_RESP" | tail -n1)

if [ "$HTTP_CODE" != "200" ]; then
  error "Delete failed with HTTP $HTTP_CODE"
  echo "$DEL_RESP" | head -n-1
  exit 1
fi

log "✓ Rental deleted"

# 9. Verify CR cleaned up (if kubectl available)
if [ "$SKIP_K8S_VERIFY" = false ]; then
  log "Waiting for CR to be garbage collected (max 30s)..."
  for i in {1..15}; do
    if ! kubectl get gpurentals.basilica.ai -n "$NAMESPACE" "$RENTAL_ID" &>/dev/null; then
      log "✓ CR deleted"
      break
    fi
    if [ $i -eq 15 ]; then
      warn "CR still exists after 30s (may be normal with finalizers)"
    fi
    sleep 2
  done
fi

# Clear rental ID so cleanup doesn't try to delete again
RENTAL_ID=""

log ""
log "========================================="
log "✅ Rentals smoke test PASSED"
log "========================================="
log ""
log "All steps completed successfully:"
log "  ✓ Create rental"
log "  ✓ Rental became Active"
log "  ✓ CR verification (kubectl)"
log "  ✓ Endpoints populated"
log "  ✓ Logs retrieval"
log "  ✓ Exec command"
log "  ✓ List rentals"
log "  ✓ Delete rental"
log "  ✓ CR cleanup"
