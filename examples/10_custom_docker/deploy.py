#!/usr/bin/env python3
"""
Deploy a custom Docker image to Basilica.

For multi-file projects, build a Docker image and push to a registry,
then deploy the image to Basilica.

Prerequisites:
    1. Build and push your image:
       docker build -t ghcr.io/yourusername/my-api:latest .
       docker push ghcr.io/yourusername/my-api:latest

    2. Set your API token:
       export BASILICA_API_TOKEN="your-token"

    3. Run this script:
       python3 deploy.py

Usage:
    python3 deploy.py <image-name>
    python3 deploy.py ghcr.io/myuser/my-api:latest
"""
import sys
from basilica import BasilicaClient

if len(sys.argv) < 2:
    print("Usage: python3 deploy.py <image-name>")
    print("Example: python3 deploy.py ghcr.io/yourusername/my-api:latest")
    sys.exit(1)

image = sys.argv[1]

client = BasilicaClient()

print(f"Deploying image: {image}")

deployment = client.deploy(
    name="custom-api",
    image=image,
    port=8000,
    ttl_seconds=3600,
    timeout=180,
)

print(f"URL:  {deployment.url}")
print(f"Docs: {deployment.url}/docs")
