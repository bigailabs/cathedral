#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

NAMESPACE="basilica-system"
DEPLOYMENT="basilica-operator"
KUBECONFIG_PATH="${KUBECONFIG:-$HOME/.kube/k3s-basilica-config}"
FOLLOW_LOGS=false
WAIT_TIMEOUT="5m"

while [[ $# -gt 0 ]]; do
    case $1 in
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --deployment)
            DEPLOYMENT="$2"
            shift 2
            ;;
        --kubeconfig)
            KUBECONFIG_PATH="$2"
            shift 2
            ;;
        --follow-logs)
            FOLLOW_LOGS=true
            shift
            ;;
        --timeout)
            WAIT_TIMEOUT="$2"
            shift 2
            ;;
        --help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Rollout basilica-operator deployment in K3s cluster"
            echo ""
            echo "Options:"
            echo "  --namespace NAMESPACE     Kubernetes namespace (default: basilica-system)"
            echo "  --deployment DEPLOYMENT   Deployment name (default: basilica-operator)"
            echo "  --kubeconfig PATH         Path to kubeconfig (default: ~/.kube/k3s-basilica-config)"
            echo "  --follow-logs             Follow logs after rollout completes"
            echo "  --timeout DURATION        Rollout wait timeout (default: 5m)"
            echo "  --help                    Show this help message"
            echo ""
            echo "Example:"
            echo "  $0"
            echo "  $0 --follow-logs"
            echo "  $0 --namespace basilica-system --deployment basilica-operator"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

export KUBECONFIG="$KUBECONFIG_PATH"

if [[ ! -f "$KUBECONFIG_PATH" ]]; then
    echo "Error: Kubeconfig not found at $KUBECONFIG_PATH" >&2
    echo "Please ensure the kubeconfig file exists or specify a different path with --kubeconfig" >&2
    exit 1
fi

echo "Rolling out operator deployment..."
echo "  Namespace:  $NAMESPACE"
echo "  Deployment: $DEPLOYMENT"
echo "  Kubeconfig: $KUBECONFIG_PATH"
echo ""

echo "Restarting deployment..."
kubectl rollout restart deployment/"$DEPLOYMENT" -n "$NAMESPACE"

echo ""
echo "Waiting for rollout to complete (timeout: $WAIT_TIMEOUT)..."
kubectl rollout status deployment/"$DEPLOYMENT" -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"

echo ""
echo "Checking pod status..."
kubectl get pods -n "$NAMESPACE" -l app="$DEPLOYMENT"

echo ""
echo "Rollout completed successfully!"

if [[ "$FOLLOW_LOGS" == "true" ]]; then
    echo ""
    echo "Following logs (Ctrl+C to exit)..."
    kubectl logs -n "$NAMESPACE" deployment/"$DEPLOYMENT" --tail=50 -f
fi
