#!/usr/bin/env python3
"""
Example: Deploy and manage containerized applications on Basilica

This example demonstrates both the high-level and low-level APIs:
1. High-level deploy() for simple deployments
2. Low-level create_deployment() for full control
3. Checking deployment status
4. Listing all deployments
5. Deleting deployments

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
from basilica import BasilicaClient, DeploymentTimeout


def main():
    api_key = os.environ.get("BASILICA_API_TOKEN")
    if not api_key:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        print("Create a token using: basilica tokens create")
        sys.exit(1)

    client = BasilicaClient()

    print("=" * 60)
    print("Basilica Deployment Example")
    print("=" * 60)

    # Step 1: Deploy using the high-level API (recommended)
    print("\n1. Deploying Python HTTP server (high-level API)...")
    print("   Using deploy() - auto-waits for ready state")

    try:
        python_deployment = client.deploy(
            name="python-example",
            source="""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'status': 'ok', 'service': 'python-example'}).encode())
    def log_message(self, *a): pass

HTTPServer(('', 8000), Handler).serve_forever()
""",
            port=8000,
            cpu="250m",
            memory="256Mi",
            ttl_seconds=1800,
            timeout=120,
        )

        print(f"   Instance: {python_deployment.name}")
        print(f"   State: {python_deployment.state}")
        print(f"   URL: {python_deployment.url}")

    except DeploymentTimeout as e:
        print(f"   Timeout: {e}")
        print("   Continuing with low-level API example...")
        python_deployment = None

    # Step 2: Deploy using the low-level API (for full control)
    print("\n2. Deploying nginx (low-level API)...")
    print("   Using create_deployment() - returns immediately")
    print("   Using nginxinc/nginx-unprivileged (runs as non-root)")

    deployment = client.create_deployment(
        instance_name="nginx-example",
        image="nginxinc/nginx-unprivileged:alpine",
        replicas=2,
        port=8080,
        env={"NGINX_HOST": "example.com"},
        cpu="500m",
        memory="512Mi",
        ttl_seconds=1800,
    )

    nginx_instance = deployment.instance_name

    print(f"   Instance: {nginx_instance}")
    print(f"   State: {deployment.state}")
    print(f"   URL: {deployment.url}")
    print(f"   Namespace: {deployment.namespace}")

    # Step 3: Manual wait for deployment (low-level pattern)
    print("\n3. Waiting for nginx deployment to become ready...")
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

    # Step 4: Get detailed status
    print("\n4. Getting detailed deployment status...")
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

    # Step 5: List all deployments using high-level API
    print("\n5. Listing all deployments (high-level API)...")
    deployments = client.list()
    print(f"   Total: {len(deployments)}")

    for dep in deployments[:5]:
        print(f"   - {dep.name}: {dep.state}")

    # Step 6: Compare with low-level listing
    print("\n6. Listing deployments (low-level API)...")
    low_level_list = client.list_deployments()
    print(f"   Total: {low_level_list.total}")

    for dep in low_level_list.deployments[:5]:
        print(f"   - {dep.instance_name}: {dep.state}")

    # Step 7: Cleanup
    print("\n7. Cleaning up deployments...")

    # High-level cleanup (if deployment exists)
    if python_deployment:
        print(f"   Deleting {python_deployment.name} (high-level)...")
        python_deployment.delete()
        print("   Deleted")

    # Low-level cleanup
    print(f"   Deleting {nginx_instance} (low-level)...")
    result = client.delete_deployment(nginx_instance)
    print(f"   {result.message}")

    # Step 8: Verify
    print("\n8. Verifying deletion...")
    deployments = client.list()
    our_deployments = [d for d in deployments if d.name in ['python-example', nginx_instance]]
    print(f"   Our deployments remaining: {len(our_deployments)}")

    print("\n" + "=" * 60)
    print("Example completed successfully!")
    print("=" * 60)
    print("")
    print("Key takeaways:")
    print("  High-level API (deploy(), list(), deployment.delete()):")
    print("    - Simpler, auto-waits, returns facade objects")
    print("    - Recommended for most use cases")
    print("")
    print("  Low-level API (create_deployment(), get_deployment(), etc.):")
    print("    - Full control, non-blocking, raw responses")
    print("    - Use when you need custom logic")


if __name__ == "__main__":
    main()
