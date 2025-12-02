"""
PyTorch GPU Demo Application

This is a sample FastAPI application that demonstrates GPU usage with PyTorch.
Deploy it using the Basilica SDK:

    deployment = client.deploy(
        name="pytorch-demo",
        source="pytorch_gpu_app.py",
        image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
        port=8000,
        gpu_count=1,
        storage=True,
    )
"""

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
