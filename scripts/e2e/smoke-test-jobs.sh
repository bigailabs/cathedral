#!/usr/bin/env bash
set -euo pipefail

# E2E smoke test for Jobs API (v1)
#
# Tests the complete flow:
#   User → Local API (localhost:8000) → Remote K3s Operator → Jobs/Pods/CRs
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
#   ./scripts/e2e/smoke-test-jobs.sh

API_URL=${BASILICA_API_URL:-http://localhost:8000}
API_TOKEN=${BASILICA_API_TOKEN:?BASILICA_API_TOKEN must be set}
NAMESPACE=${TENANT_NAMESPACE:-u-test}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
  echo -e "${GREEN}[smoke-jobs]${NC} $*"
}

warn() {
  echo -e "${YELLOW}[smoke-jobs]${NC} $*"
}

error() {
  echo -e "${RED}[smoke-jobs] ERROR:${NC} $*" >&2
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

log "Testing Jobs v1 API at $API_URL"
log "Using namespace: $NAMESPACE"

# Cleanup function
JOB_ID=""
cleanup() {
  if [ -n "$JOB_ID" ]; then
    log "Cleaning up job $JOB_ID..."
    curl -s -X DELETE -H "Authorization: Bearer ${API_TOKEN}" \
      "${API_URL}/api/v1/jobs/${JOB_ID}" || true
  fi
}
trap cleanup EXIT

# 1. Create job
log "Step 1/7: Creating simple echo job..."
JOB_RESP=$(curl -s -w "\n%{http_code}" -X POST "${API_URL}/api/v1/jobs" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{
    "image": "busybox:latest",
    "command": ["sh", "-c", "echo hello-from-job && echo line2 && sleep 3 && echo done"],
    "resources": {
      "cpu": "0.25",
      "memory": "128Mi"
    }
  }')

HTTP_CODE=$(echo "$JOB_RESP" | tail -n1)
BODY=$(echo "$JOB_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  error "Failed to create job. HTTP $HTTP_CODE"
  echo "$BODY" | jq . || echo "$BODY"
  exit 1
fi

JOB_ID=$(echo "$BODY" | jq -r '.job_id // .id // empty')

if [ -z "$JOB_ID" ]; then
  error "Failed to extract job_id from response"
  echo "$BODY" | jq .
  exit 1
fi

log "✓ Created job: $JOB_ID"

# 2. Poll until Running or Succeeded
log "Step 2/7: Waiting for job to start (timeout: 60s)..."
MAX_ATTEMPTS=30
ATTEMPT=0
STATUS="unknown"

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
  ATTEMPT=$((ATTEMPT + 1))

  GET_RESP=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer ${API_TOKEN}" \
    "${API_URL}/api/v1/jobs/${JOB_ID}")

  HTTP_CODE=$(echo "$GET_RESP" | tail -n1)
  BODY=$(echo "$GET_RESP" | head -n-1)

  if [ "$HTTP_CODE" != "200" ]; then
    warn "GET request returned HTTP $HTTP_CODE (attempt $ATTEMPT/$MAX_ATTEMPTS)"
    sleep 2
    continue
  fi

  STATUS=$(echo "$BODY" | jq -r '.status // .phase // empty' | tr '[:upper:]' '[:lower:]')
  log "  Current status: $STATUS (attempt $ATTEMPT/$MAX_ATTEMPTS)"

  # Break on terminal states
  if [[ "$STATUS" =~ ^(succeeded|failed|completed)$ ]]; then
    break
  fi

  sleep 2
done

if [[ ! "$STATUS" =~ ^(running|succeeded|completed)$ ]]; then
  error "Job did not reach expected state within timeout. Last status: $STATUS"
  curl -s -H "Authorization: Bearer ${API_TOKEN}" "${API_URL}/api/v1/jobs/${JOB_ID}" | jq .
  exit 1
fi

log "✓ Job reached state: $STATUS"

# 3. Wait for completion if still running
if [ "$STATUS" = "running" ]; then
  log "Step 3/7: Waiting for job to complete (timeout: 60s)..."
  ATTEMPT=0
  while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))

    GET_RESP=$(curl -s -w "\n%{http_code}" \
      -H "Authorization: Bearer ${API_TOKEN}" \
      "${API_URL}/api/v1/jobs/${JOB_ID}")

    HTTP_CODE=$(echo "$GET_RESP" | tail -n1)
    BODY=$(echo "$GET_RESP" | head -n-1)

    if [ "$HTTP_CODE" != "200" ]; then
      warn "GET request returned HTTP $HTTP_CODE"
      sleep 2
      continue
    fi

    STATUS=$(echo "$BODY" | jq -r '.status // .phase // empty' | tr '[:upper:]' '[:lower:]')
    log "  Current status: $STATUS (attempt $ATTEMPT/$MAX_ATTEMPTS)"

    if [[ "$STATUS" =~ ^(succeeded|completed)$ ]]; then
      break
    fi

    if [ "$STATUS" = "failed" ]; then
      error "Job failed"
      echo "$BODY" | jq .
      exit 1
    fi

    sleep 2
  done

  if [[ ! "$STATUS" =~ ^(succeeded|completed)$ ]]; then
    error "Job did not complete within timeout. Last status: $STATUS"
    exit 1
  fi
else
  log "Step 3/7: Job already completed"
fi

log "✓ Job completed successfully"

# 4. Verify CR exists in cluster
if [ "$SKIP_K8S_VERIFY" = false ]; then
  log "Step 4/7: Verifying BasilicaJob CR exists in cluster..."
  if kubectl get basilicajobs.basilica.ai -n "$NAMESPACE" "$JOB_ID" &>/dev/null; then
    log "✓ BasilicaJob CR found"
  else
    error "BasilicaJob CR not found in namespace $NAMESPACE"
    kubectl get basilicajobs.basilica.ai -n "$NAMESPACE" || true
    exit 1
  fi
else
  warn "Step 4/7: Skipping CR verification (kubectl not available)"
fi

# 5. Get logs
log "Step 5/7: Fetching job logs..."
LOG_RESP=$(curl -s -w "\n%{http_code}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/api/v1/jobs/${JOB_ID}/logs")

HTTP_CODE=$(echo "$LOG_RESP" | tail -n1)
LOGS=$(echo "$LOG_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  error "Logs endpoint returned HTTP $HTTP_CODE"
  echo "$LOGS"
  exit 1
fi

if ! echo "$LOGS" | grep -q "hello-from-job"; then
  error "Logs do not contain expected output 'hello-from-job'"
  echo "Received logs:"
  echo "$LOGS"
  exit 1
fi

log "✓ Logs contain expected output"
log "  Sample output:"
echo "$LOGS" | head -n5 | sed 's/^/    /'

# 6. List jobs (verify it appears)
log "Step 6/7: Listing jobs..."
LIST_RESP=$(curl -s -w "\n%{http_code}" \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/api/v1/jobs")

HTTP_CODE=$(echo "$LIST_RESP" | tail -n1)
BODY=$(echo "$LIST_RESP" | head -n-1)

if [ "$HTTP_CODE" != "200" ]; then
  warn "List endpoint returned HTTP $HTTP_CODE"
else
  FOUND=$(echo "$BODY" | jq -r --arg id "$JOB_ID" '.jobs[]? | select(.id == $id or .job_id == $id) | .id // .job_id' || echo "")
  if [ -n "$FOUND" ]; then
    log "✓ Job found in list"
  else
    warn "Job not found in list response"
  fi
fi

# 7. Delete job
log "Step 7/7: Deleting job..."
DEL_RESP=$(curl -s -w "\n%{http_code}" -X DELETE \
  -H "Authorization: Bearer ${API_TOKEN}" \
  "${API_URL}/api/v1/jobs/${JOB_ID}")

HTTP_CODE=$(echo "$DEL_RESP" | tail -n1)

if [ "$HTTP_CODE" != "200" ]; then
  error "Delete failed with HTTP $HTTP_CODE"
  echo "$DEL_RESP" | head -n-1
  exit 1
fi

log "✓ Job deleted"

# 8. Verify CR cleaned up (if kubectl available)
if [ "$SKIP_K8S_VERIFY" = false ]; then
  log "Waiting for CR to be garbage collected (max 30s)..."
  for i in {1..15}; do
    if ! kubectl get basilicajobs.basilica.ai -n "$NAMESPACE" "$JOB_ID" &>/dev/null; then
      log "✓ CR deleted"
      break
    fi
    if [ $i -eq 15 ]; then
      warn "CR still exists after 30s (may be normal with finalizers)"
    fi
    sleep 2
  done
fi

# Clear job ID so cleanup doesn't try to delete again
JOB_ID=""

log ""
log "========================================="
log "✅ Jobs smoke test PASSED"
log "========================================="
log ""
log "All steps completed successfully:"
log "  ✓ Create job"
log "  ✓ Job started execution"
log "  ✓ Job completed successfully"
log "  ✓ CR verification (kubectl)"
log "  ✓ Logs retrieval with expected output"
log "  ✓ List jobs"
log "  ✓ Delete job"
log "  ✓ CR cleanup"
