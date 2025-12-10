#!/usr/bin/env python3
"""
Deploy torchrun-ddp to Basilica.

Prerequisites:
    1. Build and push the Docker image:
       docker build -t ghcr.io/<username>/torchrun-ddp:latest .
       docker push ghcr.io/<username>/torchrun-ddp:latest

    2. Set environment variables:
       export BASILICA_API_TOKEN="your-token"
       export HF_TOKEN="your-huggingface-token"

    3. Run this script:
       python3 deploy.py ghcr.io/<username>/torchrun-ddp:latest

Usage:
    python3 deploy.py <image-name>
"""
import os
import sys

from basilica import BasilicaClient


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 deploy.py <image-name>")
        print("Example: python3 deploy.py ghcr.io/username/torchrun-ddp:latest")
        sys.exit(1)

    image = sys.argv[1]

    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        print("Warning: HF_TOKEN not set. Training will fail without HuggingFace access.")
        print("Set it with: export HF_TOKEN='your-token'")

    env = {}
    if hf_token:
        env["HF_TOKEN"] = hf_token

    client = BasilicaClient()

    print(f"Deploying image: {image}")

    deployment = client.create_deployment(
        instance_name="torchrun-ddp",
        image=image,
        command=["python", "entrypoint.py"],
        port=8000,
        gpu_count=1,
        gpu_models=["NVIDIA-RTX-A4000", "A100", "H100"],
        memory="16Gi",
        cpu="4",
        env=env,
        storage="/data",
        ttl_seconds=3600,
        public=True,
    )

    print(f"Deployment URL: {deployment.url}")
    print(f"Training status: {deployment.url}/status")
    print(f"Health check: {deployment.url}/health")


if __name__ == "__main__":
    main()
