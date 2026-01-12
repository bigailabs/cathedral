#!/usr/bin/env python3
"""
Start CPU Rental Example for Basilica SDK

Demonstrates how to start and stop CPU-only rentals (no GPU) with secure cloud providers.
This script is interactive - it waits for the rental to be ready and lets you terminate it.
"""

import re
import sys
import time
from pathlib import Path
from typing import Optional

from basilica import BasilicaClient
from ssh_utils import format_ssh_command


def find_private_key_for_public_key(registered_public_key: str) -> Optional[str]:
    """
    Find the local private key that matches the registered public key.

    Searches ~/.ssh/*.pub files for a matching key (comparing key type and key data,
    ignoring the optional comment field).

    Args:
        registered_public_key: The registered SSH public key content
            Format: "key-type key-data [optional-comment]"

    Returns:
        Path to the matching private key, or None if not found
    """
    # Parse registered key - format: "key-type key-data [comment]"
    parts = registered_public_key.strip().split()
    if len(parts) < 2:
        return None
    registered_type, registered_data = parts[0], parts[1]

    # Search ~/.ssh/*.pub files
    ssh_dir = Path.home() / ".ssh"
    if not ssh_dir.exists():
        return None

    for pub_file in ssh_dir.glob("*.pub"):
        try:
            content = pub_file.read_text().strip()
            file_parts = content.split()
            if len(file_parts) >= 2:
                # Compare key type and key data (ignore comment)
                if file_parts[0] == registered_type and file_parts[1] == registered_data:
                    # Found match - return private key path (without .pub)
                    private_key = pub_file.with_suffix("")
                    if private_key.exists():
                        return str(private_key)
        except (IOError, OSError):
            continue

    return None


def ssh_command_to_credentials(ssh_command: str, fallback_host: Optional[str] = None) -> Optional[str]:
    """
    Convert SSH command string to credentials format for use with ssh_utils.

    Args:
        ssh_command: SSH command string (e.g., "ssh ubuntu@1.2.3.4 -p 22")
        fallback_host: Host to use if not found in command

    Returns:
        Credentials string (e.g., "ubuntu@1.2.3.4:22") or None if cannot parse
    """
    # Extract user@host pattern
    user_host_match = re.search(r'(\w+)@([\w\.\-]+)', ssh_command)
    if user_host_match:
        username = user_host_match.group(1)
        host = user_host_match.group(2)
    elif fallback_host:
        username = "root"
        host = fallback_host
    else:
        return None

    # Extract port from -p flag
    port_match = re.search(r'-p\s*(\d+)', ssh_command)
    port = int(port_match.group(1)) if port_match else 22

    return f"{username}@{host}:{port}"


def wait_for_rental_ready(client: BasilicaClient, rental_id: str, timeout: int = 300) -> dict:
    """
    Wait for a CPU rental to become ready with SSH access.

    Args:
        client: BasilicaClient instance
        rental_id: The rental ID to wait for
        timeout: Maximum time to wait in seconds (default: 5 minutes)

    Returns:
        The rental info dict when ready (with SSH command available)

    Raises:
        TimeoutError: If rental doesn't become ready within timeout
        RuntimeError: If rental fails
    """
    print(f"\nWaiting for rental {rental_id} to be ready...")
    start_time = time.time()
    last_status = None
    waiting_for_ssh = False

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise TimeoutError(f"Rental did not become ready within {timeout} seconds")

        # Find the rental in the list
        rentals_response = client.list_cpu_rentals()
        rental = None
        for r in rentals_response.rentals:
            if r.rental_id == rental_id:
                rental = r
                break

        if rental is None:
            raise RuntimeError(f"Rental {rental_id} not found")

        status = rental.status.lower()

        # Print status updates
        if status != last_status:
            print(f"  Status: {rental.status} (elapsed: {int(elapsed)}s)")
            last_status = status

        # Check terminal states
        if status == "running":
            # Also wait for SSH command to be available
            if rental.ssh_command:
                return rental
            elif not waiting_for_ssh:
                print(f"  Waiting for SSH access... (elapsed: {int(elapsed)}s)")
                waiting_for_ssh = True
        elif status in ("failed", "error", "terminated", "stopped"):
            raise RuntimeError(f"Rental failed with status: {rental.status}")

        # Wait before next poll
        time.sleep(3)


def main():
    print("=" * 60)
    print("  Basilica CPU Rental - Interactive Example")
    print("=" * 60)

    print("\nInitializing client...")

    # Initialize client connecting to local API server
    # Create a token using: basilica tokens create
    client = BasilicaClient(base_url="http://localhost:8000")

    # Step 1: Ensure SSH key is registered
    print("\n[Step 1] Checking SSH key registration...")
    ssh_key = client.get_ssh_key()
    if ssh_key is None:
        print("No SSH key registered. Registering from ~/.ssh/id_ed25519.pub...")
        ssh_key = client.register_ssh_key("my-key")
        print(f"Registered SSH key: {ssh_key.name} (ID: {ssh_key.id})")
    else:
        print(f"Using existing SSH key: {ssh_key.name} (ID: {ssh_key.id})")

    # Step 2: List available CPU offerings
    print("\n[Step 2] Fetching available CPU offerings...")
    offerings = client.list_cpu_offerings()

    if not offerings:
        print("No CPU offerings available at this time.")
        return

    print(f"\nFound {len(offerings)} CPU offerings:")
    for i, o in enumerate(offerings):
        print(
            f"  [{i}] {o.id}: {o.vcpu_count} vCPUs, {o.system_memory_gb}GB RAM, "
            f"{o.storage_gb}GB storage @ ${o.hourly_rate}/hr ({o.region})"
        )

    # Step 3: Start a rental with the first available offering
    offering = offerings[0]
    print(f"\n[Step 3] Starting CPU rental with offering: {offering.id}")
    print(f"  - vCPUs: {offering.vcpu_count}")
    print(f"  - RAM: {offering.system_memory_gb}GB")
    print(f"  - Storage: {offering.storage_gb}GB")
    print(f"  - Hourly rate: ${offering.hourly_rate}")

    rental = client.start_cpu_rental(
        offering_id=offering.id,
        # Optional: container configuration
        environment={"EXAMPLE_VAR": "hello_from_basilica"},
        # Optional: port mappings
        ports=[
            {"container_port": 22, "host_port": 22, "protocol": "tcp"},
        ],
    )

    print("\nRental request submitted!")
    print(f"  Rental ID: {rental.rental_id}")
    print(f"  Provider: {rental.provider}")
    print(f"  Hourly cost: ${rental.hourly_cost:.4f}")

    # Step 4: Wait for rental to be ready
    print("\n[Step 4] Waiting for rental to be ready...")
    try:
        ready_rental = wait_for_rental_ready(client, rental.rental_id)
    except (TimeoutError, RuntimeError) as e:
        print(f"\nError: {e}")
        print("Attempting to stop the rental...")
        try:
            client.stop_cpu_rental(rental.rental_id)
            print("Rental stopped.")
        except Exception as stop_error:
            print(f"Failed to stop rental: {stop_error}")
        return

    # Step 5: Display SSH credentials
    print("\n" + "=" * 60)
    print("  RENTAL READY")
    print("=" * 60)

    # Display all rental information
    print(f"\n  Rental ID: {ready_rental.rental_id}")
    print(f"  Status: {ready_rental.status}")
    print(f"  Provider: {ready_rental.provider}")

    if ready_rental.vcpu_count:
        print(f"  vCPUs: {ready_rental.vcpu_count}")
    if ready_rental.system_memory_gb:
        print(f"  RAM: {ready_rental.system_memory_gb}GB")
    if ready_rental.ip_address:
        print(f"  IP Address: {ready_rental.ip_address}")
    if ready_rental.hourly_cost:
        print(f"  Hourly Cost: ${ready_rental.hourly_cost:.4f}")

    # Find the matching private key from ~/.ssh/
    private_key_path = None
    if ssh_key and ssh_key.public_key:
        private_key_path = find_private_key_for_public_key(ssh_key.public_key)

    # Convert SSH command to credentials format for use with ssh_utils
    credentials = ssh_command_to_credentials(
        ready_rental.ssh_command,
        fallback_host=ready_rental.ip_address
    )

    # Display SSH connection info
    print(f"\n  SSH Connection:")
    if credentials:
        ssh_cmd = format_ssh_command(credentials, private_key_path)
        print(f"    {ssh_cmd}")
        if not private_key_path:
            print(f"\n  Note: Could not find matching private key in ~/.ssh/")
            print(f"        Update the -i flag with your private key path.")
    else:
        # Fallback to raw ssh_command
        print(f"    {ready_rental.ssh_command}")

    if private_key_path:
        print(f"\n  Private Key: {private_key_path}")

    print("\n" + "-" * 60)

    # Step 6: Interactive - wait for user to terminate
    print("\nThe rental is now running. You can SSH into the machine using the command above.")
    print("Press Enter to terminate the rental (or Ctrl+C to exit without terminating)...")

    try:
        input()
    except KeyboardInterrupt:
        print("\n\nExiting without terminating the rental.")
        print(f"To manually stop later, run: client.stop_cpu_rental('{rental.rental_id}')")
        return

    # Step 7: Stop the rental
    print("\n[Step 7] Stopping rental...")
    try:
        result = client.stop_cpu_rental(rental.rental_id)
        print("\n" + "=" * 60)
        print("  RENTAL STOPPED")
        print("=" * 60)
        print(f"\n  Rental ID: {result.rental_id}")
        print(f"  Status: {result.status}")
        print(f"  Duration: {result.duration_hours:.4f} hours")
        print(f"  Total cost: ${result.total_cost:.4f}")
    except Exception as e:
        print(f"Error stopping rental: {e}")
        sys.exit(1)

    print("\nDone!")


if __name__ == "__main__":
    main()
