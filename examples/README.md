# Basilica Examples

Production-ready examples demonstrating how to deploy containerized applications on the Basilica platform.

## Prerequisites

1. **API Token**: Generate a Basilica API token

   ```bash
   basilica tokens create my-token
   export BASILICA_API_TOKEN="basilica_..."
   ```

2. **Python SDK** (for Python examples)

   ```bash
   pip install basilica-sdk requests
   ```

3. **curl and jq** (for shell examples)

   ```bash
   # macOS
   brew install curl jq

   # Ubuntu/Debian
   apt-get install curl jq
   ```

## Examples

### 1. Tweet-worthy: Persistent Counter in 25 Lines

**File**: `simple_deploy.py`

Deploy a counter app with persistent storage and public URL - the simplest possible example.

```bash
python3 simple_deploy.py
# Output: Live at: https://xxx.deployments.basilica.ai
# Visit the URL - counter increments and persists!
```

### 2. Quick Start - Complete Lifecycle

**File**: `quickstart_complete.py`

A minimal example showing the complete lifecycle: create, wait, test, cleanup.

```bash
python3 quickstart_complete.py
```

### 3. Container Deployment with SDK

**File**: `deploy_container.py`

Demonstrates SDK-based deployment with replicas, status monitoring, and cleanup.

```bash
python3 deploy_container.py
```

**Features**:

- Create nginx deployment with 2 replicas
- Monitor replica readiness
- Create Python HTTP server deployment
- List all deployments
- Clean up resources

### 4. Public Deployment with Storage

**File**: `public_storage_deployment.py`

Deploy a FastAPI application with public URL and persistent FUSE storage.

```bash
python3 public_storage_deployment.py
```

**Features**:

- Automatic public HTTPS URL
- Persistent storage at `/data`
- Storage read/write/list operations
- Background sync to object storage

### 5. Shell/curl Deployment

**File**: `curl_deployment.sh`

Direct API usage with curl for scripting and automation.

```bash
./curl_deployment.sh
```

**Features**:

- Raw REST API calls
- JSON payload construction
- Status polling
- Storage operations

### 6. GPU Deployment with PyTorch

**File**: `gpu_deployment.py`

Deploy GPU-accelerated PyTorch workloads.

```bash
# Default: RTX A4000
python3 gpu_deployment.py

# Custom GPU requirements
export GPU_MODEL="NVIDIA-RTX-A4000"
export GPU_COUNT="1"
export MIN_VRAM_GB="12"
python3 gpu_deployment.py
```

**Features**:

- GPU selection (RTX A4000 available)
- CUDA/cuDNN detection
- Matrix multiplication benchmarks
- Model checkpoint save/load

**Available GPU**:

| Model | VRAM | CUDA |
|-------|------|------|
| NVIDIA RTX A4000 | 14GB | 12.8 |

## API Reference

### Create Deployment

```bash
POST /deployments
```

```json
{
  "instance_name": "my-app",
  "image": "python:3.11-slim",
  "replicas": 1,
  "port": 8000,
  "command": ["python", "-m", "http.server", "8000"],
  "cpu": "500m",
  "memory": "512Mi",
  "ttl_seconds": 3600,
  "public": true,
  "storage": "/data"
}
```

### GPU Deployment

```json
{
  "instance_name": "gpu-app",
  "image": "pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
  "resources": {
    "cpu": "2",
    "memory": "8Gi",
    "gpus": {
      "count": 1,
      "model": ["NVIDIA-RTX-A4000"],
      "min_gpu_memory_gb": 12,
      "min_cuda_version": "12.0"
    }
  }
}
```

### Get Status

```bash
GET /deployments/{instance_name}
```

### Delete

```bash
DELETE /deployments/{instance_name}
```

## Storage

Storage is mounted as a FUSE filesystem backed by object storage (R2/S3).

**Features**:

- POSIX file API (`open`, `read`, `write`)
- Background sync (1s interval)
- Data persists across restarts
- Per-user isolation

**Usage**:

```python
# SDK - simple path
client.create_deployment(..., storage="/data")

# API - full spec
"storage": {
    "persistent": {
        "enabled": true,
        "backend": "r2",
        "mountPath": "/data"
    }
}
```

## Troubleshooting

### Deployment Stuck in Pending

1. Check image name and registry access
2. Reduce CPU/memory if cluster is constrained
3. For GPU: verify node availability

### Storage Not Accessible

1. Wait for storage initialization (30-60s after deployment ready)
2. Check for `.fuse_ready` marker file in mount path

### Public URL Returns 502/503

1. Wait for HTTP server to start (15-30s after ready)
2. Verify port matches deployment config
3. Check application logs

### GPU Not Detected

1. Use CUDA-enabled image (`pytorch/pytorch:*-cuda*`)
2. Verify GPU availability via API
3. Check container logs for CUDA errors

## Resources

- [Architecture Documentation](../docs/architecture/)
- [Python SDK](../crates/basilica-sdk-python/)
- [Rust SDK](../crates/basilica-sdk/)
