"""Resources command for clustermgr - cluster resource utilization."""

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl

console = Console()


def _parse_cpu(cpu_str: str) -> int:
    """Parse CPU string to millicores."""
    if not cpu_str:
        return 0
    if cpu_str.endswith("m"):
        return int(cpu_str[:-1])
    if cpu_str.endswith("n"):
        return int(cpu_str[:-1]) // 1000000
    return int(float(cpu_str) * 1000)


def _parse_memory(mem_str: str) -> int:
    """Parse memory string to bytes."""
    if not mem_str:
        return 0
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    for suffix, multiplier in units.items():
        if mem_str.endswith(suffix):
            return int(mem_str[: -len(suffix)]) * multiplier
    if mem_str.endswith("K"):
        return int(mem_str[:-1]) * 1000
    if mem_str.endswith("M"):
        return int(mem_str[:-1]) * 1000000
    if mem_str.endswith("G"):
        return int(mem_str[:-1]) * 1000000000
    return int(mem_str)


def _format_cpu(millicores: int) -> str:
    """Format millicores to human-readable string."""
    if millicores >= 1000:
        return f"{millicores / 1000:.1f} cores"
    return f"{millicores}m"


def _format_memory(mem_bytes: int) -> str:
    """Format bytes to human-readable string."""
    if mem_bytes >= 1024**3:
        return f"{mem_bytes / (1024**3):.1f} Gi"
    if mem_bytes >= 1024**2:
        return f"{mem_bytes / (1024**2):.0f} Mi"
    return f"{mem_bytes / 1024:.0f} Ki"


def _get_node_resources(config: Config) -> list[dict]:
    """Get resource allocation for all nodes."""
    result = run_kubectl(config, ["get", "nodes", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []

    for item in data.get("items", []):
        name = item["metadata"]["name"]
        allocatable = item.get("status", {}).get("allocatable", {})
        capacity = item.get("status", {}).get("capacity", {})

        # Check GPU
        gpu_count = int(capacity.get("nvidia.com/gpu", 0))

        nodes.append({
            "name": name,
            "cpu_allocatable": _parse_cpu(allocatable.get("cpu", "0")),
            "memory_allocatable": _parse_memory(allocatable.get("memory", "0")),
            "cpu_capacity": _parse_cpu(capacity.get("cpu", "0")),
            "memory_capacity": _parse_memory(capacity.get("memory", "0")),
            "gpu_count": gpu_count,
            "cpu_requested": 0,
            "memory_requested": 0,
            "gpu_requested": 0,
        })

    return nodes


def _get_pod_requests(config: Config) -> dict[str, dict]:
    """Get resource requests by node."""
    result = run_kubectl(config, ["get", "pods", "-A", "-o", "json"])
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    node_usage: dict[str, dict] = {}

    for item in data.get("items", []):
        node_name = item.get("spec", {}).get("nodeName", "")
        if not node_name:
            continue

        phase = item.get("status", {}).get("phase", "")
        if phase not in ("Running", "Pending"):
            continue

        if node_name not in node_usage:
            node_usage[node_name] = {"cpu": 0, "memory": 0, "gpu": 0}

        for container in item.get("spec", {}).get("containers", []):
            requests = container.get("resources", {}).get("requests", {})
            node_usage[node_name]["cpu"] += _parse_cpu(requests.get("cpu", "0"))
            node_usage[node_name]["memory"] += _parse_memory(requests.get("memory", "0"))
            node_usage[node_name]["gpu"] += int(requests.get("nvidia.com/gpu", 0))

    return node_usage


def _get_namespace_usage(config: Config) -> dict[str, dict]:
    """Get resource usage by namespace."""
    result = run_kubectl(config, ["get", "pods", "-A", "-o", "json"])
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    ns_usage: dict[str, dict] = {}

    for item in data.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        phase = item.get("status", {}).get("phase", "")
        if phase not in ("Running", "Pending"):
            continue

        if ns not in ns_usage:
            ns_usage[ns] = {"cpu": 0, "memory": 0, "gpu": 0, "pods": 0}

        ns_usage[ns]["pods"] += 1

        for container in item.get("spec", {}).get("containers", []):
            requests = container.get("resources", {}).get("requests", {})
            ns_usage[ns]["cpu"] += _parse_cpu(requests.get("cpu", "0"))
            ns_usage[ns]["memory"] += _parse_memory(requests.get("memory", "0"))
            ns_usage[ns]["gpu"] += int(requests.get("nvidia.com/gpu", 0))

    return ns_usage


@click.command()
@click.option("--by-namespace", "-n", is_flag=True, help="Show usage by namespace")
@click.pass_context
def resources(ctx: click.Context, by_namespace: bool) -> None:
    """Show cluster resource utilization (CPU, memory, GPU)."""
    config: Config = ctx.obj

    print_header("Cluster Resource Utilization")

    nodes = _get_node_resources(config)
    if not nodes:
        console.print("[red]Failed to get node information[/red]")
        ctx.exit(1)

    pod_requests = _get_pod_requests(config)

    # Merge pod requests into node data
    for node in nodes:
        usage = pod_requests.get(node["name"], {})
        node["cpu_requested"] = usage.get("cpu", 0)
        node["memory_requested"] = usage.get("memory", 0)
        node["gpu_requested"] = usage.get("gpu", 0)

    # Node table
    table = Table(title="Node Resources")
    table.add_column("Node", style="cyan")
    table.add_column("CPU Req/Alloc", justify="right")
    table.add_column("CPU %", justify="right")
    table.add_column("Mem Req/Alloc", justify="right")
    table.add_column("Mem %", justify="right")
    table.add_column("GPU", justify="right")

    total_cpu_req = 0
    total_cpu_alloc = 0
    total_mem_req = 0
    total_mem_alloc = 0
    total_gpu = 0
    total_gpu_req = 0

    for node in nodes:
        cpu_pct = (
            (node["cpu_requested"] / node["cpu_allocatable"] * 100)
            if node["cpu_allocatable"] > 0
            else 0
        )
        mem_pct = (
            (node["memory_requested"] / node["memory_allocatable"] * 100)
            if node["memory_allocatable"] > 0
            else 0
        )

        cpu_color = "green" if cpu_pct < 70 else "yellow" if cpu_pct < 90 else "red"
        mem_color = "green" if mem_pct < 70 else "yellow" if mem_pct < 90 else "red"

        gpu_str = "-"
        if node["gpu_count"] > 0:
            gpu_str = f"{node['gpu_requested']}/{node['gpu_count']}"

        table.add_row(
            node["name"],
            f"{_format_cpu(node['cpu_requested'])}/{_format_cpu(node['cpu_allocatable'])}",
            f"[{cpu_color}]{cpu_pct:.0f}%[/{cpu_color}]",
            f"{_format_memory(node['memory_requested'])}/{_format_memory(node['memory_allocatable'])}",
            f"[{mem_color}]{mem_pct:.0f}%[/{mem_color}]",
            gpu_str,
        )

        total_cpu_req += node["cpu_requested"]
        total_cpu_alloc += node["cpu_allocatable"]
        total_mem_req += node["memory_requested"]
        total_mem_alloc += node["memory_allocatable"]
        total_gpu += node["gpu_count"]
        total_gpu_req += node["gpu_requested"]

    # Total row
    total_cpu_pct = (total_cpu_req / total_cpu_alloc * 100) if total_cpu_alloc > 0 else 0
    total_mem_pct = (total_mem_req / total_mem_alloc * 100) if total_mem_alloc > 0 else 0

    table.add_row(
        "[bold]TOTAL[/bold]",
        f"[bold]{_format_cpu(total_cpu_req)}/{_format_cpu(total_cpu_alloc)}[/bold]",
        f"[bold]{total_cpu_pct:.0f}%[/bold]",
        f"[bold]{_format_memory(total_mem_req)}/{_format_memory(total_mem_alloc)}[/bold]",
        f"[bold]{total_mem_pct:.0f}%[/bold]",
        f"[bold]{total_gpu_req}/{total_gpu}[/bold]" if total_gpu > 0 else "-",
    )

    console.print(table)

    # Namespace breakdown
    if by_namespace:
        print_header("Usage by Namespace")
        ns_usage = _get_namespace_usage(config)

        ns_table = Table(title="Namespace Resources")
        ns_table.add_column("Namespace", style="cyan")
        ns_table.add_column("Pods", justify="right")
        ns_table.add_column("CPU Requests", justify="right")
        ns_table.add_column("Memory Requests", justify="right")
        ns_table.add_column("GPUs", justify="right")

        for ns in sorted(ns_usage.keys()):
            usage = ns_usage[ns]
            ns_table.add_row(
                ns,
                str(usage["pods"]),
                _format_cpu(usage["cpu"]),
                _format_memory(usage["memory"]),
                str(usage["gpu"]) if usage["gpu"] > 0 else "-",
            )

        console.print(ns_table)
