# Basilica Cluster Manager

A modular Python CLI tool for diagnosing and managing K3s clusters with WireGuard VPN.

## Installation

```bash
# Install with uv (recommended)
cd orchestrator/cluster-manager
uv sync

# Run commands
uv run clustermgr --help
```

## Usage

```bash
# Check cluster health
uv run clustermgr health

# Run diagnostics
uv run clustermgr diagnose

# Resource utilization
uv run clustermgr resources              # Node resource usage
uv run clustermgr resources -n           # By namespace breakdown

# User deployments
uv run clustermgr deployments            # List all deployments
uv run clustermgr deployments -u user1   # Filter by user
uv run clustermgr deployments -s running # Filter by status
uv run clustermgr deployments -d         # Show pod details

# Cluster events
uv run clustermgr events                 # Recent events
uv run clustermgr events -w              # Warnings only
uv run clustermgr events -l 100          # Last 100 events

# WireGuard management
uv run clustermgr wg status              # Show WireGuard status
uv run clustermgr wg peers               # List peers with health metrics
uv run clustermgr wg restart             # Restart WireGuard service
uv run clustermgr wg reconcile           # Check pod CIDR reconciliation
uv run clustermgr wg reconcile --fix     # Fix missing pod CIDRs
uv run clustermgr wg keys                # Show key info for rotation planning
uv run clustermgr wg handshakes          # Check handshake ages

# Flannel VXLAN diagnostics (HTTP 503 troubleshooting)
uv run clustermgr flannel status         # Show flannel.1 interface status
uv run clustermgr flannel fdb            # Inspect FDB entries for VXLAN
uv run clustermgr flannel neighbors      # Check neighbor/ARP entries
uv run clustermgr flannel routes         # Verify pod CIDR routes
uv run clustermgr flannel test           # Test VXLAN connectivity to GPU nodes
uv run clustermgr flannel diagnose       # Comprehensive Flannel health check
uv run clustermgr flannel mac-duplicates # Check for duplicate VtepMACs
uv run clustermgr flannel capture        # Packet capture on flannel.1
uv run clustermgr flannel vxlan-capture  # Capture VXLAN traffic (UDP 8472)

# Network topology discovery
uv run clustermgr topology              # Full topology view
uv run clustermgr topology -f tree      # Tree view only
uv run clustermgr topology -f table     # Table view only
uv run clustermgr topology -f matrix    # Connection matrix only

# Firewall audit
uv run clustermgr firewall              # Audit iptables rules
uv run clustermgr firewall -p           # Check required ports
uv run clustermgr firewall -d           # Show DROP counters

# MTU validation
uv run clustermgr mtu                   # Check interface MTU
uv run clustermgr mtu -t 10.200.0.1     # Test path MTU

# Full mesh connectivity test
uv run clustermgr mesh-test             # Test all paths
uv run clustermgr mesh-test -m          # Show as matrix

# Latency measurement
uv run clustermgr latency-matrix        # Measure all paths
uv run clustermgr latency-matrix -t     # Detailed table

# UserDeployment troubleshooting
uv run clustermgr ud health             # Check all UserDeployments
uv run clustermgr ud inspect myapp -n u-alice  # Deep inspection
uv run clustermgr ud logs myapp -n u-alice     # Stream pod logs
uv run clustermgr ud events myapp -n u-alice   # Show K8s events
uv run clustermgr ud restart myapp -n u-alice  # Restart pods

# Gateway API troubleshooting
uv run clustermgr gateway routes        # List HTTPRoutes with status
uv run clustermgr gateway routes -u     # Show only unhealthy routes
uv run clustermgr gateway endpoints     # Show Envoy endpoints
uv run clustermgr gateway sync          # Check route sync with UserDeployments
uv run clustermgr gateway test ud-myapp -n u-alice  # Test route connectivity

# Envoy proxy diagnostics
uv run clustermgr envoy pods            # Show Envoy pod status
uv run clustermgr envoy test            # Test connectivity to user pods
uv run clustermgr envoy logs            # Show access logs
uv run clustermgr envoy logs -s 5       # Filter by 5xx errors
uv run clustermgr envoy path mypod -n u-alice  # Trace network path

# NetworkPolicy diagnostics
uv run clustermgr netpol audit          # Audit policies across namespaces
uv run clustermgr netpol coverage       # Check policy coverage
uv run clustermgr netpol details u-alice  # Show detailed policy config
uv run clustermgr netpol test u-alice   # Test DNS, egress, ingress

# Namespace management
uv run clustermgr namespace list        # List tenant namespaces
uv run clustermgr namespace list -d     # With resource counts
uv run clustermgr namespace audit u-alice  # Audit RBAC, policies, secrets
uv run clustermgr namespace resources u-alice  # Show all resources
uv run clustermgr namespace cleanup     # Find orphaned namespaces
uv run clustermgr namespace summary     # Summary statistics

# Node maintenance
uv run clustermgr maintenance status    # Show node maintenance status
uv run clustermgr maintenance cordon mynode    # Cordon a node
uv run clustermgr maintenance uncordon mynode  # Uncordon a node
uv run clustermgr maintenance drain mynode     # Drain node for maintenance
uv run clustermgr maintenance drain mynode -g 600  # With 10min grace period
uv run clustermgr maintenance rolling-restart -t server  # Rolling restart K3s servers
uv run clustermgr maintenance rolling-restart -t gpu     # Rolling restart GPU nodes
uv run clustermgr maintenance verify    # Post-maintenance verification

# Cluster scaling and capacity
uv run clustermgr scaling capacity      # Show current capacity metrics
uv run clustermgr scaling readiness     # Analyze scaling readiness
uv run clustermgr scaling limits        # Display architecture limits
uv run clustermgr scaling baselines     # Check performance baselines

# etcd cluster management
uv run clustermgr etcd health           # Check etcd cluster health
uv run clustermgr etcd status           # Show member status (DB size, raft)
uv run clustermgr etcd members          # List etcd cluster members
uv run clustermgr etcd defrag           # Defragment etcd database
uv run clustermgr etcd defrag -a        # Defrag all members
uv run clustermgr etcd alarms           # Check for active alarms
uv run clustermgr etcd compact          # Compact history to save space

# Pod troubleshooting
uv run clustermgr pod-troubleshoot -s           # Scan for issues
uv run clustermgr pod-troubleshoot mypod -n ns  # Diagnose specific pod
uv run clustermgr pod-troubleshoot mypod -l -e  # With logs and events

# FUSE daemon troubleshooting
uv run clustermgr fuse-troubleshoot             # Check all nodes
uv run clustermgr fuse-troubleshoot mynode      # Check specific node
uv run clustermgr fuse-troubleshoot mynode --fix-mounts  # Fix stale mounts

# Node pressure detection
uv run clustermgr node-pressure         # Check node conditions
uv run clustermgr node-pressure -m      # Include usage metrics

# Security audit
uv run clustermgr audit-pods            # Audit all pods
uv run clustermgr audit-pods -s critical # Critical issues only
uv run clustermgr audit-pods -e         # Exclude kube-system

# Certificate check
uv run clustermgr cert-check            # Check K3s certs
uv run clustermgr cert-check -s         # Include TLS secrets

# Read-only kubeconfig generation (NEW!)
uv run clustermgr kubeconfig generate --name prometheus-readonly
uv run clustermgr kubeconfig generate --name ci-reader --duration 2160h
uv run clustermgr kubeconfig list       # List ServiceAccounts
uv run clustermgr kubeconfig verify --kubeconfig-path ./kubeconfig-name.yaml
uv run clustermgr kubeconfig rotate --name prometheus-readonly
uv run clustermgr kubeconfig revoke --name old-account

# Fix issues (with safety guards)
uv run clustermgr fix --dry-run    # Preview changes
uv run clustermgr fix              # Execute with confirmation

# Cleanup failed pods
uv run clustermgr cleanup

# View logs
uv run clustermgr logs -n 100

# Diagnostic bundle for escalation
uv run clustermgr bundle               # Collect full diagnostic bundle
uv run clustermgr bundle -q            # Quick bundle (skip slow ops)
uv run clustermgr bundle -n u-alice    # Focus on specific namespace
```

## Commands

### Core Operations

| Command | Description |
|---------|-------------|
| `health` | Multi-layer cluster health check (nodes, WireGuard, iptables) |
| `diagnose` | Comprehensive diagnostics (502 errors, connectivity, dropped packets) |
| `resources` | Show cluster resource utilization (CPU, memory, GPU) |
| `deployments` | List user deployments with status and pod details |
| `events` | Show filtered cluster events for incident triage |

### Network - WireGuard

| Command | Description |
|---------|-------------|
| `wg status` | Show WireGuard status on all servers |
| `wg peers` | List WireGuard peers with health metrics |
| `wg restart` | Restart WireGuard service |
| `wg reconcile` | Check/fix pod CIDR in AllowedIPs for GPU nodes |
| `wg keys` | Show key info for rotation planning |
| `wg handshakes` | Check handshake ages across all peers |

### Network - Flannel VXLAN

| Command | Description |
|---------|-------------|
| `flannel status` | Show flannel.1 interface state, MAC, MTU, dropped packets |
| `flannel fdb` | Inspect FDB entries for VXLAN MAC-to-IP mappings |
| `flannel neighbors` | Check ARP/neighbor entries for VTEP IPs |
| `flannel routes` | Verify pod CIDR routes through flannel.1 |
| `flannel test` | Test VXLAN connectivity to GPU nodes via ping |
| `flannel diagnose` | Comprehensive Flannel health check |
| `flannel mac-duplicates` | Check for duplicate VtepMAC addresses |
| `flannel capture` | Capture packets on flannel.1 for debugging |
| `flannel vxlan-capture` | Capture VXLAN encapsulated traffic (UDP 8472) |

### Network - General

| Command | Description |
|---------|-------------|
| `topology` | Network topology discovery with WireGuard connectivity status |
| `firewall` | Audit iptables rules for potential issues |
| `mtu` | Validate MTU settings across network interfaces |
| `mesh-test` | Test full mesh WireGuard connectivity between all nodes |
| `latency-matrix` | Measure and display network latency between all nodes |

### Maintenance

| Command | Description |
|---------|-------------|
| `maintenance status` | Show node schedulability and maintenance state |
| `maintenance cordon` | Cordon a node to prevent new pod scheduling |
| `maintenance uncordon` | Uncordon a node to allow scheduling |
| `maintenance drain` | Drain a node by evicting all pods |
| `maintenance rolling-restart` | Rolling restart of server or GPU nodes |
| `maintenance verify` | Post-maintenance verification checks |

### Scaling

| Command | Description |
|---------|-------------|
| `scaling capacity` | Show current cluster capacity metrics |
| `scaling readiness` | Analyze scaling readiness and recommendations |
| `scaling limits` | Display architecture limits and thresholds |
| `scaling baselines` | Check performance baselines (latency, buffers) |

### etcd Cluster

| Command | Description |
|---------|-------------|
| `etcd health` | Check etcd cluster health and quorum |
| `etcd status` | Show member status (DB size, raft indices) |
| `etcd members` | List etcd cluster members |
| `etcd defrag` | Defragment etcd database to reclaim space |
| `etcd alarms` | Check for active etcd alarms (NOSPACE, etc.) |
| `etcd compact` | Compact history to remove old revisions |

### UserDeployment Management

| Command | Description |
|---------|-------------|
| `ud health` | Check health of all UserDeployments |
| `ud inspect` | Deep inspection of a UserDeployment |
| `ud logs` | Stream logs from UserDeployment pods |
| `ud events` | Show K8s events for a UserDeployment |
| `ud restart` | Restart pods for a UserDeployment |

### Gateway API

| Command | Description |
|---------|-------------|
| `gateway routes` | List HTTPRoutes with status and backend health |
| `gateway endpoints` | Show Envoy endpoints and their state |
| `gateway sync` | Check if routes are synced with UserDeployments |
| `gateway test` | Test connectivity through a specific route |

### Envoy Proxy

| Command | Description |
|---------|-------------|
| `envoy pods` | Show Envoy proxy pod status and node distribution |
| `envoy test` | Test HTTP connectivity to user pods on GPU nodes |
| `envoy logs` | Show Envoy access logs filtered by status code |
| `envoy path` | Trace network path from Envoy to a user pod |

### NetworkPolicy

| Command | Description |
|---------|-------------|
| `netpol audit` | Audit NetworkPolicies across tenant namespaces |
| `netpol coverage` | Check NetworkPolicy coverage across all namespaces |
| `netpol details` | Show detailed NetworkPolicy configuration |
| `netpol test` | Test DNS, egress, and ingress for a namespace |

### Namespace Management

| Command | Description |
|---------|-------------|
| `namespace list` | List tenant namespaces with resource counts |
| `namespace audit` | Audit RBAC, NetworkPolicies, and Secrets |
| `namespace resources` | Show all resources in a tenant namespace |
| `namespace cleanup` | Find and clean orphaned namespaces |
| `namespace summary` | Show summary statistics for all namespaces |

### Troubleshooting

| Command | Description |
|---------|-------------|
| `pod-troubleshoot` | Deep diagnostics for pod issues |
| `fuse-troubleshoot` | Diagnose FUSE daemon issues on cluster nodes |
| `node-pressure` | Detect and report node pressure conditions |

### Security

| Command | Description |
|---------|-------------|
| `audit-pods` | Security audit of pod configurations |
| `cert-check` | Check certificate expiry dates |
| `kubeconfig generate` | Generate read-only kubeconfig for monitoring/CI |
| `kubeconfig list` | List existing ServiceAccounts for cluster access |
| `kubeconfig verify` | Verify kubeconfig has correct read-only permissions |
| `kubeconfig rotate` | Rotate ServiceAccount token (create new Secret) |
| `kubeconfig revoke` | Revoke access by deleting ServiceAccount |

### Operations

| Command | Description |
|---------|-------------|
| `fix` | Auto-fix common issues with safety guards |
| `cleanup` | Remove CrashLoopBackOff pods |
| `logs` | View recent K3s logs |
| `bundle` | Collect diagnostic bundle for escalation |

## Global Options

| Option | Description |
|--------|-------------|
| `--kubeconfig` | Path to kubeconfig file (default: ~/.kube/k3s-basilica-config) |
| `--inventory`, `-i` | Ansible inventory file |
| `--dry-run` | Preview actions without making changes |
| `--no-confirm`, `-y` | Skip confirmation prompts |
| `--verbose`, `-v` | Show verbose output |

## Safety Features

1. **Dry-run mode**: Use `--dry-run` to preview all changes
2. **Confirmation prompts**: Required for all destructive operations
3. **Impact scoring**: Remediation steps are scored by impact (1-10)
4. **Reversibility indicators**: Shows which operations can be undone

## Read-Only Kubeconfig Generation

Generate secure, read-only kubeconfig files for monitoring systems, CI/CD, or external integrations:

```bash
# Basic generation (1-year token)
uv run clustermgr kubeconfig generate --name prometheus-readonly

# Custom duration (90 days for human users)
uv run clustermgr kubeconfig generate --name alice-viewer --duration 2160h

# Custom output path
uv run clustermgr kubeconfig generate \
  --name ci-reader \
  --output /etc/ci/k3s-kubeconfig.yaml

# List existing accounts
uv run clustermgr kubeconfig list

# Verify permissions
uv run clustermgr kubeconfig verify --kubeconfig-path ./kubeconfig-name.yaml

# Rotate token (6-month rotation policy)
uv run clustermgr kubeconfig rotate --name prometheus-readonly

# Revoke access immediately
uv run clustermgr kubeconfig revoke --name compromised-account
```

**Features:**
- Custom ClusterRole with explicit read-only permissions
- Long-lived tokens (K8s 1.24+ compatible)
- Dedicated `basilica-monitoring` namespace
- Automatic CA certificate and API server extraction
- Security verification and audit trail
- Easy rotation and revocation

See [docs/READONLY-KUBECONFIG-GUIDE.md](docs/READONLY-KUBECONFIG-GUIDE.md) for comprehensive documentation.

## HTTP 503 Troubleshooting

When UserDeployments receive HTTP 503 errors, use this diagnostic workflow:

```bash
# 1. Check Flannel VXLAN health
uv run clustermgr flannel diagnose

# 2. Check for duplicate VtepMACs
uv run clustermgr flannel mac-duplicates

# 3. Verify FDB entries for GPU nodes
uv run clustermgr flannel fdb

# 4. Check neighbor/ARP entries
uv run clustermgr flannel neighbors

# 5. Verify routes to GPU node pod CIDRs
uv run clustermgr flannel routes

# 6. Test connectivity to GPU nodes
uv run clustermgr flannel test

# 7. Check Envoy pod status
uv run clustermgr envoy pods

# 8. Test HTTP connectivity to user pods
uv run clustermgr envoy test

# 9. Check Envoy logs for errors
uv run clustermgr envoy logs -s 5

# 10. Auto-fix detected issues
uv run clustermgr fix --dry-run
uv run clustermgr fix

# 11. If escalating, collect diagnostic bundle
uv run clustermgr bundle
```

## Network Maintenance Workflow

For scheduled maintenance operations:

```bash
# 1. Check current cluster state
uv run clustermgr maintenance status
uv run clustermgr scaling capacity

# 2. Cordon node to prevent new pods
uv run clustermgr maintenance cordon gpu-node-1

# 3. Drain pods from node
uv run clustermgr maintenance drain gpu-node-1

# 4. Perform maintenance (reboot, driver update, etc.)

# 5. Uncordon node
uv run clustermgr maintenance uncordon gpu-node-1

# 6. Verify recovery
uv run clustermgr maintenance verify
```

## K3s Server Maintenance

For K3s server maintenance (with etcd):

```bash
# 1. Check etcd health before maintenance
uv run clustermgr etcd health
uv run clustermgr etcd status

# 2. Perform rolling restart (one server at a time)
uv run clustermgr maintenance rolling-restart -t server

# 3. After maintenance, check etcd
uv run clustermgr etcd health
uv run clustermgr etcd alarms

# 4. Defrag etcd if needed
uv run clustermgr etcd defrag -a
```

## Architecture

```
src/clustermgr/
├── __init__.py           # Package version
├── cli.py                # Main CLI entry point
├── config.py             # Configuration management
├── utils.py              # Shared utilities
└── commands/
    ├── __init__.py       # Command exports
    ├── health.py         # Health check command
    ├── diagnose.py       # Diagnostics command
    ├── topology.py       # Network topology discovery
    ├── wg.py             # WireGuard commands
    ├── flannel.py        # Flannel VXLAN diagnostics
    ├── envoy.py          # Envoy proxy diagnostics
    ├── gateway.py        # Gateway API troubleshooting
    ├── ud.py             # UserDeployment management
    ├── netpol.py         # NetworkPolicy diagnostics
    ├── namespace.py      # Namespace management
    ├── maintenance.py    # Node maintenance commands
    ├── scaling.py        # Cluster scaling diagnostics
    ├── etcd.py           # etcd cluster management
    ├── bundle.py         # Diagnostic bundle collection
    ├── fix.py            # Remediation command
    ├── cleanup.py        # Pod cleanup command
    ├── logs.py           # Log viewing command
    ├── firewall.py       # Firewall audit
    ├── mtu.py            # MTU validation
    ├── mesh_test.py      # Mesh connectivity testing
    ├── latency_matrix.py # Latency measurement
    ├── pod_troubleshoot.py    # Pod diagnostics
    ├── fuse_troubleshoot.py   # FUSE daemon diagnostics
    ├── node_pressure.py  # Node pressure detection
    ├── audit_pods.py     # Security audit
    ├── cert_check.py     # Certificate checking
    ├── kubeconfig.py     # Read-only kubeconfig generation
    ├── resources.py      # Resource utilization
    ├── deployments.py    # User deployments listing
    └── events.py         # Cluster events
```

## Related Runbooks

- `docs/runbooks/FLANNEL-VXLAN-TROUBLESHOOTING.md` - Detailed VXLAN issue resolution
- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP 503 error diagnosis workflow
- `docs/runbooks/NETWORK-SCALING-GUIDE.md` - Cluster scaling procedures
- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Maintenance procedures
- `docs/READONLY-KUBECONFIG-GUIDE.md` - Read-only kubeconfig generation guide

## Development

```bash
# Install dev dependencies
uv sync --dev

# Run tests
uv run pytest

# Type checking
uv run mypy src/

# Linting
uv run ruff check src/
```
