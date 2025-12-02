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
    """Status of a WireGuard peer for reconciliation.

    Note: Routes for pod CIDRs should go through flannel.1 (VXLAN), not wg0.
    The WireGuard config has Table=off to prevent wg-quick from creating routes.
    Route reconciliation is handled by the K8s CronJob (wireguard-reconcile).
    This class only tracks AllowedIPs status.
    """

    node_name: str
    wg_ip: str
    pod_cidr: str
    pubkey: str | None = None
    has_cidr_in_allowed: bool = False


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

    for node in nodes:
        peer_info = peers_by_ip.get(node["wg_ip"], {})
        allowed_ips = peer_info.get("allowed_ips", "")

        status = PeerReconcileStatus(
            node_name=node["name"],
            wg_ip=node["wg_ip"],
            pod_cidr=node["pod_cidr"],
            pubkey=peer_info.get("pubkey"),
            has_cidr_in_allowed=node["pod_cidr"] in allowed_ips,
        )
        statuses.append(status)

    return statuses


def check_reconcile_needed(config: Config) -> list[PeerReconcileStatus]:
    """Check if WireGuard peer reconciliation is needed.

    Returns list of peers that need reconciliation (missing pod CIDRs in AllowedIPs).
    Note: Route reconciliation is handled separately by the K8s CronJob.
    """
    nodes = _get_gpu_nodes_with_cidrs(config)
    if not nodes:
        return []

    statuses = _check_peer_allowed_ips(config, nodes)
    return [s for s in statuses if not s.has_cidr_in_allowed]


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

    # Display status table (only AllowedIPs - routes are via flannel.1, handled by CronJob)
    table = Table(title="Peer Reconciliation Status")
    table.add_column("Node", style="cyan")
    table.add_column("WG IP")
    table.add_column("Pod CIDR")
    table.add_column("In AllowedIPs")
    table.add_column("Status")

    needs_fix = []
    for s in statuses:
        allowed_ok = "[green]Yes[/green]" if s.has_cidr_in_allowed else "[red]No[/red]"

        if s.pubkey is None:
            status = "[red]No WG peer[/red]"
        elif s.has_cidr_in_allowed:
            status = "[green]OK[/green]"
        else:
            status = "[yellow]Needs fix[/yellow]"
            needs_fix.append(s)

        table.add_row(
            s.node_name[:20],
            s.wg_ip,
            s.pod_cidr,
            allowed_ok,
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

    # Apply fixes (AllowedIPs only - routes are via flannel.1, handled by CronJob)
    if config.dry_run:
        console.print("\n[yellow][DRY RUN] Would apply the following fixes:[/yellow]")
        for s in needs_fix:
            console.print(f"  - Add {s.pod_cidr} to AllowedIPs for peer {s.wg_ip}")
        return

    if not config.no_confirm and not confirm(f"Apply fixes to {len(needs_fix)} peer(s)?"):
        console.print("Aborted.")
        return

    print_header("Applying Fixes")

    for s in needs_fix:
        if s.pubkey is None:
            print_status(s.node_name, "Skipped (no WG peer)", Severity.WARNING)
            continue

        console.print(f"  Adding {s.pod_cidr} to AllowedIPs for {s.node_name}...")

        # Get current AllowedIPs and append pod CIDR
        # Note: wg-quick save is not used since config has SaveConfig=false
        fix_cmd = (
            f"current=$(sudo wg show wg0 allowed-ips | grep '{s.pubkey}' | awk '{{$1=\"\"; print substr($0,2)}}' | tr ' ' ','); "
            f"sudo wg set wg0 peer {s.pubkey} allowed-ips \"$current,{s.pod_cidr}\""
        )

        result = run_ansible(config, "shell", fix_cmd, timeout=30)
        if result.returncode != 0:
            print_status(s.node_name, "FAILED", Severity.CRITICAL)
        else:
            print_status(s.node_name, "Updated", Severity.HEALTHY)

    console.print("\n[green]Reconciliation complete[/green]")
    console.print("[dim]Note: Route reconciliation is handled by the K8s CronJob[/dim]")


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


@wg.command("timer")
@click.pass_context
def timer_status(ctx: click.Context) -> None:
    """Check WireGuard peer reconciliation timer status.

    Shows the status of the systemd timer that reconciles pod CIDRs
    in WireGuard AllowedIPs for GPU nodes.
    """
    config: Config = ctx.obj

    print_header("WireGuard Reconciliation Timer Status")

    result = run_ansible(
        config,
        "shell",
        (
            "echo '=== Timer Status ==='; "
            "systemctl is-active wireguard-peer-reconcile.timer 2>/dev/null || echo 'inactive'; "
            "echo '=== Service Status ==='; "
            "systemctl is-active wireguard-peer-reconcile.service 2>/dev/null || echo 'inactive'; "
            "echo '=== Last Run ==='; "
            "journalctl -u wireguard-peer-reconcile -n 5 --no-pager 2>/dev/null | tail -5 || echo 'No logs'"
        ),
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get timer status[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Server", style="cyan")
    table.add_column("Timer")
    table.add_column("Last Run")

    current_server = None
    timer_status_val = ""
    last_run_lines: list[str] = []
    in_last_run = False

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            if current_server:
                timer_color = "green" if timer_status_val == "active" else "red"
                last_run_summary = last_run_lines[-1] if last_run_lines else "No data"
                if len(last_run_summary) > 50:
                    last_run_summary = last_run_summary[:47] + "..."
                table.add_row(
                    current_server,
                    f"[{timer_color}]{timer_status_val}[/{timer_color}]",
                    last_run_summary,
                )
            current_server = line.split(" | ")[0].strip()
            timer_status_val = ""
            last_run_lines = []
            in_last_run = False
        elif "=== Timer Status ===" in line:
            in_last_run = False
        elif "=== Service Status ===" in line:
            in_last_run = False
        elif "=== Last Run ===" in line:
            in_last_run = True
        elif current_server:
            text = line.strip()
            if text and "===" not in text:
                if in_last_run:
                    last_run_lines.append(text)
                elif not timer_status_val:
                    timer_status_val = text

    if current_server:
        timer_color = "green" if timer_status_val == "active" else "red"
        last_run_summary = last_run_lines[-1] if last_run_lines else "No data"
        if len(last_run_summary) > 50:
            last_run_summary = last_run_summary[:47] + "..."
        table.add_row(
            current_server,
            f"[{timer_color}]{timer_status_val}[/{timer_color}]",
            last_run_summary,
        )

    console.print(table)

    console.print("\n[dim]Timer runs every 60s to reconcile GPU node pod CIDRs in AllowedIPs[/dim]")
    console.print("[dim]See docs/runbooks/WIREGUARD-FLANNEL-RECONCILIATION.md for details[/dim]")


@wg.command("cronjob")
@click.pass_context
def cronjob_status(ctx: click.Context) -> None:
    """Check WireGuard reconciliation CronJob status.

    Shows the status of the K8s CronJob that reconciles Flannel VXLAN
    entries (routes, FDB, neighbors) for GPU nodes.
    """
    config: Config = ctx.obj

    print_header("WireGuard Reconciliation CronJob Status")

    result = run_kubectl(
        config,
        ["get", "cronjob", "-n", "kube-system", "wireguard-reconcile", "-o", "json"],
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]CronJob not found or failed to query[/red]")
        console.print(result.stderr)
        ctx.exit(1)

    data = parse_json_output(result.stdout)
    if not data:
        console.print("[red]Failed to parse CronJob data[/red]")
        ctx.exit(1)

    spec = data.get("spec", {})
    status = data.get("status", {})

    schedule = spec.get("schedule", "unknown")
    suspend = spec.get("suspend", False)
    last_schedule = status.get("lastScheduleTime", "never")
    last_successful = status.get("lastSuccessfulTime", "never")

    table = Table(title="CronJob: wireguard-reconcile")
    table.add_column("Property", style="cyan")
    table.add_column("Value")

    suspend_str = "[red]Yes[/red]" if suspend else "[green]No[/green]"
    table.add_row("Schedule", schedule)
    table.add_row("Suspended", suspend_str)
    table.add_row("Last Scheduled", last_schedule)
    table.add_row("Last Successful", last_successful)

    console.print(table)

    print_header("Recent Jobs")

    jobs_result = run_kubectl(
        config,
        ["get", "jobs", "-n", "kube-system", "-l", "app.kubernetes.io/name=wireguard-reconcile",
         "--sort-by=.metadata.creationTimestamp", "-o", "json"],
        timeout=30,
    )

    if jobs_result.returncode == 0:
        jobs_data = parse_json_output(jobs_result.stdout)
        items = jobs_data.get("items", [])[-5:]

        if items:
            jobs_table = Table()
            jobs_table.add_column("Job", style="cyan")
            jobs_table.add_column("Status")
            jobs_table.add_column("Started")
            jobs_table.add_column("Duration")

            for job in reversed(items):
                name = job.get("metadata", {}).get("name", "")[-30:]
                job_status = job.get("status", {})

                succeeded = job_status.get("succeeded", 0)
                failed = job_status.get("failed", 0)

                if succeeded:
                    status_str = "[green]Succeeded[/green]"
                elif failed:
                    status_str = "[red]Failed[/red]"
                else:
                    status_str = "[yellow]Running[/yellow]"

                start_time = job_status.get("startTime", "-")
                completion_time = job_status.get("completionTime")

                if start_time != "-" and completion_time:
                    from datetime import datetime
                    try:
                        start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        end = datetime.fromisoformat(completion_time.replace("Z", "+00:00"))
                        duration = str(end - start).split(".")[0]
                    except (ValueError, TypeError):
                        duration = "-"
                else:
                    duration = "-"

                jobs_table.add_row(name, status_str, start_time[:19] if start_time != "-" else "-", duration)

            console.print(jobs_table)
        else:
            console.print("[dim]No recent jobs found[/dim]")

    console.print("\n[dim]CronJob runs every 5min to reconcile Flannel VXLAN entries[/dim]")
    console.print("[dim]See docs/runbooks/WIREGUARD-FLANNEL-RECONCILIATION.md for details[/dim]")


@wg.command("ping")
@click.option("--count", "-c", default=3, help="Number of ping packets")
@click.option("--timeout", "-W", default=3, help="Timeout in seconds per packet")
@click.pass_context
def ping_gpu_nodes(ctx: click.Context, count: int, timeout: int) -> None:
    """Ping GPU nodes through WireGuard tunnel.

    Tests basic tunnel connectivity by pinging each GPU node's
    WireGuard IP from the K3s server. This is Step 1 in the
    network validation runbook.
    """
    config: Config = ctx.obj

    print_header("WireGuard Tunnel Connectivity Test")

    nodes = _get_gpu_nodes_with_cidrs(config)
    if not nodes:
        console.print("[yellow]No GPU nodes with WireGuard labels found[/yellow]")
        return

    console.print(f"Testing connectivity to {len(nodes)} GPU node(s)...\n")

    all_ok = True
    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("WireGuard IP")
    table.add_column("Result")
    table.add_column("Latency")

    for node in nodes:
        wg_ip = node["wg_ip"]
        result = run_ansible(
            config,
            "shell",
            f"ping -c {count} -W {timeout} {wg_ip} 2>&1 | tail -3",
            hosts="k3s_server[0]",
            timeout=30,
        )

        if result.returncode == 0 and "0% packet loss" in result.stdout:
            import re
            rtt_match = re.search(r"rtt.*?=\s*[\d.]+/([\d.]+)", result.stdout)
            latency = f"{rtt_match.group(1)}ms" if rtt_match else "OK"
            status_str = "[green]OK[/green]"
        elif "100% packet loss" in result.stdout:
            latency = "-"
            status_str = "[red]UNREACHABLE[/red]"
            all_ok = False
        else:
            loss_match = re.search(r"(\d+)% packet loss", result.stdout)
            loss = loss_match.group(1) if loss_match else "?"
            latency = "-"
            status_str = f"[yellow]{loss}% loss[/yellow]"
            all_ok = False

        table.add_row(
            node["name"][:30],
            wg_ip,
            status_str,
            latency,
        )

    console.print(table)

    if all_ok:
        console.print("\n[green]All GPU nodes reachable through WireGuard tunnel[/green]")
    else:
        console.print("\n[red]Some GPU nodes are unreachable[/red]")
        console.print("Run 'clustermgr wg status' for detailed WireGuard info")
        console.print("See docs/runbooks/WIREGUARD-TROUBLESHOOTING.md")
        ctx.exit(1)


@wg.command("gpu-nodes")
@click.pass_context
def gpu_nodes_status(ctx: click.Context) -> None:
    """Show GPU node K8s status.

    Lists all GPU nodes with their status, WireGuard IP, and pod CIDR.
    This is Step 2 in the network validation runbook.
    """
    config: Config = ctx.obj

    print_header("GPU Node Kubernetes Status")

    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "json"],
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get GPU nodes[/red]")
        ctx.exit(1)

    data = parse_json_output(result.stdout)
    items = data.get("items", [])

    if not items:
        console.print("[yellow]No GPU nodes with WireGuard labels found[/yellow]")
        return

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Status")
    table.add_column("WireGuard IP")
    table.add_column("Pod CIDR")
    table.add_column("Age")

    all_ready = True

    for item in items:
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        name = metadata.get("name", "")
        pod_cidr = spec.get("podCIDR", "N/A")

        wg_ip = "N/A"
        for addr in status.get("addresses", []):
            if addr.get("type") == "InternalIP":
                wg_ip = addr.get("address", "")
                break

        node_status = "Unknown"
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready":
                if cond.get("status") == "True":
                    node_status = "[green]Ready[/green]"
                else:
                    node_status = "[red]NotReady[/red]"
                    all_ready = False
                break

        creation = metadata.get("creationTimestamp", "")
        if creation:
            from datetime import datetime
            try:
                created = datetime.fromisoformat(creation.replace("Z", "+00:00"))
                now = datetime.now(created.tzinfo)
                delta = now - created
                if delta.days > 0:
                    age = f"{delta.days}d"
                else:
                    hours = delta.seconds // 3600
                    age = f"{hours}h"
            except (ValueError, TypeError):
                age = "-"
        else:
            age = "-"

        table.add_row(
            name[:36],
            node_status,
            wg_ip,
            pod_cidr,
            age,
        )

    console.print(table)
    console.print(f"\nTotal: {len(items)} GPU node(s)")

    if all_ready:
        console.print("[green]All GPU nodes are Ready[/green]")
    else:
        console.print("\n[red]Some GPU nodes are NotReady[/red]")
        console.print("Run 'kubectl describe node <node-name>' for details")
        ctx.exit(1)


@wg.command("validate")
@click.option("--fix", "-f", is_flag=True, help="Attempt to fix issues found")
@click.pass_context
def validate(ctx: click.Context, fix: bool) -> None:
    """Run complete WireGuard network validation.

    Executes all validation steps from the WIREGUARD-NETWORK-VALIDATION.md
    runbook and provides a summary of results.

    Steps:
    1. WireGuard tunnel ping
    2. GPU node K8s status
    3. WireGuard handshakes
    4. AllowedIPs configuration
    5. Flannel routes
    6. FDB entries
    7. CronJob status
    """
    config: Config = ctx.obj
    import re
    import time

    print_header("WireGuard Network Validation")
    console.print("Running validation checklist from WIREGUARD-NETWORK-VALIDATION.md\n")

    results: list[tuple[str, bool, str]] = []

    # Step 1: WireGuard tunnel ping
    console.print("[bold]Step 1:[/bold] WireGuard tunnel connectivity...")
    nodes = _get_gpu_nodes_with_cidrs(config)
    step1_ok = True
    step1_details = []
    for node in nodes:
        result = run_ansible(
            config,
            "shell",
            f"ping -c 2 -W 2 {node['wg_ip']} 2>&1 | grep -E 'packet loss|unreachable'",
            hosts="k3s_server[0]",
            timeout=15,
        )
        if "0% packet loss" in result.stdout:
            step1_details.append(f"{node['wg_ip']}: OK")
        else:
            step1_ok = False
            step1_details.append(f"{node['wg_ip']}: FAIL")
    results.append(("Tunnel ping", step1_ok, ", ".join(step1_details) if step1_details else "No GPU nodes"))

    # Step 2: GPU node K8s status
    console.print("[bold]Step 2:[/bold] GPU node K8s status...")
    k8s_result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "json"],
        timeout=15,
    )
    step2_ok = True
    step2_details = []
    if k8s_result.returncode == 0:
        k8s_data = parse_json_output(k8s_result.stdout)
        for item in k8s_data.get("items", []):
            name = item.get("metadata", {}).get("name", "")[:20]
            ready = False
            for cond in item.get("status", {}).get("conditions", []):
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    ready = True
                    break
            if ready:
                step2_details.append(f"{name}: Ready")
            else:
                step2_ok = False
                step2_details.append(f"{name}: NotReady")
    else:
        step2_ok = False
        step2_details.append("Failed to query")
    results.append(("K8s node status", step2_ok, ", ".join(step2_details)))

    # Step 3: WireGuard handshakes
    console.print("[bold]Step 3:[/bold] WireGuard handshakes...")
    hs_result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 dump 2>/dev/null | tail -n +2",
        hosts="k3s_server[0]",
        timeout=15,
    )
    step3_ok = True
    step3_details = []
    current_time = int(time.time())
    stale_threshold = 180

    for line in hs_result.stdout.split("\n"):
        if "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) >= 5:
            try:
                last_hs = int(parts[4])
                if last_hs == 0:
                    step3_ok = False
                    step3_details.append("Never")
                elif current_time - last_hs > stale_threshold:
                    step3_ok = False
                    age = (current_time - last_hs) // 60
                    step3_details.append(f"{age}m stale")
                else:
                    age = current_time - last_hs
                    step3_details.append(f"{age}s ago")
            except (ValueError, IndexError):
                pass
    results.append(("Handshakes", step3_ok, ", ".join(step3_details) if step3_details else "No peers"))

    # Step 4: AllowedIPs configuration
    console.print("[bold]Step 4:[/bold] AllowedIPs configuration...")
    step4_ok = True
    step4_details = []
    for node in nodes:
        aips_result = run_ansible(
            config,
            "shell",
            f"sudo wg show wg0 allowed-ips | grep {node['wg_ip']}",
            hosts="k3s_server[0]",
            timeout=15,
        )
        has_wg = f"{node['wg_ip']}/32" in aips_result.stdout
        has_cidr = node['pod_cidr'] in aips_result.stdout if node.get('pod_cidr') else False
        if has_wg and has_cidr:
            step4_details.append(f"{node['wg_ip']}: OK")
        else:
            step4_ok = False
            missing = []
            if not has_wg:
                missing.append("WG IP")
            if not has_cidr:
                missing.append("CIDR")
            step4_details.append(f"{node['wg_ip']}: missing {'+'.join(missing)}")
    results.append(("AllowedIPs", step4_ok, ", ".join(step4_details) if step4_details else "No peers"))

    # Step 5: Flannel routes
    console.print("[bold]Step 5:[/bold] Flannel routes...")
    routes_result = run_ansible(
        config,
        "shell",
        "ip route show | grep 'dev flannel.1'",
        hosts="k3s_server[0]",
        timeout=15,
    )
    step5_ok = True
    step5_details = []
    for node in nodes:
        if node.get('pod_cidr') and node['pod_cidr'] in routes_result.stdout:
            step5_details.append(f"{node['pod_cidr']}: OK")
        else:
            step5_ok = False
            step5_details.append(f"{node.get('pod_cidr', 'N/A')}: missing")
    results.append(("Flannel routes", step5_ok, ", ".join(step5_details) if step5_details else "No routes"))

    # Step 6: FDB entries
    console.print("[bold]Step 6:[/bold] FDB entries...")
    fdb_result = run_ansible(
        config,
        "shell",
        "bridge fdb show dev flannel.1 | grep 10.200",
        hosts="k3s_server[0]",
        timeout=15,
    )
    step6_ok = True
    step6_details = []
    for node in nodes:
        if node['wg_ip'] in fdb_result.stdout:
            step6_details.append(f"{node['wg_ip']}: OK")
        else:
            step6_ok = False
            step6_details.append(f"{node['wg_ip']}: missing")
    results.append(("FDB entries", step6_ok, ", ".join(step6_details) if step6_details else "No entries"))

    # Step 7: CronJob status
    console.print("[bold]Step 7:[/bold] CronJob status...")
    cj_result = run_kubectl(
        config,
        ["get", "cronjob", "-n", "kube-system", "wireguard-reconcile", "-o", "json"],
        timeout=15,
    )
    step7_ok = False
    step7_details = "Not found"
    if cj_result.returncode == 0:
        cj_data = parse_json_output(cj_result.stdout)
        suspended = cj_data.get("spec", {}).get("suspend", False)
        last_success = cj_data.get("status", {}).get("lastSuccessfulTime", "")
        if not suspended and last_success:
            step7_ok = True
            step7_details = f"Active, last success: {last_success[:19]}"
        elif suspended:
            step7_details = "Suspended"
        else:
            step7_details = "Never succeeded"
    results.append(("CronJob", step7_ok, step7_details))

    # Summary
    print_header("Validation Summary")

    summary_table = Table()
    summary_table.add_column("Step", style="cyan")
    summary_table.add_column("Status")
    summary_table.add_column("Details")

    all_ok = True
    for name, ok, details in results:
        status_str = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
        if not ok:
            all_ok = False
        summary_table.add_row(name, status_str, details[:60])

    console.print(summary_table)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    console.print(f"\nPassed: {passed}/{total}")

    if all_ok:
        console.print("\n[green]All validation checks passed[/green]")
    else:
        console.print("\n[red]Some validation checks failed[/red]")
        if fix:
            console.print("\nAttempting automatic fixes...")
            console.print("Running 'clustermgr wg reconcile --fix'...")
            # Trigger reconciliation
            recon_result = run_ansible(
                config,
                "shell",
                "/usr/local/bin/wireguard-peer-reconcile.sh 2>&1 | tail -5",
                hosts="k3s_server[0]",
                timeout=60,
            )
            if recon_result.returncode == 0:
                console.print("[green]Reconciliation script executed[/green]")
            else:
                console.print("[yellow]Reconciliation may have issues[/yellow]")
            console.print("\nRe-run 'clustermgr wg validate' to verify fixes")
        else:
            console.print("\nRun with --fix to attempt automatic remediation")
            console.print("See docs/runbooks/WIREGUARD-NETWORK-VALIDATION.md")
        ctx.exit(1)


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
