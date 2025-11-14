#!/bin/bash
set -e

# Basilica K3s Idempotent Redeployment Script
#
# This script provides targeted, idempotent redeployment options for Basilica components
# without tearing down or recreating the K3s cluster.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/ansible"

INVENTORY="inventories/production.ini"
CHECK_MODE=""
VERBOSE=""
VAULT_PASSWORD=""
MODE=""
START_AT=""

print_status() {
    echo "[INFO] $1"
}

print_success() {
    echo "[SUCCESS] $1"
}

print_error() {
    echo "[ERROR] $1"
}

show_usage() {
    cat << 'EOF'
Usage: ./redeploy.sh MODE [OPTIONS]

Idempotent redeployment of Basilica components without cluster teardown.

MODES:
    operator            Redeploy operator only (fastest, safest)
    services            Redeploy all Basilica services (CRDs, RBAC, operator, observability)
    post-operator       Continue from after operator deployment (observability, envoy, gateway-api)
    full                Full deployment (includes K3s setup if needed - idempotent)

OPTIONS:
    -i, --inventory FILE    Inventory file (default: inventories/production.ini)
    -c, --check            Dry run mode
    -v, --verbose          Verbose output (-vvv)
    --vault-password FILE  Vault password file
    --vault-prompt         Prompt for vault password
    -h, --help             Show this help

EXAMPLES:
    # Redeploy operator after code changes
    ./redeploy.sh operator

    # Dry run of services redeployment
    ./redeploy.sh services --check

    # Full idempotent deployment (safe to run multiple times)
    ./redeploy.sh full

    # Redeploy with vault password
    ./redeploy.sh services --vault-prompt

    # Continue deployment after operator is running
    ./redeploy.sh post-operator

NOTES:
    - All modes are idempotent and safe to run multiple times
    - No teardown occurs - existing resources are updated in place
    - 'operator' mode: ~30 seconds
    - 'services' mode: ~2-3 minutes
    - 'full' mode: ~5-10 minutes (if K3s already installed)

EOF
}

if [[ $# -eq 0 ]]; then
    print_error "No mode specified"
    show_usage
    exit 1
fi

if [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]]; then
    show_usage
    exit 0
fi

MODE=$1
shift

while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--inventory)
            INVENTORY="$2"
            shift 2
            ;;
        -c|--check)
            CHECK_MODE="--check"
            shift
            ;;
        -v|--verbose)
            VERBOSE="-vvv"
            shift
            ;;
        --vault-password)
            VAULT_PASSWORD="--vault-password-file $2"
            shift 2
            ;;
        --vault-prompt)
            VAULT_PASSWORD="--ask-vault-pass"
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            print_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

if [[ ! -f "$INVENTORY" ]]; then
    print_error "Inventory file '$INVENTORY' not found"
    exit 1
fi

if ! command -v ansible-playbook &> /dev/null; then
    print_error "ansible-playbook not found in PATH"
    exit 1
fi

case "$MODE" in
    operator)
        print_status "Mode: Operator redeployment"
        print_status "This will restart the basilica-operator deployment with latest image"

        PLAYBOOK="playbooks/02-deploy/basilica.yml"
        TAGS="--tags deploy_operator"
        SKIP_TAGS=""
        ;;

    services)
        print_status "Mode: Services redeployment"
        print_status "This will apply CRDs, RBAC, operator, and observability components"

        PLAYBOOK="playbooks/02-deploy/basilica.yml"
        TAGS=""
        SKIP_TAGS=""
        ;;

    post-operator)
        print_status "Mode: Post-operator deployment"
        print_status "This continues from after operator deployment: telemetry, disk cleanup, envoy, gateway-api"

        PLAYBOOK="playbooks/02-deploy/basilica.yml"
        TAGS=""
        SKIP_TAGS=""
        START_AT="--start-at-task='Deploy Alloy telemetry ConfigMap'"
        ;;

    full)
        print_status "Mode: Full idempotent deployment"
        print_status "This will run setup + deploy phases (idempotent, no teardown)"

        PLAYBOOK="playbook.yml"
        TAGS=""
        SKIP_TAGS="--skip-tags verify"
        ;;

    *)
        print_error "Unknown mode: $MODE"
        print_error "Valid modes: operator, services, post-operator, full"
        show_usage
        exit 1
        ;;
esac

ANSIBLE_CMD="ansible-playbook -i $INVENTORY $PLAYBOOK"

if [[ -n "$START_AT" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $START_AT"
fi

if [[ -n "$TAGS" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $TAGS"
fi

if [[ -n "$SKIP_TAGS" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $SKIP_TAGS"
fi

if [[ -n "$CHECK_MODE" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $CHECK_MODE"
    print_status "Running in check mode (dry run)"
fi

if [[ -n "$VERBOSE" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $VERBOSE"
fi

if [[ -n "$VAULT_PASSWORD" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $VAULT_PASSWORD"
fi

print_status "Deployment Configuration:"
echo "  Inventory: $INVENTORY"
echo "  Playbook: $PLAYBOOK"
echo "  Mode: $MODE"
if [[ -n "$TAGS" ]]; then
    echo "  Tags: ${TAGS#--tags }"
fi
if [[ -n "$CHECK_MODE" ]]; then
    echo "  Check mode: enabled"
fi

if [[ -z "$CHECK_MODE" ]]; then
    echo
    read -p "Proceed with redeployment? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_status "Redeployment cancelled"
        exit 0
    fi
fi

print_status "Starting redeployment..."
print_status "Command: $ANSIBLE_CMD"
echo

if eval $ANSIBLE_CMD; then
    echo
    print_success "Redeployment completed successfully!"

    if [[ "$MODE" == "operator" ]] && [[ -z "$CHECK_MODE" ]]; then
        echo
        print_status "Verify operator status:"
        echo "  kubectl get pods -n basilica-system -l app=basilica-operator"
        echo "  kubectl logs -n basilica-system deployment/basilica-operator --tail=20"
    elif [[ "$MODE" == "services" ]] && [[ -z "$CHECK_MODE" ]]; then
        echo
        print_status "Verify deployment:"
        echo "  kubectl get all -n basilica-system"
        echo "  kubectl get crds | grep basilica"
    elif [[ "$MODE" == "post-operator" ]] && [[ -z "$CHECK_MODE" ]]; then
        echo
        print_status "Verify observability:"
        echo "  kubectl get daemonset -n basilica-system alloy"
        echo "  kubectl get cronjob -n basilica-system disk-cleanup"
        echo "  systemctl status basilica-envoy-portforward"
    elif [[ "$MODE" == "full" ]] && [[ -z "$CHECK_MODE" ]]; then
        echo
        print_status "Verify cluster:"
        echo "  kubectl get nodes"
        echo "  kubectl get all -n basilica-system"
    fi
else
    echo
    print_error "Redeployment failed!"
    exit 1
fi
