#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

INVENTORY="${INVENTORY:-inventories/production.ini}"

echo "==============================================="
echo "Basilica K3s Ansible Configuration Discovery"
echo "==============================================="
echo ""

echo "1. INVENTORY CONFIGURATION"
echo "   Location: $INVENTORY"
echo ""

if [[ -f "$INVENTORY" ]]; then
    echo "   K3s Server Nodes:"
    grep -A 10 '^\[k3s_server\]' "$INVENTORY" | grep -v '^\[' | grep -v '^#' | grep -v '^$' || echo "     None configured"
    echo ""

    echo "   K3s Agent Nodes:"
    grep -A 10 '^\[k3s_agents\]' "$INVENTORY" | grep -v '^\[' | grep -v '^#' | grep -v '^$' || echo "     None configured"
    echo ""
else
    echo "   WARNING: Inventory file not found at $INVENTORY"
    echo ""
fi

echo "2. INFRASTRUCTURE CONFIGURATION"
echo "   Location: group_vars/all/infrastructure.yml"
echo ""

if [[ -f "group_vars/all/infrastructure.yml" ]]; then
    echo "   K3s Version:"
    grep '^k3s_version:' group_vars/all/infrastructure.yml | awk '{print "     " $2}' || echo "     Not set"

    echo "   K3s Channel:"
    grep '^k3s_channel:' group_vars/all/infrastructure.yml | awk '{print "     " $2}' || echo "     Not set"

    echo "   Cluster Token:"
    if grep -q '^k3s_token:.*[a-zA-Z0-9]' group_vars/all/infrastructure.yml; then
        echo "     Set (value hidden for security)"
    else
        echo "     Not set (will be auto-generated)"
    fi

    echo "   Custom Registries:"
    grep '^custom_registries:' group_vars/all/infrastructure.yml | awk '{print "     " $2}' || echo "     false"

    echo "   HTTP Proxy:"
    if grep -q '^proxy_env:' group_vars/all/infrastructure.yml; then
        echo "     Configured"
    else
        echo "     Not configured"
    fi

    echo "   etcd Snapshots:"
    if grep -q 'etcd-snapshot' group_vars/all/infrastructure.yml; then
        echo "     Enabled"
        grep 'etcd-snapshot-schedule-cron' group_vars/all/infrastructure.yml | sed 's/^/     /' || true
        grep 'etcd-snapshot-retention' group_vars/all/infrastructure.yml | sed 's/^/     /' || true
    else
        echo "     Not configured"
    fi
    echo ""
else
    echo "   WARNING: infrastructure.yml not found"
    echo ""
fi

echo "3. CNI CONFIGURATION"
echo "   Location: group_vars/all/cni.yml"
echo ""

if [[ -f "group_vars/all/cni.yml" ]]; then
    echo "   Current CNI: Flannel (K3s default)"
    echo "   Future options configured in cni.yml"
    echo ""
else
    echo "   Using defaults (Flannel)"
    echo ""
fi

echo "4. APPLICATION CONFIGURATION"
echo "   Location: group_vars/all/application.yml"
echo ""

if [[ -f "group_vars/all/application.yml" ]]; then
    echo "   Tenant Namespace:"
    grep '^tenant_namespace:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     basilica-system (default)"

    echo "   Operator Image:"
    grep '^operator_image:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     Not set"

    echo "   API Image:"
    grep '^api_image:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     Not set"

    echo "   Use Templates:"
    grep '^use_templates:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     true (default)"

    echo "   Generate CRDs:"
    grep '^generate_crds:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     true (default)"

    echo "   Install Rust:"
    grep '^install_rust:' group_vars/all/application.yml | awk '{print "     " $2}' || echo "     true (default)"

    echo "   Kubeconfig Mode:"
    grep 'write-kubeconfig-mode' group_vars/all/application.yml | grep -o '[0-9]\+' | sed 's/^/     /' || echo "     644 (default)"
    echo ""
else
    echo "   WARNING: application.yml not found"
    echo ""
fi

echo "5. AVAILABLE PLAYBOOKS"
echo "   Location: playbooks/"
echo ""

if [[ -d "playbooks" ]]; then
    echo "   Infrastructure:"
    echo "     - k3s-setup.yml          (Full K3s cluster setup)"
    echo "     - k3s-reset.yml          (Complete cluster cleanup)"
    echo "     - k3s-verify.yml         (Cluster health verification)"
    echo "     - preflight-check.yml    (Pre-installation validation)"
    echo "     - diagnose.yml           (Troubleshooting diagnostics)"
    echo "     - get-kubeconfig.yml     (Fetch kubeconfig from server)"
    echo ""
    echo "   Application:"
    echo "     - e2e-apply.yml          (Deploy Basilica services)"
    echo "     - e2e-teardown.yml       (Remove Basilica services)"
    echo "     - deploy-affine.yml      (Deploy AFFINE evaluation)"
    echo ""
    echo "   Subtensor:"
    echo "     - subtensor-up.yml       (Start local Subtensor chain)"
    echo "     - subtensor-down.yml     (Stop local Subtensor chain)"
    echo ""
else
    echo "   WARNING: playbooks/ directory not found"
    echo ""
fi

echo "6. HELPER SCRIPTS"
echo "   Location: scripts/"
echo ""

if [[ -d "scripts" ]]; then
    echo "     00-install-ansible.sh    (Install Ansible and Python deps)"
    echo "     01-dependencies.sh       (Install Ansible Galaxy collections)"
    echo "     02-configs.sh            (Generate configuration files)"
    echo "     03-provision.sh          (Provision K3s cluster)"
    echo "     04-show-config.sh        (This script)"
    echo ""
else
    echo "   WARNING: scripts/ directory not found"
    echo ""
fi

echo "7. ANSIBLE CONFIGURATION"
echo "   Location: ansible.cfg"
echo ""

if [[ -f "ansible.cfg" ]]; then
    echo "   Forks (parallelism):"
    grep '^forks' ansible.cfg | awk '{print "     " $3}' || echo "     5 (default)"

    echo "   Gathering:"
    grep '^gathering' ansible.cfg | awk '{print "     " $3}' || echo "     implicit (default)"

    echo "   Fact Caching:"
    grep '^fact_caching' ansible.cfg | awk '{print "     " $3}' || echo "     memory (default)"

    echo "   SSH Pipelining:"
    grep '^pipelining' ansible.cfg | awk '{print "     " $3}' || echo "     False (default)"
    echo ""
else
    echo "   Using Ansible defaults"
    echo ""
fi

echo "8. ENVIRONMENT VARIABLE OVERRIDES"
echo ""
echo "   You can override any configuration with environment variables:"
echo "   Pattern: BASILICA_<SERVICE>_<SECTION>_<KEY>"
echo ""
echo "   Examples:"
echo "     export INVENTORY=inventories/development.ini"
echo "     export PLAYBOOK=playbooks/k3s-setup.yml"
echo "     export TAGS=prepare,server"
echo "     export EXTRA_ARGS=\"--check\""
echo ""

echo "9. QUICK START COMMANDS"
echo ""
echo "   Pre-flight check:"
echo "     ./scripts/03-provision.sh PLAYBOOK=playbooks/preflight-check.yml"
echo ""
echo "   Provision K3s cluster:"
echo "     ./scripts/03-provision.sh"
echo ""
echo "   Deploy Basilica application:"
echo "     ansible-playbook -i $INVENTORY playbooks/e2e-apply.yml"
echo ""
echo "   Verify cluster health:"
echo "     ansible-playbook -i $INVENTORY playbooks/k3s-verify.yml"
echo ""
echo "   Run diagnostics:"
echo "     ansible-playbook -i $INVENTORY playbooks/diagnose.yml"
echo ""
echo "   Get kubeconfig:"
echo "     ansible-playbook -i $INVENTORY playbooks/get-kubeconfig.yml"
echo ""

echo "10. DOCUMENTATION"
echo ""
echo "     README.md              (Main documentation)"
echo "     UPGRADING.md           (Upgrade procedures)"
echo "     K9S-GUIDE.md           (K9s cluster management)"
echo "     KUBECONFIG-SETUP.md    (Kubeconfig access guide)"
echo "     README-SECURE-SETUP.md (Security hardening guide)"
echo ""

echo "==============================================="
echo "Configuration discovery complete"
echo "==============================================="
