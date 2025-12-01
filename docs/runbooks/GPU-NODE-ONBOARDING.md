# GPU Node Onboarding Runbook

**Audience**: Cluster Owners / Platform Operators
**Purpose**: Operational procedures for onboarding GPU nodes from datacenter partners
**Last Updated**: 2025-01-14

---

## Overview

This runbook covers the operational procedures for enabling and managing GPU node onboarding from trusted datacenter partners into the Basilica K3s cluster.

**Architecture Summary:**

- basilica-api runs on AWS ECS Fargate
- K3s cluster connected via VPC peering
- Datacenters run bootstrap script on GPU nodes
- Admission webhook validates nodes before joining
- Node watcher activates validated nodes

---

## Prerequisites

Before starting GPU node onboarding, ensure:

- [ ] K3s cluster is operational
- [ ] basilica-api is deployed on ECS
- [ ] VPC peering is configured between ECS and K3s VPCs
- [ ] AWS Aurora Serverless database is accessible
- [ ] You have `kubectl` access to K3s cluster
- [ ] You have `psql` access to Aurora database
- [ ] You have AWS CLI configured

**Required Access:**

- K3s cluster admin access
- AWS console/CLI access for ECS, RDS, Secrets Manager
- Database credentials for Aurora Serverless

---

## Initial Setup (One-Time)

### Step 1: Apply Database Migration

```bash
# Connect to Aurora Serverless
export DATABASE_URL="postgresql://admin:password@basilica-db.cluster-xxx.us-east-1.rds.amazonaws.com:5432/basilica_v3_api"

# Navigate to API crate
cd crates/basilica-api

# Apply migration
sqlx migrate run

# Verify tables created
psql $DATABASE_URL -c "\d gpu_node_registrations"
psql $DATABASE_URL -c "\d datacenter_quotas"
```

**Expected Output:**

```
Table "public.gpu_node_registrations"
     Column      |           Type           |
-----------------+--------------------------+
 id              | uuid                     |
 node_id         | character varying(255)   |
 datacenter_id   | character varying(255)   |
 reservation_id  | character varying(255)   |
 status          | character varying(50)    |
 ...
```

### Step 2: Create K3s ServiceAccount for basilica-api

```bash
# Apply RBAC manifests
kubectl apply -f orchestrator/k8s/core/rbac/gpu-node-api-rbac.yaml

# Verify ServiceAccount created
kubectl get sa basilica-api-gpu-nodes -n basilica-system

# Test permissions
kubectl auth can-i list nodes --as=system:serviceaccount:basilica-system:basilica-api-gpu-nodes
# Expected: yes

kubectl auth can-i patch nodes --as=system:serviceaccount:basilica-system:basilica-api-gpu-nodes
# Expected: yes
```

### Step 3: Generate and Store Kubeconfig

```bash
# Generate long-lived ServiceAccount token (10 years)
kubectl create token basilica-api-gpu-nodes \
  -n basilica-system \
  --duration=87600h > /tmp/sa-token.txt

# Extract K3s API endpoint (must be accessible from ECS VPC)
K3S_API_ENDPOINT=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')

# IMPORTANT: Replace with VPC-accessible IP if needed
# Example: https://10.0.1.50:6443 instead of https://localhost:6443
echo "K3s API Endpoint: $K3S_API_ENDPOINT"

# Extract K3s CA certificate
K3S_CA_CERT=$(kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

# Generate kubeconfig
cat <<EOF > /tmp/basilica-api-kubeconfig.yaml
apiVersion: v1
kind: Config
clusters:
  - name: k3s-basilica
    cluster:
      certificate-authority-data: ${K3S_CA_CERT}
      server: ${K3S_API_ENDPOINT}
contexts:
  - name: basilica-api-context
    context:
      cluster: k3s-basilica
      user: basilica-api-external
current-context: basilica-api-context
users:
  - name: basilica-api-external
    user:
      token: $(cat /tmp/sa-token.txt)
EOF

# Store in AWS Secrets Manager
aws secretsmanager create-secret \
  --name basilica/k3s-kubeconfig \
  --description "Kubeconfig for basilica-api ECS service to access K3s cluster" \
  --secret-string file:///tmp/basilica-api-kubeconfig.yaml \
  --region us-east-1

# Note the ARN for Terraform
aws secretsmanager describe-secret \
  --secret-id basilica/k3s-kubeconfig \
  --region us-east-1 \
  --query 'ARN' \
  --output text

# Clean up sensitive files
rm /tmp/sa-token.txt /tmp/basilica-api-kubeconfig.yaml
```

### Step 4: Extract K3s Agent Token

```bash
# SSH to K3s master node
ssh k3s-master

# Extract agent token
sudo cat /var/lib/rancher/k3s/server/node-token

# Copy output - this is K3S_AGENT_TOKEN
# Example: K10abc123def456::server:xyz789
```

### Step 5: Update Terraform Configuration

Edit `scripts/cloud/compute.tf`:

```hcl
# In module "basilica_api_service" environment_variables section:
environment_variables = {
  # ... existing variables ...

  # K3s Integration (ADD THESE)
  K3S_SERVER_URL   = "https://10.0.1.50:6443"  # K3s master private IP
  K3S_AGENT_TOKEN  = "<token-from-step-4>"     # From /var/lib/rancher/k3s/server/node-token
}

# In module "basilica_api_service" secrets section:
secrets = [
  {
    name      = "KUBECONFIG_CONTENT"
    valueFrom = "arn:aws:secretsmanager:us-east-1:xxxxx:secret:basilica/k3s-kubeconfig-xxxxx"
  }
]
```

Apply Terraform changes:

```bash
cd scripts/cloud
terraform plan
terraform apply

# Force new ECS deployment
aws ecs update-service \
  --cluster basilica-v3-cluster \
  --service basilica-v3-api \
  --force-new-deployment \
  --region us-east-1
```

### Step 6: Deploy Admission Webhook

```bash
# Apply webhook configuration
kubectl apply -f orchestrator/k8s/admission/gpu-node-webhook.yaml

# Verify webhook is configured
kubectl get validatingwebhookconfiguration validate-basilica-gpu-nodes

# Check webhook details
kubectl describe validatingwebhookconfiguration validate-basilica-gpu-nodes
```

**Expected Output:**

```yaml
Name:         validate-basilica-gpu-nodes
Namespace:
...
Webhooks:
  Name:               gpu-nodes.basilica.ai
  Client Config:
    URL:              https://api.basilica.ai/webhooks/validate-node
    Ca Bundle:        <empty>
  Rules:
    Operations:       CREATE
    API Groups:
    API Versions:     v1
    Resources:        nodes
```

### Step 7: Verify Deployment

```bash
# Check ECS service is running
aws ecs describe-services \
  --cluster basilica-v3-cluster \
  --services basilica-v3-api \
  --region us-east-1 \
  --query 'services[0].deployments[0].{status:status,running:runningCount,desired:desiredCount}'

# Check Node Watcher started
aws logs tail /ecs/basilica-v3-api --follow | grep "Starting GPU node watcher"

# Check Cleanup Job started
aws logs tail /ecs/basilica-v3-api --follow | grep "Starting reservation cleanup job"

# Test API health
curl -f https://api.basilica.ai/health

# Verify database is empty (no registrations yet)
psql $DATABASE_URL -c "SELECT COUNT(*) FROM gpu_node_registrations;"
# Expected: 0
```

---

## Onboarding a New Datacenter

### Step 1: Create Datacenter API Key

```bash
# Generate API key
cargo run -p basilica-api --bin gen-api-key -- \
  --user dc-nyc-equinix \
  --name "Equinix NYC DC" \
  --scopes "gpu-nodes:register"

# Example output:
# === Basilica API Key (dev) ===
# User ID:              dc-nyc-equinix
# Name:                 Equinix NYC DC
# Kid (hex):            a1b2c3d4e5f6
# Token (Authorization): Bearer basilica_abc123xyz...
#
# -- Run this against Postgres
# INSERT INTO api_keys (user_id, kid, name, hash, scopes)
# VALUES ('dc-nyc-equinix', 'a1b2c3d4e5f6', 'Equinix NYC DC', '$argon2...', ARRAY['gpu-nodes:register']);
```

### Step 2: Store API Key in Database

```bash
# Copy the INSERT statement from gen-api-key output
psql $DATABASE_URL <<EOF
INSERT INTO api_keys (user_id, kid, name, hash, scopes)
VALUES ('dc-nyc-equinix', 'a1b2c3d4e5f6', 'Equinix NYC DC', '\$argon2...', ARRAY['gpu-nodes:register']);
EOF

# Verify key was created
psql $DATABASE_URL -c "SELECT user_id, name, scopes FROM api_keys WHERE user_id = 'dc-nyc-equinix';"
```

### Step 3: Provide Credentials to Datacenter

Create onboarding document for datacenter operator:

```markdown
# Basilica GPU Node Onboarding - Equinix NYC

## Credentials

**API URL:** https://api.basilica.ai
**Datacenter ID:** dc-nyc-equinix
**API Key:** basilica_abc123xyz...

## Installation Instructions

On each GPU node to onboard:

1. SSH to GPU node
2. Ensure NVIDIA drivers are installed
3. Run bootstrap script:

```bash
export BASILICA_API_URL="https://api.basilica.ai"
export BASILICA_DATACENTER_ID="dc-nyc-equinix"
export BASILICA_DATACENTER_API_KEY="basilica_abc123xyz..."

curl -fsSL https://install.basilica.ai/gpu-node-join.sh | sudo bash
```

## Verification

Node will appear in Basilica cluster within 2-3 minutes:

- Status starts as "Unschedulable" (validation pending)
- After validation: Status becomes "Ready"
- Workloads can then be scheduled on the node

## Support

Contact: <ops@basilica.ai>

```

Send this document securely to datacenter operator.

### Step 4: Monitor First Node Onboarding

```bash
# Watch for new node registration
watch -n 5 "psql $DATABASE_URL -c \"SELECT node_id, datacenter_id, status, created_at FROM gpu_node_registrations ORDER BY created_at DESC LIMIT 5;\""

# Watch for node joining K3s
kubectl get nodes -w

# Watch basilica-api logs
aws logs tail /ecs/basilica-v3-api --follow | grep -E "(registration|validation|node_watcher)"
```

**Expected Flow:**

1. **Registration** (immediate):

   ```
   [INFO] GPU node registration request: node_id=gpu-01, datacenter=dc-nyc-equinix
   [INFO] GPU node registration approved: reservation_id=abc-123
   ```

2. **Webhook Validation** (when node joins K3s, ~30s):

   ```
   [INFO] Node validation passed for reservation abc-123
   ```

3. **Node Activation** (after node becomes Ready, ~1-2 min):

   ```
   [INFO] Processing newly joined node: gpu-01
   [INFO] Created BasilicaNodeProfile CRD for node gpu-01
   [INFO] Node gpu-01 fully validated and activated
   ```

### Step 5: Verify Node is Operational

```bash
# Check node is Ready and schedulable
kubectl get node <node-name>

# Verify no validation taint
kubectl describe node <node-name> | grep Taints
# Should NOT show: basilica.ai/unvalidated:NoSchedule

# Check labels
kubectl get node <node-name> --show-labels | grep basilica.ai

# Expected labels:
# basilica.ai/node-type=gpu
# basilica.ai/datacenter=dc-nyc-equinix
# basilica.ai/gpu-model=NVIDIA A100
# basilica.ai/gpu-count=8

# Check BasilicaNodeProfile CRD
kubectl get basilicanodeprofiles -n basilica-system

# Verify database status
psql $DATABASE_URL -c "SELECT node_id, status, joined_at FROM gpu_node_registrations WHERE node_id = '<node-id>';"
# Expected: status=ACTIVE, joined_at populated
```

---

## Daily Operations

### Monitor Active GPU Nodes

```bash
# Check all GPU nodes in cluster
kubectl get nodes -l basilica.ai/node-type=gpu

# Check node registration status
psql $DATABASE_URL -c "
SELECT
  datacenter_id,
  COUNT(*) as total_nodes,
  COUNT(*) FILTER (WHERE status = 'ACTIVE') as active,
  COUNT(*) FILTER (WHERE status = 'RESERVED') as pending,
  COUNT(*) FILTER (WHERE status = 'FAILED') as failed
FROM gpu_node_registrations
GROUP BY datacenter_id;
"

# Check recent registrations
psql $DATABASE_URL -c "
SELECT node_id, datacenter_id, status, created_at, joined_at
FROM gpu_node_registrations
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;
"
```

### Monitor Webhook Health

```bash
# Check webhook configuration exists
kubectl get validatingwebhookconfiguration validate-basilica-gpu-nodes

# Test webhook endpoint
curl -v https://api.basilica.ai/webhooks/validate-node
# Expected: 400 or 405 (webhook is working, but needs proper admission request)

# Check webhook rejections in API logs
aws logs filter-pattern "Node validation failed" \
  --log-group-name /ecs/basilica-v3-api \
  --start-time $(date -u -d '1 hour ago' +%s)000
```

### Monitor Cleanup Job

```bash
# Check cleanup job is running (every 5 minutes)
aws logs tail /ecs/basilica-v3-api --since 10m | grep "cleanup"

# Check for expired reservations
psql $DATABASE_URL -c "
SELECT node_id, datacenter_id, status, created_at, expires_at
FROM gpu_node_registrations
WHERE status = 'RESERVED' AND expires_at < NOW();
"
# Should be empty (cleanup job removes these)
```

---

## Troubleshooting

### Issue: Node Registration Fails

**Symptoms:**

- Datacenter reports API error during registration
- No entry in `gpu_node_registrations` table

**Diagnosis:**

```bash
# Check API logs
aws logs tail /ecs/basilica-v3-api --follow | grep "registration"

# Check API key is valid
psql $DATABASE_URL -c "SELECT user_id, name FROM api_keys WHERE user_id = 'dc-xxx';"

# Test registration endpoint
curl -X POST https://api.basilica.ai/v1/gpu-nodes/register \
  -H "Authorization: Bearer basilica_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "node_id": "test-node",
    "datacenter_id": "dc-test",
    "gpu_specs": {
      "count": 1,
      "model": "NVIDIA A100",
      "memory_gb": 80,
      "driver_version": "535.129.03",
      "cuda_version": "12.2"
    }
  }'
```

**Common Causes:**

1. Invalid API key: Regenerate and update
2. Missing K3S_SERVER_URL or K3S_AGENT_TOKEN env vars: Check ECS task definition
3. Database connection issues: Check Aurora security groups

---

### Issue: Node Joins K3s But Stays Unschedulable

**Symptoms:**

- Node appears in `kubectl get nodes`
- Node has taint: `basilica.ai/unvalidated:NoSchedule`
- Status in database is still "RESERVED"

**Diagnosis:**

```bash
# Check node details
kubectl describe node <node-name>

# Check database status
psql $DATABASE_URL -c "SELECT * FROM gpu_node_registrations WHERE k8s_node_name = '<node-name>';"

# Check Node Watcher logs
aws logs tail /ecs/basilica-v3-api --follow | grep "node_watcher"

# Check if node has required labels
kubectl get node <node-name> --show-labels | grep basilica.ai
```

**Common Causes:**

1. **Node not Ready yet**: Wait for node to reach Ready state

   ```bash
   kubectl get node <node-name> -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'
   ```

2. **Missing reservation-id label**: Node didn't get labels during join

   ```bash
   # Check labels
   kubectl get node <node-name> -o yaml | grep -A 5 labels

   # If missing, node needs to re-register
   ```

3. **Node Watcher not running**: Check ECS logs

   ```bash
   aws logs tail /ecs/basilica-v3-api --follow | grep "Starting GPU node watcher"
   ```

**Resolution:**

```bash
# If Node Watcher is stuck, restart ECS service
aws ecs update-service \
  --cluster basilica-v3-cluster \
  --service basilica-v3-api \
  --force-new-deployment \
  --region us-east-1

# If node state is corrupted, manually fix
kubectl taint nodes <node-name> basilica.ai/unvalidated:NoSchedule-

psql $DATABASE_URL -c "UPDATE gpu_node_registrations SET status = 'ACTIVE', joined_at = NOW() WHERE k8s_node_name = '<node-name>';"
```

---

### Issue: Webhook Blocks Valid Node

**Symptoms:**

- Node join fails immediately
- K3s API server logs show webhook denial
- Database has valid RESERVED entry

**Diagnosis:**

```bash
# Check webhook configuration
kubectl get validatingwebhookconfiguration validate-basilica-gpu-nodes -o yaml

# Check webhook endpoint is reachable from K3s
kubectl run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -v https://api.basilica.ai/webhooks/validate-node

# Check API logs for webhook requests
aws logs tail /ecs/basilica-v3-api --follow | grep "validate_node"

# Check VPC peering
aws ec2 describe-vpc-peering-connections --region us-east-1
```

**Common Causes:**

1. **VPC peering issue**: K3s can't reach ECS ALB

   ```bash
   # Check security group allows K3s → ALB traffic
   aws ec2 describe-security-groups \
     --group-ids <alb-security-group-id> \
     --region us-east-1
   ```

2. **Certificate issue**: Empty caBundle but using private CA

   ```bash
   # If using private CA, add caBundle
   kubectl patch validatingwebhookconfiguration validate-basilica-gpu-nodes \
     --type='json' \
     -p="[{'op': 'replace', 'path': '/webhooks/0/clientConfig/caBundle', 'value':'<BASE64_CA_CERT>'}]"
   ```

3. **Label mismatch**: Node labels don't match database

   ```bash
   # Check registration in database
   psql $DATABASE_URL -c "SELECT reservation_id, gpu_count FROM gpu_node_registrations WHERE reservation_id = '<res-id>';"

   # Compare with node labels
   kubectl get node <node-name> --show-labels
   ```

**Emergency Bypass:**

```bash
# ONLY in emergency - temporarily disable webhook
kubectl delete validatingwebhookconfiguration validate-basilica-gpu-nodes

# Allow node to join manually
# Then re-enable webhook
kubectl apply -f orchestrator/k8s/admission/gpu-node-webhook.yaml
```

---

### Issue: Reservation Stuck in RESERVED

**Symptoms:**

- Database entry shows status=RESERVED for > 10 minutes
- Node never joined cluster
- Cleanup job not clearing it

**Diagnosis:**

```bash
# Check stuck reservations
psql $DATABASE_URL -c "
SELECT node_id, datacenter_id, status, created_at, expires_at
FROM gpu_node_registrations
WHERE status = 'RESERVED' AND created_at < NOW() - INTERVAL '15 minutes';
"

# Check cleanup job logs
aws logs tail /ecs/basilica-v3-api --since 15m | grep "cleanup"
```

**Resolution:**

```bash
# Manually mark as FAILED
psql $DATABASE_URL -c "
UPDATE gpu_node_registrations
SET status = 'FAILED', status_message = 'Registration timed out (manual cleanup)'
WHERE node_id = '<node-id>' AND datacenter_id = '<datacenter-id>';
"

# Inform datacenter to retry
```

---

## Emergency Procedures

### Emergency: Disable GPU Node Onboarding

```bash
# 1. Delete webhook (allows manual node management)
kubectl delete validatingwebhookconfiguration validate-basilica-gpu-nodes

# 2. Scale down API or disable GPU features
# Option A: Remove env vars (requires ECS update)
# Option B: Set feature flag
# K3S_SERVER_URL="" will cause registrations to fail safely

# 3. Monitor no new nodes are joining
kubectl get nodes -w
```

### Emergency: Remove Rogue GPU Node

```bash
# 1. Cordon node (prevent new workloads)
kubectl cordon <node-name>

# 2. Drain node (evict existing workloads)
kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data

# 3. Mark as revoked in database
psql $DATABASE_URL -c "UPDATE gpu_node_registrations SET status = 'REVOKED', status_message = 'Emergency removal' WHERE k8s_node_name = '<node-name>';"

# 4. Delete from K3s
kubectl delete node <node-name>
```

### Emergency: Database Rollback

```bash
# If GPU node onboarding causes issues, roll back database

# 1. Disable webhook
kubectl delete validatingwebhookconfiguration validate-basilica-gpu-nodes

# 2. Mark all RESERVED as FAILED
psql $DATABASE_URL -c "UPDATE gpu_node_registrations SET status = 'FAILED' WHERE status = 'RESERVED';"

# 3. Remove validation taints from all nodes
kubectl get nodes -l basilica.ai/datacenter -o name | \
  xargs -I {} kubectl taint nodes {} basilica.ai/unvalidated:NoSchedule-

# 4. Drop tables (if needed - DESTRUCTIVE)
psql $DATABASE_URL <<EOF
DROP TABLE IF EXISTS datacenter_quotas CASCADE;
DROP TABLE IF EXISTS gpu_node_registrations CASCADE;
EOF
```

---

## Appendix: Useful Queries

### List All Active GPU Nodes

```sql
SELECT
  gr.node_id,
  gr.datacenter_id,
  gr.gpu_model,
  gr.gpu_count,
  gr.k8s_node_name,
  gr.joined_at
FROM gpu_node_registrations gr
WHERE gr.status = 'ACTIVE'
ORDER BY gr.joined_at DESC;
```

### Find Orphaned Registrations

```sql
-- Registrations with no corresponding K8s node
SELECT
  gr.node_id,
  gr.datacenter_id,
  gr.k8s_node_name,
  gr.status,
  gr.joined_at
FROM gpu_node_registrations gr
WHERE gr.status = 'ACTIVE'
  AND gr.k8s_node_name IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_name = 'k8s_nodes' -- This is a conceptual check
  );
```

### Datacenter Performance Report

```sql
SELECT
  datacenter_id,
  COUNT(*) as total_registrations,
  COUNT(*) FILTER (WHERE status = 'ACTIVE') as successful,
  COUNT(*) FILTER (WHERE status = 'FAILED') as failed,
  AVG(EXTRACT(EPOCH FROM (joined_at - created_at))) FILTER (WHERE joined_at IS NOT NULL) as avg_join_time_seconds,
  SUM(gpu_count) FILTER (WHERE status = 'ACTIVE') as total_gpus
FROM gpu_node_registrations
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY datacenter_id
ORDER BY total_gpus DESC;
```
