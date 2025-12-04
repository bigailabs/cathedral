"""Storage diagnostics commands for R2/S3 connectivity.

This module provides commands to diagnose storage connectivity issues,
particularly DNS resolution and R2/S3 endpoint accessibility from
fuse-daemon pods.

Key diagnostic areas:
- R2 endpoint DNS resolution from all nodes
- S3 connectivity from storage pods
- Storage sync status and errors
"""

from dataclasses import dataclass

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    parse_json_output,
    print_header,
    print_status,
    run_kubectl,
)

console = Console()

FUSE_NAMESPACE = "basilica-storage"
FUSE_DAEMONSET = "fuse-daemon"
R2_ENDPOINT_DOMAIN = "r2.cloudflarestorage.com"
R2_TEST_DOMAINS = [
    "r2.cloudflarestorage.com",
    "pypi.org",
    "kubernetes.default.svc.cluster.local",
]


@dataclass
class NodeStorageStatus:
    """Storage connectivity status per node."""

    node_name: str
    pod_name: str | None
    pod_ready: bool
    dns_ok: bool
    r2_dns_ok: bool
    r2_ip: str | None
    sync_errors: list[str]


def _get_fuse_pods(config: Config) -> dict[str, dict]:
    """Get fuse-daemon pods keyed by node name."""
    result = run_kubectl(
        config,
        [
            "get", "pods", "-n", FUSE_NAMESPACE,
            "-l", f"app.kubernetes.io/component={FUSE_DAEMONSET}",
            "-o", "json",
        ],
        timeout=30,
    )
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    pods: dict[str, dict] = {}

    for item in data.get("items", []):
        node = item.get("spec", {}).get("nodeName", "")
        if node:
            pods[node] = item

    return pods


def _test_dns_from_pod(config: Config, pod_name: str, domain: str) -> tuple[bool, str | None]:
    """Test DNS resolution from a pod.

    Returns (success, resolved_ip).
    """
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "getent", "hosts", domain],
        timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip():
        parts = result.stdout.strip().split()
        return True, parts[0] if parts else None
    return False, None


def _get_pod_sync_errors(config: Config, pod_name: str, tail: int = 100) -> list[str]:
    """Check fuse-daemon logs for sync/storage errors."""
    result = run_kubectl(
        config,
        ["logs", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon", f"--tail={tail}"],
        timeout=30,
    )
    if result.returncode != 0:
        return []

    errors: list[str] = []
    error_patterns = [
        "temporary failure in name resolution",
        "connection refused",
        "timeout",
        "dispatch failure",
        "s3 error",
        "access denied",
        "no such bucket",
    ]

    for line in result.stdout.lower().split("\n"):
        for pattern in error_patterns:
            if pattern in line:
                errors.append(pattern)
                break

    return list(set(errors))


def _check_storage_connectivity(config: Config, include_servers: bool = False) -> list[NodeStorageStatus]:
    """Check storage connectivity from nodes with fuse-daemon.

    Args:
        include_servers: If True, include server nodes even without fuse-daemon.
                        Default is False (only show nodes with fuse-daemon).
    """
    fuse_pods = _get_fuse_pods(config)

    if include_servers:
        # Get all nodes
        result = run_kubectl(
            config,
            ["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
            timeout=15,
        )
        all_nodes = result.stdout.strip().split() if result.returncode == 0 else []
    else:
        # Only check nodes that have fuse-daemon pods
        all_nodes = list(fuse_pods.keys())

    statuses: list[NodeStorageStatus] = []

    for node in sorted(all_nodes):
        status = NodeStorageStatus(
            node_name=node,
            pod_name=None,
            pod_ready=False,
            dns_ok=False,
            r2_dns_ok=False,
            r2_ip=None,
            sync_errors=[],
        )

        pod = fuse_pods.get(node)
        if not pod:
            statuses.append(status)
            continue

        pod_name = pod.get("metadata", {}).get("name", "")
        status.pod_name = pod_name

        # Check pod readiness
        for cond in pod.get("status", {}).get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                status.pod_ready = True
                break

        if not status.pod_ready:
            statuses.append(status)
            continue

        # Test general DNS
        dns_ok, _ = _test_dns_from_pod(config, pod_name, "kubernetes.default.svc.cluster.local")
        status.dns_ok = dns_ok

        # Test R2 DNS
        r2_ok, r2_ip = _test_dns_from_pod(config, pod_name, R2_ENDPOINT_DOMAIN)
        status.r2_dns_ok = r2_ok
        status.r2_ip = r2_ip

        # Check for sync errors
        status.sync_errors = _get_pod_sync_errors(config, pod_name)

        statuses.append(status)

    return statuses


@click.group()
def storage() -> None:
    """Storage diagnostics for R2/S3 connectivity.

    Commands to diagnose storage connectivity issues including
    DNS resolution and R2 endpoint accessibility from fuse-daemon pods.
    """
    pass


@storage.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show storage connectivity status for all nodes.

    Checks DNS resolution and R2 connectivity from each fuse-daemon pod.
    """
    config: Config = ctx.obj

    print_header("Storage Connectivity Status")

    statuses = _check_storage_connectivity(config)
    if not statuses:
        console.print("[red]No nodes found[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Pod")
    table.add_column("Ready")
    table.add_column("DNS")
    table.add_column("R2 DNS")
    table.add_column("R2 IP")
    table.add_column("Errors")

    issues_found = 0
    for s in statuses:
        ready_str = "[green]Yes[/green]" if s.pod_ready else "[red]No[/red]"
        dns_str = "[green]OK[/green]" if s.dns_ok else "[red]FAIL[/red]"
        r2_str = "[green]OK[/green]" if s.r2_dns_ok else "[red]FAIL[/red]"

        if not s.pod_ready or not s.dns_ok or not s.r2_dns_ok:
            issues_found += 1

        errors_str = ", ".join(s.sync_errors[:2]) if s.sync_errors else "-"

        table.add_row(
            s.node_name[:30],
            s.pod_name[:20] if s.pod_name else "-",
            ready_str,
            dns_str if s.pod_ready else "-",
            r2_str if s.pod_ready else "-",
            s.r2_ip[:15] if s.r2_ip else "-",
            errors_str[:30],
        )

    console.print(table)

    if issues_found > 0:
        console.print(f"\n[red]Found {issues_found} node(s) with storage connectivity issues[/red]")
        console.print("\nTroubleshooting steps:")
        console.print("  1. Check DNS: clustermgr dns diagnose")
        console.print("  2. Check specific node: clustermgr fuse-troubleshoot <node> --deep")
        console.print("  3. Check CoreDNS endpoints: clustermgr dns endpoints")
        ctx.exit(1)
    else:
        console.print(f"\n[green]All {len(statuses)} nodes have working storage connectivity[/green]")


@storage.command("test")
@click.option("--node", "-n", help="Test specific node only")
@click.option("--domain", "-d", default=R2_ENDPOINT_DOMAIN, help="Domain to test")
@click.pass_context
def test_connectivity(ctx: click.Context, node: str | None, domain: str) -> None:
    """Test DNS resolution to storage endpoints.

    Tests R2/S3 endpoint DNS resolution from fuse-daemon pods.
    """
    config: Config = ctx.obj

    print_header(f"Testing DNS Resolution: {domain}")

    fuse_pods = _get_fuse_pods(config)
    if not fuse_pods:
        console.print("[red]No fuse-daemon pods found[/red]")
        ctx.exit(1)

    if node:
        if node not in fuse_pods:
            console.print(f"[red]No fuse-daemon pod on node {node}[/red]")
            ctx.exit(1)
        test_nodes = {node: fuse_pods[node]}
    else:
        test_nodes = fuse_pods

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Result")
    table.add_column("Resolved IP")

    failures = 0
    for node_name, pod in test_nodes.items():
        pod_name = pod.get("metadata", {}).get("name", "")
        if not pod_name:
            continue

        # Check pod is running
        phase = pod.get("status", {}).get("phase", "")
        if phase != "Running":
            table.add_row(node_name, f"[yellow]Pod {phase}[/yellow]", "-")
            continue

        ok, ip = _test_dns_from_pod(config, pod_name, domain)
        if ok:
            table.add_row(node_name, "[green]OK[/green]", ip or "-")
        else:
            table.add_row(node_name, "[red]FAILED[/red]", "-")
            failures += 1

    console.print(table)

    if failures > 0:
        console.print(f"\n[red]{failures} node(s) failed to resolve {domain}[/red]")
        console.print("\nThis indicates DNS issues. Run:")
        console.print("  clustermgr dns diagnose")
        console.print("  clustermgr dns endpoints")
        ctx.exit(1)
    else:
        console.print(f"\n[green]All nodes can resolve {domain}[/green]")


@storage.command("diagnose")
@click.pass_context
def diagnose(ctx: click.Context) -> None:
    """Run comprehensive storage diagnostics.

    Checks all storage-related components and identifies issues.
    """
    config: Config = ctx.obj

    print_header("Storage Diagnostics")
    issues: list[tuple[str, str, Severity]] = []

    # Check 1: fuse-daemon DaemonSet
    console.print("Checking fuse-daemon DaemonSet...")
    result = run_kubectl(
        config,
        ["get", "daemonset", "-n", FUSE_NAMESPACE, FUSE_DAEMONSET,
         "-o", "jsonpath={.status.desiredNumberScheduled}/{.status.numberReady}"],
        timeout=15,
    )
    if result.returncode != 0:
        issues.append(("fuse-daemon", "DaemonSet not found", Severity.CRITICAL))
    elif "/" in result.stdout:
        parts = result.stdout.strip().split("/")
        desired = int(parts[0]) if parts[0] else 0
        ready = int(parts[1]) if parts[1] else 0
        if ready < desired:
            issues.append(("fuse-daemon", f"Only {ready}/{desired} pods ready", Severity.WARNING))

    # Check 2: Storage connectivity from all nodes
    console.print("Checking storage connectivity...")
    statuses = _check_storage_connectivity(config)

    dns_failures = [s for s in statuses if s.pod_ready and not s.dns_ok]
    r2_failures = [s for s in statuses if s.pod_ready and not s.r2_dns_ok]
    pod_failures = [s for s in statuses if not s.pod_ready and s.pod_name]

    if dns_failures:
        nodes = ", ".join(s.node_name[:15] for s in dns_failures[:3])
        issues.append(("DNS", f"{len(dns_failures)} node(s) cannot resolve DNS: {nodes}", Severity.CRITICAL))

    if r2_failures:
        nodes = ", ".join(s.node_name[:15] for s in r2_failures[:3])
        issues.append(("R2", f"{len(r2_failures)} node(s) cannot resolve R2: {nodes}", Severity.CRITICAL))

    if pod_failures:
        nodes = ", ".join(s.node_name[:15] for s in pod_failures[:3])
        issues.append(("fuse-daemon", f"{len(pod_failures)} pod(s) not ready: {nodes}", Severity.WARNING))

    # Check 3: Sync errors
    console.print("Checking for sync errors...")
    nodes_with_errors = [s for s in statuses if s.sync_errors]
    if nodes_with_errors:
        error_types = set()
        for s in nodes_with_errors:
            error_types.update(s.sync_errors)
        issues.append(("Storage", f"{len(nodes_with_errors)} node(s) have sync errors: {', '.join(error_types)}", Severity.WARNING))

    # Summary
    print_header("Diagnostic Summary")

    if not issues:
        console.print("[green]No storage issues detected[/green]")
        console.print(f"\nChecked: {len(statuses)} nodes")
        ready_count = len([s for s in statuses if s.pod_ready])
        r2_ok_count = len([s for s in statuses if s.r2_dns_ok])
        console.print(f"  Pods ready: {ready_count}/{len(statuses)}")
        console.print(f"  R2 connectivity: {r2_ok_count}/{len(statuses)}")
        return

    critical = [i for i in issues if i[2] == Severity.CRITICAL]
    warnings = [i for i in issues if i[2] == Severity.WARNING]

    console.print(f"Found {len(issues)} issue(s): {len(critical)} critical, {len(warnings)} warnings\n")

    table = Table()
    table.add_column("Component", style="cyan")
    table.add_column("Issue")
    table.add_column("Severity")

    for component, issue, severity in issues:
        sev_color = "red" if severity == Severity.CRITICAL else "yellow"
        table.add_row(component, issue, f"[{sev_color}]{severity.value}[/{sev_color}]")

    console.print(table)

    if critical:
        print_header("Recommended Actions")
        if dns_failures or r2_failures:
            console.print("1. Check DNS configuration:")
            console.print("   clustermgr dns diagnose")
            console.print("   clustermgr dns endpoints")
            console.print("\n2. If nodes lack local DNS endpoints:")
            console.print("   Deploy CoreDNS as DaemonSet: kubectl apply -f orchestrator/k8s/core/coredns-daemonset.yaml")
        if pod_failures:
            console.print("\n3. Check failing pods:")
            console.print("   clustermgr fuse-troubleshoot --scan")
        ctx.exit(1)
