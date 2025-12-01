"""Deployments command for clustermgr - list user deployments."""

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl, Severity, print_status

console = Console()


def _get_user_deployments(config: Config, namespace: str | None = None) -> list[dict]:
    """Get UserDeployment custom resources."""
    cmd = ["get", "userdeployments"]
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")
    cmd.extend(["-o", "json"])

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    deployments = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        # Extract conditions
        conditions = status.get("conditions", [])
        ready_condition = next(
            (c for c in conditions if c.get("type") == "Ready"), {}
        )
        ready_status = ready_condition.get("status", "Unknown")

        deployments.append({
            "name": metadata.get("name", ""),
            "namespace": metadata.get("namespace", ""),
            "image": spec.get("image", ""),
            "replicas": spec.get("replicas", 1),
            "ready_replicas": status.get("readyReplicas", 0),
            "ready": ready_status == "True",
            "phase": status.get("phase", "Unknown"),
            "created": metadata.get("creationTimestamp", ""),
            "node": status.get("nodeName", ""),
            "message": ready_condition.get("message", ""),
        })

    return deployments


def _get_related_pods(config: Config, deployment_name: str, namespace: str) -> list[dict]:
    """Get pods related to a deployment."""
    result = run_kubectl(
        config,
        [
            "get", "pods", "-n", namespace,
            "-l", f"app.kubernetes.io/instance={deployment_name}",
            "-o", "json"
        ]
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

        pods.append({
            "name": metadata.get("name", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": f"{ready_count}/{total_containers}",
            "restarts": restart_count,
            "node": spec.get("nodeName", ""),
            "ip": status.get("podIP", ""),
        })

    return pods


@click.command()
@click.option("--namespace", "-n", help="Filter by namespace (default: all)")
@click.option("--user", "-u", help="Filter by user (namespace prefix u-)")
@click.option("--status", "-s", type=click.Choice(["running", "pending", "failed", "all"]),
              default="all", help="Filter by status")
@click.option("--details", "-d", is_flag=True, help="Show pod details for each deployment")
@click.pass_context
def deployments(
    ctx: click.Context,
    namespace: str | None,
    user: str | None,
    status: str,
    details: bool,
) -> None:
    """List user deployments with status and resource usage."""
    config: Config = ctx.obj

    # Handle user filter as namespace
    if user:
        namespace = f"u-{user}" if not user.startswith("u-") else user

    print_header("User Deployments")

    deps = _get_user_deployments(config, namespace)

    if not deps:
        console.print("[yellow]No user deployments found[/yellow]")
        if not namespace:
            console.print("Tip: UserDeployment CRD may not be installed")
        return

    # Filter by status
    if status != "all":
        status_map = {
            "running": lambda d: d["ready"] and d["phase"] == "Running",
            "pending": lambda d: d["phase"] in ("Pending", "Creating"),
            "failed": lambda d: not d["ready"] or d["phase"] == "Failed",
        }
        filter_fn = status_map.get(status, lambda d: True)
        deps = [d for d in deps if filter_fn(d)]

    if not deps:
        console.print(f"[yellow]No deployments matching status '{status}'[/yellow]")
        return

    # Summary counts
    running = sum(1 for d in deps if d["ready"])
    pending = sum(1 for d in deps if d["phase"] in ("Pending", "Creating"))
    failed = sum(1 for d in deps if not d["ready"] and d["phase"] not in ("Pending", "Creating"))

    console.print(f"Found {len(deps)} deployment(s): ", end="")
    console.print(f"[green]{running} running[/green], ", end="")
    console.print(f"[yellow]{pending} pending[/yellow], ", end="")
    console.print(f"[red]{failed} failed[/red]")

    # Main table
    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Name")
    table.add_column("Image", max_width=40)
    table.add_column("Replicas", justify="center")
    table.add_column("Status")
    table.add_column("Node")

    for dep in deps:
        status_color = "green" if dep["ready"] else "red"
        if dep["phase"] in ("Pending", "Creating"):
            status_color = "yellow"

        replicas_str = f"{dep['ready_replicas']}/{dep['replicas']}"

        # Truncate image name for display
        image = dep["image"]
        if len(image) > 40:
            image = "..." + image[-37:]

        table.add_row(
            dep["namespace"],
            dep["name"],
            image,
            replicas_str,
            f"[{status_color}]{dep['phase']}[/{status_color}]",
            dep["node"] or "-",
        )

    console.print(table)

    # Show pod details if requested
    if details:
        print_header("Pod Details")
        for dep in deps:
            console.print(f"\n[bold]{dep['namespace']}/{dep['name']}[/bold]")
            pods = _get_related_pods(config, dep["name"], dep["namespace"])

            if not pods:
                console.print("  [dim]No pods found[/dim]")
                continue

            for pod in pods:
                phase_color = {
                    "Running": "green",
                    "Pending": "yellow",
                    "Failed": "red",
                    "Succeeded": "cyan",
                }.get(pod["phase"], "white")

                restart_info = ""
                if pod["restarts"] > 0:
                    restart_info = f" [yellow](restarts: {pod['restarts']})[/yellow]"

                console.print(
                    f"  [{phase_color}]{pod['phase']}[/{phase_color}] "
                    f"{pod['name']} - {pod['ready']} ready, "
                    f"node: {pod['node']}, ip: {pod['ip']}{restart_info}"
                )

            if dep["message"]:
                console.print(f"  [dim]Message: {dep['message']}[/dim]")
