#!/usr/bin/env python3
"""
Basilica SDK - Complete Quickstart Example

This script demonstrates the complete workflow for using the Basilica SDK:
1. Environment verification
2. API health check
3. Node discovery
4. Deployment examples (both high-level and low-level API)

Prerequisites:
- BASILICA_API_TOKEN environment variable set
- Python 3.10+
- basilica-sdk installed (pip install basilica-sdk)

Usage:
    export BASILICA_API_TOKEN="your-token-here"
    python3 quickstart_complete.py
"""

import os
import sys
from basilica import (
    BasilicaClient,
    DeploymentTimeout,
    DeploymentFailed,
    AuthenticationError,
)


def print_header(title):
    """Print section header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def verify_environment():
    """Verify all prerequisites are met."""
    print_header("Step 1: Environment Verification")

    api_token = os.environ.get("BASILICA_API_TOKEN")
    if not api_token:
        print("Error: BASILICA_API_TOKEN environment variable not set")
        print("\nTo generate a token:")
        print("  1. Run: basilica tokens create my-token")
        print("  2. Copy the token from the output")
        print("  3. Set: export BASILICA_API_TOKEN='your-token-here'")
        sys.exit(1)

    print(f"BASILICA_API_TOKEN is set")
    print(f"  Token prefix: {api_token[:20]}...")
    print(f"  Token length: {len(api_token)} characters")

    api_url = os.environ.get("BASILICA_API_URL", "https://api.basilica.ai")
    print(f"API URL: {api_url}")

    return api_token, api_url


def check_api_health(client):
    """Check API health and connectivity."""
    print_header("Step 2: API Health Check")

    try:
        health = client.health_check()

        print("API Connection: Success")
        print(f"  Status: {health.status}")
        print(f"  Version: {health.version}")
        print(f"  Validators: {health.healthy_validators}/{health.total_validators} healthy")

        if health.healthy_validators < health.total_validators:
            print(f"\nWarning: Some validators are unhealthy")

        return True

    except Exception as e:
        print(f"API Connection Failed: {e}")
        print("\nTroubleshooting:")
        print("  1. Check your internet connection")
        print("  2. Verify API URL is correct")
        print("  3. Ensure your token is valid: basilica tokens list")
        print("  4. Try: curl https://api.basilica.ai/health")
        return False


def discover_nodes(client):
    """Discover available GPU nodes."""
    print_header("Step 3: Discover Available GPU Nodes")

    try:
        nodes = client.list_nodes(available=True)

        print(f"Found {len(nodes)} available GPU node(s)")

        if len(nodes) == 0:
            print("\nNo nodes currently available")
            print("  Check back later or contact support")
            return

        print("\nTop 3 Available Nodes:")
        for i, node_info in enumerate(nodes[:3], 1):
            node = node_info.node
            availability = node_info.availability

            print(f"\n  Node {i}: {node.id}")
            print(f"    Location: {node.location}")
            print(f"    Uptime: {availability.uptime_percentage:.1f}%")

            if node.gpu_specs:
                for gpu in node.gpu_specs:
                    print(f"    GPU: {gpu.name} - {gpu.memory_gb} GB VRAM")

            if node.cpu_specs:
                cpu = node.cpu_specs
                print(f"    CPU: {cpu.cores} cores, {cpu.memory_gb} GB RAM")

    except Exception as e:
        print(f"Failed to list nodes: {e}")


def demonstrate_high_level_api():
    """Demonstrate the new simplified deploy() API."""
    print_header("Step 4: High-Level Deployment API (Recommended)")

    print("The new deploy() method simplifies deployment significantly:")
    print("""
    # Deploy from a file - SDK handles everything!
    deployment = client.deploy(
        name="my-api",
        source="app.py",          # File path or inline code
        port=8000,
        storage=True,             # Simple! Mounts at /data
    )

    # Deployment is automatically ready when deploy() returns
    print(f"Live at: {deployment.url}")

    # Convenient methods on the Deployment object
    print(deployment.logs(tail=50))   # Get logs
    status = deployment.status()       # Check status
    deployment.delete()                # Clean up

    # GPU deployment is just as easy
    deployment = client.deploy(
        name="pytorch-train",
        source="train.py",
        image="pytorch/pytorch:2.1.0-cuda12.1-cudnn8-runtime",
        gpu_count=1,
        gpu_models=["A100", "H100"],
        storage=True,
    )
    """)

    print("Key improvements over low-level API:")
    print("  - source='file.py' instead of complex heredoc commands")
    print("  - storage=True instead of full StorageSpec JSON")
    print("  - Auto-waits for deployment to be ready")
    print("  - Returns Deployment facade with convenient methods")
    print("  - Typed exceptions (DeploymentTimeout, DeploymentFailed)")


def demonstrate_low_level_api():
    """Demonstrate the low-level API (for advanced use cases)."""
    print_header("Step 5: Low-Level Deployment API (Advanced)")

    print("The low-level API provides full control:")
    print("""
    # Create deployment (returns immediately)
    response = client.create_deployment(
        instance_name="my-nginx-app",
        image="nginx:latest",
        replicas=2,
        port=80,
        env={"NGINX_HOST": "example.com"},
        cpu="500m",
        memory="512Mi",
        ttl_seconds=3600,
        storage="/data",         # Mount path
    )

    # Manual polling for readiness
    import time
    while True:
        status = client.get_deployment("my-nginx-app")
        if status.replicas.ready == status.replicas.desired:
            break
        time.sleep(5)

    # Access deployment
    print(f"Visit: {status.url}")

    # Cleanup
    client.delete_deployment("my-nginx-app")
    """)

    print("Use low-level API when you need:")
    print("  - Custom polling/waiting logic")
    print("  - Access to raw API responses")
    print("  - Non-blocking deployment creation")


def list_user_resources(client):
    """List user's existing resources."""
    print_header("Step 6: Your Existing Resources")

    try:
        print("Deployments:")
        # Using the new high-level list() method
        deployments = client.list()

        if len(deployments) > 0:
            print(f"  You have {len(deployments)} deployment(s)")
            for dep in deployments[:3]:
                print(f"    - {dep.name}: {dep.state}")
        else:
            print("  No active deployments")

    except Exception as e:
        print(f"  Could not list deployments: {e}")

    try:
        print("\nRentals:")
        rentals = client.list_rentals()

        if isinstance(rentals, dict) and 'rentals' in rentals:
            rental_list = rentals['rentals']
            if len(rental_list) > 0:
                print(f"  You have {len(rental_list)} active rental(s)")
                for rental in rental_list[:3]:
                    print(f"    - {rental.get('rental_id', 'N/A')}: {rental.get('status', 'N/A')}")
            else:
                print("  No active rentals")
        else:
            print("  No active rentals")

    except Exception as e:
        print(f"  Could not list rentals: {e}")


def print_next_steps():
    """Print next steps and resources."""
    print_header("Next Steps")

    print("Your Basilica SDK is ready to use!")
    print("\nExamples:")
    print("  - Simple deploy:    python3 examples/simple_deploy.py")
    print("  - With storage:     python3 examples/public_storage_deployment.py")
    print("  - GPU deployment:   python3 examples/gpu_deployment.py")
    print("  - Container deploy: python3 examples/deploy_container.py")

    print("\nQuick Commands:")
    print("  # Manage tokens")
    print("  basilica tokens list")
    print("  basilica tokens create new-token")
    print("  basilica tokens revoke old-token")

    print("\nTroubleshooting:")
    print("  - Can't connect? Check: curl https://api.basilica.ai/health")
    print("  - Auth errors? Verify: basilica tokens list")
    print("  - GPU issues? Check: client.list_nodes(available=True)")


def main():
    """Main execution flow."""
    print("""
+----------------------------------------------------------------------+
|                                                                      |
|             BASILICA SDK - COMPLETE QUICKSTART                       |
|                                                                      |
|  Decentralized GPU Compute Network                                  |
|                                                                      |
+----------------------------------------------------------------------+
    """)

    try:
        api_token, api_url = verify_environment()

        client = BasilicaClient(base_url=api_url, api_key=api_token)
        print("Basilica client initialized")

        if not check_api_health(client):
            print("\nCannot continue without API connectivity")
            sys.exit(1)

        discover_nodes(client)

        demonstrate_high_level_api()

        demonstrate_low_level_api()

        list_user_resources(client)

        print_next_steps()

        print_header("Success!")
        print("All checks passed")
        print("SDK is fully functional")
        print("Ready to start building")

    except AuthenticationError as e:
        print(f"\nAuthentication Error: {e}")
        print("Check your BASILICA_API_TOKEN")
        sys.exit(1)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)

    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
