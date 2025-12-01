# Scaling Commands

Cluster capacity analysis and scaling readiness diagnostics.

## Overview

The `scaling` command group provides visibility into cluster capacity, resource utilization, and scaling limits. It helps operators understand when the cluster is approaching limits and what actions to take.

## Why These Commands Exist

As the cluster grows with more GPU nodes and workloads, operators need to:
1. Monitor resource utilization against known limits
2. Identify bottlenecks before they cause issues
3. Plan for capacity expansion
4. Validate performance baselines

These commands codify the limits and thresholds from the NETWORK-SCALING-GUIDE.md runbook into automated checks.

## Architecture Limits

The commands use these documented limits:

| Resource | Limit | Rationale |
|----------|-------|-----------|
| K3s Servers | 7 | etcd quorum and performance |
| K3s Agents | 100 | VPC-local compute capacity |
| GPU Nodes | 250 | WireGuard peer limit per server |
| Pods per Node | 110 | Kubernetes default |
| Total Pods | 10,000 | Cluster-wide limit |
| Pod CIDR Nodes | 256 | /24 subnets available in /16 |
| WireGuard Peers | 250 | Performance limit per interface |

## Commands

### scaling capacity

**What it does:** Shows current cluster capacity metrics compared to configured limits.

**How it works:**
1. Queries K8s API for all nodes, categorizing by type
2. Counts pods across the cluster
3. Queries WireGuard peer count via Ansible
4. Counts FDB entries for Flannel VXLAN
5. Calculates utilization percentages against limits

**When to use:** Regular capacity planning, before onboarding new GPU nodes, or when investigating performance issues.

```bash
clustermgr scaling capacity
```

**Output:**
```
=== Cluster Capacity ===
                   Node Capacity
| Resource        | Current | Limit | Utilization |
|-----------------|---------|-------|-------------|
| K3s Servers     | 3       | 7     | 43%         |
| K3s Agents      | 5       | 100   | 5%          |
| GPU Nodes       | 45      | 250   | 18%         |
| WireGuard Peers | 45      | 250   | 18%         |
| Pod CIDRs       | 53      | 256   | 21%         |

Total Nodes: 53
Total Pods: 1,250 / 5,830 estimated capacity
FDB Entries: 98
```

**Color coding:**
- Green: < 60% utilization
- Yellow: 60-80% utilization
- Red: > 80% utilization

---

### scaling readiness

**What it does:** Analyzes current capacity and generates scaling recommendations.

**How it works:**
1. Gathers capacity metrics (same as `capacity` command)
2. Applies rules to identify potential issues:
   - GPU nodes > 80% of WireGuard limit: Critical
   - GPU nodes > 60% of limit: Warning
   - Large cluster (>50 GPU nodes) without enough servers: Warning
   - Pod CIDR utilization > 80%: Warning
   - Pod capacity > 70%: Warning
3. Generates actionable recommendations

**When to use:** Before planning scaling operations, during capacity reviews, or when alerts indicate resource pressure.

```bash
clustermgr scaling readiness
```

**Output:**
```
=== Scaling Readiness Analysis ===
| Category         | Status                              | Recommended Action                    |
|------------------|-------------------------------------|---------------------------------------|
| GPU Nodes        | At 82% of WireGuard peer limit      | Consider sharding into multiple       |
|                  | (205/250)                           | clusters or hub-spoke WireGuard       |
| K3s Servers      | Only 3 servers for 205 GPU nodes    | Consider adding K3s server for        |
|                  |                                     | redundancy and load distribution      |
| Reconcile        | Large cluster detected              | Consider increasing reconcile         |
| Interval         |                                     | interval to 300s to reduce API load   |
```

**Recommendations generated:**
- Cluster sharding for WireGuard limits
- Adding K3s servers for large clusters
- Reconcile interval tuning for >50 GPU nodes
- Pod CIDR expansion planning

---

### scaling limits

**What it does:** Displays the configured architecture limits and alert thresholds.

**How it works:** Simply displays the hardcoded limits from the scaling guide - no cluster queries needed.

**When to use:** Reference when planning capacity, or to understand what limits apply.

```bash
clustermgr scaling limits
```

**Output:**
```
=== Architecture Limits ===
| Resource        | Limit | Notes                 |
|-----------------|-------|-----------------------|
| K3s Servers     | 7     | etcd quorum limit     |
| K3s Agents      | 100   | VPC-local compute     |
| GPU Nodes       | 250   | WireGuard peer limit  |
| Pods per Node   | 110   | K8s default limit     |
| Total Pods      | 10000 | Cluster-wide limit    |
| Pod CIDR Nodes  | 256   | /24 subnets in /16    |
| WireGuard Peers | 250   | Per server limit      |

=== Alert Thresholds ===
| Metric            | Threshold | Unit         |
|-------------------|-----------|--------------|
| WireGuard Latency | 20        | ms (warning) |
| Handshake Stale   | 180       | seconds      |
| FDB Entries Min   | 2         | entries      |
```

---

### scaling baselines

**What it does:** Measures current performance metrics and compares against expected baselines.

**How it works:**
1. Measures WireGuard latency via ping to first peer
2. Checks conntrack table size (`net.netfilter.nf_conntrack_max`)
3. Checks network buffer sizes (`net.core.rmem_max`)
4. Compares against expected values from scaling guide

**When to use:** After cluster setup to verify tuning, during performance issues, or as part of regular health checks.

```bash
clustermgr scaling baselines
```

**Output:**
```
=== Performance Baselines ===
| Metric           | Value        | Status |
|------------------|--------------|--------|
| WireGuard Latency| 2.5ms        | OK     |
| Conntrack Max    | 1,048,576    | OK     |
| rmem_max         | 67,108,864   | OK     |
```

**Expected values:**
- WireGuard latency: < 20ms
- Conntrack max: >= 1,048,576
- rmem_max: >= 67,108,864

**If values are below expected:**
```
=== Tuning Recommendations ===
See NETWORK-SCALING-GUIDE.md for performance tuning instructions
Run: clustermgr scaling limits
```

## Capacity Planning Workflow

```bash
# 1. Check current utilization
clustermgr scaling capacity

# 2. Review limits
clustermgr scaling limits

# 3. Get recommendations
clustermgr scaling readiness

# 4. Verify performance baselines
clustermgr scaling baselines

# 5. If adding nodes, verify maintenance capacity
clustermgr maintenance status
```

## Scaling Thresholds

When utilization exceeds thresholds, consider these actions:

| Resource | 60% | 80% | Action |
|----------|-----|-----|--------|
| GPU Nodes | Plan | Urgent | Cluster sharding or hub-spoke topology |
| K3s Servers | Plan | Add | Add servers for redundancy |
| Pod CIDRs | Plan | Urgent | Expand pod CIDR or shard cluster |
| Pods | Monitor | Plan | Add nodes or optimize pod density |

## Related Commands

- `maintenance status` - Node availability for workload redistribution
- `health` - Quick cluster health check
- `wg peers` - WireGuard peer status

## Related Runbooks

- `docs/runbooks/NETWORK-SCALING-GUIDE.md` - Detailed scaling procedures
- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Maintenance during scaling
