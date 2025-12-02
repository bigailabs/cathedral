#!/usr/bin/env python3
"""
Basilica Deployment - Public URL + Persistent Storage

Deploy a FastAPI application with:
- Public HTTPS URL (automatic DNS)
- Persistent storage backed by object storage

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
from basilica import BasilicaClient


# The FastAPI application to deploy
# This app demonstrates storage read/write and provides health endpoints
FASTAPI_APP = '''
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from pathlib import Path
import socket
from datetime import datetime

app = FastAPI(title="Basilica Storage Demo")

STORAGE_PATH = Path("/data")

class WriteRequest(BaseModel):
    filename: str
    content: str

@app.get("/")
def root():
    return {
        "service": "Basilica FastAPI Demo",
        "hostname": socket.gethostname(),
        "storage_mounted": STORAGE_PATH.exists(),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/storage/list")
def list_files():
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    files = []
    for f in STORAGE_PATH.rglob("*"):
        if f.is_file() and not f.name.startswith("."):
            files.append(str(f.relative_to(STORAGE_PATH)))
    return {"files": files, "count": len(files)}

@app.post("/storage/write")
def write_file(req: WriteRequest):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / req.filename
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(req.content)
    return {"success": True, "path": req.filename, "size": len(req.content)}

@app.get("/storage/read/{filename:path}")
def read_file(filename: str):
    if not STORAGE_PATH.exists():
        raise HTTPException(status_code=503, detail="Storage not mounted")
    file_path = STORAGE_PATH / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return {"path": filename, "content": file_path.read_text()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
'''


def wait_for_deployment(client: BasilicaClient, instance_name: str, max_wait: int = 180) -> bool:
    """Wait for deployment to become ready."""
    print(f"\nWaiting for deployment '{instance_name}' to be ready...")
    elapsed = 0

    while elapsed < max_wait:
        try:
            status = client.get_deployment(instance_name)
            ready = status.replicas.ready
            desired = status.replicas.desired
            state = status.state

            print(f"  {elapsed}s: State={state}, Ready={ready}/{desired}")

            if state in ("Active", "Running") and ready == desired and ready > 0:
                print("\nDeployment is ready!")
                return True

        except Exception as e:
            print(f"  {elapsed}s: Error checking status: {e}")

        time.sleep(5)
        elapsed += 5

    print(f"\nWarning: Deployment not ready after {max_wait}s")
    return False


def test_deployment(url: str) -> bool:
    """Test the deployment endpoints."""
    print("\nTesting deployment endpoints...")
    success = True

    # Test health endpoint
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

    # Test root endpoint
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

    # Write a file
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

    # Wait for sync
    time.sleep(3)

    # Read the file back
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

    # List files
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


def cleanup_deployment(client: BasilicaClient, instance_name: str):
    """Delete the deployment."""
    print(f"\nDeleting deployment '{instance_name}'...")
    try:
        result = client.delete_deployment(instance_name)
        print(f"  State: {result.state}")
        print(f"  Message: {result.message}")
    except Exception as e:
        print(f"  Error: {e}")


def main():
    # Check for API token
    api_token = os.getenv("BASILICA_API_TOKEN")
    if not api_token:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        print("")
        print("To get a token:")
        print("  1. Run: basilica tokens create my-token")
        print("  2. Export: export BASILICA_API_TOKEN='basilica_...'")
        sys.exit(1)

    api_url = os.getenv("BASILICA_API_URL", "https://api.basilica.ai")

    # Generate unique instance name
    instance_name = f"sdk-fastapi-{int(time.time())}"

    print("=" * 72)
    print("Basilica SDK - Public Deployment with Storage")
    print("=" * 72)
    print("")
    print("Configuration:")
    print(f"  API URL:       {api_url}")
    print(f"  Instance Name: {instance_name}")
    print(f"  Token:         {api_token[:20]}...")
    print("")

    # Initialize client
    client = BasilicaClient(base_url=api_url, api_key=api_token)
    print("Basilica client initialized")

    deployment_url = None
    actual_instance_name = None

    try:
        # Step 1: Create deployment
        print("\n" + "-" * 72)
        print("Step 1: Creating deployment with FastAPI app and storage")
        print("-" * 72)

        # Build the command that installs dependencies and runs the app
        command = [
            "bash", "-c",
            f"pip install -q fastapi uvicorn pydantic && python - <<'PYCODE'\n{FASTAPI_APP}\nPYCODE\n"
        ]

        deployment = client.create_deployment(
            instance_name=instance_name,
            image="python:3.11-slim",
            replicas=1,
            port=8000,
            command=command,
            cpu="500m",
            memory="512Mi",
            ttl_seconds=1800,      # Auto-delete after 30 minutes
            public=True,            # Enable public URL with DNS
            storage="/data"         # Mount storage at /data
        )

        actual_instance_name = deployment.instance_name
        deployment_url = deployment.url

        print(f"\nDeployment created:")
        print(f"  Instance: {deployment.instance_name}")
        print(f"  State:    {deployment.state}")
        print(f"  URL:      {deployment.url}")
        print(f"  Replicas: {deployment.replicas.desired} desired, {deployment.replicas.ready} ready")

        # Step 2: Wait for deployment to be ready
        print("\n" + "-" * 72)
        print("Step 2: Waiting for deployment to be ready")
        print("-" * 72)

        ready = wait_for_deployment(client, actual_instance_name)
        if not ready:
            print("\nError: Deployment failed to become ready")
            sys.exit(1)

        # Get updated URL after deployment is ready
        status = client.get_deployment(actual_instance_name)
        deployment_url = status.url

        # Wait for HTTP server to start
        print("\nWaiting 15s for HTTP server to initialize...")
        time.sleep(15)

        # Step 3: Test the deployment
        print("\n" + "-" * 72)
        print("Step 3: Testing deployment endpoints")
        print("-" * 72)

        test_success = test_deployment(deployment_url)

        # Step 4: Test storage operations
        print("\n" + "-" * 72)
        print("Step 4: Testing storage operations")
        print("-" * 72)

        storage_success = test_storage_operations(deployment_url)

        # Summary
        print("\n" + "=" * 72)
        print("Summary")
        print("=" * 72)
        print(f"  Instance:        {actual_instance_name}")
        print(f"  Public URL:      {deployment_url}")
        print(f"  Storage Mount:   /data")
        print(f"  Endpoint Tests:  {'Passed' if test_success else 'Failed'}")
        print(f"  Storage Tests:   {'Passed' if storage_success else 'Failed'}")
        print("")

        # Cleanup prompt
        print("-" * 72)
        cleanup = input("Delete the deployment? (y/N): ").strip().lower()
        if cleanup == "y":
            cleanup_deployment(client, actual_instance_name)
            print("\nDeployment deleted.")
        else:
            print(f"\nDeployment left running at: {deployment_url}")
            print("")
            print("To delete later:")
            print(f"  client.delete_deployment('{actual_instance_name}')")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        if actual_instance_name:
            cleanup = input("\nDelete the deployment before exit? (y/N): ").strip().lower()
            if cleanup == "y":
                cleanup_deployment(client, actual_instance_name)
        sys.exit(0)

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()

        if actual_instance_name:
            print("\nCleaning up deployment...")
            try:
                cleanup_deployment(client, actual_instance_name)
            except Exception:
                pass
        sys.exit(1)

    print("\n" + "=" * 72)
    print("Example completed!")
    print("=" * 72)
    print("")
    print("Key SDK Methods Used:")
    print("  client.create_deployment() - Create a new deployment")
    print("  client.get_deployment()    - Get deployment status")
    print("  client.delete_deployment() - Delete a deployment")
    print("")
    print("For more information, see:")
    print("  - https://docs.basilica.ai")
    print("  - examples/README.md")


if __name__ == "__main__":
    main()
