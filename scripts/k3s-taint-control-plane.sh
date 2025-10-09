#!/bin/bash
# Taint K3s control plane nodes to prevent workload scheduling
set -euo pipefail

KUBECONFIG=${KUBECONFIG:-build/k3s.yaml}

echo "🔒 Tainting control plane nodes to prevent workload scheduling..."

# Taint all nodes with control-plane role
kubectl --kubeconfig="$KUBECONFIG" taint nodes \
  -l node-role.kubernetes.io/control-plane \
  node-role.kubernetes.io/control-plane=:NoSchedule \
  --overwrite || true

echo "✅ Control plane nodes tainted successfully"
echo ""

# Verify
echo "📋 Control plane nodes status:"
kubectl --kubeconfig="$KUBECONFIG" get nodes \
  -l node-role.kubernetes.io/control-plane \
  -o custom-columns=NAME:.metadata.name,ROLE:.metadata.labels.node-role\\.kubernetes\\.io/control-plane,TAINTS:.spec.taints

echo ""
echo "📋 Miner nodes status:"
kubectl --kubeconfig="$KUBECONFIG" get nodes \
  -l basilica.ai/validated=true \
  -o custom-columns=NAME:.metadata.name,GPU:.metadata.labels.basilica\\.ai/gpu-model,TAINTS:.spec.taints 2>/dev/null || echo "No miner nodes onboarded yet"
