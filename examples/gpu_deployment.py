#!/usr/bin/env python3
"""
Example: Deploy GPU-accelerated PyTorch workload on Basilica

This example demonstrates:
1. Creating a GPU deployment with PyTorch
2. Running matrix multiplication benchmarks on GPU
3. Saving/loading model checkpoints to persistent storage

Available GPU nodes:
- NVIDIA RTX A4000 (14GB VRAM, CUDA 12.8)

Prerequisites:
- BASILICA_API_TOKEN environment variable
- requests installed: pip install requests

Usage:
    export BASILICA_API_TOKEN="your-token-here"
    python3 gpu_deployment.py
"""

import os
import sys
import time
import requests
from typing import Optional, Dict, Any


PYTORCH_APP = '''
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pathlib import Path
import socket
from datetime import datetime

app = FastAPI(title="Basilica PyTorch GPU Demo")
STORAGE_PATH = Path("/data")

_torch = None
_device = None

def get_torch():
    global _torch, _device
    if _torch is None:
        import torch
        _torch = torch
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _torch, _device

class MatrixRequest(BaseModel):
    size: int = 1024
    iterations: int = 10

class ModelCheckpoint(BaseModel):
    name: str
    weights: list[list[float]]

@app.get("/")
def root():
    torch, device = get_torch()
    gpu_info = {}
    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        gpu_info = {
            "available": True,
            "count": torch.cuda.device_count(),
            "device_name": torch.cuda.get_device_name(0),
            "total_memory_gb": round(props.total_memory / 1024**3, 2),
            "compute_capability": f"{props.major}.{props.minor}",
        }
    else:
        gpu_info = {"available": False, "reason": "CUDA not available"}
    return {
        "service": "Basilica PyTorch GPU Demo",
        "hostname": socket.gethostname(),
        "device": str(device),
        "pytorch_version": torch.__version__,
        "cuda_version": torch.version.cuda if torch.cuda.is_available() else None,
        "gpu": gpu_info,
        "storage_mounted": STORAGE_PATH.exists(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/gpu/info")
def gpu_info():
    torch, device = get_torch()
    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA not available")
    devices = []
    for i in range(torch.cuda.device_count()):
        props = torch.cuda.get_device_properties(i)
        devices.append({
            "index": i,
            "name": torch.cuda.get_device_name(i),
            "compute_capability": f"{props.major}.{props.minor}",
            "total_memory_gb": round(props.total_memory / 1024**3, 2),
            "multi_processor_count": props.multi_processor_count,
        })
    return {
        "cuda_version": torch.version.cuda,
        "cudnn_version": torch.backends.cudnn.version(),
        "device_count": torch.cuda.device_count(),
        "devices": devices
    }

@app.post("/gpu/benchmark")
def gpu_benchmark(req: MatrixRequest):
    torch, device = get_torch()
    if not torch.cuda.is_available():
        raise HTTPException(status_code=503, detail="CUDA not available")
    size = min(max(req.size, 256), 8192)
    iterations = min(max(req.iterations, 1), 100)
    a = torch.randn(size, size, device=device)
    b = torch.randn(size, size, device=device)
    _ = torch.matmul(a, b)
    torch.cuda.synchronize()
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(iterations):
        c = torch.matmul(a, b)
    end.record()
    torch.cuda.synchronize()
    elapsed_ms = start.elapsed_time(end)
    flops_per_matmul = 2 * size * size * size
    total_flops = flops_per_matmul * iterations
    tflops = (total_flops / (elapsed_ms / 1000)) / 1e12
    return {
        "matrix_size": size,
        "iterations": iterations,
        "total_time_ms": round(elapsed_ms, 2),
        "avg_time_per_op_ms": round(elapsed_ms / iterations, 4),
        "estimated_tflops": round(tflops, 2),
        "device": torch.cuda.get_device_name(0)
    }

@app.post("/model/save")
def save_model(checkpoint: ModelCheckpoint):
    torch, device = get_torch()
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    weights_tensor = torch.tensor(checkpoint.weights, device=device)
    checkpoint_path = STORAGE_PATH / f"{checkpoint.name}.pt"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "name": checkpoint.name,
        "weights": weights_tensor,
        "shape": list(weights_tensor.shape),
        "device": str(device),
        "saved_at": datetime.utcnow().isoformat()
    }, checkpoint_path)
    return {
        "success": True,
        "path": str(checkpoint_path),
        "shape": list(weights_tensor.shape),
        "size_mb": round(checkpoint_path.stat().st_size / 1024**2, 4)
    }

@app.get("/model/load/{name}")
def load_model(name: str):
    torch, device = get_torch()
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    checkpoint_path = STORAGE_PATH / f"{name}.pt"
    if not checkpoint_path.exists():
        raise HTTPException(status_code=404, detail="Checkpoint not found")
    checkpoint = torch.load(checkpoint_path, map_location=device)
    return {
        "name": checkpoint["name"],
        "shape": checkpoint["shape"],
        "saved_at": checkpoint["saved_at"],
        "loaded_on": str(device)
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''


class BasilicaAPIClient:
    """Simple API client for GPU deployment operations."""

    def __init__(self, base_url: str, api_token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def create_gpu_deployment(
        self,
        instance_name: str,
        image: str,
        command: list,
        port: int = 8000,
        cpu: str = "2",
        memory: str = "8Gi",
        gpu_count: int = 1,
        gpu_models: list = None,
        min_gpu_memory_gb: int = None,
        min_cuda_version: str = None,
        ttl_seconds: int = None,
        storage: str = None
    ) -> Dict[str, Any]:
        payload = {
            "instance_name": instance_name,
            "image": image,
            "replicas": 1,
            "port": port,
            "command": command,
            "public": True,
            "resources": {
                "cpu": cpu,
                "memory": memory,
                "gpus": {
                    "count": gpu_count,
                    "model": gpu_models or ["NVIDIA-RTX-A4000"],
                }
            }
        }
        if min_gpu_memory_gb:
            payload["resources"]["gpus"]["min_gpu_memory_gb"] = min_gpu_memory_gb
        if min_cuda_version:
            payload["resources"]["gpus"]["min_cuda_version"] = min_cuda_version
        if ttl_seconds:
            payload["ttl_seconds"] = ttl_seconds
        if storage:
            payload["storage"] = {
                "persistent": {
                    "enabled": True,
                    "backend": "r2",
                    "bucket": "",
                    "syncIntervalMs": 1000,
                    "cacheSizeMb": 1024,
                    "mountPath": storage
                }
            }
        response = requests.post(
            f"{self.base_url}/deployments",
            headers=self.headers,
            json=payload,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def get_deployment(self, instance_name: str) -> Dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/deployments/{instance_name}",
            headers=self.headers,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def delete_deployment(self, instance_name: str) -> Dict[str, Any]:
        response = requests.delete(
            f"{self.base_url}/deployments/{instance_name}",
            headers=self.headers,
            timeout=30
        )
        response.raise_for_status()
        return response.json()


def wait_for_deployment(client: BasilicaAPIClient, instance_name: str, max_wait: int = 300) -> Optional[str]:
    print(f"\nWaiting for deployment '{instance_name}' to be ready...")
    elapsed = 0
    while elapsed < max_wait:
        try:
            status = client.get_deployment(instance_name)
            replicas = status.get("replicas", {})
            ready = replicas.get("ready", 0)
            desired = replicas.get("desired", 1)
            state = status.get("state", "Unknown")
            url = status.get("url", "")
            print(f"  [{elapsed}s] State={state}, Ready={ready}/{desired}")
            if state in ("Active", "Running") and ready == desired and ready > 0:
                print("\nDeployment is ready!")
                return url
        except Exception as e:
            print(f"  [{elapsed}s] Error: {e}")
        time.sleep(10)
        elapsed += 10
    print(f"\nWarning: Deployment not ready after {max_wait}s")
    return None


def test_gpu_endpoints(url: str) -> dict:
    results = {"health": False, "root": False, "gpu_info": False, "benchmark": False}

    print(f"\n  GET {url}/health")
    try:
        r = requests.get(f"{url}/health", timeout=10)
        if r.status_code == 200:
            print(f"    OK: {r.json()}")
            results["health"] = True
    except Exception as e:
        print(f"    Error: {e}")

    print(f"\n  GET {url}/")
    try:
        r = requests.get(f"{url}/", timeout=30)
        if r.status_code == 200:
            data = r.json()
            gpu = data.get('gpu', {})
            print(f"    PyTorch: {data.get('pytorch_version')}")
            print(f"    CUDA: {data.get('cuda_version')}")
            print(f"    GPU: {gpu.get('device_name')} ({gpu.get('total_memory_gb')} GB)")
            results["root"] = gpu.get('available', False)
    except Exception as e:
        print(f"    Error: {e}")

    print(f"\n  GET {url}/gpu/info")
    try:
        r = requests.get(f"{url}/gpu/info", timeout=30)
        if r.status_code == 200:
            data = r.json()
            for d in data.get('devices', []):
                print(f"    Device {d['index']}: {d['name']} ({d['total_memory_gb']} GB)")
            results["gpu_info"] = True
    except Exception as e:
        print(f"    Error: {e}")

    print(f"\n  POST {url}/gpu/benchmark")
    try:
        r = requests.post(f"{url}/gpu/benchmark", json={"size": 1024, "iterations": 10}, timeout=60)
        if r.status_code == 200:
            data = r.json()
            print(f"    Matrix: {data['matrix_size']}x{data['matrix_size']}")
            print(f"    Time: {data['total_time_ms']}ms ({data['avg_time_per_op_ms']}ms/op)")
            print(f"    TFLOPS: {data['estimated_tflops']}")
            results["benchmark"] = True
    except Exception as e:
        print(f"    Error: {e}")

    return results


def main():
    api_token = os.getenv("BASILICA_API_TOKEN")
    if not api_token:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        sys.exit(1)

    api_url = os.getenv("BASILICA_API_URL", "https://api.basilica.ai")

    # Default to RTX A4000 which is available in the cluster
    gpu_model = os.getenv("GPU_MODEL", "NVIDIA-RTX-A4000")
    gpu_count = int(os.getenv("GPU_COUNT", "1"))
    min_vram_gb = int(os.getenv("MIN_VRAM_GB", "12"))
    cuda_version = os.getenv("CUDA_VERSION", "12.0")

    instance_name = f"gpu-pytorch-{int(time.time())}"

    print("=" * 60)
    print("Basilica GPU Deployment Example")
    print("=" * 60)
    print(f"\nGPU Requirements:")
    print(f"  Model: {gpu_model}")
    print(f"  Count: {gpu_count}")
    print(f"  Min VRAM: {min_vram_gb} GB")
    print(f"  CUDA: >= {cuda_version}")

    client = BasilicaAPIClient(base_url=api_url, api_token=api_token)
    actual_instance = None

    try:
        print("\n1. Creating GPU deployment...")
        command = [
            "bash", "-c",
            f"pip install -q fastapi uvicorn pydantic && python - <<'PYCODE'\n{PYTORCH_APP}\nPYCODE\n"
        ]

        deployment = client.create_gpu_deployment(
            instance_name=instance_name,
            image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
            command=command,
            port=8000,
            cpu="2",
            memory="8Gi",
            gpu_count=gpu_count,
            gpu_models=[gpu_model],
            min_gpu_memory_gb=min_vram_gb,
            min_cuda_version=cuda_version,
            ttl_seconds=3600,
            storage="/data"
        )

        actual_instance = deployment.get("instanceName", deployment.get("instance_name"))
        print(f"   Instance: {actual_instance}")
        print(f"   URL: {deployment.get('url')}")

        print("\n2. Waiting for deployment...")
        url = wait_for_deployment(client, actual_instance, max_wait=300)

        if not url:
            print("\nError: Deployment failed to become ready")
            print("Check GPU node availability")
            sys.exit(1)

        print("\nWaiting 30s for PyTorch initialization...")
        time.sleep(30)

        print("\n3. Testing GPU endpoints...")
        results = test_gpu_endpoints(url)

        print("\n" + "=" * 60)
        print("Results:")
        passed = sum(1 for v in results.values() if v)
        for name, ok in results.items():
            print(f"  {'[OK]' if ok else '[X]'} {name}")
        print(f"\n  Total: {passed}/{len(results)} passed")

        print("\n4. Cleaning up...")
        client.delete_deployment(actual_instance)
        print("   Deployment deleted")

    except KeyboardInterrupt:
        print("\n\nInterrupted")
        if actual_instance:
            client.delete_deployment(actual_instance)
        sys.exit(0)

    except Exception as e:
        print(f"\nError: {e}")
        if actual_instance:
            try:
                client.delete_deployment(actual_instance)
            except Exception:
                pass
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)


if __name__ == "__main__":
    main()
