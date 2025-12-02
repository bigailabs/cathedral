# Flannel VXLAN Troubleshooting Runbook

**Audience**: Platform Engineers, SREs, On-Call Engineers
**Purpose**: Diagnose and resolve Flannel VXLAN networking issues in K3s clusters with WireGuard GPU nodes
**Last Updated**: 2025-12-01

---

## Table of Contents

1. [Overview](#overview)
2. [Network Architecture](#network-architecture)
3. [Prerequisites](#prerequisites)
4. [Quick Diagnostics](#quick-diagnostics)
5. [Issue 1: Duplicate VtepMAC Addresses](#issue-1-duplicate-vtepmac-addresses)
6. [Issue 2: Incorrect Routes via wg0](#issue-2-incorrect-routes-via-wg0)
7. [Issue 3: Stale FDB Entries](#issue-3-stale-fdb-entries)
8. [Issue 4: Stale Neighbor/ARP Entries](#issue-4-stale-neighborarp-entries)
9. [Issue 5: Missing Flannel Routes](#issue-5-missing-flannel-routes)
10. [Issue 6: VXLAN Interface Not Created](#issue-6-vxlan-interface-not-created)
11. [Verification Checklist](#verification-checklist)
12. [Rollback Procedures](#rollback-procedures)
13. [Monitoring and Alerting](#monitoring-and-alerting)
14. [Appendix: Command Reference](#appendix-command-reference)

---

## Overview

Flannel VXLAN provides the pod-to-pod networking layer in our K3s cluster. When GPU nodes connect via WireGuard VPN, special care is needed to ensure VXLAN traffic is properly encapsulated and routed.

### Traffic Flow

```
Envoy Pod (10.42.1.x)          User Pod (10.42.17.x)
     |                              ^
     | Pod Network                  | Pod Network
     v                              |
[flannel.1 VXLAN]              [flannel.1 VXLAN]
     |                              ^
     | VXLAN encap (UDP 8472)       | VXLAN decap
     v                              |
[wg0 interface]                [wg0 interface]
     |                              ^
     | WireGuard encap (UDP 51820)  | WireGuard decap
     v                              |
[K3s Server VPC]  ------>  [GPU Node Remote]
   10.101.x.x                  10.200.x.x
```

### Key Components

| Component | Purpose | Configuration Location |
|-----------|---------|----------------------|
| flannel.1 | VXLAN tunnel endpoint | Auto-created by K3s Flannel |
| VtepMAC | VXLAN MAC address | K8s node annotation |
| FDB entries | MAC-to-IP forwarding | `bridge fdb show dev flannel.1` |
| Neighbor entries | IP-to-MAC resolution | `ip neigh show dev flannel.1` |
| Pod CIDR routes | Route to pod networks | `ip route show` |

---

## Network Architecture

### IP Address Ranges

| Network | CIDR | Purpose |
|---------|------|---------|
| VPC Network | 10.101.0.0/16 | K3s servers and agents |
| WireGuard Overlay | 10.200.0.0/16 | Remote GPU nodes |
| Pod Network | 10.42.0.0/16 | Kubernetes pods |
| Service Network | 10.43.0.0/16 | ClusterIP services |

### Node Types

| Node Type | Network | Has WireGuard | Has flannel.1 |
|-----------|---------|---------------|---------------|
| K3s Server | 10.101.x.x | Yes (wg0) | Yes |
| K3s Agent | 10.101.x.x | No | Yes |
| GPU Node | 10.200.x.x | Yes (wg0) | Yes |

---

## Prerequisites

### Required Access

- SSH access to K3s servers and GPU nodes
- kubectl access with cluster-admin permissions
- Access to Prometheus/Grafana for metrics

### Required Tools

```bash
# On K3s servers
apt-get install -y bridge-utils iproute2 jq

# Verify tools are available
which bridge ip jq kubectl
```

### Safety Checklist

Before making any changes:

1. [ ] Identify affected nodes and pods
2. [ ] Notify team via Slack/PagerDuty
3. [ ] Document current state (save command outputs)
4. [ ] Have rollback commands ready
5. [ ] Set a time limit for troubleshooting (30 min before escalation)

---

## Quick Diagnostics

Run this diagnostic script on K3s servers to quickly identify issues:

```bash
#!/bin/bash
# Flannel VXLAN Quick Diagnostic
# Run on K3s server nodes

echo "=== VXLAN Interface Status ==="
ip -d link show flannel.1 2>/dev/null || echo "ERROR: flannel.1 not found"

echo -e "\n=== VXLAN Configuration ==="
ip -d link show flannel.1 2>/dev/null | grep -E "vxlan|link/ether"

echo -e "\n=== FDB Entries (MAC -> Destination IP) ==="
bridge fdb show dev flannel.1 2>/dev/null | grep -v "permanent" | head -20

echo -e "\n=== Neighbor Entries (VTEP IP -> MAC) ==="
ip neigh show dev flannel.1 2>/dev/null | head -20

echo -e "\n=== Pod CIDR Routes ==="
ip route show | grep "10.42" | head -20

echo -e "\n=== Routes via wg0 (SHOULD BE EMPTY for pod CIDRs) ==="
ip route show | grep "10.42.*.* dev wg0" || echo "OK: No pod routes via wg0"

echo -e "\n=== GPU Node VtepMACs from K8s ==="
kubectl get nodes -l basilica.ai/wireguard=true -o json 2>/dev/null | \
    jq -r '.items[] | "\(.metadata.name): \(.metadata.annotations["flannel.alpha.coreos.com/backend-data"] | fromjson | .VtepMAC)"'

echo -e "\n=== Duplicate MAC Check ==="
kubectl get nodes -o json 2>/dev/null | \
    jq -r '.items[].metadata.annotations["flannel.alpha.coreos.com/backend-data"] // empty' | \
    jq -r '.VtepMAC // empty' | sort | uniq -d | \
    { read dup; [ -z "$dup" ] && echo "OK: No duplicate MACs" || echo "ERROR: Duplicate MACs found: $dup"; }
```

---

## Issue 1: Duplicate VtepMAC Addresses

### Symptoms

- Intermittent connectivity to pods on different GPU nodes
- HTTP 503 errors for some user deployments
- Packet loss between specific pod pairs
- Alert: `DuplicateFlannelVtepMAC` firing

### Diagnosis

```bash
# Check for duplicate VtepMACs across all nodes
kubectl get nodes -o json | \
    jq -r '.items[] | select(.metadata.annotations["flannel.alpha.coreos.com/backend-data"]) |
    "\(.metadata.name) \(.metadata.annotations["flannel.alpha.coreos.com/backend-data"] | fromjson | .VtepMAC)"' | \
    sort -k2 | uniq -f1 -D
```

If output shows multiple nodes with the same MAC, you have duplicate VtepMACs.

### Root Cause

GPU nodes that were onboarded before v1.7.0 of onboard.sh created flannel.1 interfaces with random MACs. If two nodes happened to get the same MAC, VXLAN routing breaks.

### Resolution

**On the affected GPU node** (the one that should change its MAC):

```bash
# 1. Generate deterministic MAC from node name
NODE_NAME=$(hostname)
HASH=$(echo -n "$NODE_NAME" | sha256sum | cut -c1-10)
NEW_MAC=$(printf "02:%s:%s:%s:%s:%s" "${HASH:0:2}" "${HASH:2:2}" "${HASH:4:2}" "${HASH:6:2}" "${HASH:8:2}")
echo "New MAC will be: $NEW_MAC"

# 2. Get current VXLAN config
VXLAN_ID=$(ip -d link show flannel.1 | grep -oP 'id \K\d+')
LOCAL_IP=$(ip -d link show flannel.1 | grep -oP 'local \K[0-9.]+')

# 3. Recreate flannel.1 with new MAC
ip link del flannel.1
ip link add flannel.1 type vxlan id $VXLAN_ID local $LOCAL_IP dev wg0 nolearning dstport 8472
ip link set flannel.1 address $NEW_MAC
ip link set flannel.1 up

# 4. Update K8s node annotation
WG_IP=$(ip addr show wg0 | grep -oP 'inet \K[0-9.]+')
kubectl annotate node $NODE_NAME --overwrite \
    flannel.alpha.coreos.com/backend-data="{\"VNI\":1,\"VtepMAC\":\"$NEW_MAC\"}"
```

**On all K3s servers** (update FDB and neighbor entries):

```bash
# Get the new MAC and WireGuard IP of the fixed node
NODE_NAME="<fixed-node-name>"
NEW_MAC=$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')
WG_IP=$(kubectl get node $NODE_NAME -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
POD_CIDR=$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
VTEP_IP=$(echo $POD_CIDR | sed 's|/24||')

# Update FDB entry
bridge fdb replace $NEW_MAC dev flannel.1 dst $WG_IP self permanent

# Update neighbor entry
ip neigh replace $VTEP_IP lladdr $NEW_MAC dev flannel.1 nud permanent
```

### Verification

```bash
# No duplicate MACs should appear
kubectl get nodes -o json | \
    jq -r '.items[].metadata.annotations["flannel.alpha.coreos.com/backend-data"] // empty' | \
    jq -r '.VtepMAC // empty' | sort | uniq -d

# Test connectivity to pods on the fixed node
kubectl get pods -A -o wide | grep $NODE_NAME | head -1 | awk '{print $7}' | xargs -I{} ping -c 3 {}
```

---

## Issue 2: Incorrect Routes via wg0

### Symptoms

- HTTP 503 errors for user deployments on GPU nodes
- Pods on GPU nodes unreachable from Envoy
- `tcpdump` shows VXLAN packets going to wg0 instead of flannel.1
- Alert: `FlannelRouteViaWG0` firing

### Diagnosis

```bash
# Check for pod CIDR routes via wg0 (should be empty)
ip route show | grep "10.42.*.*/24 dev wg0"

# These routes should go via flannel.1 instead
ip route show | grep "10.42" | grep flannel
```

### Root Cause

The wireguard-peer-reconcile script (before fix) was adding direct wg0 routes for GPU pod CIDRs, bypassing VXLAN encapsulation.

### Resolution

**On affected K3s server**:

```bash
# 1. Stop the reconcile timer temporarily
systemctl stop wireguard-peer-reconcile.timer

# 2. Remove incorrect wg0 routes for each GPU pod CIDR
for CIDR in $(ip route show | grep "10.42.*.*/24 dev wg0" | awk '{print $1}'); do
    echo "Removing incorrect route: $CIDR via wg0"
    ip route del $CIDR dev wg0
done

# 3. Add correct flannel.1 routes
kubectl get nodes -l basilica.ai/wireguard=true -o json | \
    jq -r '.items[] | "\(.spec.podCIDR) \(.spec.podCIDR | split("/")[0])"' | \
    while read CIDR VTEP_IP; do
        echo "Adding route: $CIDR via $VTEP_IP dev flannel.1"
        ip route replace $CIDR via $VTEP_IP dev flannel.1 onlink
    done

# 4. Deploy fixed reconcile script and restart timer
# (Ensure wireguard-peer-reconcile.sh.j2 has reconcile_flannel function)
systemctl start wireguard-peer-reconcile.timer
```

### Verification

```bash
# No pod routes should use wg0
ip route show | grep "10.42.*.*/24 dev wg0" && echo "FAIL: Routes still via wg0" || echo "OK"

# All GPU pod CIDRs should route via flannel.1
ip route show | grep "10.42" | grep flannel
```

---

## Issue 3: Stale FDB Entries

### Symptoms

- Connectivity works initially then fails
- Packets sent to wrong destination IP
- Alert: `VXLANFDBEntriesLow` or stale entries detected

### Diagnosis

```bash
# Show all FDB entries
bridge fdb show dev flannel.1

# Compare with current node MACs
kubectl get nodes -o json | \
    jq -r '.items[] | select(.metadata.annotations["flannel.alpha.coreos.com/backend-data"]) |
    "\(.metadata.annotations["flannel.alpha.coreos.com/backend-data"] | fromjson | .VtepMAC) -> \(.status.addresses[] | select(.type=="InternalIP") | .address)"'
```

### Resolution

```bash
# For each GPU node, update FDB entry
NODE_NAME="<node-name>"
MAC=$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')
WG_IP=$(kubectl get node $NODE_NAME -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')

# Replace FDB entry (idempotent)
bridge fdb replace $MAC dev flannel.1 dst $WG_IP self permanent

# Verify
bridge fdb show dev flannel.1 | grep $MAC
```

---

## Issue 4: Stale Neighbor/ARP Entries

### Symptoms

- Intermittent connectivity
- ARP resolution failures
- Alert: `VXLANStaleNeighborEntries` firing

### Diagnosis

```bash
# Show neighbor entries with state
ip neigh show dev flannel.1

# Look for STALE or FAILED states
ip neigh show dev flannel.1 | grep -E "STALE|FAILED"
```

### Resolution

```bash
# For each GPU node, update neighbor entry
NODE_NAME="<node-name>"
MAC=$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')
POD_CIDR=$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
VTEP_IP=$(echo $POD_CIDR | sed 's|/24||')

# Replace neighbor entry (idempotent)
ip neigh replace $VTEP_IP lladdr $MAC dev flannel.1 nud permanent

# Verify
ip neigh show dev flannel.1 | grep $VTEP_IP
```

---

## Issue 5: Missing Flannel Routes

### Symptoms

- `No route to host` errors
- Pods on specific nodes completely unreachable
- Alert: Route count dropped

### Diagnosis

```bash
# List expected routes (one per node)
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name} {.spec.podCIDR}{"\n"}{end}'

# List actual routes
ip route show | grep "10.42"

# Find missing routes
comm -23 \
    <(kubectl get nodes -o jsonpath='{range .items[*]}{.spec.podCIDR}{"\n"}{end}' | sort) \
    <(ip route show | grep "10.42" | awk '{print $1}' | sort)
```

### Resolution

```bash
# Add missing route for a specific node
NODE_NAME="<node-name>"
POD_CIDR=$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
VTEP_IP=$(echo $POD_CIDR | sed 's|/24||')

# For GPU nodes (via flannel.1)
ip route add $POD_CIDR via $VTEP_IP dev flannel.1 onlink

# Verify
ip route get $(echo $POD_CIDR | sed 's|/24|.1|')
```

---

## Issue 6: VXLAN Interface Not Created or DOWN

> **For GPU nodes specifically**, see the detailed runbook: [GPU-NODE-FLANNEL-INTERFACE-RECOVERY.md](./GPU-NODE-FLANNEL-INTERFACE-RECOVERY.md)

### Symptoms

- flannel.1 interface missing or DOWN
- K3s agent not starting properly
- No pod networking at all
- HTTP 503 for pods on the affected node
- Syslog shows: `flannel.1: Link DOWN` or `flannel.1: Lost carrier`

### Diagnosis

```bash
# Check if interface exists
ip link show flannel.1

# Check K3s agent logs
journalctl -u k3s-agent -n 100 | grep -i flannel

# Check flannel subnet file
cat /run/flannel/subnet.env

# Check syslog for flannel events
grep -i flannel /var/log/syslog | tail -30
```

### Resolution

**Step 1: Restart K3s agent on the affected node**

```bash
# Restart K3s agent to recreate interface
systemctl restart k3s-agent

# Wait for flannel.1 to appear
for i in {1..30}; do
    ip link show flannel.1 &>/dev/null && break
    sleep 2
done

# Verify
ip -d link show flannel.1
```

**Step 2: For GPU nodes - Update FDB/routes on K3s servers**

After K3s agent restart on a GPU node, you MUST update the network entries on all K3s servers:

```bash
# Get node details
NODE_NAME="<node-name>"
VTEP_MAC=$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')
WG_IP=$(kubectl get node $NODE_NAME -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
POD_CIDR=$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
VTEP_IP=$(echo $POD_CIDR | sed 's|/24||')

# On each K3s server, update FDB, route, and neighbor
bridge fdb replace $VTEP_MAC dev flannel.1 dst $WG_IP self permanent
ip route replace $POD_CIDR via $VTEP_IP dev flannel.1 onlink
ip neigh replace $VTEP_IP lladdr $VTEP_MAC dev flannel.1 nud permanent
```

**Or trigger reconciliation CronJob:**

```bash
kubectl create job --from=cronjob/wireguard-reconcile wireguard-reconcile-manual-$(date +%s) -n kube-system
```

---

## Verification Checklist

After resolving any issue, verify:

```bash
# 1. flannel.1 interface exists and is UP
ip link show flannel.1 | grep "state UP"

# 2. No duplicate VtepMACs
kubectl get nodes -o json | jq -r '.items[].metadata.annotations["flannel.alpha.coreos.com/backend-data"] // empty' | jq -r '.VtepMAC // empty' | sort | uniq -d | wc -l | grep -q "^0$"

# 3. No pod routes via wg0
ip route show | grep -c "10.42.*.*/24 dev wg0" | grep -q "^0$"

# 4. All GPU nodes have FDB entries
GPU_COUNT=$(kubectl get nodes -l basilica.ai/wireguard=true --no-headers | wc -l)
FDB_COUNT=$(bridge fdb show dev flannel.1 | grep -c "self permanent")
[ "$FDB_COUNT" -ge "$GPU_COUNT" ] && echo "OK: FDB entries present"

# 5. Test HTTP endpoint
curl -sI https://<test-deployment>.deployments.basilica.ai/ | head -1
```

---

## Rollback Procedures

### Rollback Route Changes

```bash
# Flush all flannel.1 routes and let reconcile script rebuild
ip route flush dev flannel.1

# Restart reconcile to rebuild
systemctl restart wireguard-peer-reconcile.service
```

### Rollback FDB/Neighbor Changes

```bash
# Flush all FDB entries
bridge fdb flush dev flannel.1

# Flush all neighbor entries
ip neigh flush dev flannel.1

# Restart reconcile to rebuild
systemctl restart wireguard-peer-reconcile.service
```

### Emergency: Disable Reconcile Script

```bash
# If reconcile script is causing issues
systemctl stop wireguard-peer-reconcile.timer
systemctl disable wireguard-peer-reconcile.timer

# Manual intervention required until fixed
```

---

## Monitoring and Alerting

### Key Metrics

| Metric | Alert Threshold | Description |
|--------|-----------------|-------------|
| `vxlan_fdb_entries_total` | < 2 | FDB entry count |
| `vxlan_neighbor_entries_total` | < 2 | Neighbor entry count |
| `vxlan_stale_neighbor_entries` | > 3 | Stale ARP entries |
| `flannel_route_via_wg0` | > 0 | Incorrect routes |
| `flannel_vtep_mac_info` | duplicates | MAC conflicts |

### Prometheus Queries

```promql
# FDB entries per node
vxlan_fdb_entries_total

# Stale neighbor ratio
vxlan_stale_neighbor_entries / vxlan_neighbor_entries_total

# Nodes with incorrect routes
flannel_route_via_wg0 > 0

# Duplicate MAC detection
count by (vtep_mac) (flannel_vtep_mac_info) > 1
```

### Grafana Dashboard Panels

1. **VXLAN Health Overview**: FDB/neighbor counts per node
2. **Route Correctness**: Count of incorrect wg0 routes
3. **MAC Conflict Detection**: Duplicate VtepMAC indicator
4. **Stale Entry Trend**: Stale neighbor entries over time

---

## Appendix: Command Reference

### Diagnostic Commands

```bash
# Full VXLAN interface details
ip -d link show flannel.1

# FDB table (MAC to destination IP mapping)
bridge fdb show dev flannel.1

# Neighbor table (VTEP IP to MAC mapping)
ip neigh show dev flannel.1

# All pod CIDR routes
ip route show | grep "10.42"

# K8s node annotations
kubectl get nodes -o custom-columns='NAME:.metadata.name,VTEP:.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data'
```

### Packet Capture

```bash
# Capture VXLAN traffic on flannel.1
tcpdump -i flannel.1 -nn -c 100

# Capture VXLAN encapsulated traffic (UDP 8472)
tcpdump -i wg0 -nn udp port 8472 -c 50

# Capture with packet details
tcpdump -i flannel.1 -nn -vvv -c 20
```

### Network Namespace Debugging

```bash
# Find pod's network namespace
POD_ID=$(crictl pods --name <pod-name> -q)
NS_PATH=$(crictl inspectp $POD_ID | jq -r '.info.runtimeSpec.linux.namespaces[] | select(.type=="network") | .path')

# Enter pod network namespace
nsenter --net=$NS_PATH ip addr show
nsenter --net=$NS_PATH ip route show
```
