# Basilica K3s Ansible Deployment

Ansible playbooks to provision production-grade K3s clusters and deploy Basilica E2E readiness manifests.

## Deployment Architecture

### Component Deployment Paths

Basilica uses a **hybrid deployment architecture** with components distributed across K3s and AWS ECS:

#### K3s Cluster (Deployed via Ansible)

**What this playbook deploys:**

- ✅ **Basilica Operator** - Custom Kubernetes operator for GPU scheduling
- ✅ **PostgreSQL** - Database for billing and payments services
- ✅ **Validator Service** - Bittensor neuron for network validation
- ✅ **Miner Services** - GPU executor fleet management
- ✅ **Optional**: Prometheus/Grafana monitoring stack
- ✅ **Optional**: Subtensor dev chain (local testing)

**Deployment command:**

```bash
ansible-playbook -i inventories/production.ini playbook.yml
```

### API Deployment Configuration

The `deploy_api_in_cluster` flag in `group_vars/all/application.yml` controls API deployment:

| Setting | Use Case | Deployment Method |
|---------|----------|-------------------|
| `false` (default) | **Production** | AWS ECS via Terraform |
| `true` | **Development/Testing** | K3s via Ansible |

**Important:** Keep `deploy_api_in_cluster: false` for production to avoid conflicts with ECS deployment.

### Why Separate API Deployment?

The API is deployed separately to ECS for several reasons:

1. **High Availability** - Multi-AZ deployment with auto-scaling
2. **Independent Scaling** - API traffic patterns differ from K3s workloads
3. **Security** - External ALB provides SSL termination and WAF integration

### Quick Start

**Full Stack Deployment:**

```bash
# Deploy K3s infrastructure + Basilica services (excluding API)
cd scripts/ansible
ansible-playbook -i inventories/production.ini playbook.yml
```

## Infrastructure Requirements

### Control Machine (where you run Ansible)

**Operating System**:

- Linux (recommended: Ubuntu 20.04+, Debian 11+, RHEL 8+)
- macOS 11+ (Big Sur or later)
- WSL2 on Windows

**Software**:

- Ansible 2.11 or higher
- Python 3.8 or higher
- SSH client
- Git (for repository access)

**Network**:

- SSH access to all target nodes (port 22)
- Internet access for downloading K3s binaries and Ansible collections

### Target Nodes (K3s cluster hosts)

#### Minimum Requirements (Single-Node Development)

**Hardware**:

- 2 CPU cores
- 2GB RAM
- 20GB free disk space on root filesystem
- x86_64, arm64, or armhf architecture

**Operating System**:

- Ubuntu 20.04 LTS or higher (focal, jammy, noble)
- Kernel 5.4 or higher

**Software**:

- Python 3.8 or higher
- systemd init system
- iptables or nftables

**Network**:

- Static IP address or DHCP reservation
- Hostname properly configured
- Internet access for downloading K3s binaries (unless air-gapped)

#### Recommended Requirements (Production Single-Node)

**Hardware**:

- 4 CPU cores
- 8GB RAM
- 100GB SSD storage
- 1 Gbps network interface

**Operating System**:

- Ubuntu 22.04 LTS (jammy) or Ubuntu 24.04 LTS (noble)
- Kernel 6.1 or higher

#### High-Availability Multi-Node Cluster

**Server Nodes (Control Plane)** - Minimum 3 nodes:

- 4 CPU cores per node
- 8GB RAM per node
- 100GB SSD storage per node
- Low-latency network between servers (<10ms RTT)
- Odd number of servers (3, 5, or 7 for etcd quorum)

**Agent Nodes (Workers)** - Variable count:

- 2 CPU cores per node (minimum)
- 4GB RAM per node (minimum)
- 50GB storage per node (minimum)
- Varies based on workload requirements

#### Network Requirements

**Required Ports (Server Nodes)**:

- 6443/tcp - K3s API server
- 10250/tcp - kubelet metrics
- 2379-2380/tcp - etcd (HA clusters only, server-to-server)
- 8472/udp - Flannel VXLAN (if using Flannel CNI)
- 51820/udp - Flannel WireGuard (if using WireGuard backend)
- 51821/udp - Flannel WireGuard IPv6

**Required Ports (Agent Nodes)**:

- 10250/tcp - kubelet metrics
- 8472/udp - Flannel VXLAN
- 51820/udp - Flannel WireGuard
- 51821/udp - Flannel WireGuard IPv6

**Optional Ports (Basilica Services)**:

- 8080/tcp - Basilica API
- 5432/tcp - PostgreSQL (if exposed)
- 9090/tcp - Prometheus
- 3000/tcp - Grafana
- 80/tcp, 443/tcp - Envoy ingress (if deployed)

**Internet Access** (unless air-gapped):

- github.com (K3s binary downloads)
- ghcr.io (GitHub Container Registry for images)
- docker.io (Docker Hub for base images)
- registry.k8s.io (Kubernetes official images)

#### Storage Requirements

**Server Nodes**:

- `/var/lib/rancher/k3s` - K3s data directory (50GB+ for production)
- `/var/lib/rancher/k3s/server/db/snapshots` - etcd snapshots (10GB+)
- `/var/log` - System and K3s logs (10GB+)

**Agent Nodes**:

- `/var/lib/rancher/k3s` - K3s agent data (20GB+)
- `/var/lib/kubelet` - Container runtime storage (varies by workload)

**Filesystem**:

- ext4 or xfs recommended
- Avoid NFS for `/var/lib/rancher/k3s` (performance issues)
- SSD strongly recommended for etcd performance

#### Permissions

**SSH Access**:

- Non-root user with sudo privileges (recommended)
- SSH key-based authentication (password-less sudo recommended)
- User must be in sudoers group

**Example sudoers configuration**:

```
ubuntu ALL=(ALL) NOPASSWD:ALL
```

#### Kernel Parameters

The playbooks automatically configure required kernel parameters via `prereq` role:

- `net.ipv4.ip_forward = 1`
- `net.bridge.bridge-nf-call-iptables = 1`
- `net.bridge.bridge-nf-call-ip6tables = 1`

Manual verification:

```bash
sysctl net.ipv4.ip_forward
sysctl net.bridge.bridge-nf-call-iptables
```

#### Package Requirements

Automatically installed by playbooks via `prereq` role:

- curl
- wget
- tar
- iptables
- bridge-utils (Ubuntu: `bridge-utils`, not `brctl`)
- python3
- python3-pip

#### Security Considerations

**Firewall**:

- Configure firewall rules for required ports
- Restrict access to K3s API (port 6443) to authorized IPs
- Use security groups in cloud environments

**SELinux/AppArmor**:

- K3s works with both enabled (recommended)
- Playbooks do not disable security modules

**Time Synchronization**:

- NTP or systemd-timesyncd must be configured
- Critical for certificate validation and etcd consensus

## Quick Start

### Prerequisites

Before running playbooks, ensure your infrastructure meets the requirements above. Use the pre-flight validation playbook:

```bash
ansible-playbook -i inventories/production.ini playbooks/01-setup/preflight-check.yml
```

**Control Machine**:

- Ansible 2.11 or higher
- SSH access to target hosts
- Python 3.8+ on control machine and target hosts

### Installation

1. Install Ansible and dependencies:

   ```bash
   ./scripts/00-install-ansible.sh
   ./scripts/01-dependencies.sh
   ```

2. Configure inventory:

   ```bash
   ./scripts/02-configs.sh
   # Edit inventories/production.ini with your hosts
   ```

3. Configure variables:

   ```bash
   # Edit group_vars/all/*.yml files as needed
   ```

### Basic Deployment

**Complete deployment (recommended)**:

```bash
# Full end-to-end deployment: setup → deploy → verify
ansible-playbook -i inventories/production.ini playbooks/site.yml
```

**OR: Step-by-step deployment**:

Setup K3s cluster:

```bash
ansible-playbook -i inventories/production.ini playbooks/01-setup/cluster.yml
```

Deploy Basilica application stack:

```bash
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml
```

Reset cluster (complete cleanup):

```bash
ansible-playbook -i inventories/production.ini playbooks/05-teardown/cluster.yml
```

## Architecture

### Role Structure

The Ansible deployment follows SOLID principles with modular, composable roles:

**Infrastructure Roles** (Foundation):

- `prereq` - System prerequisites (sysctl, packages, timezone)
- `download` - K3s binary download with SHA256 verification (multi-arch: x86_64, arm64, armhf)
- `k3s_custom_registries` - Private registry configuration (air-gapped deployments)
- `reset` - Complete cluster cleanup with recursive unmounting

**Cluster Roles** (K3s Installation):

- `k3s_server` - K3s server with HA support and token management
- `k3s_agent` - K3s agent with PXE detection, server reachability checks
- `k3s_server_post` - Post-installation (CNI/load balancer placeholder for future)

**Application Roles** (Basilica-specific):

- `control_machine_setup` - Control machine prerequisites (Rust toolchain for CRD generation)
- `basilica-storage` - R2 storage credentials and RBAC
- `subtensor_dev` - Local Subtensor blockchain

### Role Dependencies

```
k3s-setup.yml
├── prereq (system preparation)
├── download (K3s binary with checksum verification)
├── k3s_custom_registries (optional)
├── k3s_server
│   └── depends: prereq, download, k3s_custom_registries
├── k3s_agent
│   └── depends: prereq, download, k3s_custom_registries
└── k3s_server_post
    └── depends: k3s_server
```

### Configuration Layers

Ansible automatically loads and merges configuration from multiple files:

1. **role defaults** - Built-in defaults
2. **group_vars/all/*.yml** - Global configuration (automatically loaded):
   - `infrastructure.yml` - K3s infrastructure config (versions, proxy, registries)
   - `cni.yml` - CNI configuration (Flannel, Calico, Cilium)
   - `loadbalancer.yml` - Load balancer config (MetalLB, kube-vip)
   - `application.yml` - Basilica application config
3. **inventory host_vars** - Host-specific overrides
4. **playbook vars** - Runtime variables
5. **command-line -e** - Highest priority

### Playbook Flow

**k3s-setup.yml** (Phased approach):

1. Verify Ansible version (>= 2.11)
2. Prepare all nodes (prereq, download, registries)
3. Setup K3s servers with HA support
4. Setup K3s agents (if defined)
5. Post-configuration (wait for API ready)
6. Fetch kubeconfig to control machine

**e2e-apply.yml** (Application deployment):

- 10-phase orchestration (RBAC → CRDs → Postgres → Operator → API → Envoy → etc.)
- See existing README section below for details

## Advanced Features

### Multi-Node HA Cluster

Edit `inventories/production.ini`:

```ini
[k3s_cluster:children]
k3s_server
k3s_agents

[k3s_server]
server1 ansible_host=192.168.30.10
server2 ansible_host=192.168.30.11
server3 ansible_host=192.168.30.12

[k3s_agents]
agent1 ansible_host=192.168.30.20
agent2 ansible_host=192.168.30.21
```

Edit `group_vars/all/infrastructure.yml`:

```yaml
k3s_token: "your-secure-token-here"
```

### Custom Registry (Air-Gapped)

Edit `group_vars/all/infrastructure.yml`:

```yaml
custom_registries: true
custom_registries_yaml: |
  mirrors:
    docker.io:
      endpoint:
        - "https://registry.example.com/v2/dockerhub"
```

### HTTP Proxy Support

Edit `group_vars/all/infrastructure.yml`:

```yaml
proxy_env:
  HTTP_PROXY: http://proxy.example.com:3128
  HTTPS_PROXY: http://proxy.example.com:3128
  NO_PROXY: "*.local,127.0.0.0/8,10.0.0.0/8"
```

### Future CNI Options

Basilica currently uses Flannel (K3s default). Future support for:

- Calico (edit `group_vars/all/cni.yml`)
- Cilium with Hubble (edit `group_vars/all/cni.yml`)

### Future Load Balancer Options

Future support for:

- MetalLB (Layer 2 / BGP)
- kube-vip (VIP for HA API server)

## Use CI-Built k3_test Images

Build and push images from your branch (tags all services with k3_test):

```bash
just ci-build-images TAG=k3_test
```

Deploy using the k3_test images (override defaults):

```bash
ansible-playbook -i inventories/example.ini playbooks/02-deploy/basilica.yml \
  -e operator_image=ghcr.io/one-covenant/basilica-operator:k3_test \
  -e api_image=ghcr.io/one-covenant/basilica-api:k3_test
```

## Configuration Variables

Key variables in `group_vars/all/application.yml`:

- `tenant_namespace` (default: `basilica-system`)
- `operator_image`, `api_image` - Image references
- `use_templates: true` - Inject image refs/env via templates
- `generate_crds: true` - Requires Rust on control machine to run crdgen
- `install_rust_on_control: true` - Install Rust toolchain on control machine
- Port-forward options for API, Postgres, Envoy

Key variables in `group_vars/all/infrastructure.yml`:

- `k3s_version` (default: `v1.31.1+k3s1`)
- `k3s_channel` (default: `stable`)
- `k3s_token` - Cluster join token for HA
- `custom_registries` - Enable private registries

## Idempotent Redeployments

All playbooks are fully idempotent and safe to run multiple times. No teardown occurs unless explicitly requested.

### Quick Redeployment Commands

Use `./redeploy.sh` for targeted, idempotent updates:

```bash
# Redeploy operator only (fastest - ~30s)
./redeploy.sh operator

# Redeploy all Basilica services (~2-3 min)
./redeploy.sh services

# Full stack deployment (~5-10 min, idempotent)
./redeploy.sh full

# Dry run before applying changes
./redeploy.sh operator --check
```

### Direct Ansible Invocation

```bash
# These are safe to run repeatedly
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml
ansible-playbook -i inventories/production.ini playbook.yml --skip-tags verify
ansible-playbook -i inventories/production.ini playbook.yml --tags deploy
```

## Troubleshooting

### Check K3s server status

```bash
ansible -i inventories/production.ini k3s_server -m shell -a "systemctl status k3s"
```

### View K3s logs

```bash
ansible -i inventories/production.ini k3s_server -m shell -a "journalctl -u k3s -n 100"
```

### Verify cluster nodes

```bash
ansible -i inventories/production.ini k3s_server[0] -m shell -a "kubectl get nodes"
```

### Test application health

```bash
ansible-playbook -i inventories/production.ini playbooks/03-verify/api-status.yml
```

### Run comprehensive diagnostics

```bash
ansible-playbook -i inventories/production.ini playbooks/03-verify/diagnose.yml
```

## Tie-in with docs/e2e-readiness-checklist.md

The `playbooks/02-deploy/basilica.yml` automates the checklist steps: namespaces/RBAC → CRDs → Postgres → Operator/API → optional Envoy/Gateway → smoke probe.

After the run completes, verify:

- Operator: `kubectl -n basilica-system logs deploy/basilica-operator | head`
- API health (ephemeral probe runs during the play): `curl http://127.0.0.1:8000/health`
- Interactive cluster management: Use k9s (see K9S-GUIDE.md or run `./scripts/ansible/scripts/07-k9s-basilica.sh`)

## Contents

### Playbooks

**See `playbooks/README.md` for comprehensive documentation of all playbooks.**

**Master Playbooks:**

- `playbooks/site.yml` - Complete end-to-end deployment (setup → deploy → verify)
- `playbooks/setup.yml` - Infrastructure setup lifecycle
- `playbooks/deploy.yml` - Application deployment lifecycle
- `playbooks/verify.yml` - Health check and validation lifecycle
- `playbooks/teardown.yml` - Complete teardown lifecycle

**Organized by Lifecycle:**

- `playbooks/01-setup/` - Cluster provisioning (k3s-cluster, control-machine)
- `playbooks/02-deploy/` - Application deployment (basilica, images-local)
- `playbooks/03-verify/` - Health checks (cluster-health, api-status, diagnose)
- `playbooks/04-maintain/` - Maintenance tasks (kubeconfig)
- `playbooks/05-teardown/` - Cleanup (basilica, cluster)

**Other Playbooks:**

- `playbooks/subtensor-up.yml` / `subtensor-down.yml` - Local Subtensor chain

### Roles

- `roles/prereq` - System prerequisites and pre-flight validation
- `roles/download` - K3s binary download with verification
- `roles/k3s_custom_registries` - Private registry configuration
- `roles/k3s_server` - K3s server installation
- `roles/k3s_agent` - K3s agent installation
- `roles/k3s_server_post` - Post-installation configuration
- `roles/reset` - Complete cluster cleanup
- `roles/basilica-storage` - R2 storage setup
- `roles/subtensor_dev` - Subtensor blockchain

### Configuration

- `group_vars/all.yml` - Main entry point
- `group_vars/all/infrastructure.yml` - K3s infrastructure config
- `group_vars/all/cni.yml` - CNI configuration
- `group_vars/all/loadbalancer.yml` - Load balancer config
- `group_vars/all/application.yml` - Basilica application config
- `inventories/example.ini` - Sample inventory
- `inventories/production.ini` - Production inventory

### Helper Scripts

- `scripts/00-install-ansible.sh` - Install Ansible and dependencies
- `scripts/01-dependencies.sh` - Install Ansible Galaxy collections
- `scripts/02-configs.sh` - Generate configuration files
- `scripts/03-provision.sh` - Provision K3s cluster

## Notes

### CRD Generation

By default the playbook expects you to have Rust on your control machine to run `cargo run -p basilica-operator --bin crdgen`. Set `generate_crds=false` if you provide a pre-generated `basilica-crds.yaml` at repo root, or adjust `crdgen_cmd`.

### Control Machine Prerequisites

Set `install_rust_on_control: true` in `group_vars/all/application.yml` to automatically install the Rust toolchain on your control machine (where Ansible runs). Alternatively, run the control machine setup playbook manually:

```bash
ansible-playbook playbooks/01-setup/control-machine.yml
```

### Port Forwarding

- Ephemeral probe: Set `run_smoke_probe: true` (default) - port-forward runs only long enough to probe
- Long-lived forwards: Enable in `group_vars/all/application.yml`:
  - `port_forward.enabled: true` - API service
  - `envoy_forward.enabled: true` - Envoy proxy (8080)
  - `envoy_admin_forward.enabled: true` - Envoy admin (9901)
- Forwards bind to `127.0.0.1` by default; use SSH tunnels or change `bind_address` if necessary
- Access from remote: `ssh -N -L 8000:127.0.0.1:8000 user@server` then `curl http://localhost:8000/health`

### Manifest Management

The playbook copies the repo's `config/` directory to the server under `/opt/basilica/config` and applies manifests from there. K3s installs `kubectl` on the server (`/usr/local/bin/kubectl`), so manifests are applied on the server host.

### Templates

To template image refs/env instead of inline `replace`, set `use_templates: true` (default). Templates live under `scripts/ansible/templates/`.
