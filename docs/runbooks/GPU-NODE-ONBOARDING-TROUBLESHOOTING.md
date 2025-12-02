# GPU Node Onboarding Troubleshooting Runbook

## Quick Diagnosis

### Step 1: Check Node Registration Status

```bash
# Check if node joined the cluster
KUBECONFIG=~/.kube/k3s-basilica-config kubectl get nodes | grep {node-id}

# Expected output:
# {node-id}   Ready    <none>   1m   v1.31.1+k3s1
```

**Status: NotReady** → See [Node Not Ready](#node-not-ready)
**Status: Not Found** → See [Node Not Joining](#node-not-joining)
**Status: Ready** → Success! Node is healthy

### Step 2: Check K3s Server Logs

```bash
# On any K3s server
journalctl -u k3s --since "5 minutes ago" | grep {node-id}

# Look for:
# ✓ Success: No errors about the node
# ✗ Error: "unable to verify password for node"
# ✗ Error: "hash does not match"
# ✗ Error: "illegal base64 data"
```

## Common Issues

### Node Not Joining

**Symptoms**: Node not appearing in `kubectl get nodes`

#### 1. Check API Registration

```bash
# On GPU node - check registration response
cat /var/log/basilica-onboard.log

# Should show successful API response with:
# - k3s_url
# - k3s_token
# - node_password
```

**Missing node_password?**
- API SSH may be disabled
- Check API logs: `docker logs basilica-api | grep {node-id}`

#### 2. Check Password File

```bash
# On GPU node
sudo ls -la /etc/rancher/node/password

# Expected:
# -rw------- 1 root root 44 Nov 18 00:00 /etc/rancher/node/password

# Check content (should be 44-char base64, NO newline)
sudo cat /etc/rancher/node/password | wc -c  # Should be 44
sudo cat -A /etc/rancher/node/password        # Should end with $ (no newline)
```

**Wrong size or has newline?**
- Onboard script may be using `echo` instead of `echo -n`
- Fix: `echo -n "$NODE_PASSWORD" | sudo tee /etc/rancher/node/password`

#### 3. Check K3s Agent Status

```bash
# On GPU node
sudo systemctl status k3s-agent

# If failed, check logs
sudo journalctl -u k3s-agent -f
```

**Common errors**:
- `connection refused` → K3s URL incorrect or firewall blocking
- `certificate is not valid` → TLS cert missing SAN for connect.basilica.ai
- `unable to verify password` → Hash mismatch (see below)

#### 4. Check Network Connectivity

```bash
# On GPU node - test connectivity to K3s endpoint
curl -k https://connect.basilica.ai:6443/version

# Should return K8s version JSON
# If fails: DNS or network issue
```

### Authentication Failures

**Symptoms**: `unable to verify password for node` or `hash does not match`

#### Step 1: Verify Secret Exists

```bash
KUBECONFIG=~/.kube/k3s-basilica-config \
  kubectl get secret {node-id}.node-password.k3s -n kube-system

# If not found: API failed to create secret
# Check API logs for SSH errors
```

#### Step 2: Verify Hash Format

```bash
# Get stored hash
KUBECONFIG=~/.kube/k3s-basilica-config \
  kubectl get secret {node-id}.node-password.k3s -n kube-system \
  -o jsonpath='{.data.hash}' | base64 -d

# Expected format: $1:<16-hex>:15:8:1:<86-char-base64>
# Example: $1:2f6361c7c063b618:15:8:1:abD4mRxyyx9DigewwlVNx9MCgQT4EZdbjFlZe8aEcg6iIsV4E4UMVAFpgAKuwqsd07psyVa1MUBwS5PguNL+aw
```

**Validation Checklist**:
- [ ] Starts with `$1:`
- [ ] Salt is exactly 16 hex characters (8 bytes)
- [ ] Parameters are `15:8:1`
- [ ] Hash is exactly 86 characters (unpadded base64)
- [ ] Hash has NO `=` padding at the end

**If hash has padding (`=`)**:
- API version is outdated
- Update to version with `.trim_end_matches('=').to_string()`

**If salt is 32 hex chars (16 bytes)**:
- API version is outdated
- Update to version with `[0u8; 8]` salt size

#### Step 3: Compare Password and Hash

```bash
# On GPU node - get password
PASSWORD=$(sudo cat /etc/rancher/node/password)
echo "Password length: $(echo -n "$PASSWORD" | wc -c)"  # Should be 44

# Extract salt from stored hash
HASH=$(KUBECONFIG=~/.kube/k3s-basilica-config \
  kubectl get secret {node-id}.node-password.k3s -n kube-system \
  -o jsonpath='{.data.hash}' | base64 -d)

SALT=$(echo "$HASH" | cut -d: -f2)
echo "Salt: $SALT"  # Should be 16 hex chars

# Manually compute hash to verify (requires Rust)
cd /tmp && cargo new --bin test_hash
cd test_hash
cat > Cargo.toml << 'EOF'
[package]
name = "test_hash"
version = "0.1.0"
edition = "2021"

[dependencies]
scrypt = { version = "0.11", features = ["simple"] }
hex = "0.4"
base64 = "0.21"
EOF

cat > src/main.rs << 'EOF'
use scrypt::Params;
use base64::engine::general_purpose::STANDARD;
use base64::Engine;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let password = &args[1];
    let salt_hex = &args[2];

    let salt_bytes = hex::decode(salt_hex).unwrap();
    let params = Params::new(15, 8, 1, 64).unwrap();
    let mut hash_output = [0u8; 64];
    scrypt::scrypt(password.as_bytes(), &salt_bytes, &params, &mut hash_output).unwrap();

    let hash_b64 = STANDARD.encode(hash_output).trim_end_matches('=').to_string();
    println!("{}", hash_b64);
}
EOF

cargo run --release -- "$PASSWORD" "$SALT"

# Compare output with hash from secret (should match exactly)
```

### Certificate Errors

**Symptoms**: `certificate is valid for ..., not connect.basilica.ai`

#### Fix K3s Server Certificates

```bash
# On each K3s server
# 1. Stop K3s
sudo systemctl stop k3s

# 2. Add TLS SAN to service file
sudo vim /etc/systemd/system/k3s.service
# Add: --tls-san connect.basilica.ai

# 3. Remove old certificates
sudo rm -rf /var/lib/rancher/k3s/server/tls/*

# 4. Reload and restart
sudo systemctl daemon-reload
sudo systemctl start k3s

# 5. Verify certificate includes SAN
openssl s_client -connect connect.basilica.ai:6443 2>/dev/null | \
  openssl x509 -noout -text | grep -A1 "Subject Alternative Name"
# Should show: DNS:connect.basilica.ai
```

**Important**: For etcd cluster, update servers one at a time. If cluster fails, restore from backup.

### Node Not Ready

**Symptoms**: Node appears in `kubectl get nodes` but status is `NotReady`

```bash
# Check node conditions
kubectl describe node {node-id} | grep -A 10 Conditions

# Common issues:
# - DiskPressure: Disk full
# - MemoryPressure: Memory exhausted
# - NetworkNotReady: CNI plugin issue
# - KubeletNotReady: kubelet crashed
```

#### Check Kubelet Status

```bash
# On GPU node
sudo systemctl status k3s-agent
sudo journalctl -u k3s-agent -n 100

# Look for errors about:
# - CNI plugin failures
# - Container runtime issues
# - Certificate problems
```

### SSH Connection Issues (API → K3s Server)

**Symptoms**: API logs show `SSH connection failed`

```bash
# Check API logs
docker logs basilica-api | grep -i ssh

# Common errors:
# - "Permission denied" → SSH key not authorized
# - "Connection refused" → SSH port blocked or wrong IP
# - "Connection timed out" → Network/firewall issue
```

#### Verify SSH Access

```bash
# From API container/host
ssh -i /tmp/.ssh/k3s_key basilica-api@10.101.0.27

# Should connect without password
# If fails:
# 1. Check key is in authorized_keys on K3s server
# 2. Check SSH service is running: systemctl status sshd
# 3. Check firewall allows port 22
```

#### Verify Sudo Permissions

```bash
# SSH to K3s server as basilica-api user
sudo k3s kubectl get nodes

# Should work without password prompt
# If requires password: add to /etc/sudoers
# basilica-api ALL=(ALL) NOPASSWD: /usr/local/bin/k3s
```

## Diagnostic Commands Reference

### Quick Health Check

```bash
#!/bin/bash
NODE_ID=$1

echo "=== Node Status ==="
kubectl get node $NODE_ID

echo -e "\n=== Node Labels ==="
kubectl get node $NODE_ID -o jsonpath='{.metadata.labels}' | jq

echo -e "\n=== Password Secret ==="
kubectl get secret ${NODE_ID}.node-password.k3s -n kube-system

echo -e "\n=== Secret Hash Format ==="
kubectl get secret ${NODE_ID}.node-password.k3s -n kube-system \
  -o jsonpath='{.data.hash}' | base64 -d
echo ""

echo -e "\n=== Recent K3s Logs ==="
ssh root@10.101.0.27 "journalctl -u k3s --since '5 min ago' | grep $NODE_ID"
```

### Node Password Verification

```bash
#!/bin/bash
# Run on GPU node
NODE_ID=$(hostname)

echo "=== Password File Check ==="
ls -la /etc/rancher/node/password
echo "Size: $(wc -c < /etc/rancher/node/password) bytes (should be 44)"
echo "Content: $(sudo cat /etc/rancher/node/password)"
echo "Has newline: $(sudo cat -A /etc/rancher/node/password | grep -q '\$$' && echo 'No' || echo 'Yes')"

echo -e "\n=== K3s Agent Status ==="
systemctl is-active k3s-agent

echo -e "\n=== Recent Agent Logs ==="
journalctl -u k3s-agent --since "5 min ago" | tail -20
```

## Emergency Procedures

### Re-onboard Failed Node

If a node completely fails to join:

```bash
# 1. On GPU node - clean up K3s
sudo /usr/local/bin/k3s-agent-uninstall.sh || true
sudo rm -rf /etc/rancher/node/password
sudo rm -rf /var/lib/rancher/k3s

# 2. On K3s cluster - delete old node and secret
kubectl delete node {node-id} --ignore-not-found
kubectl delete secret {node-id}.node-password.k3s -n kube-system --ignore-not-found

# 3. Re-register via API
curl -X POST https://api.basilica.ai/v1/gpu-nodes/register \
  -H "Authorization: Bearer $API_KEY" \
  -d '{"node_id":"{node-id}","datacenter_id":"...","gpu_specs":{...}}'

# 4. Run onboard script again
./onboard.sh
```

### Force Password Reset

If you suspect password corruption:

```bash
# 1. Delete existing secret
kubectl delete secret {node-id}.node-password.k3s -n kube-system

# 2. On GPU node - remove old password
sudo rm -f /etc/rancher/node/password
sudo systemctl stop k3s-agent

# 3. Re-register to get new password
# (API will create new secret automatically)

# 4. Restart agent
sudo systemctl start k3s-agent
```

## Monitoring and Alerts

### Set Up Alerts

Monitor these metrics:

```yaml
# Prometheus alerts
- alert: NodePasswordAuthFailure
  expr: |
    increase(k3s_server_authentication_failures{reason="password"}[5m]) > 5
  annotations:
    summary: "Multiple node password authentication failures"

- alert: OrphanedNodePasswordSecrets
  expr: |
    count(kube_secret_labels{secret=~".*\\.node-password\\.k3s"})
    > count(kube_node_info) + 10
  annotations:
    summary: "Too many orphaned node password secrets"
```

### Regular Audits

Run weekly:

```bash
# List all node password secrets and compare with active nodes
echo "=== Node Password Secrets ==="
kubectl get secrets -n kube-system | grep node-password | wc -l

echo "=== Active GPU Nodes ==="
kubectl get nodes -l basilica.ai/node-type=gpu | wc -l

echo "=== Orphaned Secrets (no matching node) ==="
comm -23 \
  <(kubectl get secrets -n kube-system -o name | grep node-password | cut -d. -f1 | sort) \
  <(kubectl get nodes -o name | cut -d/ -f2 | sort)
```

## Getting Help

If issues persist after following this runbook:

1. **Gather logs**:
   ```bash
   # API logs
   docker logs basilica-api --tail 100 > api.log

   # K3s server logs
   journalctl -u k3s --since "1 hour ago" > k3s-server.log

   # GPU node logs (on node)
   journalctl -u k3s-agent --since "1 hour ago" > k3s-agent.log
   sudo cat /etc/rancher/node/password | xxd > password-dump.txt
   ```

2. **Collect diagnostics**:
   ```bash
   kubectl get secret {node-id}.node-password.k3s -n kube-system -o yaml > secret.yaml
   kubectl get node {node-id} -o yaml > node.yaml
   ```

3. **Contact support** with:
   - Node ID
   - Timestamp of failure
   - All collected logs
   - Hash format from secret
   - Password file hex dump
