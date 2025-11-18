# Kubernetes Manifests

This directory contains all Kubernetes manifests for the Basilica platform, organized by component type.

## Directory Structure

```text
k8s/
├── core/                    # Required cluster resources
│   ├── namespaces.yaml     # Three namespaces: basilica-system, basilica-validators, basilica-system
│   └── rbac/               # RBAC policies
│       ├── operator-rbac.yaml
│       ├── operator-tenant-role.yaml (templated)
│       └── bootstrap-token-rbac.yaml  # Bootstrap token RBAC for GPU node joining
│
├── services/                # Service deployments
│   └── operator/           # Basilica K8s operator (deployment + pdb)
│
├── observability/           # Telemetry and monitoring
│   ├── alloy/              # Grafana Alloy telemetry agent (DaemonSet)
│   └── disk-cleanup-cronjob.yaml
│
└── networking/              # Ingress and routing
    ├── envoy/              # Envoy forward proxy
    └── gateway-api/        # Kubernetes Gateway API resources
        └── examples/       # Example Gateway/HTTPRoute manifests
```

## Deployment

All manifests are deployed via Ansible playbooks in `orchestrator/ansible/playbooks/`.

**Primary Deployment**:

```bash
cd orchestrator/ansible
ansible-playbook -i inventories/production.ini playbooks/02-deploy/basilica.yml
```

**Teardown**:

```bash
ansible-playbook -i inventories/production.ini playbooks/05-teardown/basilica.yml
```

## Important Notes

### Deployment Architecture

- **Operator**: Deployed in K3s cluster (manages GPU workloads, user deployments)
- **API**: Deployed via Terraform/ECS (see `scripts/cloud/compute.tf`)
- **Validator**: Deployed via Docker Compose (see `scripts/validator/compose.prod.yml`)
- **Telemetry**: Deployed via Ansible (see `telemetry/ansible/`)

### Optional Components

Controlled by variables in `orchestrator/ansible/group_vars/all/application.yml`:

- `install_envoy_forward_proxy` (default: true) - Forward proxy for user workloads
- `install_gateway_api` (default: true) - Kubernetes Gateway API resources

### Dynamic Routing

The Envoy ConfigMap in `networking/envoy/` contains the base configuration. For production user deployments, the api dynamically generates routing rules based on `UserDeployment` custom resources.

### Bootstrap Token RBAC

For GPU node onboarding via bootstrap tokens, the cluster requires specific RBAC permissions. These are defined in `core/rbac/bootstrap-token-rbac.yaml`:

**Required ClusterRoleBindings**:
- `kubeadm:kubelet-bootstrap` - Allows bootstrap tokens in the `system:bootstrappers:worker` group to authenticate
- `kubeadm:node-autoapprove-bootstrap` - Auto-approves CSRs for nodes joining via bootstrap tokens

**Apply manually if not already present**:
```bash
kubectl apply -f orchestrator/k8s/core/rbac/bootstrap-token-rbac.yaml
```

These bindings are required for the API's `/v1/gpu-nodes/register` endpoint to work correctly when onboarding datacenter GPU nodes.

## See Also

- [Ansible Playbooks](../ansible/playbooks/README.md)
