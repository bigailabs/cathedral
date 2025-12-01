# Clustermgr Command Documentation

This directory contains detailed documentation for each command group in the clustermgr CLI tool.

## Command Categories

### Core Operations
- [health.md](health.md) - Multi-layer cluster health checks (health, diagnose, fix)

### Network - WireGuard
- [wg.md](wg.md) - WireGuard VPN management and monitoring

### Network - Flannel VXLAN
- [flannel.md](flannel.md) - Flannel overlay network diagnostics

### Network - General
- [network.md](network.md) - Topology, firewall, MTU, mesh testing, latency

### Maintenance
- [maintenance.md](maintenance.md) - Node maintenance operations
- [scaling.md](scaling.md) - Cluster capacity and scaling
- [etcd.md](etcd.md) - etcd cluster management

### UserDeployment Management
- [userdeployment.md](userdeployment.md) - UserDeployment, Gateway, Envoy, NetworkPolicy, Namespace

### Troubleshooting
- [troubleshooting.md](troubleshooting.md) - Pod, FUSE, and node pressure diagnostics

### Operations
- [bundle.md](bundle.md) - Diagnostic bundle collection

## Quick Reference

| Command | Purpose | Documentation |
|---------|---------|---------------|
| `health` | Quick cluster health check | [health.md](health.md) |
| `diagnose` | Deep diagnostics for incidents | [health.md](health.md) |
| `fix` | Auto-fix common issues | [health.md](health.md) |
| `wg status` | WireGuard connectivity status | [wg.md](wg.md) |
| `wg reconcile` | Fix WireGuard peer configuration | [wg.md](wg.md) |
| `flannel diagnose` | Flannel VXLAN health | [flannel.md](flannel.md) |
| `flannel mac-duplicates` | Detect duplicate VtepMACs | [flannel.md](flannel.md) |
| `topology` | Cluster network topology | [network.md](network.md) |
| `mesh-test` | Test WireGuard connectivity | [network.md](network.md) |
| `maintenance status` | Node schedulability | [maintenance.md](maintenance.md) |
| `maintenance drain` | Drain node for maintenance | [maintenance.md](maintenance.md) |
| `scaling capacity` | Cluster resource utilization | [scaling.md](scaling.md) |
| `etcd health` | etcd cluster health | [etcd.md](etcd.md) |
| `ud inspect` | UserDeployment details | [userdeployment.md](userdeployment.md) |
| `envoy test` | Test Envoy to pod connectivity | [userdeployment.md](userdeployment.md) |
| `netpol audit` | Audit NetworkPolicies | [userdeployment.md](userdeployment.md) |
| `pod-troubleshoot` | Deep pod diagnostics | [troubleshooting.md](troubleshooting.md) |
| `fuse-troubleshoot` | FUSE storage diagnostics | [troubleshooting.md](troubleshooting.md) |
| `node-pressure` | Node pressure conditions | [troubleshooting.md](troubleshooting.md) |
| `bundle` | Collect diagnostic data | [bundle.md](bundle.md) |

## Common Workflows

### Incident Response
```bash
clustermgr health           # Quick assessment
clustermgr diagnose         # Deep dive
clustermgr fix --dry-run    # Preview fixes
clustermgr fix              # Apply fixes
```

### HTTP 503 Troubleshooting
```bash
clustermgr flannel diagnose
clustermgr flannel mac-duplicates
clustermgr envoy test
clustermgr fix
```

### Scheduled Maintenance
```bash
clustermgr maintenance status
clustermgr maintenance cordon <node>
clustermgr maintenance drain <node>
# ... perform maintenance ...
clustermgr maintenance uncordon <node>
clustermgr maintenance verify
```

### UserDeployment Debugging
```bash
clustermgr ud inspect <deployment>
clustermgr envoy test -n <namespace>
clustermgr netpol test <namespace>
clustermgr flannel diagnose
```

### Escalation
```bash
clustermgr bundle -o /tmp
# Attach bundle tarball to ticket
```
