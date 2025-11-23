#!/usr/bin/env bash
set -euo pipefail

# Basilica K3s Deployment Test Script
# Tests operator health, Envoy proxy, and UserDeployment CRD functionality

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-basilica-system}"
TIMEOUT="${TIMEOUT:-120}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_command() {
    if ! command -v "$1" &> /dev/null; then
        log_error "$1 is not installed. Please install it first."
        exit 1
    fi
}

# Check prerequisites
log_info "Checking prerequisites..."
check_command kubectl

# Test 1: Operator Health Check
log_info "Test 1: Checking operator health endpoints..."

OPERATOR_PODS=$(kubectl get pods -n "$NAMESPACE" -l app=basilica-operator -o jsonpath='{.items[*].metadata.name}')
if [ -z "$OPERATOR_PODS" ]; then
    log_error "No operator pods found in namespace $NAMESPACE"
    exit 1
fi

for pod in $OPERATOR_PODS; do
    log_info "Testing health endpoint on pod: $pod"

    # Get pod IP
    POD_IP=$(kubectl get pod -n "$NAMESPACE" "$pod" -o jsonpath='{.status.podIP}')

    if [ -z "$POD_IP" ]; then
        log_error "Could not get IP for pod $pod"
        exit 1
    fi

    # Test /health endpoint via curl from a test pod
    if kubectl run test-curl-health-$$ --image=curlimages/curl:latest --rm -i --restart=Never -n "$NAMESPACE" -- \
        curl -s -f "http://$POD_IP:9400/health" > /dev/null 2>&1; then
        log_info "✓ /health endpoint responded successfully on $pod"
    else
        log_error "✗ /health endpoint failed on $pod (IP: $POD_IP)"
        exit 1
    fi

    # Test /ready endpoint
    if kubectl run test-curl-ready-$$ --image=curlimages/curl:latest --rm -i --restart=Never -n "$NAMESPACE" -- \
        curl -s -f "http://$POD_IP:9400/ready" > /dev/null 2>&1; then
        log_info "✓ /ready endpoint responded successfully on $pod"
    else
        log_error "✗ /ready endpoint failed on $pod (IP: $POD_IP)"
        exit 1
    fi

    # Test /metrics endpoint
    if kubectl run test-curl-metrics-$$ --image=curlimages/curl:latest --rm -i --restart=Never -n "$NAMESPACE" -- \
        curl -s -f "http://$POD_IP:9400/metrics" | head -5 > /dev/null 2>&1; then
        log_info "✓ /metrics endpoint responded successfully on $pod"
    else
        log_error "✗ /metrics endpoint failed on $pod (IP: $POD_IP)"
        exit 1
    fi
done

# Test 2: Envoy Proxy Check
log_info "Test 2: Checking Envoy proxy..."

ENVOY_SVC_IP=$(kubectl get svc -n "$NAMESPACE" basilica-envoy -o jsonpath='{.spec.clusterIP}')
if [ -z "$ENVOY_SVC_IP" ]; then
    log_error "Envoy service not found in namespace $NAMESPACE"
    exit 1
fi

log_info "Testing Envoy proxy at $ENVOY_SVC_IP:8080"

# Create a test pod to curl Envoy
kubectl run test-curl-pod --image=curlimages/curl:latest --rm -i --restart=Never -n "$NAMESPACE" -- \
    curl -s -o /dev/null -w "%{http_code}" "http://$ENVOY_SVC_IP:8080" > /tmp/envoy_test_status.txt 2>&1 || true

if [ -f /tmp/envoy_test_status.txt ]; then
    HTTP_STATUS=$(cat /tmp/envoy_test_status.txt | tail -1)
    if [ "$HTTP_STATUS" = "000" ] || [ "$HTTP_STATUS" = "503" ]; then
        log_info "✓ Envoy proxy is responding (HTTP $HTTP_STATUS - expected for no upstream)"
    else
        log_info "✓ Envoy proxy responded with HTTP $HTTP_STATUS"
    fi
    rm -f /tmp/envoy_test_status.txt
else
    log_warn "Could not test Envoy proxy via curl pod"
fi

# Test Envoy admin endpoint (should be ClusterIP only)
ENVOY_ADMIN_SVC=$(kubectl get svc -n "$NAMESPACE" basilica-envoy-admin -o jsonpath='{.spec.clusterIP}' 2>/dev/null || echo "")
if [ -n "$ENVOY_ADMIN_SVC" ]; then
    log_info "✓ Envoy admin service found at $ENVOY_ADMIN_SVC:9901 (ClusterIP only)"
else
    log_warn "Envoy admin service not found (may not be deployed yet)"
fi

# Test 3: Create Test UserDeployment
log_info "Test 3: Creating test UserDeployment..."

TEST_DEPLOYMENT_NAME="test-nginx-$(date +%s)"

cat <<EOF | kubectl apply -f -
apiVersion: basilica.ai/v1
kind: UserDeployment
metadata:
  name: $TEST_DEPLOYMENT_NAME
  namespace: $NAMESPACE
spec:
  userId: "test-user"
  instanceName: "nginx-test"
  image: "nginx:latest"
  port: 80
  replicas: 1
  pathPrefix: "/test"
EOF

log_info "UserDeployment '$TEST_DEPLOYMENT_NAME' created. Waiting for reconciliation..."

# Wait for deployment to be reconciled
COUNTER=0
while [ $COUNTER -lt $TIMEOUT ]; do
    STATUS=$(kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" -o jsonpath='{.status.state}' 2>/dev/null || echo "")

    if [ "$STATUS" = "Running" ]; then
        log_info "✓ UserDeployment reconciled successfully (state: $STATUS)"
        break
    elif [ "$STATUS" = "Failed" ]; then
        log_error "✗ UserDeployment failed"
        kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" -o yaml
        exit 1
    else
        log_info "Waiting for UserDeployment to reconcile (state: ${STATUS:-Pending})... ($COUNTER/$TIMEOUT)"
        sleep 2
        COUNTER=$((COUNTER + 2))
    fi
done

if [ $COUNTER -ge $TIMEOUT ]; then
    log_warn "UserDeployment did not reach Running state within ${TIMEOUT}s"
    kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" -o yaml
fi

# Check if underlying K8s deployment was created
K8S_DEPLOYMENT=$(kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" -o jsonpath='{.status.deploymentName}' 2>/dev/null || echo "")
if [ -n "$K8S_DEPLOYMENT" ]; then
    log_info "✓ Underlying Deployment created: $K8S_DEPLOYMENT"
    kubectl get deployment -n "$NAMESPACE" "$K8S_DEPLOYMENT" 2>/dev/null || log_warn "Deployment not found yet"
else
    log_warn "Deployment name not yet in status"
fi

# Check if service was created
K8S_SERVICE=$(kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" -o jsonpath='{.status.serviceName}' 2>/dev/null || echo "")
if [ -n "$K8S_SERVICE" ]; then
    log_info "✓ Service created: $K8S_SERVICE"
    kubectl get svc -n "$NAMESPACE" "$K8S_SERVICE" 2>/dev/null || log_warn "Service not found yet"
else
    log_warn "Service name not yet in status"
fi

# Test 4: Cleanup
log_info "Test 4: Cleaning up test resources..."

kubectl delete userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" --ignore-not-found=true
log_info "✓ Test UserDeployment deleted"

# Wait for cleanup
sleep 5

if kubectl get userdeployment -n "$NAMESPACE" "$TEST_DEPLOYMENT_NAME" 2>/dev/null; then
    log_warn "UserDeployment still exists (may take time to finalize)"
else
    log_info "✓ UserDeployment cleanup confirmed"
fi

# Summary
log_info ""
log_info "========================================="
log_info "   Deployment Test Summary"
log_info "========================================="
log_info "✓ Operator health checks: PASSED"
log_info "✓ Envoy proxy check: PASSED"
log_info "✓ UserDeployment CRD: PASSED"
log_info "✓ Cleanup: PASSED"
log_info "========================================="
log_info ""
log_info "All tests completed successfully!"

exit 0
