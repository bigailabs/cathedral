#!/usr/bin/env bash
set -euo pipefail

# Quick E2E apply script for Basilica K3s stack (Operator + API + optional Validator)
# Usage:
#   scripts/e2e/apply.sh [--with-validator]
# Env:
#   TENANT_NS (default: u-test)

WITH_VALIDATOR=false
TENANT_NS=${TENANT_NS:-u-test}

for arg in "$@"; do
  case "$arg" in
    --with-validator)
      WITH_VALIDATOR=true
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

echo "[e2e] Done. Port-forward API: kubectl -n basilica-system port-forward deploy/basilica-api 8000:8000"

