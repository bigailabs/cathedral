#!/bin/bash
# Launch k9s connected to the Basilica K3s cluster
# Usage: ./scripts/k9s-basilica.sh [namespace]

KUBECONFIG="${HOME}/.kube/k3s-basilica-config"

if [ ! -f "$KUBECONFIG" ]; then
  echo "Error: Kubeconfig not found at $KUBECONFIG"
  echo ""
  echo "Run this playbook to fetch it:"
  echo "  cd scripts/ansible"
  echo "  ansible-playbook -i inventories/production.ini playbooks/get-kubeconfig.yml --vault-password-file=./.vault_password"
  exit 1
fi

NAMESPACE="${1:-basilica-system}"

export KUBECONFIG
echo "Connecting to K3s cluster..."
echo "Namespace: $NAMESPACE"
echo ""

k9s -n "$NAMESPACE"
