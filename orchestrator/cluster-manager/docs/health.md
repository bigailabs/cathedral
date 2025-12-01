# Core Commands: health, diagnose, fix

Core operational commands for cluster health monitoring and remediation.

## Overview

These three commands form the foundation of cluster operations:
- **health**: Quick health check for regular monitoring
- **diagnose**: Deep diagnostics for incident investigation
- **fix**: Automated remediation with safety guards

## health Command

### What it does

Performs a multi-layer health check of the cluster, providing a quick overview of cluster status.

### How it works

1. **Node Health**: Queries K8s API for node status and conditions
2. **WireGuard Health**: Checks peer connectivity and handshake freshness
3. **iptables Health**: Scans for dropped packet counters and rate limits
4. **Pod Health**: Identifies CrashLoopBackOff and failed pods

### When to use

- Regular health monitoring (e.g., morning check)
- First step in incident response
- After maintenance operations
- As part of automated monitoring

### Usage

```bash
clustermgr health
```

### Output

```
=== Cluster Health Check ===

=== Node Health ===
k3s-server-1     Ready
k3s-server-2     Ready
k3s-server-3     Ready
gpu-node-abc     Ready
gpu-node-def     Ready

=== WireGuard Health ===
server1: 2 peers, 0 stale
server2: 2 peers, 0 stale
server3: 2 peers, 0 stale

=== iptables Health ===
server1: 0 drops, no rate limit
server2: 0 drops, no rate limit
server3: 0 drops, no rate limit

=== Pod Health ===
CrashLoopBackOff: 0
Failed: 0

Overall: HEALTHY
```

### Exit codes

- 0: Cluster is healthy
- 1: Issues detected (warnings or critical)

---

## diagnose Command

### What it does

Runs comprehensive diagnostics to identify root causes of cluster issues.

### How it works

1. **Interface Diagnostics**: Checks for dropped packets on network interfaces
2. **WireGuard Diagnostics**: Detailed peer analysis and handshake status
3. **Route Analysis**: Verifies routing tables and missing routes
4. **502/503 Analysis**: Checks for issues causing HTTP errors

### When to use

- After `health` shows issues
- Investigating HTTP 502/503 errors
- Performance troubleshooting
- Deep dive after incidents

### Usage

```bash
clustermgr diagnose
```

### Output

```
=== Comprehensive Diagnostics ===

=== Interface Health ===
Checking network interfaces on all servers...
server1: wg0 - OK (0 drops)
server2: wg0 - WARN (1234 TX drops)
server3: wg0 - OK (0 drops)

=== WireGuard Peers ===
Checking peer connectivity...
All 6 peers healthy

=== Route Analysis ===
Checking for missing routes...
All routes present

=== HTTP 502/503 Analysis ===
Checking Envoy connectivity...
2 user pods tested, all responding

=== Summary ===
Found 1 issue(s):
  - server2: High TX drops on wg0 (1234)

Recommended actions:
  - Run 'clustermgr fix --dry-run' to preview remediation
```

### What it checks

| Category | Checks |
|----------|--------|
| Interfaces | Dropped packets, MTU consistency, interface state |
| WireGuard | Handshake freshness, peer count, AllowedIPs |
| Routes | Pod CIDR routes, WireGuard routes |
| HTTP | Envoy to user pod connectivity |

---

## fix Command

### What it does

Analyzes cluster state and applies automated remediation for common issues.

### How it works

1. **Analysis Phase**: Scans for issues (same checks as `diagnose`)
2. **Plan Phase**: Creates prioritized remediation steps
3. **Preview Phase**: Shows what will be done (always shown first)
4. **Execution Phase**: Applies fixes with confirmation

### Safety Features

- **Dry-run mode**: Preview changes without applying
- **Confirmation prompts**: Required before each fix
- **Impact scoring**: Each fix rated 1-10 for risk
- **Reversibility indicators**: Shows if fix can be undone
- **Post-fix health check**: Verifies cluster health after fixes

### When to use

- After `diagnose` identifies issues
- For routine maintenance automation
- Quick recovery from common issues

### Usage

```bash
# Preview what would be fixed
clustermgr fix --dry-run

# Apply fixes with confirmation
clustermgr fix

# Apply without confirmation (automation)
clustermgr fix -y
```

### What it fixes

| Issue | Remediation |
|-------|-------------|
| Dropped packets on WireGuard | Increase TX queue and network buffers |
| iptables rate limits | Remove rate limit rules |
| Stale WireGuard handshakes | Restart WireGuard service |
| Missing pod CIDRs in AllowedIPs | Run WireGuard reconciliation |
| FUSE daemon issues | Restart FUSE DaemonSets |
| CrashLoopBackOff pods | Delete failed pods |
| Failed UserDeployments | Restart deployment pods |
| Missing NetworkPolicies | Apply standard policies |
| Orphaned HTTPRoutes | Delete orphaned routes |
| Missing Flannel FDB entries | Add FDB entries |
| Missing Flannel neighbor entries | Add neighbor entries |
| Missing Flannel routes | Add routes |

### Output

```
=== Cluster Remediation ===
Timestamp: 2024-12-01T14:30:22

Analyzing cluster state...

=== Remediation Plan ===

  1. Optimize WireGuard network buffers
     Increase TX queue and network buffers on: server2
     Impact: 3/10, reversible

  2. Restart WireGuard
     Restart WireGuard on servers with stale handshakes: server3
     Impact: 7/10, NOT reversible

  Total impact score: 10

Execute 2 remediation step(s)? [y/N]: y

=== Executing Remediation ===

[1/2] Optimize WireGuard network buffers...
  Success

[2/2] Restart WireGuard...
  Success

=== Post-Remediation Health Check ===
Overall: HEALTHY
```

### Impact Scoring

| Score | Meaning |
|-------|---------|
| 1-3 | Low risk, quick to apply, minimal disruption |
| 4-6 | Moderate risk, may briefly affect service |
| 7-10 | High risk, may cause service interruption |

## Workflow

Standard incident response workflow:

```bash
# 1. Quick assessment
clustermgr health

# 2. If issues, deep dive
clustermgr diagnose

# 3. Preview fixes
clustermgr fix --dry-run

# 4. Apply fixes
clustermgr fix

# 5. Verify recovery
clustermgr health
```

## Related Commands

- `flannel diagnose` - Flannel-specific diagnostics
- `wg status` - WireGuard status
- `maintenance verify` - Post-maintenance checks

## Related Runbooks

- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP error troubleshooting
- `docs/runbooks/FLANNEL-VXLAN-TROUBLESHOOTING.md` - Network issues
