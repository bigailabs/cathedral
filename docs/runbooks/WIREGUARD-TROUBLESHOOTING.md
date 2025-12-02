# WireGuard Troubleshooting Runbook

This runbook documents common WireGuard connectivity issues between GPU nodes and K3s servers, and how to diagnose and resolve them.

## Table of Contents

1. [Symptoms](#symptoms)
2. [Safety and Backup Procedures](#safety-and-backup-procedures)
3. [Diagnostic Commands](#diagnostic-commands)
4. [Advanced Network Diagnostics](#advanced-network-diagnostics)
5. [Issue 1: WireGuard Key Mismatch](#issue-1-wireguard-key-mismatch)
6. [Issue 2: Duplicate iptables Rate Limit Rules](#issue-2-duplicate-iptables-rate-limit-rules)
7. [Issue 3: Peer Not Registered on K3s Servers](#issue-3-peer-not-registered-on-k3s-servers)
8. [Issue 4: Node Password Mismatch](#issue-4-node-password-mismatch)
9. [Issue 5: MTU and Fragmentation Issues](#issue-5-mtu-and-fragmentation-issues)
10. [Issue 6: AWS Security Group Blocking Traffic](#issue-6-aws-security-group-blocking-traffic)
11. [Issue 7: Split-Brain / Partial Connectivity](#issue-7-split-brain--partial-connectivity)
12. [K3s-Specific Diagnostics](#k3s-specific-diagnostics)
13. [Verification Checklist](#verification-checklist)
14. [Rollback Procedures](#rollback-procedures)
15. [Monitoring and Alerting](#monitoring-and-alerting)
16. [Diagnostic Collection Script](#diagnostic-collection-script)
17. [Quick Reference](#quick-reference-common-commands)
18. [Network Architecture Reference](#network-architecture-reference)
19. [Escalation Procedures](#escalation-procedures)

---

## Symptoms

- K3s agent stuck at `[INFO] systemd: Starting k3s-agent`
- K3s agent logs show: `failed to get CA certs: context deadline exceeded`
- WireGuard shows `0 B received` from all peers
- Ping to WireGuard server IPs (10.200.0.x) fails with 100% packet loss
- Intermittent connectivity or packet loss
- High latency through WireGuard tunnel

---

## Safety and Backup Procedures

**IMPORTANT:** Always create backups before making changes.

### Before Any Changes

```bash
# Create timestamped backup directory
BACKUP_DIR="/tmp/wg-backup-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

# On GPU node: Backup WireGuard config
ssh serveradmin@<GPU_NODE_IP> "sudo cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.backup"

# On K3s servers: Backup WireGuard config and iptables
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.backup.\$(date +%Y%m%d_%H%M%S)"

ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /root/iptables.backup.\$(date +%Y%m%d_%H%M%S)"
```

### Capture Current State Before Changes

```bash
# Collect baseline diagnostics
ssh serveradmin@<GPU_NODE_IP> '
  echo "=== WireGuard Status ===" && sudo wg show wg0
  echo "=== Interface Info ===" && ip addr show wg0
  echo "=== Routes ===" && ip route show | grep wg0
  echo "=== Connectivity Test ===" && ping -c 2 10.200.0.1 2>&1 || true
' > "$BACKUP_DIR/gpu-node-state.txt"
```

### Use Ansible Check Mode First

```bash
# Test changes without applying
ansible k3s_server -i orchestrator/ansible/inventories/production.ini --check \
  -m shell -a "<your_command>"
```

---

## Diagnostic Commands

### 1. Check K3s Agent Logs

```bash
ssh serveradmin@<GPU_NODE_IP> "sudo journalctl -u k3s-agent -n 50 --no-pager"
```

**What to look for:**

- `failed to get CA certs: context deadline exceeded` - Network connectivity issue
- `Node password rejected` - Node password mismatch (see Issue 4)
- `certificate has expired` - Certificate issue (see K3s-Specific Diagnostics)

### 2. Check WireGuard Interface Status

```bash
ssh serveradmin@<GPU_NODE_IP> "sudo wg show wg0"
```

**What to look for:**

- `transfer: 0 B received` - No traffic flowing from peers
- `latest handshake: X seconds ago` - Handshake happening but traffic blocked
- Missing `latest handshake` - Handshake not completing

### 3. Verify WireGuard Key Consistency

```bash
# Check public key from interface
ssh serveradmin@<GPU_NODE_IP> "sudo wg show wg0 | grep 'public key'"

# Check public key from key file
ssh serveradmin@<GPU_NODE_IP> "sudo cat /etc/wireguard/public.key"

# Verify private key generates correct public key
ssh serveradmin@<GPU_NODE_IP> "sudo cat /etc/wireguard/private.key | wg pubkey"
```

**Expected:** All three should show the same public key. If they differ, see "Key Mismatch" section.

### 4. Test WireGuard Connectivity

```bash
ssh serveradmin@<GPU_NODE_IP> "ping -c 2 10.200.0.1"
```

### 5. Check K3s Server Peer Registration

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0 | grep -A4 '<GPU_NODE_PUBLIC_KEY>'"
```

**What to look for:**

- Peer exists with correct `allowed ips` matching GPU node's WireGuard IP
- If peer missing, it needs to be registered

---

## Advanced Network Diagnostics

### Packet Capture Analysis

```bash
# Capture WireGuard handshake packets (run on K3s server)
ansible server1 -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo timeout 30 tcpdump -i any -n 'udp port 51820' -c 20"

# Capture tunnel traffic on GPU node
ssh serveradmin@<GPU_NODE_IP> "sudo timeout 30 tcpdump -i wg0 -n -c 50"

# Capture both encrypted and decrypted traffic
ssh serveradmin@<GPU_NODE_IP> "sudo tcpdump -i any -n '(udp port 51820) or (host 10.200.0.1)' -c 50"
```

### Route Verification

```bash
# Check routing table for WireGuard traffic
ssh serveradmin@<GPU_NODE_IP> "ip route show | grep -E '10.200|10.42|10.101|wg0'"

# Verify specific route
ssh serveradmin@<GPU_NODE_IP> "ip route get 10.200.0.1"

# Check for asymmetric routing
ssh serveradmin@<GPU_NODE_IP> "traceroute -n 10.200.0.1"
```

### Interface Statistics

```bash
# Check for interface errors/drops
ssh serveradmin@<GPU_NODE_IP> "ip -s link show wg0"

# Verify listening port
ssh serveradmin@<GPU_NODE_IP> "sudo ss -ulnp | grep 51820"

# Check interface is UP
ssh serveradmin@<GPU_NODE_IP> "ip link show wg0 | grep -E 'state|mtu'"
```

### Connection Tracking

```bash
# Check conntrack for WireGuard sessions (on K3s server)
ansible server1 -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo conntrack -L 2>/dev/null | grep 51820 | head -10"

# Verify conntrack table isn't full
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "cat /proc/net/nf_conntrack_count && cat /proc/sys/net/netfilter/nf_conntrack_max"
```

### DNS and Service Discovery

```bash
# Verify K3s API server reachability
ssh serveradmin@<GPU_NODE_IP> "curl -sk --connect-timeout 5 https://10.200.0.1:6443/version"

# Check CoreDNS resolution (if node is joined)
ssh serveradmin@<GPU_NODE_IP> "nslookup kubernetes.default.svc.cluster.local 10.43.0.10 2>&1 || echo 'DNS not available yet'"
```

---

## Issue 1: WireGuard Key Mismatch

### Symptoms

- `wg show wg0` shows different public key than `/etc/wireguard/public.key`
- K3s servers have peer registered with different key than GPU node is using

### Root Cause

The `wg0.conf` file contains a different private key than `/etc/wireguard/private.key`. This can happen when:

- A stale `wg0.conf` from a previous node installation exists
- The onboard script preserved the key file but didn't update the config

### Resolution

**Step 1: Create backup and stop services**

```bash
ssh serveradmin@<GPU_NODE_IP> "
  sudo cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.backup
  sudo systemctl stop wg-quick@wg0
  sudo systemctl stop k3s-agent
"
```

**Step 2: Get the correct private key and update wg0.conf**

```bash
ssh serveradmin@<GPU_NODE_IP> '
PRIVATE_KEY=$(sudo cat /etc/wireguard/private.key)
PUBLIC_KEY=$(sudo cat /etc/wireguard/public.key)
echo "Correct public key: $PUBLIC_KEY"

# Update wg0.conf with correct private key using awk
sudo awk -v key="$PRIVATE_KEY" "{gsub(/PrivateKey = .*/, \"PrivateKey = \" key); print}" \
  /etc/wireguard/wg0.conf > /tmp/wg0.conf.tmp
sudo mv /tmp/wg0.conf.tmp /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
'
```

**Step 3: Update peer on K3s servers**

```bash
# Remove old peer key and add correct one
OLD_KEY="<old_public_key>"
NEW_KEY="<correct_public_key>"
ALLOWED_IP="<gpu_node_wireguard_ip>"

ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg set wg0 peer '$OLD_KEY' remove 2>/dev/null; \
               sudo wg set wg0 peer '$NEW_KEY' allowed-ips '$ALLOWED_IP/32' persistent-keepalive 25"
```

**Step 4: Persist to config file on servers (with backup)**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo cp /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.backup && \
               sudo sed -i 's/$OLD_KEY/$NEW_KEY/g' /etc/wireguard/wg0.conf && \
               grep -q '$NEW_KEY' /etc/wireguard/wg0.conf && echo 'Updated successfully'"
```

**Step 5: Start WireGuard and K3s agent**

```bash
ssh serveradmin@<GPU_NODE_IP> "sudo systemctl start wg-quick@wg0 && sudo systemctl start k3s-agent"
```

---

## Issue 2: Duplicate iptables Rate Limit Rules

### Symptoms

- WireGuard handshakes complete (`latest handshake` shows recent time)
- Data shows sent but `0 B received`
- Ping fails despite handshakes working

### Diagnosis

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n | grep -i wire"
```

**Problem indicator:** Multiple duplicate DROP rules:

```
DROP  17  --  0.0.0.0/0  0.0.0.0/0  udp dpt:51820 limit: above 10/min burst 5 mode srcip
DROP  17  --  0.0.0.0/0  0.0.0.0/0  udp dpt:51820 limit: above 10/min burst 5 mode srcip
DROP  17  --  0.0.0.0/0  0.0.0.0/0  udp dpt:51820 limit: above 10/min burst 5 mode srcip
DROP  17  --  0.0.0.0/0  0.0.0.0/0  udp dpt:51820 limit: above 10/min burst 5 mode srcip
```

### Root Cause

The WireGuard Ansible role or performance tuning script added rate limit rules multiple times without checking if they already exist.

**Security Context:** Rate limiting is intended to prevent WireGuard handshake flood attacks. The default `10/min burst 5` may be too restrictive for legitimate peer reconnections. Consider `30/min burst 10` for production.

### Resolution

**Step 1: Backup current iptables state**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save > /root/iptables.backup.\$(date +%Y%m%d_%H%M%S)"
```

**Step 2: Get rule line numbers**

```bash
ansible server1 -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n --line-numbers | grep -i wire"
```

**Step 3: Remove duplicate rules (from highest to lowest line number)**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -D INPUT 10 2>/dev/null; \
               sudo iptables -D INPUT 9 2>/dev/null; \
               sudo iptables -D INPUT 8 2>/dev/null; \
               sudo iptables -D INPUT 7 2>/dev/null; \
               sudo iptables -L INPUT -n | grep -i wire || echo 'Rules removed'"
```

**Alternative: Safe rule removal using iptables-save/restore**

```bash
# This approach is safer and idempotent
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-save | grep -v 'dpt:51820.*limit:' | sudo iptables-restore"
```

**Step 4: Persist iptables changes**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo netfilter-persistent save 2>/dev/null || sudo iptables-save > /etc/iptables/rules.v4"
```

**Step 5: Verify connectivity**

```bash
ssh serveradmin@<GPU_NODE_IP> "ping -c 2 10.200.0.1"
```

---

## Issue 3: Peer Not Registered on K3s Servers

### Symptoms

- WireGuard interface up on GPU node
- No handshakes completing
- K3s servers don't show the peer

### Diagnosis

```bash
# Get GPU node's public key
ssh serveradmin@<GPU_NODE_IP> "sudo cat /etc/wireguard/public.key"

# Check if peer exists on servers
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0 | grep '<PUBLIC_KEY>'"
```

### Resolution

**Step 1: Get GPU node's WireGuard IP**

```bash
ssh serveradmin@<GPU_NODE_IP> "ip addr show wg0 | grep inet"
```

**Step 2: Register peer on all K3s servers**

```bash
PUBLIC_KEY="<gpu_node_public_key>"
ALLOWED_IP="<gpu_node_wireguard_ip>"
NODE_ID="<node_uuid>"

ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg set wg0 peer '$PUBLIC_KEY' allowed-ips '$ALLOWED_IP/32' persistent-keepalive 25"
```

**Step 3: Persist to config file**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "grep -q '$PUBLIC_KEY' /etc/wireguard/wg0.conf || \
    echo -e '\n[Peer]\n# Node: $NODE_ID\nPublicKey = $PUBLIC_KEY\nAllowedIPs = $ALLOWED_IP/32\nPersistentKeepalive = 25' | \
    sudo tee -a /etc/wireguard/wg0.conf > /dev/null"
```

---

## Issue 4: Node Password Mismatch

### Symptoms

K3s agent logs show:

```
Node password rejected, duplicate hostname or contents of '/etc/rancher/node/password'
may not match server node-passwd entry
```

### Root Cause

The node was previously registered with a different password, and the server still has the old password stored.

### Resolution

**Option A: Reset node password on server (preferred)**

```bash
# On the K3s server, remove the old password entry
NODE_NAME="<node-name>"
ansible server1 -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo kubectl --kubeconfig=/etc/rancher/k3s/k3s.yaml delete secret ${NODE_NAME}.node-password.k3s -n kube-system 2>/dev/null || true"
```

**Option B: Reset node state on GPU node**

```bash
ssh serveradmin@<GPU_NODE_IP> "
  sudo systemctl stop k3s-agent
  sudo rm -f /etc/rancher/node/password
  sudo rm -rf /var/lib/rancher/k3s/agent/*.crt
  sudo rm -rf /var/lib/rancher/k3s/agent/*.key
  sudo systemctl start k3s-agent
"
```

---

## Issue 5: MTU and Fragmentation Issues

### Symptoms

- Large packets fail but small packets work
- TCP connections hang or are very slow
- SSH works but file transfers fail
- `ping -s 1400 10.200.0.1` fails but `ping 10.200.0.1` works

### Diagnosis

```bash
# Check current MTU on WireGuard interface
ssh serveradmin@<GPU_NODE_IP> "ip link show wg0 | grep mtu"

# Check physical interface MTU
ssh serveradmin@<GPU_NODE_IP> "ip link show eth0 | grep mtu"

# Test path MTU discovery
ssh serveradmin@<GPU_NODE_IP> "ping -M do -s 1372 -c 3 10.200.0.1"  # Should work (1372 + 28 ICMP = 1400)
ssh serveradmin@<GPU_NODE_IP> "ping -M do -s 1400 -c 3 10.200.0.1"  # May fail
ssh serveradmin@<GPU_NODE_IP> "ping -M do -s 1472 -c 3 10.200.0.1"  # Will fail if MTU < 1500

# Check for ICMP fragmentation needed messages
ssh serveradmin@<GPU_NODE_IP> "sudo tcpdump -i any 'icmp[icmptype] == 3 and icmp[icmpcode] == 4' -c 5"
```

### Understanding MTU

WireGuard adds ~80 bytes of overhead:

- 20 bytes: IPv4 header
- 8 bytes: UDP header
- 40 bytes: WireGuard header
- 16 bytes: WireGuard authentication tag

**Recommended MTU values:**

- Physical interface: 1500 (default)
- WireGuard interface: 1420 (1500 - 80)
- Flannel VXLAN over WireGuard: 1370 (1420 - 50)

### Resolution

**Step 1: Verify wg0.conf has correct MTU**

```bash
ssh serveradmin@<GPU_NODE_IP> "grep MTU /etc/wireguard/wg0.conf"
# Should show: MTU = 1420
```

**Step 2: If MTU is wrong, update and restart**

```bash
ssh serveradmin@<GPU_NODE_IP> "
  sudo sed -i 's/^MTU = .*/MTU = 1420/' /etc/wireguard/wg0.conf
  sudo systemctl restart wg-quick@wg0
"
```

**Step 3: Verify Flannel MTU matches**

```bash
kubectl --kubeconfig=/root/.kube/k3s-basilica-config -n kube-system get cm kube-flannel-cfg -o yaml | grep -i mtu
```

**Step 4: Check for TCP MSS clamping issues**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -t mangle -L FORWARD -n | grep -i mss"
```

---

## Issue 6: AWS Security Group Blocking Traffic

### Symptoms

- WireGuard handshakes never complete
- `tcpdump` shows outbound packets but no inbound
- Works when testing from within AWS VPC

### Diagnosis

```bash
# Check if UDP 51820 is reachable from GPU node
ssh serveradmin@<GPU_NODE_IP> "nc -u -v -z <K3S_SERVER_PUBLIC_IP> 51820"

# Verify AWS security group (requires AWS CLI configured)
aws ec2 describe-security-groups \
  --group-ids <K3S_SERVER_SG_ID> \
  --query 'SecurityGroups[].IpPermissions[?IpProtocol==`udp` && FromPort<=`51820` && ToPort>=`51820`]'
```

### Resolution

**Step 1: Verify Terraform configuration**

```bash
# Check scripts/cloud/main.tf for security group rules
grep -A5 "51820" scripts/cloud/*.tf
```

**Step 2: If missing, add rule via AWS CLI (temporary)**

```bash
aws ec2 authorize-security-group-ingress \
  --group-id <K3S_SERVER_SG_ID> \
  --protocol udp \
  --port 51820 \
  --cidr 0.0.0.0/0
```

**Step 3: Update Terraform for permanent fix**
Update `scripts/cloud/main.tf` to include:

```hcl
ingress {
  from_port   = 51820
  to_port     = 51820
  protocol    = "udp"
  cidr_blocks = ["0.0.0.0/0"]
  description = "WireGuard VPN"
}
```

---

## Issue 7: Split-Brain / Partial Connectivity

### Symptoms

- GPU node can reach some K3s servers but not others
- Intermittent connectivity
- K3s agent keeps switching between servers

### Diagnosis

```bash
# Test connectivity to all K3s servers
ssh serveradmin@<GPU_NODE_IP> '
for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do
  echo -n "$ip: "
  ping -c 1 -W 2 $ip > /dev/null 2>&1 && echo "UP" || echo "DOWN"
done
'

# Check handshake status for each peer
ssh serveradmin@<GPU_NODE_IP> "sudo wg show wg0 | grep -E 'peer|handshake|transfer'"

# Verify peer registration on each server
for server in server1 server2 server3; do
  echo "=== $server ==="
  ansible $server -i orchestrator/ansible/inventories/production.ini \
    -m shell -a "sudo wg show wg0 | grep -A3 '<GPU_NODE_PUBLIC_KEY>'"
done
```

### Resolution

**Step 1: Ensure peer is registered on ALL servers**

```bash
PUBLIC_KEY="<gpu_node_public_key>"
ALLOWED_IP="<gpu_node_wireguard_ip>"

ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0 | grep -q '$PUBLIC_KEY' || \
    sudo wg set wg0 peer '$PUBLIC_KEY' allowed-ips '$ALLOWED_IP/32' persistent-keepalive 25"
```

**Step 2: Verify AllowedIPs are consistent**

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0 | grep -A1 '$PUBLIC_KEY'"
```

---

## K3s-Specific Diagnostics

### Check K3s API Server Health

```bash
# Test API server through each WireGuard peer
for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do
  echo -n "$ip: "
  curl -sk --connect-timeout 3 https://$ip:6443/healthz && echo "" || echo "FAILED"
done
```

### Verify K3s Token

```bash
# Check K3s agent token is properly configured
ssh serveradmin@<GPU_NODE_IP> "grep -i token /etc/systemd/system/k3s-agent.service.env | head -1"
```

### Check Certificate Issues

```bash
# Check for certificate errors
ssh serveradmin@<GPU_NODE_IP> "sudo journalctl -u k3s-agent | grep -i 'certificate\|cert\|x509' | tail -10"

# Verify certificate expiration
ssh serveradmin@<GPU_NODE_IP> "sudo openssl s_client -connect 10.200.0.1:6443 2>/dev/null | openssl x509 -noout -dates"
```

### Flannel VXLAN over WireGuard

```bash
# Check Flannel interface exists
ssh serveradmin@<GPU_NODE_IP> "ip link show | grep flannel"

# Verify VXLAN traffic flows through WireGuard
ssh serveradmin@<GPU_NODE_IP> "sudo tcpdump -i wg0 -n 'port 8472' -c 5"

# Check Flannel VXLAN MTU
ssh serveradmin@<GPU_NODE_IP> "ip link show flannel.1 2>/dev/null | grep mtu"
```

### Time Synchronization

```bash
# Check clock sync (certificates depend on accurate time)
ssh serveradmin@<GPU_NODE_IP> "timedatectl status"

# Verify NTP sync
ssh serveradmin@<GPU_NODE_IP> "systemctl status systemd-timesyncd || systemctl status chronyd"
```

---

## Verification Checklist

After applying fixes, verify:

1. **WireGuard connectivity:**

   ```bash
   ssh serveradmin@<GPU_NODE_IP> "ping -c 2 10.200.0.1 && ping -c 2 10.200.0.2 && ping -c 2 10.200.0.3"
   ```

2. **WireGuard handshakes:**

   ```bash
   ssh serveradmin@<GPU_NODE_IP> "sudo wg show wg0"
   ```

   All peers should show `latest handshake` within last 2 minutes and non-zero `transfer` bytes.

3. **K3s API reachable:**

   ```bash
   ssh serveradmin@<GPU_NODE_IP> "curl -sk https://10.200.0.1:6443/healthz"
   ```

4. **K3s agent status:**

   ```bash
   ssh serveradmin@<GPU_NODE_IP> "sudo systemctl status k3s-agent"
   ```

   Should show `active (running)`.

5. **Node in cluster:**

   ```bash
   kubectl --kubeconfig=/root/.kube/k3s-basilica-config get nodes | grep <NODE_ID>
   ```

   Should show `Ready` status.

6. **MTU validation:**

   ```bash
   ssh serveradmin@<GPU_NODE_IP> "ping -M do -s 1372 -c 3 10.200.0.1"
   ```

   Should succeed without fragmentation errors.

---

## Rollback Procedures

### Restore WireGuard Configuration (GPU Node)

```bash
ssh serveradmin@<GPU_NODE_IP> "
  sudo systemctl stop wg-quick@wg0
  sudo cp /etc/wireguard/wg0.conf.backup /etc/wireguard/wg0.conf
  sudo systemctl start wg-quick@wg0
"
```

### Restore WireGuard Configuration (K3s Servers)

```bash
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "
    sudo systemctl stop wg-quick@wg0
    sudo cp /etc/wireguard/wg0.conf.backup /etc/wireguard/wg0.conf
    sudo systemctl start wg-quick@wg0
  "
```

### Restore iptables Rules

```bash
# Find backup file
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "ls -lt /root/iptables.backup.* | head -1"

# Restore from backup
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables-restore < /root/iptables.backup.<TIMESTAMP>"
```

### Full Node Recovery

If all else fails, re-run the onboard script:

```bash
ssh serveradmin@<GPU_NODE_IP> "
  sudo systemctl stop k3s-agent
  sudo systemctl stop wg-quick@wg0
  sudo rm -rf /var/lib/rancher/k3s/agent/*
  # Re-run onboard.sh from Basilica API
"
```

---

## Monitoring and Alerting

### Key Metrics to Monitor

| Metric | Warning Threshold | Critical Threshold |
|--------|-------------------|-------------------|
| `wireguard_latest_handshake_seconds` | > 120s | > 300s |
| `wireguard_bytes_received_total` rate | < 100 B/s | = 0 for 5min |
| `node_network_up{device="wg0"}` | - | = 0 |
| K3s node status | NotReady > 2min | NotReady > 5min |

### Prometheus Alert Rules

Add to your Prometheus configuration:

```yaml
groups:
- name: wireguard
  interval: 30s
  rules:
  - alert: WireGuardPeerHandshakeStale
    expr: time() - wireguard_latest_handshake_seconds > 180
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "WireGuard peer {{ $labels.public_key }} handshake stale"

  - alert: WireGuardPeerDown
    expr: time() - wireguard_latest_handshake_seconds > 300
    for: 2m
    labels:
      severity: critical
    annotations:
      summary: "WireGuard peer {{ $labels.public_key }} appears down"

  - alert: WireGuardNoDataReceived
    expr: rate(wireguard_bytes_received_total[5m]) == 0
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "WireGuard peer {{ $labels.public_key }} receiving no data"
```

### Log Monitoring

Watch for these patterns in logs:

```bash
# WireGuard errors
sudo journalctl -u wg-quick@wg0 | grep -iE 'error|fail|invalid'

# K3s agent connection issues
sudo journalctl -u k3s-agent | grep -iE 'timeout|refused|unreachable'
```

---

## Diagnostic Collection Script

Save this script as `/usr/local/bin/wg-diagnostics.sh` on GPU nodes:

```bash
#!/bin/bash
set -euo pipefail

OUTPUT_DIR="/tmp/wg-diagnostics-$(date +%Y%m%d_%H%M%S)"
mkdir -p "$OUTPUT_DIR"

echo "Collecting WireGuard diagnostics to $OUTPUT_DIR"

# System info
echo "=== System Info ===" > "$OUTPUT_DIR/system.txt"
uname -a >> "$OUTPUT_DIR/system.txt"
uptime >> "$OUTPUT_DIR/system.txt"
timedatectl status >> "$OUTPUT_DIR/system.txt" 2>&1 || true

# WireGuard status
echo "=== WireGuard Status ===" > "$OUTPUT_DIR/wg-status.txt"
sudo wg show wg0 >> "$OUTPUT_DIR/wg-status.txt" 2>&1 || echo "wg0 not running"
sudo wg show wg0 dump >> "$OUTPUT_DIR/wg-dump.txt" 2>&1 || true

# Network interfaces
ip addr > "$OUTPUT_DIR/ip-addr.txt"
ip route > "$OUTPUT_DIR/ip-route.txt"
ip -s link show wg0 > "$OUTPUT_DIR/wg0-stats.txt" 2>&1 || true

# K3s agent logs
sudo journalctl -u k3s-agent -n 500 --no-pager > "$OUTPUT_DIR/k3s-agent.log" 2>&1 || true

# WireGuard service logs
sudo journalctl -u wg-quick@wg0 -n 100 --no-pager > "$OUTPUT_DIR/wg-service.log" 2>&1 || true

# iptables rules
sudo iptables-save > "$OUTPUT_DIR/iptables.txt" 2>&1 || true

# Connectivity tests
echo "=== Connectivity Tests ===" > "$OUTPUT_DIR/connectivity.txt"
for ip in 10.200.0.1 10.200.0.2 10.200.0.3; do
  echo "--- $ip ---" >> "$OUTPUT_DIR/connectivity.txt"
  ping -c 3 -W 2 $ip >> "$OUTPUT_DIR/connectivity.txt" 2>&1 || echo "FAILED" >> "$OUTPUT_DIR/connectivity.txt"
done

# K3s API test
echo "=== K3s API Test ===" >> "$OUTPUT_DIR/connectivity.txt"
curl -sk --connect-timeout 5 https://10.200.0.1:6443/healthz >> "$OUTPUT_DIR/connectivity.txt" 2>&1 || echo "FAILED"

# WireGuard keys
echo "=== Key Info ===" > "$OUTPUT_DIR/keys.txt"
echo "Public key file: $(sudo cat /etc/wireguard/public.key 2>/dev/null || echo 'not found')" >> "$OUTPUT_DIR/keys.txt"
echo "Interface public key: $(sudo wg show wg0 public-key 2>/dev/null || echo 'not found')" >> "$OUTPUT_DIR/keys.txt"
echo "Derived from private key: $(sudo cat /etc/wireguard/private.key 2>/dev/null | wg pubkey || echo 'not found')" >> "$OUTPUT_DIR/keys.txt"

# MTU info
echo "=== MTU Info ===" > "$OUTPUT_DIR/mtu.txt"
ip link show wg0 2>/dev/null | grep mtu >> "$OUTPUT_DIR/mtu.txt" || true
ip link show eth0 2>/dev/null | grep mtu >> "$OUTPUT_DIR/mtu.txt" || true
grep MTU /etc/wireguard/wg0.conf >> "$OUTPUT_DIR/mtu.txt" 2>/dev/null || true

# Create tarball
tar -czf "$OUTPUT_DIR.tar.gz" -C /tmp "$(basename $OUTPUT_DIR)"
rm -rf "$OUTPUT_DIR"

echo "Diagnostics bundle created: $OUTPUT_DIR.tar.gz"
echo "Upload this file when requesting support."
```

**Usage:**

```bash
ssh serveradmin@<GPU_NODE_IP> "sudo /usr/local/bin/wg-diagnostics.sh"
scp serveradmin@<GPU_NODE_IP>:/tmp/wg-diagnostics-*.tar.gz .
```

---

## Quick Reference: Common Commands

```bash
# GPU node WireGuard status
ssh serveradmin@<IP> "sudo wg show wg0"

# GPU node K3s agent logs
ssh serveradmin@<IP> "sudo journalctl -u k3s-agent -n 30 --no-pager"

# K3s server peer list
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo wg show wg0"

# K3s server iptables rules
ansible k3s_server -i orchestrator/ansible/inventories/production.ini \
  -m shell -a "sudo iptables -L INPUT -n | grep -i wire"

# Check node status in cluster
kubectl --kubeconfig=/root/.kube/k3s-basilica-config get nodes -o wide

# Quick connectivity test
ssh serveradmin@<IP> "ping -c 1 10.200.0.1 && echo OK || echo FAILED"

# Restart WireGuard on GPU node
ssh serveradmin@<IP> "sudo systemctl restart wg-quick@wg0"

# Restart K3s agent on GPU node
ssh serveradmin@<IP> "sudo systemctl restart k3s-agent"

# View real-time WireGuard status
ssh serveradmin@<IP> "watch -n 2 'sudo wg show wg0'"
```

---

## Network Architecture Reference

| Component | IP Range | Purpose |
|-----------|----------|---------|
| WireGuard Network | 10.200.0.0/16 | Overlay network for GPU nodes |
| K3s Server 1 WG IP | 10.200.0.1 | WireGuard endpoint |
| K3s Server 2 WG IP | 10.200.0.2 | WireGuard endpoint |
| K3s Server 3 WG IP | 10.200.0.3 | WireGuard endpoint |
| VPC Subnet Server 1 | 10.101.0.0/24 | AWS VPC routing |
| VPC Subnet Server 2 | 10.101.1.0/24 | AWS VPC routing |
| VPC Subnet Server 3 | 10.101.2.0/24 | AWS VPC routing |
| Pod Network | 10.42.0.0/16 | Flannel VXLAN overlay |
| Service Network | 10.43.0.0/16 | Kubernetes ClusterIP (NOT routed via WG) |
| WireGuard UDP Port | 51820 | Must be open in AWS security groups |

### Traffic Flow

```
GPU Node (Public IP)
    |
    | UDP:51820 (encrypted)
    v
K3s Server (Public IP:51820)
    |
    | WireGuard decrypts
    v
wg0 interface (10.200.0.x)
    |
    | Internal routing
    v
VPC Network (10.101.x.x) / Pod Network (10.42.x.x)
```

### Key Concepts

- **Endpoint**: Public IP:Port where WireGuard listens (K3s servers)
- **AllowedIPs**: Both ACL and routing table entry - only traffic from these IPs is accepted
- **PersistentKeepalive**: Required (25s) for NAT traversal since GPU nodes are behind NAT

---

## Escalation Procedures

| Severity | Timeframe | Symptoms | Actions |
|----------|-----------|----------|---------|
| P1 - Critical | Immediate | All GPU nodes disconnected | 1. Check K3s servers are running<br>2. Verify WireGuard service on servers<br>3. Check AWS networking/security groups<br>4. Page on-call SRE |
| P2 - High | 15 minutes | Single node disconnected | 1. Run diagnostic script<br>2. Check peer registration<br>3. Review recent changes<br>4. Notify team lead |
| P3 - Medium | 1 hour | Intermittent connectivity | 1. Collect metrics over time<br>2. Analyze patterns<br>3. Check for rate limiting<br>4. Create incident ticket |
| P4 - Low | Next business day | Performance degradation | 1. Review MTU settings<br>2. Check for packet loss<br>3. Plan maintenance window |

### Contact Information

- **On-call SRE**: Check PagerDuty rotation
- **Infrastructure Team**: #infrastructure Slack channel
- **Escalation Path**: On-call SRE → Team Lead → Infrastructure Manager
