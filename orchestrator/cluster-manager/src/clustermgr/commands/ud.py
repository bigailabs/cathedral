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
    instance_name: str
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
        instance_name=spec.get("instanceName", ""),
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


@ud.command()
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--timeout", "-t", default=5, help="HTTP timeout in seconds")
@click.option("--unhealthy", "-u", is_flag=True, help="Show only unreachable endpoints")
@click.pass_context
def endpoints(ctx: click.Context, namespace: str | None, timeout: int, unhealthy: bool) -> None:
    """Check public endpoint reachability for UserDeployments.

    Tests HTTP connectivity to public domains of active UserDeployments.
    Shows response status codes and latency for each endpoint.
    """
    import subprocess
    import time

    config: Config = ctx.obj

    print_header("UserDeployment Public Endpoints")

    all_uds = _get_all_userdeployments(config)

    if namespace:
        all_uds = [ud for ud in all_uds if ud.namespace == namespace]

    # Filter to active deployments with public URLs
    active_uds = [
        ud for ud in all_uds
        if ud.state in ("Active", "Running", "Ready")
        and ud.ready_replicas > 0
    ]

    if not active_uds:
        console.print("[yellow]No active UserDeployments found[/yellow]")
        return

    # Get public hostnames from HTTPRoutes
    endpoints_to_check: list[tuple[UserDeploymentStatus, str]] = []

    for ud_status in active_uds:
        # HTTPRoute name uses instanceName, not the full deployment name
        instance = ud_status.instance_name or ud_status.name.replace("-deployment", "")
        route_name = f"ud-{instance}"

        result = run_kubectl(
            config,
            ["get", "httproute", route_name, "-n", ud_status.namespace, "-o", "json"],
        )
        if result.returncode != 0:
            continue

        route = parse_json_output(result.stdout)
        hostnames = route.get("spec", {}).get("hostnames", [])
        for hostname in hostnames:
            if hostname and "deployments.basilica.ai" in hostname:
                endpoints_to_check.append((ud_status, f"https://{hostname}"))

    if not endpoints_to_check:
        console.print("[yellow]No public endpoints found (no HTTPRoutes with hostnames)[/yellow]")
        return

    console.print(f"Checking {len(endpoints_to_check)} endpoint(s)...\n")

    results: list[tuple[UserDeploymentStatus, str, int, float, str]] = []

    for ud_status, url in endpoints_to_check:
        health_url = url.rstrip("/") + "/health"
        start = time.time()

        try:
            result = subprocess.run(
                [
                    "curl", "-s", "-o", "/dev/null",
                    "-w", "%{http_code}",
                    "--connect-timeout", str(timeout),
                    "--max-time", str(timeout + 2),
                    "-k",  # Allow self-signed certs
                    health_url,
                ],
                capture_output=True,
                text=True,
                timeout=timeout + 5,
            )
            latency = (time.time() - start) * 1000
            status_code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
            error = ""
        except subprocess.TimeoutExpired:
            latency = timeout * 1000
            status_code = 0
            error = "timeout"
        except Exception as e:
            latency = 0
            status_code = 0
            error = str(e)[:30]

        results.append((ud_status, url, status_code, latency, error))

    # Filter if unhealthy only
    if unhealthy:
        results = [r for r in results if r[2] < 200 or r[2] >= 400]

    if not results:
        if unhealthy:
            console.print("[green]All endpoints are reachable[/green]")
        return

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Deployment")
    table.add_column("URL", max_width=50)
    table.add_column("Status")
    table.add_column("Latency")
    table.add_column("Error")

    reachable = 0
    unreachable = 0

    for ud_status, url, status_code, latency, error in results:
        if 200 <= status_code < 400:
            status_str = f"[green]{status_code}[/green]"
            latency_str = f"{latency:.0f}ms"
            reachable += 1
        elif status_code == 0:
            status_str = "[red]ERR[/red]"
            latency_str = "-"
            unreachable += 1
        else:
            status_str = f"[yellow]{status_code}[/yellow]"
            latency_str = f"{latency:.0f}ms"
            unreachable += 1

        # Truncate URL for display
        display_url = url.replace("https://", "")
        if len(display_url) > 50:
            display_url = display_url[:47] + "..."

        table.add_row(
            ud_status.namespace,
            ud_status.name[:30],
            display_url,
            status_str,
            latency_str,
            error or "-",
        )

    console.print(table)

    console.print(f"\nSummary: {reachable} reachable, {unreachable} unreachable")

    if unreachable > 0:
        print_header("Troubleshooting")
        console.print("For unreachable endpoints, run:")
        console.print("  clustermgr ud troubleshoot <deployment-name>")


@ud.command()
@click.argument("name")
@click.option("--namespace", "-n", help="Namespace (auto-detected if not specified)")
@click.pass_context
def troubleshoot(ctx: click.Context, name: str, namespace: str | None) -> None:
    """Diagnose 503 errors for a UserDeployment.

    Checks the full request path from external access to the pod:
    1. DNS resolution for public hostname
    2. Gateway and Envoy proxy status
    3. HTTPRoute configuration and status
    4. Service and endpoints
    5. Pod readiness and health
    6. Network connectivity from Envoy to pod
    """
    import subprocess

    config: Config = ctx.obj

    if not namespace:
        namespace = _find_userdeployment_namespace(config, name)
        if not namespace:
            console.print(f"[red]UserDeployment '{name}' not found[/red]")
            return

    print_header(f"Troubleshooting: {namespace}/{name}")

    ud_raw = _get_userdeployment(config, name, namespace)
    if not ud_raw:
        console.print(f"[red]Failed to get UserDeployment '{name}'[/red]")
        return

    ud_status = _parse_userdeployment(ud_raw)
    instance = ud_status.instance_name or name.replace("-deployment", "")
    issues: list[tuple[str, str, Severity]] = []

    # Step 1: Check UserDeployment state
    console.print("\n[bold]1. UserDeployment State[/bold]")
    state_ok = ud_status.state in ("Active", "Running", "Ready")
    replicas_ok = ud_status.ready_replicas >= ud_status.replicas and ud_status.replicas > 0

    if state_ok:
        print_status("State", ud_status.state, Severity.HEALTHY)
    else:
        print_status("State", ud_status.state, Severity.CRITICAL)
        issues.append(("UserDeployment", f"State is {ud_status.state}", Severity.CRITICAL))

    if replicas_ok:
        print_status("Replicas", f"{ud_status.ready_replicas}/{ud_status.replicas}", Severity.HEALTHY)
    else:
        print_status("Replicas", f"{ud_status.ready_replicas}/{ud_status.replicas}", Severity.CRITICAL)
        issues.append(("UserDeployment", f"Only {ud_status.ready_replicas}/{ud_status.replicas} replicas ready", Severity.CRITICAL))

    # Step 2: Check pods
    console.print("\n[bold]2. Pod Status[/bold]")
    # Pods use instance name as label, not deployment name
    pods = _get_related_pods(config, instance, namespace)
    if not pods:
        print_status("Pods", "None found", Severity.CRITICAL)
        issues.append(("Pods", "No pods found for deployment", Severity.CRITICAL))
    else:
        for pod in pods:
            pod_ok = pod["phase"] == "Running" and pod["ready_count"] == pod["total_containers"]
            if pod_ok:
                print_status(pod["name"][:40], f"{pod['phase']} ({pod['ready']})", Severity.HEALTHY)
            else:
                print_status(pod["name"][:40], f"{pod['phase']} ({pod['ready']})", Severity.CRITICAL)
                issues.append(("Pod", f"{pod['name']}: {pod['phase']}, restarts={pod['restarts']}", Severity.CRITICAL))

            if pod["waiting_reasons"]:
                for reason in pod["waiting_reasons"]:
                    console.print(f"    [yellow]{reason}[/yellow]")

    # Step 3: Check Service and Endpoints
    console.print("\n[bold]3. Service & Endpoints[/bold]")
    # Service uses instance name, not deployment name
    svc = _get_related_service(config, instance, namespace)
    if not svc:
        print_status("Service", f"s-{instance} not found", Severity.CRITICAL)
        issues.append(("Service", "Service not found", Severity.CRITICAL))
    else:
        svc_spec = svc.get("spec", {})
        print_status("Service", f"s-{instance} -> {svc_spec.get('clusterIP')}:{svc_spec.get('ports', [{}])[0].get('port')}", Severity.HEALTHY)

    # Check endpoints
    ep_result = run_kubectl(
        config,
        ["get", "endpoints", f"s-{instance}", "-n", namespace, "-o", "json"],
    )
    if ep_result.returncode == 0:
        ep_data = parse_json_output(ep_result.stdout)
        subsets = ep_data.get("subsets", [])
        if subsets:
            addresses = []
            for subset in subsets:
                for addr in subset.get("addresses", []):
                    addresses.append(addr.get("ip", ""))
            if addresses:
                print_status("Endpoints", f"{len(addresses)} endpoint(s): {', '.join(addresses[:3])}", Severity.HEALTHY)
            else:
                print_status("Endpoints", "No ready addresses", Severity.CRITICAL)
                issues.append(("Endpoints", "No ready endpoint addresses", Severity.CRITICAL))
        else:
            print_status("Endpoints", "Empty (no backends)", Severity.CRITICAL)
            issues.append(("Endpoints", "No endpoint subsets - pods not matching selector?", Severity.CRITICAL))
    else:
        print_status("Endpoints", "Not found", Severity.CRITICAL)
        issues.append(("Endpoints", "Endpoints resource not found", Severity.CRITICAL))

    # Step 4: Check HTTPRoute
    console.print("\n[bold]4. HTTPRoute[/bold]")
    route_name = f"ud-{instance}"
    route_result = run_kubectl(
        config,
        ["get", "httproute", route_name, "-n", namespace, "-o", "json"],
    )
    if route_result.returncode != 0:
        print_status("HTTPRoute", f"{route_name} not found", Severity.CRITICAL)
        issues.append(("HTTPRoute", "Route not found", Severity.CRITICAL))
    else:
        route = parse_json_output(route_result.stdout)
        hostnames = route.get("spec", {}).get("hostnames", [])
        hostname = hostnames[0] if hostnames else "none"

        # Check route status
        parents = route.get("status", {}).get("parents", [])
        route_accepted = False
        for parent in parents:
            for cond in parent.get("conditions", []):
                if cond.get("type") == "Accepted" and cond.get("status") == "True":
                    route_accepted = True

        if route_accepted:
            print_status("HTTPRoute", f"{route_name} -> {hostname}", Severity.HEALTHY)
        else:
            print_status("HTTPRoute", f"{route_name} NOT ACCEPTED", Severity.CRITICAL)
            issues.append(("HTTPRoute", "Route not accepted by gateway", Severity.CRITICAL))

        # Check backend refs
        rules = route.get("spec", {}).get("rules", [])
        for rule in rules:
            for backend in rule.get("backendRefs", []):
                backend_name = backend.get("name", "")
                backend_port = backend.get("port", "")
                console.print(f"    Backend: {backend_name}:{backend_port}")

    # Step 5: Check Gateway
    console.print("\n[bold]5. Gateway & Envoy[/bold]")
    gw_result = run_kubectl(
        config,
        ["get", "gateway", "basilica-gateway", "-n", "basilica-system", "-o", "json"],
    )
    if gw_result.returncode == 0:
        gw = parse_json_output(gw_result.stdout)
        gw_conditions = gw.get("status", {}).get("conditions", [])
        gw_programmed = any(
            c.get("type") == "Programmed" and c.get("status") == "True"
            for c in gw_conditions
        )
        if gw_programmed:
            print_status("Gateway", "basilica-gateway programmed", Severity.HEALTHY)
        else:
            print_status("Gateway", "NOT programmed", Severity.CRITICAL)
            issues.append(("Gateway", "Gateway not programmed", Severity.CRITICAL))
    else:
        print_status("Gateway", "Not found", Severity.CRITICAL)
        issues.append(("Gateway", "basilica-gateway not found", Severity.CRITICAL))

    # Check Envoy pods
    envoy_result = run_kubectl(
        config,
        ["get", "pods", "-n", "envoy-gateway-system", "-l", "gateway.envoyproxy.io/owning-gateway-name=basilica-gateway", "-o", "json"],
    )
    if envoy_result.returncode == 0:
        envoy_data = parse_json_output(envoy_result.stdout)
        envoy_pods = envoy_data.get("items", [])
        ready_envoys = sum(
            1 for p in envoy_pods
            if p.get("status", {}).get("phase") == "Running"
        )
        if ready_envoys > 0:
            print_status("Envoy Pods", f"{ready_envoys}/{len(envoy_pods)} running", Severity.HEALTHY)
        else:
            print_status("Envoy Pods", "None running", Severity.CRITICAL)
            issues.append(("Envoy", "No Envoy proxy pods running", Severity.CRITICAL))

    # Step 6: Test connectivity to pod
    console.print("\n[bold]6. Network Connectivity[/bold]")
    if pods:
        target_ip = pods[0].get("ip", "")
        target_port = ud_status.port
        target_node = pods[0].get("node", "")

        if target_ip:
            # Test via service ClusterIP first (internal K8s networking)
            if svc:
                svc_ip = svc.get("spec", {}).get("clusterIP", "")
                svc_port = svc.get("spec", {}).get("ports", [{}])[0].get("port", target_port)
                if svc_ip:
                    # Use kubectl run to test connectivity
                    test_result = run_kubectl(
                        config,
                        [
                            "run", "nettest-tmp", "--rm", "-i", "--restart=Never",
                            "--image=busybox:1.36", "--",
                            "wget", "-q", "-O", "-", "--timeout=5",
                            f"http://{svc_ip}:{svc_port}/health",
                        ],
                        timeout=30,
                    )
                    if test_result.returncode == 0:
                        print_status("Service->Pod", f"{svc_ip}:{svc_port} reachable", Severity.HEALTHY)
                    else:
                        # Check if it's a timeout vs connection refused
                        if "timed out" in test_result.stderr.lower() or "timeout" in test_result.stderr.lower():
                            print_status("Service->Pod", f"{svc_ip}:{svc_port} TIMEOUT", Severity.CRITICAL)
                            issues.append(("Network", f"Connection to service {svc_ip}:{svc_port} timed out", Severity.CRITICAL))
                        elif "connection refused" in test_result.stderr.lower():
                            print_status("Service->Pod", f"{svc_ip}:{svc_port} CONNECTION REFUSED", Severity.CRITICAL)
                            issues.append(("Network", f"Connection refused - app not listening on port {target_port}?", Severity.CRITICAL))
                        else:
                            print_status("Service->Pod", f"{svc_ip}:{svc_port} UNREACHABLE", Severity.CRITICAL)
                            issues.append(("Network", f"Cannot reach service at {svc_ip}:{svc_port}", Severity.CRITICAL))

            # Also show pod location for context
            console.print(f"    Pod IP: {target_ip} on node: {target_node}")

            # Check if pod is on a GPU node (WireGuard)
            gpu_check = run_kubectl(
                config,
                ["get", "node", target_node, "-o", "jsonpath={.metadata.labels.basilica\\.ai/wireguard}"],
            )
            if gpu_check.returncode == 0 and gpu_check.stdout.strip() == "true":
                console.print("    [yellow]Pod is on GPU node (WireGuard) - check WireGuard/Flannel connectivity[/yellow]")
        else:
            print_status("Connectivity", "No pod IP available", Severity.WARNING)
    else:
        print_status("Connectivity", "Skipped (no pods found)", Severity.WARNING)

    # Step 7: DNS resolution
    console.print("\n[bold]7. DNS Resolution[/bold]")
    if route_result.returncode == 0:
        route = parse_json_output(route_result.stdout)
        hostnames = route.get("spec", {}).get("hostnames", [])
        if hostnames:
            hostname = hostnames[0]
            try:
                dns_result = subprocess.run(
                    ["nslookup", hostname],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if dns_result.returncode == 0 and "Address:" in dns_result.stdout:
                    print_status("DNS", f"{hostname} resolves", Severity.HEALTHY)
                else:
                    print_status("DNS", f"{hostname} DOES NOT RESOLVE", Severity.CRITICAL)
                    issues.append(("DNS", f"Hostname {hostname} not resolving", Severity.CRITICAL))
            except Exception as e:
                print_status("DNS", f"Check failed: {e}", Severity.WARNING)
        else:
            print_status("DNS", "No hostname configured", Severity.WARNING)

    # Summary
    print_header("Diagnosis Summary")

    if not issues:
        console.print("[green]No issues detected in the request path[/green]")
        console.print("\nIf 503 persists, check:")
        console.print("  - External load balancer/CDN configuration")
        console.print("  - TLS termination and certificate validity")
        console.print("  - Application health endpoint returning 200")
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

    # Remediation hints
    print_header("Remediation")
    seen = set()
    for component, issue, _ in issues:
        if component in seen:
            continue
        seen.add(component)

        if component == "UserDeployment":
            console.print("  - Check deployment spec: kubectl get userdeployment -n {ns} {name} -o yaml")
        elif component == "Pods":
            console.print(f"  - Check pod logs: clustermgr ud logs {name} -n {namespace}")
            console.print(f"  - Check pod events: clustermgr ud events {name} -n {namespace}")
        elif component == "Pod":
            console.print(f"  - Restart pods: clustermgr ud restart {name} -n {namespace}")
        elif component == "Service":
            console.print(f"  - Check service selector matches pod labels")
        elif component == "Endpoints":
            console.print("  - Verify pod labels match service selector")
            console.print("  - Check if pods are passing readiness probes")
        elif component == "HTTPRoute":
            console.print("  - Check route references correct service name and port")
            console.print("  - Verify gateway accepts routes from this namespace")
        elif component == "Gateway":
            console.print("  - Check Envoy Gateway controller: kubectl logs -n envoy-gateway-system -l app=envoy-gateway")
        elif component == "Envoy":
            console.print("  - Scale up Envoy: kubectl scale deploy -n envoy-gateway-system envoy-gateway --replicas=2")
        elif component == "Network":
            console.print("  - Check Flannel/WireGuard: clustermgr flannel diagnose")
            console.print("  - Check network policies: clustermgr netpol test {namespace}")
        elif component == "DNS":
            console.print("  - Verify DNS records in your DNS provider")
            console.print("  - Check if wildcard *.deployments.basilica.ai is configured")
