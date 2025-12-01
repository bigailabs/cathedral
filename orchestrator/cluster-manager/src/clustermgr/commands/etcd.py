"""etcd health and maintenance commands for clustermgr.

Provides commands for monitoring etcd cluster health, checking
member status, and performing maintenance operations like defrag.
"""

from dataclasses import dataclass
import re

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
    run_kubectl,
)

console = Console()


@dataclass
class EtcdMember:
    """etcd cluster member status."""

    name: str
    endpoint: str
    is_leader: bool
    db_size: int
    db_size_in_use: int
    raft_index: int
    raft_term: int
    healthy: bool


def _get_etcd_pods(config: Config) -> list[str]:
    """Get list of etcd pod names."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", "kube-system", "-l", "component=etcd", "-o", "name"],
    )
    if result.returncode != 0:
        return []

    return [p.replace("pod/", "") for p in result.stdout.strip().split("\n") if p]


def _etcd_exec(config: Config, pod: str, cmd: str) -> tuple[bool, str]:
    """Execute etcdctl command in etcd pod."""
    result = run_kubectl(
        config,
        [
            "exec", "-n", "kube-system", pod, "--",
            "sh", "-c", cmd,
        ],
        timeout=30,
    )
    return result.returncode == 0, result.stdout if result.returncode == 0 else result.stderr


@click.group()
def etcd() -> None:
    """etcd cluster health and maintenance commands.

    Commands for monitoring etcd cluster health, checking member
    status, and performing maintenance operations.
    """
    pass


@etcd.command("health")
@click.pass_context
def health(ctx: click.Context) -> None:
    """Check etcd cluster health.

    Verifies etcd endpoints are healthy and cluster has quorum.
    """
    config: Config = ctx.obj

    print_header("etcd Cluster Health")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    console.print(f"Found {len(pods)} etcd pod(s)\n")

    issues = []

    for pod in pods:
        success, output = _etcd_exec(config, pod, "etcdctl endpoint health --cluster -w json")

        if not success:
            print_status(pod, "Failed to check health", Severity.CRITICAL)
            issues.append((pod, "Health check failed"))
            continue

        try:
            import json
            health_data = json.loads(output)
            for ep in health_data:
                endpoint = ep.get("endpoint", "unknown")
                healthy = ep.get("health", False)
                took = ep.get("took", "N/A")

                if healthy:
                    print_status(f"{endpoint}", f"Healthy ({took})", Severity.HEALTHY)
                else:
                    print_status(f"{endpoint}", "Unhealthy", Severity.CRITICAL)
                    issues.append((endpoint, "Unhealthy"))
        except (json.JSONDecodeError, KeyError):
            # Try plain text parsing
            if "is healthy" in output:
                print_status(pod, "Healthy", Severity.HEALTHY)
            else:
                print_status(pod, "Unknown status", Severity.WARNING)
        break  # Only need to check from one pod

    if issues:
        console.print(f"\n[red]Found {len(issues)} issue(s)[/red]")
        ctx.exit(1)
    else:
        console.print("\n[green]etcd cluster is healthy[/green]")


@etcd.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show etcd member status including DB size and raft info.

    Displays detailed status of each etcd member including
    database size, leader status, and raft indices.
    """
    config: Config = ctx.obj

    print_header("etcd Member Status")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    success, output = _etcd_exec(pods[0], pods[0], "etcdctl endpoint status --cluster -w table")

    if success:
        # Display raw table output
        in_output = False
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("+") or line.startswith("|"):
                console.print(line)
    else:
        console.print("[yellow]Could not get table format, trying JSON...[/yellow]")

    # Try JSON format for more detailed info
    success, output = _etcd_exec(pods[0], pods[0], "etcdctl endpoint status --cluster -w json")

    if success:
        try:
            import json
            data = json.loads(output)

            table = Table()
            table.add_column("Endpoint")
            table.add_column("Leader")
            table.add_column("DB Size")
            table.add_column("Raft Index")
            table.add_column("Raft Term")

            for ep in data:
                endpoint = ep.get("Endpoint", "unknown")[:30]
                status_data = ep.get("Status", {})
                is_leader = status_data.get("header", {}).get("member_id") == status_data.get("leader")
                db_size = status_data.get("dbSize", 0)
                raft_index = status_data.get("raftIndex", 0)
                raft_term = status_data.get("raftTerm", 0)

                leader_str = "[green]Yes[/green]" if is_leader else "No"

                # Format DB size
                if db_size > 1_000_000_000:
                    size_str = f"{db_size / 1_000_000_000:.2f} GB"
                elif db_size > 1_000_000:
                    size_str = f"{db_size / 1_000_000:.2f} MB"
                else:
                    size_str = f"{db_size / 1_000:.2f} KB"

                table.add_row(
                    endpoint,
                    leader_str,
                    size_str,
                    str(raft_index),
                    str(raft_term),
                )

            console.print("\n")
            console.print(table)
        except (json.JSONDecodeError, KeyError) as e:
            console.print(f"[yellow]Could not parse JSON: {e}[/yellow]")


@etcd.command("members")
@click.pass_context
def members(ctx: click.Context) -> None:
    """List etcd cluster members.

    Shows all members of the etcd cluster with their IDs
    and peer URLs.
    """
    config: Config = ctx.obj

    print_header("etcd Cluster Members")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    success, output = _etcd_exec(pods[0], pods[0], "etcdctl member list -w table")

    if success:
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("+") or line.startswith("|"):
                console.print(line)
    else:
        console.print(f"[red]Failed to list members: {output}[/red]")
        ctx.exit(1)


@etcd.command("defrag")
@click.option("--all", "-a", "all_members", is_flag=True, help="Defrag all members")
@click.pass_context
def defrag(ctx: click.Context, all_members: bool) -> None:
    """Defragment etcd database to reclaim space.

    Defragmentation compacts the etcd database and reclaims
    disk space. Should be run periodically or when DB size
    grows significantly.
    """
    config: Config = ctx.obj

    print_header("etcd Defragmentation")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    if config.dry_run:
        console.print("[yellow][DRY RUN] Would defragment etcd[/yellow]")
        return

    if not config.no_confirm:
        if not confirm("Defragment etcd? This may briefly impact performance."):
            console.print("Aborted.")
            return

    if all_members:
        cmd = "etcdctl defrag --cluster"
    else:
        cmd = "etcdctl defrag"

    for pod in pods:
        console.print(f"Defragmenting {pod}...")
        success, output = _etcd_exec(config, pod, cmd)

        if success:
            print_status(pod, "Defragmented", Severity.HEALTHY)
        else:
            print_status(pod, f"Failed: {output[:50]}", Severity.WARNING)

        if not all_members:
            break

    console.print("\n[green]Defragmentation complete[/green]")


@etcd.command("alarms")
@click.pass_context
def alarms(ctx: click.Context) -> None:
    """Check for etcd alarms.

    Alarms indicate critical issues like NOSPACE (disk full)
    that require immediate attention.
    """
    config: Config = ctx.obj

    print_header("etcd Alarms")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    success, output = _etcd_exec(pods[0], pods[0], "etcdctl alarm list")

    if not success:
        console.print(f"[red]Failed to check alarms: {output}[/red]")
        ctx.exit(1)

    if output.strip():
        console.print("[red]Active Alarms:[/red]")
        for line in output.strip().split("\n"):
            console.print(f"  [red]{line}[/red]")
        ctx.exit(1)
    else:
        console.print("[green]No active alarms[/green]")


@etcd.command("compact")
@click.option("--revisions-to-keep", "-r", default=10000, help="Number of revisions to keep")
@click.pass_context
def compact(ctx: click.Context, revisions_to_keep: int) -> None:
    """Compact etcd history to remove old revisions.

    Compaction removes historical revisions older than the
    specified number. This reduces database size but makes
    old revisions unavailable.
    """
    config: Config = ctx.obj

    print_header("etcd Compaction")

    pods = _get_etcd_pods(config)
    if not pods:
        console.print("[red]No etcd pods found[/red]")
        ctx.exit(1)

    # Get current revision
    success, output = _etcd_exec(
        pods[0], pods[0],
        "etcdctl endpoint status -w json | head -1",
    )

    if not success:
        console.print("[red]Failed to get current revision[/red]")
        ctx.exit(1)

    try:
        import json
        data = json.loads(output)
        if isinstance(data, list):
            data = data[0]
        current_rev = data.get("Status", {}).get("header", {}).get("revision", 0)
    except (json.JSONDecodeError, KeyError, IndexError):
        console.print("[red]Could not parse revision[/red]")
        ctx.exit(1)

    target_rev = current_rev - revisions_to_keep

    if target_rev <= 0:
        console.print(f"Current revision: {current_rev}")
        console.print("[yellow]Not enough revisions to compact[/yellow]")
        return

    console.print(f"Current revision: {current_rev}")
    console.print(f"Compact to revision: {target_rev}")
    console.print(f"Keeping last {revisions_to_keep} revisions")

    if config.dry_run:
        console.print("\n[yellow][DRY RUN] Would compact to revision {target_rev}[/yellow]")
        return

    if not config.no_confirm:
        if not confirm(f"Compact etcd to revision {target_rev}?"):
            console.print("Aborted.")
            return

    success, output = _etcd_exec(pods[0], pods[0], f"etcdctl compact {target_rev}")

    if success:
        console.print(f"\n[green]Compacted to revision {target_rev}[/green]")
    else:
        console.print(f"\n[red]Compaction failed: {output}[/red]")
        ctx.exit(1)
