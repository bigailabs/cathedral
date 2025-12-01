"""Pod-troubleshoot command for clustermgr - deep pod diagnostics."""

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl, Severity, print_status

console = Console()


def _get_pod_details(config: Config, namespace: str, pod_name: str) -> dict | None:
    """Get detailed pod information."""
    result = run_kubectl(config, ["get", "pod", "-n", namespace, pod_name, "-o", "json"])
    if result.returncode != 0:
        return None
    return parse_json_output(result.stdout)


def _get_pod_events(config: Config, namespace: str, pod_name: str) -> list[dict]:
    """Get events related to a pod."""
    result = run_kubectl(
        config,
        ["get", "events", "-n", namespace,
         "--field-selector", f"involvedObject.name={pod_name}",
         "-o", "json", "--sort-by=.lastTimestamp"]
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    return data.get("items", [])


def _get_pod_logs(config: Config, namespace: str, pod_name: str, container: str | None, tail: int) -> str:
    """Get pod logs."""
    cmd = ["logs", "-n", namespace, pod_name, f"--tail={tail}"]
    if container:
        cmd.extend(["-c", container])
    cmd.append("--timestamps=true")

    result = run_kubectl(config, cmd, timeout=30)
    return result.stdout if result.returncode == 0 else result.stderr


def _analyze_pod_issues(pod: dict) -> list[dict]:
    """Analyze pod for common issues."""
    issues: list[dict] = []
    status = pod.get("status", {})
    spec = pod.get("spec", {})

    # Check phase
    phase = status.get("phase", "")
    if phase == "Failed":
        issues.append({
            "severity": Severity.CRITICAL,
            "issue": "Pod failed",
            "reason": status.get("reason", "Unknown"),
            "message": status.get("message", ""),
        })
    elif phase == "Pending":
        conditions = status.get("conditions", [])
        for cond in conditions:
            if cond.get("status") == "False":
                issues.append({
                    "severity": Severity.WARNING,
                    "issue": f"Condition {cond.get('type')} is False",
                    "reason": cond.get("reason", ""),
                    "message": cond.get("message", ""),
                })

    # Check container statuses
    for cs in status.get("containerStatuses", []):
        container_name = cs.get("name", "")

        # Check restart count
        restarts = cs.get("restartCount", 0)
        if restarts > 5:
            issues.append({
                "severity": Severity.WARNING,
                "issue": f"Container {container_name} has high restarts",
                "reason": f"{restarts} restarts",
                "message": "May indicate OOM kills or crash loops",
            })

        # Check waiting state
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting:
            reason = waiting.get("reason", "")
            if reason in ("CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull"):
                issues.append({
                    "severity": Severity.CRITICAL,
                    "issue": f"Container {container_name} is {reason}",
                    "reason": reason,
                    "message": waiting.get("message", ""),
                })
            elif reason:
                issues.append({
                    "severity": Severity.WARNING,
                    "issue": f"Container {container_name} waiting",
                    "reason": reason,
                    "message": waiting.get("message", ""),
                })

        # Check terminated state
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated and terminated.get("exitCode", 0) != 0:
            issues.append({
                "severity": Severity.CRITICAL,
                "issue": f"Container {container_name} terminated with error",
                "reason": terminated.get("reason", ""),
                "message": f"Exit code: {terminated.get('exitCode')}",
            })

    # Check resource issues
    for container in spec.get("containers", []):
        resources = container.get("resources", {})
        if not resources.get("requests") and not resources.get("limits"):
            issues.append({
                "severity": Severity.WARNING,
                "issue": f"Container {container.get('name')} has no resource limits",
                "reason": "No resource constraints",
                "message": "Pod may be evicted under pressure",
            })

    return issues


def _find_problematic_pods(config: Config, namespace: str | None) -> list[dict]:
    """Find pods that are not running normally."""
    cmd = ["get", "pods", "-o", "json"]
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    problematic = []

    for item in data.get("items", []):
        status = item.get("status", {})
        phase = status.get("phase", "")

        is_problematic = False

        # Check for non-running phases
        if phase in ("Failed", "Pending", "Unknown"):
            is_problematic = True

        # Check container statuses
        for cs in status.get("containerStatuses", []):
            if cs.get("restartCount", 0) > 3:
                is_problematic = True
            waiting = cs.get("state", {}).get("waiting", {})
            if waiting.get("reason") in ("CrashLoopBackOff", "Error", "ImagePullBackOff"):
                is_problematic = True

        if is_problematic:
            problematic.append({
                "namespace": item.get("metadata", {}).get("namespace", ""),
                "name": item.get("metadata", {}).get("name", ""),
                "phase": phase,
                "restarts": sum(cs.get("restartCount", 0) for cs in status.get("containerStatuses", [])),
            })

    return problematic


@click.command("pod-troubleshoot")
@click.argument("pod_name", required=False)
@click.option("--namespace", "-n", default="default", help="Pod namespace")
@click.option("--logs", "-l", is_flag=True, help="Show pod logs")
@click.option("--events", "-e", is_flag=True, help="Show pod events")
@click.option("--tail", default=50, help="Number of log lines (default: 50)")
@click.option("--container", "-c", help="Container name for logs")
@click.option("--scan", "-s", is_flag=True, help="Scan for problematic pods")
@click.pass_context
def pod_troubleshoot(
    ctx: click.Context,
    pod_name: str | None,
    namespace: str,
    logs: bool,
    events: bool,
    tail: int,
    container: str | None,
    scan: bool,
) -> None:
    """Deep diagnostics for pod issues."""
    config: Config = ctx.obj

    # Scan mode - find problematic pods
    if scan or not pod_name:
        print_header("Scanning for Problematic Pods")

        problematic = _find_problematic_pods(config, namespace if namespace != "default" else None)

        if not problematic:
            console.print("[green]No problematic pods found[/green]")
            return

        table = Table(title=f"Found {len(problematic)} problematic pod(s)")
        table.add_column("Namespace", style="cyan")
        table.add_column("Pod")
        table.add_column("Phase")
        table.add_column("Restarts", justify="right")

        for pod in problematic:
            phase_color = "red" if pod["phase"] in ("Failed", "Unknown") else "yellow"
            table.add_row(
                pod["namespace"],
                pod["name"],
                f"[{phase_color}]{pod['phase']}[/{phase_color}]",
                str(pod["restarts"]) if pod["restarts"] > 0 else "-",
            )

        console.print(table)
        console.print("\nRun 'clustermgr pod-troubleshoot <pod> -n <namespace>' for details")
        return

    # Single pod analysis
    print_header(f"Pod Troubleshooting: {namespace}/{pod_name}")

    pod = _get_pod_details(config, namespace, pod_name)
    if not pod:
        console.print(f"[red]Pod {namespace}/{pod_name} not found[/red]")
        ctx.exit(1)

    # Basic info
    metadata = pod.get("metadata", {})
    status = pod.get("status", {})
    spec = pod.get("spec", {})

    info_table = Table(title="Pod Information", show_header=False)
    info_table.add_column("Key", style="bold")
    info_table.add_column("Value")

    info_table.add_row("Name", metadata.get("name", ""))
    info_table.add_row("Namespace", metadata.get("namespace", ""))
    info_table.add_row("Node", spec.get("nodeName", "-"))
    info_table.add_row("Phase", status.get("phase", "Unknown"))
    info_table.add_row("IP", status.get("podIP", "-"))
    info_table.add_row("Created", metadata.get("creationTimestamp", ""))

    console.print(info_table)

    # Container statuses
    print_header("Container Status")
    for cs in status.get("containerStatuses", []):
        name = cs.get("name", "")
        ready = cs.get("ready", False)
        restarts = cs.get("restartCount", 0)

        state = cs.get("state", {})
        if "running" in state:
            state_str = f"[green]Running since {state['running'].get('startedAt', '')}[/green]"
        elif "waiting" in state:
            state_str = f"[yellow]Waiting: {state['waiting'].get('reason', '')}[/yellow]"
        elif "terminated" in state:
            exit_code = state["terminated"].get("exitCode", 0)
            color = "green" if exit_code == 0 else "red"
            state_str = f"[{color}]Terminated (exit {exit_code})[/{color}]"
        else:
            state_str = "Unknown"

        console.print(f"  [bold]{name}[/bold]: {state_str}")
        console.print(f"    Ready: {'Yes' if ready else 'No'}, Restarts: {restarts}")

    # Analyze issues
    issues = _analyze_pod_issues(pod)
    if issues:
        print_header("Detected Issues")
        for issue in issues:
            severity_color = "red" if issue["severity"] == Severity.CRITICAL else "yellow"
            console.print(f"  [{severity_color}][{issue['severity'].name}][/{severity_color}] {issue['issue']}")
            if issue["reason"]:
                console.print(f"    Reason: {issue['reason']}")
            if issue["message"]:
                console.print(f"    Message: {issue['message'][:100]}")

    # Show events if requested
    if events:
        print_header("Pod Events")
        pod_events = _get_pod_events(config, namespace, pod_name)

        if not pod_events:
            console.print("  [dim]No events found[/dim]")
        else:
            for ev in pod_events[-10:]:  # Last 10 events
                ev_type = ev.get("type", "Normal")
                color = "yellow" if ev_type == "Warning" else "dim"
                reason = ev.get("reason", "")
                message = ev.get("message", "")[:80]
                console.print(f"  [{color}]{reason}[/{color}]: {message}")

    # Show logs if requested
    if logs:
        print_header("Pod Logs")
        containers = [c.get("name") for c in spec.get("containers", [])]

        if container:
            containers = [container]

        for c in containers:
            console.print(f"\n[bold cyan]Container: {c}[/bold cyan]")
            log_output = _get_pod_logs(config, namespace, pod_name, c, tail)
            if log_output:
                # Highlight errors in logs
                for line in log_output.split("\n")[-tail:]:
                    if "error" in line.lower() or "fail" in line.lower():
                        console.print(f"  [red]{line}[/red]")
                    elif "warn" in line.lower():
                        console.print(f"  [yellow]{line}[/yellow]")
                    else:
                        console.print(f"  [dim]{line}[/dim]")
            else:
                console.print("  [dim]No logs available[/dim]")

    # Exit code based on issues
    if any(i["severity"] == Severity.CRITICAL for i in issues):
        ctx.exit(1)
