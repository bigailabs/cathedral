"""MTU command for clustermgr - validate MTU settings across interfaces."""

import re

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import print_header, print_status, run_ansible, Severity

console = Console()

# Expected MTU values for different interfaces
EXPECTED_MTU = {
    "eth0": 1500,      # Physical interface
    "ens": 1500,       # Physical interface (cloud naming)
    "wg0": 1420,       # WireGuard (1500 - 80 overhead)
    "flannel": 1370,   # Flannel VXLAN over WireGuard (1420 - 50)
    "cni0": 1370,      # CNI bridge
    "veth": 1370,      # Pod veth interfaces
}


def _get_interface_mtus(config: Config) -> dict[str, list[dict]]:
    """Get MTU values for all interfaces on all servers."""
    result = run_ansible(
        config,
        "shell",
        "ip -o link show | awk '{print $2, $5}' | tr -d ':'",
        timeout=30,
    )

    servers: dict[str, list[dict]] = {}
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            servers[current_server] = []
        elif current_server and line.strip():
            parts = line.strip().split()
            if len(parts) >= 2:
                iface = parts[0]
                try:
                    mtu = int(parts[1])
                    servers[current_server].append({"interface": iface, "mtu": mtu})
                except ValueError:
                    pass

    return servers


def _get_expected_mtu(interface: str) -> int | None:
    """Get expected MTU for an interface based on its name prefix."""
    for prefix, expected in EXPECTED_MTU.items():
        if interface.startswith(prefix):
            return expected
    return None


def _test_path_mtu(config: Config, target_ip: str, mtu: int) -> dict[str, bool]:
    """Test path MTU to a target IP from all servers."""
    # Send ICMP with DF bit set, payload = mtu - 28 (IP + ICMP headers)
    payload_size = mtu - 28
    result = run_ansible(
        config,
        "shell",
        f"ping -c 1 -W 2 -M do -s {payload_size} {target_ip} 2>&1 | grep -q 'bytes from' && echo OK || echo FAIL",
        timeout=30,
    )

    servers: dict[str, bool] = {}
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server:
            if "OK" in line:
                servers[current_server] = True
            elif "FAIL" in line:
                servers[current_server] = False

    return servers


@click.command()
@click.option("--test-path", "-t", help="Test path MTU to a specific IP")
@click.option("--mtu-size", "-m", type=int, default=1392, help="MTU size to test (default: 1392)")
@click.option("--verbose", "-v", is_flag=True, help="Show all interfaces")
@click.pass_context
def mtu(
    ctx: click.Context,
    test_path: str | None,
    mtu_size: int,
    verbose: bool,
) -> None:
    """Validate MTU settings across network interfaces."""
    config: Config = ctx.obj

    print_header("MTU Validation")

    servers = _get_interface_mtus(config)
    if not servers:
        console.print("[red]Failed to retrieve interface information[/red]")
        ctx.exit(1)

    issues: list[dict] = []

    # Key interfaces to always show
    key_interfaces = {"eth0", "ens3", "ens5", "wg0", "flannel.1", "cni0"}

    # Check each server
    for server, interfaces in servers.items():
        for iface_info in interfaces:
            iface = iface_info["interface"]
            actual_mtu = iface_info["mtu"]
            expected = _get_expected_mtu(iface)

            if expected and actual_mtu != expected:
                issues.append({
                    "server": server,
                    "interface": iface,
                    "actual": actual_mtu,
                    "expected": expected,
                })

    # Summary table
    table = Table(title="Interface MTU Status")
    table.add_column("Server", style="cyan")
    table.add_column("Interface")
    table.add_column("MTU", justify="right")
    table.add_column("Expected", justify="right")
    table.add_column("Status")

    for server, interfaces in servers.items():
        for iface_info in interfaces:
            iface = iface_info["interface"]
            actual_mtu = iface_info["mtu"]
            expected = _get_expected_mtu(iface)

            # Skip non-key interfaces unless verbose or there's an issue
            is_key = any(iface.startswith(k) for k in key_interfaces)
            has_issue = expected and actual_mtu != expected
            if not verbose and not is_key and not has_issue:
                continue

            if expected:
                if actual_mtu == expected:
                    status = "[green]OK[/green]"
                else:
                    status = "[red]MISMATCH[/red]"
                exp_str = str(expected)
            else:
                status = "[dim]-[/dim]"
                exp_str = "-"

            table.add_row(server, iface, str(actual_mtu), exp_str, status)

    console.print(table)

    # Show issues
    if issues:
        print_header("MTU Issues")
        for issue in issues:
            console.print(
                f"[red]MISMATCH[/red] {issue['server']}:{issue['interface']} - "
                f"actual={issue['actual']}, expected={issue['expected']}"
            )

        console.print("\n[bold]Remediation:[/bold]")
        console.print("  For WireGuard (wg0): Set MTU=1420 in /etc/wireguard/wg0.conf")
        console.print("  For Flannel: Set --iface-mtu in k3s/flannel configuration")
        console.print("  Manual fix: ip link set <interface> mtu <value>")

    # Test path MTU if requested
    if test_path:
        print_header(f"Path MTU Test to {test_path}")
        console.print(f"Testing with payload size {mtu_size - 28} (MTU {mtu_size} - 28 byte headers)")

        results = _test_path_mtu(config, test_path, mtu_size)
        for server, success in sorted(results.items()):
            if success:
                print_status(server, f"MTU {mtu_size} OK", Severity.HEALTHY)
            else:
                print_status(server, f"MTU {mtu_size} blocked (fragmentation needed)", Severity.WARNING)

    # Summary
    if issues:
        console.print(f"\n[bold]Summary:[/bold] {len(issues)} MTU mismatches found")
        ctx.exit(1)
    else:
        console.print("\n[green]All MTU values are correctly configured[/green]")
