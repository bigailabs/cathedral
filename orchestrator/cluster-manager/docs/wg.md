# WireGuard Commands

WireGuard VPN management and monitoring for K3s cluster networking.

## Overview

The `wg` command group provides tools for managing and monitoring the WireGuard VPN that connects GPU nodes to the K3s cluster. WireGuard forms the secure transport layer for all traffic between K3s servers and remote GPU nodes.

## Why These Commands Exist

WireGuard connectivity issues cause:
- GPU nodes becoming unreachable
- Pod scheduling failures on GPU nodes
- HTTP 503 errors for workloads on GPU nodes
- Stale handshakes leading to intermittent failures

These commands provide visibility into WireGuard state and automate common maintenance tasks.

## Background: WireGuard in Basilica

```
K3s Servers                     GPU Nodes
+---------------+              +---------------+
| k3s-server-1  |              | gpu-node-1    |
| wg0: 10.200.0.1|<============>| wg0: 10.200.0.10
+---------------+   WireGuard  +---------------+
| k3s-server-2  |   UDP 51820  | gpu-node-2    |
| wg0: 10.200.0.2|<============>| wg0: 10.200.0.11
+---------------+              +---------------+
```

**Key concepts:**
- **Peers**: Remote endpoints (GPU nodes are peers of K3s servers)
- **AllowedIPs**: IP ranges routed through the tunnel (includes pod CIDRs)
- **Handshake**: Periodic key exchange (every 2 minutes when active)
- **Endpoint**: Public IP:port of the remote peer

## Commands

### wg status

**What it does:** Shows WireGuard interface status on all K3s servers.

**How it works:**
1. Executes `wg show wg0` on each server via Ansible
2. Parses interface config and peer information
3. Displays formatted output with connection status

**When to use:** Quick check of WireGuard connectivity, or initial troubleshooting.

```bash
clustermgr wg status
```

**Output:**
```
=== WireGuard Status ===

server1
  interface: wg0

  peer: abc123def456...
    allowed ips: 10.200.0.10/32, 10.42.15.0/24
    latest handshake: 45 seconds ago
    transfer: 1.2 GiB received, 890 MiB sent

  peer: ghi789jkl012...
    allowed ips: 10.200.0.11/32, 10.42.16.0/24
    latest handshake: 1 minute, 23 seconds ago
    transfer: 2.1 GiB received, 1.5 GiB sent
```

**What to look for:**
- Handshake should be within last 2-3 minutes for active peers
- AllowedIPs should include both WireGuard IP and pod CIDR
- Transfer counters show traffic is flowing

---

### wg peers

**What it does:** Lists WireGuard peers with health metrics and status indicators.

**How it works:**
1. Calls internal `check_wireguard_peers()` function
2. Parses peer information from all servers
3. Calculates handshake staleness
4. Displays with health indicators

**When to use:** To quickly identify peers with stale handshakes.

```bash
clustermgr wg peers
```

**Output:**
```
=== WireGuard Peers ===

server1:
  abc123def456...
    IPs: 10.200.0.10/32, 10.42.15.0/24
    Handshake: 45 seconds ago [OK]
  ghi789jkl012...
    IPs: 10.200.0.11/32, 10.42.16.0/24
    Handshake: 5 minutes ago [STALE]
```

**Handshake status:**
- **OK**: Handshake within last 3 minutes
- **STALE**: Handshake older than 3 minutes (may indicate issues)

---

### wg restart

**What it does:** Restarts WireGuard service on specified nodes.

**How it works:**
1. Executes `systemctl restart wg-quick@wg0` via Ansible
2. Waits for service to restart
3. Shows brief status after restart

**When to use:** When peers have stale handshakes, or after configuration changes.

```bash
# Restart on all servers
clustermgr wg restart

# Restart on specific nodes
clustermgr wg restart -n server1,server2
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-n, --nodes` | all | Comma-separated node names |

**Caution:**
- Briefly interrupts VPN connectivity (1-2 seconds)
- Requires confirmation unless `-y` specified

---

### wg reconcile

**What it does:** Checks and fixes WireGuard AllowedIPs to include GPU node pod CIDRs.

**How it works:**
1. Gets GPU nodes with pod CIDRs from K8s API
2. Gets current WireGuard peer configuration
3. Checks if pod CIDRs are in AllowedIPs for each peer
4. Checks if routes exist for pod CIDRs
5. Optionally fixes missing entries

**When to use:** When GPU nodes are reachable but pod traffic fails, or after new GPU nodes join.

```bash
# Check reconciliation status
clustermgr wg reconcile

# Fix missing pod CIDRs
clustermgr wg reconcile --fix
```

**Output (check only):**
```
=== WireGuard Peer Reconciliation ===
Found 2 GPU node(s) with pod CIDRs

| Node         | WG IP       | Pod CIDR      | In AllowedIPs | Route Exists | Status     |
|--------------|-------------|---------------|---------------|--------------|------------|
| gpu-node-abc | 10.200.0.10 | 10.42.15.0/24 | Yes           | Yes          | OK         |
| gpu-node-def | 10.200.0.11 | 10.42.16.0/24 | No            | No           | Needs fix  |

1 peer(s) need reconciliation

Run 'clustermgr wg reconcile --fix' to apply fixes
```

**What it fixes:**
- Adds pod CIDR to WireGuard AllowedIPs
- Adds route for pod CIDR via wg0
- Saves WireGuard config (`wg-quick save wg0`)

---

### wg keys

**What it does:** Shows WireGuard key information for rotation planning.

**How it works:**
1. Reads public key from `/etc/wireguard/public.key`
2. Checks creation date of private key file
3. Checks if backup key exists

**When to use:** Before key rotation, to understand current key state.

```bash
clustermgr wg keys
```

**Output:**
```
=== WireGuard Key Status ===
| Server  | Public Key (truncated)  | Key Created | Backup |
|---------|-------------------------|-------------|--------|
| server1 | abc123def456ghi789...   | 2024-06-15  | No     |
| server2 | jkl012mno345pqr678...   | 2024-06-15  | No     |
| server3 | stu901vwx234yza567...   | 2024-06-15  | No     |

=== Key Rotation Recommendations ===
Keys should be rotated quarterly. See NETWORK-MAINTENANCE-PROCEDURES.md

Before rotation:
  1. Schedule maintenance window
  2. Generate new keys on all servers
  3. Coordinate cutover across all servers and GPU nodes
  4. Update GPU node configs via API
  5. Verify connectivity after rotation
```

---

### wg handshakes

**What it does:** Checks handshake ages across all WireGuard peers.

**How it works:**
1. Executes `wg show wg0 dump` on each server
2. Parses last handshake timestamps
3. Calculates age from current time
4. Reports stale handshakes

**When to use:** To identify peers that may be offline or having connectivity issues.

```bash
# Default threshold: 180 seconds
clustermgr wg handshakes

# Custom threshold
clustermgr wg handshakes -t 300
```

**Options:**
| Option | Default | Description |
|--------|---------|-------------|
| `-t, --stale-threshold` | 180 | Seconds before handshake is stale |

**Output:**
```
=== WireGuard Handshake Status ===
Total peers: 45
Healthy: 43
Stale (>180s): 2

| Server  | Peer            | Handshake Age |
|---------|-----------------|---------------|
| server1 | abc123def456... | Never         |
| server2 | ghi789jkl012... | 5m 23s        |

Stale handshakes may indicate:
  - GPU node is offline
  - Network path is blocked
  - WireGuard needs restart

Run 'clustermgr wg restart' to restart WireGuard service
```

**Handshake states:**
- **Never**: Peer configured but never connected
- **Age shown**: Time since last successful handshake

## Troubleshooting Workflow

Complete workflow for WireGuard issues:

```bash
# 1. Check overall status
clustermgr wg status

# 2. Identify stale peers
clustermgr wg handshakes

# 3. Check peer reconciliation
clustermgr wg reconcile

# 4. If peers missing pod CIDRs
clustermgr wg reconcile --fix

# 5. If peers still stale, restart WireGuard
clustermgr wg restart

# 6. Verify recovery
clustermgr wg peers
```

## Key Rotation Workflow

Quarterly key rotation procedure:

```bash
# 1. Check current key state
clustermgr wg keys

# 2. During maintenance window:
#    - Generate new keys on all servers
#    - Update GPU node configurations via API
#    - Restart WireGuard on servers

# 3. Verify connectivity
clustermgr wg handshakes
clustermgr wg peers

# 4. Monitor for issues
clustermgr health
```

## Common Issues

### Stale Handshakes
```bash
clustermgr wg handshakes  # Identify stale peers
clustermgr wg restart     # Restart WireGuard
```

### Missing Pod CIDRs in AllowedIPs
```bash
clustermgr wg reconcile        # Check status
clustermgr wg reconcile --fix  # Fix automatically
```

### Peer Never Connected
1. Check GPU node is online
2. Check firewall allows UDP 51820
3. Check GPU node has correct server endpoint
4. Check public key matches

## Related Commands

- `flannel diagnose` - Flannel overlay (runs on top of WireGuard)
- `mesh-test` - Test connectivity between all nodes
- `health` - Overall cluster health including WireGuard

## Related Runbooks

- `docs/runbooks/WIREGUARD-TROUBLESHOOTING.md` - WireGuard troubleshooting
- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Key rotation procedures
