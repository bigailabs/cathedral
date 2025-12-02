# WireGuard and Flannel Reconciliation Architecture

This document describes the reconciliation mechanisms that maintain network connectivity between K3s control-plane servers and remote GPU nodes connected via WireGuard VPN.

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Architecture Overview](#architecture-overview)
3. [Reconciliation Mechanisms](#reconciliation-mechanisms)
4. [Troubleshooting Guide](#troubleshooting-guide)
5. [Verification Commands](#verification-commands)
6. [Common Issues and Fixes](#common-issues-and-fixes)
7. [Design Decisions](#design-decisions)

---

## Problem Statement

### The Chicken-and-Egg Problem

When a GPU node joins the Basilica cluster, it faces a timing issue:

1. **GPU node registers with WireGuard** - Gets a WireGuard IP (e.g., `10.200.3.54/32`)
2. **GPU node joins K3s cluster** - K3s assigns a pod CIDR (e.g., `10.42.17.0/24`)
3. **Problem**: WireGuard AllowedIPs only contains `/32`, not the pod CIDR

Without the pod CIDR in AllowedIPs, traffic destined for pods on the GPU node cannot traverse the WireGuard tunnel.

### The Flannel VXLAN Problem

Flannel uses VXLAN encapsulation for pod-to-pod traffic. For GPU nodes:

1. Pod traffic needs to be encapsulated by Flannel (via `flannel.1` interface)
2. The encapsulated traffic then traverses the WireGuard tunnel
3. **Problem**: Routes may incorrectly point to `wg0` instead of `flannel.1`
4. **Problem**: FDB (Forwarding Database) and neighbor entries may be stale or missing

---

## Architecture Overview

### Network Layers

```
+------------------+     +------------------+
|  K3s Server      |     |  GPU Node        |
|  (Control Plane) |     |  (Remote)        |
+------------------+     +------------------+
        |                        |
        | Layer 3: WireGuard     |
        | (10.200.0.0/16)        |
        |<---------------------->|
        |                        |
        | Layer 2: Flannel VXLAN |
        | (10.42.0.0/16)         |
        |<~~~~~~~~~~~~~~~~~~~~~~>|
        |                        |
```

### Two Reconciliation Mechanisms

| Mechanism | Responsibility | Location | Frequency |
|-----------|---------------|----------|-----------|
| **Systemd Timer** | WireGuard AllowedIPs | All K3s servers | Every 60s |
| **K8s CronJob** | Flannel VXLAN (routes, FDB, neighbors) | Control-plane nodes | Every 5min |

This separation follows the **Single Responsibility Principle**:

- Systemd timer handles Layer 3 (WireGuard routing)
- K8s CronJob handles Layer 2 (Flannel overlay)

---

## Reconciliation Mechanisms

### 1. Systemd Timer: WireGuard AllowedIPs Reconciliation

**Location**: `/usr/local/bin/wireguard-peer-reconcile.sh`
**Deployed by**: Ansible role `wireguard`
**Template**: `orchestrator/ansible/roles/wireguard/templates/wireguard-peer-reconcile.sh.j2`

**What it does**:

1. Queries K8s API for nodes with label `basilica.ai/wireguard=true`
2. Extracts node's WireGuard IP and pod CIDR
3. Finds the WireGuard peer by matching the IP
4. Adds the pod CIDR to the peer's AllowedIPs if missing

**Key Commands**:

```bash
# Check timer status
systemctl status wireguard-peer-reconcile.timer

# View recent logs
journalctl -u wireguard-peer-reconcile -n 20

# Run manually
/usr/local/bin/wireguard-peer-reconcile.sh

# Check AllowedIPs
wg show wg0 allowed-ips
```

**Expected Output** (AllowedIPs should have both /32 and /24):

```
PIfGoesl2YPuOsYzaVlrnExPw9571hDXlq7oVyvQIho=    10.200.99.150/32 10.42.15.0/24
3bUBMvpP0+9kAUV0nTqWx3Qsd7p8mKfYqRJ+DTf1bFw=    10.200.3.54/32 10.42.17.0/24
```

### 2. K8s CronJob: Flannel VXLAN Reconciliation

**Location**: `orchestrator/k8s/services/wireguard/wireguard-reconcile-cronjob.yaml`
**Namespace**: `kube-system`
**Schedule**: Every 5 minutes (`*/5 * * * *`)

**What it does**:

1. Removes incorrect routes that point pod CIDRs to `wg0`
2. Adds correct routes via `flannel.1` VXLAN interface
3. Updates bridge FDB entries (MAC -> WireGuard IP mappings)
4. Updates neighbor entries (VTEP IP -> MAC mappings)
5. Writes Prometheus metrics

**Key Commands**:

```bash
# Check CronJob status
kubectl get cronjob -n kube-system wireguard-reconcile

# View recent jobs
kubectl get jobs -n kube-system | grep wireguard | tail -5

# View latest job logs
kubectl logs -n kube-system job/$(kubectl get jobs -n kube-system --sort-by=.metadata.creationTimestamp -o name | grep wireguard | tail -1 | cut -d/ -f2)

# Check Flannel routes (should be via flannel.1, NOT wg0)
ip route show | grep -E "10.42.1[57]"

# Check FDB entries
bridge fdb show dev flannel.1 | grep -E "10.200\."

# Check neighbor entries
ip neigh show dev flannel.1 | grep -E "10.42\."
```

**Expected Output** (routes via flannel.1):

```
10.42.15.0/24 via 10.42.15.0 dev flannel.1 onlink
10.42.17.0/24 via 10.42.17.0 dev flannel.1 onlink
```

---

## Troubleshooting Guide

### Issue 1: Systemd Timer Not Running (Dependency Failed)

**Symptom**:

```
journalctl -u wireguard-peer-reconcile
Dec 01 16:14:29 k3s-server-2 systemd[1]: Dependency failed for wireguard-peer-reconcile.service
```

**Root Cause**: The service has `Requires=wg-quick@wg0.service`, but wg-quick shows "failed" state even though the WireGuard interface is actually up and working.

**Solution**: Changed from `Requires` to `Wants` in the systemd service file. The script checks for the interface itself and exits gracefully if unavailable.

**Fix Applied** (2025-12-01):

```ini
[Unit]
Description=WireGuard Peer Reconciliation
After=wg-quick@wg0.service k3s.service
# Use Wants instead of Requires - the script checks for wg0 interface itself
Wants=wg-quick@wg0.service
```

### Issue 2: AllowedIPs Not Being Updated (Parsing Bug)

**Symptom**: Script logs "no changes needed" but AllowedIPs are missing pod CIDRs.

**Root Cause**: Two parsing issues:

1. `wg show wg0 allowed-ips` outputs space-separated IPs, not comma-separated
2. `awk '{print $2}'` only captured the first IP, missing additional CIDRs

**Solution**:

1. Updated `cidr_in_allowed()` to handle both space and comma separators
2. Updated awk to capture all fields: `awk '{$1=""; print substr($0,2)}'`
3. Added conversion to comma-separated for `wg set` command

**Fix Applied** (2025-12-01):

```bash
# Before (broken)
current_allowed=$(wg show "$INTERFACE" allowed-ips | grep "^$pubkey" | awk '{print $2}')

# After (fixed)
current_allowed=$(wg show "$INTERFACE" allowed-ips | grep "^$pubkey" | awk '{$1=""; print substr($0,2)}')
```

### Issue 3: WireGuard Restart Fails (Route Conflict)

**Symptom**:

```
wg-quick[2331459]: RTNETLINK answers: File exists
systemd[1]: wg-quick@wg0.service: Failed with result 'exit-code'
```

**Root Cause**: When peers have pod CIDRs in AllowedIPs and `SaveConfig=true`, wg-quick tries to add routes for those CIDRs via `wg0`, but Flannel already has routes via `flannel.1`.

**Solution**: Updated WireGuard config template:

1. Added `Table = off` to prevent wg-quick from managing routes
2. Added `SaveConfig = false` to prevent runtime changes from persisting

**Fix Applied** (2025-12-01):

```ini
[Interface]
Address = 10.200.0.1/16
ListenPort = 51820
PrivateKey = ...
Table = off
SaveConfig = false
MTU = 8921
```

### Issue 4: CronJob Pod Pending (Untolerated Taint)

**Symptom**: CronJob pod stuck in Pending state.

**Root Cause**: Missing toleration for `basilica.ai/control-plane-only` taint.

**Solution**: Added toleration to CronJob spec.

### Issue 5: CronJob Script Errors (BusyBox grep)

**Symptom**: `grep: invalid option -- 'P'`

**Root Cause**: netshoot image uses BusyBox grep which doesn't support Perl regex (`-P`).

**Solution**: Replaced `grep -oP 'pattern \K...'` with awk:

```bash
# Before (broken)
grep -oP 'dst \K[0-9.]+'

# After (fixed)
awk '{for(i=1;i<=NF;i++) if($i=="dst") print $(i+1)}'
```

---

## Verification Commands

### Complete Verification Checklist

```bash
# 1. WireGuard Interface
ansible -i inventories/production.ini k3s_server -m shell -a 'wg show wg0 | head -10'

# 2. AllowedIPs (should have /32 AND /24 for each peer)
ansible -i inventories/production.ini k3s_server -m shell -a 'wg show wg0 allowed-ips'

# 3. Systemd Timer
ansible -i inventories/production.ini k3s_server -m shell -a 'systemctl is-active wireguard-peer-reconcile.timer'

# 4. K8s CronJob
kubectl get cronjob -n kube-system wireguard-reconcile
kubectl get jobs -n kube-system | grep wireguard | tail -3

# 5. Flannel Routes (should be via flannel.1)
ansible -i inventories/production.ini k3s_server -m shell -a 'ip route show | grep -E "10.42.1[57]"'

# 6. FDB Entries
ansible -i inventories/production.ini k3s_server -m shell -a 'bridge fdb show dev flannel.1 | grep -E "10.200\."'

# 7. Neighbor Entries
ansible -i inventories/production.ini k3s_server -m shell -a 'ip neigh show dev flannel.1 | grep -E "10.42\..*PERMANENT"'

# 8. GPU Node Connectivity
ansible -i inventories/production.ini server1 -m shell -a 'ping -c 2 10.200.3.54; ping -c 2 10.200.99.150'

# 9. Pod Connectivity
ansible -i inventories/production.ini server1 -m shell -a 'ping -c 2 10.42.15.5'

# 10. GPU Nodes Ready
kubectl get nodes -l basilica.ai/wireguard=true
```

---

## Common Issues and Fixes

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Missing pod CIDR in AllowedIPs | Timer not running or parsing bug | Run `/usr/local/bin/wireguard-peer-reconcile.sh` manually |
| Routes via wg0 instead of flannel.1 | CronJob not running | Check `kubectl get cronjob -n kube-system wireguard-reconcile` |
| "Dependency failed" in timer logs | wg-quick service in failed state | Ensure service uses `Wants=` not `Requires=` |
| CronJob pod pending | Missing toleration | Add `basilica.ai/control-plane-only` toleration |
| WireGuard restart fails | Route conflict with Flannel | Ensure config has `Table = off` |

---

## Design Decisions

### Why Two Reconciliation Mechanisms?

**Separation of Concerns**:

- **Systemd timer** runs on all K3s servers, has direct access to `wg` command
- **K8s CronJob** provides cluster-wide view, better observability, Prometheus metrics

**Defense in Depth**:

- If systemd timer fails, CronJob continues fixing Flannel entries
- If CronJob fails, WireGuard AllowedIPs are still updated by systemd timer

### Why `Wants` Instead of `Requires`?

The WireGuard interface can be up and functional even when `wg-quick@wg0.service` shows "failed" status (e.g., after a restart with route conflicts). The reconciliation script checks for the interface directly using `wg show`, so the hard dependency is unnecessary.

### Why `Table = off` in WireGuard Config?

With `Table = off`, wg-quick doesn't create routes for AllowedIPs. This prevents conflicts with Flannel routes:

- Pod CIDR routes should go via `flannel.1` (VXLAN encapsulation)
- WireGuard peer routes (`/32`) are handled by kernel routing
- The reconciliation CronJob ensures correct Flannel routing

### Why Remove Flannel Reconciliation from Systemd Script?

Originally, the systemd script handled both WireGuard AllowedIPs AND Flannel VXLAN entries. This was simplified to follow the DRY and Single Responsibility principles:

- Duplicate logic in two places makes maintenance harder
- CronJob provides better observability (Prometheus metrics)
- Single source of truth for Flannel reconciliation

---

## Files Reference

| File | Purpose |
|------|---------|
| `orchestrator/ansible/roles/wireguard/templates/wireguard-peer-reconcile.sh.j2` | Systemd timer script template |
| `orchestrator/ansible/roles/wireguard/templates/wg0.conf.j2` | WireGuard config template |
| `orchestrator/ansible/roles/wireguard/tasks/main.yml` | Ansible tasks for WireGuard setup |
| `orchestrator/ansible/roles/wireguard/defaults/main.yml` | Default variables |
| `orchestrator/k8s/services/wireguard/wireguard-reconcile-cronjob.yaml` | K8s CronJob manifest |

---

## Changelog

### 2025-12-01: Reconciliation Architecture Simplification

**Changes**:

1. Removed Flannel reconciliation from systemd script (now K8s CronJob only)
2. Fixed AllowedIPs parsing for space-separated output
3. Changed systemd dependency from `Requires` to `Wants`
4. Added `Table = off` to WireGuard config to prevent route conflicts
5. Fixed CronJob to use awk instead of grep -P for BusyBox compatibility

**Verification**:

- All 3 K3s servers have correct AllowedIPs with pod CIDRs
- CronJob running successfully every 5 minutes
- Flannel routes correctly via flannel.1
- Full connectivity to GPU nodes and their pods
