# GPU Node Flannel Interface Recovery Runbook

**Audience**: Platform Engineers, SREs, On-Call Engineers
**Purpose**: Diagnose and resolve missing or DOWN flannel.1 VXLAN interface on GPU nodes connected via WireGuard
**Last Updated**: 2025-12-02
**Severity**: P1 - Service impacting, user deployments unreachable

---

## Table of Contents

1. [Overview](#overview)
2. [Symptoms](#symptoms)
3. [Quick Diagnosis](#quick-diagnosis)
4. [Root Cause Analysis](#root-cause-analysis)
5. [Resolution Steps](#resolution-steps)
6. [Post-Recovery Verification](#post-recovery-verification)
7. [Prevention and Monitoring](#prevention-and-monitoring)
8. [Related Runbooks](#related-runbooks)

---

## Overview

This runbook addresses a critical failure scenario where the `flannel.1` VXLAN interface on a GPU node goes DOWN or is missing entirely. This breaks all cross-node pod communication, causing HTTP 503 errors for UserDeployments running on the affected GPU node.

### Impact

- All pods on the affected GPU node become unreachable from other cluster nodes
- HTTP 503 "Service Unavailable" for UserDeployments
- Envoy Gateway cannot route traffic to backend pods
- WireGuard tunnel remains functional (ping to WG IP works), but VXLAN overlay is broken

### Traffic Path

```
Envoy Pod (VPC Node)
     |
     | Flannel VXLAN (flannel.1)
     v
K3s Server (VPC)
     |
     | WireGuard Tunnel (wg0)
     v
GPU Node (Remote) <-- flannel.1 MISSING/DOWN
     |
     X (traffic cannot be delivered)
     |
User Pod (unreachable)
```

---

## Symptoms

### Primary Indicators

1. **HTTP 503 errors** from public URLs (`*.deployments.basilica.ai`)
2. **Envoy logs** show `connection_timeout` or `upstream_reset_before_response_started`
3. **Ping to VTEP IP fails** (e.g., `ping 10.42.17.0` from K3s server times out)
4. **Ping to WireGuard IP succeeds** (e.g., `ping 10.200.3.54` works)
5. **Pod logs on GPU node** show no incoming requests

### Envoy Log Pattern

```json
{
  "response_code": "503",
  "response_flags": "UF",
  "response_code_details": "upstream_reset_before_response_started{connection_timeout}",
  "upstream_host": "10.42.17.23:8000"
}
```

### Key Distinguishing Factor

If WireGuard ping works but VXLAN/VTEP ping fails, the issue is specifically with the Flannel VXLAN layer on the GPU node.

---

## Quick Diagnosis

### Step 1: Identify Affected GPU Node

```bash
# Find which node hosts the failing deployment
INSTANCE_ID="<instance-id-from-503-url>"
kubectl get pods -A -o wide | grep $INSTANCE_ID

# Example output shows node name:
# u-github-434149  xxx-deployment-xxx  10.42.17.23  8a2fbf46-3b34-42c3-b62d-4d9b66ea9a1a
```

### Step 2: Check flannel.1 Interface on GPU Node

```bash
# Method 1: SSH directly to GPU node
ssh <user>@<gpu-node-public-ip> 'ip link show flannel.1'

# Method 2: Via kubectl debug (if SSH unavailable)
kubectl debug node/<node-name> -it --image=nicolaka/netshoot:latest -- ip link show flannel.1
```

**Expected output (healthy):**
```
478: flannel.1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1370 qdisc noqueue state UNKNOWN
    link/ether ba:84:44:61:c5:7c brd ff:ff:ff:ff:ff:ff
```

**Problem indicator:**
```
Device "flannel.1" does not exist.
```

### Step 3: Verify WireGuard Tunnel is Up

```bash
# From K3s server, ping the GPU node's WireGuard IP
ansible -i inventories/production.ini server1 -m shell -a "ping -c 3 -W 3 <gpu-wg-ip>"

# Example: ping 10.200.3.54
```

If WireGuard ping succeeds but flannel.1 is missing, proceed to resolution.

### Step 4: Check Routing Table on GPU Node

```bash
ssh <user>@<gpu-node-public-ip> 'ip route show | grep 10.42'
```

**Problem indicator (missing flannel routes):**
```
10.42.0.0/16 dev wg0 scope link       # ALL traffic via wg0, no flannel routes
10.42.17.0/24 dev cni0 proto kernel scope link src 10.42.17.1
```

**Healthy output (with flannel routes):**
```
10.42.0.0/24 via 10.42.0.0 dev flannel.1 onlink
10.42.1.0/24 via 10.42.1.0 dev flannel.1 onlink
10.42.17.0/24 dev cni0 proto kernel scope link src 10.42.17.1
```

---

## Root Cause Analysis

### Common Causes

| Cause | Evidence | Resolution |
|-------|----------|------------|
| K3s agent crash/restart | Syslog shows k3s-agent restart | Restart K3s agent |
| flannel.1 went DOWN | Syslog: `flannel.1: Link DOWN` | Restart K3s agent |
| Kernel module issue | `modprobe vxlan` fails | Reboot node |
| Network namespace corruption | Multiple restarts didn't help | Reboot node |

### How to Check Syslog

```bash
# SSH to GPU node and check flannel-related logs
ssh <user>@<gpu-node-public-ip> 'grep -i flannel /var/log/syslog | tail -30'

# Or via kubectl debug
kubectl debug node/<node-name> -it --image=nicolaka/netshoot:latest -- \
    sh -c "cat /host/var/log/syslog | grep -i flannel | tail -30"
```

**Example output showing the issue:**
```
Dec  1 15:51:09 shadecloud systemd-networkd[641]: flannel.1: Link DOWN
Dec  1 15:51:09 shadecloud systemd-networkd[641]: flannel.1: Lost carrier
```

---

## Resolution Steps

### Step 1: SSH to Affected GPU Node

```bash
# Use the appropriate credentials for your GPU node provider
# Shadeform example:
ssh shadeform@<gpu-node-public-ip>

# Verify you're on the correct node
hostname
```

### Step 2: Restart K3s Agent

```bash
# Restart the K3s agent service
sudo systemctl restart k3s-agent

# Monitor startup
sudo journalctl -u k3s-agent -f
```

Wait approximately 10-15 seconds for Flannel to initialize.

### Step 3: Verify flannel.1 Interface is Recreated

```bash
# Check interface exists and is UP
ip link show flannel.1

# Expected output:
# 655: flannel.1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1370 qdisc noqueue state UNKNOWN
#     link/ether 26:8d:8b:ab:c4:3a brd ff:ff:ff:ff:ff:ff
```

### Step 4: Verify Flannel Routes are Recreated

```bash
ip route show | grep flannel

# Expected: Routes for other nodes via flannel.1
# 10.42.0.0/24 via 10.42.0.0 dev flannel.1 onlink
# 10.42.1.0/24 via 10.42.1.0 dev flannel.1 onlink
```

### Step 5: Update FDB/Routes on K3s Servers

After K3s agent restart on the GPU node, you MUST update the FDB entries and routes on all K3s servers. The reconciliation CronJob runs every 5 minutes, but for immediate recovery:

```bash
# Get GPU node details
NODE_NAME="<gpu-node-name>"
VTEP_MAC=$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')
WG_IP=$(kubectl get node $NODE_NAME -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}')
POD_CIDR=$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
VTEP_IP=$(echo $POD_CIDR | sed 's|/24||')

echo "Node: $NODE_NAME"
echo "VTEP MAC: $VTEP_MAC"
echo "WireGuard IP: $WG_IP"
echo "Pod CIDR: $POD_CIDR"
echo "VTEP IP: $VTEP_IP"
```

Then on each K3s server (server1, server2, server3):

```bash
# Add/update FDB entry
ansible -i inventories/production.ini k3s_server -m shell -a \
    "bridge fdb replace $VTEP_MAC dev flannel.1 dst $WG_IP self permanent"

# Add/update route
ansible -i inventories/production.ini k3s_server -m shell -a \
    "ip route replace $POD_CIDR via $VTEP_IP dev flannel.1 onlink"

# Add/update neighbor entry
ansible -i inventories/production.ini k3s_server -m shell -a \
    "ip neigh replace $VTEP_IP lladdr $VTEP_MAC dev flannel.1 nud permanent"
```

### Alternative: Trigger Reconciliation CronJob

```bash
# Create a manual job from the CronJob template
kubectl create job --from=cronjob/wireguard-reconcile wireguard-reconcile-manual-$(date +%s) -n kube-system

# Wait for completion
kubectl get jobs -n kube-system -l app.kubernetes.io/name=wireguard-reconcile --sort-by=.metadata.creationTimestamp | tail -3
```

---

## Post-Recovery Verification

### 1. Verify VXLAN Connectivity

```bash
# From K3s server, ping the VTEP IP (should succeed now)
ansible -i inventories/production.ini server1 -m shell -a "ping -c 3 $VTEP_IP"

# Expected: 0% packet loss
```

### 2. Verify FDB Entries on All Servers

```bash
ansible -i inventories/production.ini k3s_server -m shell -a \
    "bridge fdb show dev flannel.1 | grep $WG_IP"

# Each server should show:
# <vtep-mac> dst <wg-ip> self permanent
```

### 3. Verify Routes on All Servers

```bash
ansible -i inventories/production.ini k3s_server -m shell -a \
    "ip route show | grep $POD_CIDR"

# Each server should show:
# <pod-cidr> via <vtep-ip> dev flannel.1 onlink
```

### 4. Test Public URL

```bash
# Test multiple times to ensure all Envoy pods can reach the backend
for i in 1 2 3 4 5; do
    curl -s -o /dev/null -w "%{http_code} " https://<instance-id>.deployments.basilica.ai/
done
echo ""

# Expected: 200 200 200 200 200
```

### 5. Check Pod Logs

```bash
# Verify requests are reaching the pod
kubectl logs -n <namespace> <pod-name> --tail=10

# Expected: Recent HTTP 200 OK log entries
```

---

## Prevention and Monitoring

### Recommended Monitoring

Add these alerts to detect the issue before users report 503 errors:

```yaml
# Alert: flannel.1 interface DOWN on GPU node
- alert: FlannelInterfaceDown
  expr: |
    node_network_up{device="flannel.1", node=~".*wireguard.*"} == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "flannel.1 interface DOWN on {{ $labels.node }}"
    runbook: "docs/runbooks/GPU-NODE-FLANNEL-INTERFACE-RECOVERY.md"

# Alert: Missing flannel routes for GPU node CIDRs
- alert: MissingFlannelRoutes
  expr: |
    sum(flannel_route_count{node_type="gpu"}) < count(kube_node_labels{label_basilica_ai_wireguard="true"})
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Missing Flannel routes for GPU node pod CIDRs"
```

### Log Monitoring

Monitor syslog on GPU nodes for these patterns:

- `flannel.1: Link DOWN`
- `flannel.1: Lost carrier`
- `k3s-agent.*flannel.*error`

### Periodic Health Check

Consider adding a CronJob or DaemonSet that periodically verifies flannel.1 is UP:

```bash
# Simple health check script for GPU nodes
#!/bin/bash
if ! ip link show flannel.1 | grep -q "state UNKNOWN"; then
    echo "CRITICAL: flannel.1 is not UP"
    # Send alert or auto-recover
    sudo systemctl restart k3s-agent
fi
```

---

## Related Runbooks

- [HTTP-503-DIAGNOSIS.md](./HTTP-503-DIAGNOSIS.md) - General 503 troubleshooting
- [FLANNEL-VXLAN-TROUBLESHOOTING.md](./FLANNEL-VXLAN-TROUBLESHOOTING.md) - Comprehensive VXLAN issues
- [WIREGUARD-TROUBLESHOOTING.md](./WIREGUARD-TROUBLESHOOTING.md) - WireGuard tunnel issues
- [WIREGUARD-FLANNEL-RECONCILIATION.md](./WIREGUARD-FLANNEL-RECONCILIATION.md) - Reconciliation system
- [GPU-NODE-ONBOARDING-TROUBLESHOOTING.md](./GPU-NODE-ONBOARDING-TROUBLESHOOTING.md) - New node issues

---

## Appendix: Quick Reference Commands

### Diagnosis Commands

```bash
# Check flannel.1 on GPU node
ssh <user>@<gpu-ip> 'ip link show flannel.1'

# Check routes on GPU node
ssh <user>@<gpu-ip> 'ip route show | grep 10.42'

# Check FDB on K3s server
bridge fdb show dev flannel.1 | grep <wg-ip>

# Check routes on K3s server
ip route show | grep <pod-cidr>

# Check neighbor entries
ip neigh show dev flannel.1 | grep <vtep-ip>
```

### Recovery Commands

```bash
# Restart K3s agent on GPU node
sudo systemctl restart k3s-agent

# Add FDB entry on K3s server
bridge fdb replace <mac> dev flannel.1 dst <wg-ip> self permanent

# Add route on K3s server
ip route replace <pod-cidr> via <vtep-ip> dev flannel.1 onlink

# Add neighbor entry on K3s server
ip neigh replace <vtep-ip> lladdr <mac> dev flannel.1 nud permanent

# Trigger reconciliation
kubectl create job --from=cronjob/wireguard-reconcile wireguard-reconcile-manual-$(date +%s) -n kube-system
```

### GPU Node SSH Credentials

| Provider | User | Example |
|----------|------|---------|
| Shadeform | shadeform | `ssh shadeform@149.36.0.57` |
| Lambda | ubuntu | `ssh ubuntu@<ip>` |
| RunPod | root | `ssh root@<ip>` |

---

## Incident Timeline Template

Use this template when documenting incidents:

```
## Incident: GPU Node Flannel Interface Failure

**Date**: YYYY-MM-DD
**Duration**: HH:MM - HH:MM (X minutes)
**Impact**: UserDeployments on GPU node <name> unreachable
**Severity**: P1

### Timeline
- HH:MM - Alert triggered / User report received
- HH:MM - Identified flannel.1 missing on <node>
- HH:MM - Restarted K3s agent
- HH:MM - Updated FDB/routes on K3s servers
- HH:MM - Verified recovery, closed incident

### Root Cause
<description of why flannel.1 went down>

### Resolution
1. SSH to GPU node: `ssh <user>@<ip>`
2. Restart K3s agent: `sudo systemctl restart k3s-agent`
3. Update FDB/routes on servers: <commands>
4. Verify: <test commands>

### Action Items
- [ ] Investigate why flannel.1 went down
- [ ] Add monitoring for flannel.1 state
- [ ] Consider auto-recovery mechanism
```
