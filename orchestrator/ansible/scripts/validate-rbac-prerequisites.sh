#!/usr/bin/env bash

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
EXIT_CODE=0

log_info() {
    echo "[INFO] $*"
}

log_error() {
    echo "[ERROR] $*" >&2
}

log_success() {
    echo "[SUCCESS] $*"
}

check_clusterrole() {
    local role_name="$1"

    if kubectl get clusterrole "$role_name" &>/dev/null; then
        log_success "ClusterRole '$role_name' exists"
        return 0
    else
        log_error "ClusterRole '$role_name' NOT FOUND"
        return 1
    fi
}

check_rbac_enabled() {
    log_info "Checking if RBAC is enabled in cluster..."

    if kubectl auth can-i create clusterroles --as=system:serviceaccount:default:default &>/dev/null; then
        log_info "RBAC check: API server responds to authorization queries"
    fi

    local api_resources
    if api_resources=$(kubectl api-resources --api-group=rbac.authorization.k8s.io 2>&1); then
        log_success "RBAC API group is available"
        return 0
    else
        log_error "RBAC API group is NOT available - RBAC may be disabled"
        log_error "Output: $api_resources"
        return 1
    fi
}

main() {
    log_info "=========================================="
    log_info "Basilica RBAC Prerequisites Validation"
    log_info "=========================================="
    echo

    if ! check_rbac_enabled; then
        EXIT_CODE=1
    fi

    echo
    log_info "Checking required built-in ClusterRoles for bootstrap token support..."
    echo

    if ! check_clusterrole "system:node-bootstrapper"; then
        EXIT_CODE=1
        log_error "This ClusterRole is required for kubelet bootstrap authentication"
        log_error "It should be created automatically during K3s cluster initialization"
    fi

    if ! check_clusterrole "system:certificates.k8s.io:certificatesigningrequests:nodeclient"; then
        EXIT_CODE=1
        log_error "This ClusterRole is required for automatic CSR approval during node joining"
        log_error "It should be created automatically during K3s cluster initialization"
    fi

    echo
    log_info "=========================================="

    if [ $EXIT_CODE -eq 0 ]; then
        log_success "All RBAC prerequisites are satisfied"
        log_info "Safe to apply orchestrator/k8s/core/rbac/bootstrap-token-rbac.yaml"
    else
        log_error "RBAC prerequisites are NOT satisfied"
        log_error ""
        log_error "Troubleshooting steps:"
        log_error "1. Verify K3s cluster is properly initialized"
        log_error "2. Check K3s server logs for initialization errors"
        log_error "3. Confirm RBAC is not explicitly disabled via --disable=rbac flag"
        log_error "4. Restart K3s server if necessary: systemctl restart k3s"
        log_error ""
        log_error "For K3s clusters, these ClusterRoles are created automatically."
        log_error "If they are missing, the cluster may not be fully initialized."
    fi

    echo "=========================================="
    exit $EXIT_CODE
}

main "$@"
