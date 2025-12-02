"""Envoy proxy diagnostics commands for clustermgr.

These commands help diagnose HTTP 503 errors by testing connectivity
from Envoy Gateway pods to user pods on GPU nodes.

Key areas diagnosed:
- Envoy pod health and placement
- HTTP connectivity from Envoy to user pods
- Envoy access logs for error patterns
- Pod-to-pod network path validation
"""

import re
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

GATEWAY_NAMESPACE = "envoy-gateway-system"


@dataclass
class EnvoyPod:
    """Envoy proxy pod information."""

    name: str
    node: str
    node_ip: str
    pod_ip: str
    phase: str
    ready: bool
    containers_ready: str
    restarts: int


@dataclass
class UserPodInfo:
    """User pod information for connectivity testing."""

    name: str
    namespace: str
    node: str
    pod_ip: str
    port: int
    ready: bool


@dataclass
class ConnectivityResult:
    """Result of a connectivity test."""

    source_pod: str
    target_pod: str
    target_ip: str
    target_port: int
    success: bool
    status_code: int
    latency_ms: float
    error: str


def _get_envoy_pods(config: Config) -> list[EnvoyPod]:
    """Get all Envoy proxy pods with detailed information."""
    result = run_kubectl(
        config,
        [
            "get", "pods", "-n", GATEWAY_NAMESPACE,
            "-l", "gateway.envoyproxy.io/owning-gateway-name",
            "-o", "json",
        ],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    pods = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        container_statuses = status.get("containerStatuses", [])
        ready_count = sum(1 for c in container_statuses if c.get("ready", False))
        total_containers = len(container_statuses)
        total_restarts = sum(c.get("restartCount", 0) for c in container_statuses)

        node_name = spec.get("nodeName", "")
        node_ip = status.get("hostIP", "")

        pods.append(EnvoyPod(
            name=metadata.get("name", ""),
            node=node_name,
            node_ip=node_ip,
            pod_ip=status.get("podIP", ""),
            phase=status.get("phase", "Unknown"),
            ready=ready_count == total_containers and total_containers > 0,
            containers_ready=f"{ready_count}/{total_containers}",
            restarts=total_restarts,
        ))

    return pods


def _get_user_pods_on_gpu_nodes(config: Config) -> list[UserPodInfo]:
    """Get user pods running on GPU nodes (WireGuard-connected)."""
    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "jsonpath={.items[*].metadata.name}"],
    )
    if result.returncode != 0:
        return []

    gpu_nodes = set(result.stdout.split())
    if not gpu_nodes:
        return []

    result = run_kubectl(
        config,
        ["get", "pods", "-A", "-o", "json"],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    pods = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        namespace = metadata.get("namespace", "")
        if not namespace.startswith("u-"):
            continue

        node_name = spec.get("nodeName", "")
        if node_name not in gpu_nodes:
            continue

        container_statuses = status.get("containerStatuses", [])
        ready = any(c.get("ready", False) for c in container_statuses)

        containers = spec.get("containers", [])
        port = 8000
        for container in containers:
            ports = container.get("ports", [])
            if ports:
                port = ports[0].get("containerPort", 8000)
                break

        pods.append(UserPodInfo(
            name=metadata.get("name", ""),
            namespace=namespace,
            node=node_name,
            pod_ip=status.get("podIP", ""),
            port=port,
            ready=ready,
        ))

    return pods


def _test_connectivity_from_server(
    config: Config,
    target_ip: str,
    target_port: int,
    timeout: int = 5,
) -> ConnectivityResult:
    """Test HTTP connectivity from a K3s server to a pod IP.

    Since Envoy containers are minimal without curl, we test from K3s servers
    which have access to the pod network via Flannel VXLAN.
    """
    from clustermgr.utils import run_ansible

    curl_cmd = (
        f"curl -s -o /dev/null -w '%{{http_code}} %{{time_total}}' "
        f"--connect-timeout {timeout} -m {timeout} "
        f"http://{target_ip}:{target_port}/ 2>&1 || echo '000 0'"
    )

    result = run_ansible(
        config,
        "shell",
        curl_cmd,
        hosts="k3s_server[0]",
        timeout=timeout + 10,
    )

    if result.returncode != 0:
        return ConnectivityResult(
            source_pod="k3s-server",
            target_pod="",
            target_ip=target_ip,
            target_port=target_port,
            success=False,
            status_code=0,
            latency_ms=0,
            error=result.stderr[:100] if result.stderr else "curl failed",
        )

    output = result.stdout
    for line in output.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            continue
        line = line.strip()
        if not line:
            continue

        parts = line.split()
        if len(parts) >= 2:
            try:
                status_code = int(parts[0])
                latency = float(parts[1]) * 1000
                success = status_code > 0 and status_code < 500
                error = "" if success else f"HTTP {status_code}"
                return ConnectivityResult(
                    source_pod="k3s-server",
                    target_pod="",
                    target_ip=target_ip,
                    target_port=target_port,
                    success=success,
                    status_code=status_code,
                    latency_ms=latency,
                    error=error,
                )
            except (ValueError, IndexError):
                continue

    return ConnectivityResult(
        source_pod="k3s-server",
        target_pod="",
        target_ip=target_ip,
        target_port=target_port,
        success=False,
        status_code=0,
        latency_ms=0,
        error="Failed to parse output",
    )


def _get_envoy_logs(
    config: Config,
    envoy_pod: str,
    status_filter: str | None = None,
    lines: int = 100,
) -> list[dict]:
    """Get Envoy access logs, optionally filtered by status code."""
    result = run_kubectl(
        config,
        ["logs", "-n", GATEWAY_NAMESPACE, envoy_pod, "--tail", str(lines)],
        timeout=30,
    )
    if result.returncode != 0:
        return []

    logs = []
    for line in result.stdout.split("\n"):
        if not line.strip():
            continue

        status_match = re.search(r'"(\d{3})"', line)
        if not status_match:
            continue

        status_code = status_match.group(1)

        if status_filter and not status_code.startswith(status_filter):
            continue

        path_match = re.search(r'"(GET|POST|PUT|DELETE|PATCH|HEAD)\s+([^"]+)"', line)
        path = path_match.group(2) if path_match else ""

        duration_match = re.search(r'"(\d+)"$', line)
        duration = duration_match.group(1) if duration_match else ""

        upstream_match = re.search(r'"([0-9.]+:\d+)"', line)
        upstream = upstream_match.group(1) if upstream_match else ""

        logs.append({
            "status": status_code,
            "path": path[:60],
            "duration_ms": duration,
            "upstream": upstream,
            "raw": line[:200],
        })

    return logs


@click.group()
def envoy() -> None:
    """Envoy proxy diagnostics commands.

    Commands for diagnosing HTTP 503 errors by testing Envoy
    Gateway connectivity to user pods on GPU nodes.

    Key diagnostics:
    - Envoy pod health and node placement
    - HTTP connectivity from Envoy to user pods
    - Access log analysis for error patterns
    """
    pass


@envoy.command("pods")
@click.pass_context
def pods(ctx: click.Context) -> None:
    """Show Envoy proxy pod status and locations.

    Displays all Envoy Gateway pods with their node placement,
    readiness status, and restart counts.
    """
    config: Config = ctx.obj

    print_header("Envoy Gateway Pods")

    envoy_pods = _get_envoy_pods(config)
    if not envoy_pods:
        console.print("[red]No Envoy pods found[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Pod", style="cyan")
    table.add_column("Node")
    table.add_column("Node IP")
    table.add_column("Pod IP")
    table.add_column("Phase")
    table.add_column("Ready")
    table.add_column("Restarts")

    issues_found = False

    for pod in envoy_pods:
        phase_color = "green" if pod.phase == "Running" else "red"
        ready_color = "green" if pod.ready else "red"
        restart_color = "red" if pod.restarts > 5 else "green"

        if not pod.ready or pod.restarts > 5:
            issues_found = True

        table.add_row(
            pod.name[:40],
            pod.node[:25],
            pod.node_ip,
            pod.pod_ip,
            f"[{phase_color}]{pod.phase}[/{phase_color}]",
            f"[{ready_color}]{pod.containers_ready}[/{ready_color}]",
            f"[{restart_color}]{pod.restarts}[/{restart_color}]",
        )

    console.print(table)

    print_header("Node Distribution")
    nodes = {}
    for pod in envoy_pods:
        nodes[pod.node] = nodes.get(pod.node, 0) + 1

    for node, count in sorted(nodes.items()):
        is_server = "server" in node.lower()
        node_type = "[cyan]server[/cyan]" if is_server else "[dim]agent[/dim]"
        console.print(f"  {node}: {count} pod(s) {node_type}")

    if issues_found:
        ctx.exit(1)


@envoy.command("test")
@click.option("--namespace", "-n", help="Filter by user namespace")
@click.option("--limit", "-l", default=5, help="Max pods to test")
@click.pass_context
def test(ctx: click.Context, namespace: str | None, limit: int) -> None:
    """Test HTTP connectivity to user pods on GPU nodes.

    Tests whether the K3s servers (which route Envoy traffic) can reach
    user pods on GPU nodes via Flannel VXLAN. This diagnoses the network
    path that causes HTTP 503 errors.
    """
    config: Config = ctx.obj

    print_header("Flannel Network Connectivity Test")

    envoy_pods = _get_envoy_pods(config)
    ready_envoys = [p for p in envoy_pods if p.ready]

    if not ready_envoys:
        console.print("[yellow]No Envoy pods found (will still test Flannel path)[/yellow]")

    user_pods = _get_user_pods_on_gpu_nodes(config)
    if namespace:
        user_pods = [p for p in user_pods if p.namespace == namespace]

    if not user_pods:
        console.print("[yellow]No user pods found on GPU nodes[/yellow]")
        if namespace:
            console.print(f"  [dim]Filtered by namespace: {namespace}[/dim]")
        return

    ready_user_pods = [p for p in user_pods if p.ready]
    console.print(f"Found {len(ready_envoys)} Envoy pod(s) and {len(ready_user_pods)} ready user pod(s)")
    console.print("Testing from K3s server via Flannel VXLAN overlay\n")

    if not ready_user_pods:
        console.print("[yellow]No ready user pods to test[/yellow]")
        return

    test_pods = ready_user_pods[:limit]

    results: list[ConnectivityResult] = []

    for user_pod in test_pods:
        console.print(f"Testing {user_pod.namespace}/{user_pod.name}...", end=" ")

        result = _test_connectivity_from_server(
            config,
            user_pod.pod_ip,
            user_pod.port,
        )
        result = ConnectivityResult(
            source_pod=result.source_pod,
            target_pod=f"{user_pod.namespace}/{user_pod.name}",
            target_ip=result.target_ip,
            target_port=result.target_port,
            success=result.success,
            status_code=result.status_code,
            latency_ms=result.latency_ms,
            error=result.error,
        )
        results.append(result)

        if result.success:
            console.print(f"[green]HTTP {result.status_code}[/green] ({result.latency_ms:.0f}ms)")
        else:
            console.print(f"[red]FAILED[/red] - {result.error}")

    print_header("Results Summary")

    table = Table()
    table.add_column("Target Pod", style="cyan")
    table.add_column("IP:Port")
    table.add_column("Node")
    table.add_column("Status")
    table.add_column("Latency")

    successful = 0
    for result in results:
        user_pod = next((p for p in test_pods if f"{p.namespace}/{p.name}" == result.target_pod), None)
        node = user_pod.node[:20] if user_pod else "-"

        if result.success:
            successful += 1
            status_str = f"[green]HTTP {result.status_code}[/green]"
            latency_str = f"{result.latency_ms:.0f}ms"
        else:
            status_str = f"[red]{result.error}[/red]"
            latency_str = "-"

        table.add_row(
            result.target_pod[:35],
            f"{result.target_ip}:{result.target_port}",
            node,
            status_str,
            latency_str,
        )

    console.print(table)

    console.print(f"\n{successful}/{len(results)} tests passed")

    if successful < len(results):
        print_header("Troubleshooting")
        console.print("Failed connectivity may indicate:")
        console.print("  - Flannel VXLAN routing issues (run: clustermgr flannel diagnose)")
        console.print("  - Missing FDB/neighbor entries (run: clustermgr flannel fdb)")
        console.print("  - NetworkPolicy blocking traffic (run: clustermgr netpol test <ns>)")
        ctx.exit(1)


@envoy.command("logs")
@click.option("--status", "-s", help="Filter by status code prefix (e.g., '5' for 5xx)")
@click.option("--lines", "-l", default=100, help="Number of log lines to fetch")
@click.option("--pod", "-p", help="Specific Envoy pod name")
@click.pass_context
def logs(ctx: click.Context, status: str | None, lines: int, pod: str | None) -> None:
    """Show Envoy access logs filtered by status code.

    Displays access logs from Envoy pods, optionally filtered
    to show only error responses (5xx, 4xx).

    Examples:
        clustermgr envoy logs --status 5    # Show 5xx errors only
        clustermgr envoy logs --status 503  # Show only 503 errors
    """
    config: Config = ctx.obj

    print_header("Envoy Access Logs")

    envoy_pods = _get_envoy_pods(config)
    if not envoy_pods:
        console.print("[red]No Envoy pods found[/red]")
        ctx.exit(1)

    if pod:
        target_pods = [p for p in envoy_pods if pod in p.name]
        if not target_pods:
            console.print(f"[red]Pod '{pod}' not found[/red]")
            ctx.exit(1)
    else:
        target_pods = envoy_pods[:1]

    for envoy_pod in target_pods:
        print_header(f"Logs from {envoy_pod.name}")

        log_entries = _get_envoy_logs(config, envoy_pod.name, status, lines)

        if not log_entries:
            console.print(f"  [dim]No matching log entries (filter: status={status or 'all'})[/dim]")
            continue

        table = Table()
        table.add_column("Status")
        table.add_column("Path", max_width=50)
        table.add_column("Duration")
        table.add_column("Upstream")

        status_counts: dict[str, int] = {}

        for entry in log_entries[:50]:
            status_code = entry["status"]
            status_counts[status_code] = status_counts.get(status_code, 0) + 1

            if status_code.startswith("5"):
                status_color = "red"
            elif status_code.startswith("4"):
                status_color = "yellow"
            else:
                status_color = "green"

            table.add_row(
                f"[{status_color}]{status_code}[/{status_color}]",
                entry["path"] or "-",
                f"{entry['duration_ms']}ms" if entry["duration_ms"] else "-",
                entry["upstream"] or "-",
            )

        console.print(table)

        if len(log_entries) > 50:
            console.print(f"  [dim]... and {len(log_entries) - 50} more entries[/dim]")

        print_header("Status Code Distribution")
        for code, count in sorted(status_counts.items()):
            if code.startswith("5"):
                color = "red"
            elif code.startswith("4"):
                color = "yellow"
            else:
                color = "green"
            console.print(f"  [{color}]{code}[/{color}]: {count}")


@envoy.command("path")
@click.argument("user_pod")
@click.option("--namespace", "-n", required=True, help="Namespace of the user pod")
@click.pass_context
def path(ctx: click.Context, user_pod: str, namespace: str) -> None:
    """Trace the network path from Envoy to a user pod.

    Shows the complete path that traffic takes from an Envoy pod
    to a specific user pod, including Flannel VXLAN routing.
    """
    config: Config = ctx.obj

    print_header(f"Network Path to {namespace}/{user_pod}")

    result = run_kubectl(
        config,
        ["get", "pod", user_pod, "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        console.print(f"[red]Pod '{user_pod}' not found in namespace '{namespace}'[/red]")
        ctx.exit(1)

    data = parse_json_output(result.stdout)
    pod_ip = data.get("status", {}).get("podIP", "")
    pod_node = data.get("spec", {}).get("nodeName", "")

    if not pod_ip:
        console.print("[red]Pod has no IP address[/red]")
        ctx.exit(1)

    console.print(f"Target: {namespace}/{user_pod}")
    console.print(f"Pod IP: {pod_ip}")
    console.print(f"Node: {pod_node}")

    print_header("Step 1: Envoy Pod")
    envoy_pods = _get_envoy_pods(config)
    if envoy_pods:
        envoy = envoy_pods[0]
        console.print(f"  Source: {envoy.name}")
        console.print(f"  Node: {envoy.node} ({envoy.node_ip})")
        console.print(f"  Pod IP: {envoy.pod_ip}")
    else:
        console.print("  [red]No Envoy pods found[/red]")
        ctx.exit(1)

    print_header("Step 2: K8s Service")
    parts = user_pod.split("-")
    if len(parts) >= 2:
        service_name = f"s-{'-'.join(parts[:5])}"
        svc_result = run_kubectl(
            config,
            ["get", "service", service_name, "-n", namespace, "-o", "json"],
        )
        if svc_result.returncode == 0:
            svc_data = parse_json_output(svc_result.stdout)
            cluster_ip = svc_data.get("spec", {}).get("clusterIP", "")
            ports = svc_data.get("spec", {}).get("ports", [])
            port = ports[0].get("port", 0) if ports else 0
            console.print(f"  Service: {service_name}")
            console.print(f"  ClusterIP: {cluster_ip}:{port}")
        else:
            console.print(f"  [dim]Service {service_name} not found[/dim]")

    print_header("Step 3: Flannel Route")
    pod_cidr = ".".join(pod_ip.split(".")[:3]) + ".0/24"
    from clustermgr.commands.flannel import _get_flannel_routes
    routes = _get_flannel_routes(config)

    route_found = False
    for node, node_routes in routes.items():
        for route in node_routes:
            if route.pod_cidr == pod_cidr:
                console.print(f"  Route: {route.pod_cidr} via {route.via} dev {route.device}")
                console.print(f"  Found on: {node}")
                route_found = True
                break

    if not route_found:
        print_status("Flannel Route", "MISSING", Severity.CRITICAL)

    print_header("Step 4: Node Info")
    result = run_kubectl(
        config,
        ["get", "node", pod_node, "-o", "json"],
    )
    if result.returncode == 0:
        node_data = parse_json_output(result.stdout)
        annotations = node_data.get("metadata", {}).get("annotations", {})
        flannel_ip = annotations.get("flannel.alpha.coreos.com/public-ip", "")
        backend_data = annotations.get("flannel.alpha.coreos.com/backend-data", "")

        console.print(f"  Flannel Public IP: {flannel_ip}")
        console.print(f"  Backend Data: {backend_data[:60]}")

        is_wg = "10.200" in flannel_ip
        if is_wg:
            console.print("  [cyan]WireGuard-connected GPU node[/cyan]")

    print_header("Step 5: Connectivity Test")
    result = _test_connectivity_from_envoy(config, envoy.name, pod_ip, 8000)

    if result.success:
        print_status("HTTP Connectivity", f"HTTP {result.status_code} ({result.latency_ms:.0f}ms)", Severity.HEALTHY)
    else:
        print_status("HTTP Connectivity", result.error, Severity.CRITICAL)
        console.print("\n[yellow]Troubleshooting:[/yellow]")
        console.print("  - Check Flannel FDB: clustermgr flannel fdb")
        console.print("  - Check neighbors: clustermgr flannel neighbors")
        console.print("  - Check routes: clustermgr flannel routes")
