# Network Maintenance Procedures

**Audience**: Platform Engineers, SREs
**Purpose**: Standard operating procedures for network maintenance tasks
**Last Updated**: 2025-12-01

---

## Table of Contents

1. [Overview](#overview)
2. [Scheduled Maintenance Windows](#scheduled-maintenance-windows)
3. [GPU Node Maintenance](#gpu-node-maintenance)
4. [K3s Server Maintenance](#k3s-server-maintenance)
5. [WireGuard Key Rotation](#wireguard-key-rotation)
6. [Flannel VXLAN Maintenance](#flannel-vxlan-maintenance)
7. [Network Configuration Updates](#network-configuration-updates)
8. [Emergency Procedures](#emergency-procedures)
9. [Post-Maintenance Verification](#post-maintenance-verification)

---

## Overview

This runbook covers routine and emergency maintenance procedures for the Basilica network infrastructure. All procedures include pre-flight checks, step-by-step instructions, and rollback procedures.

### Maintenance Types

| Type | Frequency | Downtime | Approval Required |
|------|-----------|----------|-------------------|
| GPU Node Reboot | As needed | Per-node | No |
| K3s Server Patch | Monthly | Rolling | Yes |
| WireGuard Key Rotation | Quarterly | None | Yes |
| Network Config Update | As needed | None | Yes |
| Emergency Repair | Unplanned | Varies | Post-hoc |

---

## Scheduled Maintenance Windows

### Standard Window

- **Day**: Tuesday or Wednesday
- **Time**: 02:00-06:00 UTC
- **Duration**: 4 hours maximum
- **Notification**: 48 hours advance notice

### Emergency Window

- **Trigger**: P1 incident affecting > 10% of users
- **Notification**: Immediate via PagerDuty
- **Approval**: VP Engineering or delegate

### Maintenance Checklist

Before any maintenance:

1. [ ] Create maintenance ticket
2. [ ] Notify stakeholders (Slack #platform-notices)
3. [ ] Verify backup procedures are current
4. [ ] Identify rollback procedure
5. [ ] Ensure on-call engineer is aware
6. [ ] Set status page to "Scheduled Maintenance"

---

## GPU Node Maintenance

### Procedure: Graceful Node Drain

Use when rebooting or updating a GPU node.

```bash
NODE_NAME="<gpu-node-name>"

# 1. Cordon node (prevent new pods)
kubectl cordon $NODE_NAME

# 2. Check running pods
kubectl get pods -A -o wide --field-selector spec.nodeName=$NODE_NAME

# 3. Notify affected users (if production pods)
# ... notification steps ...

# 4. Drain node (evict pods with 5-minute grace)
kubectl drain $NODE_NAME \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=300 \
    --timeout=600s

# 5. Perform maintenance
ssh $NODE_NAME "sudo reboot"

# 6. Wait for node to return
kubectl wait --for=condition=Ready node/$NODE_NAME --timeout=600s

# 7. Uncordon node
kubectl uncordon $NODE_NAME

# 8. Verify node health
kubectl get node $NODE_NAME
kubectl describe node $NODE_NAME | grep -A5 Conditions
```

### Procedure: Update GPU Drivers

```bash
NODE_NAME="<gpu-node-name>"

# 1. Drain node (as above)
kubectl cordon $NODE_NAME
kubectl drain $NODE_NAME --ignore-daemonsets --delete-emptydir-data

# 2. SSH to node and update drivers
ssh $NODE_NAME <<'EOF'
    # Stop K3s agent
    sudo systemctl stop k3s-agent

    # Update NVIDIA drivers
    sudo apt-get update
    sudo apt-get install -y nvidia-driver-550  # or desired version

    # Reboot
    sudo reboot
EOF

# 3. Wait for node
sleep 120
kubectl wait --for=condition=Ready node/$NODE_NAME --timeout=600s

# 4. Verify GPU
kubectl exec -it $(kubectl get pods -n kube-system -l app=nvidia-device-plugin -o name --field-selector spec.nodeName=$NODE_NAME) -- nvidia-smi

# 5. Uncordon
kubectl uncordon $NODE_NAME
```

### Procedure: Replace GPU Node

When a GPU node needs to be fully replaced:

```bash
OLD_NODE="<old-node-name>"
NEW_NODE_IP="<new-node-ip>"

# 1. Drain and delete old node
kubectl cordon $OLD_NODE
kubectl drain $OLD_NODE --ignore-daemonsets --delete-emptydir-data --force
kubectl delete node $OLD_NODE

# 2. Remove from WireGuard peers on K3s servers
ansible k3s_server -i inventories/production.ini -m shell -a "
    wg set wg0 peer <old-node-public-key> remove
"

# 3. Onboard new node
ssh $NEW_NODE_IP <<EOF
    export BASILICA_DATACENTER_ID="<datacenter-id>"
    export BASILICA_DATACENTER_API_KEY="<api-key>"
    curl -fsSL https://onboard.basilica.ai/install.sh | sudo bash
EOF

# 4. Verify new node
kubectl get node | grep $NEW_NODE_IP
```

---

## K3s Server Maintenance

### Procedure: Rolling Server Restart

Restart servers one at a time to maintain quorum:

```bash
# Servers: k3s-server-1, k3s-server-2, k3s-server-3

for SERVER in k3s-server-1 k3s-server-2 k3s-server-3; do
    echo "=== Restarting $SERVER ==="

    # 1. Verify etcd quorum before proceeding
    kubectl exec -n kube-system etcd-$SERVER -- etcdctl endpoint health

    # 2. Restart K3s
    ansible $SERVER -i inventories/production.ini -m shell -a "sudo systemctl restart k3s"

    # 3. Wait for server to rejoin
    sleep 60

    # 4. Verify health
    kubectl get nodes | grep $SERVER
    kubectl exec -n kube-system etcd-$SERVER -- etcdctl endpoint status

    # 5. Wait before next server
    sleep 120
done

# Final verification
kubectl get nodes
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl member list
```

### Procedure: K3s Version Upgrade

```bash
TARGET_VERSION="v1.31.2+k3s1"

for SERVER in k3s-server-1 k3s-server-2 k3s-server-3; do
    echo "=== Upgrading $SERVER to $TARGET_VERSION ==="

    # 1. Cordon all nodes on this server's subnet (if applicable)

    # 2. Upgrade K3s
    ansible $SERVER -i inventories/production.ini -m shell -a "
        curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=$TARGET_VERSION sh -
    "

    # 3. Wait for restart
    sleep 120

    # 4. Verify version
    kubectl get node $SERVER -o jsonpath='{.status.nodeInfo.kubeletVersion}'

    # 5. Verify workloads
    kubectl get pods -A -o wide | grep $SERVER | head -10

    # 6. Wait before next
    sleep 300
done
```

### Procedure: etcd Maintenance

```bash
# Check etcd health
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl endpoint health --cluster

# Check etcd size
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl endpoint status --cluster -w table

# Defragment etcd (one server at a time)
for SERVER in k3s-server-1 k3s-server-2 k3s-server-3; do
    echo "Defragging $SERVER..."
    kubectl exec -n kube-system etcd-$SERVER -- etcdctl defrag
    sleep 30
done

# Compact etcd (remove old revisions)
REVISION=$(kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl endpoint status -w json | jq -r '.[0].Status.header.revision')
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl compact $((REVISION - 10000))
```

---

## WireGuard Key Rotation

### Quarterly Key Rotation Procedure

```bash
# Phase 1: Generate new keys on K3s servers
ansible k3s_server -i inventories/production.ini -m shell -a "
    # Backup current keys
    cp /etc/wireguard/private.key /etc/wireguard/private.key.backup
    cp /etc/wireguard/public.key /etc/wireguard/public.key.backup

    # Generate new keys
    wg genkey | tee /etc/wireguard/private.key.new | wg pubkey > /etc/wireguard/public.key.new
"

# Phase 2: Update configuration files (dry run first)
# This requires updating all GPU node configs with new server public keys

# Phase 3: Coordinated cutover during maintenance window
# On each K3s server:
ansible k3s_server -i inventories/production.ini -m shell -a "
    mv /etc/wireguard/private.key.new /etc/wireguard/private.key
    mv /etc/wireguard/public.key.new /etc/wireguard/public.key
    chmod 600 /etc/wireguard/private.key
    # Note: wg0.conf needs to be regenerated with new private key
"

# Phase 4: Update GPU nodes
# Use API to push new server public keys to all GPU nodes

# Phase 5: Restart WireGuard everywhere
ansible k3s_server -i inventories/production.ini -m shell -a "
    systemctl restart wg-quick@wg0
"

# On each GPU node (via API or manual):
# systemctl restart wg-quick@wg0

# Phase 6: Verify connectivity
ansible k3s_server -i inventories/production.ini -m shell -a "
    wg show wg0 | head -20
"
```

---

## Flannel VXLAN Maintenance

### Procedure: Rebuild FDB/Neighbor Tables

When VXLAN tables become corrupted or stale:

```bash
SERVER="k3s-server-1"

# 1. Stop reconcile to prevent interference
ansible $SERVER -i inventories/production.ini -m shell -a "
    systemctl stop wireguard-peer-reconcile.timer
"

# 2. Flush tables
ansible $SERVER -i inventories/production.ini -m shell -a "
    bridge fdb flush dev flannel.1
    ip neigh flush dev flannel.1
"

# 3. Rebuild from K8s state
ansible $SERVER -i inventories/production.ini -m shell -a "
    # Get all GPU nodes and rebuild entries
    kubectl get nodes -l basilica.ai/wireguard=true -o json | jq -r '
        .items[] |
        \"\(.metadata.annotations[\"flannel.alpha.coreos.com/backend-data\"] | fromjson | .VtepMAC) \(.status.addresses[] | select(.type==\"InternalIP\") | .address) \(.spec.podCIDR | split(\"/\")[0])\"
    ' | while read MAC WG_IP VTEP_IP; do
        bridge fdb replace \$MAC dev flannel.1 dst \$WG_IP self permanent
        ip neigh replace \$VTEP_IP lladdr \$MAC dev flannel.1 nud permanent
    done
"

# 4. Restart reconcile
ansible $SERVER -i inventories/production.ini -m shell -a "
    systemctl start wireguard-peer-reconcile.timer
"

# 5. Verify
ansible $SERVER -i inventories/production.ini -m shell -a "
    bridge fdb show dev flannel.1 | head -10
    ip neigh show dev flannel.1 | head -10
"
```

### Procedure: Recreate flannel.1 Interface

When interface is corrupted:

```bash
NODE="<node-name>"

# 1. Get current config
VXLAN_ID=$(ssh $NODE "ip -d link show flannel.1 | grep -oP 'id \K\d+'")
LOCAL_IP=$(ssh $NODE "ip -d link show flannel.1 | grep -oP 'local \K[0-9.]+'")
CURRENT_MAC=$(ssh $NODE "ip link show flannel.1 | grep -oP 'link/ether \K[0-9a-f:]+'")

# 2. Delete interface
ssh $NODE "sudo ip link del flannel.1"

# 3. Recreate with same MAC
ssh $NODE "
    sudo ip link add flannel.1 type vxlan id $VXLAN_ID local $LOCAL_IP dev wg0 nolearning dstport 8472
    sudo ip link set flannel.1 address $CURRENT_MAC
    sudo ip link set flannel.1 up
"

# 4. Restart K3s agent to restore routes
ssh $NODE "sudo systemctl restart k3s-agent"

# 5. Verify
ssh $NODE "ip -d link show flannel.1"
```

---

## Network Configuration Updates

### Procedure: Update Sysctl Settings

```bash
# 1. Update template in Ansible
vim orchestrator/ansible/roles/performance_tuning/templates/99-wireguard-performance.conf.j2

# 2. Deploy to all nodes (check mode first)
ansible all -i inventories/production.ini --check -m template \
    -a "src=roles/performance_tuning/templates/99-wireguard-performance.conf.j2 dest=/etc/sysctl.d/99-wireguard-performance.conf"

# 3. Apply if check mode looks good
ansible all -i inventories/production.ini -m template \
    -a "src=roles/performance_tuning/templates/99-wireguard-performance.conf.j2 dest=/etc/sysctl.d/99-wireguard-performance.conf"

# 4. Reload sysctl
ansible all -i inventories/production.ini -m shell -a "sysctl --system"

# 5. Verify
ansible all -i inventories/production.ini -m shell -a "sysctl net.core.rmem_max"
```

### Procedure: Update WireGuard Reconcile Script

```bash
# 1. Update template
vim orchestrator/ansible/roles/wireguard/templates/wireguard-peer-reconcile.sh.j2

# 2. Deploy (check mode)
ansible k3s_server -i inventories/production.ini --check \
    -m template \
    -a "src=roles/wireguard/templates/wireguard-peer-reconcile.sh.j2 dest=/usr/local/bin/wireguard-peer-reconcile.sh mode=0755"

# 3. Deploy
ansible k3s_server -i inventories/production.ini \
    -m template \
    -a "src=roles/wireguard/templates/wireguard-peer-reconcile.sh.j2 dest=/usr/local/bin/wireguard-peer-reconcile.sh mode=0755"

# 4. Restart timer
ansible k3s_server -i inventories/production.ini -m systemd \
    -a "name=wireguard-peer-reconcile.timer state=restarted"

# 5. Verify
ansible k3s_server -i inventories/production.ini -m shell \
    -a "systemctl status wireguard-peer-reconcile.timer"
```

---

## Emergency Procedures

### Emergency: All GPU Nodes Unreachable

```bash
# 1. Check WireGuard on K3s servers
ansible k3s_server -i inventories/production.ini -m shell -a "wg show wg0"

# 2. If WireGuard down, restart
ansible k3s_server -i inventories/production.ini -m shell -a "systemctl restart wg-quick@wg0"

# 3. Check iptables (may be blocking)
ansible k3s_server -i inventories/production.ini -m shell -a "iptables -L -n | grep -i drop"

# 4. If iptables issue, flush and restore
ansible k3s_server -i inventories/production.ini -m shell -a "
    iptables-restore < /root/iptables.backup.latest
"
```

### Emergency: etcd Quorum Lost

```bash
# 1. Identify surviving member
kubectl exec -n kube-system etcd-k3s-server-1 -- etcdctl member list 2>/dev/null || \
kubectl exec -n kube-system etcd-k3s-server-2 -- etcdctl member list 2>/dev/null || \
kubectl exec -n kube-system etcd-k3s-server-3 -- etcdctl member list

# 2. If quorum lost, restore from backup
# See K3s disaster recovery documentation

# 3. Emergency single-node recovery
ssh k3s-server-1 "
    systemctl stop k3s
    # Remove cluster state
    rm -rf /var/lib/rancher/k3s/server/db/etcd
    # Start fresh (CAUTION: loses cluster state)
    systemctl start k3s
"
```

### Emergency: Network Partition

```bash
# 1. Identify partitioned nodes
kubectl get nodes -o wide | grep NotReady

# 2. Check WireGuard from both sides
ssh k3s-server-1 "ping -c 3 10.200.3.54"
ssh gpu-node-1 "ping -c 3 10.200.0.1"

# 3. Check for firewall blocks
ssh k3s-server-1 "iptables -L -n | grep DROP"

# 4. Restart WireGuard on affected nodes
ssh gpu-node-1 "systemctl restart wg-quick@wg0"
```

---

## Post-Maintenance Verification

### Standard Verification Checklist

```bash
#!/bin/bash
# Post-maintenance verification script

echo "=== Node Status ==="
kubectl get nodes -o wide

echo -e "\n=== Pod Health ==="
kubectl get pods -A | grep -v Running | grep -v Completed

echo -e "\n=== WireGuard Status ==="
ansible k3s_server -i inventories/production.ini -m shell -a "wg show wg0 | head -5"

echo -e "\n=== Flannel Health ==="
ansible k3s_server -i inventories/production.ini -m shell -a "
    echo 'FDB entries:' \$(bridge fdb show dev flannel.1 | wc -l)
    echo 'Neighbor entries:' \$(ip neigh show dev flannel.1 | wc -l)
"

echo -e "\n=== Test HTTP Endpoint ==="
INSTANCE_ID=$(kubectl get userdeployments -A -o jsonpath='{.items[0].status.instanceId}' 2>/dev/null)
if [ -n "$INSTANCE_ID" ]; then
    curl -sI "https://${INSTANCE_ID}.deployments.basilica.ai/" | head -1
else
    echo "No UserDeployments found"
fi

echo -e "\n=== Prometheus Alerts ==="
kubectl get prometheusrules -A 2>/dev/null || echo "Prometheus not installed"
```

### Extended Verification (After Major Maintenance)

```bash
# Run full diagnostic suite
./docs/runbooks/scripts/full-network-diagnostic.sh

# Check metrics for anomalies
# - WireGuard handshake ages
# - VXLAN entry counts
# - Pod network latency
# - Error rates in Envoy logs

# Verify random sample of user deployments
kubectl get userdeployments -A --no-headers | shuf | head -5 | while read NS NAME REST; do
    INSTANCE=$(kubectl get userdeployment -n $NS $NAME -o jsonpath='{.status.instanceId}')
    echo "Testing $INSTANCE..."
    curl -sI "https://${INSTANCE}.deployments.basilica.ai/" | head -1
done
```

---

## Appendix: Maintenance Scripts

### Script: Safe Node Drain

Save as `/usr/local/bin/safe-drain.sh`:

```bash
#!/bin/bash
set -euo pipefail

NODE=$1
GRACE=${2:-300}

echo "Draining node: $NODE with grace period: ${GRACE}s"

# Check node exists
kubectl get node $NODE || { echo "Node not found"; exit 1; }

# Cordon
kubectl cordon $NODE

# Show affected pods
echo "Pods to be evicted:"
kubectl get pods -A -o wide --field-selector spec.nodeName=$NODE | grep -v kube-system

# Confirm
read -p "Proceed with drain? (y/N) " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] || { kubectl uncordon $NODE; exit 1; }

# Drain
kubectl drain $NODE \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --grace-period=$GRACE \
    --timeout=600s

echo "Node $NODE drained successfully"
```

### Script: Network Health Check

Save as `/usr/local/bin/network-health.sh`:

```bash
#!/bin/bash
set -euo pipefail

ERRORS=0

# Check WireGuard
echo "Checking WireGuard..."
if ! wg show wg0 &>/dev/null; then
    echo "ERROR: WireGuard not running"
    ERRORS=$((ERRORS + 1))
fi

# Check flannel.1
echo "Checking flannel.1..."
if ! ip link show flannel.1 &>/dev/null; then
    echo "ERROR: flannel.1 not found"
    ERRORS=$((ERRORS + 1))
fi

# Check FDB entries
FDB_COUNT=$(bridge fdb show dev flannel.1 2>/dev/null | wc -l)
if [ "$FDB_COUNT" -lt 2 ]; then
    echo "ERROR: Low FDB count: $FDB_COUNT"
    ERRORS=$((ERRORS + 1))
fi

# Check for bad routes
BAD_ROUTES=$(ip route show | grep -c "10.42.*.*/24 dev wg0" || echo 0)
if [ "$BAD_ROUTES" -gt 0 ]; then
    echo "ERROR: $BAD_ROUTES pod routes via wg0"
    ERRORS=$((ERRORS + 1))
fi

if [ $ERRORS -eq 0 ]; then
    echo "OK: All network checks passed"
    exit 0
else
    echo "FAIL: $ERRORS errors found"
    exit 1
fi
```
