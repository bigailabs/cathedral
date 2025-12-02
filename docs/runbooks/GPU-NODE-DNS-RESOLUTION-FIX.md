# GPU Node DNS Resolution Fix Runbook

This runbook documents the diagnosis and resolution of DNS resolution failures on GPU nodes connected via WireGuard to the K3s cluster. The issue prevented user deployment pods on GPU nodes from resolving external hostnames like `pypi.org`, causing pip installations and other network operations to fail.

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Environment Context](#environment-context)
3. [Diagnosis Steps](#diagnosis-steps)
4. [Root Cause Analysis](#root-cause-analysis)
5. [Solution Implementation](#solution-implementation)
6. [Verification](#verification)
7. [Persistence Configuration](#persistence-configuration)
8. [Prevention and Monitoring](#prevention-and-monitoring)

---

## Problem Statement

### Symptom

User deployment pods running on GPU nodes (connected via WireGuard) failed to resolve DNS names:

```
socket.gaierror: [Errno -3] Temporary failure in name resolution
```

This caused:
- Python pip installations to fail
- HTTPS connections to external services to fail
- Any operation requiring DNS resolution to fail

### Impact

- User deployments with `storage="/data"` were scheduled to GPU nodes
- These deployments could not install dependencies or access external APIs
- The `userdeployment_sdk_example.py` test script failed

---

## Environment Context

### Cluster Topology

```
K3s Cluster:
- 3x k3s-server nodes (10.101.x.x - AWS VPC)
- 5x k3s-agent nodes (10.101.x.x - AWS VPC)
- 2x GPU nodes (10.200.x.x - WireGuard network)

Pod CIDR: 10.42.0.0/16
Service CIDR: 10.43.0.0/16
CoreDNS ClusterIP: 10.43.0.10
```

### Network Paths

```
GPU Pod -> wg0 -> k3s-server -> flannel.1 -> destination
         |                                         |
         +---- WireGuard Tunnel -------------------+
```

### Key Components

| Component | Location | Purpose |
|-----------|----------|---------|
| CoreDNS | k3s-agent-5 (initially) | Cluster DNS resolution |
| kube-dns Service | ClusterIP 10.43.0.10 | DNS service endpoint |
| Flannel | All nodes | Pod network (VXLAN) |
| WireGuard | k3s-servers | VPN tunnel for GPU nodes |

---

## Diagnosis Steps

### Step 1: Confirm DNS Failure

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-fea4-4db0-9d5e-a2b1ef13c840-deployment-6499558574bcztk -- \
  python3 -c "import socket; print(socket.gethostbyname('pypi.org'))"
```

**Result:**
```
Traceback (most recent call last):
  File "<string>", line 1, in <module>
socket.gaierror: [Errno -3] Temporary failure in name resolution
```

**Explanation:** This confirmed DNS resolution was failing inside the user pod on the GPU node.

### Step 2: Verify Pod resolv.conf

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- cat /etc/resolv.conf
```

**Result:**
```
search u-github-434149.svc.cluster.local svc.cluster.local cluster.local openstacklocal
nameserver 10.43.0.10
options ndots:5
```

**Explanation:** The pod's DNS configuration was correct, pointing to the kube-dns ClusterIP.

### Step 3: Test Direct UDP Connectivity to CoreDNS

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- python3 -c "
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)
query = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x04pypi\x03org\x00\x00\x01\x00\x01'
sock.sendto(query, ('10.43.0.10', 53))
response, addr = sock.recvfrom(512)
print(f'Got {len(response)} bytes from {addr}')
"
```

**Result:**
```
socket.timeout: timed out
```

**Explanation:** Raw DNS queries to the ClusterIP also timed out, confirming the issue was at the network layer, not the resolver.

### Step 4: Check CoreDNS Pod Location

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n kube-system \
  -l k8s-app=kube-dns -o wide
```

**Result:**
```
NAME                       READY   STATUS    IP           NODE
coredns-56f6fc8fd7-zbs8t   1/1     Running   10.42.7.13   k3s-agent-5
```

**Explanation:** CoreDNS was running only on k3s-agent-5 (AWS VPC node), not reachable directly from GPU nodes.

### Step 5: Test Pod-to-Pod Connectivity

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- python3 -c "
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)
query = b'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x04pypi\x03org\x00\x00\x01\x00\x01'
sock.sendto(query, ('10.42.7.13', 53))  # Direct to CoreDNS pod IP
response, addr = sock.recvfrom(512)
print(f'Got {len(response)} bytes')
"
```

**Result:**
```
socket.timeout: timed out
```

**Explanation:** Even direct pod-to-pod communication failed, indicating a fundamental routing issue.

### Step 6: Check GPU Node Routing Table

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl debug node/8a2fbf46-3b34-42c3-b62d-4d9b66ea9a1a \
  --image=nicolaka/netshoot -- sh -c "ip route | grep -E '10.42|flannel'"
```

**Result:**
```
10.42.0.0/16 dev wg0 scope link
10.42.17.0/24 dev cni0 proto kernel scope link src 10.42.17.1
```

**Explanation:** GPU node routes ALL pod traffic (10.42.0.0/16) through WireGuard, not Flannel VXLAN. This is by design for GPU nodes.

### Step 7: Check K3s Server Flannel Routes

**Command:**
```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "ip route | grep -E '10.42.7|flannel'" --limit server1
```

**Result:**
```
10.42.7.0/24 via 10.42.7.0 dev flannel.1 onlink
```

**Explanation:** K3s servers route traffic to CoreDNS pod CIDR via Flannel VXLAN, as expected.

### Step 8: Check Conntrack Entries

**Command:**
```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "conntrack -L 2>/dev/null | grep -E '10.42.17|10.43.0.10' | grep 53 | head -10" \
  --limit server1
```

**Result:**
```
udp  17 141 src=10.42.17.8 dst=10.42.7.13 sport=34982 dport=53 src=10.42.7.13 dst=10.42.0.0 sport=53 dport=37674 [ASSURED]
udp  17 39 src=10.42.17.30 dst=10.42.7.13 sport=35519 dport=53 src=10.42.7.13 dst=10.42.0.0 sport=53 dport=17591
```

**Explanation:** CRITICAL FINDING! The return path shows `dst=10.42.0.0` instead of the original source (`10.42.17.x`). This means SNAT was incorrectly applied to forwarded traffic.

### Step 9: Check Flannel NAT Rules

**Command:**
```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "iptables -t nat -L FLANNEL-POSTRTG -n -v" --limit server1
```

**Result:**
```
Chain FLANNEL-POSTRTG (1 references)
 pkts bytes target     prot opt in     out     source               destination
    0     0 RETURN     0    --  *      *       0.0.0.0/0            0.0.0.0/0            mark match 0x4000/0x4000
 517K   53M RETURN     0    --  *      *       10.42.0.0/24         10.42.0.0/16
    0     0 RETURN     0    --  *      *       10.42.0.0/16         10.42.0.0/24
    0     0 RETURN     0    --  *      *      !10.42.0.0/16         10.42.0.0/24
83725 8046K MASQUERADE  0    --  *      *       10.42.0.0/16        !224.0.0.0/4
 6465  582K MASQUERADE  0    --  *      *      !10.42.0.0/16         10.42.0.0/16
```

**Explanation:** Flannel's MASQUERADE rule applies to all traffic from pod CIDR (10.42.0.0/16) to non-multicast destinations. When GPU pod traffic is forwarded through the server, this rule incorrectly masquerades it.

---

## Root Cause Analysis

### The Problem Flow

1. **GPU pod sends DNS query**: Pod on GPU node (10.42.17.x) sends UDP packet to CoreDNS ClusterIP (10.43.0.10)

2. **Packet traverses WireGuard**: GPU node routes 10.42.0.0/16 via wg0 to k3s-server

3. **Server performs DNAT**: kube-proxy on server DNAT's 10.43.0.10 to CoreDNS pod IP (10.42.7.13)

4. **Flannel MASQUERADE applies incorrectly**: Since the packet has source 10.42.17.x (pod CIDR), Flannel's MASQUERADE rule changes source IP to server's cni0 address (10.42.0.1)

5. **Packet reaches CoreDNS**: CoreDNS receives query and sends response

6. **Response goes to wrong destination**: Return packet is sent to 10.42.0.1 (server's cni0) instead of original source (10.42.17.x)

7. **DNS timeout**: GPU pod never receives response

### Why This Only Affects GPU Nodes

- GPU nodes route all pod traffic through WireGuard to k3s-servers
- k3s-servers act as routers, forwarding traffic to other nodes via Flannel
- Flannel's MASQUERADE was designed for local pod traffic, not forwarded traffic
- Regular k3s-agents use Flannel VXLAN directly, bypassing this issue

---

## Solution Implementation

### Fix 1: Skip MASQUERADE for Forwarded Pod Traffic

**Purpose:** Prevent Flannel from masquerading pod-to-pod traffic that's being forwarded through the server.

**Command (applied to all k3s-servers):**
```bash
iptables -t nat -I FLANNEL-POSTRTG 1 -s 10.42.0.0/16 -d 10.42.0.0/16 -j RETURN \
  -m comment --comment "flannel skip forwarded pod traffic"
```

**Verification:**
```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "iptables -t nat -L FLANNEL-POSTRTG -n -v | head -5"
```

**Result:**
```
Chain FLANNEL-POSTRTG (1 references)
 pkts bytes target     prot opt in     out     source               destination
   37  4253 RETURN     0    --  *      *       10.42.0.0/16         10.42.0.0/16         /* flannel skip forwarded pod traffic */
```

### Fix 2: Scale CoreDNS for Local Availability

**Purpose:** Ensure CoreDNS pods run on GPU nodes so DNS queries can be served locally.

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl scale deployment coredns \
  -n kube-system --replicas=3
```

**Verification:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get pods -n kube-system \
  -l k8s-app=kube-dns -o wide
```

**Result:**
```
NAME                       READY   STATUS    IP            NODE
coredns-56f6fc8fd7-9xrzl   1/1     Running   10.42.15.25   fa09143c-263d-431f-b919-eef0fb8e69d2
coredns-56f6fc8fd7-bdxdg   1/1     Running   10.42.17.33   8a2fbf46-3b34-42c3-b62d-4d9b66ea9a1a
coredns-56f6fc8fd7-zbs8t   1/1     Running   10.42.7.13    k3s-agent-5
```

CoreDNS now runs on the GPU node (8a2fbf46...).

### Fix 3: Set internalTrafficPolicy to Local

**Purpose:** Force kube-proxy to route DNS traffic only to local CoreDNS pods, avoiding cross-node traffic.

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl patch svc kube-dns -n kube-system \
  -p '{"spec":{"internalTrafficPolicy":"Local"}}'
```

**Verification:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get svc kube-dns -n kube-system \
  -o jsonpath='{.spec.internalTrafficPolicy}'
```

**Result:**
```
Local
```

---

## Verification

### Test 1: DNS Resolution Success Rate

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- python3 -c "
import socket
success = 0
for i in range(20):
    try:
        socket.gethostbyname('pypi.org')
        success += 1
    except: pass
print(f'Success rate: {success}/20 ({100*success/20:.0f}%)')
"
```

**Result:**
```
Success rate: 20/20 (100%)
```

### Test 2: External Service Connectivity

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- python3 -c "
import urllib.request, ssl
ctx = ssl.create_default_context()
req = urllib.request.Request('https://pypi.org/simple/', method='HEAD')
with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
    print(f'PyPI: HTTP {response.status}')
"
```

**Result:**
```
PyPI: HTTP 200
```

### Test 3: Multiple Domain Resolution

**Command:**
```bash
KUBECONFIG=~/.kube/k3s-basilica-config kubectl exec -n u-github-434149 \
  3f2f7fe4-...-deployment-... -- python3 -c "
import socket
for domain in ['pypi.org', 'files.pythonhosted.org', 'google.com', 'github.com']:
    try:
        ip = socket.gethostbyname(domain)
        print(f'{domain}: {ip}')
    except Exception as e:
        print(f'{domain}: FAILED ({e})')
"
```

**Result:**
```
pypi.org: 151.101.192.223
files.pythonhosted.org: 151.101.0.223
google.com: 142.250.74.14
github.com: 20.26.156.215
```

---

## Persistence Configuration

### Ansible Role Updates

#### 1. WireGuard Role (`orchestrator/ansible/roles/wireguard/tasks/main.yml`)

Added task to skip MASQUERADE for forwarded pod traffic:

```yaml
# Fix for DNS resolution on GPU nodes:
# When GPU node pods send traffic to other cluster pods, packets traverse:
# GPU pod -> wg0 -> k3s-server -> flannel.1 -> destination pod
# Without this rule, Flannel's MASQUERADE changes source IP to server's cni0 IP,
# breaking return traffic. This rule skips MASQUERADE for forwarded pod-to-pod traffic.
- name: Skip MASQUERADE for forwarded pod-to-pod traffic (DNS fix)
  ansible.builtin.shell: |
    # Check if rule already exists
    if iptables -t nat -C FLANNEL-POSTRTG -s 10.42.0.0/16 -d 10.42.0.0/16 -j RETURN -m comment --comment "flannel skip forwarded pod traffic" 2>/dev/null; then
      echo "Rule already exists"
      exit 0
    fi
    # Insert at position 1 (before MASQUERADE rules)
    iptables -t nat -I FLANNEL-POSTRTG 1 -s 10.42.0.0/16 -d 10.42.0.0/16 -j RETURN -m comment --comment "flannel skip forwarded pod traffic"
    echo "Rule added"
  register: flannel_masq_fix
  changed_when: "'Rule added' in flannel_masq_fix.stdout"
  tags: ['wireguard', 'security', 'dns-fix']
```

Added VPC-to-WireGuard MASQUERADE rule:

```yaml
# MASQUERADE VPC traffic going out to WireGuard peers
# This allows k3s-agents (on VPC network) to reach GPU nodes (on WireGuard network)
# via the k3s-servers which act as WireGuard gateways.
- name: Add MASQUERADE rule for VPC traffic to WireGuard
  ansible.builtin.iptables:
    table: nat
    chain: POSTROUTING
    source: "10.101.0.0/16"
    out_interface: "{{ wireguard_interface }}"
    jump: MASQUERADE
    comment: "NAT VPC to WireGuard for agent connectivity"
  tags: ['wireguard', 'iptables']
```

#### 2. K3s Server Post Role (`orchestrator/ansible/roles/k3s_server_post/tasks/coredns-affinity.yml`)

Updated CoreDNS configuration:

```yaml
- name: Scale CoreDNS to ensure redundancy and local availability
  ansible.builtin.command:
    cmd: kubectl -n kube-system scale deployment coredns --replicas=3
  register: coredns_scale
  changed_when: "'scaled' in coredns_scale.stdout"
  tags: ['k3s', 'post', 'coredns']

# Set internalTrafficPolicy to Local for GPU node DNS reliability
# When WireGuard is enabled, cross-node pod traffic may be unreliable.
# Setting Local ensures pods use the CoreDNS instance on their own node,
# avoiding cross-node traffic through WireGuard.
- name: Patch kube-dns service to use local traffic policy
  ansible.builtin.command:
    cmd: kubectl -n kube-system patch svc kube-dns -p '{"spec":{"internalTrafficPolicy":"Local"}}'
  register: dns_svc_patch
  changed_when: "'patched' in dns_svc_patch.stdout"
  tags: ['k3s', 'post', 'coredns']
```

### iptables Persistence

**Command:**
```bash
ansible -i orchestrator/ansible/inventories/production.ini k3s_server \
  -m shell -a "iptables-save > /etc/iptables.rules.v4"
```

Rules are restored on boot via the `iptables-restore.service` systemd unit.

---

## Prevention and Monitoring

### Alerting

Consider adding Prometheus alerts for:

1. **DNS query latency** from GPU nodes
2. **CoreDNS pod distribution** - alert if no CoreDNS on GPU nodes
3. **iptables rule presence** - verify FLANNEL-POSTRTG fix exists

### Pre-deployment Checklist

Before onboarding new GPU nodes:

1. Verify WireGuard role includes the Flannel MASQUERADE fix
2. Confirm CoreDNS replicas >= number of node groups
3. Check `internalTrafficPolicy: Local` on kube-dns service

### Testing New GPU Nodes

After onboarding, run:

```bash
# Test DNS from a pod on the new GPU node
kubectl run dns-test --image=busybox:1.36 --rm -it --restart=Never \
  --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"NEW_GPU_NODE"}}}' \
  -- nslookup pypi.org
```

---

## Summary

| Issue | Fix | Location |
|-------|-----|----------|
| Flannel MASQUERADE breaking return traffic | iptables RETURN rule for pod-to-pod | k3s-servers |
| CoreDNS not available on GPU nodes | Scale to 3 replicas | kube-system/coredns |
| kube-proxy selecting unreachable endpoints | internalTrafficPolicy: Local | kube-system/kube-dns service |

### Files Modified

1. `orchestrator/ansible/roles/wireguard/tasks/main.yml` - Flannel fix + VPC NAT
2. `orchestrator/ansible/roles/k3s_server_post/tasks/coredns-affinity.yml` - CoreDNS config

### Commands for Future Reference

```bash
# Check Flannel MASQUERADE rules
iptables -t nat -L FLANNEL-POSTRTG -n -v

# Check CoreDNS pod distribution
kubectl get pods -n kube-system -l k8s-app=kube-dns -o wide

# Check kube-dns service traffic policy
kubectl get svc kube-dns -n kube-system -o jsonpath='{.spec.internalTrafficPolicy}'

# Test DNS from GPU node pod
kubectl exec -n NAMESPACE POD_NAME -- python3 -c "import socket; print(socket.gethostbyname('pypi.org'))"
```
