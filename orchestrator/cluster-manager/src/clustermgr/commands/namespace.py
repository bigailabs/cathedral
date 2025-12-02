"""Tenant namespace management commands for clustermgr."""

from dataclasses import dataclass
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.panel import Panel
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

REQUIRED_LABELS = [
    "pod-security.kubernetes.io/enforce",
    "basilica.ai/tenant",
]

REQUIRED_RBAC = [
    "user-workload-restricted",
    "user-workload-restricted-binding",
    "operator-elevated-binding",
]

REQUIRED_NETPOLS = [
    "default-deny-all",
    "allow-dns",
    "allow-internet-egress",
    "allow-ingress-from-envoy",
]


@dataclass
class NamespaceInfo:
    """Information about a tenant namespace."""

    name: str
    created: str
    labels: dict
    deployments: int
    pods: int
    services: int
    secrets: int
    netpols: int
    httproutes: int
    user_deployments: int


def _get_tenant_namespaces(config: Config) -> list[dict]:
    """Get all tenant namespaces with metadata."""
    result = run_kubectl(config, ["get", "namespaces", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    namespaces = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        name = metadata.get("name", "")
        if name.startswith("u-"):
            namespaces.append({
                "name": name,
                "created": metadata.get("creationTimestamp", ""),
                "labels": metadata.get("labels", {}),
                "annotations": metadata.get("annotations", {}),
                "phase": item.get("status", {}).get("phase", ""),
            })

    return sorted(namespaces, key=lambda x: x["name"])


def _count_resources(config: Config, namespace: str) -> dict[str, int]:
    """Count resources in a namespace."""
    counts: dict[str, int] = {}

    resource_types = [
        ("deployments", "deployments"),
        ("pods", "pods"),
        ("services", "services"),
        ("secrets", "secrets"),
        ("netpols", "networkpolicies"),
        ("httproutes", "httproutes"),
        ("user_deployments", "userdeployments"),
    ]

    for key, resource in resource_types:
        result = run_kubectl(
            config,
            ["get", resource, "-n", namespace, "-o", "json"],
            timeout=10,
        )
        if result.returncode == 0:
            data = parse_json_output(result.stdout)
            counts[key] = len(data.get("items", []))
        else:
            counts[key] = 0

    return counts


def _get_namespace_info(config: Config, namespace: str) -> NamespaceInfo | None:
    """Get detailed information about a namespace."""
    result = run_kubectl(
        config,
        ["get", "namespace", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return None

    data = parse_json_output(result.stdout)
    metadata = data.get("metadata", {})

    counts = _count_resources(config, namespace)

    return NamespaceInfo(
        name=namespace,
        created=metadata.get("creationTimestamp", ""),
        labels=metadata.get("labels", {}),
        deployments=counts.get("deployments", 0),
        pods=counts.get("pods", 0),
        services=counts.get("services", 0),
        secrets=counts.get("secrets", 0),
        netpols=counts.get("netpols", 0),
        httproutes=counts.get("httproutes", 0),
        user_deployments=counts.get("user_deployments", 0),
    )


def _check_rbac(config: Config, namespace: str) -> dict[str, bool]:
    """Check if required RBAC resources exist."""
    results = {}

    result = run_kubectl(
        config,
        ["get", "role", "user-workload-restricted", "-n", namespace],
    )
    results["user-workload-restricted"] = result.returncode == 0

    result = run_kubectl(
        config,
        ["get", "rolebinding", "user-workload-restricted-binding", "-n", namespace],
    )
    results["user-workload-restricted-binding"] = result.returncode == 0

    result = run_kubectl(
        config,
        ["get", "rolebinding", "operator-elevated-binding", "-n", namespace],
    )
    results["operator-elevated-binding"] = result.returncode == 0

    return results


def _check_secrets(config: Config, namespace: str) -> dict[str, bool]:
    """Check if required secrets exist."""
    results = {}

    result = run_kubectl(
        config,
        ["get", "secret", "basilica-r2-credentials", "-n", namespace],
    )
    results["basilica-r2-credentials"] = result.returncode == 0

    return results


def _find_orphaned_namespaces(config: Config) -> list[dict]:
    """Find namespaces that have no active UserDeployments."""
    namespaces = _get_tenant_namespaces(config)
    orphaned = []

    for ns in namespaces:
        result = run_kubectl(
            config,
            ["get", "userdeployments", "-n", ns["name"], "-o", "json"],
        )
        if result.returncode == 0:
            data = parse_json_output(result.stdout)
            items = data.get("items", [])

            if not items:
                result = run_kubectl(
                    config,
                    ["get", "pods", "-n", ns["name"], "-o", "json"],
                )
                pod_count = 0
                if result.returncode == 0:
                    pod_data = parse_json_output(result.stdout)
                    pod_count = len(pod_data.get("items", []))

                orphaned.append({
                    "name": ns["name"],
                    "created": ns["created"],
                    "pods": pod_count,
                })

    return orphaned


@click.group("namespace")
def namespace() -> None:
    """Tenant namespace management commands.

    Commands for listing, auditing, and managing tenant namespaces
    (u-* prefixed namespaces) used for UserDeployment isolation.
    """
    pass


@namespace.command("list")
@click.option("--details", "-d", is_flag=True, help="Show resource counts")
@click.pass_context
def list_namespaces(ctx: click.Context, details: bool) -> None:
    """List tenant namespaces with resource counts.

    Shows all u-* namespaces with their creation time and
    optionally the number of resources in each.
    """
    config: Config = ctx.obj

    print_header("Tenant Namespaces")

    namespaces = _get_tenant_namespaces(config)

    if not namespaces:
        console.print("[yellow]No tenant namespaces found[/yellow]")
        return

    console.print(f"Found {len(namespaces)} tenant namespace(s)")

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Created")
    table.add_column("Phase")

    if details:
        table.add_column("UDs")
        table.add_column("Pods")
        table.add_column("Services")
        table.add_column("NetPols")
        table.add_column("Routes")

    for ns in namespaces:
        created = ns["created"]
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                created = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, AttributeError):
                created = created[:16]

        phase = ns.get("phase", "Active")
        phase_color = "green" if phase == "Active" else "yellow"

        if details:
            counts = _count_resources(config, ns["name"])
            table.add_row(
                ns["name"],
                created,
                f"[{phase_color}]{phase}[/{phase_color}]",
                str(counts.get("user_deployments", 0)),
                str(counts.get("pods", 0)),
                str(counts.get("services", 0)),
                str(counts.get("netpols", 0)),
                str(counts.get("httproutes", 0)),
            )
        else:
            table.add_row(
                ns["name"],
                created,
                f"[{phase_color}]{phase}[/{phase_color}]",
            )

    console.print(table)


@namespace.command()
@click.argument("name")
@click.pass_context
def audit(ctx: click.Context, name: str) -> None:
    """Audit RBAC, NetworkPolicies, and Secrets for a namespace.

    Checks if all required resources are present and properly configured
    for the tenant namespace.
    """
    config: Config = ctx.obj

    if not name.startswith("u-"):
        name = f"u-{name}"

    print_header(f"Namespace Audit: {name}")

    ns_info = _get_namespace_info(config, name)
    if not ns_info:
        console.print(f"[red]Namespace '{name}' not found[/red]")
        return

    console.print(Panel(
        f"[bold]Created:[/bold] {ns_info.created}\n"
        f"[bold]UserDeployments:[/bold] {ns_info.user_deployments}\n"
        f"[bold]Pods:[/bold] {ns_info.pods}\n"
        f"[bold]Services:[/bold] {ns_info.services}\n"
        f"[bold]Secrets:[/bold] {ns_info.secrets}\n"
        f"[bold]NetworkPolicies:[/bold] {ns_info.netpols}\n"
        f"[bold]HTTPRoutes:[/bold] {ns_info.httproutes}",
        title="Resource Summary",
    ))

    print_header("Labels")
    for label in REQUIRED_LABELS:
        present = label in ns_info.labels
        severity = Severity.HEALTHY if present else Severity.WARNING
        value = ns_info.labels.get(label, "missing")
        print_status(label, value, severity)

    print_header("RBAC")
    rbac_status = _check_rbac(config, name)
    for resource, present in rbac_status.items():
        severity = Severity.HEALTHY if present else Severity.CRITICAL
        status = "Present" if present else "Missing"
        print_status(resource, status, severity)

    print_header("NetworkPolicies")
    result = run_kubectl(
        config,
        ["get", "networkpolicy", "-n", name, "-o", "json"],
    )
    if result.returncode == 0:
        data = parse_json_output(result.stdout)
        policies = {item.get("metadata", {}).get("name", "") for item in data.get("items", [])}

        for policy in REQUIRED_NETPOLS:
            present = policy in policies
            severity = Severity.HEALTHY if present else Severity.CRITICAL
            status = "Present" if present else "Missing"
            print_status(policy, status, severity)
    else:
        console.print("[red]Failed to list NetworkPolicies[/red]")

    print_header("Secrets")
    secrets_status = _check_secrets(config, name)
    for secret, present in secrets_status.items():
        severity = Severity.HEALTHY if present else Severity.WARNING
        status = "Present" if present else "Missing"
        print_status(secret, status, severity)

    print_header("Reference Grant")
    result = run_kubectl(
        config,
        ["get", "referencegrant", "-n", name, "-o", "json"],
    )
    if result.returncode == 0:
        data = parse_json_output(result.stdout)
        grants = data.get("items", [])
        if grants:
            for grant in grants:
                grant_name = grant.get("metadata", {}).get("name", "")
                print_status(grant_name, "Present", Severity.HEALTHY)
        else:
            print_status("ReferenceGrant", "Missing", Severity.WARNING)
    else:
        print_status("ReferenceGrant", "Cannot check", Severity.WARNING)

    print_header("Summary")
    all_ok = all(rbac_status.values()) and ns_info.netpols >= len(REQUIRED_NETPOLS)
    if all_ok:
        console.print("[green]Namespace is properly configured[/green]")
    else:
        console.print("[yellow]Namespace has configuration issues[/yellow]")
        console.print("\nRun 'clustermgr netpol audit -n {name} --fix' for remediation commands")


@namespace.command()
@click.option("--force", "-f", is_flag=True, help="Delete without confirmation")
@click.pass_context
def cleanup(ctx: click.Context, force: bool) -> None:
    """Find and clean orphaned namespaces.

    Lists namespaces that have no UserDeployments and offers
    to delete them if they appear to be abandoned.
    """
    config: Config = ctx.obj

    print_header("Orphaned Namespace Detection")

    console.print("Scanning tenant namespaces...")
    orphaned = _find_orphaned_namespaces(config)

    if not orphaned:
        console.print("[green]No orphaned namespaces found[/green]")
        return

    console.print(f"Found {len(orphaned)} potentially orphaned namespace(s)")

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Created")
    table.add_column("Pods")
    table.add_column("Status")

    for ns in orphaned:
        created = ns["created"]
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - dt
                age_str = f"{age.days}d" if age.days > 0 else f"{age.seconds // 3600}h"
            except (ValueError, AttributeError):
                age_str = "unknown"
        else:
            age_str = "unknown"

        status = "[red]Empty[/red]" if ns["pods"] == 0 else f"[yellow]{ns['pods']} pods[/yellow]"

        table.add_row(
            ns["name"],
            f"{created[:10]} ({age_str} ago)",
            str(ns["pods"]),
            status,
        )

    console.print(table)

    empty_namespaces = [ns for ns in orphaned if ns["pods"] == 0]

    if not empty_namespaces:
        console.print("\n[yellow]All orphaned namespaces have running pods - manual review needed[/yellow]")
        return

    if config.dry_run:
        console.print(f"\n[yellow][DRY RUN] Would delete {len(empty_namespaces)} empty namespace(s)[/yellow]")
        return

    if not force and not config.no_confirm:
        if not confirm(f"Delete {len(empty_namespaces)} empty namespace(s)?"):
            console.print("Aborted.")
            return

    print_header("Deleting Empty Namespaces")

    for ns in empty_namespaces:
        result = run_kubectl(
            config,
            ["delete", "namespace", ns["name"], "--grace-period=30"],
            timeout=60,
        )
        if result.returncode == 0:
            console.print(f"  [green]Deleted {ns['name']}[/green]")
        else:
            console.print(f"  [red]Failed to delete {ns['name']}: {result.stderr}[/red]")


@namespace.command()
@click.argument("name")
@click.pass_context
def resources(ctx: click.Context, name: str) -> None:
    """Show all resources in a tenant namespace.

    Lists all Kubernetes resources including UserDeployments,
    pods, services, secrets, and routing configuration.
    """
    config: Config = ctx.obj

    if not name.startswith("u-"):
        name = f"u-{name}"

    print_header(f"Resources: {name}")

    resource_types = [
        ("UserDeployments", "userdeployments"),
        ("Deployments", "deployments"),
        ("Pods", "pods"),
        ("Services", "services"),
        ("Secrets", "secrets"),
        ("NetworkPolicies", "networkpolicies"),
        ("HTTPRoutes", "httproutes"),
        ("ReferenceGrants", "referencegrants"),
    ]

    for display_name, resource in resource_types:
        result = run_kubectl(
            config,
            ["get", resource, "-n", name, "-o", "json"],
            timeout=10,
        )
        if result.returncode != 0:
            continue

        data = parse_json_output(result.stdout)
        items = data.get("items", [])

        if not items:
            continue

        console.print(f"\n[bold cyan]{display_name}[/bold cyan] ({len(items)})")

        for item in items:
            metadata = item.get("metadata", {})
            item_name = metadata.get("name", "")
            status = item.get("status", {})

            status_str = ""
            if resource == "pods":
                phase = status.get("phase", "Unknown")
                phase_color = {"Running": "green", "Pending": "yellow", "Failed": "red"}.get(phase, "white")
                status_str = f" [{phase_color}]({phase})[/{phase_color}]"
            elif resource == "userdeployments":
                state = status.get("state", "Unknown")
                state_color = {"Active": "green", "Running": "green", "Pending": "yellow"}.get(state, "red")
                status_str = f" [{state_color}]({state})[/{state_color}]"

            console.print(f"  - {item_name}{status_str}")


@namespace.command()
@click.pass_context
def summary(ctx: click.Context) -> None:
    """Show summary statistics for all tenant namespaces.

    Displays aggregate statistics including total resources,
    health status, and capacity usage.
    """
    config: Config = ctx.obj

    print_header("Tenant Namespace Summary")

    namespaces = _get_tenant_namespaces(config)

    if not namespaces:
        console.print("[yellow]No tenant namespaces found[/yellow]")
        return

    total_uds = 0
    total_pods = 0
    total_services = 0
    healthy_ns = 0
    unhealthy_ns = 0

    for ns in namespaces:
        counts = _count_resources(config, ns["name"])
        total_uds += counts.get("user_deployments", 0)
        total_pods += counts.get("pods", 0)
        total_services += counts.get("services", 0)

        netpols = counts.get("netpols", 0)
        if netpols >= len(REQUIRED_NETPOLS):
            healthy_ns += 1
        else:
            unhealthy_ns += 1

    console.print(Panel(
        f"[bold]Total Namespaces:[/bold] {len(namespaces)}\n"
        f"[bold]Healthy:[/bold] [green]{healthy_ns}[/green]\n"
        f"[bold]Unhealthy:[/bold] [red]{unhealthy_ns}[/red]\n"
        f"\n"
        f"[bold]Total UserDeployments:[/bold] {total_uds}\n"
        f"[bold]Total Pods:[/bold] {total_pods}\n"
        f"[bold]Total Services:[/bold] {total_services}",
        title="Summary",
    ))

    if unhealthy_ns > 0:
        console.print("\n[yellow]Run 'clustermgr namespace audit <name>' to check individual namespaces[/yellow]")
