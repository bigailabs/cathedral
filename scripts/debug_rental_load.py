#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "aiohttp",
# ]
# ///
"""
Debug script to replicate rental load scenarios.

Starts multiple secure-cloud rentals in parallel, waits for them to become active,
then stops them all in parallel. Useful for debugging cases where API stops succeed
but Hyperstack VMs remain active.

Usage:
    ./scripts/debug_rental_load.py --count 3
    ./scripts/debug_rental_load.py --count 5 --offering-id <id>
    ./scripts/debug_rental_load.py --env dev --api-url http://localhost:8000
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: Optional[str] = None


@dataclass
class GpuOffering:
    id: str
    provider: str
    gpu_type: str
    gpu_count: int
    gpu_memory_gb_per_gpu: Optional[int]
    hourly_rate_per_gpu: float
    region: str
    availability: bool
    vcpu_count: int
    system_memory_gb: int


@dataclass
class RentalInfo:
    rental_id: str
    deployment_id: str
    provider: str
    status: str
    ip_address: Optional[str]
    hourly_cost: float


@dataclass
class StopResult:
    rental_id: str
    status: str
    duration_hours: float
    total_cost: float
    error: Optional[str] = None


def load_token(env: str) -> TokenSet:
    """Load OAuth token from CLI's token file."""
    data_dir = Path.home() / ".local" / "share" / "basilica"
    token_file = data_dir / ("auth.dev.json" if env == "dev" else "auth.json")

    if not token_file.exists():
        print(f"Error: Token file not found at {token_file}")
        print("Please login first using: basilica login")
        sys.exit(1)

    with open(token_file) as f:
        data = json.load(f)

    return TokenSet(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        expires_at=data.get("expires_at"),
    )


class BasilicaClient:
    def __init__(self, base_url: str, token: TokenSet):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self.token.access_token}"},
            timeout=aiohttp.ClientTimeout(total=60),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._session:
            await self._session.close()

    async def _get(self, path: str) -> dict:
        async with self._session.get(f"{self.base_url}{path}") as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"GET {path} failed ({resp.status}): {text}")
            return await resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        async with self._session.post(f"{self.base_url}{path}", json=data) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                raise Exception(f"POST {path} failed ({resp.status}): {text}")
            return await resp.json()

    async def list_gpu_offerings(self) -> list[GpuOffering]:
        """List available GPU offerings sorted by price."""
        resp = await self._get("/secure-cloud/gpu-prices?available_only=true")
        offerings = []
        for node in resp.get("nodes", []):
            offerings.append(
                GpuOffering(
                    id=node["id"],
                    provider=node["provider"],
                    gpu_type=node["gpu_type"],
                    gpu_count=node["gpu_count"],
                    gpu_memory_gb_per_gpu=node.get("gpu_memory_gb_per_gpu"),
                    hourly_rate_per_gpu=float(node["hourly_rate_per_gpu"]),
                    region=node["region"],
                    availability=node["availability"],
                    vcpu_count=node.get("vcpu_count", 0),
                    system_memory_gb=node.get("system_memory_gb", 0),
                )
            )
        # Sort by total hourly cost (rate * count)
        offerings.sort(key=lambda o: o.hourly_rate_per_gpu * o.gpu_count)
        return offerings

    async def get_ssh_key(self) -> Optional[dict]:
        """Get user's registered SSH key."""
        try:
            return await self._get("/ssh-keys")
        except Exception:
            return None

    async def start_rental(self, offering_id: str, ssh_key_id: str) -> RentalInfo:
        """Start a secure cloud rental."""
        resp = await self._post(
            "/secure-cloud/rentals/start",
            {
                "offering_id": offering_id,
                "ssh_public_key_id": ssh_key_id,
                "container_image": None,
                "environment": {},
                "ports": [],
            },
        )
        return RentalInfo(
            rental_id=resp["rental_id"],
            deployment_id=resp["deployment_id"],
            provider=resp["provider"],
            status=resp["status"],
            ip_address=resp.get("ip_address"),
            hourly_cost=resp["hourly_cost"],
        )

    async def get_rental_status(self, rental_id: str) -> str:
        """Get rental status."""
        resp = await self._get("/secure-cloud/rentals")
        for rental in resp.get("rentals", []):
            if rental["rental_id"] == rental_id:
                return rental["status"]
        return "not_found"

    async def list_rentals(self) -> list[dict]:
        """List all active secure cloud rentals."""
        resp = await self._get("/secure-cloud/rentals")
        return resp.get("rentals", [])

    async def stop_rental(self, rental_id: str) -> StopResult:
        """Stop a secure cloud rental."""
        try:
            resp = await self._post(f"/secure-cloud/rentals/{rental_id}/stop", {})
            return StopResult(
                rental_id=resp["rental_id"],
                status=resp["status"],
                duration_hours=resp["duration_hours"],
                total_cost=resp["total_cost"],
            )
        except Exception as e:
            return StopResult(
                rental_id=rental_id,
                status="error",
                duration_hours=0,
                total_cost=0,
                error=str(e),
            )


def display_offerings(offerings: list[GpuOffering]) -> None:
    """Display GPU offerings in a table format."""
    print("\n" + "=" * 100)
    print("Available GPU Offerings (sorted by price)")
    print("=" * 100)
    print(
        f"{'#':<4} {'ID':<40} {'Provider':<12} {'GPU':<10} {'Count':<6} {'VRAM':<6} {'$/hr':<8} {'Region':<15}"
    )
    print("-" * 100)

    for i, o in enumerate(offerings, 1):
        vram = f"{o.gpu_memory_gb_per_gpu}GB" if o.gpu_memory_gb_per_gpu else "N/A"
        total_cost = o.hourly_rate_per_gpu * o.gpu_count
        print(
            f"{i:<4} {o.id:<40} {o.provider:<12} {o.gpu_type:<10} {o.gpu_count:<6} {vram:<6} ${total_cost:<7.2f} {o.region:<15}"
        )
    print("=" * 100)


@dataclass
class RentalState:
    """Tracks rental state during polling."""

    rental_id: str
    is_running: bool
    final_status: str


async def poll_until_running(
    client: BasilicaClient,
    rental_id: str,
    poll_interval: int = 5,
) -> RentalState:
    """Poll until rental reaches running status (no timeout)."""
    last_status = "unknown"
    while True:
        status = await client.get_rental_status(rental_id)
        if status != last_status:
            print(f"  [{rental_id[:8]}...] {status}")
            last_status = status
        if status == "running":
            return RentalState(rental_id=rental_id, is_running=True, final_status=status)
        if status in ("error", "deleted", "not_found"):
            return RentalState(
                rental_id=rental_id, is_running=False, final_status=status
            )
        await asyncio.sleep(poll_interval)


async def stop_rentals_parallel(
    client: BasilicaClient, rental_ids: list[str]
) -> list[StopResult]:
    """Stop multiple rentals in parallel and return results."""
    stop_tasks = [client.stop_rental(rental_id) for rental_id in rental_ids]
    return await asyncio.gather(*stop_tasks)


async def main():
    parser = argparse.ArgumentParser(description="Debug rental load testing script")
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        help="Number of rentals to start (default: 3)",
    )
    parser.add_argument(
        "--offering-id",
        type=str,
        help="GPU offering ID to use (if not provided, will prompt)",
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default="prod",
        help="Environment (default: prod)",
    )
    parser.add_argument(
        "--api-url",
        type=str,
        help="Override API base URL",
    )
    args = parser.parse_args()

    # Determine API URL
    if args.api_url:
        api_url = args.api_url
    elif args.env == "dev":
        api_url = "http://localhost:8000"
    else:
        api_url = "https://api.basilica.ai"

    # Use dev auth for localhost URLs unless explicitly overridden
    auth_env = args.env
    if args.api_url and "localhost" in args.api_url:
        auth_env = "dev"

    print(f"Loading token for {auth_env} environment...")
    token = load_token(auth_env)

    async with BasilicaClient(api_url, token) as client:
        # Check for existing rentals first
        print("Checking for existing rentals...")
        existing_rentals = await client.list_rentals()
        if existing_rentals:
            print(f"\nFound {len(existing_rentals)} existing rentals (previous run may have failed):")
            for r in existing_rentals:
                print(f"  - {r['rental_id']}: {r['status']} ({r.get('provider', 'unknown')})")

            confirm = input("\nStop all existing rentals before continuing? [Y/n]: ").strip().lower()
            if confirm != "n":
                print(f"\nStopping {len(existing_rentals)} existing rentals...")
                rental_ids = [r["rental_id"] for r in existing_rentals]
                stop_results = await stop_rentals_parallel(client, rental_ids)

                for result in stop_results:
                    if result.error:
                        print(f"  {result.rental_id}: ERROR - {result.error}")
                    else:
                        print(f"  {result.rental_id}: {result.status}")

                print("Existing rentals cleaned up.\n")

        # Get SSH key
        print("Fetching SSH key...")
        ssh_key = await client.get_ssh_key()
        if not ssh_key:
            print("Error: No SSH key registered. Please register one first:")
            print("  basilica ssh-keys add <name> <public_key_file>")
            sys.exit(1)
        ssh_key_id = ssh_key["id"]
        print(f"Using SSH key: {ssh_key['name']} ({ssh_key_id})")

        # Get offering ID
        offering_id = args.offering_id
        if not offering_id:
            print("Fetching GPU offerings...")
            offerings = await client.list_gpu_offerings()
            if not offerings:
                print("Error: No GPU offerings available")
                sys.exit(1)

            display_offerings(offerings)

            while True:
                try:
                    choice = input(
                        f"\nSelect offering number (1-{len(offerings)}): "
                    ).strip()
                    idx = int(choice) - 1
                    if 0 <= idx < len(offerings):
                        offering_id = offerings[idx].id
                        selected = offerings[idx]
                        print(
                            f"\nSelected: {selected.provider} {selected.gpu_type} x{selected.gpu_count} @ ${selected.hourly_rate_per_gpu * selected.gpu_count:.2f}/hr"
                        )
                        break
                    print("Invalid selection")
                except ValueError:
                    print("Please enter a number")

        # Confirm
        total_hourly = 0
        if not args.offering_id:
            total_hourly = (
                offerings[idx].hourly_rate_per_gpu * offerings[idx].gpu_count
            )
        print(f"\nAbout to start {args.count} rentals using offering: {offering_id}")
        if total_hourly:
            print(f"Estimated cost: ${total_hourly * args.count:.2f}/hr total")
        confirm = input("Continue? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted")
            sys.exit(0)

        # Start rentals in parallel
        print(f"\n{'='*60}")
        print(f"Starting {args.count} rentals in parallel...")
        print("=" * 60)

        start_tasks = [
            client.start_rental(offering_id, ssh_key_id) for _ in range(args.count)
        ]
        start_results = await asyncio.gather(*start_tasks, return_exceptions=True)

        rentals: list[RentalInfo] = []
        for i, result in enumerate(start_results):
            if isinstance(result, Exception):
                print(f"  Rental {i+1}: FAILED - {result}")
            else:
                rentals.append(result)
                print(
                    f"  Rental {i+1}: {result.rental_id} ({result.provider}) - {result.status}"
                )

        if not rentals:
            print("\nNo rentals started successfully")
            sys.exit(1)

        # Wait for all rentals to reach running status
        print(f"\n{'='*60}")
        print(f"Waiting for {len(rentals)} rentals to become running...")
        print("=" * 60)

        poll_tasks = [
            poll_until_running(client, r.rental_id) for r in rentals
        ]
        poll_results: list[RentalState] = await asyncio.gather(*poll_tasks)

        running_rentals = [r for r in poll_results if r.is_running]
        not_running_rentals = [r for r in poll_results if not r.is_running]

        print(f"\n{len(running_rentals)}/{len(rentals)} rentals reached running status")

        if not_running_rentals:
            print(f"\nRentals NOT running (will still be stopped):")
            for r in not_running_rentals:
                print(f"  - {r.rental_id}: {r.final_status}")

        # Prompt before stopping
        input(f"\nPress Enter to stop all {len(poll_results)} rentals...")

        # Stop all rentals in parallel (including failed ones)
        print(f"\n{'='*60}")
        print(f"Stopping all {len(poll_results)} rentals in parallel...")
        print("=" * 60)

        stop_tasks = [client.stop_rental(r.rental_id) for r in poll_results]
        stop_results = await asyncio.gather(*stop_tasks)

        # Report results
        print(f"\n{'='*60}")
        print("Stop Results")
        print("=" * 60)
        total_cost = 0
        errors = []
        for result in stop_results:
            if result.error:
                print(f"  {result.rental_id}: ERROR - {result.error}")
                errors.append(result)
            else:
                print(
                    f"  {result.rental_id}: {result.status} (duration: {result.duration_hours:.3f}h, cost: ${result.total_cost:.4f})"
                )
                total_cost += result.total_cost

        print(f"\n{'='*60}")
        print(f"Summary: {len(stop_results) - len(errors)}/{len(stop_results)} stopped successfully")
        print(f"Total cost: ${total_cost:.4f}")
        if errors:
            print(f"Errors: {len(errors)}")
            for e in errors:
                print(f"  - {e.rental_id}: {e.error}")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
