"""Cluster scaling and capacity commands for clustermgr.

Provides diagnostics for cluster capacity, scaling readiness,
and performance baselines based on NETWORK-SCALING-GUIDE.md.
"""

from dataclasses import dataclass
from typing import Literal

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    parse_json_output,
    print_header,
    print_status,
    run_ansible,
    run_kubectl,
)

console = Console()


# Architecture limits from NETWORK-SCALING-GUIDE.md
LIMITS = {
    "k3s_servers_max": 7,
    "k3s_agents_max": 100,
    "gpu_nodes_max": 250,
    "pods_per_node_max": 110,
    "total_pods_max": 10000,
    "pod_cidr_nodes_max": 256,
    "wireguard_peers_max": 250,
}

# Alert thresholds
THRESHOLDS = {
    "wireguard_latency_warn": 20,
    "wireguard_handshake_stale": 180,
    "fdb_entries_min": 2,
}


@dataclass
class ClusterCapacity:
    """Current cluster capacity metrics."""

    k3s_servers: int
    k3s_agents: int
    gpu_nodes: int
    total_nodes: int
    total_pods: int
    wg_peers: int
    fdb_entries: int
    used_pod_cidrs: int


@dataclass
class ScalingRecommendation:
    """Recommendation for scaling operations."""

    category: str
    message: str
    severity: Severity
    action: str


def _get_cluster_capacity(config: Config) -> ClusterCapacity:
    """Gather current cluster capacity metrics."""
    result = run_kubectl(config, ["get", "nodes", "-o", "json"])
    if result.returncode != 0:
        return ClusterCapacity(0, 0, 0, 0, 0, 0, 0, 0)

    data = parse_json_output(result.stdout)
    k3s_servers = 0
    k3s_agents = 0
    gpu_nodes = 0
    used_cidrs = set()

    for item in data.get("items", []):
        labels = item.get("metadata", {}).get("labels", {})
        pod_cidr = item.get("spec", {}).get("podCIDR", "")

        if pod_cidr:
            used_cidrs.add(pod_cidr)

        if labels.get("basilica.ai/wireguard") == "true":
            gpu_nodes += 1
        elif labels.get("node-role.kubernetes.io/control-plane") == "true":
            k3s_servers += 1
        else:
            k3s_agents += 1

    total_nodes = k3s_servers + k3s_agents + gpu_nodes

    # Get pod count
    result = run_kubectl(config, ["get", "pods", "-A", "--no-headers"])
    total_pods = len(result.stdout.strip().split("\n")) if result.returncode == 0 and result.stdout.strip() else 0

    # Get WireGuard peer count from first server
    wg_peers = 0
    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 2>/dev/null | grep -c 'peer:' || echo 0",
        hosts="k3s_server[0]",
        timeout=30,
    )
    if result.returncode == 0:
        try:
            for line in result.stdout.strip().split("\n"):
                if line.strip().isdigit():
                    wg_peers = int(line.strip())
                    break
        except ValueError:
            pass

    # Get FDB entry count
    fdb_entries = 0
    result = run_ansible(
        config,
        "shell",
        "bridge fdb show dev flannel.1 2>/dev/null | wc -l || echo 0",
        hosts="k3s_server[0]",
        timeout=30,
    )
    if result.returncode == 0:
        try:
            for line in result.stdout.strip().split("\n"):
                if line.strip().isdigit():
                    fdb_entries = int(line.strip())
                    break
        except ValueError:
            pass

    return ClusterCapacity(
        k3s_servers=k3s_servers,
        k3s_agents=k3s_agents,
        gpu_nodes=gpu_nodes,
        total_nodes=total_nodes,
        total_pods=total_pods,
        wg_peers=wg_peers,
        fdb_entries=fdb_entries,
        used_pod_cidrs=len(used_cidrs),
    )


def _analyze_scaling_readiness(capacity: ClusterCapacity) -> list[ScalingRecommendation]:
    """Analyze capacity and generate scaling recommendations."""
    recommendations: list[ScalingRecommendation] = []

    # Check GPU node capacity
    gpu_utilization = (capacity.gpu_nodes / LIMITS["gpu_nodes_max"]) * 100
    if gpu_utilization > 80:
        recommendations.append(ScalingRecommendation(
            category="GPU Nodes",
            message=f"At {gpu_utilization:.0f}% of WireGuard peer limit ({capacity.gpu_nodes}/{LIMITS['gpu_nodes_max']})",
            severity=Severity.CRITICAL,
            action="Consider sharding into multiple clusters or hub-spoke WireGuard topology",
        ))
    elif gpu_utilization > 60:
        recommendations.append(ScalingRecommendation(
            category="GPU Nodes",
            message=f"At {gpu_utilization:.0f}% of WireGuard peer limit",
            severity=Severity.WARNING,
            action="Plan for cluster sharding if growth continues",
        ))

    # Check K3s server capacity
    if capacity.gpu_nodes > 100 and capacity.k3s_servers < 5:
        recommendations.append(ScalingRecommendation(
            category="K3s Servers",
            message=f"Only {capacity.k3s_servers} servers for {capacity.gpu_nodes} GPU nodes",
            severity=Severity.WARNING,
            action="Consider adding K3s server for redundancy and load distribution",
        ))

    # Check pod CIDR capacity
    cidr_utilization = (capacity.used_pod_cidrs / LIMITS["pod_cidr_nodes_max"]) * 100
    if cidr_utilization > 80:
        recommendations.append(ScalingRecommendation(
            category="Pod CIDRs",
            message=f"At {cidr_utilization:.0f}% of available /24 subnets ({capacity.used_pod_cidrs}/{LIMITS['pod_cidr_nodes_max']})",
            severity=Severity.WARNING,
            action="Plan for pod CIDR expansion or cluster sharding",
        ))

    # Check total pod capacity
    max_pods = capacity.total_nodes * LIMITS["pods_per_node_max"]
    pod_utilization = (capacity.total_pods / max_pods) * 100 if max_pods > 0 else 0
    if pod_utilization > 70:
        recommendations.append(ScalingRecommendation(
            category="Total Pods",
            message=f"At {pod_utilization:.0f}% of estimated pod capacity",
            severity=Severity.WARNING,
            action="Add nodes to increase pod capacity",
        ))

    # Scaling guidance based on current size
    if capacity.gpu_nodes > 50:
        recommendations.append(ScalingRecommendation(
            category="Reconcile Interval",
            message="Large cluster detected",
            severity=Severity.INFO,
            action="Consider increasing reconcile interval to 300s to reduce API load",
        ))

    if not recommendations:
        recommendations.append(ScalingRecommendation(
            category="Overall",
            message="Cluster capacity is healthy",
            severity=Severity.HEALTHY,
            action="No immediate scaling actions required",
        ))

    return recommendations


@click.group()
def scaling() -> None:
    """Cluster scaling and capacity commands.

    Commands for analyzing cluster capacity, scaling readiness,
    and generating recommendations based on current utilization.
    """
    pass


@scaling.command("capacity")
@click.pass_context
def capacity(ctx: click.Context) -> None:
    """Show current cluster capacity metrics.

    Displays node counts, pod capacity, and network resource
    utilization against configured limits.
    """
    config: Config = ctx.obj

    print_header("Cluster Capacity")

    cap = _get_cluster_capacity(config)

    # Node capacity table
    table = Table(title="Node Capacity")
    table.add_column("Resource")
    table.add_column("Current")
    table.add_column("Limit")
    table.add_column("Utilization")

    def add_capacity_row(name: str, current: int, limit: int) -> None:
        util = (current / limit) * 100 if limit > 0 else 0
        if util > 80:
            util_str = f"[red]{util:.0f}%[/red]"
        elif util > 60:
            util_str = f"[yellow]{util:.0f}%[/yellow]"
        else:
            util_str = f"[green]{util:.0f}%[/green]"

        table.add_row(name, str(current), str(limit), util_str)

    add_capacity_row("K3s Servers", cap.k3s_servers, LIMITS["k3s_servers_max"])
    add_capacity_row("K3s Agents", cap.k3s_agents, LIMITS["k3s_agents_max"])
    add_capacity_row("GPU Nodes", cap.gpu_nodes, LIMITS["gpu_nodes_max"])
    add_capacity_row("WireGuard Peers", cap.wg_peers, LIMITS["wireguard_peers_max"])
    add_capacity_row("Pod CIDRs", cap.used_pod_cidrs, LIMITS["pod_cidr_nodes_max"])

    console.print(table)

    # Pod capacity
    max_pods = cap.total_nodes * LIMITS["pods_per_node_max"]
    console.print(f"\nTotal Nodes: {cap.total_nodes}")
    console.print(f"Total Pods: {cap.total_pods} / {max_pods} estimated capacity")
    console.print(f"FDB Entries: {cap.fdb_entries}")


@scaling.command("readiness")
@click.pass_context
def readiness(ctx: click.Context) -> None:
    """Analyze scaling readiness and recommendations.

    Checks current capacity against limits and provides
    recommendations for scaling operations.
    """
    config: Config = ctx.obj

    print_header("Scaling Readiness Analysis")

    cap = _get_cluster_capacity(config)
    recommendations = _analyze_scaling_readiness(cap)

    table = Table()
    table.add_column("Category")
    table.add_column("Status")
    table.add_column("Recommended Action")

    for rec in recommendations:
        if rec.severity == Severity.CRITICAL:
            status_str = f"[red]{rec.message}[/red]"
        elif rec.severity == Severity.WARNING:
            status_str = f"[yellow]{rec.message}[/yellow]"
        elif rec.severity == Severity.HEALTHY:
            status_str = f"[green]{rec.message}[/green]"
        else:
            status_str = f"[dim]{rec.message}[/dim]"

        table.add_row(rec.category, status_str, rec.action)

    console.print(table)

    critical = [r for r in recommendations if r.severity == Severity.CRITICAL]
    if critical:
        ctx.exit(1)


@scaling.command("limits")
@click.pass_context
def limits(ctx: click.Context) -> None:
    """Display architecture limits and thresholds.

    Shows the configured limits for scaling operations
    based on NETWORK-SCALING-GUIDE.md.
    """
    print_header("Architecture Limits")

    table = Table()
    table.add_column("Resource")
    table.add_column("Limit")
    table.add_column("Notes")

    table.add_row("K3s Servers", str(LIMITS["k3s_servers_max"]), "etcd quorum limit")
    table.add_row("K3s Agents", str(LIMITS["k3s_agents_max"]), "VPC-local compute")
    table.add_row("GPU Nodes", str(LIMITS["gpu_nodes_max"]), "WireGuard peer limit")
    table.add_row("Pods per Node", str(LIMITS["pods_per_node_max"]), "K8s default limit")
    table.add_row("Total Pods", str(LIMITS["total_pods_max"]), "Cluster-wide limit")
    table.add_row("Pod CIDR Nodes", str(LIMITS["pod_cidr_nodes_max"]), "/24 subnets in /16")
    table.add_row("WireGuard Peers", str(LIMITS["wireguard_peers_max"]), "Per server limit")

    console.print(table)

    print_header("Alert Thresholds")

    table2 = Table()
    table2.add_column("Metric")
    table2.add_column("Threshold")
    table2.add_column("Unit")

    table2.add_row("WireGuard Latency", str(THRESHOLDS["wireguard_latency_warn"]), "ms (warning)")
    table2.add_row("Handshake Stale", str(THRESHOLDS["wireguard_handshake_stale"]), "seconds")
    table2.add_row("FDB Entries Min", str(THRESHOLDS["fdb_entries_min"]), "entries")

    console.print(table2)


@scaling.command("baselines")
@click.pass_context
def baselines(ctx: click.Context) -> None:
    """Check performance baselines.

    Measures current network performance and compares
    against expected baselines from the scaling guide.
    """
    config: Config = ctx.obj

    print_header("Performance Baselines")

    issues: list[tuple[str, str, Severity]] = []

    # Check WireGuard latency
    console.print("Measuring WireGuard latency...")
    result = run_ansible(
        config,
        "shell",
        "ping -c 3 -W 2 10.200.0.1 2>/dev/null | tail -1 | awk -F'/' '{print $5}'",
        hosts="k3s_server[0]",
        timeout=30,
    )

    wg_latency = None
    if result.returncode == 0:
        try:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and line.replace(".", "").isdigit():
                    wg_latency = float(line)
                    break
        except ValueError:
            pass

    if wg_latency is not None:
        if wg_latency > THRESHOLDS["wireguard_latency_warn"]:
            issues.append(("WireGuard Latency", f"{wg_latency:.1f}ms (expected < {THRESHOLDS['wireguard_latency_warn']}ms)", Severity.WARNING))
        else:
            issues.append(("WireGuard Latency", f"{wg_latency:.1f}ms", Severity.HEALTHY))
    else:
        issues.append(("WireGuard Latency", "Could not measure", Severity.INFO))

    # Check conntrack capacity
    console.print("Checking conntrack capacity...")
    result = run_ansible(
        config,
        "shell",
        "sysctl -n net.netfilter.nf_conntrack_max 2>/dev/null || echo 0",
        hosts="k3s_server[0]",
        timeout=30,
    )

    if result.returncode == 0:
        try:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.isdigit():
                    conntrack_max = int(line)
                    if conntrack_max >= 1048576:
                        issues.append(("Conntrack Max", f"{conntrack_max:,}", Severity.HEALTHY))
                    else:
                        issues.append(("Conntrack Max", f"{conntrack_max:,} (should be >= 1,048,576)", Severity.WARNING))
                    break
        except ValueError:
            pass

    # Check network buffer sizes
    console.print("Checking network buffers...")
    result = run_ansible(
        config,
        "shell",
        "sysctl -n net.core.rmem_max 2>/dev/null || echo 0",
        hosts="k3s_server[0]",
        timeout=30,
    )

    if result.returncode == 0:
        try:
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line.isdigit():
                    rmem_max = int(line)
                    if rmem_max >= 67108864:
                        issues.append(("rmem_max", f"{rmem_max:,}", Severity.HEALTHY))
                    else:
                        issues.append(("rmem_max", f"{rmem_max:,} (should be >= 67,108,864)", Severity.WARNING))
                    break
        except ValueError:
            pass

    # Display results
    table = Table()
    table.add_column("Metric")
    table.add_column("Value")
    table.add_column("Status")

    for metric, value, severity in issues:
        if severity == Severity.HEALTHY:
            status_str = "[green]OK[/green]"
        elif severity == Severity.WARNING:
            status_str = "[yellow]WARN[/yellow]"
        else:
            status_str = "[dim]INFO[/dim]"

        table.add_row(metric, value, status_str)

    console.print(table)

    warnings = [i for i in issues if i[2] == Severity.WARNING]
    if warnings:
        print_header("Tuning Recommendations")
        console.print("See NETWORK-SCALING-GUIDE.md for performance tuning instructions")
        console.print("Run: uv run clustermgr scaling limits")
