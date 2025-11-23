#!/bin/bash
set -euo pipefail

echo "Creating ReferenceGrants for existing user namespaces..."

for ns in $(kubectl get namespaces -o jsonpath='{.items[?(@.metadata.name matches "^u-.*")].metadata.name}' 2>/dev/null || kubectl get namespaces -o json | jq -r '.items[] | select(.metadata.name | startswith("u-")) | .metadata.name'); do
  echo "Creating ReferenceGrant for namespace: $ns"

  cat <<EOF | kubectl apply -f -
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-httproutes-$ns
  namespace: basilica-system
spec:
  from:
  - group: gateway.networking.k8s.io
    kind: HTTPRoute
    namespace: $ns
  to:
  - group: gateway.networking.k8s.io
    kind: Gateway
    name: basilica-gateway
EOF
done

echo "Done creating ReferenceGrants for existing user namespaces"
