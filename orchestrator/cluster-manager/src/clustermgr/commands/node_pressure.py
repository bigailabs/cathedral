"""Node-pressure command for clustermgr - detect node pressure conditions."""

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl, Severity, print_status

console = Console()

# Pressure condition types and their implications
PRESSURE_CONDITIONS = {
    "MemoryPressure": {
        "description": "Node is under memory pressure",
        "impact": "Pods may be evicted",
        "fix": "Free memory or add capacity",
    },
    "DiskPressure": {
        "description": "Node is under disk pressure",
        "impact": "Image pulls may fail, pods evicted",
        "fix": "Clean up disk space or expand storage",
    },
    "PIDPressure": {
        "description": "Too many processes on node",
        "impact": "New containers may fail to start",
        "fix": "Reduce process count or increase limit",
    },
    "NetworkUnavailable": {
        "description": "Network is not configured correctly",
        "impact": "Pods cannot communicate",
        "fix": "Check CNI plugin and network configuration",
    },
}


def _parse_quantity(quantity: str) -> int:
    """Parse Kubernetes quantity to bytes or millicores."""
    if not quantity:
        return 0

    units = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
        "m": 0.001,
    }

    for suffix, multiplier in units.items():
        if quantity.endswith(suffix):
            return int(float(quantity[:-len(suffix)]) * multiplier)

    return int(float(quantity))


def _get_node_conditions(config: Config) -> list[dict]:
    """Get node conditions and resource status."""
    result = run_kubectl(config, ["get", "nodes", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []

    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        status = item.get("status", {})
        conditions = status.get("conditions", [])

        # Parse conditions
        node_conditions: dict[str, dict] = {}
        for cond in conditions:
            cond_type = cond.get("type", "")
            node_conditions[cond_type] = {
                "status": cond.get("status", "Unknown"),
                "reason": cond.get("reason", ""),
                "message": cond.get("message", ""),
                "lastTransition": cond.get("lastTransitionTime", ""),
            }

        # Get allocatable vs capacity
        allocatable = status.get("allocatable", {})
        capacity = status.get("capacity", {})

        # Calculate resource pressure indicators
        cpu_alloc = _parse_quantity(allocatable.get("cpu", "0"))
        cpu_cap = _parse_quantity(capacity.get("cpu", "0"))
        mem_alloc = _parse_quantity(allocatable.get("memory", "0"))
        mem_cap = _parse_quantity(capacity.get("memory", "0"))
        pods_alloc = int(allocatable.get("pods", "0"))
        pods_cap = int(capacity.get("pods", "0"))

        # Check for storage
        ephemeral_storage = _parse_quantity(allocatable.get("ephemeral-storage", "0"))

        nodes.append({
            "name": name,
            "conditions": node_conditions,
            "cpu_allocatable": cpu_alloc,
            "cpu_capacity": cpu_cap,
            "memory_allocatable": mem_alloc,
            "memory_capacity": mem_cap,
            "pods_allocatable": pods_alloc,
            "pods_capacity": pods_cap,
            "ephemeral_storage": ephemeral_storage,
            "ready": node_conditions.get("Ready", {}).get("status") == "True",
        })

    return nodes


def _get_node_metrics(config: Config) -> dict[str, dict]:
    """Get node metrics from metrics-server."""
    result = run_kubectl(config, ["top", "nodes", "--no-headers"])
    if result.returncode != 0:
        return {}

    metrics: dict[str, dict] = {}
    for line in result.stdout.split("\n"):
        parts = line.split()
        if len(parts) >= 5:
            name = parts[0]
            cpu_usage = parts[1]  # e.g., "100m" or "1"
            cpu_pct = parts[2].rstrip("%")
            mem_usage = parts[3]  # e.g., "1000Mi"
            mem_pct = parts[4].rstrip("%")

            metrics[name] = {
                "cpu_usage": cpu_usage,
                "cpu_percent": int(cpu_pct) if cpu_pct.isdigit() else 0,
                "memory_usage": mem_usage,
                "memory_percent": int(mem_pct) if mem_pct.isdigit() else 0,
            }

    return metrics


def _format_bytes(b: int) -> str:
    """Format bytes to human readable."""
    if b >= 1024 ** 3:
        return f"{b / (1024 ** 3):.1f}Gi"
    if b >= 1024 ** 2:
        return f"{b / (1024 ** 2):.0f}Mi"
    return f"{b / 1024:.0f}Ki"


@click.command("node-pressure")
@click.option("--metrics", "-m", is_flag=True, help="Include current usage metrics")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed condition info")
@click.pass_context
def node_pressure(
    ctx: click.Context,
    metrics: bool,
    verbose: bool,
) -> None:
    """Detect and report node pressure conditions."""
    config: Config = ctx.obj

    print_header("Node Pressure Detection")

    nodes = _get_node_conditions(config)
    if not nodes:
        console.print("[red]Failed to get node information[/red]")
        ctx.exit(1)

    # Get metrics if requested
    node_metrics: dict[str, dict] = {}
    if metrics:
        node_metrics = _get_node_metrics(config)

    # Analyze each node
    all_issues: list[dict] = []

    for node in nodes:
        issues: list[dict] = []

        # Check pressure conditions
        for cond_type in ["MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"]:
            cond = node["conditions"].get(cond_type, {})
            if cond.get("status") == "True":
                issues.append({
                    "condition": cond_type,
                    "reason": cond.get("reason", ""),
                    "message": cond.get("message", ""),
                    "info": PRESSURE_CONDITIONS.get(cond_type, {}),
                })

        # Check Ready condition
        ready_cond = node["conditions"].get("Ready", {})
        if ready_cond.get("status") != "True":
            issues.append({
                "condition": "NotReady",
                "reason": ready_cond.get("reason", ""),
                "message": ready_cond.get("message", ""),
                "info": {
                    "description": "Node is not ready",
                    "impact": "No pods can be scheduled",
                    "fix": "Check kubelet status and node health",
                },
            })

        # Check metrics thresholds
        if node["name"] in node_metrics:
            m = node_metrics[node["name"]]
            if m["cpu_percent"] > 90:
                issues.append({
                    "condition": "HighCPU",
                    "reason": f"{m['cpu_percent']}% CPU usage",
                    "message": "CPU utilization is very high",
                    "info": {
                        "description": "CPU approaching capacity",
                        "impact": "Performance degradation",
                        "fix": "Reduce workload or add capacity",
                    },
                })
            if m["memory_percent"] > 90:
                issues.append({
                    "condition": "HighMemory",
                    "reason": f"{m['memory_percent']}% memory usage",
                    "message": "Memory utilization is very high",
                    "info": {
                        "description": "Memory approaching capacity",
                        "impact": "OOM kills likely",
                        "fix": "Reduce workload or add memory",
                    },
                })

        node["issues"] = issues
        all_issues.extend([{**i, "node": node["name"]} for i in issues])

    # Summary table
    table = Table(title="Node Status")
    table.add_column("Node", style="cyan")
    table.add_column("Ready")
    table.add_column("Memory", justify="right")
    table.add_column("Storage", justify="right")
    table.add_column("Pods", justify="right")
    table.add_column("Issues")

    if metrics:
        table.add_column("CPU %", justify="right")
        table.add_column("Mem %", justify="right")

    for node in nodes:
        ready_str = "[green]Yes[/green]" if node["ready"] else "[red]No[/red]"
        issue_count = len(node["issues"])
        issue_str = f"[red]{issue_count}[/red]" if issue_count > 0 else "[green]0[/green]"

        row = [
            node["name"],
            ready_str,
            _format_bytes(node["memory_allocatable"]),
            _format_bytes(node["ephemeral_storage"]),
            str(node["pods_allocatable"]),
            issue_str,
        ]

        if metrics and node["name"] in node_metrics:
            m = node_metrics[node["name"]]
            cpu_color = "green" if m["cpu_percent"] < 70 else "yellow" if m["cpu_percent"] < 90 else "red"
            mem_color = "green" if m["memory_percent"] < 70 else "yellow" if m["memory_percent"] < 90 else "red"
            row.append(f"[{cpu_color}]{m['cpu_percent']}%[/{cpu_color}]")
            row.append(f"[{mem_color}]{m['memory_percent']}%[/{mem_color}]")
        elif metrics:
            row.extend(["-", "-"])

        table.add_row(*row)

    console.print(table)

    # Show issues
    if all_issues:
        print_header("Detected Pressure Conditions")

        for issue in all_issues:
            severity = Severity.CRITICAL if issue["condition"] in ("NotReady", "MemoryPressure") else Severity.WARNING
            print_status(issue["node"], f"{issue['condition']}: {issue['reason']}", severity)

            if verbose:
                info = issue.get("info", {})
                if info:
                    console.print(f"    Description: {info.get('description', '')}")
                    console.print(f"    Impact: {info.get('impact', '')}")
                    console.print(f"    Fix: {info.get('fix', '')}")
                if issue.get("message"):
                    console.print(f"    Message: {issue['message'][:100]}")

    # Summary
    pressure_count = sum(1 for i in all_issues if i["condition"] in PRESSURE_CONDITIONS)
    not_ready = sum(1 for i in all_issues if i["condition"] == "NotReady")

    if all_issues:
        console.print(f"\n[bold]Summary:[/bold] {not_ready} nodes not ready, {pressure_count} pressure conditions")
        ctx.exit(1)
    else:
        console.print("\n[green]All nodes healthy, no pressure conditions detected[/green]")
