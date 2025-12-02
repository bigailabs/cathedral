#!/usr/bin/env python3
"""
Basilica Deployment - Public URL + Persistent Storage

Deploy a FastAPI application with:
- Public HTTPS URL (automatic DNS)
- Persistent storage backed by object storage

This example demonstrates the simplified SDK interface using deploy()
instead of the low-level create_deployment() method.

Prerequisites:
    - BASILICA_API_TOKEN environment variable set
    - basilica-sdk installed: pip install basilica-sdk
    - requests installed: pip install requests

Usage:
    export BASILICA_API_TOKEN="your-token-here"
    python3 public_storage_deployment.py
"""

import os
import sys
import time
import requests
from basilica import BasilicaClient, DeploymentTimeout, DeploymentFailed


def test_deployment(url: str) -> bool:
    """Test the deployment endpoints."""
    print("\nTesting deployment endpoints...")
    success = True

    print(f"\n  GET {url}/health")
    try:
        response = requests.get(f"{url}/health", timeout=10)
        if response.status_code == 200:
            print(f"    Status: {response.status_code}")
            print(f"    Response: {response.json()}")
        else:
            print(f"    Failed: HTTP {response.status_code}")
            success = False
    except Exception as e:
        print(f"    Error: {e}")
        success = False

    print(f"\n  GET {url}/")
    try:
        response = requests.get(f"{url}/", timeout=10)
        if response.status_code == 200:
            print(f"    Status: {response.status_code}")
            data = response.json()
            print(f"    Service: {data.get('service')}")
            print(f"    Hostname: {data.get('hostname')}")
            print(f"    Storage Mounted: {data.get('storage_mounted')}")
        else:
            print(f"    Failed: HTTP {response.status_code}")
            success = False
    except Exception as e:
        print(f"    Error: {e}")
        success = False

    return success


def test_storage_operations(url: str) -> bool:
    """Test storage read/write operations."""
    print("\nTesting storage operations...")
    success = True

    print(f"\n  POST {url}/storage/write")
    try:
        response = requests.post(
            f"{url}/storage/write",
            json={"filename": "hello.txt", "content": "Hello from Basilica SDK!"},
            timeout=10
        )
        if response.status_code == 200:
            print(f"    Status: {response.status_code}")
            print(f"    Response: {response.json()}")
        else:
            print(f"    Failed: HTTP {response.status_code}")
            success = False
    except Exception as e:
        print(f"    Error: {e}")
        success = False

    time.sleep(3)

    print(f"\n  GET {url}/storage/read/hello.txt")
    try:
        response = requests.get(f"{url}/storage/read/hello.txt", timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"    Status: {response.status_code}")
            print(f"    Content: {data.get('content')}")
            if "Hello from Basilica SDK!" in data.get("content", ""):
                print("    Verification: Content matches!")
            else:
                print("    Warning: Content does not match")
                success = False
        else:
            print(f"    Failed: HTTP {response.status_code}")
            success = False
    except Exception as e:
        print(f"    Error: {e}")
        success = False

    print(f"\n  GET {url}/storage/list")
    try:
        response = requests.get(f"{url}/storage/list", timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"    Status: {response.status_code}")
            print(f"    Files: {data.get('files')}")
            print(f"    Count: {data.get('count')}")
        else:
            print(f"    Failed: HTTP {response.status_code}")
            success = False
    except Exception as e:
        print(f"    Error: {e}")
        success = False

    return success


def main():
    api_token = os.getenv("BASILICA_API_TOKEN")
    if not api_token:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        print("")
        print("To get a token:")
        print("  1. Run: basilica tokens create my-token")
        print("  2. Export: export BASILICA_API_TOKEN='basilica_...'")
        sys.exit(1)

    instance_name = f"sdk-fastapi-{int(time.time())}"

    print("=" * 72)
    print("Basilica SDK - Public Deployment with Storage")
    print("=" * 72)
    print("")
    print("Configuration:")
    print(f"  Instance Name: {instance_name}")
    print(f"  Token:         {api_token[:20]}...")
    print("")

    client = BasilicaClient()
    print("Basilica client initialized")

    deployment = None

    try:
        print("\n" + "-" * 72)
        print("Step 1: Deploying FastAPI app with storage (auto-waits for ready)")
        print("-" * 72)

        # Using the new high-level deploy() method
        # - source: file path OR inline code (auto-detects)
        # - storage=True: enables storage at /data
        # - deploy() automatically waits until ready
        deployment = client.deploy(
            name=instance_name,
            source="fastapi_storage_app.py",  # Will be created below
            port=8000,
            storage=True,           # Simple! Mounts storage at /data
            ttl_seconds=1800,       # Auto-delete after 30 minutes
        )

        print(f"\nDeployment ready!")
        print(f"  Instance: {deployment.name}")
        print(f"  State:    {deployment.state}")
        print(f"  URL:      {deployment.url}")

        # Wait for HTTP server to start
        print("\nWaiting 15s for HTTP server to initialize...")
        time.sleep(15)

        print("\n" + "-" * 72)
        print("Step 2: Testing deployment endpoints")
        print("-" * 72)
        test_success = test_deployment(deployment.url)

        print("\n" + "-" * 72)
        print("Step 3: Testing storage operations")
        print("-" * 72)
        storage_success = test_storage_operations(deployment.url)

        print("\n" + "=" * 72)
        print("Summary")
        print("=" * 72)
        print(f"  Instance:        {deployment.name}")
        print(f"  Public URL:      {deployment.url}")
        print(f"  Storage Mount:   /data")
        print(f"  Endpoint Tests:  {'Passed' if test_success else 'Failed'}")
        print(f"  Storage Tests:   {'Passed' if storage_success else 'Failed'}")
        print("")

        print("-" * 72)
        cleanup = input("Delete the deployment? (y/N): ").strip().lower()
        if cleanup == "y":
            deployment.delete()
            print("\nDeployment deleted.")
        else:
            print(f"\nDeployment left running at: {deployment.url}")
            print("")
            print("To delete later:")
            print(f"  deployment.delete()  # or client.get('{deployment.name}').delete()")

    except DeploymentTimeout as e:
        print(f"\nError: {e}")
        print("The deployment did not become ready within the timeout.")
        sys.exit(1)

    except DeploymentFailed as e:
        print(f"\nError: {e}")
        print("The deployment failed to start.")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        if deployment:
            cleanup = input("\nDelete the deployment before exit? (y/N): ").strip().lower()
            if cleanup == "y":
                deployment.delete()
        sys.exit(0)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

        if deployment:
            print("\nCleaning up deployment...")
            try:
                deployment.delete()
            except Exception:
                pass
        sys.exit(1)

    print("\n" + "=" * 72)
    print("Example completed!")
    print("=" * 72)
    print("")
    print("Key SDK Methods Used (New Simplified API):")
    print("  client.deploy()       - Deploy with auto-wait")
    print("  deployment.url        - Get public URL")
    print("  deployment.logs()     - Get container logs")
    print("  deployment.status()   - Check current status")
    print("  deployment.delete()   - Clean up")
    print("")
    print("Compare with low-level API:")
    print("  client.create_deployment()  - Create (returns immediately)")
    print("  client.get_deployment()     - Check status")
    print("  client.delete_deployment()  - Delete")


if __name__ == "__main__":
    main()
