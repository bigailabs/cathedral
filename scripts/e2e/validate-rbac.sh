#!/usr/bin/env bash
set -euo pipefail

# RBAC validation for Basilica E2E
# Verifies ServiceAccounts have necessary permissions
#
# Prerequisites:
#   - kubectl with access to cluster
#   - Basilica ServiceAccounts created in cluster
#
# Usage:
#   ./scripts/e2e/validate-rbac.sh [--tenant-ns NAMESPACE]

TENANT_NS="${TENANT_NAMESPACE:-u-test}"
OPERATOR_SA="system:serviceaccount:basilica-system:basilica-operator"
API_SA="system:serviceaccount:basilica-system:basilica-api"
VALIDATOR_SA="system:serviceaccount:basilica-validators:basilica-validator"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tenant-ns)
      TENANT_NS="$2"
      shift 2
      ;;
    --help)
      echo "Usage: $0 [--tenant-ns NAMESPACE]"
      echo ""
      echo "Validates RBAC permissions for Basilica ServiceAccounts"
      echo ""
      echo "Options:"
      echo "  --tenant-ns NAMESPACE  Tenant namespace to test (default: u-test)"
      echo ""
      echo "Environment Variables:"
      echo "  TENANT_NAMESPACE  Same as --tenant-ns flag"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      echo "Use --help for usage information"
      exit 1
      ;;
  esac
done

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
  echo -e "${GREEN}[rbac]${NC} $*"
}

warn() {
  echo -e "${YELLOW}[rbac]${NC} $*"
}

error() {
  echo -e "${RED}[rbac] ERROR:${NC} $*" >&2
}

# Check if kubectl is available
if ! command -v kubectl &>/dev/null; then
  error "kubectl is required but not installed"
  exit 1
fi

# Check if cluster is reachable
if ! kubectl cluster-info &>/dev/null; then
  error "Cannot connect to Kubernetes cluster"
  exit 1
fi

log "Validating RBAC for Basilica components"
log "Tenant namespace: $TENANT_NS"
log ""

FAILURES=0

# Helper to test permission
test_perm() {
  local sa="$1"
  local verb="$2"
  local resource="$3"
  local namespace="${4:-}"
  local description="$5"

  local cmd="kubectl auth can-i $verb $resource --as=\"$sa\""
  if [ -n "$namespace" ]; then
    cmd="$cmd -n \"$namespace\""
  fi

  if eval "$cmd" &>/dev/null; then
    log "✓ $description"
  else
    error "✗ $description"
    FAILURES=$((FAILURES + 1))
  fi
}

# ============================================================================
# OPERATOR RBAC CHECKS
# ============================================================================

log "Validating Operator RBAC ($OPERATOR_SA)..."
log ""

# Core resources in tenant namespaces
test_perm "$OPERATOR_SA" "create" "pods" "$TENANT_NS" "Operator can create pods in tenant namespace"
test_perm "$OPERATOR_SA" "get" "pods" "$TENANT_NS" "Operator can get pods in tenant namespace"
test_perm "$OPERATOR_SA" "list" "pods" "$TENANT_NS" "Operator can list pods in tenant namespace"
test_perm "$OPERATOR_SA" "delete" "pods" "$TENANT_NS" "Operator can delete pods in tenant namespace"
test_perm "$OPERATOR_SA" "patch" "pods" "$TENANT_NS" "Operator can patch pods in tenant namespace"

test_perm "$OPERATOR_SA" "create" "services" "$TENANT_NS" "Operator can create services in tenant namespace"
test_perm "$OPERATOR_SA" "get" "services" "$TENANT_NS" "Operator can get services in tenant namespace"
test_perm "$OPERATOR_SA" "delete" "services" "$TENANT_NS" "Operator can delete services in tenant namespace"

test_perm "$OPERATOR_SA" "create" "networkpolicies" "$TENANT_NS" "Operator can create networkpolicies in tenant namespace"
test_perm "$OPERATOR_SA" "create" "jobs" "$TENANT_NS" "Operator can create batch jobs in tenant namespace"

test_perm "$OPERATOR_SA" "create" "persistentvolumeclaims" "$TENANT_NS" "Operator can create PVCs in tenant namespace"

# Cluster-scoped resources
test_perm "$OPERATOR_SA" "get" "nodes" "" "Operator can get nodes (cluster-scoped)"
test_perm "$OPERATOR_SA" "list" "nodes" "" "Operator can list nodes (cluster-scoped)"
test_perm "$OPERATOR_SA" "patch" "nodes" "" "Operator can patch nodes (for cordoning)"
test_perm "$OPERATOR_SA" "delete" "nodes" "" "Operator can delete nodes (for removal)"

# Eviction API (PDB-aware draining)
test_perm "$OPERATOR_SA" "create" "pods/eviction" "" "Operator can create pod evictions (cluster-scoped)"

# Custom resources
test_perm "$OPERATOR_SA" "get" "gpurentals" "$TENANT_NS" "Operator can get GpuRentals in tenant namespace"
test_perm "$OPERATOR_SA" "list" "gpurentals" "$TENANT_NS" "Operator can list GpuRentals in tenant namespace"
test_perm "$OPERATOR_SA" "watch" "gpurentals" "$TENANT_NS" "Operator can watch GpuRentals in tenant namespace"
test_perm "$OPERATOR_SA" "patch" "gpurentals/status" "$TENANT_NS" "Operator can patch GpuRental status"

test_perm "$OPERATOR_SA" "get" "basilicajobs" "$TENANT_NS" "Operator can get BasilicaJobs in tenant namespace"
test_perm "$OPERATOR_SA" "patch" "basilicajobs/status" "$TENANT_NS" "Operator can patch BasilicaJob status"

test_perm "$OPERATOR_SA" "get" "basilicanodeprofiles" "" "Operator can get BasilicaNodeProfiles (cluster-scoped)"
test_perm "$OPERATOR_SA" "patch" "basilicanodeprofiles/status" "" "Operator can patch BasilicaNodeProfile status"

# Gateway API (if CRDs installed)
if kubectl api-resources | grep -q httproutes; then
  test_perm "$OPERATOR_SA" "create" "httproutes" "$TENANT_NS" "Operator can create HTTPRoutes in tenant namespace"
else
  warn "⊘ HTTPRoutes CRD not installed (Gateway API feature - optional)"
fi

log ""

# ============================================================================
# API RBAC CHECKS
# ============================================================================

log "Validating API RBAC ($API_SA)..."
log ""

# Custom resources
test_perm "$API_SA" "create" "gpurentals" "$TENANT_NS" "API can create GpuRentals in tenant namespace"
test_perm "$API_SA" "get" "gpurentals" "$TENANT_NS" "API can get GpuRentals in tenant namespace"
test_perm "$API_SA" "list" "gpurentals" "$TENANT_NS" "API can list GpuRentals in tenant namespace"
test_perm "$API_SA" "delete" "gpurentals" "$TENANT_NS" "API can delete GpuRentals in tenant namespace"

test_perm "$API_SA" "create" "basilicajobs" "$TENANT_NS" "API can create BasilicaJobs in tenant namespace"
test_perm "$API_SA" "get" "basilicajobs" "$TENANT_NS" "API can get BasilicaJobs in tenant namespace"
test_perm "$API_SA" "delete" "basilicajobs" "$TENANT_NS" "API can delete BasilicaJobs in tenant namespace"

# Pod logs and exec
test_perm "$API_SA" "get" "pods/log" "$TENANT_NS" "API can get pod logs in tenant namespace"

# Note: kubectl auth can-i has issues testing pods/exec subresource
# Verify with: kubectl auth can-i --list --as=system:serviceaccount:basilica-system:basilica-api -n u-test | grep exec
if kubectl auth can-i --list --as="$API_SA" -n "$TENANT_NS" 2>/dev/null | grep -q "pods/exec.*\[create\]"; then
  log "✓ API can exec into pods in tenant namespace (verified via --list)"
elif kubectl auth can-i create pods/exec --as="$API_SA" -n "$TENANT_NS" &>/dev/null; then
  log "✓ API can exec into pods in tenant namespace"
else
  error "✗ API can exec into pods in tenant namespace"
  FAILURES=$((FAILURES + 1))
fi

# Pod listing (for resolving rental -> pod)
test_perm "$API_SA" "get" "pods" "$TENANT_NS" "API can get pods in tenant namespace"
test_perm "$API_SA" "list" "pods" "$TENANT_NS" "API can list pods in tenant namespace"

log ""

# ============================================================================
# VALIDATOR RBAC CHECKS
# ============================================================================

log "Validating Validator RBAC ($VALIDATOR_SA)..."
log ""

# NodeProfile management
test_perm "$VALIDATOR_SA" "create" "basilicanodeprofiles" "" "Validator can create BasilicaNodeProfiles (cluster-scoped)"
test_perm "$VALIDATOR_SA" "get" "basilicanodeprofiles" "" "Validator can get BasilicaNodeProfiles (cluster-scoped)"

# Note: kubectl auth can-i has issues testing status subresources
# Verify with: kubectl auth can-i --list --as=system:serviceaccount:basilica-validators:basilica-validator | grep status
if kubectl auth can-i --list --as="$VALIDATOR_SA" 2>/dev/null | grep -q "basilicanodeprofiles/status.*\[patch\|update\]"; then
  log "✓ Validator can patch BasilicaNodeProfile status (verified via --list)"
elif kubectl auth can-i patch basilicanodeprofiles/status --as="$VALIDATOR_SA" &>/dev/null; then
  log "✓ Validator can patch BasilicaNodeProfile status"
else
  error "✗ Validator can patch BasilicaNodeProfile status"
  FAILURES=$((FAILURES + 1))
fi

# Node label/taint patching (optional, for node profile publisher)
# Note: This may not be required if validator only creates NodeProfiles and operator patches nodes
test_perm "$VALIDATOR_SA" "get" "nodes" "" "Validator can get nodes (for profile publishing)"
# Uncomment if validator needs to patch node labels/taints directly:
# test_perm "$VALIDATOR_SA" "patch" "nodes" "" "Validator can patch nodes (for labels/taints)"

log ""

# ============================================================================
# SUMMARY
# ============================================================================

log "========================================="
if [ $FAILURES -eq 0 ]; then
  log "✅ All RBAC checks PASSED"
  log "========================================="
  exit 0
else
  error "========================================="
  error "❌ $FAILURES RBAC check(s) FAILED"
  error "========================================="
  error ""
  error "Fix by reviewing and applying RBAC manifests:"
  error "  kubectl apply -f config/rbac/operator-rbac.yaml"
  error "  kubectl apply -f config/rbac/api-rbac.yaml"
  error "  kubectl apply -f config/rbac/validator-rbac.yaml"
  error "  sed \"s/TENANT_NAMESPACE/$TENANT_NS/g\" config/rbac/operator-tenant-role.yaml | kubectl apply -f -"
  exit 1
fi
