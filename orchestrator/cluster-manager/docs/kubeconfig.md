# Kubeconfig Commands

Generate and manage read-only kubeconfig files for K3s cluster access.

## Overview

The `kubeconfig` command group provides tools for creating, managing, and revoking read-only access to the K3s cluster. It creates ServiceAccounts with restricted permissions suitable for monitoring systems, CI/CD pipelines, and external integrations.

## Why This Command Exists

Granting access to a Kubernetes cluster requires careful permission management:
- Full admin kubeconfig is too permissive for monitoring
- Short-lived tokens require constant rotation
- Manual RBAC setup is error-prone
- Revoking access requires cleanup of multiple resources

This command automates the entire lifecycle of read-only cluster access with security best practices.

## Security Architecture

### Custom ClusterRole: `basilica-readonly`

Instead of using the built-in `view` ClusterRole (which includes Secrets read access), we create a custom role with explicit permissions:

**Allowed resources:**
- Core: pods, pods/log, pods/status, services, endpoints, namespaces, nodes, events
- Storage: persistentvolumeclaims, persistentvolumes
- Workloads: deployments, daemonsets, replicasets, statefulsets
- Batch: jobs, cronjobs
- Networking: ingresses, networkpolicies
- Basilica CRDs: userdeployments, gpurentals, basilicajobs, basilicaqueues, basilicanodeprofiles
- Metrics: pods, nodes (metrics.k8s.io)

**Explicitly denied:**
- secrets (all operations)
- pods/exec, pods/attach, pods/portforward
- All write operations (create, update, delete, patch)

### Token Strategy

Uses K8s 1.24+ Secret-based long-lived tokens:
- Never expires until Secret is deleted
- Survives ServiceAccount recreation
- Explicit lifecycle management
- Immediate revocation capability

### Dedicated Namespace

All monitoring ServiceAccounts are created in `basilica-monitoring` namespace:
- Isolation from application namespaces
- Easier RBAC auditing
- Clear separation of concerns
- Prevents accidental deletion

## Commands

### kubeconfig generate

**What it does:** Creates a complete read-only kubeconfig file with all required RBAC resources.

**How it works:**
1. Creates `basilica-monitoring` namespace (if needed)
2. Creates `basilica-readonly` ClusterRole
3. Creates ServiceAccount in the namespace
4. Creates ClusterRoleBinding to bind permissions
5. Creates Secret with long-lived token
6. Extracts cluster CA and API server from current kubeconfig
7. Builds and writes kubeconfig file with 600 permissions

**When to use:**
- Setting up Prometheus/Grafana monitoring
- Configuring CI/CD pipeline access
- Providing read-only access to external teams
- Creating debugging access for developers

```bash
# Basic usage
clustermgr kubeconfig generate --name prometheus-readonly

# Custom token duration (90 days)
clustermgr kubeconfig generate --name ci-reader --duration 2160h

# Custom output path
clustermgr kubeconfig generate --name grafana --output /etc/grafana/kubeconfig.yaml

# Skip RBAC installation (if already installed)
clustermgr kubeconfig generate --name secondary-monitor --skip-rbac

# Custom cluster name in kubeconfig
clustermgr kubeconfig generate --name external --cluster-name production-k3s

# Dry run to preview
clustermgr kubeconfig generate --name test --dry-run
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-n, --name` | required | ServiceAccount name |
| `--namespace` | basilica-monitoring | Namespace for ServiceAccount |
| `-o, --output` | ./kubeconfig-{name}.yaml | Output file path |
| `-d, --duration` | 8760h (1 year) | Token duration |
| `--cluster-name` | basilica-k3s | Cluster name in kubeconfig |
| `--install-rbac/--skip-rbac` | install | Install RBAC resources |

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

+--------------------------------------------------+
| Summary                                          |
+--------------------------------------------------+
| Kubeconfig generated successfully!               |
|                                                  |
| ServiceAccount: prometheus-readonly              |
| Namespace: basilica-monitoring                   |
| Token Duration: 8760h                            |
| Output File: ./kubeconfig-prometheus-readonly.yaml|
|                                                  |
| Usage:                                           |
|   export KUBECONFIG=./kubeconfig-prometheus...   |
|   kubectl get pods --all-namespaces              |
|                                                  |
| Security Notes:                                  |
|   - Token is read-only                           |
|   - File permissions set to 600                  |
|   - Store securely and rotate periodically       |
+--------------------------------------------------+
```

---

### kubeconfig list

**What it does:** Lists all ServiceAccounts in the monitoring namespace.

**How it works:**
1. Queries ServiceAccounts in specified namespace
2. Displays name, creation time, and secret count

**When to use:**
- Auditing existing read-only accounts
- Before creating new accounts
- Finding accounts to revoke

```bash
# List all accounts
clustermgr kubeconfig list

# List in specific namespace
clustermgr kubeconfig list --namespace custom-monitoring
```

**Output:**
```
ServiceAccounts in basilica-monitoring

+----------------------+------------------------+---------+
| Name                 | Age                    | Secrets |
+----------------------+------------------------+---------+
| prometheus-readonly  | 2024-11-01T10:30:00Z   | 1       |
| grafana-readonly     | 2024-11-15T14:22:00Z   | 1       |
| ci-reader            | 2024-12-01T09:00:00Z   | 1       |
+----------------------+------------------------+---------+
```

---

### kubeconfig revoke

**What it does:** Immediately revokes access by deleting ServiceAccount and token.

**How it works:**
1. Asks for confirmation (unless -y flag)
2. Deletes ServiceAccount
3. Deletes associated token Secret
4. ClusterRoleBinding becomes orphaned (harmless)

**When to use:**
- Access is no longer needed
- Token may be compromised
- User/system is decommissioned
- Security incident response

```bash
# Revoke with confirmation
clustermgr kubeconfig revoke --name prometheus-readonly

# Revoke without confirmation
clustermgr kubeconfig revoke --name compromised-account -y

# Revoke from specific namespace
clustermgr kubeconfig revoke --name old-monitor --namespace custom-ns
```

**Output:**
```
Revoking ServiceAccount: prometheus-readonly

Delete ServiceAccount 'prometheus-readonly' in namespace 'basilica-monitoring'? [y/N]: y

ServiceAccount 'prometheus-readonly' revoked successfully
```

---

### kubeconfig verify

**What it does:** Tests that a kubeconfig file has correct read-only permissions.

**How it works:**
1. Tests read operations (should succeed)
2. Tests write operations (should fail)
3. Reports pass/fail for each test

**When to use:**
- After generating a new kubeconfig
- Auditing existing kubeconfigs
- Debugging permission issues

```bash
clustermgr kubeconfig verify --kubeconfig-path ./kubeconfig-prometheus-readonly.yaml
```

**Output:**
```
Verifying kubeconfig: ./kubeconfig-prometheus-readonly.yaml

Permission Tests
+--------------------------------+----------+--------+
| Test                           | Expected | Result |
+--------------------------------+----------+--------+
| list pods                      | Allow    | PASS   |
| list nodes                     | Allow    | PASS   |
| list namespaces                | Allow    | PASS   |
| get userdeployments            | Allow    | PASS   |
| create pod (should fail)       | Deny     | PASS   |
| delete namespace (should fail) | Deny     | PASS   |
+--------------------------------+----------+--------+
```

---

### kubeconfig rotate

**What it does:** Rotates the token for an existing ServiceAccount.

**How it works:**
1. Deletes old token Secret
2. Creates new token Secret with timestamp
3. Waits for token to be populated
4. Generates new kubeconfig file

**When to use:**
- Scheduled token rotation
- After potential token exposure
- Security policy compliance

```bash
# Rotate with default settings
clustermgr kubeconfig rotate --name prometheus-readonly

# Custom output for new kubeconfig
clustermgr kubeconfig rotate --name prometheus-readonly --output ~/new-kubeconfig.yaml

# Custom token duration
clustermgr kubeconfig rotate --name ci-reader --duration 2160h
```

**Output:**
```
Rotating token for: prometheus-readonly

Token rotated successfully
New kubeconfig: ./kubeconfig-prometheus-readonly-new.yaml
```

## Token Duration Recommendations

| Use Case | Duration | Rationale |
|----------|----------|-----------|
| Monitoring (Prometheus/Grafana) | 8760h (1 year) | Long-running infrastructure |
| CI/CD pipelines | 4380h (6 months) | Balance security/maintenance |
| Developer debugging | 2160h (90 days) | Limited access period |
| Emergency/incident access | 24h | Minimal exposure window |
| Audit/compliance review | 168h (1 week) | Time-boxed access |

## Usage Examples

### Prometheus Monitoring Setup

```bash
# Generate kubeconfig
clustermgr kubeconfig generate --name prometheus-readonly

# Configure Prometheus
# In prometheus.yml:
#   kubernetes_sd_configs:
#   - kubeconfig_file: /etc/prometheus/kubeconfig.yaml
```

### CI/CD Pipeline (GitHub Actions)

```bash
# Generate with 6-month duration
clustermgr kubeconfig generate --name github-ci --duration 4380h

# Store as GitHub secret
cat kubeconfig-github-ci.yaml | base64 | gh secret set KUBECONFIG_B64
```

### External Monitoring (Grafana Cloud)

```bash
# Generate and output to specific location
clustermgr kubeconfig generate \
  --name grafana-cloud \
  --output /etc/grafana-agent/kubeconfig.yaml \
  --cluster-name production
```

### Developer Debugging Access

```bash
# Short-duration token for debugging
clustermgr kubeconfig generate --name debug-alice --duration 168h

# When done
clustermgr kubeconfig revoke --name debug-alice
```

## Troubleshooting

### Token Not Working

```bash
# Verify permissions
clustermgr kubeconfig verify --kubeconfig-path ./kubeconfig-name.yaml

# Check ServiceAccount exists
clustermgr kubeconfig list

# Check token Secret
kubectl get secret -n basilica-monitoring -l app=basilica-cluster-manager
```

### Permission Denied Errors

```bash
# Check ClusterRole exists
kubectl get clusterrole basilica-readonly -o yaml

# Check ClusterRoleBinding
kubectl get clusterrolebinding | grep readonly

# Verify binding subjects
kubectl get clusterrolebinding name-binding -o yaml
```

### Token Expired or Invalid

```bash
# Rotate the token
clustermgr kubeconfig rotate --name account-name

# Or regenerate completely
clustermgr kubeconfig revoke --name account-name
clustermgr kubeconfig generate --name account-name
```

## Security Best Practices

1. **Minimal permissions**: The custom ClusterRole grants only read access
2. **Namespace isolation**: All accounts in dedicated namespace
3. **File permissions**: Kubeconfig files created with 600 permissions
4. **Token rotation**: Rotate tokens periodically (recommended: quarterly)
5. **Immediate revocation**: Use `revoke` command when access no longer needed
6. **Audit trail**: Track who has access via ServiceAccount names
7. **Separate accounts**: Create separate accounts per use case

## Related Commands

- `cert-check` - Check certificate expiration
- `audit-pods` - Security audit of running pods

## Related Runbooks

- `docs/runbooks/NETWORK-MAINTENANCE-PROCEDURES.md` - Security procedures
