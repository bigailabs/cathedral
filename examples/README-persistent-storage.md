# Basilica Persistent Storage Examples

This directory contains example manifests for using Basilica's FUSE-based persistent storage layer.

## Overview

Basilica's persistent storage provides:
- **Zero code changes**: Your app writes to `/data`, data automatically syncs to R2/S3
- **Crash resilience**: Data synced every 1 second (configurable)
- **Fast resume**: Lazy loading on job restart
- **Transparent mmap**: Works with PyTorch, TensorFlow, numpy

## Quick Start

### 1. Create Storage Credentials Secret

For R2:
```bash
kubectl create secret generic r2-credentials \
  --from-literal=STORAGE_ACCESS_KEY_ID=your-key \
  --from-literal=STORAGE_SECRET_ACCESS_KEY=your-secret
```

For S3:
```bash
kubectl create secret generic aws-credentials \
  --from-literal=STORAGE_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE \
  --from-literal=STORAGE_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

### 2. Submit a Job with Persistent Storage

```bash
kubectl apply -f persistent-storage-job.yaml
```

### 3. Verify Storage Daemon

Check that the storage sidecar is running:
```bash
kubectl get pods -l basilica.ai/job=ml-training-persistent
kubectl logs <pod-name> -c basilica-storage-ml-training-persistent
```

## Configuration Options

### Storage Spec Fields

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `enabled` | bool | Yes | false | Enable persistent storage |
| `backend` | string | Yes | - | Storage backend: `r2`, `s3`, `gcs` |
| `bucket` | string | Yes | - | Bucket name |
| `region` | string | No | `us-east-1` | AWS region (S3 only) |
| `endpoint` | string | No | - | Custom endpoint (required for R2) |
| `credentialsSecret` | string | Yes | - | K8s Secret with credentials |
| `syncIntervalMs` | int | No | 1000 | Background sync interval (ms) |
| `cacheSizeMb` | int | No | 2048 | In-memory cache size (MB) |
| `mountPath` | string | No | `/data` | Mount point in container |

### Credentials Secret Format

The secret must contain these keys:
- `STORAGE_ACCESS_KEY_ID`: Access key ID
- `STORAGE_SECRET_ACCESS_KEY`: Secret access key

Example:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: storage-creds
type: Opaque
stringData:
  STORAGE_ACCESS_KEY_ID: "your-access-key"
  STORAGE_SECRET_ACCESS_KEY: "your-secret-key"
```

## How It Works

### Architecture

```
┌─────────────────────────────────────┐
│         Your Container              │
│                                     │
│  Your app writes to /data/model.pt │
│         ↓                          │
│    [FUSE Filesystem]               │
│         ↓                          │
│    [Page Cache (2GB)]              │
│         ↓                          │
│  [Background Sync Thread]          │
└──────────┬──────────────────────────┘
           │
           ↓ Every 1s
   ┌───────────────┐
   │  R2/S3 Bucket │
   └───────────────┘
```

### Data Flow

1. **Write**: Your app writes to `/data/checkpoint.pt`
2. **Cache**: Data stored in fast in-memory page cache (nanosecond latency)
3. **Mark Dirty**: Region marked for background sync
4. **Return**: Write returns immediately to your app
5. **Background Sync**: Worker syncs dirty pages to R2/S3 every 1 second
6. **Crash Protection**: Even if node crashes, max 1 second of data lost

### Lazy Loading

When a job resumes on a different node:
1. FUSE filesystem mounts immediately
2. Files loaded on-demand as accessed
3. Only needed pages fetched from R2/S3
4. Subsequent reads served from cache

## Performance

### Write Performance
- **Cache Write**: ~10ns (in-memory)
- **Background Sync**: 1s latency (async, non-blocking)
- **User Impact**: Zero blocking - writes return immediately

### Read Performance
- **Cache Hit**: ~10ns (in-memory)
- **Cache Miss**: ~50-100ms (fetch from R2/S3)
- **Lazy Loading**: Only fetch what you need

### Memory Usage
- **Default Cache**: 2GB (configurable)
- **Page Size**: 64KB
- **LRU Eviction**: Automatic when cache fills

## Best Practices

### 1. Cache Size Tuning

For large models, increase cache:
```yaml
storage:
  persistent:
    cacheSizeMb: 8192  # 8GB for large language models
```

### 2. Sync Interval Tuning

For critical data, sync more frequently:
```yaml
storage:
  persistent:
    syncIntervalMs: 500  # Sync every 0.5 seconds
```

For less critical data, sync less often:
```yaml
storage:
  persistent:
    syncIntervalMs: 5000  # Sync every 5 seconds (saves network)
```

### 3. Directory Structure

Organize your data:
```
/data/
  ├── checkpoints/     # Model checkpoints
  ├── logs/            # Training logs
  └── artifacts/       # Output artifacts
```

### 4. Manual Flush

For critical saves, call `fsync()`:
```python
with open('/data/checkpoint.pt', 'wb') as f:
    torch.save(model.state_dict(), f)
    f.flush()
    os.fsync(f.fileno())  # Force immediate sync to R2/S3
```

## Troubleshooting

### Check Sidecar Logs
```bash
kubectl logs <pod-name> -c basilica-storage-<job-name>
```

### Verify Mount
```bash
kubectl exec <pod-name> -- df -h | grep /data
kubectl exec <pod-name> -- mount | grep /data
```

### Check Sync Status
Look for log messages like:
```
INFO Syncing 3 dirty regions
INFO Successfully synced: /data/checkpoint.pt @ 0
```

### Common Issues

**Problem**: "Failed to mount filesystem: permission denied"
- **Solution**: Ensure pod has `privileged: true` security context

**Problem**: "Failed to create storage backend: invalid credentials"
- **Solution**: Check that secret exists and contains correct keys

**Problem**: "Slow writes"
- **Solution**: This is expected on first write to a region. Subsequent writes to same region are cached.

## Examples

See the example manifests:
- `persistent-storage-job.yaml` - Basic R2 configuration
- `persistent-storage-s3.yaml` - S3 configuration with multi-GPU

## Additional Resources

- [FUSE Implementation Status](../docs/fuse-implementation-status.md)
- [Storage Daemon Source](../crates/basilica-storage)
- [Operator Integration](../crates/basilica-operator)
