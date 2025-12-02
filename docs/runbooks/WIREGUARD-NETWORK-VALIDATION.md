# WireGuard Network Validation Runbook

**Audience**: Platform Engineers, SREs, On-Call Engineers
**Purpose**: Systematically verify WireGuard/Flannel network health and GPU node connectivity
**Last Updated**: 2025-12-01

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Validation Checklist](#validation-checklist)
4. [Step-by-Step Validation](#step-by-step-validation)
5. [Common Issues and Resolutions](#common-issues-and-resolutions)
6. [Post-Incident Validation](#post-incident-validation)

---

## Overview

This runbook provides a systematic approach to validate the WireGuard VPN network and Flannel VXLAN overlay that connects GPU nodes to the K3s cluster. Use this after network changes, incident recovery, or as part of regular health checks.

### Quick Validation

For a quick automated check, use the cluster-manager tool:

```bash
# Run full validation checklist
clustermgr wg validate

# Run validation and attempt automatic fixes
clustermgr wg validate --fix
```

### Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     K3s Control Plane (AWS)                      │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Pod Network: 10.42.0.0/16 (Flannel VXLAN)                   │ │
│  │   - flannel.1 interface handles encapsulation               │ │
│  │   - FDB entries map pod CIDRs to VTEP MACs                 │ │
│  │   - Neighbor entries map VTEP IPs to MACs                  │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ WireGuard Network: 10.200.0.0/16                            │ │
│  │   - wg0 interface provides encrypted tunnel                │ │
│  │   - Peers registered via Basilica API                      │ │
│  │   - AllowedIPs determine what traffic goes through tunnel  │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                    WireGuard Tunnel (UDP/51820)
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Remote GPU Node (Data Center)                 │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Pod Network: 10.42.X.0/24 (assigned by K3s)                 │ │
│  │   - Flannel agent creates VXLAN over WireGuard             │ │
│  └─────────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ WireGuard: 10.200.X.X/32                                    │ │
│  │   - Registered when node joins via miner                   │ │
│  └─────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### Validation Layers

| Layer | What | Why | How |
|-------|------|-----|-----|
| 1. WireGuard Tunnel | Encrypted VPN between K3s and GPU nodes | Foundation for all traffic | Ping test, handshake check |
| 2. K8s Node Status | GPU nodes reporting Ready | K8s can schedule pods | kubectl get nodes |
| 3. Flannel VXLAN | Overlay network for pod IPs | Pod-to-pod communication | Route, FDB, neighbor checks |
| 4. Pod Communication | Actual traffic flow | End-to-end verification | HTTP requests, log analysis |
| 5. Reconciliation | Automated repair mechanisms | Self-healing | CronJob and timer status |
| 6. UserDeployments | Application workloads | Production readiness | Deployment and pod status |

---

## Prerequisites

### Required Access

- SSH access to K3s servers via Ansible
- kubectl with kubeconfig for the cluster
- Ansible inventory configured

### Environment Setup

```bash
# Set kubeconfig
export KUBECONFIG=~/.kube/k3s-basilica-config

# Navigate to Ansible directory
cd /path/to/basilica/orchestrator/ansible
```

---

## Validation Checklist

Use this checklist for quick validation. Each item includes pass/fail criteria.

| # | Check | clustermgr Command | Manual Command | Pass Criteria |
|---|-------|-------------------|----------------|---------------|
| 1 | WireGuard tunnel ping | `clustermgr wg ping` | `ansible ... ping` | 0% packet loss |
| 2 | GPU nodes Ready | `clustermgr wg gpu-nodes` | `kubectl get nodes` | STATUS=Ready |
| 3 | WireGuard handshakes | `clustermgr wg handshakes` | `wg show wg0` | < 3 minutes ago |
| 4 | Pod CIDRs in AllowedIPs | `clustermgr wg reconcile` | `wg show wg0 allowed-ips` | Both /32 and /24 present |
| 5 | Routes via flannel.1 | `clustermgr flannel routes` | `ip route show` | Not via wg0 |
| 6 | FDB entries correct | `clustermgr flannel fdb` | `bridge fdb show` | dst=WireGuard IP |
| 7 | CronJob success | `clustermgr wg cronjob` | `kubectl get jobs` | Recent STATUS=Complete |
| 8 | Deployment pods Running | - | `kubectl get pods` | 1/1 Running |
| 9 | HTTP traffic flowing | - | Pod logs | 200 responses visible |

---

## Step-by-Step Validation

### Step 1: WireGuard Tunnel Connectivity

**What**: Verify the encrypted tunnel between K3s servers and GPU nodes is operational.

**Why**: WireGuard is the foundation layer. If the tunnel is down, no traffic can reach GPU nodes.

**How** (using clustermgr):

```bash
clustermgr wg ping
```

**How** (manual):

```bash
# Ping GPU nodes from K3s servers
ansible -i inventories/production.ini server1 -m shell -a '
echo "=== Ping GPU Node 1 (10.200.99.150) ==="
ping -c 3 -W 3 10.200.99.150

echo ""
echo "=== Ping GPU Node 2 (10.200.3.54) ==="
ping -c 3 -W 3 10.200.3.54
' --one-line
```

**Expected Output**:

```
3 packets transmitted, 3 received, 0% packet loss
```

**Failure Indicators**:

- 100% packet loss: Tunnel is down
- High latency (>500ms): Network congestion or routing issue
- Partial loss: Intermittent connectivity

**Resolution**: See [WIREGUARD-TROUBLESHOOTING.md](./WIREGUARD-TROUBLESHOOTING.md)

---

### Step 2: GPU Node K8s Status

**What**: Verify GPU nodes are registered and Ready in the K3s cluster.

**Why**: Even if WireGuard is up, K8s needs to see the node as Ready to schedule pods.

**How** (using clustermgr):

```bash
clustermgr wg gpu-nodes
```

**How** (manual):

```bash
# List all nodes with GPU node highlighting
kubectl get nodes -o wide | grep -E "NAME|basilica.ai/wireguard|10\.200\."
```

**Expected Output**:

```
NAME                                   STATUS   ROLES    INTERNAL-IP
8a2fbf46-3b34-42c3-b62d-4d9b66ea9a1a   Ready    <none>   10.200.3.54
fa09143c-263d-431f-b919-eef0fb8e69d2   Ready    <none>   10.200.99.150
```

**Failure Indicators**:

- STATUS=NotReady: Node cannot communicate with API server
- Missing nodes: WireGuard peer not registered

**Resolution**:

```bash
# Check node conditions
kubectl describe node <node-name> | grep -A5 Conditions

# Check kubelet on GPU node (requires SSH to GPU node)
journalctl -u k3s-agent -n 50
```

---

### Step 3: WireGuard Peer Handshakes

**What**: Verify WireGuard peers have recent cryptographic handshakes.

**Why**: Stale handshakes indicate the tunnel is not actively exchanging traffic or the peer is unreachable.

**How** (using clustermgr):

```bash
clustermgr wg handshakes
```

**How** (manual):

```bash
# Check WireGuard status on K3s server
ansible -i inventories/production.ini server1 -m shell -a "wg show wg0" --one-line
```

**Expected Output**:

```
peer: PIfGoesl2YPuOsYzaVlrnExPw9571hDXlq7oVyvQIho=
  endpoint: 149.36.1.187:35638
  allowed ips: 10.200.99.150/32, 10.42.15.0/24
  latest handshake: 4 seconds ago        <-- Should be < 3 minutes
  transfer: 291.78 MiB received, 139.70 MiB sent
  persistent keepalive: every 1 minute
```

**Failure Indicators**:

- "latest handshake: (never)": Peer never connected
- Handshake > 3 minutes ago: Possible connectivity issue
- No transfer data: Traffic not flowing

**Resolution**:

```bash
# On K3s server, restart WireGuard
ansible -i inventories/production.ini k3s_server -m shell -a "systemctl restart wg-quick@wg0"

# Verify peers re-establish
sleep 10
ansible -i inventories/production.ini server1 -m shell -a "wg show wg0" --one-line
```

---

### Step 4: WireGuard AllowedIPs Configuration

**What**: Verify pod CIDRs are included in peer AllowedIPs.

**Why**: WireGuard uses AllowedIPs as an ACL. Traffic to pod CIDRs must be permitted through the tunnel.

**How** (using clustermgr):

```bash
clustermgr wg reconcile
```

**How** (manual):

```bash
ansible -i inventories/production.ini server1 -m shell -a "wg show wg0 allowed-ips" --one-line
```

**Expected Output**:

```
PIfGoesl2YPuOsYzaVlrnExPw9571hDXlq7oVyvQIho=    10.200.99.150/32 10.42.15.0/24
3bUBMvpP0+9kAUV0nTqWx3Qsd7p8mKfYqRJ+DTf1bFw=    10.200.3.54/32 10.42.17.0/24
```

**What to verify**:

- Each peer has a /32 WireGuard IP (e.g., 10.200.99.150/32)
- Each peer has a /24 pod CIDR (e.g., 10.42.15.0/24)

**Failure Indicators**:

- Only /32 present: Reconciliation hasn't run or failed
- Wrong pod CIDR: Node reassigned, reconciliation needed

**Resolution**:

```bash
# Run reconciliation manually
ansible -i inventories/production.ini server1 -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh"
```

---

### Step 5: Flannel Route Verification

**What**: Verify pod CIDR routes go via flannel.1, not wg0.

**Why**: Flannel handles VXLAN encapsulation. Routes via wg0 bypass VXLAN and break pod networking.

**How** (using clustermgr):

```bash
clustermgr flannel routes
```

**How** (manual):

```bash
# Check routes for GPU node pod CIDRs
ansible -i inventories/production.ini server1 -m shell -a "ip route | grep -E '10\.42\.(15|17)'" --one-line
```

**Expected Output**:

```
10.42.15.0/24 via 10.42.15.0 dev flannel.1 onlink
10.42.17.0/24 via 10.42.17.0 dev flannel.1 onlink
```

**Failure Indicators**:

- Route shows `dev wg0`: Incorrect routing, VXLAN will fail
- No route: Flannel hasn't programmed routes
- Route shows different interface: Misconfiguration

**Resolution**:

```bash
# Remove incorrect route and let reconciliation fix it
ansible -i inventories/production.ini server1 -m shell -a "
ip route del 10.42.X.0/24 dev wg0 2>/dev/null || true
systemctl restart wireguard-peer-reconcile.service
"
```

---

### Step 6: Flannel FDB Entry Verification

**What**: Verify Forwarding Database entries point VTEP MACs to WireGuard IPs.

**Why**: FDB tells Flannel where to send VXLAN-encapsulated packets.

**How** (using clustermgr):

```bash
clustermgr flannel fdb
```

**How** (manual):

```bash
# Check FDB entries for GPU node destinations
ansible -i inventories/production.ini server1 -m shell -a "bridge fdb show dev flannel.1 | grep -E '10\.200\.(99|3)'" --one-line
```

**Expected Output**:

```
26:8d:8b:ab:c4:3a dst 10.200.3.54 self permanent
ba:84:44:61:c5:7c dst 10.200.99.150 self permanent
```

**What to verify**:

- Each GPU node's VTEP MAC has an FDB entry
- dst= points to the WireGuard IP (10.200.x.x), not the public IP

**Failure Indicators**:

- Missing entries: VXLAN can't reach GPU nodes
- Wrong dst IP: Traffic sent to wrong destination
- Entries not "permanent": Will expire and break connectivity

**Resolution**:

```bash
# Let reconciliation rebuild FDB entries
ansible -i inventories/production.ini server1 -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh"
```

---

### Step 7: CronJob Reconciliation Status

**What**: Verify the Kubernetes CronJob for Flannel reconciliation is running successfully.

**Why**: The CronJob provides automated repair of VXLAN entries every 5 minutes.

**How** (using clustermgr):

```bash
clustermgr wg cronjob
```

**How** (manual):

```bash
# Check recent CronJob executions
kubectl get jobs -n kube-system -l app.kubernetes.io/name=wireguard-reconcile --sort-by=.metadata.creationTimestamp | tail -5
```

**Expected Output**:

```
NAME                           STATUS     COMPLETIONS   DURATION   AGE
wireguard-reconcile-29410115   Complete   1/1           5s         11m
wireguard-reconcile-29410120   Complete   1/1           6s         6m
wireguard-reconcile-29410125   Complete   1/1           5s         1m
```

**Failure Indicators**:

- STATUS=Failed: Script error, check logs
- No recent jobs: CronJob suspended or misconfigured
- Long DURATION (>60s): Performance issue

**Resolution**:

```bash
# Check failed job logs
kubectl logs -n kube-system job/wireguard-reconcile-<job-id>

# Check CronJob configuration
kubectl get cronjob -n kube-system wireguard-reconcile -o yaml
```

---

### Step 8: UserDeployment Pod Status

**What**: Verify user application pods are running on GPU nodes.

**Why**: This confirms the entire stack is operational for production workloads.

**How**:

```bash
# Find pods on GPU nodes
kubectl get pods -A -o wide | grep -E "10\.42\.(15|17)\." | head -10
```

**Expected Output**:

```
u-github-434149   2552f2b0-deployment-79d5cb5f4-vqmgf   1/1   Running   0   38h   10.42.17.23   8a2fbf46...
```

**What to verify**:

- Pods show `1/1` Ready
- STATUS is `Running`
- Pod IPs are in GPU node CIDRs (10.42.15.x or 10.42.17.x)

**Failure Indicators**:

- STATUS=Pending: Scheduling issue
- STATUS=CrashLoopBackOff: Application error
- 0/1 Ready: Container not starting

---

### Step 9: HTTP Traffic Verification

**What**: Verify HTTP traffic is flowing to pods on GPU nodes.

**Why**: End-to-end verification that the network path is complete.

**How**:

```bash
# Check recent logs from a deployment on GPU node
kubectl logs -n u-github-434149 <pod-name> --tail=10
```

**Expected Output**:

```
INFO:     10.42.6.12:50780 - "GET / HTTP/1.1" 200 OK
INFO:     10.42.1.47:49116 - "GET / HTTP/1.1" 200 OK
```

**What to verify**:

- HTTP 200 responses visible
- Source IPs are from various pod CIDRs (showing cross-node traffic)
- Recent timestamps

**Failure Indicators**:

- No logs: No traffic reaching the pod
- 5xx errors: Application issues
- Only local traffic: Cross-node networking broken

---

## Common Issues and Resolutions

### Issue 1: WireGuard Restart Fails

**Symptom**: `wg-quick up wg0` fails with "RTNETLINK answers: File exists"

**Cause**: Pod CIDR routes conflict between WireGuard and Flannel

**Resolution**:

```bash
# Ensure wg0.conf has Table = off
ansible -i inventories/production.ini k3s_server -m shell -a "
grep -q 'Table = off' /etc/wireguard/wg0.conf || {
  sed -i '/ListenPort/a Table = off' /etc/wireguard/wg0.conf
}
wg-quick up wg0
"
```

### Issue 2: Stale WireGuard Handshakes

**Symptom**: Handshakes show > 3 minutes ago

**Cause**: Network connectivity issue or peer not responding

**Resolution**:

```bash
# Check if peer endpoint is reachable
ansible -i inventories/production.ini server1 -m shell -a "
ENDPOINT=\$(wg show wg0 endpoints | awk '{print \$2}' | head -1)
nc -zvu \${ENDPOINT%:*} \${ENDPOINT#*:} 2>&1
"

# If unreachable, check GPU node WireGuard
# (requires SSH to GPU node)
```

### Issue 3: Missing FDB Entries

**Symptom**: Pod traffic times out, FDB entries missing

**Cause**: Flannel didn't program entries or they were flushed

**Resolution**:

```bash
# Manually add FDB entry (temporary fix)
ansible -i inventories/production.ini k3s_server -m shell -a "
bridge fdb replace <VTEP-MAC> dev flannel.1 dst <WG-IP> self permanent
"

# Permanent fix: run reconciliation
ansible -i inventories/production.ini k3s_server -m shell -a "/usr/local/bin/wireguard-peer-reconcile.sh"
```

### Issue 4: Routes via wg0 Instead of flannel.1

**Symptom**: `ip route show` shows pod CIDR via `dev wg0`

**Cause**: WireGuard config has `SaveConfig = true` and accumulated pod CIDRs

**Resolution**:

```bash
# Fix the route
ansible -i inventories/production.ini k3s_server -m shell -a "
ip route del 10.42.X.0/24 dev wg0
ip route add 10.42.X.0/24 via 10.42.X.0 dev flannel.1 onlink
"

# Prevent recurrence: ensure SaveConfig = false
ansible -i inventories/production.ini k3s_server -m shell -a "
sed -i 's/SaveConfig = true/SaveConfig = false/' /etc/wireguard/wg0.conf
"
```

---

## Post-Incident Validation

After any network incident, run through this complete validation:

```bash
#!/bin/bash
# Post-incident validation script

echo "=== Step 1: WireGuard Tunnel ==="
ansible -i inventories/production.ini server1 -m shell -a "ping -c 3 -W 3 10.200.99.150; ping -c 3 -W 3 10.200.3.54" --one-line

echo ""
echo "=== Step 2: K8s Node Status ==="
kubectl get nodes | grep -E "NAME|10\.200\."

echo ""
echo "=== Step 3: WireGuard Handshakes ==="
ansible -i inventories/production.ini server1 -m shell -a "wg show wg0 | grep -A1 'peer:'" --one-line

echo ""
echo "=== Step 4: AllowedIPs ==="
ansible -i inventories/production.ini server1 -m shell -a "wg show wg0 allowed-ips" --one-line

echo ""
echo "=== Step 5: Flannel Routes ==="
ansible -i inventories/production.ini server1 -m shell -a "ip route | grep flannel" --one-line

echo ""
echo "=== Step 6: FDB Entries ==="
ansible -i inventories/production.ini server1 -m shell -a "bridge fdb show dev flannel.1 | grep 10.200" --one-line

echo ""
echo "=== Step 7: CronJob Status ==="
kubectl get jobs -n kube-system -l app.kubernetes.io/name=wireguard-reconcile | tail -3

echo ""
echo "=== Step 8: GPU Node Pods ==="
kubectl get pods -A -o wide | grep -E "10\.42\.(15|17)\." | head -5

echo ""
echo "=== Validation Complete ==="
```

---

## Cluster Manager Commands Reference

The `clustermgr` tool provides commands to automate validation and troubleshooting:

### WireGuard Commands (`clustermgr wg`)

| Command | Description |
|---------|-------------|
| `clustermgr wg validate` | Run full validation checklist (Steps 1-7) |
| `clustermgr wg validate --fix` | Run validation and attempt automatic fixes |
| `clustermgr wg ping` | Ping all GPU nodes through WireGuard tunnel |
| `clustermgr wg gpu-nodes` | Show GPU node K8s status (Ready/NotReady) |
| `clustermgr wg status` | Show WireGuard interface status on all servers |
| `clustermgr wg handshakes` | Check peer handshake ages (stale detection) |
| `clustermgr wg reconcile` | Check AllowedIPs configuration |
| `clustermgr wg reconcile --fix` | Add missing pod CIDRs to AllowedIPs |
| `clustermgr wg cronjob` | Check CronJob status and recent jobs |
| `clustermgr wg timer` | Check systemd timer status |
| `clustermgr wg restart` | Restart WireGuard service |
| `clustermgr wg keys` | Show key information for rotation planning |

### Flannel Commands (`clustermgr flannel`)

| Command | Description |
|---------|-------------|
| `clustermgr flannel diagnose` | Comprehensive Flannel health check |
| `clustermgr flannel status` | Show flannel.1 interface status |
| `clustermgr flannel routes` | Verify pod CIDR routes via flannel.1 |
| `clustermgr flannel fdb` | Inspect FDB entries for VXLAN |
| `clustermgr flannel neighbors` | Check neighbor/ARP entries for VTEPs |
| `clustermgr flannel test` | Test VXLAN connectivity to GPU nodes |
| `clustermgr flannel mac-duplicates` | Detect duplicate VtepMAC addresses |
| `clustermgr flannel capture` | Capture packets on Flannel interface |
| `clustermgr flannel vxlan-capture` | Capture VXLAN traffic on wg0 |

### Global Options

```bash
clustermgr --kubeconfig ~/.kube/k3s-basilica-config wg validate
clustermgr --inventory inventories/production.ini wg ping
clustermgr --dry-run wg reconcile --fix
clustermgr --verbose wg status
```

---

## Related Documentation

- [WIREGUARD-TROUBLESHOOTING.md](./WIREGUARD-TROUBLESHOOTING.md) - Detailed WireGuard debugging
- [FLANNEL-VXLAN-TROUBLESHOOTING.md](./FLANNEL-VXLAN-TROUBLESHOOTING.md) - VXLAN-specific issues
- [HTTP-503-DIAGNOSIS.md](./HTTP-503-DIAGNOSIS.md) - User-facing error diagnosis
- [GPU-NODE-ONBOARDING.md](./GPU-NODE-ONBOARDING.md) - Adding new GPU nodes
