# Flannel Commands

Flannel VXLAN overlay network diagnostics for troubleshooting pod-to-pod connectivity.

## Overview

The `flannel` command group provides tools for diagnosing Flannel overlay network issues. Flannel is the CNI (Container Network Interface) plugin that provides pod networking in K3s, using VXLAN encapsulation to tunnel traffic between nodes.

## Why These Commands Exist

HTTP 503 errors for UserDeployments are often caused by Flannel VXLAN routing failures. When Envoy pods on K3s agents try to reach user pods on GPU nodes, the traffic must:

1. Be encapsulated in VXLAN by the local flannel.1 interface
2. Routed through WireGuard to the GPU node
3. Decapsulated and delivered to the pod

If any component (FDB entries, neighbor entries, routes) is missing or incorrect, traffic fails with 503 errors.

## Background: Flannel VXLAN Architecture

```
K3s Agent (Envoy)              GPU Node (User Pod)
+------------------+           +------------------+
|  Pod: Envoy      |           |  Pod: user-app   |
|  10.42.1.5       |           |  10.42.15.10     |
+--------+---------+           +--------+---------+
         |                              |
+--------+---------+           +--------+---------+
|  flannel.1       |           |  flannel.1       |
|  MAC: aa:bb:cc   |  VXLAN    |  MAC: dd:ee:ff   |
+--------+---------+  ======>  +--------+---------+
         |                              |
+--------+---------+           +--------+---------+
|  wg0 (WireGuard) |           |  wg0 (WireGuard) |
|  10.200.0.2      |           |  10.200.0.15     |
+------------------+           +------------------+
```

**Key components:**
- **flannel.1**: VXLAN tunnel interface
- **FDB (Forwarding Database)**: Maps MAC addresses to destination IPs
- **Neighbor/ARP entries**: Maps VTEP IPs to MAC addresses
- **Routes**: Directs pod CIDR traffic through flannel.1

## Commands

### flannel status

**What it does:** Shows the flannel.1 interface status on all K3s servers.

**How it works:**
1. Queries `ip link show flannel.1` on each server via Ansible
2. Reads interface statistics from `/sys/class/net/flannel.1/statistics/`
3. Parses MAC address, MTU, state, and packet counters

**When to use:** First step in Flannel troubleshooting, or to check for dropped packets.

```bash
clustermgr flannel status
```

**Output:**
```
=== Flannel Interface Status ===
| Node     | State | MAC               | MTU  | RX Bytes | TX Bytes | Dropped |
|----------|-------|-------------------|------|----------|----------|---------|
| server1  | UP    | 7e:3a:2b:4c:5d:6e | 1450 | 1.2G     | 890.5M   | 0       |
| server2  | UP    | 8f:4b:3c:5d:6e:7f | 1450 | 1.1G     | 920.1M   | 12      |
| server3  | UP    | 9g:5c:4d:6e:7f:8g | 1450 | 1.3G     | 875.2M   | 0       |
```

**What to look for:**
- State should be UP on all servers
- Dropped packets > 1000 indicates issues
- MTU should be consistent (typically 1450 for VXLAN over WireGuard)

---

### flannel fdb

**What it does:** Inspects FDB (Forwarding Database) entries for VXLAN tunneling.

**How it works:**
1. Gets GPU node information from K8s API (including Flannel MAC annotations)
2. Queries `bridge fdb show dev flannel.1` on each server
3. Matches FDB entries to known GPU nodes
4. Reports missing entries

**When to use:** When HTTP 503 errors occur, to verify FDB entries exist for GPU nodes.

```bash
clustermgr flannel fdb

# Filter by server
clustermgr flannel fdb -n server1
```

**Output:**
```
=== Flannel FDB Entries ===

=== GPU Nodes (Expected FDB Entries) ===
  gpu-node-abc: MAC=dd:ee:ff:11:22:33, WG=10.200.0.15

=== FDB on server1 ===
| MAC               | Destination  | GPU Node     | Permanent |
|-------------------|--------------|--------------|-----------|
| dd:ee:ff:11:22:33 | 10.200.0.15  | gpu-node-abc | Yes       |
| 00:00:00:00:00:00 | 10.200.0.15  | gpu-node-abc | Yes       |

=== Missing FDB Entries ===
  All GPU nodes have FDB entries
```

**What to look for:**
- Each GPU node should have an FDB entry
- FDB should map GPU node's Flannel MAC to its WireGuard IP
- Missing entries cause 503 errors

---

### flannel neighbors

**What it does:** Checks neighbor/ARP entries for VTEP (VXLAN Tunnel Endpoint) IPs.

**How it works:**
1. Gets GPU node pod CIDRs from K8s API
2. Calculates VTEP IPs (e.g., 10.42.15.0 for pod CIDR 10.42.15.0/24)
3. Queries `ip neigh show dev flannel.1` on each server
4. Matches entries to GPU nodes
5. Reports missing entries

**When to use:** When FDB entries exist but traffic still fails.

```bash
clustermgr flannel neighbors

# Filter by server
clustermgr flannel neighbors -n server1
```

**Output:**
```
=== Flannel Neighbor Entries ===

=== Expected VTEP Entries (GPU Nodes) ===
  10.42.15.0 -> dd:ee:ff:11:22:33 (gpu-node-abc)

=== Neighbors on server1 ===
| VTEP IP     | MAC               | State     | GPU Node     |
|-------------|-------------------|-----------|--------------|
| 10.42.15.0  | dd:ee:ff:11:22:33 | PERMANENT | gpu-node-abc |

=== Missing Neighbor Entries ===
  All GPU nodes have neighbor entries
```

**What to look for:**
- Each GPU node's VTEP IP should have a neighbor entry
- MAC should match the GPU node's flannel.1 MAC
- State should be PERMANENT (not STALE or REACHABLE)

---

### flannel routes

**What it does:** Verifies routes for GPU node pod CIDRs through flannel.1.

**How it works:**
1. Gets GPU node pod CIDRs from K8s API
2. Queries `ip route show | grep flannel.1` on each server
3. Matches routes to expected pod CIDRs
4. Reports missing routes

**When to use:** When FDB and neighbor entries exist but traffic still fails.

```bash
clustermgr flannel routes

# Filter by server
clustermgr flannel routes -n server1
```

**Output:**
```
=== Flannel Routes ===

=== Expected Routes (GPU Node Pod CIDRs) ===
  10.42.15.0/24 -> gpu-node-abc

=== Routes on server1 ===
| Pod CIDR       | Via         | Device    | Onlink |
|----------------|-------------|-----------|--------|
| 10.42.15.0/24  | 10.42.15.0  | flannel.1 | Yes    |

=== Missing Routes ===
  All GPU node pod CIDRs have routes
```

**What to look for:**
- Each GPU node's pod CIDR should have a route via flannel.1
- The `onlink` flag should be present
- Via address should be the VTEP IP (first IP of pod CIDR)

---

### flannel test

**What it does:** Tests VXLAN connectivity to GPU nodes by pinging VTEP IPs.

**How it works:**
1. Gets GPU node pod CIDRs from K8s API
2. Calculates VTEP IPs
3. Pings each VTEP IP from first K3s server
4. Reports success/failure and latency

**When to use:** After verifying FDB, neighbors, and routes; to confirm end-to-end connectivity.

```bash
clustermgr flannel test

# Test specific GPU node
clustermgr flannel test -g gpu-node-abc
```

**Output:**
```
=== Flannel VXLAN Connectivity Test ===
Testing connectivity to 2 GPU node(s)...

gpu-node-abc (10.42.15.0)     2.5ms
gpu-node-def (10.42.16.0)     UNREACHABLE
```

**What to look for:**
- All GPU nodes should be reachable
- Latency should be low (< 10ms typically)
- UNREACHABLE indicates missing FDB/neighbor/route

---

### flannel diagnose

**What it does:** Comprehensive Flannel health check combining all diagnostics.

**How it works:**
1. Checks flannel.1 interface status on all servers
2. Verifies GPU node Flannel annotations
3. Checks FDB entries for all GPU nodes
4. Checks neighbor entries for all VTEP IPs
5. Checks routes for all pod CIDRs
6. Reports all issues found

**When to use:** Quick triage of Flannel issues, or regular health checks.

```bash
clustermgr flannel diagnose
```

**Output:**
```
=== Flannel Comprehensive Diagnostics ===
Checking flannel.1 interfaces...
Checking GPU node information...
Checking FDB entries...
Checking neighbor entries...
Checking routes...

=== Diagnostic Summary ===
Found 2 issue(s): 2 critical, 0 warnings

| Node         | Issue                               | Severity |
|--------------|-------------------------------------|----------|
| gpu-node-def | Missing FDB entry for MAC dd:ee:ff  | critical |
| gpu-node-def | Missing neighbor entry for 10.42.16.0| critical |

=== Remediation Steps ===
Run 'clustermgr fix' to attempt automatic remediation
Or manually fix using:
  - Missing FDB: bridge fdb add <MAC> dev flannel.1 dst <WG_IP>
  - Missing neighbor: ip neigh add <VTEP_IP> lladdr <MAC> dev flannel.1
  - Missing route: ip route add <POD_CIDR> via <VTEP_IP> dev flannel.1 onlink
```

---

### flannel mac-duplicates

**What it does:** Detects duplicate VtepMAC addresses across GPU nodes.

**How it works:**
1. Gets Flannel MAC annotations from all GPU nodes
2. Groups nodes by MAC address
3. Reports any MAC used by multiple nodes

**When to use:** When seeing intermittent connectivity issues, or after onboarding nodes.

```bash
clustermgr flannel mac-duplicates
```

**Output (no duplicates):**
```
=== Duplicate VtepMAC Detection ===
No duplicate MACs found among 45 GPU nodes
```

**Output (duplicates found):**
```
=== Duplicate VtepMAC Detection ===
Found 1 duplicate MAC(s)!

=== Duplicate MAC: dd:ee:ff:11:22:33 ===
| Node         | WireGuard IP | Pod CIDR       |
|--------------|--------------|----------------|
| gpu-node-abc | 10.200.0.15  | 10.42.15.0/24  |
| gpu-node-xyz | 10.200.0.42  | 10.42.42.0/24  |

=== Resolution ===
For each conflicting node (except one), regenerate the VtepMAC:

  1. Generate deterministic MAC from node name:
     NODE_NAME=$(hostname)
     HASH=$(echo -n "$NODE_NAME" | sha256sum | cut -c1-10)
     NEW_MAC=$(printf "02:%s:%s:%s:%s:%s" ...)

  2. Recreate flannel.1 with new MAC
  3. Update K8s node annotation
  4. Update FDB/neighbor entries on K3s servers

See FLANNEL-VXLAN-TROUBLESHOOTING.md for detailed steps
```

**Why duplicates are bad:**
- VXLAN traffic gets routed to wrong node
- Intermittent failures (works when packet goes to right node)
- Very hard to debug without this check

---

### flannel capture

**What it does:** Captures packets on flannel.1 for debugging.

**How it works:**
1. Runs tcpdump on specified interface via Ansible
2. Captures specified number of packets
3. Displays colorized output

**When to use:** Deep debugging when higher-level checks pass but traffic still fails.

```bash
# Capture 50 packets on flannel.1
clustermgr flannel capture

# Capture with filter
clustermgr flannel capture -f "host 10.42.15.0"

# Capture on specific interface
clustermgr flannel capture -i wg0

# Capture on specific server
clustermgr flannel capture -s server2
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-i, --interface` | flannel.1 | Interface to capture on |
| `-c, --count` | 50 | Number of packets |
| `-f, --filter` | "" | tcpdump filter expression |
| `-s, --server` | k3s_server[0] | Server to run capture on |

---

### flannel vxlan-capture

**What it does:** Captures VXLAN-encapsulated traffic on wg0 (UDP port 8472).

**How it works:**
1. Runs tcpdump on wg0 filtering for UDP port 8472
2. Shows VXLAN encapsulated packets traversing WireGuard

**When to use:** To verify VXLAN traffic is actually being sent/received over WireGuard.

```bash
clustermgr flannel vxlan-capture

# More packets
clustermgr flannel vxlan-capture -c 50
```

## Troubleshooting Workflow

Complete workflow for HTTP 503 debugging:

```bash
# 1. Quick health check
clustermgr flannel diagnose

# 2. If issues found, check details
clustermgr flannel fdb
clustermgr flannel neighbors
clustermgr flannel routes

# 3. Test connectivity
clustermgr flannel test

# 4. Check for duplicate MACs
clustermgr flannel mac-duplicates

# 5. Auto-fix if possible
clustermgr fix --dry-run
clustermgr fix

# 6. Deep debugging if needed
clustermgr flannel capture -f "host 10.42.15.0"
```

## Related Commands

- `fix` - Auto-fix missing FDB/neighbor/route entries
- `wg status` - WireGuard connectivity (underlying transport)
- `envoy test` - Test HTTP connectivity through Envoy

## Related Runbooks

- `docs/runbooks/FLANNEL-VXLAN-TROUBLESHOOTING.md` - Detailed troubleshooting guide
- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP 503 diagnosis workflow
