# GPU Node fuse-daemon and Pod Networking Troubleshooting Runbook

This runbook documents how to diagnose and resolve fuse-daemon failures on GPU nodes caused by pod network connectivity issues over WireGuard.

## Table of Contents

1. [Overview](#overview)
2. [Symptoms](#symptoms)
3. [Architecture Background](#architecture-background)
4. [Quick Diagnosis Flowchart](#quick-diagnosis-flowchart)
5. [Issue 1: Stale FUSE Mounts](#issue-1-stale-fuse-mounts)
6. [Issue 2: Pod-to-Pod Network Connectivity Failure](#issue-2-pod-to-pod-network-connectivity-failure)
7. [Issue 3: DNS Resolution Failure in Containers](#issue-3-dns-resolution-failure-in-containers)
8. [Issue 4: R2/S3 Dispatch Failures](#issue-4-r2s3-dispatch-failures)
9. [Automated Reconciliation System](#automated-reconciliation-system)
10. [Verification Procedures](#verification-procedures)
11. [Preventive Measures](#preventive-measures)
12. [Quick Reference Commands](#quick-reference-commands)

---

## Overview

The fuse-daemon runs as a DaemonSet on all K8s nodes, providing FUSE-based filesystem mounts for user namespaces. On GPU nodes connected via WireGuard, the fuse-daemon requires:

1. Working pod-to-pod network connectivity (to reach CoreDNS)
2. DNS resolution (to resolve R2/S3 endpoint hostnames)
3. Outbound HTTPS connectivity (to upload/download from object storage)

When any of these fail, the fuse-daemon shows errors like:
- `Failed to put object: dispatch failure`
- `Temporary failure in name resolution`
- `File exists (os error 17)` when creating mounts

---

## Symptoms

### Primary Symptoms

| Symptom | Likely Cause | Section |
|---------|--------------|---------|
| `dispatch failure` errors in logs | DNS or network failure | [Issue 2](#issue-2-pod-to-pod-network-connectivity-failure) |
| `File exists (os error 17)` | Stale FUSE mount | [Issue 1](#issue-1-stale-fuse-mounts) |
| `Temporary failure in name resolution` | DNS unreachable | [Issue 3](#issue-3-dns-resolution-failure-in-containers) |
| Container can't ping CoreDNS pod | Missing WireGuard routes | [Issue 2](#issue-2-pod-to-pod-network-connectivity-failure) |
| Host can ping but container can't | Pod CIDR not in AllowedIPs | [Issue 2](#issue-2-pod-to-pod-network-connectivity-failure) |

### How to Check fuse-daemon Status

```bash
# Check pod status on GPU nodes
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n basilica-storage -o wide | grep fuse-daemon

# Check logs for errors
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage <POD_NAME> --tail=50 | grep -E "ERROR|WARN|dispatch|resolution"

# Check specific GPU node (replace with actual node ID)
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName=<GPU_NODE_ID> --tail=50
```

---

## Architecture Background

### Network Topology

```
GPU Node (Remote Datacenter)
+------------------------------------------+
|  Pod: fuse-daemon (10.42.X.Y)            |
|    |                                      |
|    | Container network namespace          |
|    v                                      |
|  cni0 bridge (10.42.X.1)                 |
|    |                                      |
|    | Host network namespace               |
|    v                                      |
|  wg0 interface (10.200.X.X)              |
|    |                                      |
+----+--------------------------------------+
     | UDP:51820 (WireGuard encrypted)
     v
+------------------------------------------+
|  K3s Server (AWS VPC)                    |
|    wg0 (10.200.0.Z)                      |
|    |                                      |
|    | Needs route: 10.42.X.0/24 -> wg0    |
|    v                                      |
|  flannel.1 / Pod network                 |
|    |                                      |
|    v                                      |
|  CoreDNS Pod (10.42.Y.Z)                 |
+------------------------------------------+
```

### Key Networks

| Network | CIDR | Purpose | Routed via WireGuard? |
|---------|------|---------|----------------------|
| WireGuard Overlay | 10.200.0.0/16 | Node-to-node communication | Yes (always) |
| Pod Network (Flannel) | 10.42.0.0/16 | Pod-to-pod communication | Yes (for GPU nodes) |
| Service Network | 10.43.0.0/16 | ClusterIP services | No (kube-proxy handles locally) |

### The Chicken-and-Egg Problem

When a GPU node onboards:
1. WireGuard is configured with the node's WireGuard IP (e.g., 10.200.3.54/32)
2. Node joins K3s cluster
3. K3s assigns a pod CIDR (e.g., 10.42.17.0/24)
4. **Problem**: K3s servers don't know to route 10.42.17.0/24 to this WireGuard peer

This causes return traffic from cluster pods to GPU node pods to fail.

---

## Quick Diagnosis Flowchart

```
fuse-daemon showing errors?
          |
          v
    Check pod logs
          |
    +-----+-----+
    |           |
    v           v
"dispatch    "File exists
 failure"    (os error 17)"
    |              |
    v              v
 Check DNS     Check stale
 resolution    FUSE mounts
    |              |
    v              |
Can container      |
ping CoreDNS?      |
 (10.42.X.X)       |
    |              |
+---+---+          |
|       |          |
v       v          v
NO      YES    Issue 1:
|       |      Stale Mounts
|       v
|    DNS works,
|    check R2
|    connectivity
|
v
Check WireGuard
AllowedIPs include
pod CIDR?
    |
+---+---+
|       |
v       v
NO      YES
|       |
v       v
Issue 2:   Check routes
Missing    on K3s servers
pod CIDR   (ip route)
```

---

## Issue 1: Stale FUSE Mounts

### Symptoms

```
ERROR basilica_storage::daemon::namespace_watcher: Failed to create mount for namespace
  namespace=u-github-434149
  error=Failed to create mount directory: Failed to create directory '/var/lib/basilica/fuse/u-github-434149': File exists (os error 17)
```

When you check the directory:
```bash
ssh shadeform@<GPU_NODE_IP> "ls -la /var/lib/basilica/fuse/"
# Shows: d????????? ? ? ? ? ? u-github-434149
```

### Root Cause

Previous fuse-daemon pod restarts left FUSE mounts in a broken state. The mount point exists but the FUSE process that owned it is gone, leaving a "Transport endpoint not connected" error.

### Diagnosis

```bash
# SSH to GPU node
ssh shadeform@<GPU_NODE_IP>

# Check for stale mounts
ls -la /var/lib/basilica/fuse/

# Look for broken mounts (shows ????????? or "Transport endpoint not connected")
for dir in /var/lib/basilica/fuse/u-*; do
  stat "$dir" 2>&1 | grep -q "Transport endpoint" && echo "STALE: $dir"
done

# Check mount table
mount | grep basilica
```

### Resolution

**Step 1: Identify stale mount points**
```bash
ssh shadeform@<GPU_NODE_IP> '
for dir in /var/lib/basilica/fuse/u-*; do
  if ! stat "$dir" >/dev/null 2>&1; then
    echo "Stale mount: $dir"
  fi
done
'
```

**Step 2: Unmount and remove stale directories**
```bash
ssh shadeform@<GPU_NODE_IP> '
for dir in /var/lib/basilica/fuse/u-*; do
  if ! stat "$dir" >/dev/null 2>&1; then
    echo "Cleaning: $dir"
    # Try fusermount first (graceful)
    sudo fusermount -uz "$dir" 2>/dev/null || true
    # Force unmount if fusermount fails
    sudo umount -l "$dir" 2>/dev/null || true
    # Remove the directory
    sudo rmdir "$dir" 2>/dev/null || sudo rm -rf "$dir" 2>/dev/null || true
  fi
done
'
```

**Step 3: Restart fuse-daemon pod**
```bash
# Find the pod on this node
POD=$(KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n basilica-storage -o wide | grep <GPU_NODE_ID> | awk '{print $1}')

# Delete pod (DaemonSet will recreate it)
KUBECONFIG=~/.kube/k3s-basilica-config kubectl delete pod -n basilica-storage $POD
```

**Step 4: Verify**
```bash
# Wait for pod to restart
sleep 30

# Check logs for successful mount
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName=<GPU_NODE_ID> --tail=20 | grep -E "mount_created|Successfully"
```

---

## Issue 2: Pod-to-Pod Network Connectivity Failure

### Symptoms

- Container inside fuse-daemon cannot ping CoreDNS pod
- Host can ping CoreDNS pod successfully
- `wg show` on K3s servers shows peer with only WireGuard IP in AllowedIPs (missing pod CIDR)

### Root Cause

K3s servers have WireGuard peers configured with only the WireGuard IP (e.g., `10.200.3.54/32`), not the pod CIDR (e.g., `10.42.17.0/24`). This means:

1. GPU node pod sends packet with source IP 10.42.17.6
2. Packet reaches K3s server via WireGuard tunnel
3. K3s server forwards to destination pod
4. Response packet has destination 10.42.17.6
5. **K3s server doesn't know to send 10.42.17.0/24 via WireGuard to that peer**
6. Packet is dropped or sent via wrong interface

### Diagnosis

**Step 1: Find GPU node's pod CIDR and WireGuard IP**
```bash
# Get pod CIDRs for all nodes
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'

# Example output:
# 8a2fbf46-3b34-42c3-b62d-4d9b66ea9a1a    10.42.17.0/24
# fa09143c-263d-431f-b919-eef0fb8e69d2    10.42.15.0/24
```

**Step 2: Get GPU node's WireGuard public key**
```bash
ssh shadeform@<GPU_NODE_IP> "sudo cat /etc/wireguard/public.key"
```

**Step 3: Check AllowedIPs on K3s servers**
```bash
# Replace <PUBLIC_KEY> with the key from step 2
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "sudo wg show wg0 | grep -A2 '<PUBLIC_KEY_PREFIX>'" 2>/dev/null
```

**Expected output** (GOOD):
```
peer: 3bUBMvpP0+9kAUV0nTqWx3Qsd7p8mKfYqRJ+DTf1bFw=
  endpoint: 149.36.0.57:35329
  allowed ips: 10.200.3.54/32, 10.42.17.0/24   # <-- Pod CIDR included
```

**Problem output** (BAD):
```
peer: 3bUBMvpP0+9kAUV0nTqWx3Qsd7p8mKfYqRJ+DTf1bFw=
  endpoint: 149.36.0.57:35329
  allowed ips: 10.200.3.54/32                   # <-- Pod CIDR MISSING
```

**Step 4: Test container connectivity**
```bash
# Get fuse-daemon container PID
ssh shadeform@<GPU_NODE_IP> '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")
echo "Container PID: $FUSE_PID"

# Find CoreDNS pod IP
COREDNS_IP="10.42.7.13"  # Get this from: kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide

# Test ping from container namespace
sudo nsenter -t $FUSE_PID -n ping -c 3 -W 2 $COREDNS_IP
'
```

### Resolution

**Step 1: Add pod CIDR to WireGuard AllowedIPs on all K3s servers**

```bash
# Variables - replace with actual values
PUBLIC_KEY="3bUBMvpP0+9kAUV0nTqWx3Qsd7p8mKfYqRJ+DTf1bFw="
WG_IP="10.200.3.54"
POD_CIDR="10.42.17.0/24"

# Update all K3s servers
for server in server1 server2 server3; do
  ansible -i orchestrator/ansible/inventories/production.ini $server \
    -m shell -a "sudo wg set wg0 peer '$PUBLIC_KEY' allowed-ips '$WG_IP/32,$POD_CIDR'" --become
done
```

**Step 2: Add route for pod CIDR via WireGuard**

```bash
# Add route on all K3s servers
for server in server1 server2 server3; do
  ansible -i orchestrator/ansible/inventories/production.ini $server \
    -m shell -a "sudo ip route replace $POD_CIDR dev wg0" --become
done
```

**Step 3: Save WireGuard configuration**

```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "sudo wg-quick save wg0" --become 2>/dev/null
```

**Step 4: Verify connectivity**

```bash
# Test from container again
ssh shadeform@<GPU_NODE_IP> '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")
sudo nsenter -t $FUSE_PID -n ping -c 3 -W 2 10.42.7.13
'
```

---

## Issue 3: DNS Resolution Failure in Containers

### Symptoms

```
ERROR: Failed to put object: dispatch failure
# Or in more detail:
error trying to connect: dns error: failed to lookup address information: Temporary failure in name resolution
```

### Root Cause

The container cannot reach CoreDNS (10.43.0.10 ClusterIP or actual pod IP). This is usually a symptom of [Issue 2](#issue-2-pod-to-pod-network-connectivity-failure).

### Diagnosis

```bash
ssh shadeform@<GPU_NODE_IP> '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")

# Test DNS resolution
sudo nsenter -t $FUSE_PID -n nslookup google.com 10.43.0.10

# If that fails, try direct CoreDNS pod IP
# Get CoreDNS pod IP first:
COREDNS_POD_IP=$(KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n kube-system -l k8s-app=kube-dns -o jsonpath="{.items[0].status.podIP}")
echo "CoreDNS Pod IP: $COREDNS_POD_IP"
sudo nsenter -t $FUSE_PID -n nslookup google.com $COREDNS_POD_IP
'
```

### Resolution

1. First resolve [Issue 2](#issue-2-pod-to-pod-network-connectivity-failure) (pod-to-pod connectivity)
2. DNS should start working automatically once pod network is fixed
3. Restart fuse-daemon pod to pick up the fix:

```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl rollout restart daemonset/fuse-daemon -n basilica-storage
```

---

## Issue 4: R2/S3 Dispatch Failures

### Symptoms

```
WARN basilica_storage::fuse::sync_worker: Failed to sync /.fuse_ready @ 0: Backend error: Failed to put object: dispatch failure
ERROR basilica_storage::fuse::sync_worker: Failed to sync region /.fuse_ready @ 0: Storage error: Backend error: Failed to put object: dispatch failure
```

### Root Cause

This is the downstream effect of DNS or network issues. The fuse-daemon cannot:
1. Resolve the R2 endpoint hostname (DNS issue), OR
2. Establish TCP connection to R2 (network issue)

### Diagnosis

```bash
ssh shadeform@<GPU_NODE_IP> '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")

# Test DNS resolution for R2
echo "=== DNS Resolution ==="
sudo nsenter -t $FUSE_PID -n nslookup <your-account-id>.r2.cloudflarestorage.com 10.43.0.10

# Test HTTPS connectivity (if DNS works)
echo "=== HTTPS Connectivity ==="
sudo nsenter -t $FUSE_PID -n curl -I --connect-timeout 5 https://<your-account-id>.r2.cloudflarestorage.com 2>&1 | head -5
'
```

### Resolution

1. Fix underlying network/DNS issues (Issues 2 and 3)
2. Restart fuse-daemon:

```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl rollout restart daemonset/fuse-daemon -n basilica-storage
```

3. Verify successful sync:

```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName=<GPU_NODE_ID> --tail=30 | grep -E "Successfully synced|mount_created"
```

---

## Automated Reconciliation System

To prevent Issue 2 from recurring, an automated reconciliation system runs on K3s servers.

### Components

1. **Reconciliation Script**: `/usr/local/bin/wireguard-peer-reconcile.sh`
2. **Systemd Service**: `wireguard-peer-reconcile.service`
3. **Systemd Timer**: `wireguard-peer-reconcile.timer` (runs every 60 seconds)

### How It Works

1. Queries K8s API for nodes with label `basilica.ai/wireguard=true`
2. Gets each node's pod CIDR from `spec.podCIDR`
3. Finds the WireGuard peer by matching WireGuard IP to node's InternalIP
4. Adds pod CIDR to peer's AllowedIPs if missing
5. Adds/updates route for pod CIDR via wg0

### Checking Reconciliation Status

```bash
# Check timer status
ansible -i orchestrator/ansible/inventories/production.ini server1 \
  -m shell -a "systemctl status wireguard-peer-reconcile.timer" --become

# Check recent reconciliation runs
ansible -i orchestrator/ansible/inventories/production.ini server1 \
  -m shell -a "journalctl -u wireguard-peer-reconcile -n 20 --no-pager" --become

# Manually trigger reconciliation
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh" --become
```

### Deploying/Updating Reconciliation

```bash
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/01-setup/wireguard.yml --tags wireguard,reconcile -l k3s_server
```

---

## Verification Procedures

### Full System Verification

Run this after any fix to ensure everything is working:

```bash
#!/bin/bash
# Save as: verify-gpu-fuse.sh

GPU_NODE_IP="$1"
GPU_NODE_ID="$2"

if [ -z "$GPU_NODE_IP" ] || [ -z "$GPU_NODE_ID" ]; then
  echo "Usage: $0 <GPU_NODE_IP> <GPU_NODE_ID>"
  exit 1
fi

echo "=== 1. Checking fuse-daemon pod status ==="
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n basilica-storage -o wide | grep "$GPU_NODE_ID"

echo ""
echo "=== 2. Checking for recent errors ==="
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName="$GPU_NODE_ID" --tail=20 | grep -E "ERROR|WARN" || echo "No recent errors"

echo ""
echo "=== 3. Checking WireGuard connectivity ==="
ssh shadeform@$GPU_NODE_IP "ping -c 2 -W 2 10.200.0.1" 2>/dev/null

echo ""
echo "=== 4. Checking container network connectivity ==="
ssh shadeform@$GPU_NODE_IP '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")
echo "Testing ping to CoreDNS..."
sudo nsenter -t $FUSE_PID -n ping -c 2 -W 2 10.42.7.13 2>&1 || echo "FAILED"
' 2>/dev/null

echo ""
echo "=== 5. Checking DNS resolution ==="
ssh shadeform@$GPU_NODE_IP '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")
sudo nsenter -t $FUSE_PID -n nslookup google.com 10.43.0.10 2>&1 | head -5
' 2>/dev/null

echo ""
echo "=== 6. Checking FUSE mounts ==="
ssh shadeform@$GPU_NODE_IP "ls -la /var/lib/basilica/fuse/" 2>/dev/null

echo ""
echo "=== 7. Checking successful syncs ==="
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName="$GPU_NODE_ID" --tail=50 | grep -E "Successfully synced|mount_created" | tail -5 || echo "No recent successful syncs found"

echo ""
echo "=== Verification Complete ==="
```

### Quick Health Check

```bash
# One-liner to check if fuse-daemon is healthy on a GPU node
ssh shadeform@<GPU_NODE_IP> '
CONTAINER_ID=$(sudo crictl ps | grep fuse-daemon | grep -v init | head -1 | awk "{print \$1}")
FUSE_PID=$(sudo crictl inspect $CONTAINER_ID 2>/dev/null | jq -r ".info.pid")
sudo nsenter -t $FUSE_PID -n ping -c 1 -W 2 10.42.7.13 >/dev/null 2>&1 && echo "HEALTHY" || echo "UNHEALTHY: Cannot reach CoreDNS"
'
```

---

## Preventive Measures

### 1. Enable Automated Reconciliation

Ensure the reconciliation timer is running on all K3s servers:

```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "systemctl is-active wireguard-peer-reconcile.timer" --become
```

### 2. Monitor fuse-daemon Health

Add Prometheus alerts for fuse-daemon errors:

```yaml
groups:
- name: fuse-daemon
  rules:
  - alert: FuseDaemonDispatchFailure
    expr: increase(fuse_daemon_sync_errors_total{error="dispatch_failure"}[5m]) > 0
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "fuse-daemon dispatch failures on {{ $labels.node }}"
      description: "fuse-daemon is failing to sync to R2, likely due to network issues"

  - alert: FuseDaemonMountFailure
    expr: increase(fuse_daemon_mount_errors_total[5m]) > 0
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "fuse-daemon mount failures on {{ $labels.node }}"
```

### 3. Pre-check Before Onboarding

Before onboarding a new GPU node, ensure the reconciliation system is working:

```bash
# Run reconciliation manually and check logs
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh && journalctl -u wireguard-peer-reconcile -n 5 --no-pager" --become
```

---

## Quick Reference Commands

### GPU Node Commands

```bash
# Check fuse-daemon logs
ssh shadeform@<IP> "sudo journalctl -u k3s-agent -n 50 | grep -i fuse"

# List FUSE mounts
ssh shadeform@<IP> "mount | grep fuse"

# Clean stale mount
ssh shadeform@<IP> "sudo fusermount -uz /var/lib/basilica/fuse/<namespace> && sudo rmdir /var/lib/basilica/fuse/<namespace>"

# Test container network
ssh shadeform@<IP> '
CID=$(sudo crictl ps | grep fuse-daemon | head -1 | awk "{print \$1}")
PID=$(sudo crictl inspect $CID | jq -r ".info.pid")
sudo nsenter -t $PID -n ping -c 2 10.42.7.13
'

# Check WireGuard status
ssh shadeform@<IP> "sudo wg show wg0"
```

### K3s Server Commands

```bash
# Check peer AllowedIPs
ansible k3s_server -i inventories/production.ini -m shell -a "sudo wg show wg0" --become | grep -A3 "<PEER_KEY>"

# Add pod CIDR to peer
ansible k3s_server -i inventories/production.ini -m shell -a "sudo wg set wg0 peer '<KEY>' allowed-ips '<WG_IP>/32,<POD_CIDR>'" --become

# Add route
ansible k3s_server -i inventories/production.ini -m shell -a "sudo ip route replace <POD_CIDR> dev wg0" --become

# Save WireGuard config
ansible k3s_server -i inventories/production.ini -m shell -a "sudo wg-quick save wg0" --become

# Run reconciliation
ansible k3s_server -i inventories/production.ini -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh" --become

# Check reconciliation logs
ansible server1 -i inventories/production.ini -m shell -a "journalctl -u wireguard-peer-reconcile -n 20 --no-pager" --become
```

### Kubernetes Commands

```bash
# Check all fuse-daemon pods
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n basilica-storage -o wide

# Check fuse-daemon logs on specific node
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage -l app.kubernetes.io/component=fuse-daemon --field-selector spec.nodeName=<NODE_ID> --tail=50

# Restart fuse-daemon on all nodes
KUBECONFIG=~/.kube/k3s-basilica-config kubectl rollout restart daemonset/fuse-daemon -n basilica-storage

# Delete specific fuse-daemon pod (will be recreated)
KUBECONFIG=~/.kube/k3s-basilica-config kubectl delete pod -n basilica-storage <POD_NAME>

# Get node pod CIDRs
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'

# Get CoreDNS pod IPs
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide
```

---

## Related Documentation

- [WIREGUARD-TROUBLESHOOTING.md](./WIREGUARD-TROUBLESHOOTING.md) - General WireGuard issues
- [gpu-node-onboarding-troubleshooting.md](./gpu-node-onboarding-troubleshooting.md) - Onboarding failures
- [FUSE-DEPLOYMENT-ISSUE.md](./FUSE-DEPLOYMENT-ISSUE.md) - FUSE deployment problems

---

## Changelog

| Date | Version | Changes |
|------|---------|---------|
| 2025-11-29 | 1.0.0 | Initial creation based on production incident |
