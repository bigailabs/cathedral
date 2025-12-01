"""WireGuard management commands for clustermgr."""

from dataclasses import dataclass

import click
from rich.console import Console
from rich.table import Table

from clustermgr.commands.health import check_wireguard_peers
from clustermgr.config import Config
from clustermgr.utils import confirm, parse_json_output, print_header, run_ansible, run_kubectl, Severity, print_status

console = Console()


@dataclass
class PeerReconcileStatus:
    """Status of a WireGuard peer for reconciliation."""

    node_name: str
    wg_ip: str
    pod_cidr: str
    pubkey: str | None = None
    has_cidr_in_allowed: bool = False
    has_route: bool = False


@click.group()
def wg() -> None:
    """WireGuard management commands."""
    pass


@wg.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show WireGuard status on all servers."""
    config: Config = ctx.obj

    print_header("WireGuard Status")

    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0",
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get WireGuard status[/red]")
        ctx.exit(1)

    current_server: str | None = None
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            console.print(f"\n[bold cyan]{current_server}[/bold cyan]")
        elif current_server and line.strip():
            text = line.strip()
            if text.startswith("interface:"):
                console.print(f"  [green]{text}[/green]")
            elif text.startswith("peer:"):
                console.print(f"\n  [yellow]{text}[/yellow]")
            elif "latest handshake" in text:
                if "minute" in text:
                    parts = text.split(":")
                    if len(parts) > 1:
                        mins_str = parts[1].strip().split()[0]
                        try:
                            mins = int(mins_str)
                            color = "red" if mins > 3 else "green"
                        except ValueError:
                            color = "green"
                    else:
                        color = "green"
                else:
                    color = "green"
                console.print(f"    [{color}]{text}[/{color}]")
            else:
                console.print(f"    {text}")


@wg.command()
@click.pass_context
def peers(ctx: click.Context) -> None:
    """List WireGuard peers with health metrics."""
    config: Config = ctx.obj

    print_header("WireGuard Peers")

    peers_data = check_wireguard_peers(config)

    for server, server_peers in peers_data.items():
        console.print(f"\n[bold]{server}[/bold]:")
        for peer in server_peers:
            key_short = peer.get("key", "unknown")[:16] + "..."
            ips = peer.get("allowed_ips", "unknown")
            handshake = peer.get("handshake", "unknown")
            stale = peer.get("handshake_stale", False)

            status_color = "red" if stale else "green"
            status_text = "STALE" if stale else "OK"

            console.print(f"  [cyan]{key_short}[/cyan]")
            console.print(f"    IPs: {ips}")
            console.print(f"    Handshake: {handshake} [[{status_color}]{status_text}[/{status_color}]]")


@wg.command()
@click.option("--nodes", "-n", multiple=True, help="Specific nodes to restart")
@click.pass_context
def restart(ctx: click.Context, nodes: tuple[str, ...]) -> None:
    """Restart WireGuard service on specified nodes."""
    config: Config = ctx.obj

    target = ",".join(nodes) if nodes else "k3s_server"

    print_header(f"Restarting WireGuard on {target}")

    if config.dry_run:
        console.print("[yellow][DRY RUN] Would restart WireGuard service[/yellow]")
        return

    if not config.no_confirm and not confirm(
        "This will briefly interrupt VPN connectivity. Continue?"
    ):
        console.print("Aborted.")
        return

    result = run_ansible(
        config,
        "shell",
        "sudo systemctl restart wg-quick@wg0 && sleep 2 && sudo wg show wg0 | head -3",
        hosts=target,
        timeout=60,
    )

    if result.returncode == 0:
        console.print("[green]WireGuard restarted successfully[/green]")
        console.print(result.stdout)
    else:
        console.print("[red]Failed to restart WireGuard[/red]")
        console.print(result.stderr)
        ctx.exit(1)


def _get_gpu_nodes_with_cidrs(config: Config) -> list[dict]:
    """Get GPU nodes that have WireGuard labels and pod CIDRs assigned."""
    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "json"],
        timeout=30,
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        pod_cidr = spec.get("podCIDR")
        if not pod_cidr:
            continue

        # Get internal IP (WireGuard IP for GPU nodes)
        wg_ip = None
        for addr in status.get("addresses", []):
            if addr.get("type") == "InternalIP":
                wg_ip = addr.get("address")
                break

        if wg_ip:
            nodes.append({
                "name": metadata.get("name", ""),
                "wg_ip": wg_ip,
                "pod_cidr": pod_cidr,
            })

    return nodes


def _check_peer_allowed_ips(config: Config, nodes: list[dict]) -> list[PeerReconcileStatus]:
    """Check if pod CIDRs are in WireGuard AllowedIPs for each peer."""
    statuses: list[PeerReconcileStatus] = []

    # Get WireGuard peer info from all servers
    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 dump 2>/dev/null | tail -n +2",
        timeout=30,
    )

    if result.returncode != 0:
        return statuses

    # Parse peer info: pubkey, preshared, endpoint, allowed_ips, ...
    peers_by_ip: dict[str, dict] = {}
    for line in result.stdout.split("\n"):
        if "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 4:
            pubkey = parts[0]
            allowed_ips = parts[3]
            # Extract individual IPs from AllowedIPs
            for ip_cidr in allowed_ips.split(","):
                ip = ip_cidr.split("/")[0]
                if ip.startswith("10.200"):
                    peers_by_ip[ip] = {
                        "pubkey": pubkey,
                        "allowed_ips": allowed_ips,
                    }

    # Check routes on first server
    route_result = run_ansible(
        config,
        "shell",
        "ip route show | grep 'dev wg0'",
        hosts="k3s_server[0]",
        timeout=30,
    )
    existing_routes = route_result.stdout if route_result.returncode == 0 else ""

    for node in nodes:
        peer_info = peers_by_ip.get(node["wg_ip"], {})
        allowed_ips = peer_info.get("allowed_ips", "")

        status = PeerReconcileStatus(
            node_name=node["name"],
            wg_ip=node["wg_ip"],
            pod_cidr=node["pod_cidr"],
            pubkey=peer_info.get("pubkey"),
            has_cidr_in_allowed=node["pod_cidr"] in allowed_ips,
            has_route=node["pod_cidr"] in existing_routes,
        )
        statuses.append(status)

    return statuses


def check_reconcile_needed(config: Config) -> list[PeerReconcileStatus]:
    """Check if WireGuard peer reconciliation is needed.

    Returns list of peers that need reconciliation (missing pod CIDRs in AllowedIPs).
    """
    nodes = _get_gpu_nodes_with_cidrs(config)
    if not nodes:
        return []

    statuses = _check_peer_allowed_ips(config, nodes)
    return [s for s in statuses if not s.has_cidr_in_allowed or not s.has_route]


@wg.command()
@click.option("--fix", "-f", is_flag=True, help="Fix missing pod CIDRs in AllowedIPs")
@click.pass_context
def reconcile(ctx: click.Context, fix: bool) -> None:
    """Reconcile WireGuard peer AllowedIPs with pod CIDRs.

    Checks if GPU node pod CIDRs are configured in WireGuard AllowedIPs
    and routes. This is needed because GPU nodes register with WireGuard
    before joining K3s, but pod CIDRs are assigned when they join.
    """
    config: Config = ctx.obj

    print_header("WireGuard Peer Reconciliation")

    # Get GPU nodes with pod CIDRs
    nodes = _get_gpu_nodes_with_cidrs(config)

    if not nodes:
        console.print("[dim]No GPU nodes with WireGuard labels and pod CIDRs found[/dim]")
        return

    console.print(f"Found {len(nodes)} GPU node(s) with pod CIDRs\n")

    # Check current status
    statuses = _check_peer_allowed_ips(config, nodes)

    # Display status table
    table = Table(title="Peer Reconciliation Status")
    table.add_column("Node", style="cyan")
    table.add_column("WG IP")
    table.add_column("Pod CIDR")
    table.add_column("In AllowedIPs")
    table.add_column("Route Exists")
    table.add_column("Status")

    needs_fix = []
    for s in statuses:
        allowed_ok = "[green]Yes[/green]" if s.has_cidr_in_allowed else "[red]No[/red]"
        route_ok = "[green]Yes[/green]" if s.has_route else "[red]No[/red]"

        if s.pubkey is None:
            status = "[red]No WG peer[/red]"
        elif s.has_cidr_in_allowed and s.has_route:
            status = "[green]OK[/green]"
        else:
            status = "[yellow]Needs fix[/yellow]"
            needs_fix.append(s)

        table.add_row(
            s.node_name[:20],
            s.wg_ip,
            s.pod_cidr,
            allowed_ok,
            route_ok,
            status,
        )

    console.print(table)

    if not needs_fix:
        console.print("\n[green]All peers are properly reconciled[/green]")
        return

    console.print(f"\n[yellow]{len(needs_fix)} peer(s) need reconciliation[/yellow]")

    if not fix:
        console.print("\nRun 'clustermgr wg reconcile --fix' to apply fixes")
        console.print("Or run 'clustermgr fix' for automated remediation")
        ctx.exit(1)
        return

    # Apply fixes
    if config.dry_run:
        console.print("\n[yellow][DRY RUN] Would apply the following fixes:[/yellow]")
        for s in needs_fix:
            if not s.has_cidr_in_allowed:
                console.print(f"  - Add {s.pod_cidr} to AllowedIPs for peer {s.wg_ip}")
            if not s.has_route:
                console.print(f"  - Add route for {s.pod_cidr} via wg0")
        return

    if not config.no_confirm and not confirm(f"Apply fixes to {len(needs_fix)} peer(s)?"):
        console.print("Aborted.")
        return

    print_header("Applying Fixes")

    for s in needs_fix:
        if s.pubkey is None:
            print_status(s.node_name, "Skipped (no WG peer)", Severity.WARNING)
            continue

        success = True

        # Add pod CIDR to AllowedIPs
        if not s.has_cidr_in_allowed:
            console.print(f"  Adding {s.pod_cidr} to AllowedIPs for {s.node_name}...")

            # Get current AllowedIPs and append pod CIDR
            fix_cmd = (
                f"current=$(sudo wg show wg0 allowed-ips | grep '{s.pubkey}' | awk '{{print $2}}'); "
                f"sudo wg set wg0 peer {s.pubkey} allowed-ips \"$current,{s.pod_cidr}\" && "
                f"sudo wg-quick save wg0"
            )

            result = run_ansible(config, "shell", fix_cmd, timeout=30)
            if result.returncode != 0:
                print_status(f"  {s.node_name} AllowedIPs", "FAILED", Severity.CRITICAL)
                success = False
            else:
                print_status(f"  {s.node_name} AllowedIPs", "Updated", Severity.HEALTHY)

        # Add route
        if not s.has_route:
            console.print(f"  Adding route for {s.pod_cidr}...")

            route_cmd = f"sudo ip route replace {s.pod_cidr} dev wg0"
            result = run_ansible(config, "shell", route_cmd, timeout=30)
            if result.returncode != 0:
                print_status(f"  {s.node_name} route", "FAILED", Severity.CRITICAL)
                success = False
            else:
                print_status(f"  {s.node_name} route", "Added", Severity.HEALTHY)

        if success:
            print_status(s.node_name, "Reconciled", Severity.HEALTHY)
        else:
            print_status(s.node_name, "Partial failure", Severity.WARNING)

    console.print("\n[green]Reconciliation complete[/green]")


@wg.command("keys")
@click.pass_context
def keys(ctx: click.Context) -> None:
    """Show WireGuard key information for rotation planning.

    Displays public key hashes and key age information to help
    plan quarterly key rotation procedures.
    """
    config: Config = ctx.obj

    print_header("WireGuard Key Status")

    result = run_ansible(
        config,
        "shell",
        (
            "echo \"=== Public Key ===\"; "
            "cat /etc/wireguard/public.key 2>/dev/null || echo 'N/A'; "
            "echo \"=== Key File Age ===\"; "
            "stat -c '%y' /etc/wireguard/private.key 2>/dev/null | cut -d' ' -f1 || echo 'N/A'; "
            "echo \"=== Backup Exists ===\"; "
            "test -f /etc/wireguard/private.key.backup && echo 'Yes' || echo 'No'"
        ),
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get key information[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Server", style="cyan")
    table.add_column("Public Key (truncated)")
    table.add_column("Key Created")
    table.add_column("Backup")

    current_server = None
    pubkey = ""
    key_date = ""
    backup = ""

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            if current_server:
                table.add_row(
                    current_server,
                    pubkey[:20] + "..." if pubkey and pubkey != "N/A" else "N/A",
                    key_date,
                    backup,
                )
            current_server = line.split(" | ")[0].strip()
            pubkey = ""
            key_date = ""
            backup = ""
        elif "=== Public Key ===" in line:
            continue
        elif "=== Key File Age ===" in line:
            continue
        elif "=== Backup Exists ===" in line:
            continue
        elif current_server:
            text = line.strip()
            if not pubkey and text and "===" not in text:
                pubkey = text
            elif not key_date and text and "===" not in text and pubkey:
                key_date = text
            elif not backup and text in ("Yes", "No"):
                backup = text

    if current_server:
        table.add_row(
            current_server,
            pubkey[:20] + "..." if pubkey and pubkey != "N/A" else "N/A",
            key_date,
            backup,
        )

    console.print(table)

    print_header("Key Rotation Recommendations")
    console.print("Keys should be rotated quarterly. See NETWORK-MAINTENANCE-PROCEDURES.md")
    console.print("")
    console.print("Before rotation:")
    console.print("  1. Schedule maintenance window")
    console.print("  2. Generate new keys on all servers")
    console.print("  3. Coordinate cutover across all servers and GPU nodes")
    console.print("  4. Update GPU node configs via API")
    console.print("  5. Verify connectivity after rotation")


@wg.command("handshakes")
@click.option("--stale-threshold", "-t", default=180, help="Seconds before handshake is considered stale")
@click.pass_context
def handshakes(ctx: click.Context, stale_threshold: int) -> None:
    """Check WireGuard handshake ages across all peers.

    Reports peers with stale handshakes that may indicate
    connectivity issues or need for restart.
    """
    config: Config = ctx.obj

    print_header("WireGuard Handshake Status")

    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 dump 2>/dev/null | tail -n +2",
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get handshake information[/red]")
        ctx.exit(1)

    stale_peers: list[tuple[str, str, int]] = []
    healthy_count = 0
    total_count = 0
    current_server = None

    import time
    current_time = int(time.time())

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            continue

        if not current_server or "\t" not in line:
            continue

        parts = line.split("\t")
        if len(parts) >= 5:
            total_count += 1
            pubkey = parts[0][:16] + "..."
            try:
                last_handshake = int(parts[4])
                if last_handshake == 0:
                    stale_peers.append((current_server, pubkey, -1))
                else:
                    age = current_time - last_handshake
                    if age > stale_threshold:
                        stale_peers.append((current_server, pubkey, age))
                    else:
                        healthy_count += 1
            except (ValueError, IndexError):
                pass

    console.print(f"Total peers: {total_count}")
    console.print(f"Healthy: {healthy_count}")
    console.print(f"Stale (>{stale_threshold}s): {len(stale_peers)}\n")

    if stale_peers:
        table = Table()
        table.add_column("Server", style="cyan")
        table.add_column("Peer")
        table.add_column("Handshake Age")

        for server, peer, age in stale_peers:
            if age < 0:
                age_str = "[red]Never[/red]"
            else:
                mins = age // 60
                secs = age % 60
                age_str = f"[yellow]{mins}m {secs}s[/yellow]"

            table.add_row(server, peer, age_str)

        console.print(table)

        console.print("\n[yellow]Stale handshakes may indicate:[/yellow]")
        console.print("  - GPU node is offline")
        console.print("  - Network path is blocked")
        console.print("  - WireGuard needs restart")
        console.print("")
        console.print("Run 'clustermgr wg restart' to restart WireGuard service")
    else:
        console.print("[green]All handshakes are healthy[/green]")
