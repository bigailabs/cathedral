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

# WireGuard status
uv run clustermgr wg status
uv run clustermgr wg peers
uv run clustermgr wg restart

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

# Pod troubleshooting
uv run clustermgr pod-troubleshoot -s           # Scan for issues
uv run clustermgr pod-troubleshoot mypod -n ns  # Diagnose specific pod
uv run clustermgr pod-troubleshoot mypod -l -e  # With logs and events

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

# Fix issues (with safety guards)
uv run clustermgr fix --dry-run    # Preview changes
uv run clustermgr fix              # Execute with confirmation

# Cleanup failed pods
uv run clustermgr cleanup

# View logs
uv run clustermgr logs -n 100
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

### Network

| Command | Description |
|---------|-------------|
| `topology` | Network topology discovery with WireGuard connectivity status |
| `wg status` | Show WireGuard status on all servers |
| `wg peers` | List WireGuard peers with health metrics |
| `wg restart` | Restart WireGuard service |
| `firewall` | Audit iptables rules for potential issues |
| `mtu` | Validate MTU settings across network interfaces |
| `mesh-test` | Test full mesh WireGuard connectivity between all nodes |
| `latency-matrix` | Measure and display network latency between all nodes |

### Troubleshooting

| Command | Description |
|---------|-------------|
| `pod-troubleshoot` | Deep diagnostics for pod issues |
| `node-pressure` | Detect and report node pressure conditions |

### Security

| Command | Description |
|---------|-------------|
| `audit-pods` | Security audit of pod configurations |
| `cert-check` | Check certificate expiry dates |

### Operations

| Command | Description |
|---------|-------------|
| `fix` | Auto-fix common issues with safety guards |
| `cleanup` | Remove CrashLoopBackOff pods |
| `logs` | View recent K3s logs |

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
    ├── fix.py            # Remediation command
    ├── cleanup.py        # Pod cleanup command
    └── logs.py           # Log viewing command
```

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
