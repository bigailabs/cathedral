#!/bin/bash
set -e

# Basilica K3s Deployment Script

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/ansible"

# Default values
INVENTORY="inventories/production.ini"
TAGS=""
CHECK_MODE=""
VAULT_PASSWORD=""
VERBOSE=""
SKIP_VERIFY=""

# Function to print output
print_status() {
    echo "[INFO] $1"
}

print_success() {
    echo "[SUCCESS] $1"
}

print_warning() {
    echo "[WARNING] $1"
}

print_error() {
    echo "[ERROR] $1"
}

# Function to show usage
show_usage() {
    cat << EOF
Usage: $0 [OPTIONS]

Deploy Basilica K3s infrastructure using Ansible

OPTIONS:
    -i, --inventory FILE     Inventory file (default: inventories/production.ini)
    -t, --tags TAGS         Run only tasks tagged with these tags
    -c, --check             Run in check mode (dry run)
    -v, --verbose           Verbose output (-vvv)
    --skip-verify           Skip verification phase
    --vault-password FILE   Vault password file
    --vault-prompt          Prompt for vault password
    -h, --help              Show this help message

TAGS:
    setup                   Setup phase (K3s cluster provisioning)
    deploy                  Deploy phase (Basilica services)
    verify                  Verify phase (health checks)

EXAMPLES:
    $0                                    # Full deployment (setup + deploy + verify)
    $0 -c                                 # Dry run
    $0 -t setup                           # Setup K3s cluster only
    $0 -t deploy                          # Deploy services only
    $0 --skip-verify                      # Deploy without verification
    $0 -i inventories/example.ini         # Use different inventory
    $0 --vault-prompt                     # Prompt for vault password

EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--inventory)
            INVENTORY="$2"
            shift 2
            ;;
        -t|--tags)
            TAGS="--tags $2"
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
        --skip-verify)
            SKIP_VERIFY="--skip-tags verify"
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
            print_error "Unknown option $1"
            show_usage
            exit 1
            ;;
    esac
done

# Pre-flight checks
print_status "Running pre-flight checks..."

# Check if ansible is installed
if ! command -v ansible-playbook &> /dev/null; then
    print_error "ansible-playbook is not installed or not in PATH"
    print_warning "Run: ./scripts/00-install-ansible.sh"
    exit 1
fi

# Check if inventory file exists
if [[ ! -f "$INVENTORY" ]]; then
    print_error "Inventory file '$INVENTORY' not found"
    print_warning "Copy inventories/example.ini to inventories/production.ini and configure your hosts"
    exit 1
fi

# Check if playbook exists
if [[ ! -f "playbook.yml" ]]; then
    print_error "Playbook file 'playbook.yml' not found"
    exit 1
fi

# Check if required directories exist
if [[ ! -d "group_vars" ]]; then
    print_error "Configuration directory 'group_vars/' not found"
    exit 1
fi

print_success "Pre-flight checks passed"

# Build ansible command
ANSIBLE_CMD="ansible-playbook -i $INVENTORY playbook.yml"

if [[ -n "$TAGS" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $TAGS"
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

if [[ -n "$SKIP_VERIFY" ]]; then
    ANSIBLE_CMD="$ANSIBLE_CMD $SKIP_VERIFY"
    print_warning "Verification phase will be skipped"
fi

# Show deployment information
print_status "Deployment Configuration:"
echo "  Inventory: $INVENTORY"
echo "  Playbook: playbook.yml"
if [[ -n "$TAGS" ]]; then
    echo "  Tags: ${TAGS#--tags }"
fi
if [[ -n "$CHECK_MODE" ]]; then
    echo "  Mode: Check (dry run)"
else
    echo "  Mode: Deploy"
fi

# Confirm deployment
if [[ -z "$CHECK_MODE" ]]; then
    echo
    read -p "Proceed with deployment? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        print_status "Deployment cancelled"
        exit 0
    fi
fi

# Run ansible playbook
print_status "Starting deployment..."
print_status "Command: $ANSIBLE_CMD"
echo

# Execute the command
if eval $ANSIBLE_CMD; then
    echo
    print_success "Deployment completed successfully!"

    if [[ -z "$CHECK_MODE" ]]; then
        echo
        print_status "Next steps:"
        echo "  1. Fetch kubeconfig:"
        echo "     cd ansible && ansible-playbook -i $INVENTORY playbooks/04-maintain/kubeconfig.yml"
        echo
        echo "  2. Verify cluster health:"
        echo "     export KUBECONFIG=~/.kube/k3s-basilica-config"
        echo "     kubectl get nodes"
        echo "     kubectl get pods -n basilica-system"
        echo
        echo "  3. Access cluster with k9s:"
        echo "     ./scripts/k9s-basilica.sh"
    fi
else
    echo
    print_error "Deployment failed!"
    print_status "Check logs above for error details"
    print_status "For diagnostics, run:"
    echo "     ansible-playbook -i $INVENTORY playbooks/03-verify/diagnose.yml"
    exit 1
fi
