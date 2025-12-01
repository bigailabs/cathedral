"""UserDeployment troubleshooting commands for clustermgr."""

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


@dataclass
class UserDeploymentStatus:
    """Status information for a UserDeployment."""

    name: str
    namespace: str
    user_id: str
    image: str
    replicas: int
    ready_replicas: int
    port: int
    state: str
    public_url: str
    endpoint: str
    message: str
    created: str
    storage_enabled: bool
    gpu_count: int
    conditions: list[dict]


def _get_userdeployment(config: Config, name: str, namespace: str | None) -> dict | None:
    """Get a single UserDeployment by name."""
    if namespace:
        cmd = ["get", "userdeployment", name, "-n", namespace, "-o", "json"]
    else:
        cmd = ["get", "userdeployment", name, "-A", "-o", "json"]

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return None

    return parse_json_output(result.stdout)


def _find_userdeployment_namespace(config: Config, name: str) -> str | None:
    """Find the namespace for a UserDeployment by name."""
    result = run_kubectl(config, ["get", "userdeployment", "-A", "-o", "json"])
    if result.returncode != 0:
        return None

    data = parse_json_output(result.stdout)
    for item in data.get("items", []):
        if item.get("metadata", {}).get("name") == name:
            return item["metadata"]["namespace"]

    return None


def _parse_userdeployment(item: dict) -> UserDeploymentStatus:
    """Parse a UserDeployment resource into status dataclass."""
    metadata = item.get("metadata", {})
    spec = item.get("spec", {})
    status = item.get("status", {})

    resources = spec.get("resources", {})
    gpus = resources.get("gpus", {})
    storage = spec.get("storage", {})
    persistent = storage.get("persistent", {})

    return UserDeploymentStatus(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        user_id=spec.get("userId", ""),
        image=spec.get("image", ""),
        replicas=spec.get("replicas", 1),
        ready_replicas=status.get("replicasReady", 0),
        port=spec.get("port", 8080),
        state=status.get("state", "Unknown"),
        public_url=status.get("publicUrl", ""),
        endpoint=status.get("endpoint", ""),
        message=status.get("message", ""),
        created=metadata.get("creationTimestamp", ""),
        storage_enabled=persistent.get("enabled", False),
        gpu_count=gpus.get("count", 0),
        conditions=status.get("conditions", []),
    )


def _get_related_pods(config: Config, name: str, namespace: str) -> list[dict]:
    """Get pods related to a UserDeployment."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "json"],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    pods = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        spec = item.get("spec", {})

        container_statuses = status.get("containerStatuses", [])
        ready_count = sum(1 for c in container_statuses if c.get("ready", False))
        total_containers = len(container_statuses)
        restart_count = sum(c.get("restartCount", 0) for c in container_statuses)

        waiting_reasons = []
        for cs in container_statuses:
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting.get("reason"):
                waiting_reasons.append(f"{cs['name']}: {waiting['reason']}")

        pods.append({
            "name": metadata.get("name", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": f"{ready_count}/{total_containers}",
            "ready_count": ready_count,
            "total_containers": total_containers,
            "restarts": restart_count,
            "node": spec.get("nodeName", ""),
            "ip": status.get("podIP", ""),
            "waiting_reasons": waiting_reasons,
            "containers": [c.get("name", "") for c in spec.get("containers", [])],
        })

    return pods


def _get_related_service(config: Config, name: str, namespace: str) -> dict | None:
    """Get the Service for a UserDeployment."""
    svc_name = f"s-{name}"
    result = run_kubectl(
        config,
        ["get", "service", svc_name, "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return None

    return parse_json_output(result.stdout)


def _get_related_httproute(config: Config, name: str, namespace: str) -> dict | None:
    """Get the HTTPRoute for a UserDeployment."""
    route_name = f"ud-{name}"
    result = run_kubectl(
        config,
        ["get", "httproute", route_name, "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return None

    return parse_json_output(result.stdout)


def _get_deployment_events(
    config: Config, name: str, namespace: str, limit: int = 20
) -> list[dict]:
    """Get events for a UserDeployment and its pods."""
    events = []

    result = run_kubectl(
        config,
        [
            "get", "events", "-n", namespace,
            "--field-selector", f"involvedObject.name={name}",
            "-o", "json",
        ],
    )
    if result.returncode == 0:
        data = parse_json_output(result.stdout)
        events.extend(data.get("items", []))

    pods = _get_related_pods(config, name, namespace)
    for pod in pods:
        result = run_kubectl(
            config,
            [
                "get", "events", "-n", namespace,
                "--field-selector", f"involvedObject.name={pod['name']}",
                "-o", "json",
            ],
        )
        if result.returncode == 0:
            data = parse_json_output(result.stdout)
            events.extend(data.get("items", []))

    events.sort(key=lambda x: x.get("lastTimestamp") or x.get("eventTime") or "", reverse=True)
    return events[:limit]


def _get_all_userdeployments(config: Config) -> list[UserDeploymentStatus]:
    """Get all UserDeployments across all namespaces."""
    result = run_kubectl(config, ["get", "userdeployments", "-A", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    return [_parse_userdeployment(item) for item in data.get("items", [])]


@click.group()
def ud() -> None:
    """UserDeployment troubleshooting commands.

    Commands for inspecting, debugging, and managing UserDeployment
    custom resources and their related Kubernetes resources.
    """
    pass


@ud.command()
@click.argument("name")
@click.option("--namespace", "-n", help="Namespace (auto-detected if not specified)")
@click.pass_context
def inspect(ctx: click.Context, name: str, namespace: str | None) -> None:
    """Deep inspection of a UserDeployment.

    Shows spec, status, conditions, and all related resources including
    pods, service, HTTPRoute, and NetworkPolicy.
    """
    config: Config = ctx.obj

    if not namespace:
        namespace = _find_userdeployment_namespace(config, name)
        if not namespace:
            console.print(f"[red]UserDeployment '{name}' not found[/red]")
            return

    print_header(f"UserDeployment: {namespace}/{name}")

    ud_raw = _get_userdeployment(config, name, namespace)
    if not ud_raw:
        console.print(f"[red]Failed to get UserDeployment '{name}'[/red]")
        return

    ud_status = _parse_userdeployment(ud_raw)

    state_color = {
        "Active": "green",
        "Running": "green",
        "Ready": "green",
        "Pending": "yellow",
        "Creating": "yellow",
        "Failed": "red",
        "Error": "red",
    }.get(ud_status.state, "white")

    console.print(Panel(
        f"[bold]State:[/bold] [{state_color}]{ud_status.state}[/{state_color}]\n"
        f"[bold]User:[/bold] {ud_status.user_id}\n"
        f"[bold]Image:[/bold] {ud_status.image}\n"
        f"[bold]Replicas:[/bold] {ud_status.ready_replicas}/{ud_status.replicas}\n"
        f"[bold]Port:[/bold] {ud_status.port}\n"
        f"[bold]Storage:[/bold] {'Enabled (FUSE)' if ud_status.storage_enabled else 'None'}\n"
        f"[bold]GPUs:[/bold] {ud_status.gpu_count or 'None'}\n"
        f"[bold]Created:[/bold] {ud_status.created}",
        title="Specification",
    ))

    if ud_status.public_url or ud_status.endpoint:
        console.print(Panel(
            f"[bold]Public URL:[/bold] {ud_status.public_url or 'N/A'}\n"
            f"[bold]Endpoint:[/bold] {ud_status.endpoint or 'N/A'}",
            title="Endpoints",
        ))

    if ud_status.message:
        console.print(Panel(
            ud_status.message,
            title="Status Message",
            border_style="yellow" if "error" in ud_status.message.lower() else "dim",
        ))

    if ud_status.conditions:
        print_header("Conditions")
        for cond in ud_status.conditions:
            cond_type = cond.get("type", "Unknown")
            cond_status = cond.get("status", "Unknown")
            reason = cond.get("reason", "")
            message = cond.get("message", "")

            severity = Severity.HEALTHY if cond_status == "True" else Severity.CRITICAL
            print_status(cond_type, f"{cond_status} - {reason}", severity)
            if message:
                console.print(f"    [dim]{message}[/dim]")

    print_header("Related Pods")
    pods = _get_related_pods(config, name, namespace)
    if not pods:
        console.print("  [yellow]No pods found[/yellow]")
    else:
        table = Table()
        table.add_column("Pod", style="cyan")
        table.add_column("Phase")
        table.add_column("Ready")
        table.add_column("Restarts")
        table.add_column("Node")
        table.add_column("IP")

        for pod in pods:
            phase_color = {
                "Running": "green",
                "Pending": "yellow",
                "Failed": "red",
                "Succeeded": "cyan",
            }.get(pod["phase"], "white")

            restart_style = "red" if pod["restarts"] > 5 else "yellow" if pod["restarts"] > 0 else ""
            restart_str = f"[{restart_style}]{pod['restarts']}[/{restart_style}]" if restart_style else str(pod["restarts"])

            table.add_row(
                pod["name"],
                f"[{phase_color}]{pod['phase']}[/{phase_color}]",
                pod["ready"],
                restart_str,
                pod["node"] or "-",
                pod["ip"] or "-",
            )

        console.print(table)

        for pod in pods:
            if pod["waiting_reasons"]:
                console.print(f"  [yellow]Waiting: {', '.join(pod['waiting_reasons'])}[/yellow]")

    print_header("Related Service")
    svc = _get_related_service(config, name, namespace)
    if svc:
        svc_spec = svc.get("spec", {})
        ports = svc_spec.get("ports", [])
        port_str = ", ".join(f"{p.get('port')}:{p.get('targetPort')}" for p in ports)
        console.print(f"  Name: s-{name}")
        console.print(f"  Type: {svc_spec.get('type', 'ClusterIP')}")
        console.print(f"  Ports: {port_str}")
        console.print(f"  Selector: {svc_spec.get('selector', {})}")
    else:
        console.print("  [red]Service not found[/red]")

    print_header("Related HTTPRoute")
    route = _get_related_httproute(config, name, namespace)
    if route:
        route_spec = route.get("spec", {})
        hostnames = route_spec.get("hostnames", [])
        parent_refs = route_spec.get("parentRefs", [])
        route_status = route.get("status", {})
        parents = route_status.get("parents", [])

        console.print(f"  Name: ud-{name}")
        console.print(f"  Hostnames: {', '.join(hostnames) if hostnames else 'N/A'}")
        console.print(f"  Parent Gateway: {parent_refs[0].get('name', 'N/A') if parent_refs else 'N/A'}")

        for parent in parents:
            conditions = parent.get("conditions", [])
            for cond in conditions:
                cond_status = cond.get("status", "Unknown")
                reason = cond.get("reason", "")
                severity = Severity.HEALTHY if cond_status == "True" else Severity.WARNING
                print_status(f"  {cond.get('type', 'Unknown')}", f"{cond_status} ({reason})", severity)
    else:
        console.print("  [yellow]HTTPRoute not found[/yellow]")

    print_header("NetworkPolicy")
    result = run_kubectl(
        config,
        ["get", "networkpolicy", f"{name}-netpol", "-n", namespace, "-o", "json"],
    )
    if result.returncode == 0:
        netpol = parse_json_output(result.stdout)
        netpol_spec = netpol.get("spec", {})
        console.print(f"  Name: {name}-netpol")
        console.print(f"  Pod Selector: {netpol_spec.get('podSelector', {})}")
        console.print(f"  Policy Types: {netpol_spec.get('policyTypes', [])}")
    else:
        result = run_kubectl(
            config,
            ["get", "networkpolicy", "-n", namespace, "-o", "json"],
        )
        if result.returncode == 0:
            data = parse_json_output(result.stdout)
            policies = [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]
            console.print(f"  [dim]Available policies: {', '.join(policies) if policies else 'None'}[/dim]")


@ud.command()
@click.argument("name")
@click.option("--namespace", "-n", help="Namespace (auto-detected if not specified)")
@click.option("--container", "-c", default="main", help="Container name (default: main)")
@click.option("--tail", "-t", default=100, help="Number of lines to show")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--all-containers", "-a", is_flag=True, help="Show logs from all containers")
@click.pass_context
def logs(
    ctx: click.Context,
    name: str,
    namespace: str | None,
    container: str,
    tail: int,
    follow: bool,
    all_containers: bool,
) -> None:
    """Stream logs from UserDeployment pods.

    By default shows the main container. Use -c to specify a different
    container (e.g., fuse-storage for FUSE sidecar) or -a for all containers.
    """
    config: Config = ctx.obj

    if not namespace:
        namespace = _find_userdeployment_namespace(config, name)
        if not namespace:
            console.print(f"[red]UserDeployment '{name}' not found[/red]")
            return

    pods = _get_related_pods(config, name, namespace)
    if not pods:
        console.print(f"[red]No pods found for UserDeployment '{name}'[/red]")
        return

    for pod in pods:
        print_header(f"Logs: {pod['name']}")

        if all_containers:
            for cont_name in pod["containers"]:
                console.print(f"\n[bold cyan]--- Container: {cont_name} ---[/bold cyan]")
                cmd = [
                    "logs", "-n", namespace, pod["name"],
                    "-c", cont_name,
                    f"--tail={tail}",
                    "--timestamps=true",
                ]
                if follow:
                    cmd.append("-f")

                result = run_kubectl(config, cmd, timeout=60 if not follow else 300)
                if result.returncode == 0:
                    console.print(result.stdout)
                else:
                    console.print(f"[red]Failed to get logs: {result.stderr}[/red]")
        else:
            cmd = [
                "logs", "-n", namespace, pod["name"],
                "-c", container,
                f"--tail={tail}",
                "--timestamps=true",
            ]
            if follow:
                cmd.append("-f")

            result = run_kubectl(config, cmd, timeout=60 if not follow else 300)
            if result.returncode == 0:
                console.print(result.stdout)
            else:
                console.print(f"[red]Failed to get logs: {result.stderr}[/red]")
                if "container" in result.stderr.lower():
                    console.print(f"[dim]Available containers: {', '.join(pod['containers'])}[/dim]")


@ud.command()
@click.argument("name")
@click.option("--namespace", "-n", help="Namespace (auto-detected if not specified)")
@click.option("--limit", "-l", default=20, help="Number of events to show")
@click.pass_context
def events(ctx: click.Context, name: str, namespace: str | None, limit: int) -> None:
    """Show Kubernetes events for a UserDeployment and its pods."""
    config: Config = ctx.obj

    if not namespace:
        namespace = _find_userdeployment_namespace(config, name)
        if not namespace:
            console.print(f"[red]UserDeployment '{name}' not found[/red]")
            return

    print_header(f"Events: {namespace}/{name}")

    all_events = _get_deployment_events(config, name, namespace, limit)

    if not all_events:
        console.print("[yellow]No events found[/yellow]")
        return

    table = Table()
    table.add_column("Time", style="dim")
    table.add_column("Type")
    table.add_column("Reason")
    table.add_column("Object")
    table.add_column("Message", max_width=60)

    for event in all_events:
        timestamp = event.get("lastTimestamp") or event.get("eventTime") or ""
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                timestamp = dt.strftime("%H:%M:%S")
            except (ValueError, AttributeError):
                timestamp = timestamp[:19]

        event_type = event.get("type", "Normal")
        type_color = "yellow" if event_type == "Warning" else "green"

        involved = event.get("involvedObject", {})
        obj_name = f"{involved.get('kind', 'Unknown')}/{involved.get('name', 'unknown')}"

        table.add_row(
            timestamp,
            f"[{type_color}]{event_type}[/{type_color}]",
            event.get("reason", ""),
            obj_name,
            event.get("message", "")[:60],
        )

    console.print(table)


@ud.command()
@click.argument("name")
@click.option("--namespace", "-n", help="Namespace (auto-detected if not specified)")
@click.pass_context
def restart(ctx: click.Context, name: str, namespace: str | None) -> None:
    """Restart pods for a UserDeployment.

    Deletes all pods, allowing the deployment to recreate them.
    """
    config: Config = ctx.obj

    if not namespace:
        namespace = _find_userdeployment_namespace(config, name)
        if not namespace:
            console.print(f"[red]UserDeployment '{name}' not found[/red]")
            return

    pods = _get_related_pods(config, name, namespace)
    if not pods:
        console.print(f"[yellow]No pods found for UserDeployment '{name}'[/yellow]")
        return

    print_header(f"Restart: {namespace}/{name}")
    console.print(f"Found {len(pods)} pod(s) to restart:")
    for pod in pods:
        console.print(f"  - {pod['name']} ({pod['phase']})")

    if config.dry_run:
        console.print("\n[yellow][DRY RUN] Would delete pods[/yellow]")
        return

    if not config.no_confirm:
        if not confirm(f"Restart {len(pods)} pod(s)?"):
            console.print("Aborted.")
            return

    for pod in pods:
        result = run_kubectl(
            config,
            ["delete", "pod", pod["name"], "-n", namespace, "--grace-period=30"],
        )
        if result.returncode == 0:
            console.print(f"  [green]Deleted {pod['name']}[/green]")
        else:
            console.print(f"  [red]Failed to delete {pod['name']}: {result.stderr}[/red]")

    console.print("\n[dim]New pods will be created by the deployment controller[/dim]")


@ud.command()
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--unhealthy", "-u", is_flag=True, help="Show only unhealthy deployments")
@click.pass_context
def health(ctx: click.Context, namespace: str | None, unhealthy: bool) -> None:
    """Check health of all UserDeployments.

    Shows a summary of all UserDeployments with their health status,
    highlighting any issues that need attention.
    """
    config: Config = ctx.obj

    print_header("UserDeployment Health Check")

    all_uds = _get_all_userdeployments(config)

    if namespace:
        all_uds = [ud for ud in all_uds if ud.namespace == namespace]

    if not all_uds:
        console.print("[yellow]No UserDeployments found[/yellow]")
        return

    healthy_count = 0
    unhealthy_count = 0
    pending_count = 0
    issues: list[tuple[UserDeploymentStatus, str]] = []

    for ud_status in all_uds:
        is_healthy = ud_status.state in ("Active", "Running", "Ready")
        is_pending = ud_status.state in ("Pending", "Creating")
        replicas_ok = ud_status.ready_replicas >= ud_status.replicas

        if is_healthy and replicas_ok:
            healthy_count += 1
        elif is_pending:
            pending_count += 1
            issues.append((ud_status, f"State: {ud_status.state}"))
        else:
            unhealthy_count += 1
            reason = ud_status.message or f"State: {ud_status.state}, Replicas: {ud_status.ready_replicas}/{ud_status.replicas}"
            issues.append((ud_status, reason))

    console.print(f"Total: {len(all_uds)} deployment(s)")
    console.print(f"  [green]Healthy: {healthy_count}[/green]")
    console.print(f"  [yellow]Pending: {pending_count}[/yellow]")
    console.print(f"  [red]Unhealthy: {unhealthy_count}[/red]")

    if unhealthy and not issues:
        console.print("\n[green]All deployments are healthy[/green]")
        return

    display_uds = [ud for ud, _ in issues] if unhealthy else all_uds

    if not display_uds:
        return

    print_header("Deployment Status")

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Replicas")
    table.add_column("Storage")
    table.add_column("GPUs")
    table.add_column("URL", max_width=40)

    for ud_status in display_uds:
        state_color = {
            "Active": "green",
            "Running": "green",
            "Ready": "green",
            "Pending": "yellow",
            "Creating": "yellow",
            "Failed": "red",
            "Error": "red",
        }.get(ud_status.state, "white")

        replicas_ok = ud_status.ready_replicas >= ud_status.replicas
        replicas_color = "green" if replicas_ok else "red"

        url = ud_status.public_url
        if len(url) > 40:
            url = url[:37] + "..."

        table.add_row(
            ud_status.namespace,
            ud_status.name,
            f"[{state_color}]{ud_status.state}[/{state_color}]",
            f"[{replicas_color}]{ud_status.ready_replicas}/{ud_status.replicas}[/{replicas_color}]",
            "FUSE" if ud_status.storage_enabled else "-",
            str(ud_status.gpu_count) if ud_status.gpu_count else "-",
            url or "-",
        )

    console.print(table)

    if issues:
        print_header("Issues Detected")
        for ud_status, reason in issues:
            console.print(f"  [red]{ud_status.namespace}/{ud_status.name}[/red]: {reason}")
