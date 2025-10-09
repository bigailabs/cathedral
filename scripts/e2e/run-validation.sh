#!/usr/bin/env bash
# E2E Validation Runner
# Executes the complete E2E readiness checklist

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${YELLOW}║          Basilica E2E Validation Test Suite                   ║${NC}"
echo -e "${YELLOW}╔════════════════════════════════════════════════════════════════╗${NC}"
echo ""

# Set environment variables
export KUBECONFIG="$PROJECT_ROOT/build/k3s.yaml"
export BASILICA_API_URL="http://localhost:8000"
export TENANT_NAMESPACE="u-test"

# Check prerequisites
echo -e "${YELLOW}[Pre-flight] Checking prerequisites...${NC}"

if [ ! -f "$KUBECONFIG" ]; then
    echo -e "${RED}✗ Kubeconfig not found at $KUBECONFIG${NC}"
    echo "  Run 'just e2e-up' first to set up the environment"
    exit 1
fi

if ! curl -s http://localhost:8000/health > /dev/null; then
    echo -e "${RED}✗ API not responding at http://localhost:8000${NC}"
    echo "  Run 'just local-api-up' to start the API"
    exit 1
fi

# Check cluster connectivity
if ! kubectl cluster-info > /dev/null 2>&1; then
    echo -e "${RED}✗ Cannot connect to Kubernetes cluster${NC}"
    exit 1
fi

# Check operator is running
if ! kubectl get deployment -n basilica-system basilica-operator -o jsonpath='{.status.availableReplicas}' | grep -q "1"; then
    echo -e "${RED}✗ Operator is not running${NC}"
    exit 1
fi

echo -e "${GREEN}✓ All prerequisites met${NC}"
echo ""

# ============================================================================
# STEP 1: RBAC Validation
# ============================================================================
echo -e "${YELLOW}[Step 1/5] Validating RBAC permissions...${NC}"
if [ -x "$SCRIPT_DIR/validate-rbac.sh" ]; then
    if "$SCRIPT_DIR/validate-rbac.sh"; then
        echo -e "${GREEN}✓ RBAC validation passed${NC}"
    else
        echo -e "${RED}✗ RBAC validation failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}✗ validate-rbac.sh not found or not executable${NC}"
    exit 1
fi
echo ""

# ============================================================================
# STEP 2: Bootstrap API Key
# ============================================================================
echo -e "${YELLOW}[Step 2/5] Bootstrapping API key...${NC}"
if [ -x "$SCRIPT_DIR/bootstrap-api-key.sh" ]; then
    # Run bootstrap script and capture token
    BOOTSTRAP_OUTPUT=$("$SCRIPT_DIR/bootstrap-api-key.sh" 2>&1)

    # Extract token from output
    if echo "$BOOTSTRAP_OUTPUT" | grep -q "export BASILICA_API_TOKEN"; then
        export BASILICA_API_TOKEN=$(echo "$BOOTSTRAP_OUTPUT" | grep "export BASILICA_API_TOKEN" | cut -d'=' -f2 | tr -d '"' | tr -d "'")
        echo -e "${GREEN}✓ API key generated successfully${NC}"
        echo "  Token: ${BASILICA_API_TOKEN:0:20}..."
    else
        echo -e "${RED}✗ Failed to generate API key${NC}"
        echo "$BOOTSTRAP_OUTPUT"
        exit 1
    fi
else
    echo -e "${RED}✗ bootstrap-api-key.sh not found or not executable${NC}"
    exit 1
fi
echo ""

# ============================================================================
# STEP 3: Rentals Smoke Test
# ============================================================================
echo -e "${YELLOW}[Step 3/5] Running Rentals smoke test...${NC}"
if [ -x "$SCRIPT_DIR/smoke-test-rentals.sh" ]; then
    if "$SCRIPT_DIR/smoke-test-rentals.sh"; then
        echo -e "${GREEN}✓ Rentals smoke test passed${NC}"
    else
        echo -e "${RED}✗ Rentals smoke test failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}✗ smoke-test-rentals.sh not found or not executable${NC}"
    exit 1
fi
echo ""

# ============================================================================
# STEP 4: Jobs Smoke Test
# ============================================================================
echo -e "${YELLOW}[Step 4/5] Running Jobs smoke test...${NC}"
if [ -x "$SCRIPT_DIR/smoke-test-jobs.sh" ]; then
    if "$SCRIPT_DIR/smoke-test-jobs.sh"; then
        echo -e "${GREEN}✓ Jobs smoke test passed${NC}"
    else
        echo -e "${RED}✗ Jobs smoke test failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}✗ smoke-test-jobs.sh not found or not executable${NC}"
    exit 1
fi
echo ""

# ============================================================================
# STEP 5: Node Groups Isolation Test
# ============================================================================
echo -e "${YELLOW}[Step 5/5] Running Node Groups isolation test...${NC}"
if [ -x "$SCRIPT_DIR/smoke-test-node-groups.sh" ]; then
    if "$SCRIPT_DIR/smoke-test-node-groups.sh"; then
        echo -e "${GREEN}✓ Node Groups isolation test passed${NC}"
    else
        echo -e "${RED}✗ Node Groups isolation test failed${NC}"
        exit 1
    fi
else
    echo -e "${RED}✗ smoke-test-node-groups.sh not found or not executable${NC}"
    exit 1
fi
echo ""

# ============================================================================
# Summary
# ============================================================================
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              🎉 All E2E Validation Tests Passed! 🎉           ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Summary of tests run:"
echo "  ✓ RBAC permissions validated"
echo "  ✓ API key authentication working"
echo "  ✓ Rentals v2 lifecycle (create/logs/exec/delete)"
echo "  ✓ Jobs v1 lifecycle (create/logs/delete)"
echo "  ✓ Node Groups isolation (jobs vs rentals workload separation)"
echo ""
echo "Next steps:"
echo "  1. Review test output above for any warnings"
echo "  2. Check operator logs: kubectl logs -n basilica-system deploy/basilica-operator"
echo "  3. Check API logs: docker logs basilica-api-local"
echo "  4. Document any findings in docs/e2e-gaps-and-tests.md"
echo ""
echo "To clean up:"
echo "  just e2e-down"
