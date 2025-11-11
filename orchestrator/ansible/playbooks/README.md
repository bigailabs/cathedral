# Basilica K3s Ansible Playbooks

Comprehensive collection of Ansible playbooks for deploying and managing the Basilica GPU rental platform on K3s clusters.

## Table of Contents

- [Overview](#overview)
- [Directory Structure](#directory-structure)
- [Quick Start](#quick-start)
- [Playbook Reference](#playbook-reference)
- [Common Workflows](#common-workflows)
- [Variables](#variables)
- [Examples](#examples)

## Overview

The playbooks are organized by lifecycle stage for clarity and maintainability:

- **01-setup**: Infrastructure provisioning and cluster installation
- **02-deploy**: Application deployment
- **03-verify**: Health checks and validation
- **04-maintain**: Operational maintenance tasks
- **05-teardown**: Cleanup and resource destruction

Master playbooks (`setup.yml`, `deploy.yml`, `verify.yml`, `teardown.yml`, `site.yml`) compose individual playbooks for common workflows.

## Directory Structure

```
playbooks/
├── README.md                    # This file
├── site.yml                     # Master orchestration (setup → deploy → verify)
├── setup.yml                    # Setup lifecycle master
├── deploy.yml                   # Deploy lifecycle master
├── verify.yml                   # Verify lifecycle master
├── teardown.yml                 # Teardown lifecycle master
│
├── 01-setup/
│   └── k3s-cluster.yml         # Install K3s cluster with HA support
│
├── 02-deploy/
│   └── basilica.yml            # Deploy complete Basilica stack
│
├── 03-verify/
│   ├── cluster-health.yml      # Basic K3s cluster health check
│   └── diagnose.yml            # Comprehensive cluster diagnostics
│
├── 04-maintain/
│   └── kubeconfig.yml          # Fetch kubeconfig for local kubectl access
│
├── 05-teardown/
│   ├── basilica.yml            # Remove Basilica services from cluster
│   └── cluster.yml             # Complete K3s cluster removal
│
└── archived/
    ├── affine.yml              # AFINE service deployment - removed from main flow
    ├── fetch-kubeconfig.yml    # Deprecated: Use 04-maintain/kubeconfig.yml
    ├── get-kubeconfig.yml      # Deprecated: Use 04-maintain/kubeconfig.yml
    ├── preflight-check.yml     # Moved to roles/prereq/tasks/validation.yml
    └── e2e-apply-token-fetch.yml # Incomplete template
```

## Quick Start

### Full Deployment (Setup → Deploy → Verify)

```bash
cd scripts/ansible
ansible-playbook -i inventories/production.ini playbooks/site.yml
```

### Setup Only (New Cluster)

```bash
ansible-playbook -i inventories/production.ini playbooks/setup.yml
```

### Deploy Only (Existing Cluster)

```bash
ansible-playbook -i inventories/production.ini playbooks/deploy.yml
```

### Verify Deployment

```bash
ansible-playbook -i inventories/production.ini playbooks/verify.yml
```

### Complete Teardown

```bash
ansible-playbook -i inventories/production.ini playbooks/teardown.yml
```

## Playbook Reference

### Master Playbooks

#### `site.yml`
Complete end-to-end deployment orchestration.

**Usage:**
```bash
# Full deployment
ansible-playbook -i inventories/production.ini playbooks/site.yml

# Skip verification
ansible-playbook -i inventories/production.ini playbooks/site.yml --skip-tags verify

# Dry run
ansible-playbook -i inventories/production.ini playbooks/site.yml --check
```

**Executes:**
1. `setup.yml` - Infrastructure provisioning
2. `deploy.yml` - Application deployment
3. `verify.yml` - Health validation

---

#### `setup.yml`
Infrastructure setup and cluster provisioning.

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/setup.yml
```

**Executes:**
1. `01-setup/k3s-cluster.yml` - Install K3s cluster (includes pre-flight validation via prereq role)

**Duration:** ~10-15 minutes

**Note:** Pre-flight validation tasks are now integrated into the `prereq` role and run automatically during cluster setup.

---

#### `deploy.yml`
Application deployment to existing cluster.

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/deploy.yml
```

**Executes:**
1. `02-deploy/basilica.yml` - Main Basilica stack

**Duration:** ~15-20 minutes

---

#### `verify.yml`
Health checks and validation.

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/verify.yml
```

**Executes:**
1. `03-verify/cluster-health.yml` - K3s cluster status

**Duration:** ~1 minute

---

#### `teardown.yml`
Complete infrastructure teardown.

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/teardown.yml
```

**Executes:**
1. `05-teardown/basilica.yml` - Remove Basilica services
2. `05-teardown/cluster.yml` - Remove K3s cluster

**Duration:** ~5-10 minutes

---

### Setup Playbooks

#### `01-setup/k3s-cluster.yml`
Production-grade K3s cluster installation with HA support and integrated pre-flight validation.

**Features:**
- Pre-flight validation via prereq role (OS, CPU, memory, disk, network checks)
- Multi-node HA cluster support
- Multi-arch support (x86_64, arm64, armhf)
- SHA256 binary verification
- Custom registry support (air-gapped deployments)

**Usage:**
```bash
# Install cluster with validation
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml

# Skip validation
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml --skip-tags validation

# Specific phase only
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml --tags server
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml --tags agent
```

**Tags:** `validation`, `prereq`, `prepare`, `server`, `agent`, `post`, `kubeconfig`

**Note:** Pre-flight validation tasks are now part of the `prereq` role (see `roles/prereq/tasks/validation.yml`). To disable validation, set `run_validation: false` or use `--skip-tags validation`.

---

### Deploy Playbooks

#### `02-deploy/basilica.yml`
Deploys complete Basilica application stack to K3s.

**Deploys:**
- Namespaces (basilica-system, basilica-validators, basilica-system)
- Custom Resource Definitions (pre-generated from `k8s/crds/basilica-crds.yaml`)
- Basilica Operator
- Envoy forward proxy
- Alloy telemetry
- Disk cleanup CronJob

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml
```

**Configuration:** See `group_vars/all/application.yml`

**Note:** CRDs are pre-generated and version-controlled. Dynamic generation has been removed.

---

### Verify Playbooks

#### `03-verify/cluster-health.yml`
Basic K3s cluster health check.

**Displays:**
- K3s service status (systemd)
- Cluster nodes
- Namespaces
- Pods in all namespaces

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml
```

---

#### `03-verify/diagnose.yml`
Comprehensive cluster diagnostics.

**Gathers:**
- System information
- K3s binary and version
- Service status and logs
- Cluster resources
- Network connectivity
- DNS resolution
- System resources

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/03-verify/diagnose.yml
```

---

### Maintain Playbooks

#### `04-maintain/kubeconfig.yml`
Fetches K3s kubeconfig for local kubectl access.

**Options:**
- Destination: `home` (default: `~/.kube/k3s-basilica-config`) or `build` (`repo_root/build/k3s.yaml`)
- Public IP detection: Auto-detect server public IP (default: enabled)

**Usage:**
```bash
# Fetch to home directory (default)
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml

# Fetch to build directory
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml \
  -e kubeconfig_dest=build

# Use inventory IP instead of public IP detection
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml \
  -e kubeconfig_public_ip=false
```

**Features:**
- Auto-detects public IP via `ifconfig.me`
- Tests kubectl connectivity
- Provides clear usage instructions
- Troubleshooting guidance if connection fails

---

### Teardown Playbooks

#### `05-teardown/basilica.yml`
Removes all Basilica services from K3s cluster.

**Deletes:**
- ServiceMonitors
- Prometheus + Grafana
- API/Operator/Validator deployments
- PostgreSQL
- Envoy proxy
- Gateway API manifests
- RBAC resources
- Custom Resource instances
- CRDs
- Namespaces

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/05-teardown/basilica.yml
```

---

#### `05-teardown/cluster.yml`
Complete K3s cluster removal with recursive unmounting.

**Removes:**
- K3s server and agent processes
- K3s binaries
- Configuration files
- Container images
- Network interfaces

**Usage:**
```bash
ansible-playbook -i inventories/production.ini playbooks/05-teardown/cluster.yml
```

**Warning:** This is destructive and irreversible.

---

## Common Workflows

### New Deployment (Fresh Infrastructure)

```bash
cd scripts/ansible

# 1. Install K3s cluster (includes pre-flight validation)
ansible-playbook -i inventories/production.ini playbooks/01-setup/k3s-cluster.yml

# 2. Deploy Basilica
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml

# 3. Verify deployment
ansible-playbook -i inventories/production.ini playbooks/verify.yml

# 4. Fetch kubeconfig
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml
```

### OR: Use Master Playbook

```bash
# Complete deployment in one command
ansible-playbook -i inventories/production.ini playbooks/site.yml

# Fetch kubeconfig
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml
```

---

### Update Deployment (Existing Cluster)

```bash
# Update Basilica stack only
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml

# Verify
ansible-playbook -i inventories/production.ini playbooks/verify.yml
```

---

### Troubleshooting

```bash
# Cluster health check
ansible-playbook -i inventories/production.ini playbooks/03-verify/cluster-health.yml

# Comprehensive diagnostics
ansible-playbook -i inventories/production.ini playbooks/03-verify/diagnose.yml
```

---

### Complete Teardown

```bash
# Remove Basilica services
ansible-playbook -i inventories/production.ini playbooks/05-teardown/basilica.yml

# Remove K3s cluster
ansible-playbook -i inventories/production.ini playbooks/05-teardown/cluster.yml
```

### OR: Use Master Playbook

```bash
ansible-playbook -i inventories/production.ini playbooks/teardown.yml
```

---

## Variables

### Key Configuration Files

- `group_vars/all.yml` - Main entry point
- `group_vars/all/application.yml` - Basilica application config
- `group_vars/all/infrastructure.yml` - K3s cluster config
- `group_vars/all/r2.yml` - R2 storage credentials
- `group_vars/all/vault.yml` - Encrypted secrets

### Important Variables

#### Application Config (`group_vars/all/application.yml`)

```yaml
# Namespaces
tenant_namespace: basilica-system

# Images
operator_image: ghcr.io/one-covenant/basilica-operator:latest
operator_image_pull_policy: Always

# Toggles
install_envoy_forward_proxy: true
install_gateway_api: true
```

#### Infrastructure Config (`group_vars/all/infrastructure.yml`)

```yaml
# K3s Version
k3s_version: v1.31.1+k3s1
k3s_channel: stable

# K3s Cluster
k3s_token: <cluster-join-token>
k3s_disable_traefik: true

# Custom Registries (air-gapped)
custom_registries: false
```

### Variable Overrides

Override variables via command line:

```bash
# Deploy with different operator image
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml \
  -e operator_image=ghcr.io/myorg/basilica-operator:v0.5.0

# Disable Envoy forward proxy
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml \
  -e install_envoy_forward_proxy=false
```

---

## Examples

### Production Deployment

```bash
# Full production deployment with verification
ansible-playbook -i inventories/production.ini playbooks/site.yml

# Fetch kubeconfig
ansible-playbook -i inventories/production.ini playbooks/04-maintain/kubeconfig.yml

# Test from control machine
export KUBECONFIG=~/.kube/k3s-basilica-config
kubectl get nodes
kubectl get pods -A
```

### Partial Deployment (Operator Only)

```bash
# Deploy only operator and CRDs (skip API)
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml \
  --tags deploy_crds,deploy_operator
```

### Dry Run (Check Mode)

```bash
# Preview changes without applying
ansible-playbook -i inventories/production.ini playbooks/site.yml --check
```

### Verbose Output

```bash
# Debug playbook execution
ansible-playbook -i inventories/production.ini playbooks/site.yml -vvv
```

---

## Archived Playbooks

The following playbooks have been removed or deprecated:

- **`01-setup/control-machine.yml`** - DELETED (2025-01-06): CRDs are now pre-generated, Rust installation no longer required
- **`02-deploy/images-local.yml`** - DELETED (2025-01-06): Violated "never build locally" principle
- **`03-verify/api-status.yml`** - DELETED (2025-01-06): API deployed via Terraform/ECS, not K8s
- **`archived/fetch-kubeconfig.yml`** - Replaced by `04-maintain/kubeconfig.yml` with `kubeconfig_dest=build`
- **`archived/get-kubeconfig.yml`** - Replaced by `04-maintain/kubeconfig.yml` (default behavior)
- **`archived/e2e-apply-token-fetch.yml`** - Incomplete template, not integrated

Use `04-maintain/kubeconfig.yml` for all kubeconfig fetching needs.

---

## Support

For issues or questions:
- Review parent README: `scripts/ansible/README.md`
- Check troubleshooting playbook: `03-verify/diagnose.yml`
- GitHub Issues: https://github.com/one-covenant/basilica/issues
