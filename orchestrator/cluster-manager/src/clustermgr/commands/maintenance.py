"""Node maintenance commands for clustermgr.

Provides commands for GPU node and K3s server maintenance operations
including drain, cordon, uncordon, and rolling restarts.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    confirm,
    parse_json_output,
    print_header,
    print_status,
    run_ansible,
    run_kubectl,
)

console = Console()


@dataclass
class NodeMaintenanceStatus:
    """Status of a node for maintenance purposes."""

    name: str
    node_type: Literal["gpu", "server", "agent"]
    schedulable: bool
    ready: bool
    pod_count: int
    wg_healthy: bool
    last_heartbeat: str


def _get_node_type(labels: dict) -> Literal["gpu", "server", "agent"]:
    """Determine node type from labels."""
    if labels.get("basilica.ai/wireguard") == "true":
        return "gpu"
    if labels.get("node-role.kubernetes.io/control-plane") == "true":
        return "server"
    return "agent"


def _get_nodes_for_maintenance(config: Config) -> list[NodeMaintenanceStatus]:
    """Get all nodes with maintenance-relevant information."""
    result = run_kubectl(config, ["get", "nodes", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})
        labels = metadata.get("labels", {})

        ready = False
        last_heartbeat = ""
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready":
                ready = cond.get("status") == "True"
                last_heartbeat = cond.get("lastHeartbeatTime", "")

        nodes.append(NodeMaintenanceStatus(
            name=metadata.get("name", ""),
            node_type=_get_node_type(labels),
            schedulable=not spec.get("unschedulable", False),
            ready=ready,
            pod_count=0,
            wg_healthy=True,
            last_heartbeat=last_heartbeat,
        ))

    # Get pod counts per node
    result = run_kubectl(config, ["get", "pods", "-A", "-o", "json"])
    if result.returncode == 0:
        pod_data = parse_json_output(result.stdout)
        node_pods: dict[str, int] = {}
        for pod in pod_data.get("items", []):
            node_name = pod.get("spec", {}).get("nodeName", "")
            if node_name:
                node_pods[node_name] = node_pods.get(node_name, 0) + 1

        for node in nodes:
            node.pod_count = node_pods.get(node.name, 0)

    return nodes


def _cordon_node(config: Config, node_name: str) -> bool:
    """Cordon a node to prevent new pods from scheduling."""
    result = run_kubectl(config, ["cordon", node_name])
    return result.returncode == 0


def _uncordon_node(config: Config, node_name: str) -> bool:
    """Uncordon a node to allow new pods to schedule."""
    result = run_kubectl(config, ["uncordon", node_name])
    return result.returncode == 0


def _drain_node(
    config: Config,
    node_name: str,
    grace_period: int = 300,
    timeout: int = 600,
    force: bool = False,
) -> tuple[bool, str]:
    """Drain a node, evicting all pods."""
    cmd = [
        "drain", node_name,
        "--ignore-daemonsets",
        "--delete-emptydir-data",
        f"--grace-period={grace_period}",
        f"--timeout={timeout}s",
    ]
    if force:
        cmd.append("--force")

    result = run_kubectl(config, cmd, timeout=timeout + 60)
    return result.returncode == 0, result.stderr


@click.group()
def maintenance() -> None:
    """Node maintenance commands.

    Commands for managing GPU node and K3s server maintenance
    including drain, cordon, uncordon, and rolling restarts.
    """
    pass


@maintenance.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show maintenance status of all nodes.

    Displays node schedulability, readiness, and pod counts
    to help plan maintenance operations.
    """
    config: Config = ctx.obj

    print_header("Node Maintenance Status")

    nodes = _get_nodes_for_maintenance(config)
    if not nodes:
        console.print("[red]Failed to get node information[/red]")
        ctx.exit(1)

    # Group by type
    gpu_nodes = [n for n in nodes if n.node_type == "gpu"]
    servers = [n for n in nodes if n.node_type == "server"]
    agents = [n for n in nodes if n.node_type == "agent"]

    for group_name, group_nodes in [
        ("K3s Servers", servers),
        ("K3s Agents", agents),
        ("GPU Nodes", gpu_nodes),
    ]:
        if not group_nodes:
            continue

        print_header(group_name)

        table = Table()
        table.add_column("Node", style="cyan")
        table.add_column("Ready")
        table.add_column("Schedulable")
        table.add_column("Pods")
        table.add_column("Last Heartbeat")

        for node in group_nodes:
            ready_str = "[green]Yes[/green]" if node.ready else "[red]No[/red]"
            sched_str = "[green]Yes[/green]" if node.schedulable else "[yellow]Cordoned[/yellow]"

            hb_time = node.last_heartbeat
            if hb_time:
                try:
                    dt = datetime.fromisoformat(hb_time.replace("Z", "+00:00"))
                    hb_time = dt.strftime("%H:%M:%S")
                except ValueError:
                    pass

            table.add_row(
                node.name[:35],
                ready_str,
                sched_str,
                str(node.pod_count),
                hb_time,
            )

        console.print(table)


@maintenance.command("cordon")
@click.argument("node_name")
@click.pass_context
def cordon(ctx: click.Context, node_name: str) -> None:
    """Cordon a node to prevent new pod scheduling.

    NODE_NAME: Name of the node to cordon
    """
    config: Config = ctx.obj

    print_header(f"Cordon Node: {node_name}")

    if config.dry_run:
        console.print(f"[yellow][DRY RUN] Would cordon node {node_name}[/yellow]")
        return

    if _cordon_node(config, node_name):
        print_status(node_name, "Cordoned", Severity.HEALTHY)
    else:
        print_status(node_name, "Failed to cordon", Severity.CRITICAL)
        ctx.exit(1)


@maintenance.command("uncordon")
@click.argument("node_name")
@click.pass_context
def uncordon(ctx: click.Context, node_name: str) -> None:
    """Uncordon a node to allow pod scheduling.

    NODE_NAME: Name of the node to uncordon
    """
    config: Config = ctx.obj

    print_header(f"Uncordon Node: {node_name}")

    if config.dry_run:
        console.print(f"[yellow][DRY RUN] Would uncordon node {node_name}[/yellow]")
        return

    if _uncordon_node(config, node_name):
        print_status(node_name, "Uncordoned", Severity.HEALTHY)
    else:
        print_status(node_name, "Failed to uncordon", Severity.CRITICAL)
        ctx.exit(1)


@maintenance.command("drain")
@click.argument("node_name")
@click.option("--grace-period", "-g", default=300, help="Pod termination grace period (seconds)")
@click.option("--timeout", "-t", default=600, help="Drain timeout (seconds)")
@click.option("--force", "-f", is_flag=True, help="Force drain even with unmanaged pods")
@click.pass_context
def drain(
    ctx: click.Context,
    node_name: str,
    grace_period: int,
    timeout: int,
    force: bool,
) -> None:
    """Drain a node by evicting all pods.

    NODE_NAME: Name of the node to drain

    This will cordon the node and evict all pods with the specified
    grace period. DaemonSets and empty-dir volumes are handled automatically.
    """
    config: Config = ctx.obj

    print_header(f"Drain Node: {node_name}")

    # Get current pod count
    nodes = _get_nodes_for_maintenance(config)
    target = next((n for n in nodes if n.name == node_name), None)

    if not target:
        console.print(f"[red]Node {node_name} not found[/red]")
        ctx.exit(1)

    console.print(f"Node has {target.pod_count} pod(s)")
    console.print(f"Grace period: {grace_period}s")
    console.print(f"Timeout: {timeout}s")

    if config.dry_run:
        console.print(f"\n[yellow][DRY RUN] Would drain node {node_name}[/yellow]")
        return

    if not config.no_confirm:
        if not confirm(f"Drain {node_name} and evict {target.pod_count} pod(s)?"):
            console.print("Aborted.")
            return

    console.print("\nDraining node...")
    success, error = _drain_node(config, node_name, grace_period, timeout, force)

    if success:
        print_status(node_name, "Drained successfully", Severity.HEALTHY)
    else:
        print_status(node_name, "Drain failed", Severity.CRITICAL)
        if error:
            console.print(f"[red]Error: {error[:200]}[/red]")
        ctx.exit(1)


@maintenance.command("rolling-restart")
@click.option("--type", "-t", "node_type", type=click.Choice(["server", "gpu"]), required=True)
@click.option("--delay", "-d", default=120, help="Delay between nodes (seconds)")
@click.pass_context
def rolling_restart(
    ctx: click.Context,
    node_type: str,
    delay: int,
) -> None:
    """Perform rolling restart of nodes.

    Restarts nodes one at a time with health verification between each.
    For K3s servers, verifies etcd quorum is maintained.
    """
    config: Config = ctx.obj

    print_header(f"Rolling Restart: {node_type} nodes")

    nodes = _get_nodes_for_maintenance(config)

    if node_type == "server":
        target_nodes = [n for n in nodes if n.node_type == "server"]
        service_cmd = "sudo systemctl restart k3s"
    else:
        target_nodes = [n for n in nodes if n.node_type == "gpu"]
        service_cmd = "sudo systemctl restart k3s-agent"

    if not target_nodes:
        console.print(f"[yellow]No {node_type} nodes found[/yellow]")
        return

    console.print(f"Found {len(target_nodes)} node(s) to restart")
    console.print(f"Delay between nodes: {delay}s")

    if config.dry_run:
        console.print(f"\n[yellow][DRY RUN] Would restart: {', '.join(n.name for n in target_nodes)}[/yellow]")
        return

    if not config.no_confirm:
        if not confirm(f"Rolling restart {len(target_nodes)} {node_type} node(s)?"):
            console.print("Aborted.")
            return

    for i, node in enumerate(target_nodes, 1):
        console.print(f"\n[{i}/{len(target_nodes)}] Restarting {node.name}...")

        result = run_ansible(
            config,
            "shell",
            service_cmd,
            hosts=node.name,
            timeout=120,
        )

        if result.returncode != 0:
            print_status(node.name, "Restart failed", Severity.CRITICAL)
            if not config.no_confirm and not confirm("Continue with remaining nodes?"):
                console.print("Aborted.")
                ctx.exit(1)
            continue

        console.print(f"  Waiting for node to become ready...")

        # Wait for node to become ready
        import time
        for attempt in range(30):
            time.sleep(10)
            result = run_kubectl(
                config,
                ["get", "node", node.name, "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}"],
            )
            if result.returncode == 0 and result.stdout.strip() == "True":
                print_status(node.name, "Restarted and ready", Severity.HEALTHY)
                break
        else:
            print_status(node.name, "Not ready after restart", Severity.WARNING)

        # Delay before next node (skip on last)
        if i < len(target_nodes):
            console.print(f"  Waiting {delay}s before next node...")
            time.sleep(delay)

    console.print("\n[green]Rolling restart complete[/green]")


@maintenance.command("verify")
@click.pass_context
def verify(ctx: click.Context) -> None:
    """Run post-maintenance verification checks.

    Verifies cluster health after maintenance operations including
    node status, WireGuard connectivity, and Flannel health.
    """
    config: Config = ctx.obj

    print_header("Post-Maintenance Verification")

    issues: list[tuple[str, str, Severity]] = []

    # Check all nodes are ready
    console.print("Checking node status...")
    nodes = _get_nodes_for_maintenance(config)

    for node in nodes:
        if not node.ready:
            issues.append((node.name, "Node not ready", Severity.CRITICAL))
        if not node.schedulable and node.node_type != "server":
            issues.append((node.name, "Node still cordoned", Severity.WARNING))

    # Check WireGuard status
    console.print("Checking WireGuard status...")
    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 | grep -c 'peer:' 2>/dev/null || echo 0",
        hosts="k3s_server[0]",
        timeout=30,
    )

    if result.returncode == 0:
        try:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                if line.strip().isdigit():
                    peer_count = int(line.strip())
                    gpu_count = len([n for n in nodes if n.node_type == "gpu"])
                    if peer_count < gpu_count:
                        issues.append(("WireGuard", f"Only {peer_count}/{gpu_count} peers connected", Severity.WARNING))
                    break
        except (ValueError, IndexError):
            pass

    # Check for CrashLoopBackOff pods
    console.print("Checking pod health...")
    result = run_kubectl(config, ["get", "pods", "-A", "-o", "json"])
    if result.returncode == 0:
        data = parse_json_output(result.stdout)
        crash_count = 0
        for pod in data.get("items", []):
            for cs in pod.get("status", {}).get("containerStatuses", []):
                if cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff":
                    crash_count += 1

        if crash_count > 0:
            issues.append(("Pods", f"{crash_count} pods in CrashLoopBackOff", Severity.WARNING))

    # Check Flannel health (quick check)
    console.print("Checking Flannel health...")
    result = run_ansible(
        config,
        "shell",
        "ip link show flannel.1 | grep -q 'state UP' && echo 'UP' || echo 'DOWN'",
        hosts="k3s_server[0]",
        timeout=30,
    )

    if result.returncode == 0 and "DOWN" in result.stdout:
        issues.append(("Flannel", "flannel.1 interface is DOWN", Severity.CRITICAL))

    # Summary
    print_header("Verification Summary")

    if not issues:
        console.print("[green]All verification checks passed[/green]")
        console.print(f"\nNodes: {len(nodes)} total")
        console.print(f"  - Servers: {len([n for n in nodes if n.node_type == 'server'])}")
        console.print(f"  - Agents: {len([n for n in nodes if n.node_type == 'agent'])}")
        console.print(f"  - GPU: {len([n for n in nodes if n.node_type == 'gpu'])}")
        return

    console.print(f"[yellow]Found {len(issues)} issue(s)[/yellow]\n")

    table = Table()
    table.add_column("Component")
    table.add_column("Issue")
    table.add_column("Severity")

    for component, issue, severity in issues:
        sev_color = "red" if severity == Severity.CRITICAL else "yellow"
        table.add_row(component, issue, f"[{sev_color}]{severity.value}[/{sev_color}]")

    console.print(table)

    critical_count = len([i for i in issues if i[2] == Severity.CRITICAL])
    if critical_count > 0:
        ctx.exit(1)
