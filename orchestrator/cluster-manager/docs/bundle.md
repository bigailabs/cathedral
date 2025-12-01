# Bundle Command

Diagnostic bundle collection for troubleshooting and escalation.

## Overview

The `bundle` command collects comprehensive diagnostic information from the cluster and packages it into a tarball for analysis or escalation to senior engineers.

## Why This Command Exists

When troubleshooting complex issues or escalating to other team members, you need:
- Current cluster state (nodes, pods, services)
- Recent logs from key components
- Network configuration (routes, FDB, neighbors)
- System resource status

Manually collecting this information is time-consuming and error-prone. This command automates the collection into a consistent, comprehensive package.

## What's Collected

The bundle includes:

### Kubernetes State
| File | Contents |
|------|----------|
| `nodes.txt` | Node list with status (`kubectl get nodes -o wide`) |
| `nodes-yaml.txt` | Full node definitions |
| `pods.txt` | All pods across namespaces |
| `services.txt` | All services |
| `endpoints.txt` | Service endpoints |
| `events.txt` | Recent events sorted by time |
| `httproutes.txt` | Gateway API routes |
| `userdeployments.txt` | UserDeployment resources |
| `networkpolicies.txt` | NetworkPolicy definitions |

### Logs
| File | Contents |
|------|----------|
| `envoy-logs.txt` | Envoy Gateway proxy logs (last 500 lines) |
| `operator-logs.txt` | Basilica operator logs (last 500 lines) |
| `namespace-{ns}-*.txt` | Namespace-specific logs (if `-n` specified) |

### Network State
| File | Contents |
|------|----------|
| `routes.txt` | Routing tables from all servers |
| `fdb.txt` | Flannel FDB entries |
| `neighbors.txt` | Neighbor/ARP entries |
| `wireguard.txt` | WireGuard status |
| `interfaces.txt` | Network interface details |
| `iptables.txt` | Firewall rules (unless `-q`) |
| `sysctl.txt` | Network sysctl settings (unless `-q`) |

### System Info
| File | Contents |
|------|----------|
| `system-resources.txt` | Memory, CPU, disk usage |
| `metadata.txt` | Bundle creation timestamp and options |

## Command Usage

### Basic Usage

```bash
# Collect full diagnostic bundle
clustermgr bundle

# Output to specific directory
clustermgr bundle -o /home/user/diagnostics
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-o, --output` | `/tmp` | Output directory for the bundle tarball |
| `-n, --namespace` | none | Focus on specific namespace (adds extra data) |
| `-q, --quick` | false | Skip slow operations (iptables, sysctl) |

### Quick Mode

Use quick mode when time is critical:

```bash
clustermgr bundle -q
```

Quick mode skips:
- iptables rule collection
- sysctl settings collection

This reduces collection time from ~2 minutes to ~30 seconds.

### Namespace Focus

When investigating a specific UserDeployment:

```bash
clustermgr bundle -n u-alice
```

This adds:
- Pods in the namespace
- Events for the namespace
- Detailed namespace-specific information

## Output

The command creates a gzipped tarball:

```
/tmp/basilica-diag-20241201_143022.tar.gz
```

**Output:**
```
=== Diagnostic Bundle Collection ===
Output: /tmp/basilica-diag-20241201_143022
Quick mode: False

=== Collecting Kubernetes State ===
  Collecting nodes...
  Collecting pods...
  Collecting services...
  Collecting endpoints...
  Collecting events...
  Collecting HTTPRoutes...
  Collecting UserDeployments...
  Collecting NetworkPolicies...

=== Collecting Logs ===
  Collecting Envoy Gateway logs...
  Collecting operator logs...

=== Collecting Network State ===
  Collecting routing tables...
  Collecting FDB entries...
  Collecting neighbor entries...
  Collecting WireGuard status...
  Collecting interface info...
  Collecting iptables rules...
  Collecting sysctl settings...

=== Collecting System Info ===
  Collecting memory/CPU...

=== Creating Archive ===

Bundle created: /tmp/basilica-diag-20241201_143022.tar.gz
Size: 2.35 MB

To extract: tar -xzf /tmp/basilica-diag-20241201_143022.tar.gz

Include this bundle when escalating issues.
```

## Extracting the Bundle

```bash
# Extract to current directory
tar -xzf basilica-diag-20241201_143022.tar.gz

# View contents
ls basilica-diag-20241201_143022/
# envoy-logs.txt  events.txt  fdb.txt  interfaces.txt  ...
```

## When to Use

### Escalation
When escalating an issue to senior engineers:
1. Collect the bundle
2. Attach to the ticket/issue
3. Include a brief description of the problem

### Complex Debugging
When local debugging isn't sufficient:
1. Collect the bundle
2. Review offline
3. Share with team members

### Incident Documentation
For post-incident analysis:
1. Collect bundle during/after incident
2. Archive for later review
3. Use for root cause analysis

## Best Practices

1. **Collect early**: Gather bundle as soon as issue is reported
2. **Use namespace focus**: If issue is namespace-specific, use `-n`
3. **Include context**: When sharing, describe what was happening
4. **Timestamp matters**: Note when the issue occurred vs bundle collection

## Security Considerations

The bundle may contain:
- Pod names and namespaces
- Service configurations
- Network topology
- IP addresses

**Do not share bundles publicly** - they contain internal cluster information.

## Workflow Example

Complete escalation workflow:

```bash
# 1. Attempt local diagnosis
clustermgr health
clustermgr diagnose
clustermgr flannel diagnose

# 2. If unable to resolve, collect bundle
clustermgr bundle -o /tmp

# 3. Create escalation ticket with:
#    - Problem description
#    - Steps already taken
#    - Bundle tarball attached

# 4. For namespace-specific issues
clustermgr bundle -n u-affected-user -o /tmp
```

## Related Commands

- `health` - Quick health check (run before bundle)
- `diagnose` - Detailed diagnostics (run before bundle)
- `flannel diagnose` - Flannel-specific diagnostics

## Related Runbooks

- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - Includes escalation criteria
