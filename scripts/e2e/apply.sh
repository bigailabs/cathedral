#!/usr/bin/env bash
set -euo pipefail

# Quick E2E apply script for Basilica K3s stack (Operator + API + optional Validator)
# Usage:
#   scripts/e2e/apply.sh [--with-validator]
# Env:
#   TENANT_NS (default: u-test)

WITH_VALIDATOR=false
WITH_ENVOY_PROXY=true
WITH_GATEWAY=true
TENANT_NS=${TENANT_NS:-u-test}

for arg in "$@"; do
  case "$arg" in
    --with-validator)
      WITH_VALIDATOR=true
      shift
      ;;
    --with-envoy-proxy)
      WITH_ENVOY_PROXY=true
      shift
      ;;
    --no-envoy-proxy)
      WITH_ENVOY_PROXY=false
      shift
      ;;
    --with-gateway)
      WITH_GATEWAY=true
      shift
      ;;
    --no-gateway)
      WITH_GATEWAY=false
      shift
      ;;
    *) ;;
  esac
done

echo "[e2e] Applying namespaces"
kubectl apply -f config/deploy/namespaces.yaml

echo "[e2e] Applying RBAC"
kubectl apply -f config/rbac/operator-rbac.yaml
kubectl apply -f config/rbac/api-rbac.yaml
kubectl apply -f config/rbac/validator-rbac.yaml
sed "s/TENANT_NAMESPACE/${TENANT_NS}/g" config/rbac/operator-tenant-role.yaml | kubectl apply -f -

echo "[e2e] Generating CRDs (requires Rust toolchain)"
cargo run -p basilica-operator --bin crdgen > basilica-crds.yaml
kubectl apply -f basilica-crds.yaml

echo "[e2e] Deploying Postgres"
kubectl apply -f config/deploy/postgres.yaml

echo "[e2e] Deploying Operator"
kubectl apply -f config/deploy/operator-deployment.yaml

echo "[e2e] Deploying API"
kubectl apply -f config/deploy/api-deployment.yaml

echo "[e2e] Applying ServiceMonitors (if Prometheus Operator is installed)"
kubectl apply -f config/deploy/operator-servicemonitor.yaml || true
kubectl apply -f config/deploy/api-servicemonitor.yaml || true

if [ "$WITH_VALIDATOR" = true ]; then
  echo "[e2e] Deploying Validator"
  kubectl apply -f config/deploy/validator-deployment.yaml
  kubectl apply -f config/deploy/validator-servicemonitor.yaml || true
fi

if [ "$WITH_ENVOY_PROXY" = true ]; then
  echo "[e2e] Deploying Envoy forward proxy (HTTP)"
  kubectl apply -f config/deploy/ingress/envoy-configmap.yaml
  kubectl apply -f config/deploy/ingress/envoy-deployment.yaml
  kubectl apply -f config/deploy/ingress/envoy-service.yaml
fi

if [ "$WITH_GATEWAY" = true ]; then
  echo "[e2e] Setting up Gateway API (Envoy Gateway)"
  # Optional: install Envoy Gateway controller (requires cluster egress/network)
  # Set INSTALL_ENVOY_GATEWAY=false to skip installation from GitHub releases.
  if [ "${INSTALL_ENVOY_GATEWAY:-true}" = true ]; then
    EGW_VERSION=${ENVOY_GATEWAY_VERSION:-v1.0.0}
    echo "[e2e] Installing Envoy Gateway ${EGW_VERSION} (controller + data plane)"
    kubectl apply -f "https://github.com/envoyproxy/gateway/releases/download/${EGW_VERSION}/install.yaml" || {
      echo "[warn] Failed to install Envoy Gateway from internet. Please install manually as per docs.";
    }
  else
    echo "[e2e] Skipping Envoy Gateway controller install (unset INSTALL_ENVOY_GATEWAY or set to true to enable)"
  fi

  echo "[e2e] Applying GatewayClass and a sample tenant Gateway/HTTPRoute"
  kubectl apply -f config/deploy/gateway/gatewayclass.yaml
  # Per-namespace Gateway: by default for TENANT_NS
  if [ "$TENANT_NS" != "u-test" ]; then
    # Render a Gateway for the target TENANT_NS from the example
    sed "s/namespace: u-test/namespace: ${TENANT_NS}/g" config/deploy/gateway/gateway-u-test.yaml | kubectl apply -f -
    sed "s/namespace: u-test/namespace: ${TENANT_NS}/g" config/deploy/gateway/httproute-example.yaml | kubectl apply -f -
  else
    kubectl apply -f config/deploy/gateway/gateway-u-test.yaml
    kubectl apply -f config/deploy/gateway/httproute-example.yaml
  fi
fi

echo "[e2e] Done. Port-forward API: kubectl -n basilica-system port-forward deploy/basilica-api 8000:8000"
