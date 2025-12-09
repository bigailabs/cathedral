# CLI Deploy Guide

Deploy applications to Basilica using the command line.

## Files

| File | Description |
|------|-------------|
| `hello.py` | Simple HTTP server |
| `my_api.py` | FastAPI application |
| `inference.py` | GPU inference server |

## Setup

```bash
pip install basilica-cli
basilica login
```

Or use an API token:
```bash
export BASILICA_API_TOKEN="basilica_..."
```

## 1. Deploy Inline Code

The simplest way to deploy - pass Python code directly:

```bash
basilica deploy 'from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from Basilica!")

HTTPServer(("", 8000), Handler).serve_forever()' \
  --name hello \
  --port 8000 \
  --ttl 300
```

Check status:
```bash
basilica deploy status hello
```

## 2. Deploy a Python File

For larger applications, deploy from a file. See [my_api.py](my_api.py):

```bash
basilica deploy my_api.py \
  --name my-api \
  --port 8000 \
  --pip fastapi uvicorn \
  --ttl 600
```

View the API docs:
```bash
basilica deploy status my-api --show-phases
# Then open {url}/docs in browser
```

## 3. Deploy a Docker Image

Deploy pre-built container images:

```bash
basilica deploy nginxinc/nginx-unprivileged:alpine \
  --name nginx-demo \
  --port 8080 \
  --cpu 250m \
  --memory 256Mi \
  --ttl 300
```

## 4. Deploy with GPU

For ML workloads requiring GPU. See [inference.py](inference.py):

```bash
basilica deploy inference.py \
  --name gpu-model \
  --gpu 1 \
  --gpu-model H100 \
  --memory 32Gi \
  --pip torch
```

Available GPU models: `H100`, `A100`, `L40S`, `RTX4090`, `RTX-A4000`

## 5. Deploy with Persistent Storage

Data persists across restarts at the specified path:

```bash
basilica deploy hello.py \
  --name stateful-app \
  --storage \
  --storage-path /data
```

Storage is backed by object storage (R2) and syncs automatically.

## 6. Resource Configuration

Control CPU and memory allocation:

```bash
basilica deploy my_api.py \
  --name my-app \
  --cpu 500m \
  --memory 1Gi \
  --cpu-request 250m \
  --memory-request 512Mi \
  --pip fastapi uvicorn
```

## 7. Health Checks

Configure custom health check endpoints:

```bash
basilica deploy my_api.py \
  --name my-app \
  --health-path /health \
  --health-initial-delay 10 \
  --health-period 30 \
  --pip fastapi uvicorn
```

## Management Commands

### List Deployments

```bash
basilica deploy ls
basilica deploy ls --json
```

### Get Status

```bash
basilica deploy status my-app
basilica deploy status my-app --show-phases
basilica deploy status my-app --json
```

### View Logs

```bash
basilica deploy logs my-app
basilica deploy logs my-app --follow
basilica deploy logs my-app --tail 100
```

### Scale Replicas

```bash
basilica deploy scale my-app --replicas 3
```

### Delete Deployment

```bash
basilica deploy delete my-app
basilica deploy delete my-app --yes  # skip confirmation
```

## Quick Reference

| Use Case | Command |
|----------|---------|
| Inline code | `basilica deploy 'print("hello")' --name hello` |
| Python file | `basilica deploy my_api.py --name my-app --pip fastapi uvicorn` |
| Docker image | `basilica deploy nginx:alpine --name nginx --port 80` |
| With GPU | `basilica deploy inference.py --name ml --gpu 1 --gpu-model A100` |
| With storage | `basilica deploy hello.py --name db --storage` |

## Help

```bash
basilica deploy --help
```
