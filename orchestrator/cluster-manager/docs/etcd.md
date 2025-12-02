# etcd Commands

etcd cluster health monitoring and maintenance operations for K3s.

## Overview

The `etcd` command group provides tools for monitoring and maintaining the etcd cluster that backs K3s. etcd is the distributed key-value store that holds all Kubernetes state, making its health critical for cluster operations.

## Why These Commands Exist

etcd issues can cause:
- API server slowness or unavailability
- Failed pod scheduling
- Lost cluster state
- Split-brain scenarios

Regular monitoring and maintenance prevents these issues. These commands provide visibility into etcd health without requiring direct etcd access.

## Background: etcd in K3s

K3s embeds etcd (when using embedded etcd mode) or uses an external etcd cluster. The etcd data includes:
- All Kubernetes objects (pods, services, deployments, etc.)
- Secrets and ConfigMaps
- Custom resources (UserDeployments, etc.)

Key etcd concepts:
- **Raft**: Consensus algorithm for leader election and replication
- **Quorum**: Majority of members must agree (e.g., 2 of 3, 3 of 5)
- **Compaction**: Removing historical revisions to save space
- **Defragmentation**: Reclaiming disk space after compaction

## Commands

### etcd health

**What it does:** Verifies all etcd endpoints are healthy and responding.

**How it works:**
1. Finds etcd pods in kube-system namespace
2. Executes `etcdctl endpoint health --cluster` inside the pod
3. Parses response to show health status per endpoint
4. Reports latency for each health check

**When to use:** Regular health monitoring, before maintenance, or when API server is slow.

```bash
clustermgr etcd health
```

**Output:**
```
=== etcd Cluster Health ===
Found 3 etcd pod(s)

https://10.0.1.10:2379 Healthy (took 12ms)
https://10.0.1.11:2379 Healthy (took 8ms)
https://10.0.1.12:2379 Healthy (took 15ms)

etcd cluster is healthy
```

**Exit codes:**
- 0: All endpoints healthy
- 1: One or more endpoints unhealthy

---

### etcd status

**What it does:** Shows detailed status of each etcd member including database size and raft state.

**How it works:**
1. Executes `etcdctl endpoint status --cluster` inside etcd pod
2. Parses JSON output to extract:
   - Database size
   - Leader status
   - Raft index (how many operations processed)
   - Raft term (leader election epoch)

**When to use:** Investigating etcd performance, checking leader distribution, or monitoring DB growth.

```bash
clustermgr etcd status
```

**Output:**
```
=== etcd Member Status ===
| Endpoint                  | Leader | DB Size  | Raft Index | Raft Term |
|---------------------------|--------|----------|------------|-----------|
| https://10.0.1.10:2379    | Yes    | 156.2 MB | 1234567    | 42        |
| https://10.0.1.11:2379    | No     | 156.1 MB | 1234567    | 42        |
| https://10.0.1.12:2379    | No     | 156.3 MB | 1234567    | 42        |
```

**Key metrics:**
- **DB Size**: Should be similar across members; large differences indicate sync issues
- **Raft Index**: Should be identical or very close across members
- **Raft Term**: Increments with each leader election; high values may indicate instability

---

### etcd members

**What it does:** Lists all members of the etcd cluster.

**How it works:**
1. Executes `etcdctl member list` inside etcd pod
2. Displays member IDs, names, and peer URLs

**When to use:** Verifying cluster membership, before adding/removing members.

```bash
clustermgr etcd members
```

**Output:**
```
=== etcd Cluster Members ===
+------------------+----------+--------------------+------------------------+
|        ID        |  STATUS  |       NAME         |       PEER ADDRS       |
+------------------+----------+--------------------+------------------------+
| 8e9e05c52164694d | started  | k3s-server-1       | https://10.0.1.10:2380 |
| a3f2b1c4d5e67890 | started  | k3s-server-2       | https://10.0.1.11:2380 |
| b4c3d2e1f0987654 | started  | k3s-server-3       | https://10.0.1.12:2380 |
+------------------+----------+--------------------+------------------------+
```

---

### etcd defrag

**What it does:** Defragments the etcd database to reclaim disk space.

**How it works:**
1. Executes `etcdctl defrag` inside etcd pod
2. etcd reorganizes its database file, freeing unused space
3. Can target single member or all members

**When to use:** After compaction, when disk usage is high, or as regular maintenance (monthly).

```bash
# Defrag local member
clustermgr etcd defrag

# Defrag all members
clustermgr etcd defrag -a
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-a, --all` | false | Defragment all cluster members |

**Caution:**
- Defragmentation briefly pauses etcd operations
- Run during maintenance windows
- Defrag one member at a time in production

**Expected impact:**
- Brief latency spike (100-500ms)
- Disk I/O increase during defrag
- Space reclaimed after completion

---

### etcd alarms

**What it does:** Checks for active etcd alarms that indicate critical issues.

**How it works:**
1. Executes `etcdctl alarm list` inside etcd pod
2. Reports any active alarms

**When to use:** When etcd is behaving unexpectedly, API server is rejecting writes, or disk is full.

```bash
clustermgr etcd alarms
```

**Output (healthy):**
```
=== etcd Alarms ===
No active alarms
```

**Output (alarm present):**
```
=== etcd Alarms ===
Active Alarms:
  NOSPACE
```

**Common alarms:**
- **NOSPACE**: Disk quota exceeded; etcd is read-only until resolved
- **CORRUPT**: Data corruption detected; requires recovery

**Resolving NOSPACE:**
```bash
# 1. Compact history
clustermgr etcd compact

# 2. Defrag to reclaim space
clustermgr etcd defrag -a

# 3. If still full, increase disk or reduce retention
```

---

### etcd compact

**What it does:** Compacts etcd history to remove old revisions and reduce database size.

**How it works:**
1. Gets current revision from etcd status
2. Calculates target revision (current - revisions_to_keep)
3. Executes `etcdctl compact <revision>`
4. Old revisions become inaccessible

**When to use:** Before defragmentation, when database is growing large, or as regular maintenance.

```bash
# Keep last 10,000 revisions (default)
clustermgr etcd compact

# Keep more history
clustermgr etcd compact -r 50000
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-r, --revisions-to-keep` | 10000 | Number of revisions to preserve |

**Output:**
```
=== etcd Compaction ===
Current revision: 1234567
Compact to revision: 1224567
Keeping last 10000 revisions

Compacted to revision 1224567
```

**Important:**
- Compaction is irreversible
- Cannot access revisions before compaction point
- Follow with defrag to reclaim disk space

## Maintenance Workflow

Regular etcd maintenance:

```bash
# 1. Check health first
clustermgr etcd health

# 2. Check for alarms
clustermgr etcd alarms

# 3. Check current status
clustermgr etcd status

# 4. Compact old revisions
clustermgr etcd compact -r 10000

# 5. Defragment all members (one at a time in prod)
clustermgr etcd defrag -a

# 6. Verify health after
clustermgr etcd health
```

## Troubleshooting

### High DB Size
```bash
clustermgr etcd status  # Check current size
clustermgr etcd compact # Remove old revisions
clustermgr etcd defrag -a  # Reclaim space
```

### NOSPACE Alarm
```bash
clustermgr etcd alarms  # Confirm alarm
clustermgr etcd compact -r 5000  # Aggressive compaction
clustermgr etcd defrag -a
clustermgr etcd alarms  # Should be clear
```

### Leader Instability (high raft term)
```bash
clustermgr etcd status  # Check raft terms
clustermgr etcd health  # Check all members healthy
# If network issues, check:
clustermgr wg status
clustermgr mesh-test
```

## Related Commands

- `maintenance rolling-restart -t server` - Restart K3s servers safely
- `health` - Overall cluster health including etcd
- `diagnose` - Deep diagnostics

## Related Runbooks

- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - K3s server maintenance
