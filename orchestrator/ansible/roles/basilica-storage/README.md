# Basilica Storage Ansible Role

This role deploys the R2 credentials secret for Basilica's FUSE-based persistent storage layer.

## Description

The `basilica-storage` role creates a Kubernetes Secret containing validator-managed R2 (or S3) credentials that are used by the storage daemon sidecar for transparent file I/O backed by object storage.

## Requirements

- Kubernetes cluster accessible via `kubectl` or in-cluster config
- `kubernetes.core` Ansible collection installed
- R2 or S3 bucket created and credentials available

## Role Variables

### Required Variables

These must be provided via environment variables or playbook vars:

| Variable | Environment Variable | Description |
|----------|---------------------|-------------|
| `basilica_r2_access_key_id` | `BASILICA_R2_ACCESS_KEY_ID` | R2/S3 access key ID |
| `basilica_r2_secret_access_key` | `BASILICA_R2_SECRET_ACCESS_KEY` | R2/S3 secret access key |
| `basilica_r2_bucket` | `BASILICA_R2_BUCKET` | R2/S3 bucket name |
| `basilica_r2_endpoint` | `BASILICA_R2_ENDPOINT` | R2 endpoint URL (e.g., `https://account.r2.cloudflarestorage.com`) |

### Optional Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `basilica_r2_backend` | `r2` | Storage backend: `r2`, `s3`, or `gcs` |
| `basilica_enable_persistent_storage` | `false` | Enable persistent storage deployment |

## Dependencies

None.

## Example Playbook

```yaml
---
- name: Deploy Basilica Storage Credentials
  hosts: localhost
  gather_facts: false
  roles:
    - role: basilica-storage
      when: basilica_enable_persistent_storage | default(false)
```

## Example Usage

### 1. Set Environment Variables

```bash
export BASILICA_R2_ACCESS_KEY_ID="your-r2-access-key"
export BASILICA_R2_SECRET_ACCESS_KEY="your-r2-secret-key"
export BASILICA_R2_BUCKET="basilica-storage-production"
export BASILICA_R2_ENDPOINT="https://your-account.r2.cloudflarestorage.com"
export BASILICA_ENABLE_PERSISTENT_STORAGE=true
```

### 2. Run Playbook

```bash
cd scripts/ansible
ansible-playbook -i inventories/example.ini playbooks/e2e-apply.yml
```

### 3. Verify Deployment

```bash
kubectl get secret basilica-r2-credentials -n basilica-system
kubectl describe secret basilica-r2-credentials -n basilica-system
```

## What This Role Does

1. **Validates** R2 credentials are provided
2. **Creates** `basilica-system` namespace if not exists
3. **Deploys** `basilica-r2-credentials` Secret with R2 credentials
4. **Creates** RBAC Role for reading the secret
5. **Binds** operator service account to the role
6. **Verifies** secret creation and displays status

## Security

- Credentials stored as Kubernetes Secret in `basilica-system` namespace
- RBAC restricts access to operator service account only
- Secret values are masked in Ansible output
- Use Kubernetes External Secrets Operator for production (fetch from Vault/AWS Secrets Manager)

## Secret Format

The created secret contains:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: basilica-r2-credentials
  namespace: basilica-system
type: Opaque
data:
  STORAGE_ACCESS_KEY_ID: <base64-encoded>
  STORAGE_SECRET_ACCESS_KEY: <base64-encoded>
  STORAGE_BUCKET: <base64-encoded>
  STORAGE_ENDPOINT: <base64-encoded>
  STORAGE_BACKEND: <base64-encoded>
```

## Storage Daemon Usage

The operator automatically injects these credentials into the storage daemon sidecar via `envFrom`:

```yaml
env_from:
  - secretRef:
      name: basilica-r2-credentials
```

## Troubleshooting

### Secret not found

```bash
# Check if secret exists
kubectl get secret basilica-r2-credentials -n basilica-system

# Check RBAC
kubectl get role basilica-storage-secret-reader -n basilica-system
kubectl get rolebinding basilica-operator-storage-secret -n basilica-system
```

### Permission denied

Ensure the operator service account has the RBAC binding:

```bash
kubectl describe rolebinding basilica-operator-storage-secret -n basilica-system
```

### Invalid credentials

Check secret values (masked):

```bash
kubectl get secret basilica-r2-credentials -n basilica-system -o jsonpath='{.data}'
```

## License

Proprietary

## Author

Basilica Team
