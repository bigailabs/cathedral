# Network Commands

Network topology, firewall auditing, MTU validation, and connectivity testing.

## Overview

The network command group provides tools for understanding cluster network architecture, validating configuration, and testing connectivity. These commands work across all K3s servers and WireGuard peers.

## Why These Commands Exist

Network issues are the root cause of many cluster problems:
- HTTP 503 errors from unreachable pods
- Slow API responses from high latency paths
- Dropped connections from MTU mismatches
- Blocked traffic from firewall rules

These commands provide visibility into network state and help identify configuration issues before they cause production problems.

## Commands

### topology

**What it does:** Discovers and displays the complete cluster network topology including all nodes, WireGuard peers, and their health status.

**How it works:**
1. Parses the Ansible inventory file to identify K3s servers
2. Executes `wg show wg0 dump` on each server to get peer information
3. Reads `/etc/wireguard/wg0.conf` for peer names from comments
4. Collects interface statistics from `/sys/class/net/wg0/statistics/`
5. Checks iptables for DROP rule counters
6. Calculates health based on handshake age, errors, and drops

**When to use:**
- Initial cluster exploration to understand the network layout
- Quick health overview across all nodes
- Identifying which peers are connected to which servers
- Troubleshooting connectivity issues

```bash
# Full topology with all views
clustermgr topology

# Tree view only
clustermgr topology -f tree

# Table view with detailed stats
clustermgr topology -f table

# Connection matrix showing peer relationships
clustermgr topology -f matrix
```

**Output formats:**

| Format | Description |
|--------|-------------|
| `tree` | Hierarchical view showing control plane and workers |
| `table` | Detailed stats per node and peer |
| `matrix` | Shows which nodes are peered with which |
| `all` | All formats (default) |

**Health indicators:**

| Symbol | Meaning |
|--------|---------|
| `[OK]` | Healthy: handshake < 3 minutes, no errors |
| `[!]` | Warning: handshake 3-5 minutes or minor errors |
| `[X]` | Critical: handshake > 5 minutes or major issues |

**What to look for:**
- Handshake age should be within 2-3 minutes for active peers
- Interface errors indicate packet issues
- IPTables drops indicate rate limiting
- All peers should show traffic flowing (RX/TX > 0)

---

### firewall

**What it does:** Audits iptables rules for potential issues that could block cluster traffic.

**How it works:**
1. Executes `iptables -S` on all K3s servers via Ansible
2. Scans rules against known problematic patterns
3. Checks if required ports are explicitly allowed
4. Reports DROP rule packet counters

**When to use:**
- Troubleshooting WireGuard connectivity issues
- Verifying firewall configuration after changes
- Investigating dropped packet reports
- Regular security audits

```bash
# Basic audit
clustermgr firewall

# Check required ports are allowed
clustermgr firewall -p

# Show all rules
clustermgr firewall -r

# Show DROP counters
clustermgr firewall -d

# Full audit with all checks
clustermgr firewall -p -r -d
```

**Options:**

| Option | Description |
|--------|-------------|
| `-p, --check-ports` | Check if required ports are allowed |
| `-r, --show-rules` | Show all iptables rules |
| `-d, --drops` | Show DROP rule packet counters |

**What it checks:**

| Pattern | Severity | Description |
|---------|----------|-------------|
| WireGuard rate-limit DROP | Critical | Blocks WireGuard handshakes |
| Default DROP on INPUT | Warning | May block legitimate traffic |
| Default DROP on FORWARD | Warning | May block pod-to-pod traffic |

**Required ports:**

| Port | Protocol | Service |
|------|----------|---------|
| 6443 | TCP | Kubernetes API server |
| 10250 | TCP | Kubelet API |
| 51820 | UDP | WireGuard VPN |
| 8472 | UDP | Flannel VXLAN |
| 2379 | TCP | etcd clients |
| 2380 | TCP | etcd peers |

---

### mtu

**What it does:** Validates MTU settings across all network interfaces to ensure proper packet sizing.

**How it works:**
1. Executes `ip -o link show` on all servers
2. Compares actual MTU to expected values for each interface type
3. Optionally tests path MTU using ICMP with DF (Don't Fragment) bit set

**When to use:**
- Diagnosing fragmentation issues
- Verifying MTU configuration after network changes
- Troubleshooting packet loss on specific paths
- Validating VXLAN/WireGuard stack configuration

```bash
# Validate MTU settings
clustermgr mtu

# Show all interfaces (not just key ones)
clustermgr mtu -v

# Test path MTU to specific IP
clustermgr mtu -t 10.200.0.10

# Test with specific MTU size
clustermgr mtu -t 10.200.0.10 -m 1400
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-t, --test-path` | - | Test path MTU to specific IP |
| `-m, --mtu-size` | 1392 | MTU size to test |
| `-v, --verbose` | false | Show all interfaces |

**Expected MTU values:**

| Interface | MTU | Reason |
|-----------|-----|--------|
| eth0/ens* | 1500 | Physical interface |
| wg0 | 1420 | WireGuard (1500 - 80 overhead) |
| flannel.1 | 1370 | VXLAN over WireGuard (1420 - 50) |
| cni0 | 1370 | CNI bridge |
| veth* | 1370 | Pod interfaces |

**MTU stack:**
```
eth0: 1500 (physical)
  |
wg0: 1420 (WireGuard adds 80 bytes overhead)
  |
flannel.1: 1370 (VXLAN adds 50 bytes overhead)
  |
pod: 1370 (inherits from CNI)
```

---

### mesh-test

**What it does:** Tests WireGuard connectivity from each server to its configured peers using ICMP ping.

**How it works:**
1. Gets WireGuard IPs for all Ansible-managed servers
2. On each server, discovers actual WireGuard peers from config
3. Pings each peer from each server
4. Extracts latency and packet loss from ping output
5. Reports connectivity status, failures, and high latency paths

**When to use:**
- Regular connectivity verification
- After WireGuard configuration changes
- Investigating intermittent connectivity
- Before and after maintenance operations

```bash
# Basic mesh test
clustermgr mesh-test

# More pings for reliability
clustermgr mesh-test -c 10

# Show connectivity matrix
clustermgr mesh-test -m

# Verbose output with full table
clustermgr mesh-test -v
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-c, --count` | 3 | Number of pings per target |
| `-m, --matrix` | false | Show results as matrix |
| `-v, --verbose` | false | Show detailed results table |

**Output:**
```
=== Full Mesh Connectivity Test ===
Testing from 3 servers to their WireGuard peers...
  server1: 10.200.0.1
  server2: 10.200.0.2
  server3: 10.200.0.3

Running ping tests (3 packets each)...

Results: 15/15 paths OK, 0 failed
Average latency: 2.3ms

=== Connectivity Matrix ===
From \ To     server2     server3     gpu-node-1
server1       2.1ms       2.4ms       5.2ms
server2       --          2.2ms       5.5ms
server3       2.3ms       --          5.8ms
```

**What to look for:**
- All paths should show OK or latency value
- FAIL indicates no connectivity
- High latency (>50ms) may indicate network issues
- Packet loss indicates unstable connection

---

### latency-matrix

**What it does:** Measures detailed network latency statistics from each server to its WireGuard peers.

**How it works:**
1. Gets WireGuard IPs for all servers
2. On each server, discovers actual WireGuard peers
3. Runs multiple pings with faster interval for statistics
4. Extracts min/avg/max/stddev from ping output
5. Displays as matrix or detailed table

**When to use:**
- Performance baseline measurement
- Investigating slow API responses
- Identifying network bottlenecks
- Comparing latency before/after changes

```bash
# Latency matrix (default 10 pings)
clustermgr latency-matrix

# More samples for accuracy
clustermgr latency-matrix -c 50

# Detailed table view
clustermgr latency-matrix -t

# Custom warning threshold
clustermgr latency-matrix --threshold 30
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-c, --count` | 10 | Number of pings per path |
| `-t, --table` | false | Show detailed table instead of matrix |
| `--threshold` | 50 | Latency threshold for warnings (ms) |

**Output (matrix):**
```
=== Latency Matrix ===
            server2     server3     gpu-node-1
server1       2.1         2.4         5.2
server2       --          2.2         5.5
server3       2.3         --          5.8

Values in milliseconds (avg RTT)
```

**Output (table):**
```
=== Detailed Latency Table ===
| Source  | Target     | Min    | Avg    | Max    | StdDev | Loss |
|---------|------------|--------|--------|--------|--------|------|
| server1 | server2    | 1.8ms  | 2.1ms  | 2.5ms  | 0.2ms  | -    |
| server1 | server3    | 2.0ms  | 2.4ms  | 3.1ms  | 0.3ms  | -    |
| server1 | gpu-node-1 | 4.8ms  | 5.2ms  | 6.1ms  | 0.4ms  | -    |
```

**What to look for:**
- Latency should be consistent (low stddev)
- High stddev relative to avg indicates jitter
- Increasing latency over time indicates congestion
- Asymmetric latency may indicate routing issues

## Troubleshooting Workflow

Network issue investigation:

```bash
# 1. Get overview of topology
clustermgr topology

# 2. Check for firewall issues
clustermgr firewall -p -d

# 3. Validate MTU settings
clustermgr mtu

# 4. Test connectivity
clustermgr mesh-test -m

# 5. Measure latency baselines
clustermgr latency-matrix -t
```

## Common Issues

### High Latency to GPU Nodes

```bash
# Check latency
clustermgr latency-matrix -c 50

# Verify WireGuard handshakes
clustermgr wg handshakes

# Check for dropped packets
clustermgr topology -f table
```

### Firewall Blocking Traffic

```bash
# Audit firewall
clustermgr firewall -p -d

# If rate limit rules found
clustermgr fix --dry-run
clustermgr fix
```

### MTU Fragmentation Issues

```bash
# Check MTU settings
clustermgr mtu -v

# Test path MTU
clustermgr mtu -t 10.42.15.0 -m 1370
```

### Intermittent Connectivity

```bash
# Extended mesh test
clustermgr mesh-test -c 100 -v

# Check for packet loss patterns
clustermgr latency-matrix -c 100 -t
```

## Related Commands

- `wg status` - WireGuard-specific diagnostics
- `flannel diagnose` - Flannel overlay diagnostics
- `health` - Overall cluster health
- `diagnose` - Comprehensive diagnostics

## Related Runbooks

- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP error troubleshooting
- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Network maintenance
