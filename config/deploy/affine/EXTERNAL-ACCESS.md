# External Access to Affine Service

The Affine evaluation service is exposed to the internet via the Envoy proxy LoadBalancer.

## Public URLs

**Base URL:** `http://3.21.154.119:8080/affine/`

### Endpoints

1. **Health Check**
   ```bash
   curl http://3.21.154.119:8080/affine/health
   ```
   Response:
   ```json
   {"status":"healthy","service":"affine-evaluation","version":"1.0.0"}
   ```

2. **List Available Methods**
   ```bash
   curl http://3.21.154.119:8080/affine/methods
   ```
   Response:
   ```json
   {"methods":["evaluate"],"tasks":["sat","abd","ded"]}
   ```

3. **Run Evaluation**
   ```bash
   curl -X POST http://3.21.154.119:8080/affine/evaluate \
     -H "Content-Type: application/json" \
     -d '{
       "task_type": "sat",
       "model": "deepseek-ai/DeepSeek-V3",
       "base_url": "https://llm.chutes.ai/v1",
       "num_samples": 1,
       "timeout": 600,
       "temperature": 0.7
     }'
   ```

## Architecture

### Request Flow

```
Internet
  │
  ▼ HTTP Request to :8080/affine/*
┌─────────────────────────────────────┐
│  Envoy Proxy LoadBalancer           │
│  Public IP: 3.21.154.119            │
│  Service: basilica-envoy            │
│  Namespace: basilica-system         │
└─────────────────────────────────────┘
  │
  │ Path rewrite: /affine/* → /*
  │ Forward to affine-service:8000
  ▼
┌─────────────────────────────────────┐
│  Affine Service (ClusterIP)         │
│  Internal: affine-service.affine-   │
│            system.svc.cluster.local │
│  Port: 8000                         │
│  Replicas: 2 (load balanced)        │
└─────────────────────────────────────┘
  │
  ▼ Handle request
┌─────────────────────────────────────┐
│  Affine Pods                        │
│  - FastAPI server                   │
│  - Actor class                      │
│  - Task implementations             │
└─────────────────────────────────────┘
```

### Envoy Configuration

The routing is configured in the Envoy ConfigMap:

**File:** `/root/workspace/spacejar/basilica/basilica/config/deploy/ingress/envoy-configmap-with-routing.yaml`

**Key Configuration:**
```yaml
routes:
  # Route /affine/ to affine-service
  - match:
      prefix: "/affine/"
    route:
      cluster: affine_service
      prefix_rewrite: "/"      # Remove /affine prefix
      timeout: 300s

clusters:
  - name: affine_service
    type: STRICT_DNS
    load_assignment:
      endpoints:
        - lb_endpoints:
            - endpoint:
                address:
                  socket_address:
                    address: affine-service.affine-system.svc.cluster.local
                    port_value: 8000
```

## Security Considerations

### Current Setup
- ✅ HTTP only (no TLS encryption)
- ✅ ClusterIP service (not directly exposed)
- ✅ Routed through Envoy proxy
- ✅ Health checks enabled
- ✅ Resource limits enforced
- ✅ API key required (from Kubernetes Secret)

### Recommended Improvements
1. **Add TLS/HTTPS**
   - Use cert-manager with Let's Encrypt
   - Configure HTTPS listener on Envoy
   - Redirect HTTP to HTTPS

2. **Add Authentication**
   - API key authentication at Envoy level
   - Rate limiting per client
   - IP allowlisting

3. **Add Monitoring**
   - Prometheus metrics from Envoy
   - Request logging and tracing
   - Alert on high error rates

4. **Add WAF/Security Headers**
   - CORS configuration
   - Security headers (HSTS, CSP, etc.)
   - Request size limits

## Maintenance

### Update Routing Configuration

1. Edit ConfigMap:
   ```bash
   kubectl edit configmap basilica-envoy-config -n basilica-system
   ```

2. Restart Envoy to apply changes:
   ```bash
   kubectl rollout restart deployment/basilica-envoy -n basilica-system
   ```

3. Verify routing:
   ```bash
   curl http://3.21.154.119:8080/affine/health
   ```

### View Envoy Admin Dashboard

Envoy admin interface is available on port 9901:
```bash
# Port forward (for security, not exposed externally)
kubectl port-forward -n basilica-system svc/basilica-envoy 9901:9901

# Access at http://localhost:9901
# Useful endpoints:
# - /stats - Metrics
# - /config_dump - Current configuration
# - /clusters - Backend cluster status
```

### Monitor Traffic

Check Envoy logs:
```bash
kubectl logs -n basilica-system -l app=basilica-envoy --tail=100 -f
```

Check Affine service logs:
```bash
kubectl logs -n affine-system -l app=affine-service --tail=100 -f
```

## Troubleshooting

### 502 Bad Gateway
- **Cause**: Affine service is down or not ready
- **Fix**: Check pod status: `kubectl get pods -n affine-system`
- **Fix**: Check logs: `kubectl logs -n affine-system -l app=affine-service`

### 404 Not Found
- **Cause**: Path doesn't match routing rules
- **Fix**: Ensure URL includes `/affine/` prefix
- **Fix**: Check Envoy config: `kubectl get configmap basilica-envoy-config -n basilica-system -o yaml`

### Connection Timeout
- **Cause**: Network connectivity issue or security group blocking port 8080
- **Fix**: Check security group allows inbound traffic on port 8080
- **Fix**: Verify LoadBalancer IP: `kubectl get svc basilica-envoy -n basilica-system`

### 503 Service Unavailable
- **Cause**: No healthy upstream endpoints
- **Fix**: Check Affine pods are running: `kubectl get pods -n affine-system`
- **Fix**: Check health checks: `kubectl exec -n affine-system deployment/affine-service -- curl localhost:8000/health`

## Network Configuration

### AWS Security Group

Ensure the security group for the K3s server allows inbound traffic:

**Required Rules:**
- **Type**: Custom TCP
- **Protocol**: TCP
- **Port Range**: 8080
- **Source**: 0.0.0.0/0 (or restrict to specific IPs)
- **Description**: Envoy proxy HTTP traffic

### LoadBalancer Details

```bash
kubectl get svc basilica-envoy -n basilica-system
```

Output:
```
NAME             TYPE           CLUSTER-IP      EXTERNAL-IP     PORT(S)
basilica-envoy   LoadBalancer   10.43.102.220   172.31.18.204   8080:32500/TCP,9901:30702/TCP
```

- **Internal IP**: 172.31.18.204
- **Public IP**: 3.21.154.119 (EC2 instance public IP)
- **Port Mapping**: 8080 (external) → 8080 (container)

## Testing from Different Locations

### From Internet
```bash
curl http://3.21.154.119:8080/affine/health
```

### From K3s Cluster
```bash
kubectl run test-pod --rm -i --tty --image=curlimages/curl -- sh
curl http://basilica-envoy.basilica-system:8080/affine/health
```

### From Another Namespace
```bash
kubectl run test-pod --rm -i --tty --image=curlimages/curl -n default -- sh
curl http://affine-service.affine-system:8000/health
```

## Performance

### Load Balancing
- **Envoy**: Round-robin load balancing across Affine pods
- **Affine**: 2 replicas for high availability
- **Health Checks**: Envoy checks `/health` every 10s

### Capacity
- **Concurrent Connections**: Limited by Envoy resources (CPU/memory)
- **Request Timeout**: 300 seconds (5 minutes) for evaluation tasks
- **Pod Resources**: 250m CPU, 512Mi memory (request) per pod

### Scaling

Increase Affine replicas:
```bash
kubectl scale deployment affine-service -n affine-system --replicas=4
```

Increase Envoy replicas:
```bash
kubectl scale deployment basilica-envoy -n basilica-system --replicas=2
```

## Documentation References

- [Affine Service README](../services/affine/README.md)
- [Envoy Proxy Documentation](https://www.envoyproxy.io/docs)
- [K3s LoadBalancer](https://docs.k3s.io/networking#service-load-balancer)
