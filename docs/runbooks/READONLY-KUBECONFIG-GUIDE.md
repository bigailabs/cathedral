# Read-Only Kubeconfig Generation Guide

Production-grade guide for generating secure, read-only kubeconfig files for K3s cluster monitoring and external integrations.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Best Practices](#best-practices)
- [Quick Start](#quick-start)
- [Advanced Usage](#advanced-usage)
- [Security Considerations](#security-considerations)
- [Troubleshooting](#troubleshooting)
- [Reference](#reference)

## Overview

The `clustermgr kubeconfig` command automates creation of read-only kubeconfig files for:

- **Monitoring systems** (Prometheus, Grafana, Datadog)
- **CI/CD pipelines** (GitHub Actions, GitLab CI, Jenkins)
- **External dashboards** (Kubernetes Dashboard, Lens, K9s)
- **Incident response teams** (read-only cluster access)
- **Third-party integrations** (cost management, security scanning)

### Why Custom Read-Only Kubeconfig?

**Problem:** Built-in `view` ClusterRole is too permissive for public-facing clusters:

- Grants read access to Secrets in namespaces where bound
- No fine-grained control over specific resources
- Cannot restrict by namespace patterns
- Difficult to audit and track usage

**Solution:** Custom ClusterRole with:

- Explicit resource permissions
- No Secret access
- Namespace-scoped bindings
- Clear audit trail
- Easy rotation and revocation

## Architecture

### Components

```
┌─────────────────────────────────────────────────────────────┐
│                    K3s Cluster (1.24+)                       │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌───────────────────────────────────────────────────┐      │
│  │ basilica-monitoring namespace                      │      │
│  ├───────────────────────────────────────────────────┤      │
│  │                                                     │      │
│  │  ServiceAccount: prometheus-readonly               │      │
│  │  Secret: prometheus-readonly-token                 │      │
│  │    └─ type: kubernetes.io/service-account-token   │      │
│  │    └─ token: <long-lived JWT>                     │      │
│  │                                                     │      │
│  └───────────────────────────────────────────────────┘      │
│                          │                                    │
│                          │ bound to                           │
│                          ▼                                    │
│  ┌───────────────────────────────────────────────────┐      │
│  │ ClusterRole: basilica-readonly                     │      │
│  ├───────────────────────────────────────────────────┤      │
│  │ Rules:                                              │      │
│  │   - pods (get, list, watch)                        │      │
│  │   - nodes (get, list, watch)                       │      │
│  │   - deployments (get, list, watch)                 │      │
│  │   - userdeployments (get, list, watch)             │      │
│  │   - metrics (get, list)                            │      │
│  │                                                     │      │
│  │ Explicitly DENY:                                   │      │
│  │   - secrets (all operations)                       │      │
│  │   - pods/exec, pods/attach                         │      │
│  │   - create, update, delete (all resources)         │      │
│  └───────────────────────────────────────────────────┘      │
│                                                               │
└─────────────────────────────────────────────────────────────┘
                          │
                          │ token extracted
                          ▼
              ┌───────────────────────┐
              │  kubeconfig file      │
              ├───────────────────────┤
              │  - cluster CA cert    │
              │  - API server URL     │
              │  - ServiceAccount     │
              │    token              │
              └───────────────────────┘
```

### RBAC Model

**Namespace Isolation:**

```
basilica-monitoring (dedicated namespace)
  └─ ServiceAccount: <name>
       └─ ClusterRoleBinding
            └─ ClusterRole: basilica-readonly (cluster-wide read)
```

**Why ClusterRole + ClusterRoleBinding:**

- Enables read access across all namespaces
- Single RBAC policy for consistency
- Easier to audit and manage
- Supports multi-tenant architecture

**Alternative: Namespace-scoped access:**

```yaml
# Use RoleBinding instead for single namespace
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: readonly-binding
  namespace: u-alice
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: basilica-readonly
subjects:
- kind: ServiceAccount
  name: readonly-user
  namespace: basilica-monitoring
```

### Token Lifecycle

**K8s 1.24+ Token Generation:**

Prior to K8s 1.24, ServiceAccount tokens were automatically created as Secrets. This changed in 1.24+ for security reasons.

**Two approaches:**

1. **Long-lived tokens via Secret (Recommended for automation):**

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: sa-token
  annotations:
    kubernetes.io/service-account.name: readonly-user
type: kubernetes.io/service-account-token
```

**Advantages:**

- Never expires (until Secret deleted)
- Survives ServiceAccount recreation
- Ideal for monitoring systems
- Explicit lifecycle management

**Disadvantages:**

- Manual rotation required
- No automatic expiration

2. **TokenRequest API (Recommended for short-lived):**

```bash
kubectl create token readonly-user --duration=8760h
```

**Advantages:**

- Configurable expiration (max 1 year in K3s)
- No Secret required
- Automatic cleanup

**Disadvantages:**

- Lost if ServiceAccount deleted
- Harder to track usage
- Manual renewal required

**Our implementation uses approach #1 for monitoring/CI systems.**

## Best Practices

### 1. ClusterRole Selection

**Recommendation: Custom ClusterRole** (`basilica-readonly`)

**Explicitly allowed resources:**

- `pods`, `pods/log`, `pods/status` (monitoring)
- `nodes` (capacity planning)
- `deployments`, `statefulsets`, `daemonsets` (workload visibility)
- `services`, `endpoints` (networking)
- `namespaces` (multi-tenancy)
- `events` (troubleshooting)
- `userdeployments`, `gpurentals` (Basilica CRDs)
- `metrics.k8s.io/pods`, `metrics.k8s.io/nodes` (metrics)

**Explicitly denied:**

- `secrets` (all operations) - prevents credential leakage
- `configmaps` with sensitive data
- `serviceaccounts/token` - prevents token theft
- `pods/exec`, `pods/attach`, `pods/portforward` - prevents command execution
- `nodes/proxy` - prevents node access
- All write operations (`create`, `update`, `patch`, `delete`)

### 2. Namespace Strategy

**Recommendation: Dedicated `basilica-monitoring` namespace**

**Rationale:**

- Isolates monitoring accounts from application workloads
- Easier RBAC auditing (`kubectl get clusterrolebindings | grep basilica-monitoring`)
- Clear separation of concerns
- Prevents accidental deletion with app resources
- Enables namespace-level policies (NetworkPolicy, ResourceQuota)

**Alternative namespaces:**

- `kube-system` - NOT recommended (core cluster components)
- `default` - NOT recommended (mixed with apps)
- `monitoring` - acceptable if existing monitoring stack
- Custom per-integration (e.g., `prometheus-system`, `ci-system`)

### 3. Token Expiration

**Recommendations by use case:**

| Use Case | Duration | Rotation Policy |
|----------|----------|-----------------|
| Monitoring systems | 1 year (8760h) | Annual rotation |
| CI/CD pipelines | 6 months (4380h) | Bi-annual rotation |
| Human users | 90 days (2160h) | Quarterly rotation |
| External integrations | 6 months (4380h) | Bi-annual rotation |
| Emergency access | 24 hours (24h) | Single-use |
| Development/testing | 7 days (168h) | Weekly rotation |

**Rotation automation:**

```bash
# Rotate every 6 months via cron
0 0 1 */6 * /usr/local/bin/clustermgr kubeconfig rotate \
  --name prometheus-readonly \
  --output /etc/prometheus/kubeconfig.yaml && \
  systemctl restart prometheus
```

### 4. K3s CA Certificate & API Server Discovery

**Extract from K3s configuration:**

```bash
# K3s stores cluster config at /etc/rancher/k3s/k3s.yaml

# Extract CA certificate (base64 encoded)
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'

# Or from file (PEM format)
cat /var/lib/rancher/k3s/server/tls/server-ca.crt | base64 -w 0

# Extract API server endpoint
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.server}'

# K3s default: https://<node-ip>:6443
```

**For public clusters:**

- Use load balancer URL: `https://k3s-lb.basilica.ai:6443`
- NOT individual node IPs (prevents failure if node down)
- Enable TLS verification (include CA cert)

### 5. Security Considerations for Public K3s

**Critical security measures:**

1. **Enable API server audit logging:**

```yaml
# /etc/rancher/k3s/config.yaml
kube-apiserver-arg:
  - "audit-log-path=/var/log/kubernetes/audit.log"
  - "audit-log-maxage=30"
  - "audit-log-maxbackup=10"
  - "audit-policy-file=/etc/rancher/k3s/audit-policy.yaml"
```

2. **Rate limiting:**

```yaml
kube-apiserver-arg:
  - "max-requests-inflight=400"
  - "max-mutating-requests-inflight=200"
```

3. **Disable anonymous auth:**

```yaml
kube-apiserver-arg:
  - "anonymous-auth=false"
```

4. **Network policies:**

```yaml
# Restrict API server access
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-server-ingress
  namespace: kube-system
spec:
  podSelector:
    matchLabels:
      component: kube-apiserver
  policyTypes:
    - Ingress
  ingress:
    - from:
      - ipBlock:
          cidr: 10.0.0.0/8  # Internal networks only
      ports:
      - protocol: TCP
        port: 6443
```

5. **Monitor token usage:**

```bash
# Audit log analysis
jq 'select(.user.username | startswith("system:serviceaccount:basilica-monitoring:"))' \
  /var/log/kubernetes/audit.log | \
  jq -r '[.timestamp, .user.username, .verb, .objectRef.resource] | @tsv'
```

6. **Rotate tokens periodically:**

```bash
# Set reminder for 6-month rotation
echo "0 0 1 */6 * /usr/local/bin/rotate-tokens.sh" | crontab -
```

7. **Revoke on compromise:**

```bash
# Immediate revocation
clustermgr kubeconfig revoke --name compromised-account
```

## Quick Start

### 1. Generate Read-Only Kubeconfig

```bash
# Basic generation (1-year token)
clustermgr kubeconfig generate --name prometheus-readonly

# Custom duration (90 days)
clustermgr kubeconfig generate \
  --name ci-reader \
  --duration 2160h

# Custom output path
clustermgr kubeconfig generate \
  --name grafana-readonly \
  --output /etc/grafana/k3s-kubeconfig.yaml

# Skip RBAC (if already installed)
clustermgr kubeconfig generate \
  --name datadog-agent \
  --skip-rbac
```

**Output:**

```
Generating read-only kubeconfig: prometheus-readonly

Installing RBAC resources...
RBAC resources installed

Creating ServiceAccount: prometheus-readonly...
ServiceAccount created

Creating token (duration: 8760h)...
Token created

Extracting cluster CA certificate...
CA certificate extracted

Extracting API server endpoint...
API server endpoint extracted

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                            Summary                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

Kubeconfig generated successfully!

ServiceAccount: prometheus-readonly
Namespace: basilica-monitoring
Token Duration: 8760h
Output File: ./kubeconfig-prometheus-readonly.yaml

Usage:
  export KUBECONFIG=./kubeconfig-prometheus-readonly.yaml
  kubectl get pods --all-namespaces
  kubectl get userdeployments -A

Security Notes:
  - Token is read-only (no create/update/delete permissions)
  - File permissions set to 600 (owner read/write only)
  - Store securely and rotate periodically
  - Revoke access: clustermgr kubeconfig revoke --name prometheus-readonly

Verification:
  clustermgr kubeconfig verify --kubeconfig-path ./kubeconfig-prometheus-readonly.yaml
```

### 2. Verify Permissions

```bash
# Test read and write operations
clustermgr kubeconfig verify --kubeconfig-path kubeconfig-prometheus-readonly.yaml
```

**Output:**

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                   Permission Tests                      ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Test                       │ Expected │ Result         │
├────────────────────────────┼──────────┼────────────────┤
│ list pods                  │ Allow    │ PASS           │
│ list nodes                 │ Allow    │ PASS           │
│ list namespaces            │ Allow    │ PASS           │
│ get userdeployments        │ Allow    │ PASS           │
│ create pod (should fail)   │ Deny     │ PASS           │
│ delete namespace (...)     │ Deny     │ PASS           │
└────────────────────────────┴──────────┴────────────────┘
```

### 3. Use Kubeconfig

```bash
# Export as environment variable
export KUBECONFIG=./kubeconfig-prometheus-readonly.yaml

# Test access
kubectl get pods --all-namespaces
kubectl get userdeployments -A
kubectl get nodes

# Integrate with monitoring
# Prometheus
prometheus --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus/data \
  --kubeconfig=/etc/prometheus/kubeconfig.yaml

# Grafana
# Add kubeconfig to grafana.ini
[auth.generic_oauth]
kubeconfig_path = /etc/grafana/k3s-kubeconfig.yaml
```

## Advanced Usage

### Rotate Token

```bash
# Rotate token (creates new Secret, deletes old)
clustermgr kubeconfig rotate \
  --name prometheus-readonly \
  --output kubeconfig-prometheus-readonly-new.yaml

# Atomic replacement
mv kubeconfig-prometheus-readonly-new.yaml kubeconfig-prometheus-readonly.yaml

# Restart services using kubeconfig
systemctl restart prometheus
```

### List ServiceAccounts

```bash
# List all monitoring accounts
clustermgr kubeconfig list

# List in custom namespace
clustermgr kubeconfig list --namespace monitoring
```

**Output:**

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃        ServiceAccounts in basilica-monitoring         ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Name                  │ Age                │ Secrets │
├───────────────────────┼────────────────────┼─────────┤
│ prometheus-readonly   │ 2024-11-15T10:00Z  │ 1       │
│ ci-reader             │ 2024-11-20T14:30Z  │ 1       │
│ grafana-readonly      │ 2024-12-01T08:00Z  │ 1       │
└───────────────────────┴────────────────────┴─────────┘
```

### Revoke Access

```bash
# Revoke immediately (deletes ServiceAccount and token)
clustermgr kubeconfig revoke --name compromised-account

# With confirmation prompt
clustermgr kubeconfig revoke --name old-integration

# Skip confirmation (automation)
clustermgr kubeconfig revoke --name expired-token --no-confirm
```

### Dry-Run Mode

```bash
# Preview changes without applying
clustermgr --dry-run kubeconfig generate --name test-account

# Shows what would be created
clustermgr --dry-run kubeconfig revoke --name test-account
```

### Namespace-Scoped Access

For single-namespace read access, manually create RoleBinding:

```bash
# Generate ServiceAccount
clustermgr kubeconfig generate \
  --name alice-viewer \
  --skip-rbac

# Create RoleBinding (not ClusterRoleBinding)
kubectl create rolebinding alice-viewer-binding \
  --clusterrole=basilica-readonly \
  --serviceaccount=basilica-monitoring:alice-viewer \
  --namespace=u-alice

# Now alice-viewer can only read u-alice namespace
```

## Security Considerations

### Threat Model

**Assets protected:**

- Cluster configuration and state
- Workload secrets (credentials, API keys)
- User data (via exec/attach)
- Node access (via proxy)

**Threats mitigated:**

1. **Unauthorized write operations** - RBAC denies all mutations
2. **Secret leakage** - ClusterRole excludes `secrets` resource
3. **Command execution** - Denies `pods/exec`, `pods/attach`
4. **Node compromise** - Denies `nodes/proxy`
5. **Token theft** - Denies `serviceaccounts/token`

**Threats NOT mitigated:**

1. **Token compromise** - If kubeconfig file stolen, attacker has read access
   - **Mitigation:** Rotate tokens regularly, monitor audit logs, use short durations
2. **API server DoS** - Read-only access still allows list/watch operations
   - **Mitigation:** Rate limiting, network policies, token revocation
3. **Information disclosure** - Read access exposes cluster topology, workload names
   - **Mitigation:** Consider this acceptable for monitoring systems

### Token Storage

**DO NOT store tokens in:**

- Version control (Git, SVN)
- Shared filesystems without encryption
- Container images
- CI/CD logs
- Publicly accessible locations

**DO store tokens in:**

- Secrets management (Vault, AWS Secrets Manager)
- Encrypted filesystems
- Service-specific config directories with 600 permissions
- Environment variables (for CI/CD, short-lived)

**Example: Vault integration**

```bash
# Store in Vault
vault kv put secret/k3s/readonly-kubeconfig \
  data=@kubeconfig-prometheus-readonly.yaml

# Retrieve in application
vault kv get -field=data secret/k3s/readonly-kubeconfig > /tmp/kubeconfig
export KUBECONFIG=/tmp/kubeconfig
```

### Audit Logging

**Enable audit logging for ServiceAccount operations:**

```yaml
# /etc/rancher/k3s/audit-policy.yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
  # Log all requests from monitoring ServiceAccounts
  - level: RequestResponse
    users:
      - system:serviceaccount:basilica-monitoring:*
    omitStages:
      - RequestReceived

  # Log ServiceAccount token creation/deletion
  - level: RequestResponse
    verbs: ["create", "delete", "patch"]
    resources:
      - group: ""
        resources: ["secrets"]
        resourceNames: ["*-token"]
    omitStages:
      - RequestReceived
```

**Analyze audit logs:**

```bash
# Requests by ServiceAccount
jq 'select(.user.username | startswith("system:serviceaccount:basilica-monitoring:"))' \
  /var/log/kubernetes/audit.log | \
  jq -r '.user.username' | sort | uniq -c | sort -rn

# Denied requests (potential attacks)
jq 'select(.user.username | startswith("system:serviceaccount:basilica-monitoring:")) |
    select(.responseStatus.code >= 400)' \
  /var/log/kubernetes/audit.log | \
  jq -r '[.timestamp, .user.username, .verb, .objectRef.resource, .responseStatus.code] | @tsv'
```

### Network Isolation

**Restrict API server access:**

```yaml
# Allow only from monitoring subnet
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-server-access
  namespace: kube-system
spec:
  podSelector:
    matchLabels:
      component: kube-apiserver
  policyTypes:
    - Ingress
  ingress:
    - from:
      - ipBlock:
          cidr: 10.0.1.0/24  # Monitoring subnet
      - podSelector:
          matchLabels:
            app: prometheus
      ports:
      - protocol: TCP
        port: 6443
```

## Troubleshooting

### Token Not Generated

**Symptom:** `Token not populated in Secret after 10s`

**Cause:** K8s 1.24+ requires explicit token controller

**Solution:**

```bash
# Check token controller is running
kubectl get pods -n kube-system -l component=kube-controller-manager

# Check Secret created
kubectl get secret -n basilica-monitoring prometheus-readonly-token -o yaml

# Manual token creation (fallback)
kubectl create token prometheus-readonly \
  --namespace basilica-monitoring \
  --duration=8760h
```

### Permission Denied

**Symptom:** `Error from server (Forbidden): pods is forbidden`

**Cause:** ClusterRoleBinding not created or incorrect

**Solution:**

```bash
# Check ClusterRoleBinding exists
kubectl get clusterrolebinding | grep readonly

# Verify binding
kubectl describe clusterrolebinding prometheus-readonly-binding

# Expected output:
# Role:
#   Kind:  ClusterRole
#   Name:  basilica-readonly
# Subjects:
#   Kind            Name                 Namespace
#   ----            ----                 ---------
#   ServiceAccount  prometheus-readonly  basilica-monitoring
```

### CA Certificate Verification Failed

**Symptom:** `x509: certificate signed by unknown authority`

**Cause:** Incorrect or missing CA certificate in kubeconfig

**Solution:**

```bash
# Extract CA cert manually
kubectl config view --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | \
  base64 -d > /tmp/ca.crt

# Verify CA cert
openssl x509 -in /tmp/ca.crt -text -noout

# Update kubeconfig
kubectl config set-cluster basilica-k3s \
  --certificate-authority=/tmp/ca.crt \
  --embed-certs=true
```

### Connection Refused

**Symptom:** `dial tcp <ip>:6443: connect: connection refused`

**Cause:** API server endpoint incorrect or not accessible

**Solution:**

```bash
# Test API server connectivity
curl -k https://<api-server>:6443/healthz

# Check K3s service
systemctl status k3s

# Verify firewall allows 6443
sudo iptables -L -n | grep 6443
```

### Token Expired

**Symptom:** `error: You must be logged in to the server (Unauthorized)`

**Cause:** Token expired (if using TokenRequest API)

**Solution:**

```bash
# Check token expiration
kubectl get secret -n basilica-monitoring prometheus-readonly-token -o yaml | \
  grep -A 1 'kubernetes.io/service-account.token'

# Rotate token
clustermgr kubeconfig rotate --name prometheus-readonly
```

## Reference

### Complete YAML Manifests

#### 1. Namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: basilica-monitoring
  labels:
    purpose: monitoring
    security: restricted
```

#### 2. ClusterRole (Read-Only)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: basilica-readonly
  labels:
    app: basilica-cluster-manager
rules:
  # Core resources
  - apiGroups: [""]
    resources:
      - pods
      - pods/log
      - pods/status
      - services
      - endpoints
      - namespaces
      - nodes
      - persistentvolumeclaims
      - persistentvolumes
      - events
    verbs: ["get", "list", "watch"]

  # Workload resources
  - apiGroups: ["apps"]
    resources:
      - deployments
      - daemonsets
      - replicasets
      - statefulsets
    verbs: ["get", "list", "watch"]

  # Batch resources
  - apiGroups: ["batch"]
    resources:
      - jobs
      - cronjobs
    verbs: ["get", "list", "watch"]

  # Networking
  - apiGroups: ["networking.k8s.io"]
    resources:
      - ingresses
      - networkpolicies
    verbs: ["get", "list", "watch"]

  # Custom resources (Basilica CRDs)
  - apiGroups: ["basilica.ai"]
    resources:
      - userdeployments
      - gpurentals
      - basilicajobs
      - basilicaqueues
      - basilicanodeprofiles
    verbs: ["get", "list", "watch"]

  # Metrics
  - apiGroups: ["metrics.k8s.io"]
    resources:
      - pods
      - nodes
    verbs: ["get", "list"]
```

#### 3. ServiceAccount

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: prometheus-readonly
  namespace: basilica-monitoring
  labels:
    app: basilica-cluster-manager
    purpose: readonly-access
```

#### 4. Token Secret (Long-Lived)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: prometheus-readonly-token
  namespace: basilica-monitoring
  annotations:
    kubernetes.io/service-account.name: prometheus-readonly
  labels:
    app: basilica-cluster-manager
type: kubernetes.io/service-account-token
```

#### 5. ClusterRoleBinding

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: prometheus-readonly-binding
  labels:
    app: basilica-cluster-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: basilica-readonly
subjects:
  - kind: ServiceAccount
    name: prometheus-readonly
    namespace: basilica-monitoring
```

### Manual kubectl Commands

If not using `clustermgr`, here are equivalent kubectl commands:

```bash
# 1. Create namespace
kubectl create namespace basilica-monitoring

# 2. Create ClusterRole
kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: basilica-readonly
rules:
  - apiGroups: [""]
    resources: ["pods", "nodes", "namespaces", "services"]
    verbs: ["get", "list", "watch"]
EOF

# 3. Create ServiceAccount
kubectl create serviceaccount prometheus-readonly -n basilica-monitoring

# 4. Create token Secret
kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: prometheus-readonly-token
  namespace: basilica-monitoring
  annotations:
    kubernetes.io/service-account.name: prometheus-readonly
type: kubernetes.io/service-account-token
EOF

# 5. Create ClusterRoleBinding
kubectl create clusterrolebinding prometheus-readonly-binding \
  --clusterrole=basilica-readonly \
  --serviceaccount=basilica-monitoring:prometheus-readonly

# 6. Wait for token population (up to 10s)
sleep 5

# 7. Extract token
TOKEN=$(kubectl get secret prometheus-readonly-token \
  -n basilica-monitoring \
  -o jsonpath='{.data.token}' | base64 -d)

# 8. Extract CA cert
CA_CERT=$(kubectl config view --raw \
  -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

# 9. Extract API server
API_SERVER=$(kubectl config view --raw \
  -o jsonpath='{.clusters[0].cluster.server}')

# 10. Build kubeconfig
cat > kubeconfig-prometheus-readonly.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CA_CERT}
    server: ${API_SERVER}
  name: basilica-k3s
contexts:
- context:
    cluster: basilica-k3s
    user: prometheus-readonly
  name: prometheus-readonly@basilica-k3s
current-context: prometheus-readonly@basilica-k3s
users:
- name: prometheus-readonly
  user:
    token: ${TOKEN}
EOF

# 11. Set permissions
chmod 600 kubeconfig-prometheus-readonly.yaml
```

### CLI Reference

```bash
# Generate read-only kubeconfig
clustermgr kubeconfig generate --name <name> [OPTIONS]

Options:
  --namespace TEXT          Namespace for ServiceAccount (default: basilica-monitoring)
  --output, -o PATH         Output file path (default: ./kubeconfig-{name}.yaml)
  --duration, -d TEXT       Token duration (default: 8760h)
  --cluster-name TEXT       Cluster name in kubeconfig (default: basilica-k3s)
  --install-rbac/--skip-rbac  Install RBAC resources (default: --install-rbac)

# List ServiceAccounts
clustermgr kubeconfig list [OPTIONS]

Options:
  --namespace, -n TEXT      Namespace to list from (default: basilica-monitoring)

# Verify kubeconfig permissions
clustermgr kubeconfig verify --kubeconfig-path PATH

# Rotate token
clustermgr kubeconfig rotate --name TEXT [OPTIONS]

Options:
  --namespace TEXT          Namespace (default: basilica-monitoring)
  --output, -o PATH         Output file for new kubeconfig
  --duration, -d TEXT       Token duration (default: 8760h)

# Revoke access
clustermgr kubeconfig revoke --name TEXT [OPTIONS]

Options:
  --namespace TEXT          Namespace (default: basilica-monitoring)

# Global options
  --dry-run                 Preview changes without applying
  --no-confirm, -y          Skip confirmation prompts
  --verbose, -v             Show verbose output
  --kubeconfig PATH         Path to kubeconfig for cluster access
```

## Related Documentation

- [Kubernetes RBAC Documentation](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)
- [K3s Security Hardening Guide](https://docs.k3s.io/security/hardening-guide)
- [CIS Kubernetes Benchmark](https://www.cisecurity.org/benchmark/kubernetes)
- [Kubernetes Audit Logging](https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/)
