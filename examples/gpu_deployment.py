#!/usr/bin/env python3
"""
Example: Deploy GPU-accelerated PyTorch workload on Basilica

This example demonstrates the simplified SDK interface for GPU deployments:
1. Deploy from a file with gpu_count parameter
2. Automatic waiting for deployment to be ready
3. Using the Deployment facade for easy management

Available GPU nodes:
- NVIDIA RTX A4000 (14GB VRAM, CUDA 12.8)

Prerequisites:
- BASILICA_API_TOKEN environment variable
- basilica-sdk installed: pip install basilica-sdk
- requests installed: pip install requests

Usage:
    export BASILICA_API_TOKEN="your-token-here"
    python3 gpu_deployment.py
"""

import os
import sys
import time
import requests
from basilica import (
    BasilicaClient,
    DeploymentTimeout,
    DeploymentFailed,
    ResourceError,
)


def test_gpu_endpoints(url: str) -> dict:
    """Test GPU-related endpoints."""
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

    gpu_model = os.getenv("GPU_MODEL", "NVIDIA-RTX-A4000")
    gpu_count = int(os.getenv("GPU_COUNT", "1"))
    min_vram_gb = int(os.getenv("MIN_VRAM_GB", "12"))
    cuda_version = os.getenv("CUDA_VERSION", "12.0")

    instance_name = f"gpu-pytorch-{int(time.time())}"

    print("=" * 60)
    print("Basilica GPU Deployment Example (Simplified SDK)")
    print("=" * 60)
    print(f"\nGPU Requirements:")
    print(f"  Model: {gpu_model}")
    print(f"  Count: {gpu_count}")
    print(f"  Min VRAM: {min_vram_gb} GB")
    print(f"  CUDA: >= {cuda_version}")

    client = BasilicaClient()
    deployment = None

    try:
        print("\n1. Deploying GPU workload (auto-waits for ready)...")

        # The new simplified deploy() method handles:
        # - Reading source from file
        # - Building container command with pip install
        # - Waiting for deployment to be ready
        # - Returning a Deployment facade with convenient methods
        deployment = client.deploy(
            name=instance_name,
            source="pytorch_gpu_app.py",  # Load from file
            image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
            port=8000,
            cpu="2",
            memory="8Gi",
            gpu_count=gpu_count,
            gpu_models=[gpu_model],
            min_gpu_memory_gb=min_vram_gb,
            min_cuda_version=cuda_version,
            storage=True,
            ttl_seconds=3600,
            timeout=300,
        )

        print(f"\n   Deployment ready!")
        print(f"   Instance: {deployment.name}")
        print(f"   URL: {deployment.url}")

        print("\nWaiting 30s for PyTorch initialization...")
        time.sleep(30)

        print("\n2. Testing GPU endpoints...")
        results = test_gpu_endpoints(deployment.url)

        print("\n" + "=" * 60)
        print("Results:")
        passed = sum(1 for v in results.values() if v)
        for name, ok in results.items():
            print(f"  {'[OK]' if ok else '[X]'} {name}")
        print(f"\n  Total: {passed}/{len(results)} passed")

        print("\n3. Getting logs...")
        logs = deployment.logs(tail=20)
        print("   Last 20 lines of logs:")
        for line in logs.split('\n')[-10:]:
            print(f"   {line}")

        print("\n4. Cleaning up...")
        deployment.delete()
        print("   Deployment deleted")

    except DeploymentTimeout as e:
        print(f"\nError: {e}")
        print("Check GPU node availability")
        sys.exit(1)

    except DeploymentFailed as e:
        print(f"\nError: {e}")
        if deployment:
            print("\nGetting logs for debugging...")
            try:
                logs = deployment.logs(tail=50)
                print(logs)
            except Exception:
                pass
        sys.exit(1)

    except ResourceError as e:
        print(f"\nError: {e}")
        print("No GPU nodes available matching requirements")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nInterrupted")
        if deployment:
            deployment.delete()
        sys.exit(0)

    except Exception as e:
        print(f"\nError: {e}")
        if deployment:
            try:
                deployment.delete()
            except Exception:
                pass
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Example completed!")
    print("=" * 60)
    print("")
    print("Key differences from low-level API:")
    print("  Before: command=['bash', '-c', f'pip install ... && python - <<PYCODE\\n{code}\\nPYCODE']")
    print("  After:  source='pytorch_gpu_app.py'  # SDK handles everything")
    print("")
    print("  Before: Manual polling loop with get_deployment()")
    print("  After:  deploy() blocks until ready")
    print("")
    print("  Before: client.delete_deployment(instance_name)")
    print("  After:  deployment.delete()")


if __name__ == "__main__":
    main()
