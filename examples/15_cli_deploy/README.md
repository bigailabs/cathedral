# CLI Deploy Guide

Deploy applications to Cathedral using the command line.

## Files

| File | Description |
|------|-------------|
| `hello.py` | Simple HTTP server |
| `my_api.py` | FastAPI application |
| `inference.py` | GPU inference server |

## Setup

```bash
pip install cathedral-cli
cathedral login
```

Or use an API token:
```bash
export BASILICA_API_TOKEN="cathedral_..."
```

## 1. Deploy Inline Code

The simplest way to deploy - pass Python code directly:

```bash
cathedral deploy 'from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hello from Cathedral!")

HTTPServer(("", 8000), Handler).serve_forever()' \
  --name hello \
  --port 8000 \
  --ttl 300
```

Check status:
```bash
cathedral deploy status hello
```

## 2. Deploy a Python File

For larger applications, deploy from a file. See [my_api.py](my_api.py):

```bash
cathedral deploy my_api.py \
  --name my-api \
  --port 8000 \
  --pip fastapi uvicorn \
  --ttl 600
```

View the API docs:
```bash
cathedral deploy status my-api --show-phases
# Then open {url}/docs in browser
```

## 3. Deploy a Docker Image

Deploy pre-built container images:

```bash
cathedral deploy nginxinc/nginx-unprivileged:alpine \
  --name nginx-demo \
  --port 8080 \
  --cpu 250m \
  --memory 256Mi \
  --ttl 300
```

## 4. Deploy with GPU

For ML workloads requiring GPU. See [inference.py](inference.py):

```bash
cathedral deploy inference.py \
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
cathedral deploy hello.py \
  --name stateful-app \
  --storage \
  --storage-path /data
```

Storage is backed by object storage (R2) and syncs automatically.

## 6. Resource Configuration

Control CPU and memory allocation:

```bash
cathedral deploy my_api.py \
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
cathedral deploy my_api.py \
  --name my-app \
  --health-path /health \
  --health-initial-delay 10 \
  --health-period 30 \
  --pip fastapi uvicorn
```

## Management Commands

### List Deployments

```bash
cathedral deploy ls
cathedral deploy ls --json
```

### Get Status

```bash
cathedral deploy status my-app
cathedral deploy status my-app --show-phases
cathedral deploy status my-app --json
```

### View Logs

```bash
cathedral deploy logs my-app
cathedral deploy logs my-app --follow
cathedral deploy logs my-app --tail 100
```

### Scale Replicas

```bash
cathedral deploy scale my-app --replicas 3
```

### Delete Deployment

```bash
cathedral deploy delete my-app
cathedral deploy delete my-app --yes  # skip confirmation
```

## Quick Reference

| Use Case | Command |
|----------|---------|
| Inline code | `cathedral deploy 'print("hello")' --name hello` |
| Python file | `cathedral deploy my_api.py --name my-app --pip fastapi uvicorn` |
| Docker image | `cathedral deploy nginx:alpine --name nginx --port 80` |
| With GPU | `cathedral deploy inference.py --name ml --gpu 1 --gpu-model A100` |
| With storage | `cathedral deploy hello.py --name db --storage` |

## Help

```bash
cathedral deploy --help
```
