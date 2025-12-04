#!/usr/bin/env python3
"""
Deploy from an external Python file.

This example shows how to deploy an application from a separate .py file
rather than inline source code. Useful for larger applications.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 08_external_file.py
"""
import requests
from basilica import BasilicaClient

client = BasilicaClient()

# Deploy from external file - SDK reads and packages the file
deployment = client.deploy(
    name="file-deploy",
    source="app_file.py",  # External file in same directory
    port=8000,
    pip_packages=["fastapi", "uvicorn"],
    ttl_seconds=600,
    timeout=180,
)

print(f"Deployed: {deployment.url}")
print(f"Health:   {deployment.url}/health")
print(f"Info:     {deployment.url}/info")

# Test the deployment
print("\nTesting endpoints...")
import time
time.sleep(10)

try:
    r = requests.get(f"{deployment.url}/health", timeout=10)
    print(f"  /health: {r.json()}")
except Exception as e:
    print(f"  /health: {e}")

try:
    r = requests.get(f"{deployment.url}/info", timeout=10)
    print(f"  /info:   {r.json()}")
except Exception as e:
    print(f"  /info:   {e}")

# Cleanup
print("\nCleaning up...")
deployment.delete()
print("Done.")
