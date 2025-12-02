# K3s WireGuard 502 Bad Gateway Investigation Runbook

This runbook documents the systematic investigation and resolution of K3s cluster unresponsiveness caused by WireGuard iptables rate limiting. It provides a complete methodology for diagnosing and resolving similar connectivity issues.

**Incident Date**: 2025-11-28
**Affected Nodes**: Remote GPU nodes connected via WireGuard VPN
**Root Cause**: Overly aggressive iptables hashlimit rule blocking WireGuard traffic
**Resolution Time**: ~45 minutes

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Symptom Recognition](#symptom-recognition)
3. [Investigation Methodology](#investigation-methodology)
4. [Diagnostic Commands Reference](#diagnostic-commands-reference)
5. [Root Cause Analysis](#root-cause-analysis)
6. [Resolution Steps](#resolution-steps)
7. [Prevention and Hardening](#prevention-and-hardening)
8. [Decision Tree](#decision-tree)
9. [Key Learnings](#key-learnings)

---

## Executive Summary

### Problem Statement

Remote GPU nodes in the K3s cluster became unreachable via `kubectl logs/exec`, returning 502 Bad Gateway errors. Nodes appeared "Ready" in `kubectl get nodes` but kubelet (port 10250) was inaccessible from the K3s API server.

### Root Cause

An iptables hashlimit rule on K3s control plane servers was rate-limiting WireGuard UDP traffic:

```
DROP udp dpt:51820 limit: above 10/min burst 5 mode srcip
```

This rule dropped 25KB-468KB of legitimate WireGuard traffic across the three control plane servers, preventing:

- WireGuard handshakes from completing reliably
- Bidirectional tunnel establishment
- K3s API server from reaching kubelet on remote nodes

### Resolution

1. Removed the rate limit rule from all K3s servers
2. Restarted WireGuard on affected GPU nodes
3. Deleted CrashLoopBackOff pods to clear crash history
4. Updated Ansible role to disable rate limiting by default

---

## Symptom Recognition

### Primary Symptoms

| Symptom | Observation | Implication |
|---------|-------------|-------------|
| `kubectl logs` returns 502 | `proxy error from 127.0.0.1:6443 while dialing 10.200.X.Y:10250, code 502: 502 Bad Gateway` | API server cannot reach kubelet via WireGuard |
| Nodes show Ready but unreachable | `kubectl get nodes` shows Ready, but pod logs fail | K3s remotedialer heartbeats working, direct kubelet access broken |
| TLS handshake errors in logs | `http: TLS handshake error from 10.101.X.Y` | Connection attempts from VPC IPs timing out |
| Ping to WireGuard IPs fails | `From 10.200.X.Y icmp_seq=1 Destination Host Unreachable` | Bidirectional WireGuard tunnel broken |
| "Required key not available" | WireGuard-specific error during ping | Peer not registered or handshake blocked |

### Why Nodes Appeared "Ready"

K3s uses **remotedialer** for agent-to-server communication. Even when kubelet is unreachable:

1. Agents maintain persistent WebSocket to API server
2. Node heartbeats flow through this tunnel
3. Node-controller receives heartbeats, marks node Ready
4. But **reverse connections** (API server to kubelet) fail because they require direct WireGuard access

**Key Insight**: "Ready" status only confirms the agent can reach the server, NOT that the server can reach the agent.

---

## Investigation Methodology

### Phase 1: Gather Initial Evidence

**Step 1.1: Confirm the symptom scope**

Determine which nodes are affected and what operations fail:

```bash
# Test kubectl logs on all nodes
export KUBECONFIG=~/.kube/k3s-basilica-config
for node in $(kubectl get nodes -o name); do
  echo "=== $node ==="
  kubectl get pods -A --field-selector spec.nodeName=${node#node/} -o wide | head -3
  POD=$(kubectl get pods -A --field-selector spec.nodeName=${node#node/} -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  NS=$(kubectl get pods -A --field-selector spec.nodeName=${node#node/} -o jsonpath='{.items[0].metadata.namespace}' 2>/dev/null)
  if [ -n "$POD" ]; then
    kubectl logs -n $NS $POD --tail=1 2>&1 | head -2
  fi
done
```

**Why**: Establishes baseline - which nodes fail, which succeed. Patterns reveal if issue is node-specific or cluster-wide.

**Step 1.2: Check node status from cluster perspective**

```bash
kubectl get nodes -o wide
kubectl describe node <affected-node> | grep -A10 "Conditions:"
```

**Why**: Confirms node is "Ready" despite connectivity issues. The Ready condition uses RemoteDial heartbeats, not direct kubelet probes.

### Phase 2: WireGuard Tunnel Verification

**Step 2.1: Check WireGuard interface on affected GPU node**

```bash
ssh shadeform@<GPU_NODE_PUBLIC_IP> "
  echo '=== WireGuard Interface ===' && ip addr show wg0
  echo && echo '=== WireGuard Status ===' && sudo wg show wg0
"
```

**What to look for**:

- Interface is UP with correct IP (10.200.X.Y/16)
- Peers have `latest handshake` (indicates encrypted tunnel works)
- Non-zero `transfer` stats (indicates bidirectional traffic)

**Example of healthy output**:

```
peer: /r7NrwlbQU12o3bG1+swyvlYCBk+2aIibiNVc7vjGWI=
  endpoint: 18.191.135.192:51820
  allowed ips: 10.200.0.1/32, 10.101.0.0/24, 10.42.0.0/16
  latest handshake: 32 seconds ago
  transfer: 209.30 KiB received, 1.21 MiB sent
  persistent keepalive: every 25 seconds
```

**Example of BROKEN output (our case)**:

```
  latest handshake: 1 minute, 29 seconds ago
  transfer: 26.81 MiB received, 24.50 MiB sent  # looks OK but...
```

**Why**: Handshakes completing suggests encryption works. But if pings fail with handshakes working, traffic is being blocked after decryption (iptables) or before encryption (routing).

**Step 2.2: Test bidirectional connectivity**

From GPU node:

```bash
ssh shadeform@<GPU_NODE_PUBLIC_IP> "
  for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do
    echo -n \"\$ip: \"
    ping -c 1 -W 2 \$ip 2>&1 | grep -E 'bytes from|100%|Unreachable' | head -1
  done
"
```

From K3s servers:

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ping -c 1 -W 2 10.200.X.Y 2>&1 | grep -E 'bytes from|100%|Unreachable'"
```

**Why**: Tests both directions. Asymmetric results indicate issue on one side. In our case:

- Server → GPU node: 100% packet loss
- GPU node → Server: 100% packet loss
- But handshakes were completing

This asymmetry with working handshakes pointed to iptables on the servers.

### Phase 3: Peer Registration Verification

**Step 3.1: Verify peer exists on all K3s servers**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "echo '=== \$(hostname) ===' && sudo wg show wg0 | grep -E 'peer|allowed|handshake'" 2>/dev/null
```

**Expected**: Each server should list the GPU node's public key with:

- Correct `allowed ips` (matching GPU node's WireGuard IP)
- Recent `latest handshake`

**Why**: Missing peers or stale handshakes indicate registration issues (see WIREGUARD-TROUBLESHOOTING.md Issue 3).

### Phase 4: The Critical Diagnostic - iptables Analysis

**Step 4.1: Check iptables INPUT chain for WireGuard rules**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n -v --line-numbers | grep -E 'num|51820|hashlimit|limit:'"
```

**What we found**:

```
7      853  437K DROP  17  --  *  *  0.0.0.0/0  0.0.0.0/0  udp dpt:51820 limit: above 10/min burst 5
```

**Key indicators of the problem**:

1. `DROP` action on UDP 51820 (WireGuard port)
2. `437K` bytes dropped - **significant legitimate traffic dropped**
3. `limit: above 10/min burst 5` - **too restrictive for production**

**Why this matters**:

- PersistentKeepalive every 25 seconds = 2.4 handshakes/min per peer
- With 3 servers × 10 GPU nodes = 7.2 handshakes/min baseline (per server)
- 10/min limit with burst 5 means any reconnection spikes trigger drops
- NAT port changes or network flaps cause bursts that exceed the limit

**Step 4.2: Quantify the damage**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "echo '\$(hostname):'; sudo iptables -L INPUT -n -v | grep 51820 | awk '{print \"Dropped: \" \$2 \" packets, \" \$3}'"
```

**Our results**:

- server1: 25KB dropped
- server2: **437KB dropped** (primary server for affected node)
- server3: 226KB dropped

**Why**: Quantifies impact. High drop counts confirm rate limiting as root cause.

---

## Diagnostic Commands Reference

### Quick Diagnosis Flow

```bash
# 1. Identify affected nodes
kubectl get nodes -o wide

# 2. Test kubectl logs (reveals 502 errors)
kubectl logs -n <namespace> <pod> --tail=1

# 3. Check WireGuard from GPU node
ssh user@<GPU_IP> "sudo wg show wg0"

# 4. Test bidirectional ping
ssh user@<GPU_IP> "ping -c 1 10.200.0.1"
ansible k3s_server -m shell -a "ping -c 1 10.200.X.Y"

# 5. Check iptables rate limiting (THE KEY CHECK)
ansible k3s_server -m shell -a "sudo iptables -L INPUT -n -v | grep 51820"

# 6. Check drop counts
ansible k3s_server -m shell -a "sudo iptables -L INPUT -n -v --line-numbers | grep -E 'DROP.*51820'"
```

### Comprehensive Diagnostic One-Liner

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "
    echo '=== \$(hostname) ==='
    echo 'WireGuard Peers:' && sudo wg show wg0 | grep -c peer
    echo 'Rate Limit Drops:' && sudo iptables -L INPUT -n -v | grep '51820.*limit' | awk '{print \$2 \" pkts, \" \$3}'
    echo 'Recent Handshakes:' && sudo wg show wg0 latest-handshakes | awk '{if (\$2 > 0 && (systime() - \$2) < 180) print \"Healthy\"}'
  "
```

---

## Root Cause Analysis

### Technical Root Cause

**The iptables rule**:

```
iptables -A INPUT -p udp --dport 51820 -m hashlimit \
  --hashlimit-name wireguard_handshake --hashlimit-mode srcip \
  --hashlimit-above 10/minute --hashlimit-burst 5 \
  -m comment --comment "Rate limit WireGuard handshakes" -j DROP
```

**Why it caused failures**:

1. **`10/minute` is too restrictive**
   - PersistentKeepalive: 25s = 2.4 handshakes/min
   - With 3 peers (servers): 7.2 handshakes/min baseline
   - Any reconnection or NAT change pushes over limit

2. **`burst 5` is too small**
   - WireGuard restarts send 3 handshakes (one per peer) immediately
   - NAT port changes trigger new handshakes
   - 5-packet burst exhausted in ~2 seconds of activity

3. **`srcip` mode problematic with NAT**
   - Multiple GPU nodes behind same NAT share quota
   - 10 nodes behind one NAT = 1 handshake/min per node

4. **DROP action is too aggressive**
   - REJECT would allow faster recovery
   - DROP causes timeouts and backoff delays

### Why This Wasn't Caught Earlier

1. **Nodes appeared Ready**: K3s remotedialer uses outbound connections from agent, not affected by this rule
2. **Handshakes showed recent**: Rate limit only triggered during bursts, not steady-state
3. **Intermittent nature**: Issue worsened during node reconnections, resets, or network changes

### Contributing Factors

| Factor | Impact |
|--------|--------|
| GPU nodes behind NAT | Port changes trigger new handshakes |
| 25s PersistentKeepalive | Continuous handshake traffic |
| 3 K3s servers | 3x handshake traffic per node |
| Burst of reconnections | Exhausted rate limit bucket |

---

## Resolution Steps

### Step 1: Backup Current State

```bash
# Create timestamped backup
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /root/iptables.backup.\$(date +%Y%m%d_%H%M%S)"
```

**Verify backups created**:

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ls -la /root/iptables.backup.* | tail -1"
```

### Step 2: Remove Rate Limit Rule

**Identify rule number**:

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n --line-numbers | grep 51820"
```

**Remove by rule number** (example: rule 7):

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -D INPUT 7"
```

**Or remove by specification** (safer, idempotent):

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "
    while sudo iptables -D INPUT -p udp --dport 51820 -m hashlimit \
      --hashlimit-name wireguard_handshake --hashlimit-mode srcip \
      --hashlimit-above 10/minute --hashlimit-burst 5 \
      -m comment --comment 'Rate limit WireGuard handshakes' -j DROP 2>/dev/null
    do :; done
    echo 'All rate limit rules removed'
  "
```

### Step 3: Persist iptables Changes

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /etc/iptables.rules.v4"
```

### Step 4: Restart WireGuard on GPU Nodes

```bash
ssh shadeform@<GPU_NODE_IP> "sudo systemctl restart wg-quick@wg0"
```

**Wait 3 seconds, then verify**:

```bash
ssh shadeform@<GPU_NODE_IP> "
  sleep 3
  for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do
    echo -n \"\$ip: \"
    ping -c 1 -W 2 \$ip 2>&1 | grep -E 'bytes from|100%' | head -1
  done
"
```

### Step 5: Verify Cluster Connectivity

```bash
# From servers to GPU nodes
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ping -c 1 10.200.X.Y"

# kubectl logs should work now
kubectl logs -n <namespace> <pod> --tail=5

# Node status
kubectl get nodes
```

### Step 6: Clean Up Crashed Pods

If pods were in CrashLoopBackOff due to connectivity issues:

```bash
kubectl delete pod -n <namespace> <crashed-pod>
```

The DaemonSet/Deployment will recreate them with fresh state.

---

## Prevention and Hardening

### Ansible Role Updates Applied

**`roles/wireguard/defaults/main.yml`**:

```yaml
# Rate limiting is DISABLED by default after production outage
wireguard_ratelimit_enabled: false

# If re-enabled, use these production-safe values
wireguard_ratelimit_rate: "30/minute"  # Up from 10
wireguard_ratelimit_burst: 10          # Up from 5
```

**`roles/wireguard/tasks/main.yml`**:

- Added cleanup task for legacy aggressive rules
- Made rate limiting conditional
- Uses configurable rate/burst values

### Monitoring Recommendations

Add Prometheus alert for iptables drops:

```yaml
- alert: WireGuardRateLimitDrops
  expr: |
    increase(node_netfilter_nf_conntrack_events_total{event="drop"}[5m]) > 0
    and on(instance) label_replace(kube_node_info{}, "instance", "$1:9100", "node", "(.*)")
  for: 2m
  annotations:
    summary: "Possible WireGuard rate limiting on {{ $labels.instance }}"
    description: "Check iptables INPUT chain for DROP rules on UDP 51820"
```

### Alternative Protection Strategies

Instead of aggressive rate limiting:

1. **IP Allowlisting**: Only accept WireGuard from known GPU node NAT IPs
2. **Cloud Firewall**: AWS Security Group rate limiting at network edge
3. **Increased Limits**: If rate limiting needed, use 60/min burst 20
4. **WireGuard Native Protection**: Cookie-based DoS mitigation is built-in

---

## Decision Tree

```
kubectl logs returns 502 Bad Gateway?
├── YES
│   ├── Check node status: kubectl get nodes
│   │   ├── Node is Ready
│   │   │   └── Issue is kubelet reachability, not node health
│   │   └── Node is NotReady
│   │       └── Different issue - check agent logs
│   │
│   ├── SSH to GPU node, check WireGuard: sudo wg show wg0
│   │   ├── No handshakes (missing 'latest handshake')
│   │   │   └── Check peer registration on servers
│   │   ├── Handshakes present but ping fails
│   │   │   └── Check iptables rate limiting (this runbook)
│   │   └── Handshakes present and ping works
│   │       └── Check K3s API server routing
│   │
│   └── Check iptables on K3s servers
│       ├── DROP rule with high packet count
│       │   └── Rate limiting - REMOVE THE RULE
│       └── No DROP rules or low counts
│           └── Check other firewall layers (Security Groups)
│
└── NO (kubectl logs works)
    └── Different issue - check pod status directly
```

---

## Key Learnings

### Technical Lessons

1. **"Ready" doesn't mean "Reachable"**: K3s remotedialer masks bidirectional connectivity issues
2. **Rate limits must account for architecture**: 10/min is insufficient for 3-server × N-node setup
3. **Handshakes ≠ Traffic**: WireGuard handshakes can succeed while data traffic is blocked
4. **iptables packet counts are gold**: Drop counters immediately reveal rate limit impact

### Operational Lessons

1. **Check iptables FIRST when traffic fails but handshakes work**: This saves 30+ minutes of investigation
2. **Backup before changes**: iptables-save creates instant rollback capability
3. **Test bidirectionally**: One-way tests miss asymmetric issues
4. **Quantify the problem**: Drop counts prove causation, not just correlation

### Architecture Lessons

1. **Disable aggressive protections by default**: Enable only after production validation
2. **Make protections configurable**: Hardcoded limits break under edge cases
3. **Prefer allowlisting over rate limiting**: Known peers don't need rate limits
4. **Monitor infrastructure metrics**: iptables counters, WireGuard handshake age

---

## Appendix: Full Command Log

The following commands were executed during this investigation, in order:

```bash
# 1. Checked WireGuard peers on all K3s servers
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0 | grep -E 'peer|allowed|handshake'"

# 2. Tested bidirectional ping
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ping -c 1 -W 2 10.200.3.54 && ping -c 1 -W 2 10.200.99.150"

ssh shadeform@149.36.0.57 "for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do ping -c 1 -W 2 \$ip; done"
ssh shadeform@149.36.1.187 "for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do ping -c 1 -W 2 \$ip; done"

# 3. Checked iptables rate limiting (FOUND THE ISSUE)
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n -v --line-numbers | grep -E 'num|51820'"

# 4. Backed up iptables
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /root/iptables.backup.\$(date +%Y%m%d_%H%M%S)"

# 5. Removed rate limit rule
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -D INPUT 7"

# 6. Persisted changes
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /etc/iptables.rules.v4"

# 7. Restarted WireGuard on GPU nodes
ssh shadeform@149.36.1.187 "sudo systemctl restart wg-quick@wg0"
ssh shadeform@149.36.0.57 "sudo systemctl restart wg-quick@wg0"

# 8. Verified connectivity
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ping -c 1 10.200.3.54 && ping -c 1 10.200.99.150"

# 9. Tested kubectl logs
KUBECONFIG=~/.kube/k3s-basilica-config kubectl logs -n basilica-storage fuse-daemon-ddqjh --tail=3

# 10. Cleaned up crashed pod
KUBECONFIG=~/.kube/k3s-basilica-config kubectl delete pod -n basilica-storage fuse-daemon-mr25h
```

---

**Document Version**: 1.0
**Last Updated**: 2025-11-28
**Author**: Infrastructure Team
**Review Status**: Post-incident documentation
