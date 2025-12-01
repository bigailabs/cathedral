# Maintenance Commands

Node maintenance commands for managing GPU node and K3s server lifecycle operations.

## Overview

The `maintenance` command group provides tools for safely performing maintenance operations on cluster nodes. It handles the complexity of draining workloads, coordinating restarts, and verifying recovery.

## Why These Commands Exist

When performing maintenance on Kubernetes nodes (driver updates, security patches, hardware repairs), you need to:
1. Prevent new workloads from scheduling on the node
2. Gracefully evict existing workloads
3. Perform the maintenance
4. Verify the node recovers correctly

Manual execution of these steps is error-prone. These commands automate the process with safety guards.

## Commands

### maintenance status

**What it does:** Displays the maintenance state of all nodes including schedulability, readiness, and pod counts.

**How it works:**
1. Queries K8s API for all nodes
2. Categorizes nodes by type (K3s server, K3s agent, GPU node) based on labels
3. Checks `spec.unschedulable` for cordon status
4. Counts pods per node from pod list
5. Extracts last heartbeat time from node conditions

**When to use:** Before starting maintenance to understand current cluster state, or to verify nodes are in expected state.

```bash
clustermgr maintenance status
```

**Output:**
```
=== Node Maintenance Status ===

=== K3s Servers ===
| Node         | Ready | Schedulable | Pods | Last Heartbeat |
|--------------|-------|-------------|------|----------------|
| k3s-server-1 | Yes   | Yes         | 12   | 14:32:45       |
| k3s-server-2 | Yes   | Yes         | 8    | 14:32:41       |

=== GPU Nodes ===
| Node           | Ready | Schedulable | Pods | Last Heartbeat |
|----------------|-------|-------------|------|----------------|
| gpu-node-abc12 | Yes   | Cordoned    | 0    | 14:32:38       |
```

---

### maintenance cordon

**What it does:** Marks a node as unschedulable, preventing new pods from being placed on it.

**How it works:**
1. Executes `kubectl cordon <node>` which sets `spec.unschedulable: true`
2. Existing pods continue running; only new scheduling is blocked

**When to use:** First step before draining a node for maintenance.

```bash
clustermgr maintenance cordon gpu-node-1
```

**Options:**
- None (node name is required argument)

**Notes:**
- Safe operation - does not affect running workloads
- Reversible with `uncordon`
- DaemonSets ignore cordon and will still schedule

---

### maintenance uncordon

**What it does:** Marks a node as schedulable again, allowing new pods to be placed on it.

**How it works:**
1. Executes `kubectl uncordon <node>` which removes `spec.unschedulable`
2. Node becomes available for pod scheduling immediately

**When to use:** After maintenance is complete and the node is ready for workloads.

```bash
clustermgr maintenance uncordon gpu-node-1
```

---

### maintenance drain

**What it does:** Safely evicts all pods from a node, preparing it for maintenance.

**How it works:**
1. Cordons the node first (prevents new pods)
2. Evicts pods respecting PodDisruptionBudgets
3. Waits for pods to terminate with configurable grace period
4. DaemonSet pods are ignored (they're expected on every node)
5. Pods with emptyDir volumes are deleted (data is ephemeral)

**When to use:** Before taking a node offline for maintenance, upgrades, or decommissioning.

```bash
# Basic drain
clustermgr maintenance drain gpu-node-1

# With longer grace period for slow applications
clustermgr maintenance drain gpu-node-1 -g 600

# With timeout and force for stubborn pods
clustermgr maintenance drain gpu-node-1 -t 900 -f
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-g, --grace-period` | 300 | Pod termination grace period in seconds |
| `-t, --timeout` | 600 | Total drain timeout in seconds |
| `-f, --force` | false | Force drain even with unmanaged pods |

**Safety features:**
- Respects PodDisruptionBudgets
- Requires confirmation (unless `-y`)
- Shows pod count before draining
- Reports success/failure status

---

### maintenance rolling-restart

**What it does:** Restarts nodes one at a time with health verification between each.

**How it works:**
1. Gets list of target nodes (server or GPU)
2. For each node:
   - Restarts the K3s/K3s-agent service via Ansible
   - Waits for node to become Ready (up to 5 minutes)
   - Waits configurable delay before next node
3. For servers: uses `systemctl restart k3s`
4. For GPU nodes: uses `systemctl restart k3s-agent`

**When to use:** After configuration changes that require service restart, or for applying kernel updates.

```bash
# Rolling restart of K3s servers (with etcd)
clustermgr maintenance rolling-restart -t server

# Rolling restart of GPU nodes
clustermgr maintenance rolling-restart -t gpu

# With longer delay between nodes
clustermgr maintenance rolling-restart -t gpu -d 300
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-t, --type` | required | Node type: `server` or `gpu` |
| `-d, --delay` | 120 | Seconds between node restarts |

**Safety features:**
- One node at a time
- Health verification before proceeding
- Option to abort if a restart fails
- For servers, maintains etcd quorum

---

### maintenance verify

**What it does:** Runs post-maintenance verification checks to confirm cluster health.

**How it works:**
1. Checks all nodes are Ready
2. Verifies no nodes are unexpectedly cordoned
3. Checks WireGuard peer count matches GPU node count
4. Scans for CrashLoopBackOff pods
5. Verifies flannel.1 interface is UP

**When to use:** After completing maintenance and uncordoning nodes.

```bash
clustermgr maintenance verify
```

**Output:**
```
=== Post-Maintenance Verification ===
Checking node status...
Checking WireGuard status...
Checking pod health...
Checking Flannel health...

=== Verification Summary ===
All verification checks passed

Nodes: 10 total
  - Servers: 3
  - Agents: 5
  - GPU: 2
```

**Exit codes:**
- 0: All checks passed
- 1: Critical issues found (requires attention)

## Workflow Example

Complete maintenance workflow for a GPU node:

```bash
# 1. Check current state
clustermgr maintenance status

# 2. Cordon the node
clustermgr maintenance cordon gpu-node-abc

# 3. Drain workloads
clustermgr maintenance drain gpu-node-abc

# 4. Perform maintenance (SSH to node, update drivers, reboot, etc.)

# 5. Uncordon when ready
clustermgr maintenance uncordon gpu-node-abc

# 6. Verify recovery
clustermgr maintenance verify
```

## Related Commands

- `scaling capacity` - Check if cluster can handle workload redistribution
- `wg status` - Verify WireGuard connectivity after node restart
- `health` - Quick cluster health check

## Related Runbooks

- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Detailed maintenance procedures
- `docs/runbooks/gpu-node-onboarding-troubleshooting.md` - GPU node issues
