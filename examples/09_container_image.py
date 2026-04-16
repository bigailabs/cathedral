#!/usr/bin/env python3
"""
Deploy a pre-built container image.

This example shows how to deploy existing Docker images without custom code.
Useful for standard services like nginx, redis, or your own pre-built images.

Note: Cathedral runs containers as non-root (UID 1000).
Use images designed for non-root execution.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 09_container_image.py
"""
import requests
from cathedral import CathedralClient

client = CathedralClient()

print("Deploying nginx (non-root image)...")

# Deploy pre-built image - no source code needed
deployment = client.deploy(
    name="nginx-demo",
    image="nginxinc/nginx-unprivileged:alpine",
    port=8080,  # nginx-unprivileged uses 8080
    replicas=1,
    env={"NGINX_HOST": "localhost"},
    cpu="250m",
    memory="256Mi",
    ttl_seconds=600,
    timeout=120,
)

print(f"Instance: {deployment.name}")
print(f"State:    {deployment.state}")
print(f"URL:      {deployment.url}")

# Test the deployment
# Note: client.deploy() already waits for readiness via wait_until_ready()
print("\nTesting nginx...")
try:
    r = requests.get(deployment.url, timeout=10)
    print(f"  Status: {r.status_code}")
    if "nginx" in r.text.lower() or "welcome" in r.text.lower():
        print("  Content: nginx default page")
except Exception as e:
    print(f"  Error: {e}")

# Cleanup
print("\nCleaning up...")
deployment.delete()
print("Done.")
