#!/usr/bin/env python3
"""
Example: Deploy and manage containerized applications on Basilica

This example demonstrates:
1. Creating a deployment with replicas
2. Checking deployment status
3. Listing all deployments
4. Deleting a deployment

Prerequisites:
- BASILICA_API_TOKEN environment variable
- basilica-sdk installed: pip install basilica-sdk

Usage:
    export BASILICA_API_TOKEN="your-token-here"
    python3 deploy_container.py

Note: Basilica runs containers with non-root security context (UID 1000).
Use images designed for non-root execution (e.g., nginxinc/nginx-unprivileged).
"""

import os
import sys
import time
from basilica import BasilicaClient


def main():
    api_key = os.environ.get("BASILICA_API_TOKEN")
    if not api_key:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        print("Create a token using: basilica tokens create")
        sys.exit(1)

    base_url = os.environ.get("BASILICA_API_URL", "https://api.basilica.ai")
    client = BasilicaClient(base_url=base_url, api_key=api_key)

    print("=" * 60)
    print("Basilica Deployment Example")
    print("=" * 60)

    # Step 1: Create nginx deployment
    print("\n1. Creating nginx deployment with 2 replicas...")
    print("   Using nginxinc/nginx-unprivileged (runs as non-root)")

    deployment = client.create_deployment(
        instance_name="nginx-example",
        image="nginxinc/nginx-unprivileged:alpine",
        replicas=2,
        port=8080,
        env={"NGINX_HOST": "example.com"},
        cpu="500m",
        memory="512Mi",
        ttl_seconds=3600,
    )

    # Store the actual instance name (UUID) returned by the API
    nginx_instance = deployment.instance_name

    print(f"   Instance: {nginx_instance}")
    print(f"   State: {deployment.state}")
    print(f"   URL: {deployment.url}")
    print(f"   Namespace: {deployment.namespace}")

    # Step 2: Wait for deployment to become ready
    print("\n2. Waiting for deployment to become ready...")
    max_wait = 120
    elapsed = 0

    while elapsed < max_wait:
        status = client.get_deployment(nginx_instance)
        ready = status.replicas.ready
        desired = status.replicas.desired
        print(f"   [{elapsed}s] State: {status.state}, Replicas: {ready}/{desired}")

        if ready == desired and ready > 0:
            print("   Deployment is ready!")
            break

        time.sleep(5)
        elapsed += 5

    if elapsed >= max_wait:
        print("   Warning: Deployment not ready within timeout")

    # Step 3: Get detailed status
    print("\n3. Getting detailed deployment status...")
    status = client.get_deployment(nginx_instance)
    print(f"   Instance: {status.instance_name}")
    print(f"   State: {status.state}")
    print(f"   URL: {status.url}")
    print(f"   Created: {status.created_at}")

    if status.pods:
        print("   Pods:")
        for pod in status.pods:
            node_info = f" (node: {pod.node})" if pod.node else ""
            print(f"     - {pod.name}: {pod.status}{node_info}")

    # Step 4: List all deployments
    print("\n4. Listing all deployments...")
    deployments = client.list_deployments()
    print(f"   Total: {deployments.total}")

    for dep in deployments.deployments[:5]:
        print(f"   - {dep.instance_name}: {dep.state}")

    # Step 5: Create second deployment
    print("\n5. Creating Python HTTP server deployment...")

    python_dep = client.create_deployment(
        instance_name="python-example",
        image="python:3.11-slim",
        replicas=1,
        port=8000,
        command=["python", "-m", "http.server", "8000"],
        cpu="250m",
        memory="256Mi",
        ttl_seconds=3600,
    )

    python_instance = python_dep.instance_name
    print(f"   Instance: {python_instance}")
    print(f"   URL: {python_dep.url}")

    # Step 6: Cleanup
    print("\n6. Cleaning up deployments...")

    for instance in [nginx_instance, python_instance]:
        print(f"   Deleting {instance}...")
        result = client.delete_deployment(instance)
        print(f"   {result.message}")

    # Step 7: Verify
    print("\n7. Verifying deletion...")
    deployments = client.list_deployments()
    print(f"   Remaining deployments: {deployments.total}")

    print("\n" + "=" * 60)
    print("Example completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
