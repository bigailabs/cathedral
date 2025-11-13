#!/bin/bash
set -euo pipefail

KUBECONFIG="${KUBECONFIG:-~/.kube/k3s-basilica-config}"
export KUBECONFIG

echo "=== Applying Control Plane Taints ==="
echo

echo "Getting control plane nodes..."
CONTROL_PLANE_NODES=$(kubectl get nodes -l node-role.kubernetes.io/control-plane=true -o jsonpath='{.items[*].metadata.name}')

if [ -z "$CONTROL_PLANE_NODES" ]; then
    echo "ERROR: No control plane nodes found!"
    exit 1
fi

echo "Control plane nodes: $CONTROL_PLANE_NODES"
echo

for node in $CONTROL_PLANE_NODES; do
    echo "Applying basilica.ai/control-plane-only=true:NoSchedule taint to $node..."
    kubectl taint nodes "$node" basilica.ai/control-plane-only=true:NoSchedule --overwrite || {
        echo "WARNING: Failed to taint $node (may already be tainted)"
    }
done

echo
echo "=== Verifying Taints ==="
echo

for node in $CONTROL_PLANE_NODES; do
    echo "Node: $node"
    kubectl get node "$node" -o jsonpath='{.spec.taints}' | jq '.'
    echo
done

echo "✅ Control plane taints applied successfully!"
