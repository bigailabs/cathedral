#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "httpx",
#     "rich",
# ]
# ///
"""Interactive script to debug and manage Hyperstack VMs."""

import os
import sys
import json

import httpx
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.panel import Panel
from rich.syntax import Syntax

BASE_URL = "https://infrahub-api.nexgencloud.com/v1"
CALLBACK_URL_BASE = "https://api.basilica.ai/webhooks/cloud-provider/hyperstack"
console = Console()
DEBUG = False


def get_api_key() -> str:
    """Get API key from environment or prompt user."""
    api_key = os.environ.get("HYPERSTACK_API_KEY")
    if api_key:
        console.print("[dim]Using API key from HYPERSTACK_API_KEY environment variable[/dim]")
        return api_key

    api_key = Prompt.ask("Enter your Hyperstack API key", password=True)
    if not api_key:
        console.print("[red]API key is required[/red]")
        sys.exit(1)
    return api_key


def get_callback_token() -> str:
    """Get callback token from environment or command-line argument or prompt."""
    # Check command-line argument (--callback-token=xxx)
    for arg in sys.argv[1:]:
        if arg.startswith("--callback-token="):
            return arg.split("=", 1)[1]

    # Check environment variable
    token = os.environ.get("BASILICA_CALLBACK_TOKEN")
    if token:
        console.print("[dim]Using token from BASILICA_CALLBACK_TOKEN environment variable[/dim]")
        return token

    # Prompt user
    token = Prompt.ask("Enter callback token", password=True)
    if not token:
        console.print("[red]Callback token is required[/red]")
        sys.exit(1)
    return token


def build_callback_url(token: str) -> str:
    """Build the full callback URL with token."""
    return f"{CALLBACK_URL_BASE}?token={token}"


def list_vms(client: httpx.Client) -> list[dict]:
    """Fetch all VMs from Hyperstack API."""
    response = client.get(f"{BASE_URL}/core/virtual-machines")
    response.raise_for_status()
    data = response.json()

    if DEBUG:
        console.print(f"[dim]Raw API response keys: {list(data.keys())}[/dim]")
        console.print(f"[dim]{json.dumps(data, indent=2)[:2000]}[/dim]")

    if not data.get("status"):
        console.print(f"[red]API error: {data.get('message', 'Unknown error')}[/red]")
        return []

    # Try different possible keys for VM list
    vms = data.get("virtual_machines") or data.get("instances") or data.get("data") or []
    return vms


def get_vm_by_id(client: httpx.Client, vm_id: int) -> dict | None:
    """Fetch a single VM by ID."""
    try:
        response = client.get(f"{BASE_URL}/core/virtual-machines/{vm_id}")
        response.raise_for_status()
        data = response.json()

        if DEBUG:
            console.print(f"[dim]Raw API response:[/dim]")
            console.print(Syntax(json.dumps(data, indent=2), "json", theme="monokai"))

        if not data.get("status"):
            console.print(f"[red]API error: {data.get('message', 'Unknown error')}[/red]")
            return None

        return data.get("virtual_machine") or data.get("instance") or data
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error: {e.response.status_code}[/red]")
        if e.response.status_code == 404:
            console.print(f"[yellow]VM with ID {vm_id} not found[/yellow]")
        return None


def delete_vm(client: httpx.Client, vm_id: int, vm_name: str) -> bool:
    """Delete a VM by ID. Returns True if successful."""
    try:
        response = client.delete(f"{BASE_URL}/core/virtual-machines/{vm_id}")
        response.raise_for_status()
        data = response.json()

        if data.get("status"):
            console.print(f"[green]Successfully deleted VM '{vm_name}' (ID: {vm_id})[/green]")
            return True
        else:
            console.print(f"[red]Failed to delete VM '{vm_name}': {data.get('message', 'Unknown error')}[/red]")
            return False
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error deleting VM '{vm_name}': {e.response.status_code}[/red]")
        return False


def attach_callback(client: httpx.Client, vm_id: int, vm_name: str, callback_url: str) -> bool:
    """Attach a callback URL to a VM. Returns True if successful."""
    try:
        response = client.post(
            f"{BASE_URL}/core/virtual-machines/{vm_id}/attach-callback",
            json={"url": callback_url}
        )
        response.raise_for_status()
        data = response.json()

        if data.get("status"):
            console.print(f"[green]Callback attached to '{vm_name}' (ID: {vm_id})[/green]")
            return True
        else:
            console.print(f"[red]Failed for '{vm_name}': {data.get('message', 'Unknown error')}[/red]")
            return False
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error for '{vm_name}': {e.response.status_code}[/red]")
        return False


def display_vms(vms: list[dict], title: str = "Virtual Machines") -> None:
    """Display VMs in a formatted table."""
    table = Table(title=title)
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Name", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Flavor", style="blue")
    table.add_column("Environment", style="magenta")
    table.add_column("IP", style="white")

    for vm in vms:
        ip = vm.get("floating_ip") or vm.get("fixed_ip") or "-"
        table.add_row(
            str(vm.get("id", "-")),
            vm.get("name", "-"),
            vm.get("status", "-"),
            vm.get("flavor", {}).get("name", "-") if isinstance(vm.get("flavor"), dict) else vm.get("flavor_name", "-"),
            vm.get("environment", {}).get("name", "-") if isinstance(vm.get("environment"), dict) else vm.get("environment_name", "-"),
            ip,
        )

    console.print(table)


def display_vm_details(vm: dict) -> None:
    """Display detailed information about a single VM."""
    # Basic info table
    table = Table(title=f"VM Details: {vm.get('name', 'Unknown')}", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("ID", str(vm.get("id", "-")))
    table.add_row("Name", vm.get("name", "-"))
    table.add_row("Status", f"[{'green' if vm.get('status') == 'ACTIVE' else 'yellow'}]{vm.get('status', '-')}[/]")
    table.add_row("Power State", vm.get("power_state", "-"))
    table.add_row("VM State", vm.get("vm_state", "-"))

    # Flavor info
    flavor = vm.get("flavor", {})
    if isinstance(flavor, dict):
        table.add_row("Flavor", flavor.get("name", "-"))
        table.add_row("  - CPU", str(flavor.get("cpu", "-")))
        table.add_row("  - RAM", f"{flavor.get('ram', '-')} GB")
        table.add_row("  - Disk", f"{flavor.get('disk', '-')} GB")
        table.add_row("  - GPU", f"{flavor.get('gpu', '-')} x {flavor.get('gpu_name', '-')}")
    else:
        table.add_row("Flavor", vm.get("flavor_name", "-"))

    # Environment
    env = vm.get("environment", {})
    if isinstance(env, dict):
        table.add_row("Environment", env.get("name", "-"))
        table.add_row("Region", env.get("region", "-"))
    else:
        table.add_row("Environment", vm.get("environment_name", "-"))

    # Network
    table.add_row("Floating IP", vm.get("floating_ip") or "-")
    table.add_row("Fixed IP", vm.get("fixed_ip") or "-")

    # Timestamps
    table.add_row("Created", vm.get("created_at", "-"))
    table.add_row("Updated", vm.get("updated_at", "-"))

    console.print(table)

    # Show raw JSON if debug mode
    if DEBUG:
        console.print("\n[dim]Raw JSON:[/dim]")
        console.print(Syntax(json.dumps(vm, indent=2, default=str), "json", theme="monokai"))


def action_list_vms(client: httpx.Client) -> None:
    """List all VMs."""
    console.print("\n[dim]Fetching VMs...[/dim]")
    vms = list_vms(client)

    if not vms:
        console.print("[yellow]No virtual machines found.[/yellow]")
        return

    display_vms(vms, "All Virtual Machines")
    console.print(f"\n[bold]Found {len(vms)} VM(s)[/bold]")


def action_get_vm_status(client: httpx.Client) -> None:
    """Get status of a specific VM by ID."""
    vm_id_str = Prompt.ask("Enter VM ID")
    try:
        vm_id = int(vm_id_str)
    except ValueError:
        console.print("[red]Invalid VM ID. Must be an integer.[/red]")
        return

    console.print(f"\n[dim]Fetching VM {vm_id}...[/dim]")
    vm = get_vm_by_id(client, vm_id)

    if vm:
        display_vm_details(vm)


def action_delete_vms(client: httpx.Client) -> None:
    """Delete VMs interactively."""
    console.print("\n[dim]Fetching VMs...[/dim]")
    vms = list_vms(client)

    if not vms:
        console.print("[yellow]No virtual machines found.[/yellow]")
        return

    display_vms(vms, "All Virtual Machines")
    console.print(f"\n[bold]Found {len(vms)} VM(s)[/bold]\n")

    if Confirm.ask("[red]Delete ALL VMs without confirmation for each?[/red]", default=False):
        deleted = 0
        for vm in vms:
            if delete_vm(client, vm["id"], vm["name"]):
                deleted += 1
        console.print(f"\n[bold green]Deleted {deleted}/{len(vms)} VMs[/bold green]")
        return

    console.print("\n[dim]For each VM: [y]es to delete, [n]o to skip, [a]ll to delete remaining, [q]uit[/dim]\n")

    deleted = 0
    skipped = 0
    delete_all = False

    for vm in vms:
        vm_id = vm["id"]
        vm_name = vm["name"]
        status = vm.get("status", "unknown")
        ip = vm.get("floating_ip") or vm.get("fixed_ip") or "no IP"

        if delete_all:
            if delete_vm(client, vm_id, vm_name):
                deleted += 1
            continue

        console.print(f"[bold]{vm_name}[/bold] (ID: {vm_id}, Status: {status}, IP: {ip})")
        choice = Prompt.ask("  [red]Delete[/red] this VM?", choices=["y", "n", "a", "q"], default="n")

        if choice == "q":
            console.print("[yellow]Quitting...[/yellow]")
            break
        elif choice == "a":
            delete_all = True
            if delete_vm(client, vm_id, vm_name):
                deleted += 1
        elif choice == "y":
            if delete_vm(client, vm_id, vm_name):
                deleted += 1
        else:
            skipped += 1
            console.print("  [dim]Skipped[/dim]")

    console.print(f"\n[bold]Summary:[/bold] Deleted {deleted}, Skipped {skipped}")


def action_set_callbacks(client: httpx.Client) -> None:
    """Set callback URL for VMs."""
    console.print("\n[dim]Fetching VMs...[/dim]")
    vms = list_vms(client)

    if not vms:
        console.print("[yellow]No virtual machines found.[/yellow]")
        return

    display_vms(vms, "All Virtual Machines")
    console.print(f"\n[bold]Found {len(vms)} VM(s)[/bold]\n")

    # Get token and build callback URL
    token = get_callback_token()
    callback_url = build_callback_url(token)

    # Show URL with masked token for security
    masked_url = f"{CALLBACK_URL_BASE}?token=****{token[-4:]}" if len(token) > 4 else f"{CALLBACK_URL_BASE}?token=****"
    console.print(f"[cyan]Callback URL: {masked_url}[/cyan]\n")

    if Confirm.ask(f"[yellow]Set callback for ALL {len(vms)} VMs?[/yellow]", default=True):
        success = 0
        for vm in vms:
            if attach_callback(client, vm["id"], vm["name"], callback_url):
                success += 1
        console.print(f"\n[bold green]Callback set for {success}/{len(vms)} VMs[/bold green]")
    else:
        console.print("[dim]Cancelled[/dim]")


def show_menu() -> str:
    """Display main menu and get user choice."""
    console.print("\n[bold]Available Actions:[/bold]")
    console.print("  [cyan]1[/cyan] - List all VMs")
    console.print("  [cyan]2[/cyan] - Get VM status by ID")
    console.print("  [cyan]3[/cyan] - Delete VMs (interactive)")
    console.print("  [cyan]4[/cyan] - Set callback URL for all VMs")
    console.print("  [cyan]q[/cyan] - Quit")
    return Prompt.ask("\nSelect action", choices=["1", "2", "3", "4", "q"], default="1")


def main():
    global DEBUG
    DEBUG = "--debug" in sys.argv

    console.print(Panel.fit(
        "[bold blue]Hyperstack API Debugger[/bold blue]\n"
        "[dim]Debug and manage Hyperstack virtual machines[/dim]",
        border_style="blue"
    ))

    api_key = get_api_key()

    headers = {
        "api_key": api_key,
        "accept": "application/json",
    }

    with httpx.Client(headers=headers, timeout=30.0) as client:
        while True:
            choice = show_menu()

            if choice == "q":
                console.print("[dim]Goodbye![/dim]")
                break
            elif choice == "1":
                action_list_vms(client)
            elif choice == "2":
                action_get_vm_status(client)
            elif choice == "3":
                action_delete_vms(client)
            elif choice == "4":
                action_set_callbacks(client)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
        sys.exit(130)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP error: {e.response.status_code} - {e.response.text}[/red]")
        sys.exit(1)
