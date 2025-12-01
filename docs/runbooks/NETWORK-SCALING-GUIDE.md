# Network Scaling Guide

**Audience**: Platform Engineers, Infrastructure Architects
**Purpose**: Guidelines for scaling the K3s + WireGuard + Flannel VXLAN network infrastructure
**Last Updated**: 2025-12-01

---

## Table of Contents

1. [Current Architecture Limits](#current-architecture-limits)
2. [Scaling GPU Nodes](#scaling-gpu-nodes)
3. [Scaling K3s Servers](#scaling-k3s-servers)
4. [Scaling K3s Agents](#scaling-k3s-agents)
5. [Network Capacity Planning](#network-capacity-planning)
6. [Performance Tuning](#performance-tuning)
7. [Monitoring at Scale](#monitoring-at-scale)
8. [Known Scaling Issues](#known-scaling-issues)

---

## Current Architecture Limits

### Tested Limits

| Component | Current | Tested Max | Hard Limit |
|-----------|---------|------------|------------|
| K3s Servers | 3 | 5 | 7 (etcd quorum) |
| K3s Agents | 5 | 20 | 100+ |
| GPU Nodes (WireGuard) | 10 | 50 | ~250 (WireGuard peers) |
| Pods per Node | 110 | 110 | 250 (K8s limit) |
| Total Pods | 1000 | 5000 | 10000+ |

### Network Limits

| Resource | Limit | Notes |
|----------|-------|-------|
| Pod CIDR | 10.42.0.0/16 | 65,536 IPs, /24 per node |
| WireGuard Network | 10.200.0.0/16 | 65,536 IPs |
| Service CIDR | 10.43.0.0/16 | 65,536 IPs |
| Max Nodes | 256 | With /24 per node |
| Flannel FDB entries | ~256 | One per node |

### Performance Baselines

| Metric | Expected | Alert Threshold |
|--------|----------|-----------------|
| WireGuard latency | < 5ms | > 20ms |
| Pod-to-pod latency (same node) | < 1ms | > 5ms |
| Pod-to-pod latency (cross-node) | < 10ms | > 50ms |
| VXLAN throughput | > 5 Gbps | < 1 Gbps |

---

## Scaling GPU Nodes

### Pre-Scaling Checklist

Before adding new GPU nodes:

1. [ ] Verify etcd cluster health
2. [ ] Check WireGuard peer count on servers
3. [ ] Verify available pod CIDR ranges
4. [ ] Ensure K3s token is valid
5. [ ] Test onboard.sh on staging first

### Adding GPU Nodes (1-10 nodes)

Standard procedure using onboard.sh:

```bash
# On new GPU node
export BASILICA_DATACENTER_ID="<datacenter-id>"
export BASILICA_DATACENTER_API_KEY="<api-key>"
curl -fsSL https://onboard.basilica.ai/install.sh | sudo bash
```

### Adding GPU Nodes (10-50 nodes)

For bulk onboarding, use parallel execution with rate limiting:

```bash
# Create inventory of new nodes
cat > /tmp/new-gpu-nodes.txt <<EOF
gpu-node-11 10.200.3.11
gpu-node-12 10.200.3.12
gpu-node-13 10.200.3.13
EOF

# Parallel onboard with 5 concurrent (prevent API rate limits)
cat /tmp/new-gpu-nodes.txt | xargs -P 5 -I {} bash -c '
    NODE_NAME=$(echo {} | cut -d" " -f1)
    ssh $NODE_NAME "
        export BASILICA_DATACENTER_ID=<datacenter-id>
        export BASILICA_DATACENTER_API_KEY=<api-key>
        export BASILICA_NODE_ID=$NODE_NAME
        curl -fsSL https://onboard.basilica.ai/install.sh | sudo bash
    "
    sleep 5  # Rate limit
'
```

### Post-Scaling Verification

```bash
# Verify all nodes joined
kubectl get nodes -l basilica.ai/wireguard=true

# Verify FDB entries on K3s servers
ansible k3s_server -i inventories/production.ini -m shell -a "
    bridge fdb show dev flannel.1 | wc -l
"

# Verify WireGuard peers
ansible k3s_server -i inventories/production.ini -m shell -a "
    wg show wg0 | grep -c 'peer:'
"
```

### Scaling Beyond 50 GPU Nodes

For large scale (50+ nodes):

1. **Consider WireGuard Hub-Spoke**: Instead of full mesh, use K3s servers as hubs
2. **Increase reconcile interval**: Reduce API server load
3. **Shard by datacenter**: Separate K3s clusters per region
4. **Monitor etcd performance**: Watch for slow queries

```bash
# Increase reconcile interval for large clusters
# In wireguard-peer-reconcile.timer
[Timer]
OnBootSec=60
OnUnitActiveSec=300  # Increase from 60s to 300s for large clusters
```

---

## Scaling K3s Servers

### When to Add K3s Servers

Add servers when:
- WireGuard peer count > 100 per server
- etcd latency > 100ms
- API server response time > 500ms
- Need geographic redundancy

### Adding a K3s Server

```bash
# 1. Provision new server in VPC
# 2. Run Ansible playbook
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/k3s-server-add.yml \
    -e "new_server_ip=10.101.3.100" \
    -e "new_server_name=k3s-server-4"

# 3. Verify etcd membership
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl member list

# 4. Update WireGuard peers on GPU nodes
# (Automatic via API during next onboard or manual update)
```

### K3s Server Capacity

| Server Count | Recommended Node Count | etcd Write Latency |
|--------------|----------------------|-------------------|
| 1 | < 20 | N/A (no HA) |
| 3 | 20-100 | < 50ms |
| 5 | 100-300 | < 100ms |
| 7 | 300-500 | < 150ms |

---

## Scaling K3s Agents

### When to Add K3s Agents

Add agents when:
- Need more VPC-local compute
- Pod scheduling constraints require VPC nodes
- Testing/staging workloads

### Adding K3s Agents

```bash
# Run Ansible playbook
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/k3s-agent-add.yml \
    -e "new_agent_ip=10.101.1.50" \
    -e "new_agent_name=k3s-agent-6"

# Apply WireGuard routes for new agent
ansible-playbook -i inventories/production.ini playbooks/wireguard-agent-routes.yml \
    -l k3s-agent-6
```

### Agent Scaling Considerations

- Agents don't need WireGuard interface (routes via K3s servers)
- Each agent gets a /24 pod CIDR
- Ensure VPC routing to 10.200.0.0/16 via K3s servers

---

## Network Capacity Planning

### Pod CIDR Allocation

Default: /24 per node = 254 pod IPs

```bash
# Check current allocations
kubectl get nodes -o custom-columns='NAME:.metadata.name,CIDR:.spec.podCIDR'

# Verify remaining capacity
USED=$(kubectl get nodes --no-headers | wc -l)
TOTAL=256  # /24 subnets in /16
REMAINING=$((TOTAL - USED))
echo "Remaining pod CIDRs: $REMAINING"
```

### WireGuard IP Allocation

```bash
# Check current WireGuard IPs
kubectl get nodes -l basilica.ai/wireguard=true \
    -o custom-columns='NAME:.metadata.name,WG_IP:.status.addresses[?(@.type=="InternalIP")].address'

# Plan new allocations
# Format: 10.200.<datacenter>.<node>
# Datacenter 1: 10.200.1.x
# Datacenter 2: 10.200.2.x
# etc.
```

### Bandwidth Planning

| Traffic Type | Expected BW | Scaling Factor |
|--------------|-------------|----------------|
| Pod-to-pod | 1-10 Gbps | Linear with pods |
| WireGuard overhead | ~20% | Constant |
| VXLAN overhead | ~10% | Constant |
| Control plane | 10-100 Mbps | Log(nodes) |

---

## Performance Tuning

### Sysctl Tuning for Scale

Already applied by onboard.sh, verify on existing nodes:

```bash
# Verify key settings
sysctl net.core.rmem_max  # Should be 67108864
sysctl net.core.netdev_max_backlog  # Should be 50000
sysctl net.netfilter.nf_conntrack_max  # Should be 1048576
```

### WireGuard Performance at Scale

```bash
# Increase WireGuard buffer sizes for high throughput
# /etc/wireguard/wg0.conf
[Interface]
# ... existing config ...
# Add for high-throughput scenarios:
# FwMark = 0x100  # Traffic marking for QoS
```

### Flannel VXLAN Tuning

```bash
# For high pod density, increase ARP cache
sysctl -w net.ipv4.neigh.default.gc_thresh1=16384
sysctl -w net.ipv4.neigh.default.gc_thresh2=65536
sysctl -w net.ipv4.neigh.default.gc_thresh3=131072
```

### RPS/RFS for Multi-Core

Verify RPS is enabled on WireGuard interface:

```bash
# Check current RPS configuration
for q in /sys/class/net/wg0/queues/rx-*/rps_cpus; do
    echo "$q: $(cat $q)"
done

# Should show hex mask for all CPUs (e.g., "ff" for 8 CPUs)
```

---

## Monitoring at Scale

### Key Metrics to Watch

```promql
# Node count by type
count(kube_node_labels{label_basilica_ai_wireguard="true"})
count(kube_node_labels{label_basilica_ai_wireguard!="true"})

# WireGuard peer health
sum(wireguard_peers) by (instance)
avg(time() - wireguard_latest_handshake_seconds) by (instance)

# VXLAN health
avg(vxlan_fdb_entries_total) by (instance)
sum(vxlan_stale_neighbor_entries) by (instance)
sum(flannel_route_via_wg0)  # Should be 0

# etcd performance (critical for scale)
histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m]))
etcd_server_proposals_pending
```

### Alerting Thresholds for Scale

| Metric | Small (< 20 nodes) | Medium (20-100) | Large (100+) |
|--------|-------------------|-----------------|--------------|
| WireGuard handshake age | 180s | 300s | 600s |
| FDB entry count | > 5 | > 20 | > 50 |
| etcd fsync latency | 50ms | 100ms | 200ms |
| API server latency p99 | 500ms | 1s | 2s |

### Capacity Dashboard

Create Grafana dashboard with:

1. **Node Capacity**: Current vs max nodes
2. **Pod CIDR Usage**: Allocated vs available /24s
3. **WireGuard Peers**: Count per server
4. **Network Latency**: Cross-node latency histogram
5. **Throughput**: WireGuard bytes in/out

---

## Known Scaling Issues

### Issue 1: FDB Entry Limit

**Symptom**: New GPU nodes can't communicate after ~250 nodes

**Cause**: Linux bridge FDB table default limit

**Solution**:
```bash
# Increase FDB limit
echo 4096 > /sys/class/net/flannel.1/bridge/hash_max
# Make persistent in sysctl
echo "net.bridge.bridge-nf-call-iptables = 1" >> /etc/sysctl.d/99-flannel.conf
```

### Issue 2: WireGuard Handshake Storm

**Symptom**: High CPU on K3s servers during bulk node addition

**Cause**: All new nodes initiating handshakes simultaneously

**Solution**:
```bash
# Stagger node onboarding (30s apart)
# In bulk onboard script, add sleep between nodes
sleep 30
```

### Issue 3: etcd Slow Queries

**Symptom**: kubectl commands slow, pods scheduling delayed

**Cause**: Too many node annotations being watched/updated

**Solution**:
- Increase reconcile interval
- Use labels instead of annotations for frequently-changing data
- Consider etcd defrag

```bash
# Defrag etcd
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl defrag
```

### Issue 4: Conntrack Table Exhaustion

**Symptom**: New connections failing, "nf_conntrack: table full" in dmesg

**Cause**: Too many concurrent connections at scale

**Solution**:
```bash
# Already set by onboard.sh, verify:
sysctl net.netfilter.nf_conntrack_max  # Should be 1048576

# If still hitting limits, increase further
sysctl -w net.netfilter.nf_conntrack_max=2097152
```

### Issue 5: DNS Resolution Delays

**Symptom**: Service discovery slow at scale

**Cause**: CoreDNS overwhelmed

**Solution**:
```bash
# Scale CoreDNS
kubectl -n kube-system scale deployment coredns --replicas=5

# Or use node-local DNS
kubectl apply -f https://raw.githubusercontent.com/kubernetes/kubernetes/master/cluster/addons/dns/nodelocaldns/nodelocaldns.yaml
```

---

## Scaling Decision Matrix

| Current State | Action | Timeline |
|---------------|--------|----------|
| < 20 GPU nodes | Standard onboard.sh | Immediate |
| 20-50 GPU nodes | Bulk onboard with rate limit | 1-2 hours |
| 50-100 GPU nodes | Add K3s server, tune sysctls | 4-8 hours |
| 100-200 GPU nodes | Multiple datacenters, sharding | 1-2 days |
| 200+ GPU nodes | Dedicated clusters per region | 1-2 weeks |

---

## Appendix: Capacity Calculator

```bash
#!/bin/bash
# Network Capacity Calculator

echo "=== Current Capacity ==="
NODES=$(kubectl get nodes --no-headers | wc -l)
GPU_NODES=$(kubectl get nodes -l basilica.ai/wireguard=true --no-headers | wc -l)
PODS=$(kubectl get pods -A --no-headers | wc -l)

echo "Total Nodes: $NODES / 256"
echo "GPU Nodes: $GPU_NODES / 250 (WireGuard limit)"
echo "Total Pods: $PODS"

echo -e "\n=== Available Capacity ==="
echo "Remaining Node Slots: $((256 - NODES))"
echo "Remaining GPU Slots: $((250 - GPU_NODES))"
echo "Pod Capacity: $((NODES * 110)) max"

echo -e "\n=== Recommendations ==="
if [ $GPU_NODES -gt 200 ]; then
    echo "WARNING: Approaching WireGuard peer limit. Consider sharding."
elif [ $GPU_NODES -gt 100 ]; then
    echo "INFO: Consider adding 4th K3s server for redundancy."
elif [ $GPU_NODES -gt 50 ]; then
    echo "INFO: Increase reconcile interval to 300s."
else
    echo "OK: Capacity is healthy."
fi
```
