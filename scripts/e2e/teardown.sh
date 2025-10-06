#!/usr/bin/env bash
set -euo pipefail

# Quick E2E teardown script for Basilica K3s/K8s stack
# Safely removes resources created by scripts/e2e/apply.sh and docs Fast Path.
#
# Usage:
#   scripts/e2e/teardown.sh [--keep-namespaces] [--keep-crds] [--keep-monitoring]
# Env:
#   TENANT_NS (default: u-test)

KEEP_NS=false
KEEP_CRDS=false
KEEP_MON=false
TENANT_NS=${TENANT_NS:-u-test}

for arg in "$@"; do
  case "$arg" in
    --keep-namespaces) KEEP_NS=true ;;
    --keep-crds) KEEP_CRDS=true ;;
    --keep-monitoring) KEEP_MON=true ;;
    *) ;;
  esac
done

echo "[teardown] Deleting ServiceMonitors (if present)"
kubectl delete -f config/deploy/operator-servicemonitor.yaml --ignore-not-found || true
kubectl delete -f config/deploy/api-servicemonitor.yaml --ignore-not-found || true
kubectl delete -f config/deploy/validator-servicemonitor.yaml --ignore-not-found || true

if [ "$KEEP_MON" = false ]; then
  echo "[teardown] Deleting minimal Prometheus + Grafana (if applied)"
  kubectl delete -f config/deploy/monitoring/grafana.yaml --ignore-not-found || true
  kubectl delete -f config/deploy/monitoring/prometheus.yaml --ignore-not-found || true
fi

echo "[teardown] Deleting API/Operator/Validator/DB manifests"
kubectl delete -f config/deploy/api-deployment.yaml --ignore-not-found || true
kubectl delete -f config/deploy/operator-deployment.yaml --ignore-not-found || true
kubectl delete -f config/deploy/validator-deployment.yaml --ignore-not-found || true
kubectl delete -f config/deploy/postgres.yaml --ignore-not-found || true

echo "[teardown] Deleting Envoy forward proxy (if applied)"
kubectl delete -f config/deploy/ingress/envoy-service.yaml --ignore-not-found || true
kubectl delete -f config/deploy/ingress/envoy-deployment.yaml --ignore-not-found || true
kubectl delete -f config/deploy/ingress/envoy-configmap.yaml --ignore-not-found || true

echo "[teardown] Deleting Gateway API routes and Gateways (if applied)"
kubectl delete -f config/deploy/gateway/httproute-example.yaml --ignore-not-found || true
kubectl delete -f config/deploy/gateway/gateway-u-test.yaml --ignore-not-found || true
kubectl delete -f config/deploy/gateway/gatewayclass.yaml --ignore-not-found || true

echo "[teardown] Note: Envoy Gateway controller/CRDs (if installed) must be uninstalled separately per upstream instructions."

echo "[teardown] Deleting tenant Role/RoleBinding"
sed "s/TENANT_NAMESPACE/${TENANT_NS}/g" config/rbac/operator-tenant-role.yaml | kubectl delete -f - --ignore-not-found || true

echo "[teardown] Deleting cluster RBAC (roles/bindings/serviceaccounts)"
kubectl delete -f config/rbac/operator-rbac.yaml --ignore-not-found || true
kubectl delete -f config/rbac/api-rbac.yaml --ignore-not-found || true
kubectl delete -f config/rbac/validator-rbac.yaml --ignore-not-found || true

if [ "$KEEP_CRDS" = false ]; then
  echo "[teardown] Deleting Basilica CR instances (if any)"
  kubectl delete gpurentals.basilica.io --all -A --ignore-not-found || true
  kubectl delete basilicajobs.basilica.io --all -A --ignore-not-found || true
  kubectl delete basilicanodeprofiles.basilica.io --all -A --ignore-not-found || true
  kubectl delete basilicaqueues.basilica.io --all -A --ignore-not-found || true

  echo "[teardown] Deleting CRDs"
  if [ -f basilica-crds.yaml ]; then
    kubectl delete -f basilica-crds.yaml --ignore-not-found || true
  else
    kubectl delete crd gpurentals.basilica.io basilicajobs.basilica.io basilicanodeprofiles.basilica.io basilicaqueues.basilica.io --ignore-not-found || true
  fi
fi

if [ "$KEEP_NS" = false ]; then
  echo "[teardown] Deleting namespaces (this may take time)"
  kubectl delete namespace basilica-system --ignore-not-found || true
  kubectl delete namespace basilica-validators --ignore-not-found || true
  kubectl delete namespace "${TENANT_NS}" --ignore-not-found || true
fi

echo "[teardown] Complete"
