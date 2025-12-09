#!/usr/bin/env python3
"""
Progress Callback - Monitor deployment progress with custom callbacks.

This example demonstrates how to use custom progress callbacks to monitor
deployment status, useful for building custom UIs or logging systems.

Usage:
    export BASILICA_API_TOKEN="your-token"
    python3 16_progress_callback.py
"""
from basilica import BasilicaClient, DeploymentStatus


def custom_progress(status: DeploymentStatus) -> None:
    """Custom progress callback that formats output with timestamps and details."""
    import datetime

    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    phase = status.phase or "unknown"
    replicas = f"{status.replicas_ready}/{status.replicas_desired}"

    # Phase-specific messages
    phase_icons = {
        "pending": "...",
        "scheduling": "[~]",
        "pulling": "[v]",
        "initializing": "[*]",
        "storage_sync": "[S]",
        "starting": "[>]",
        "health_check": "[+]",
        "ready": "[ok]",
        "failed": "[X]",
    }
    icon = phase_icons.get(phase, "[?]")

    print(f"[{timestamp}] {icon} {phase} (replicas: {replicas})")

    # Show storage sync progress if available
    if status.phase == "storage_sync" and status.progress:
        if status.progress.percentage is not None:
            bar_width = 20
            filled = int(bar_width * status.progress.percentage / 100)
            bar = "#" * filled + "-" * (bar_width - filled)
            print(f"           [{bar}] {status.progress.percentage:.1f}%")


def main():
    client = BasilicaClient()

    print("=== Deployment with Custom Progress Callback ===\n")

    # Create deployment without waiting
    response = client.create_deployment(
        instance_name="progress-demo",
        image="python:3.11-slim",
        command=[
            "python",
            "-c",
            "from http.server import HTTPServer, BaseHTTPRequestHandler; "
            "HTTPServer(('', 8000), type('H', (BaseHTTPRequestHandler,), "
            "{'do_GET': lambda s: (s.send_response(200), s.end_headers(), "
            "s.wfile.write(b'Progress demo!'))})).serve_forever()",
        ],
        port=8000,
        ttl_seconds=300,
    )

    print(f"Deployment ID: {response.instance_name}")
    print(f"Waiting for deployment to be ready...\n")

    # Get Deployment object using the public API
    deployment = client.get(response.instance_name)

    try:
        status = deployment.wait_until_ready(
            timeout=120,
            poll_interval=3,
            on_progress=custom_progress,
        )
        print(f"\n[ok] Deployment ready!")
        print(f"     URL: {deployment.url}")
    except Exception as e:
        print(f"\n[X] Deployment failed: {e}")
        return

    # Test the deployment
    import urllib.request

    try:
        with urllib.request.urlopen(deployment.url, timeout=10) as resp:
            print(f"     Response: {resp.read().decode()}")
    except Exception as e:
        print(f"     Could not reach deployment: {e}")

    # Cleanup
    print("\nCleaning up...")
    deployment.delete()
    print("Done!")


if __name__ == "__main__":
    main()
