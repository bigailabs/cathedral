# UserDeployment Commands

UserDeployment lifecycle management, Gateway API routing, Envoy diagnostics, NetworkPolicy auditing, and namespace management.

## Overview

These command groups work together to manage and troubleshoot UserDeployments - the primary workload resource for user applications in Basilica. They cover the full stack from application deployment through network routing to security policies.

## Why These Commands Exist

UserDeployments involve multiple Kubernetes resources:
- The UserDeployment custom resource itself
- Pods running user workloads on GPU nodes
- Services exposing the pods
- HTTPRoutes for external access
- NetworkPolicies for security isolation
- Tenant namespaces (u-*) for multi-tenancy

Troubleshooting requires visibility across all these layers. These commands provide that visibility and automate common diagnostic tasks.

## Command Groups

### ud - UserDeployment Troubleshooting

Commands for inspecting and debugging UserDeployment resources.

#### ud inspect

**What it does:** Deep inspection of a single UserDeployment showing spec, status, conditions, and all related resources.

**How it works:**
1. Gets UserDeployment resource from K8s API
2. Gets related pods via label selector
3. Gets service, HTTPRoute, and NetworkPolicy
4. Gets recent events for the deployment and pods
5. Displays comprehensive status view

**When to use:** When a deployment is not working correctly, or to understand current state.

```bash
# Inspect a deployment (auto-detects namespace)
clustermgr ud inspect my-deployment

# Specify namespace explicitly
clustermgr ud inspect my-deployment -n u-alice
```

**Output:**
```
=== UserDeployment: u-alice/my-deployment ===

Specification
  State: Active
  User: alice
  Image: pytorch/pytorch:latest
  Replicas: 1/1
  Port: 8000
  Storage: Enabled (FUSE)
  GPUs: 1

Endpoints
  Public URL: https://my-deployment.deployments.basilica.ai
  Endpoint: 10.42.15.23:8000

=== Conditions ===
  [OK] Available - True - DeploymentAvailable
  [OK] Progressing - True - NewReplicaSetAvailable

=== Related Pods ===
| Pod                           | Phase   | Ready | Restarts | Node          | IP           |
|-------------------------------|---------|-------|----------|---------------|--------------|
| my-deployment-abc123-xyz      | Running | 1/1   | 0        | gpu-node-abc  | 10.42.15.23  |

=== Related Service ===
  Name: s-my-deployment
  Type: ClusterIP
  Ports: 8000:8000

=== Related HTTPRoute ===
  Name: ud-my-deployment
  Hostnames: my-deployment.deployments.basilica.ai
  Parent Gateway: basilica-system/basilica-gateway
  [OK] Accepted - True (Accepted)
  [OK] ResolvedRefs - True (ResolvedRefs)

=== NetworkPolicy ===
  Name: my-deployment-netpol
  Pod Selector: {app: my-deployment}
  Policy Types: ['Ingress', 'Egress']
```

---

#### ud logs

**What it does:** Streams logs from UserDeployment pods.

**How it works:**
1. Finds pods for the UserDeployment
2. Runs kubectl logs with specified options
3. Shows logs from main container or all containers

**When to use:** Debugging application issues, checking for errors.

```bash
# Show logs from main container
clustermgr ud logs my-deployment

# Follow logs in real-time
clustermgr ud logs my-deployment -f

# Show all containers (including FUSE sidecar)
clustermgr ud logs my-deployment -a

# Show specific container
clustermgr ud logs my-deployment -c fuse-storage

# Limit lines
clustermgr ud logs my-deployment -t 50
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-n, --namespace` | auto | Namespace |
| `-c, --container` | main | Container name |
| `-t, --tail` | 100 | Number of lines |
| `-f, --follow` | false | Follow output |
| `-a, --all-containers` | false | All containers |

---

#### ud events

**What it does:** Shows Kubernetes events for a UserDeployment and its pods.

**How it works:**
1. Gets events for the UserDeployment resource
2. Gets events for all related pods
3. Sorts by timestamp and displays

**When to use:** Understanding what's happening during deployment creation or failures.

```bash
clustermgr ud events my-deployment

# Limit events
clustermgr ud events my-deployment -l 10
```

---

#### ud restart

**What it does:** Restarts pods for a UserDeployment by deleting them.

**How it works:**
1. Finds all pods for the UserDeployment
2. Shows pods that will be deleted
3. Deletes pods with grace period
4. Deployment controller creates new pods

**When to use:** When pods are in a bad state, or after configuration changes.

```bash
clustermgr ud restart my-deployment
```

---

#### ud health

**What it does:** Checks health of all UserDeployments across the cluster.

**How it works:**
1. Gets all UserDeployments from K8s API
2. Checks state and replica readiness for each
3. Reports summary and identifies issues

**When to use:** Morning health checks, incident triage.

```bash
# Health check all deployments
clustermgr ud health

# Show only unhealthy
clustermgr ud health -u

# Filter by namespace
clustermgr ud health -n u-alice
```

**Output:**
```
=== UserDeployment Health Check ===
Total: 45 deployment(s)
  Healthy: 42
  Pending: 2
  Unhealthy: 1

=== Deployment Status ===
| Namespace | Name        | State    | Replicas | Storage | GPUs | URL                              |
|-----------|-------------|----------|----------|---------|------|----------------------------------|
| u-alice   | my-app      | Active   | 1/1      | FUSE    | 1    | https://my-app.deployments...    |
| u-bob     | ml-training | Pending  | 0/1      | FUSE    | 4    | -                                |

=== Issues Detected ===
  u-bob/ml-training: State: Pending, Replicas: 0/1
```

---

### gateway - Gateway API Troubleshooting

Commands for debugging the Envoy Gateway and HTTPRoute configuration.

#### gateway routes

**What it does:** Lists HTTPRoutes with their status and backend health.

**How it works:**
1. Gets the main basilica-gateway status
2. Gets all HTTPRoutes with acceptance status
3. Shows which routes are accepted/resolved

**When to use:** Troubleshooting routing issues, verifying route configuration.

```bash
# List all routes
clustermgr gateway routes

# Show only unhealthy routes
clustermgr gateway routes -u

# Filter by namespace
clustermgr gateway routes -n u-alice
```

---

#### gateway endpoints

**What it does:** Shows Envoy proxy pods and backend service endpoints.

**How it works:**
1. Gets Envoy Gateway proxy pods
2. Gets backend endpoints for each HTTPRoute
3. Shows endpoint readiness

**When to use:** Verifying Envoy can reach backends.

```bash
clustermgr gateway endpoints
```

---

#### gateway test

**What it does:** Tests connectivity through a specific route.

**How it works:**
1. Gets route configuration
2. Tests DNS resolution for hostname
3. Tests backend endpoint availability
4. Tests HTTP connectivity to public URL

**When to use:** End-to-end route verification.

```bash
# Test by route name
clustermgr gateway test ud-my-app -n u-alice

# Test by URL
clustermgr gateway test https://my-app.deployments.basilica.ai
```

---

#### gateway sync

**What it does:** Checks if HTTPRoutes are in sync with UserDeployments.

**How it works:**
1. Gets all HTTPRoutes and UserDeployments
2. Matches routes to deployments
3. Identifies orphaned routes and missing routes

**When to use:** Cleaning up after deleted deployments.

```bash
clustermgr gateway sync
```

---

### envoy - Envoy Proxy Diagnostics

Commands for diagnosing HTTP 503 errors by testing Envoy connectivity to user pods.

#### envoy pods

**What it does:** Shows Envoy Gateway proxy pod status and node placement.

**How it works:**
1. Gets pods with Envoy Gateway labels
2. Shows node placement and readiness
3. Shows distribution across nodes

**When to use:** Verifying Envoy is running correctly.

```bash
clustermgr envoy pods
```

**Output:**
```
=== Envoy Gateway Pods ===
| Pod                          | Node        | Node IP     | Pod IP      | Phase   | Ready | Restarts |
|------------------------------|-------------|-------------|-------------|---------|-------|----------|
| envoy-basilica-gateway-abc   | k3s-server-1| 10.0.1.10   | 10.42.0.15  | Running | 1/1   | 0        |

=== Node Distribution ===
  k3s-server-1: 1 pod(s) [server]
```

---

#### envoy test

**What it does:** Tests HTTP connectivity to user pods on GPU nodes.

**How it works:**
1. Gets user pods running on GPU nodes (WireGuard-connected)
2. Tests HTTP connectivity from K3s server via Flannel VXLAN
3. Reports success/failure for each pod

**When to use:** Diagnosing HTTP 503 errors.

```bash
# Test all GPU node pods
clustermgr envoy test

# Filter by namespace
clustermgr envoy test -n u-alice

# Limit pods tested
clustermgr envoy test -l 10
```

**Output:**
```
=== Flannel Network Connectivity Test ===
Found 1 Envoy pod(s) and 5 ready user pod(s)
Testing from K3s server via Flannel VXLAN overlay

Testing u-alice/my-app-abc123... HTTP 200 (45ms)
Testing u-bob/training-xyz789... FAILED - Connection timeout

=== Results Summary ===
| Target Pod               | IP:Port         | Node         | Status   | Latency |
|--------------------------|-----------------|--------------|----------|---------|
| u-alice/my-app-abc123    | 10.42.15.23:8000| gpu-node-abc | HTTP 200 | 45ms    |
| u-bob/training-xyz789    | 10.42.16.10:8000| gpu-node-def | FAILED   | -       |

1/2 tests passed

=== Troubleshooting ===
Failed connectivity may indicate:
  - Flannel VXLAN routing issues (run: clustermgr flannel diagnose)
  - Missing FDB/neighbor entries (run: clustermgr flannel fdb)
  - NetworkPolicy blocking traffic (run: clustermgr netpol test <ns>)
```

---

#### envoy logs

**What it does:** Shows Envoy access logs filtered by status code.

**How it works:**
1. Gets logs from Envoy pod
2. Parses access log format
3. Filters by status code prefix

**When to use:** Analyzing error patterns.

```bash
# Show all logs
clustermgr envoy logs

# Show only 5xx errors
clustermgr envoy logs --status 5

# Show only 503 errors
clustermgr envoy logs --status 503
```

---

#### envoy path

**What it does:** Traces the network path from Envoy to a specific user pod.

**How it works:**
1. Gets pod information
2. Shows Envoy source pod
3. Shows K8s service
4. Shows Flannel route
5. Shows node information
6. Tests connectivity

**When to use:** Deep debugging of specific pod connectivity.

```bash
clustermgr envoy path my-app-abc123 -n u-alice
```

---

### netpol - NetworkPolicy Diagnostics

Commands for auditing and testing NetworkPolicies in tenant namespaces.

#### netpol audit

**What it does:** Audits NetworkPolicies across tenant namespaces.

**How it works:**
1. Gets all tenant namespaces (u-*)
2. Checks for required policies in each
3. Reports compliance status

**When to use:** Security audit, compliance verification.

```bash
# Audit all namespaces
clustermgr netpol audit

# Audit specific namespace
clustermgr netpol audit -n u-alice

# Show fix commands
clustermgr netpol audit --fix
```

**Required policies:**
- `default-deny-all`: Block all traffic by default
- `allow-dns`: Allow DNS resolution
- `allow-internet-egress`: Allow outbound internet
- `allow-ingress-from-envoy`: Allow traffic from Envoy Gateway

**Output:**
```
=== NetworkPolicy Audit ===
Auditing 45 namespace(s)...

| Namespace | Policies | default-deny | allow-dns | allow-internet | allow-envoy |
|-----------|----------|--------------|-----------|----------------|-------------|
| u-alice   | 4        | Yes          | Yes       | Yes            | Yes         |
| u-bob     | 3        | Yes          | Yes       | No             | Yes         |

Compliant: 40, Non-compliant: 5
```

---

#### netpol test

**What it does:** Tests DNS, egress, and ingress connectivity for a namespace.

**How it works:**
1. Runs nslookup from a pod to test DNS
2. Tests internet egress with curl/wget
3. Checks ingress policy for Envoy

**When to use:** Verifying policies are working correctly.

```bash
clustermgr netpol test u-alice
```

---

#### netpol coverage

**What it does:** Shows NetworkPolicy coverage statistics.

**How it works:**
1. Gets all tenant namespaces
2. Counts required policies in each
3. Shows coverage summary

**When to use:** Quick coverage overview.

```bash
clustermgr netpol coverage
```

---

#### netpol details

**What it does:** Shows detailed NetworkPolicy configuration for a namespace.

**How it works:**
1. Gets all NetworkPolicies
2. Parses and displays rules
3. Shows ingress/egress details

**When to use:** Understanding exact policy configuration.

```bash
clustermgr netpol details u-alice
```

---

### namespace - Tenant Namespace Management

Commands for managing tenant namespaces (u-* prefixed).

#### namespace list

**What it does:** Lists all tenant namespaces with optional resource counts.

```bash
# Basic list
clustermgr namespace list

# With resource counts
clustermgr namespace list -d
```

---

#### namespace audit

**What it does:** Audits RBAC, NetworkPolicies, and Secrets for a namespace.

**How it works:**
1. Gets namespace information
2. Checks required labels
3. Checks RBAC (roles/bindings)
4. Checks NetworkPolicies
5. Checks required secrets
6. Checks ReferenceGrants

**When to use:** Verifying namespace is properly configured.

```bash
# Auto-prefixes with u- if needed
clustermgr namespace audit alice
```

**Output:**
```
=== Namespace Audit: u-alice ===

Resource Summary
  Created: 2024-11-01T10:30:00Z
  UserDeployments: 3
  Pods: 5
  Services: 3
  Secrets: 2
  NetworkPolicies: 4
  HTTPRoutes: 3

=== Labels ===
  pod-security.kubernetes.io/enforce: restricted [OK]
  basilica.ai/tenant: alice [OK]

=== RBAC ===
  user-workload-restricted: Present [OK]
  user-workload-restricted-binding: Present [OK]
  operator-elevated-binding: Present [OK]

=== NetworkPolicies ===
  default-deny-all: Present [OK]
  allow-dns: Present [OK]
  allow-internet-egress: Present [OK]
  allow-ingress-from-envoy: Present [OK]
```

---

#### namespace cleanup

**What it does:** Finds and cleans orphaned namespaces.

**How it works:**
1. Finds namespaces with no UserDeployments
2. Checks for remaining pods
3. Offers to delete empty namespaces

**When to use:** Regular maintenance cleanup.

```bash
clustermgr namespace cleanup
```

---

#### namespace resources

**What it does:** Shows all resources in a tenant namespace.

```bash
clustermgr namespace resources u-alice
```

---

#### namespace summary

**What it does:** Shows aggregate statistics for all tenant namespaces.

```bash
clustermgr namespace summary
```

---

## Troubleshooting Workflows

### UserDeployment Not Accessible (HTTP 503)

```bash
# 1. Check deployment status
clustermgr ud inspect my-app

# 2. Check pod connectivity
clustermgr envoy test -n u-alice

# 3. Check Flannel routing
clustermgr flannel diagnose

# 4. Check HTTPRoute status
clustermgr gateway routes -n u-alice

# 5. Check NetworkPolicy
clustermgr netpol test u-alice
```

### Deployment Stuck in Pending

```bash
# 1. Inspect deployment
clustermgr ud inspect my-app

# 2. Check events
clustermgr ud events my-app

# 3. Check cluster capacity
clustermgr scaling capacity

# 4. Check node availability
clustermgr maintenance status
```

### New Namespace Setup Verification

```bash
# 1. Audit namespace configuration
clustermgr namespace audit u-newuser

# 2. Check NetworkPolicy coverage
clustermgr netpol audit -n u-newuser

# 3. Test connectivity
clustermgr netpol test u-newuser
```

## Related Commands

- `health` - Overall cluster health
- `flannel diagnose` - Flannel overlay diagnostics
- `wg status` - WireGuard connectivity
- `fix` - Automated remediation

## Related Runbooks

- `docs/runbooks/HTTP-503-DIAGNOSIS.md` - HTTP 503 troubleshooting
- `docs/runbooks/FLANNEL-VXLAN-TROUBLESHOOTING.md` - Flannel issues
