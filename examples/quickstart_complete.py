#!/usr/bin/env python3
"""
Basilica SDK - Complete Quickstart Example

This script demonstrates the complete workflow for using the Basilica SDK:
1. Environment verification
2. API health check
3. Node discovery
4. Rental management (demo)
5. Deployment management (demo)

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
from basilica import BasilicaClient


def print_header(title):
    """Print section header"""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")


def verify_environment():
    """Verify all prerequisites are met"""
    print_header("Step 1: Environment Verification")

    api_token = os.environ.get("BASILICA_API_TOKEN")
    if not api_token:
        print("❌ Error: BASILICA_API_TOKEN environment variable not set")
        print("\nTo generate a token:")
        print("  1. Run: basilica tokens create my-token")
        print("  2. Copy the token from the output")
        print("  3. Set: export BASILICA_API_TOKEN='your-token-here'")
        print("\nOr add to your shell profile (~/.bashrc or ~/.zshrc):")
        print("  echo 'export BASILICA_API_TOKEN=\"...\"' >> ~/.bashrc")
        print("  source ~/.bashrc")
        sys.exit(1)

    print(f"✓ BASILICA_API_TOKEN is set")
    print(f"  Token prefix: {api_token[:20]}...")
    print(f"  Token length: {len(api_token)} characters")

    api_url = os.environ.get("BASILICA_API_URL", "https://api.basilica.ai")
    print(f"✓ API URL: {api_url}")

    ssh_key_path = os.path.expanduser("~/.ssh/basilica_ed25519.pub")
    if os.path.exists(ssh_key_path):
        print(f"✓ SSH public key found: {ssh_key_path}")
    else:
        print(f"⚠ SSH public key not found: {ssh_key_path}")
        print("  Generate one with: ssh-keygen -t ed25519 -f ~/.ssh/basilica_ed25519 -N ''")

    return api_token, api_url


def check_api_health(client):
    """Check API health and connectivity"""
    print_header("Step 2: API Health Check")

    try:
        health = client.health_check()

        print("API Connection: ✓ Success")
        print(f"  Status: {health.status}")
        print(f"  Version: {health.version}")
        print(f"  Validators: {health.healthy_validators}/{health.total_validators} healthy")

        if health.healthy_validators < health.total_validators:
            print(f"\n⚠ Warning: Some validators are unhealthy")

        return True

    except Exception as e:
        print(f"❌ API Connection Failed: {e}")
        print("\nTroubleshooting:")
        print("  1. Check your internet connection")
        print("  2. Verify API URL is correct")
        print("  3. Ensure your token is valid: basilica tokens list")
        print("  4. Try: curl https://api.basilica.ai/health")
        return False


def discover_nodes(client):
    """Discover available GPU nodes"""
    print_header("Step 3: Discover Available GPU Nodes")

    try:
        nodes = client.list_nodes(available=True)

        print(f"Found {len(nodes)} available GPU node(s)")

        if len(nodes) == 0:
            print("\n⚠ No nodes currently available")
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
        print(f"❌ Failed to list nodes: {e}")


def demonstrate_rental_workflow(client):
    """Demonstrate rental workflow (without actually creating one)"""
    print_header("Step 4: GPU Rental Workflow (Demo)")

    print("To start a GPU rental, use this code:")
    print("""
    rental = client.start_rental(
        container_image="nvidia/cuda:12.2.0-base-ubuntu22.04",
        gpu_type="b200",
        environment={
            "CUDA_VISIBLE_DEVICES": "0",
            "MY_VARIABLE": "value"
        },
        ports=[
            {"container_port": 8888, "host_port": 8888, "protocol": "tcp"},  # Jupyter
            {"container_port": 6006, "host_port": 6006, "protocol": "tcp"},  # TensorBoard
        ],
        command=["/bin/bash"]
    )

    print(f"Rental ID: {rental.rental_id}")
    print(f"Status: {rental.status}")
    print(f"Container: {rental.container_name}")

    if rental.ssh_credentials:
        print(f"SSH: ssh -i ~/.ssh/basilica_ed25519 {rental.ssh_credentials}")
    """)

    print("\n📝 Note: Uncomment the code above to actually start a rental")
    print("   Rentals incur charges based on GPU usage")


def demonstrate_deployment_workflow(client):
    """Demonstrate K8s deployment workflow (without actually creating one)"""
    print_header("Step 5: K8s Deployment Workflow (Demo)")

    print("To deploy a containerized application, use this code:")
    print("""
    deployment = client.create_deployment(
        instance_name="my-nginx-app",
        image="nginx:latest",
        replicas=2,
        port=80,
        env={
            "NGINX_HOST": "example.com",
            "NGINX_PORT": "80"
        },
        cpu="500m",
        memory="512Mi",
        ttl_seconds=3600  # Auto-delete after 1 hour
    )

    print(f"Deployment: {deployment.instance_name}")
    print(f"URL: {deployment.url}")
    print(f"State: {deployment.state}")
    print(f"Replicas: {deployment.replicas.desired}")

    # Wait for deployment to be ready
    import time
    while True:
        status = client.get_deployment("my-nginx-app")
        if status.replicas.ready == status.replicas.desired:
            print("✓ Deployment is ready!")
            break
        time.sleep(5)

    # Access your deployment
    print(f"Visit: {deployment.url}")

    # Cleanup when done
    client.delete_deployment("my-nginx-app")
    """)

    print("\n📝 Note: Uncomment the code above to actually create a deployment")
    print("   Instance names must be DNS-safe (lowercase, hyphens only)")


def list_user_resources(client):
    """List user's existing rentals and deployments"""
    print_header("Step 6: Your Existing Resources")

    try:
        print("Rentals:")
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
        print(f"  ⚠ Could not list rentals: {e}")

    try:
        print("\nDeployments:")
        deployments = client.list_deployments()

        if deployments.total > 0:
            print(f"  You have {deployments.total} deployment(s)")
            for dep in deployments.deployments[:3]:
                print(f"    - {dep.instance_name}: {dep.state} ({dep.replicas.ready}/{dep.replicas.desired} ready)")
        else:
            print("  No active deployments")

    except Exception as e:
        print(f"  ⚠ Could not list deployments: {e}")


def print_next_steps():
    """Print next steps and resources"""
    print_header("Next Steps")

    print("✓ Your Basilica SDK is ready to use!")
    print("\n📚 Learn More:")
    print("  - Complete guide: docs/GETTING-STARTED.md")
    print("  - SDK reference: crates/basilica-sdk-python/README.md")
    print("  - Examples: crates/basilica-sdk-python/examples/")
    print("  - Deployment guide: examples/README-deployments.md")

    print("\n🚀 Try These Examples:")
    print("  - List GPU nodes: python3 examples/list_executors.py")
    print("  - Start a rental: python3 examples/start_rental.py")
    print("  - Deploy container: python3 examples/deploy_container.py")
    print("  - Health check: python3 examples/health_check.py")

    print("\n💡 Quick Commands:")
    print("  # Manage tokens")
    print("  basilica tokens list")
    print("  basilica tokens create new-token")
    print("  basilica tokens revoke old-token")

    print("\n🔧 Troubleshooting:")
    print("  - Can't connect? Check: curl https://api.basilica.ai/health")
    print("  - Auth errors? Verify: basilica tokens list")
    print("  - SSH issues? Generate key: ssh-keygen -t ed25519 -f ~/.ssh/basilica_ed25519 -N ''")

    print("\n📞 Get Help:")
    print("  - Documentation: https://docs.basilica.ai")
    print("  - GitHub Issues: https://github.com/your-org/basilica/issues")
    print("  - Discord: https://discord.gg/basilica")


def main():
    """Main execution flow"""
    print("""
╔══════════════════════════════════════════════════════════════════╗
║                                                                  ║
║             BASILICA SDK - COMPLETE QUICKSTART                   ║
║                                                                  ║
║  Decentralized GPU Compute Network                              ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
    """)

    try:
        api_token, api_url = verify_environment()

        client = BasilicaClient(base_url=api_url, api_key=api_token)
        print("✓ Basilica client initialized")

        if not check_api_health(client):
            print("\n❌ Cannot continue without API connectivity")
            sys.exit(1)

        discover_nodes(client)

        demonstrate_rental_workflow(client)

        demonstrate_deployment_workflow(client)

        list_user_resources(client)

        print_next_steps()

        print_header("Success!")
        print("✓ All checks passed")
        print("✓ SDK is fully functional")
        print("✓ Ready to start building")

    except KeyboardInterrupt:
        print("\n\n⚠ Interrupted by user")
        sys.exit(0)

    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        print("\nPlease report this issue with the full error message:")
        print("  https://github.com/your-org/basilica/issues")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
