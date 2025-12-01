"""Latency-matrix command for clustermgr - network latency measurement."""

import re
from dataclasses import dataclass

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import print_header, run_ansible

console = Console()


@dataclass
class LatencyResult:
    """Result of latency measurement."""
    source: str
    target: str
    target_ip: str
    min_ms: float | None = None
    avg_ms: float | None = None
    max_ms: float | None = None
    stddev_ms: float | None = None
    packet_loss: float = 0.0
    success: bool = False


def _get_wireguard_ips(config: Config) -> dict[str, str]:
    """Get WireGuard IPs for all servers."""
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


def _measure_latency(config: Config, servers: dict[str, str], count: int = 10) -> list[LatencyResult]:
    """Measure latency from each server to its actual WireGuard peers.

    Only tests connectivity to peers that exist in each server's WireGuard config.
    """
    results: list[LatencyResult] = []

    # Discover actual peers and measure latency to each
    ping_cmd = (
        "for ip in $(sudo wg show wg0 2>/dev/null | grep 'allowed ips' | "
        "awk '{print $3}' | cut -d'/' -f1); do "
        f"  name=$(sudo grep -B1 \"$ip\" /etc/wireguard/wg0.conf 2>/dev/null | grep '# Node:' | cut -d: -f2 | tr -d ' '); "
        f'  output=$(ping -c {count} -i 0.2 $ip 2>&1 | tail -2); '
        '  echo "LATENCY:$ip:$name:$output"; '
        "done"
    )

    result = run_ansible(config, "shell", ping_cmd, timeout=120)

    current_server: str | None = None
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and "LATENCY:" in line:
            # Parse LATENCY:ip:name:output
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue

            target_ip = parts[1].strip()
            peer_name = parts[2].strip()
            output = parts[3].strip()

            # Use peer name if available, check if matches a known server
            target_name = peer_name if peer_name else target_ip
            for name, ip in servers.items():
                if ip == target_ip:
                    target_name = name
                    break

            # Extract packet loss percentage
            loss_match = re.search(r"(\d+)% packet loss", output)
            loss = float(loss_match.group(1)) if loss_match else 100.0

            # Parse ping statistics
            # Format: rtt min/avg/max/mdev = 0.123/0.456/0.789/0.012 ms
            rtt_match = re.search(
                r"rtt min/avg/max/mdev = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
                output
            )

            # Success only if loss < 100%
            if rtt_match and loss < 100.0:
                results.append(LatencyResult(
                    source=current_server,
                    target=target_name,
                    target_ip=target_ip,
                    min_ms=float(rtt_match.group(1)),
                    avg_ms=float(rtt_match.group(2)),
                    max_ms=float(rtt_match.group(3)),
                    stddev_ms=float(rtt_match.group(4)),
                    packet_loss=loss,
                    success=True,
                ))
            else:
                results.append(LatencyResult(
                    source=current_server,
                    target=target_name,
                    target_ip=target_ip,
                    packet_loss=loss,
                    success=False,
                ))

    return results


def _print_latency_matrix(results: list[LatencyResult], targets: list[str]) -> None:
    """Print latency as a matrix.

    Rows are sources (servers), columns are targets (discovered peers).
    """
    # Build lookup
    lookup: dict[tuple[str, str], LatencyResult] = {
        (r.source, r.target): r for r in results
    }

    # Get unique sources from results
    sources = sorted(set(r.source for r in results))

    # Calculate column width
    all_names = sources + targets
    col_width = max(12, max((len(s) for s in all_names), default=10) + 2)

    # Header
    header = "".ljust(col_width) + "".join(t[:col_width-2].center(col_width) for t in targets)
    console.print(f"[bold]{header}[/bold]")

    for source in sources:
        row = source[:col_width-2].ljust(col_width)
        for target in targets:
            if source == target:
                cell = "[dim]--[/dim]"
            else:
                r = lookup.get((source, target))
                if r is None:
                    cell = "[dim]-[/dim]"  # No connection (not a peer)
                elif not r.success:
                    cell = "[red]FAIL[/red]"
                elif r.avg_ms is not None:
                    if r.avg_ms < 5:
                        cell = f"[green]{r.avg_ms:.1f}[/green]"
                    elif r.avg_ms < 20:
                        cell = f"[cyan]{r.avg_ms:.1f}[/cyan]"
                    elif r.avg_ms < 50:
                        cell = f"[yellow]{r.avg_ms:.1f}[/yellow]"
                    else:
                        cell = f"[red]{r.avg_ms:.1f}[/red]"
                else:
                    cell = "[dim]?[/dim]"
            row += cell.center(col_width + 10)  # Extra for color codes
        console.print(row)

    console.print("\n[dim]Values in milliseconds (avg RTT)[/dim]")


@click.command("latency-matrix")
@click.option("--count", "-c", default=10, help="Number of pings (default: 10)")
@click.option("--table", "-t", is_flag=True, help="Show detailed table instead of matrix")
@click.option("--threshold", default=50.0, help="Latency threshold for warnings (ms)")
@click.pass_context
def latency_matrix(
    ctx: click.Context,
    count: int,
    table: bool,
    threshold: float,
) -> None:
    """Measure network latency from each server to its WireGuard peers.

    Tests only actual peer connections - each server measures latency to the
    peers it has configured in WireGuard.
    """
    config: Config = ctx.obj

    print_header("Network Latency Measurement")

    # Get server info
    servers = _get_wireguard_ips(config)
    if not servers:
        console.print("[red]No WireGuard interfaces found[/red]")
        ctx.exit(1)

    console.print(f"Measuring latency from {len(servers)} servers to their WireGuard peers ({count} pings each)...")

    # Measure latency - discovers actual peers per server
    results = _measure_latency(config, servers, count)

    if not results:
        console.print("[red]No latency measurements obtained[/red]")
        ctx.exit(1)

    # Calculate statistics
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    avg_latencies = [r.avg_ms for r in successful if r.avg_ms is not None]

    if avg_latencies:
        overall_avg = sum(avg_latencies) / len(avg_latencies)
        overall_min = min(avg_latencies)
        overall_max = max(avg_latencies)
    else:
        overall_avg = overall_min = overall_max = 0

    # Print summary
    console.print(f"\n[bold]Summary:[/bold]")
    console.print(f"  Paths tested: {len(results)}")
    console.print(f"  Successful: {len(successful)}, Failed: {len(failed)}")
    if avg_latencies:
        console.print(f"  Latency: min={overall_min:.1f}ms, avg={overall_avg:.1f}ms, max={overall_max:.1f}ms")

    # Print matrix or table
    if table:
        print_header("Detailed Latency Table")
        detail_table = Table()
        detail_table.add_column("Source", style="cyan")
        detail_table.add_column("Target", style="cyan")
        detail_table.add_column("Min", justify="right")
        detail_table.add_column("Avg", justify="right")
        detail_table.add_column("Max", justify="right")
        detail_table.add_column("StdDev", justify="right")
        detail_table.add_column("Loss", justify="right")

        for r in sorted(results, key=lambda x: (x.source, x.target)):
            if r.success and r.avg_ms is not None:
                avg_color = "green" if r.avg_ms < 20 else "yellow" if r.avg_ms < 50 else "red"
                detail_table.add_row(
                    r.source,
                    r.target,
                    f"{r.min_ms:.2f}ms" if r.min_ms else "-",
                    f"[{avg_color}]{r.avg_ms:.2f}ms[/{avg_color}]",
                    f"{r.max_ms:.2f}ms" if r.max_ms else "-",
                    f"{r.stddev_ms:.2f}ms" if r.stddev_ms else "-",
                    f"{r.packet_loss:.0f}%" if r.packet_loss > 0 else "-",
                )
            else:
                detail_table.add_row(
                    r.source,
                    r.target,
                    "-", "[red]FAIL[/red]", "-", "-",
                    f"[red]{r.packet_loss:.0f}%[/red]",
                )

        console.print(detail_table)
    else:
        print_header("Latency Matrix")
        # Get unique targets from results for dynamic matrix
        target_names = sorted(set(r.target for r in results))
        _print_latency_matrix(results, target_names)

    # Show warnings
    high_latency = [r for r in successful if r.avg_ms and r.avg_ms > threshold]
    if high_latency:
        print_header(f"High Latency Paths (>{threshold}ms)")
        for r in sorted(high_latency, key=lambda x: x.avg_ms or 0, reverse=True):
            console.print(f"  [yellow]{r.source} -> {r.target}: {r.avg_ms:.1f}ms[/yellow]")

    # Show jitter warnings (high stddev relative to avg)
    high_jitter = [r for r in successful if r.avg_ms and r.stddev_ms and r.stddev_ms > r.avg_ms * 0.5]
    if high_jitter:
        print_header("High Jitter Paths")
        for r in high_jitter:
            console.print(f"  [yellow]{r.source} -> {r.target}: stddev={r.stddev_ms:.1f}ms (avg={r.avg_ms:.1f}ms)[/yellow]")

    # Show failed paths
    if failed:
        print_header("Failed Paths")
        for r in failed:
            console.print(f"  [red]{r.source} -> {r.target} ({r.target_ip}): 100% packet loss[/red]")

    # Exit code
    if failed or high_latency:
        ctx.exit(1)
