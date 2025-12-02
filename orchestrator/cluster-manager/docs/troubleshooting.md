# Troubleshooting Commands

Deep diagnostics for pods, FUSE storage daemon, and node pressure conditions.

## Overview

These standalone troubleshooting commands provide targeted diagnostics for specific subsystems. They complement the general `diagnose` command with deeper analysis capabilities.

## Why These Commands Exist

When the general health check or diagnostics indicate an issue, you often need to drill down into a specific subsystem:

- **Pod issues**: CrashLoopBackOff, ImagePullBackOff, resource exhaustion
- **FUSE issues**: Storage mount failures, stale mounts, network isolation
- **Node pressure**: Memory, disk, or PID pressure causing evictions

These commands provide the depth of analysis needed for root cause identification.

## Commands

### pod-troubleshoot

**What it does:** Deep diagnostics for pod issues including container status, events, and log analysis.

**How it works:**
1. Scans for problematic pods across namespaces
2. Analyzes container states and restart counts
3. Checks for common issues (CrashLoopBackOff, ImagePullBackOff)
4. Retrieves and highlights errors in logs
5. Shows relevant Kubernetes events

**When to use:**
- Pods stuck in Pending or CrashLoopBackOff
- High restart counts
- Application not responding
- After failed deployments

```bash
# Scan all namespaces for problematic pods
clustermgr pod-troubleshoot

# Scan specific namespace
clustermgr pod-troubleshoot -n u-alice

# Troubleshoot specific pod
clustermgr pod-troubleshoot my-pod -n u-alice

# Include logs
clustermgr pod-troubleshoot my-pod -n u-alice --logs

# Include events
clustermgr pod-troubleshoot my-pod -n u-alice --events

# Specify container and log lines
clustermgr pod-troubleshoot my-pod -n u-alice --logs --container main --tail 100
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-n, --namespace` | default | Pod namespace |
| `-l, --logs` | false | Show pod logs |
| `-e, --events` | false | Show pod events |
| `--tail` | 50 | Number of log lines |
| `-c, --container` | - | Container name for logs |
| `-s, --scan` | false | Scan for problematic pods |

**Scan output:**
```
=== Scanning for Problematic Pods ===

| Namespace | Pod                    | Phase    | Restarts |
|-----------|------------------------|----------|----------|
| u-alice   | my-app-abc123          | Pending  | -        |
| u-bob     | training-xyz789        | Failed   | 12       |

Run 'clustermgr pod-troubleshoot <pod> -n <namespace>' for details
```

**Detailed output:**
```
=== Pod Troubleshooting: u-alice/my-app-abc123 ===

Pod Information
| Key       | Value                           |
|-----------|----------------------------------|
| Name      | my-app-abc123                    |
| Namespace | u-alice                          |
| Node      | gpu-node-abc                     |
| Phase     | Pending                          |
| IP        | -                                |
| Created   | 2024-12-01T10:30:00Z             |

=== Container Status ===
  main: [yellow]Waiting: ContainerCreating[/yellow]
    Ready: No, Restarts: 0

=== Detected Issues ===
  [WARNING] Container main waiting
    Reason: ContainerCreating
    Message: waiting for volume mount
```

**Issue analysis:**

| Issue | Severity | Common Causes |
|-------|----------|---------------|
| CrashLoopBackOff | Critical | Application crash, OOM, config error |
| ImagePullBackOff | Critical | Wrong image name, auth failure, registry down |
| Pending | Warning | No schedulable nodes, resource constraints |
| High restarts | Warning | OOM kills, liveness probe failures |
| No resource limits | Warning | Pod may be evicted under pressure |

---

### fuse-troubleshoot

**What it does:** Diagnoses FUSE storage daemon issues across cluster nodes.

**How it works:**
1. Checks FUSE module loader DaemonSet status
2. Checks FUSE daemon DaemonSet status
3. Identifies missing or unhealthy pods
4. Tests container network connectivity
5. Detects stale mount points
6. Analyzes daemon logs for error patterns

**When to use:**
- Storage mount failures in user pods
- "File exists" errors in pod logs
- FUSE daemon crash loops
- After network configuration changes

```bash
# Scan all nodes for FUSE issues
clustermgr fuse-troubleshoot

# Troubleshoot specific node
clustermgr fuse-troubleshoot gpu-node-abc

# Deep diagnostics with network tests
clustermgr fuse-troubleshoot gpu-node-abc --deep

# Show FUSE daemon logs
clustermgr fuse-troubleshoot gpu-node-abc --logs

# Show events
clustermgr fuse-troubleshoot gpu-node-abc --events

# Clean up stale mounts
clustermgr fuse-troubleshoot gpu-node-abc --fix-mounts
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-l, --logs` | false | Show FUSE daemon logs |
| `-e, --events` | false | Show pod events |
| `--tail` | 50 | Number of log lines |
| `-s, --scan` | false | Scan all nodes |
| `-d, --deep` | false | Deep diagnostics (network, DNS, stale mounts) |
| `--fix-mounts` | false | Clean up stale FUSE mounts |

**Scan output:**
```
=== FUSE Daemon Status Overview ===

| Node         | Pod                  | Phase   | Ready | Restarts | Issues |
|--------------|----------------------|---------|-------|----------|--------|
| gpu-node-abc | fuse-daemon-abc123   | Running | Yes   | 0        | -      |
| gpu-node-def | fuse-daemon-def456   | Running | No    | 5        | crash  |
| gpu-node-xyz | -                    | Missing | -     | -        | -      |

=== FUSE Diagnostics ===
Found 2 issue(s):

  [CRITICAL] gpu-node-xyz: fuse_daemon_missing
    FUSE daemon pod not scheduled on node
    Fix: Check node taints/tolerations and daemonset spec

  [CRITICAL] gpu-node-def: fuse_daemon_crash_loop
    FUSE daemon has 5 restarts
    Fix: kubectl logs -n basilica-storage fuse-daemon-def456 --previous
```

**Deep diagnostics output:**
```
=== FUSE Troubleshooting: gpu-node-abc ===

=== FUSE Module Loader ===
  Pod: fuse-module-loader-abc123 [OK]
  Phase: Running [OK]
  Ready: Yes [OK]

=== FUSE Daemon ===
  Pod: fuse-daemon-abc123 [OK]
  Phase: Running [OK]
  Ready: Yes [OK]
  Restarts: 0 [OK]

=== Deep Diagnostics ===
  Stale Mounts: None [OK]
  Log Errors: None detected [OK]

  Testing container network to CoreDNS (10.42.0.5)...
  Container -> CoreDNS: OK [OK]

  Testing DNS resolution from container...
  DNS Resolution: OK [OK]
```

**Common FUSE issues:**

| Issue | Cause | Remediation |
|-------|-------|-------------|
| fuse_loader_missing | DaemonSet not scheduled | Check node selector/tolerations |
| fuse_daemon_missing | DaemonSet not scheduled | Check node selector/tolerations |
| fuse_daemon_crash_loop | Init container or daemon crash | Check logs, FUSE module loaded |
| stale_fuse_mounts | Previous mount not cleaned | Use `--fix-mounts` to clean |
| network_or_dns_failure | WireGuard routing issue | Run `clustermgr wg reconcile --fix` |

**Log error patterns detected:**

| Pattern | Meaning |
|---------|---------|
| dispatch_failure | gRPC dispatch to backend failed |
| dns_failure | Container cannot resolve DNS |
| stale_mount | Mount point exists but is broken |
| connection_refused | Backend service not reachable |
| timeout | Operation timed out |

---

### node-pressure

**What it does:** Detects and reports Kubernetes node pressure conditions.

**How it works:**
1. Gets node conditions from K8s API
2. Checks for pressure conditions (Memory, Disk, PID)
3. Optionally gets current usage metrics
4. Identifies nodes at risk of eviction

**When to use:**
- Pods being evicted
- Nodes becoming NotReady
- Investigating capacity issues
- Proactive monitoring

```bash
# Check all nodes for pressure conditions
clustermgr node-pressure

# Include current usage metrics
clustermgr node-pressure --metrics

# Verbose output with remediation
clustermgr node-pressure --verbose
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-m, --metrics` | false | Include current usage metrics |
| `-v, --verbose` | false | Show detailed condition info |

**Output:**
```
=== Node Pressure Detection ===

| Node         | Ready | Memory | Storage | Pods | Issues | CPU % | Mem % |
|--------------|-------|--------|---------|------|--------|-------|-------|
| k3s-server-1 | Yes   | 31.3Gi | 95Gi    | 110  | 0      | 25%   | 45%   |
| k3s-server-2 | Yes   | 31.3Gi | 95Gi    | 110  | 0      | 30%   | 50%   |
| gpu-node-abc | Yes   | 125Gi  | 450Gi   | 110  | 1      | 85%   | 92%   |

=== Detected Pressure Conditions ===
  gpu-node-abc: HighMemory: 92% memory usage [CRITICAL]

Summary: 0 nodes not ready, 1 pressure conditions
```

**Verbose output:**
```
=== Detected Pressure Conditions ===
  gpu-node-abc: HighMemory: 92% memory usage [CRITICAL]
    Description: Memory approaching capacity
    Impact: OOM kills likely
    Fix: Reduce workload or add memory
```

**Pressure conditions:**

| Condition | Description | Impact |
|-----------|-------------|--------|
| MemoryPressure | Node is under memory pressure | Pods may be evicted |
| DiskPressure | Node is under disk pressure | Image pulls may fail |
| PIDPressure | Too many processes on node | Containers may fail to start |
| NetworkUnavailable | Network not configured | Pods cannot communicate |
| NotReady | Node is not ready | No pods can be scheduled |
| HighCPU (>90%) | CPU utilization very high | Performance degradation |
| HighMemory (>90%) | Memory utilization very high | OOM kills likely |

## Troubleshooting Workflows

### Pod CrashLoopBackOff

```bash
# 1. Identify the pod
clustermgr pod-troubleshoot --scan

# 2. Get detailed diagnostics
clustermgr pod-troubleshoot <pod> -n <namespace> --logs --events

# 3. Check previous container logs
kubectl logs -n <namespace> <pod> --previous

# 4. Check resource usage
clustermgr node-pressure --metrics
```

### FUSE Storage Mount Failure

```bash
# 1. Scan for FUSE issues
clustermgr fuse-troubleshoot

# 2. Deep diagnostics on affected node
clustermgr fuse-troubleshoot <node> --deep

# 3. If stale mounts found
clustermgr fuse-troubleshoot <node> --fix-mounts

# 4. Check network path
clustermgr wg reconcile
clustermgr flannel diagnose
```

### Node Capacity Issues

```bash
# 1. Check pressure conditions
clustermgr node-pressure --metrics --verbose

# 2. Check overall capacity
clustermgr scaling capacity

# 3. If disk pressure
kubectl get pods --all-namespaces -o wide | grep <node>
# Clean up unused images/pods

# 4. If memory pressure
# Identify high-memory pods and reduce workload
```

## Integration with Other Commands

These troubleshooting commands work with:

- `health` - Quick cluster health overview
- `diagnose` - Comprehensive diagnostics
- `fix` - Automated remediation
- `ud inspect` - UserDeployment-specific diagnostics
- `flannel diagnose` - Network layer issues
- `wg status` - WireGuard connectivity

## Related Runbooks

- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP error troubleshooting
- `docs/runbooks/FLANNEL-VXLAN-TROUBLESHOOTING.md` - Network issues
