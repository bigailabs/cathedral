#!/bin/bash
# Test script for K3s node onboarding feature
#
# This script verifies that the validator can successfully onboard validated
# miner nodes to the K3s cluster.

set -e

echo "========================================="
echo "  Basilica Node Onboarding Test Script"
echo "========================================="
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
KUBECONFIG=${KUBECONFIG:-"build/k3s.yaml"}
VALIDATOR_NAMESPACE=${VALIDATOR_NAMESPACE:-"basilica-validators"}
OPERATOR_NAMESPACE=${OPERATOR_NAMESPACE:-"basilica-system"}

# Functions
check_prereq() {
    local cmd=$1
    local name=$2
    if ! command -v "$cmd" &> /dev/null; then
        echo -e "${RED}✗${NC} $name not found. Please install it."
        exit 1
    fi
    echo -e "${GREEN}✓${NC} $name found"
}

check_env_var() {
    local var=$1
    local deployment=$2

    echo -n "  Checking $var... "
    if kubectl -n "$deployment" exec deploy/basilica-validator -- env 2>/dev/null | grep -q "^$var="; then
        local value=$(kubectl -n "$deployment" exec deploy/basilica-validator -- env 2>/dev/null | grep "^$var=" | cut -d'=' -f2)
        if [ "$var" = "BASILICA_K3S_TOKEN" ]; then
            echo -e "${GREEN}✓${NC} Set (hidden)"
        else
            echo -e "${GREEN}✓${NC} $value"
        fi
        return 0
    else
        echo -e "${RED}✗${NC} Not set"
        return 1
    fi
}

# Step 1: Check prerequisites
echo "1. Checking prerequisites..."
check_prereq kubectl "kubectl"
check_prereq jq "jq"
echo ""

# Step 2: Check K3s cluster
echo "2. Checking K3s cluster..."
if ! kubectl --kubeconfig="$KUBECONFIG" cluster-info &> /dev/null; then
    echo -e "${RED}✗${NC} Cannot connect to K3s cluster"
    echo "  Make sure KUBECONFIG is set correctly: export KUBECONFIG=$KUBECONFIG"
    exit 1
fi
echo -e "${GREEN}✓${NC} K3s cluster is accessible"

K3S_SERVER=$(kubectl --kubeconfig="$KUBECONFIG" config view -o jsonpath='{.clusters[0].cluster.server}')
echo "  Server: $K3S_SERVER"
echo ""

# Step 3: Check validator deployment
echo "3. Checking validator deployment..."
if ! kubectl -n "$VALIDATOR_NAMESPACE" get deploy/basilica-validator &> /dev/null; then
    echo -e "${YELLOW}⚠${NC}  Validator deployment not found in namespace $VALIDATOR_NAMESPACE"
    echo "  This test assumes validator is running. You may need to deploy it first."
else
    echo -e "${GREEN}✓${NC} Validator deployment found"

    # Check if validator is running
    READY=$(kubectl -n "$VALIDATOR_NAMESPACE" get deploy/basilica-validator -o jsonpath='{.status.readyReplicas}')
    if [ "$READY" = "1" ]; then
        echo -e "${GREEN}✓${NC} Validator is ready"
    else
        echo -e "${RED}✗${NC} Validator is not ready"
    fi
fi
echo ""

# Step 4: Check environment variables
echo "4. Checking K3s join configuration..."
if kubectl -n "$VALIDATOR_NAMESPACE" get deploy/basilica-validator &> /dev/null; then
    all_set=true
    check_env_var "BASILICA_ENABLE_K3S_JOIN" "$VALIDATOR_NAMESPACE" || all_set=false
    check_env_var "BASILICA_K3S_URL" "$VALIDATOR_NAMESPACE" || all_set=false
    check_env_var "BASILICA_K3S_TOKEN" "$VALIDATOR_NAMESPACE" || all_set=false

    if [ "$all_set" = true ]; then
        echo -e "${GREEN}✓${NC} All required environment variables are set"
    else
        echo -e "${YELLOW}⚠${NC}  Some environment variables are missing"
        echo ""
        echo "To enable node onboarding, set these environment variables:"
        echo ""
        echo "  export BASILICA_ENABLE_K3S_JOIN=true"
        echo "  export BASILICA_K3S_URL=$K3S_SERVER"
        echo "  export BASILICA_K3S_TOKEN=<get-from-k3s-server>"
        echo ""
        echo "To get the K3s token, run on the K3s server:"
        echo "  sudo cat /var/lib/rancher/k3s/server/node-token"
        echo ""
        exit 1
    fi
fi
echo ""

# Step 5: Check CRDs
echo "5. Checking BasilicaNodeProfile CRD..."
if ! kubectl get crd basilicannodeprofiles.basilica.ai &> /dev/null; then
    echo -e "${RED}✗${NC} BasilicaNodeProfile CRD not found"
    echo "  Install CRDs: kubectl apply -f basilica-crds.yaml"
    exit 1
fi
echo -e "${GREEN}✓${NC} BasilicaNodeProfile CRD exists"
echo ""

# Step 6: Check operator
echo "6. Checking Basilica operator..."
if ! kubectl -n "$OPERATOR_NAMESPACE" get deploy/basilica-operator &> /dev/null; then
    echo -e "${YELLOW}⚠${NC}  Operator deployment not found"
else
    READY=$(kubectl -n "$OPERATOR_NAMESPACE" get deploy/basilica-operator -o jsonpath='{.status.readyReplicas}')
    if [ "$READY" = "1" ]; then
        echo -e "${GREEN}✓${NC} Operator is ready"
    else
        echo -e "${RED}✗${NC} Operator is not ready"
    fi
fi
echo ""

# Step 7: Check existing nodes
echo "7. Checking current K3s nodes..."
kubectl get nodes
echo ""

# Step 8: Check existing NodeProfiles
echo "8. Checking existing BasilicaNodeProfiles..."
if kubectl get basilicannodeprofiles.basilica.ai &> /dev/null; then
    COUNT=$(kubectl get basilicannodeprofiles.basilica.ai --no-headers 2>/dev/null | wc -l)
    if [ "$COUNT" -gt 0 ]; then
        echo "Found $COUNT NodeProfile(s):"
        kubectl get basilicannodeprofiles.basilica.ai -o custom-columns=NAME:.metadata.name,NODE:.status.kubeNodeName,HEALTH:.status.health,GPU:.spec.gpuCount,VALIDATED:.status.lastValidated
    else
        echo "No NodeProfiles found yet"
    fi
else
    echo "Unable to list NodeProfiles"
fi
echo ""

# Step 9: Show monitoring commands
echo "========================================="
echo "  Test Setup Complete!"
echo "========================================="
echo ""
echo "Node onboarding is configured and ready. To test:"
echo ""
echo "1. Watch for new nodes joining:"
echo "   watch -n 1 kubectl get nodes -o wide"
echo ""
echo "2. Watch NodeProfile CRs:"
echo "   watch -n 1 \"kubectl get basilicannodeprofiles -o custom-columns=NAME:.metadata.name,NODE:.status.kubeNodeName,HEALTH:.status.health,GPU:.spec.gpuCount\""
echo ""
echo "3. Watch validator logs:"
echo "   kubectl -n $VALIDATOR_NAMESPACE logs -f deploy/basilica-validator | grep -E 'K3s|join|node|validation'"
echo ""
echo "4. Trigger a validation (if you have a test miner):"
echo "   # Via validator API or automatic verification cycle"
echo ""
echo "5. After a node is validated, check it joined:"
echo "   kubectl get node <node-id> -o jsonpath='{.metadata.labels}' | jq ."
echo "   kubectl get node <node-id> -o jsonpath='{.spec.taints}' | jq ."
echo ""
echo "6. Verify the NodeProfile:"
echo "   kubectl get basilicannodeprofile <node-id> -o yaml"
echo ""
echo -e "${GREEN}✓${NC} Ready to test node onboarding!"
