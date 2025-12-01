"""Mesh-test command for clustermgr - full mesh connectivity testing."""

import re
from dataclasses import dataclass

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import print_header, run_ansible, Severity, print_status

console = Console()


@dataclass
class PingResult:
    """Result of a ping test."""
    source: str
    target: str
    target_ip: str
    success: bool
    latency_ms: float | None = None
    packet_loss: float = 0.0


def _get_server_wg_ips(config: Config) -> dict[str, str]:
    """Get WireGuard IPs for Ansible-managed servers."""
    result = run_ansible(
        config,
        "shell",
        "ip -4 addr show wg0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d'/' -f1",
        timeout=30,
    )

    servers: dict[str, str] = {}
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and line.strip().startswith("10.200"):
            servers[current_server] = line.strip()

    return servers


def _get_all_wireguard_peers(config: Config) -> dict[str, str]:
    """Get all WireGuard peers including remote GPU nodes.

    Returns dict mapping peer_name -> wg_ip
    """
    # Get peer info with names from config comments
    result = run_ansible(
        config,
        "shell",
        (
            "sudo wg show wg0 dump 2>/dev/null | tail -n +2 | "
            "while read line; do "
            "  pubkey=$(echo \"$line\" | cut -f1); "
            "  allowed=$(echo \"$line\" | cut -f4 | cut -d'/' -f1); "
            "  name=$(sudo grep -B1 \"$pubkey\" /etc/wireguard/wg0.conf 2>/dev/null | grep '# Node:' | cut -d: -f2 | tr -d ' '); "
            "  echo \"PEER:$name:$allowed\"; "
            "done"
        ),
        timeout=30,
    )

    all_peers: dict[str, str] = {}

    for line in result.stdout.split("\n"):
        if line.startswith("PEER:"):
            parts = line.split(":")
            if len(parts) >= 3:
                name = parts[1].strip()
                ip = parts[2].strip()
                if ip.startswith("10.200") and ip not in all_peers.values():
                    # Use name if available, otherwise use IP as key
                    key = name if name else f"peer-{ip}"
                    all_peers[key] = ip

    return all_peers


def _get_combined_targets(config: Config) -> dict[str, str]:
    """Get combined list of all test targets (servers + GPU peers).

    Returns dict mapping name -> wg_ip
    """
    # Get server IPs (these are the Ansible-managed nodes)
    servers = _get_server_wg_ips(config)

    # Get all WireGuard peers (including GPU nodes)
    all_peers = _get_all_wireguard_peers(config)

    # Combine, preferring server names over peer names for overlaps
    combined: dict[str, str] = {}

    # Add all peers first
    for name, ip in all_peers.items():
        combined[name] = ip

    # Add/update with server info (servers have authoritative names)
    for name, ip in servers.items():
        # Remove any peer entry with same IP but different name
        to_remove = [k for k, v in combined.items() if v == ip and k != name]
        for k in to_remove:
            del combined[k]
        combined[name] = ip

    return combined


def _run_mesh_ping(config: Config, targets: dict[str, str], count: int = 3) -> list[PingResult]:
    """Run ping tests from each server to its actual WireGuard peers.

    Only tests connectivity to peers that exist in each server's WireGuard config,
    not all possible targets.
    """
    results: list[PingResult] = []

    # Get peer info with names, ping only actual peers for each server
    # This discovers each server's actual WG peers and pings them
    ping_cmd = (
        "for ip in $(sudo wg show wg0 2>/dev/null | grep 'allowed ips' | "
        "awk '{print $3}' | cut -d'/' -f1); do "
        f"  name=$(sudo grep -B1 \"$ip\" /etc/wireguard/wg0.conf 2>/dev/null | grep '# Node:' | cut -d: -f2 | tr -d ' '); "
        f'  output=$(ping -c {count} -W 2 $ip 2>&1 | tail -2); '
        '  echo "TARGET:$ip:$name:$output"; '
        "done"
    )

    result = run_ansible(config, "shell", ping_cmd, timeout=120)

    current_server: str | None = None
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and "TARGET:" in line:
            # Parse TARGET:ip:name:output
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue

            target_ip = parts[1].strip()
            peer_name = parts[2].strip()
            output = parts[3].strip()

            # Use peer name if available, otherwise use IP
            target_name = peer_name if peer_name else target_ip
            # Check if this IP matches a known target name
            for name, ip in targets.items():
                if ip == target_ip:
                    target_name = name
                    break

            # Extract packet loss percentage
            loss_match = re.search(r"(\d+)% packet loss", output)
            loss = float(loss_match.group(1)) if loss_match else 100.0

            # Extract latency if available
            latency_match = re.search(r"rtt min/avg/max.*= [\d.]+/([\d.]+)/", output)
            latency = float(latency_match.group(1)) if latency_match else None

            # Success only if we have < 100% packet loss
            success = loss < 100.0

            results.append(PingResult(
                source=current_server,
                target=target_name,
                target_ip=target_ip,
                success=success,
                latency_ms=latency,
                packet_loss=loss,
            ))

    return results


def _print_matrix(results: list[PingResult], sources: list[str], targets: list[str]) -> None:
    """Print connectivity as a matrix.

    Args:
        results: List of ping results
        sources: List of source names (rows - Ansible servers)
        targets: List of target names (columns - all peers)
    """
    print_header("Connectivity Matrix")

    # Build lookup
    lookup: dict[tuple[str, str], PingResult] = {
        (r.source, r.target): r for r in results
    }

    # Calculate column width based on target names
    col_width = max(12, max((len(t[:10]) for t in targets), default=10) + 2)

    # Header row
    header = "From \\ To".ljust(20) + "".join(t[:10].ljust(col_width) for t in targets)
    console.print(f"[bold]{header}[/bold]")

    for source in sources:
        row = source[:18].ljust(20)
        for target in targets:
            if source == target:
                cell = "[dim]--[/dim]"
            else:
                result = lookup.get((source, target))
                if result is None:
                    cell = "[dim]?[/dim]"
                elif result.success:
                    if result.latency_ms:
                        if result.latency_ms < 10:
                            cell = f"[green]{result.latency_ms:.0f}ms[/green]"
                        elif result.latency_ms < 50:
                            cell = f"[yellow]{result.latency_ms:.0f}ms[/yellow]"
                        else:
                            cell = f"[red]{result.latency_ms:.0f}ms[/red]"
                    else:
                        cell = "[green]OK[/green]"
                else:
                    cell = "[red]FAIL[/red]"
            row += cell.ljust(col_width + 10)  # Extra padding for color codes
        console.print(row)


@click.command("mesh-test")
@click.option("--count", "-c", default=3, help="Number of pings per target (default: 3)")
@click.option("--matrix", "-m", is_flag=True, help="Show results as matrix")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed results")
@click.pass_context
def mesh_test(
    ctx: click.Context,
    count: int,
    matrix: bool,
    verbose: bool,
) -> None:
    """Test WireGuard connectivity from each server to its configured peers.

    Tests only actual peer connections - each server pings the peers it has
    configured in WireGuard, not all possible nodes.
    """
    config: Config = ctx.obj

    print_header("Full Mesh Connectivity Test")

    # Get server info for reference
    servers = _get_server_wg_ips(config)
    if not servers:
        console.print("[red]No WireGuard interfaces found on servers[/red]")
        ctx.exit(1)

    console.print(f"Testing from {len(servers)} servers to their WireGuard peers...")
    for name, ip in sorted(servers.items()):
        console.print(f"  {name}: {ip}")

    # Run mesh test - discovers and pings actual peers per server
    console.print(f"\nRunning ping tests ({count} packets each)...")
    results = _run_mesh_ping(config, servers, count)

    if not results:
        console.print("[red]No results obtained - servers may have no WireGuard peers[/red]")
        ctx.exit(1)

    # Calculate statistics
    total_tests = len(results)
    successful = sum(1 for r in results if r.success)
    failed = total_tests - successful
    latencies = [r.latency_ms for r in results if r.latency_ms]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0

    # Summary
    console.print(f"\n[bold]Results:[/bold] {successful}/{total_tests} paths OK, {failed} failed")
    if avg_latency:
        console.print(f"[bold]Average latency:[/bold] {avg_latency:.1f}ms")

    # Show matrix if requested
    if matrix:
        # Get unique targets from results
        source_names = list(servers.keys())
        target_names = sorted(set(r.target for r in results))
        _print_matrix(results, source_names, target_names)

    # Show failures
    failures = [r for r in results if not r.success]
    if failures:
        print_header("Failed Connections")
        for f in failures:
            print_status(f"{f.source} -> {f.target}", f"FAILED ({f.target_ip})", Severity.CRITICAL)

    # Show high latency
    high_latency = [r for r in results if r.success and r.latency_ms and r.latency_ms > 50]
    if high_latency:
        print_header("High Latency Paths (>50ms)")
        for r in sorted(high_latency, key=lambda x: x.latency_ms or 0, reverse=True):
            print_status(f"{r.source} -> {r.target}", f"{r.latency_ms:.0f}ms", Severity.WARNING)

    # Show packet loss
    packet_loss = [r for r in results if r.success and r.packet_loss > 0]
    if packet_loss:
        print_header("Paths with Packet Loss")
        for r in packet_loss:
            print_status(f"{r.source} -> {r.target}", f"{r.packet_loss:.0f}% loss", Severity.WARNING)

    # Verbose output
    if verbose:
        print_header("All Results")
        table = Table()
        table.add_column("Source", style="cyan")
        table.add_column("Target", style="cyan")
        table.add_column("IP")
        table.add_column("Status")
        table.add_column("Latency", justify="right")
        table.add_column("Loss", justify="right")

        for r in results:
            status = "[green]OK[/green]" if r.success else "[red]FAIL[/red]"
            latency = f"{r.latency_ms:.1f}ms" if r.latency_ms else "-"
            loss = f"{r.packet_loss:.0f}%" if r.packet_loss > 0 else "-"

            table.add_row(r.source, r.target, r.target_ip, status, latency, loss)

        console.print(table)

    # Exit code
    if failures:
        ctx.exit(1)
