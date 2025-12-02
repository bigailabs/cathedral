# HTTP 503 Diagnosis Runbook

**Audience**: Platform Engineers, SREs, On-Call Engineers
**Purpose**: Systematically diagnose and resolve HTTP 503 errors for UserDeployments on GPU nodes
**Last Updated**: 2025-12-01

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Triage Decision Tree](#quick-triage-decision-tree)
3. [Layer-by-Layer Diagnosis](#layer-by-layer-diagnosis)
4. [Root Cause Categories](#root-cause-categories)
5. [Resolution Procedures](#resolution-procedures)
6. [Verification Steps](#verification-steps)
7. [Escalation Criteria](#escalation-criteria)

---

## Overview

HTTP 503 "Service Unavailable" errors for UserDeployments indicate the request reached Envoy Gateway but could not be forwarded to the backend pod. This runbook provides a systematic approach to identify the failure point.

### Request Path

```
User Browser
     |
     v
[CloudFlare/CDN]
     |
     v
[AWS ALB] (port 443)
     |
     v
[Envoy Gateway Pod] (envoy-gateway-system namespace)
     |
     | HTTPRoute matching
     v
[Backend Service] (u-{user-id} namespace)
     |
     | ClusterIP -> Pod IP
     v
[User Pod] (on GPU node via WireGuard)
```

### Common Failure Points

| Layer | Symptom | Likely Cause |
|-------|---------|--------------|
| Envoy -> Service | 503 immediate | Service not found, no endpoints |
| Service -> Pod | 503 with delay | Pod unreachable, network issue |
| Pod internal | 503 + logs | Application crash, OOM, timeout |

---

## Quick Triage Decision Tree

```
HTTP 503 Error
     |
     +-- Is the deployment running?
     |        |
     |        +-- No --> Check deployment status (Section 4.1)
     |        |
     |        +-- Yes --> Is the pod on a GPU node?
     |                        |
     |                        +-- No --> Standard K8s networking issue
     |                        |
     |                        +-- Yes --> Is WireGuard healthy?
     |                                        |
     |                                        +-- No --> See WIREGUARD-TROUBLESHOOTING.md
     |                                        |
     |                                        +-- Yes --> Is Flannel VXLAN healthy?
     |                                                        |
     |                                                        +-- No --> See FLANNEL-VXLAN-TROUBLESHOOTING.md
     |                                                        |
     |                                                        +-- Yes --> Check application logs
```

---

## Layer-by-Layer Diagnosis

### Step 1: Identify the Deployment

```bash
# Get deployment details from instance ID
INSTANCE_ID="<instance-id-from-url>"
NAMESPACE="u-$(kubectl get userdeployments -A -o json | jq -r ".items[] | select(.status.instanceId == \"$INSTANCE_ID\") | .metadata.namespace" | sed 's/u-//')"

# Or find by hostname
HOSTNAME="${INSTANCE_ID}.deployments.basilica.ai"
kubectl get httproutes -A -o json | jq -r ".items[] | select(.spec.hostnames[] | contains(\"$HOSTNAME\"))"
```

### Step 2: Check Deployment and Pod Status

```bash
# Find the UserDeployment
kubectl get userdeployments -A | grep $INSTANCE_ID

# Check pod status
kubectl get pods -n $NAMESPACE -o wide

# Check pod events
kubectl describe pod -n $NAMESPACE -l app=$INSTANCE_ID
```

**What to look for:**

- Pod in `Running` state with `1/1` ready
- No recent restarts
- Pod scheduled on expected node
- No pending events

### Step 3: Check Service and Endpoints

```bash
# Check service exists
kubectl get svc -n $NAMESPACE -l app=$INSTANCE_ID

# Check endpoints (should have pod IP)
kubectl get endpoints -n $NAMESPACE -l app=$INSTANCE_ID

# Detailed endpoint check
kubectl describe endpoints -n $NAMESPACE <service-name>
```

**What to look for:**

- Service exists with correct selector
- Endpoints list shows pod IP:port
- No "NotReadyAddresses"

### Step 4: Check HTTPRoute Configuration

```bash
# Find the HTTPRoute
kubectl get httproutes -n $NAMESPACE

# Check HTTPRoute details
kubectl describe httproute -n $NAMESPACE <route-name>

# Verify parent gateway reference
kubectl get httproute -n $NAMESPACE <route-name> -o yaml | grep -A5 parentRefs
```

**What to look for:**

- Route attached to `basilica-gateway`
- Correct hostname configured
- Backend reference matches service name
- Status shows `Accepted: True`

### Step 5: Check Envoy Gateway

```bash
# Check Envoy pods are running
kubectl get pods -n envoy-gateway-system

# Check Envoy logs for errors
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=basilica-gateway --tail=100 | grep -i error

# Check if route is programmed in Envoy
kubectl exec -n envoy-gateway-system -it $(kubectl get pods -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=basilica-gateway -o name | head -1) -- curl -s localhost:19000/config_dump | jq '.configs[] | select(.["@type"] | contains("RoutesConfigDump"))'
```

### Step 6: Check Network Path to Pod

```bash
# Get pod details
POD_NAME=$(kubectl get pods -n $NAMESPACE -l app=$INSTANCE_ID -o name | head -1)
POD_IP=$(kubectl get $POD_NAME -n $NAMESPACE -o jsonpath='{.status.podIP}')
NODE_NAME=$(kubectl get $POD_NAME -n $NAMESPACE -o jsonpath='{.spec.nodeName}')

echo "Pod: $POD_NAME"
echo "Pod IP: $POD_IP"
echo "Node: $NODE_NAME"

# Check if node is a GPU node
kubectl get node $NODE_NAME -o jsonpath='{.metadata.labels.basilica\.ai/wireguard}'
```

### Step 7: Test Connectivity from Envoy

```bash
# Get an Envoy pod
ENVOY_POD=$(kubectl get pods -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=basilica-gateway -o name | head -1)

# Test HTTP connectivity to user pod
kubectl exec -n envoy-gateway-system $ENVOY_POD -- curl -v --connect-timeout 5 http://$POD_IP:8000/

# Test TCP connectivity
kubectl exec -n envoy-gateway-system $ENVOY_POD -- nc -zv $POD_IP 8000
```

**What to look for:**

- Connection timeout = network routing issue
- Connection refused = pod not listening
- HTTP error = application issue

### Step 8: Check Flannel VXLAN (if GPU node)

```bash
# SSH to K3s server and check VXLAN health
ansible k3s_server -i inventories/production.ini -m shell -a "
    echo '=== FDB for GPU node MAC ==='
    bridge fdb show dev flannel.1 | grep -i \$(kubectl get node $NODE_NAME -o jsonpath='{.metadata.annotations.flannel\.alpha\.coreos\.com/backend-data}' | jq -r '.VtepMAC')

    echo '=== Route to pod CIDR ==='
    ip route get $POD_IP

    echo '=== Ping VTEP ==='
    POD_CIDR=\$(kubectl get node $NODE_NAME -o jsonpath='{.spec.podCIDR}')
    VTEP_IP=\${POD_CIDR%/*}
    ping -c 1 -W 2 \$VTEP_IP
"
```

---

## Root Cause Categories

### Category 1: Deployment Not Ready

**Symptoms:**

- Pod not in Running state
- Endpoints list is empty
- Recent OOMKilled or CrashLoopBackOff

**Resolution:**

```bash
# Check pod logs
kubectl logs -n $NAMESPACE $POD_NAME --tail=100

# Check events
kubectl get events -n $NAMESPACE --sort-by='.lastTimestamp' | tail -20

# Restart deployment if stuck
kubectl rollout restart deployment -n $NAMESPACE <deployment-name>
```

### Category 2: Service/Endpoint Misconfiguration

**Symptoms:**

- Service exists but endpoints empty
- Label selector mismatch

**Resolution:**

```bash
# Check label match
SVC_SELECTOR=$(kubectl get svc -n $NAMESPACE <svc-name> -o jsonpath='{.spec.selector}')
kubectl get pods -n $NAMESPACE -l "$SVC_SELECTOR"

# If mismatch, check UserDeployment controller
kubectl logs -n basilica-system deployment/basilica-operator | grep $INSTANCE_ID
```

### Category 3: HTTPRoute Not Programmed

**Symptoms:**

- HTTPRoute status not Accepted
- Route not in Envoy config dump

**Resolution:**

```bash
# Check HTTPRoute status
kubectl get httproute -n $NAMESPACE -o yaml | grep -A10 status

# Check Envoy Gateway controller logs
kubectl logs -n envoy-gateway-system deployment/envoy-gateway | grep $NAMESPACE
```

### Category 4: Network Routing Failure (GPU Nodes)

**Symptoms:**

- Timeout connecting to pod IP
- Works for VPC nodes, fails for GPU nodes
- Alerts: `FlannelRouteViaWG0`, `VXLANFDBEntriesLow`

**Resolution:**
See [FLANNEL-VXLAN-TROUBLESHOOTING.md](./FLANNEL-VXLAN-TROUBLESHOOTING.md)

Quick fix:

```bash
# On K3s server, restart reconcile
systemctl restart wireguard-peer-reconcile.service

# Verify route exists
ip route get $POD_IP
```

### Category 4b: Missing flannel.1 Interface on GPU Node

**Symptoms:**

- Timeout connecting to pod IP on specific GPU node
- WireGuard tunnel is UP (ping to WG IP works)
- VXLAN/VTEP ping fails (ping to 10.42.x.0 fails)
- Syslog on GPU node shows: `flannel.1: Link DOWN`

**Resolution:**
See [GPU-NODE-FLANNEL-INTERFACE-RECOVERY.md](./GPU-NODE-FLANNEL-INTERFACE-RECOVERY.md)

Quick fix:

```bash
# SSH to GPU node and restart K3s agent
ssh <user>@<gpu-node-ip> 'sudo systemctl restart k3s-agent'

# Then update FDB/routes on K3s servers or trigger reconciliation
kubectl create job --from=cronjob/wireguard-reconcile wireguard-reconcile-manual-$(date +%s) -n kube-system
```

### Category 5: WireGuard Tunnel Down

**Symptoms:**

- GPU node shows NotReady
- WireGuard handshake stale
- Alerts: `WireGuardPeerDisconnected`

**Resolution:**
See [WIREGUARD-TROUBLESHOOTING.md](./WIREGUARD-TROUBLESHOOTING.md)

Quick fix:

```bash
# On GPU node
sudo systemctl restart wg-quick@wg0

# Or trigger watchdog recovery
sudo systemctl restart wireguard-watchdog
```

### Category 6: NetworkPolicy Blocking Traffic

**Symptoms:**

- Connection refused or timeout
- Pod logs show no incoming requests
- NetworkPolicy exists in namespace

**Resolution:**

```bash
# List NetworkPolicies
kubectl get networkpolicies -n $NAMESPACE

# Check if Envoy is allowed
kubectl get networkpolicy -n $NAMESPACE -o yaml | grep -A20 ingress

# Temporarily delete NetworkPolicy to test (CAUTION)
# kubectl delete networkpolicy -n $NAMESPACE <policy-name>
```

---

## Resolution Procedures

### Quick Recovery: Restart Pod

```bash
kubectl delete pod -n $NAMESPACE $POD_NAME
# Wait for new pod to schedule
kubectl wait --for=condition=Ready pod -n $NAMESPACE -l app=$INSTANCE_ID --timeout=120s
```

### Quick Recovery: Recreate Service

```bash
# Delete and let operator recreate
kubectl delete svc -n $NAMESPACE <service-name>
# Operator should recreate within 30s
```

### Quick Recovery: Force HTTPRoute Reconcile

```bash
# Add annotation to trigger reconcile
kubectl annotate httproute -n $NAMESPACE <route-name> force-reconcile=$(date +%s) --overwrite
```

### Network Recovery: Flush and Rebuild

```bash
# On K3s server
systemctl restart wireguard-peer-reconcile.service

# If that doesn't work, manual rebuild
ip route flush dev flannel.1
bridge fdb flush dev flannel.1
ip neigh flush dev flannel.1
systemctl restart wireguard-peer-reconcile.service
```

---

## Verification Steps

After any fix, verify:

```bash
# 1. Pod is running
kubectl get pods -n $NAMESPACE -l app=$INSTANCE_ID

# 2. Endpoints populated
kubectl get endpoints -n $NAMESPACE -l app=$INSTANCE_ID

# 3. HTTPRoute accepted
kubectl get httproute -n $NAMESPACE -o jsonpath='{.items[*].status.parents[*].conditions[?(@.type=="Accepted")].status}'

# 4. HTTP request succeeds
curl -sI https://$INSTANCE_ID.deployments.basilica.ai/ | head -1

# 5. No 503 in recent Envoy logs
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=basilica-gateway --since=5m | grep -c "503" | grep -q "^0$" && echo "OK: No recent 503s"
```

---

## Escalation Criteria

Escalate to senior engineer if:

1. **Time limit exceeded**: Issue not resolved within 30 minutes
2. **Multiple deployments affected**: More than 3 deployments have 503 errors
3. **Infrastructure failure**: K3s server or Envoy Gateway pods failing
4. **Unknown root cause**: All diagnostic steps show healthy but 503 persists
5. **Data loss risk**: Deployment contains stateful data that may be lost

### Escalation Information to Gather

```bash
# Collect diagnostic bundle
DIAG_DIR="/tmp/503-diag-$(date +%Y%m%d_%H%M%S)"
mkdir -p $DIAG_DIR

kubectl get pods -A -o wide > $DIAG_DIR/pods.txt
kubectl get svc -A > $DIAG_DIR/services.txt
kubectl get endpoints -A > $DIAG_DIR/endpoints.txt
kubectl get httproutes -A -o yaml > $DIAG_DIR/httproutes.yaml
kubectl get events -A --sort-by='.lastTimestamp' > $DIAG_DIR/events.txt
kubectl logs -n envoy-gateway-system -l gateway.envoyproxy.io/owning-gateway-name=basilica-gateway --tail=500 > $DIAG_DIR/envoy-logs.txt

# On K3s servers
ansible k3s_server -i inventories/production.ini -m shell -a "
    ip route show
    bridge fdb show dev flannel.1
    ip neigh show dev flannel.1
    wg show wg0
" > $DIAG_DIR/network-state.txt

tar -czf $DIAG_DIR.tar.gz $DIAG_DIR
echo "Diagnostic bundle: $DIAG_DIR.tar.gz"
```

### Escalation Contacts

| Severity | Contact Method | Response Time |
|----------|---------------|---------------|
| P1 (Outage) | PagerDuty | 15 min |
| P2 (Degraded) | Slack #incidents | 1 hour |
| P3 (Single user) | Slack #support | 4 hours |
