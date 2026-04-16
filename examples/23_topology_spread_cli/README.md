# Topology Spread - CLI

Deploy pods across different nodes for high availability.

## Setup

```bash
export BASILICA_API_TOKEN="your-token"
```

## Deploy with Unique Nodes

```bash
cathedral deploy hashicorp/http-echo:latest \
  --name spread-demo \
  --port 5678 \
  --replicas 2 \
  --unique-nodes
```

Each pod runs on a different node, giving unique IP subnets.

## Other Spread Modes

```bash
# Best-effort spreading
cathedral deploy image --name app --replicas 4 --spread-mode preferred

# Strict spreading (won't schedule if can't spread)
cathedral deploy image --name app --replicas 4 --spread-mode required --max-skew 1

# Spread across zones
cathedral deploy image --name app --replicas 4 --spread-mode required \
  --topology-key topology.kubernetes.io/zone
```

## Cleanup

```bash
cathedral deployments delete spread-demo
```
