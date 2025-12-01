"""Gateway API troubleshooting commands for clustermgr."""

import re
import socket
import subprocess
from dataclasses import dataclass

import click
from rich.console import Console
from rich.panel import Panel
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
BASILICA_SYSTEM_NS = "basilica-system"
GATEWAY_NAME = "basilica-gateway"


@dataclass
class HTTPRouteStatus:
    """Status information for an HTTPRoute."""

    name: str
    namespace: str
    hostnames: list[str]
    parent_gateway: str
    backend_service: str
    backend_port: int
    accepted: bool
    programmed: bool
    reason: str
    message: str


@dataclass
class GatewayStatus:
    """Status information for a Gateway."""

    name: str
    namespace: str
    address: str
    address_type: str
    listeners: list[dict]
    programmed: bool
    accepted: bool


def _get_gateway(config: Config) -> GatewayStatus | None:
    """Get the main Basilica gateway status."""
    result = run_kubectl(
        config,
        ["get", "gateway", GATEWAY_NAME, "-n", BASILICA_SYSTEM_NS, "-o", "json"],
    )
    if result.returncode != 0:
        result = run_kubectl(
            config,
            ["get", "gateway", GATEWAY_NAME, "-n", GATEWAY_NAMESPACE, "-o", "json"],
        )
        if result.returncode != 0:
            return None

    data = parse_json_output(result.stdout)
    if not data:
        return None

    metadata = data.get("metadata", {})
    spec = data.get("spec", {})
    status = data.get("status", {})

    addresses = status.get("addresses", [])
    address = addresses[0].get("value", "") if addresses else ""
    address_type = addresses[0].get("type", "") if addresses else ""

    listeners = []
    for listener in spec.get("listeners", []):
        listener_status = None
        for ls in status.get("listeners", []):
            if ls.get("name") == listener.get("name"):
                listener_status = ls
                break

        attached = 0
        if listener_status:
            attached = listener_status.get("attachedRoutes", 0)

        listeners.append({
            "name": listener.get("name", ""),
            "port": listener.get("port", 0),
            "protocol": listener.get("protocol", ""),
            "attached_routes": attached,
        })

    conditions = status.get("conditions", [])
    programmed = any(
        c.get("type") == "Programmed" and c.get("status") == "True"
        for c in conditions
    )
    accepted = any(
        c.get("type") == "Accepted" and c.get("status") == "True"
        for c in conditions
    )

    return GatewayStatus(
        name=metadata.get("name", ""),
        namespace=metadata.get("namespace", ""),
        address=address,
        address_type=address_type,
        listeners=listeners,
        programmed=programmed,
        accepted=accepted,
    )


def _get_all_httproutes(config: Config, namespace: str | None = None) -> list[HTTPRouteStatus]:
    """Get all HTTPRoutes."""
    if namespace:
        cmd = ["get", "httproute", "-n", namespace, "-o", "json"]
    else:
        cmd = ["get", "httproute", "-A", "-o", "json"]

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    routes = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        hostnames = spec.get("hostnames", [])
        parent_refs = spec.get("parentRefs", [])
        parent_gateway = ""
        if parent_refs:
            parent_gateway = f"{parent_refs[0].get('namespace', '')}/{parent_refs[0].get('name', '')}"

        backend_service = ""
        backend_port = 0
        rules = spec.get("rules", [])
        if rules:
            backend_refs = rules[0].get("backendRefs", [])
            if backend_refs:
                backend_service = backend_refs[0].get("name", "")
                backend_port = backend_refs[0].get("port", 0)

        parents = status.get("parents", [])
        accepted = False
        programmed = False
        reason = ""
        message = ""

        if parents:
            conditions = parents[0].get("conditions", [])
            for cond in conditions:
                if cond.get("type") == "Accepted":
                    accepted = cond.get("status") == "True"
                    if not accepted:
                        reason = cond.get("reason", "")
                        message = cond.get("message", "")
                if cond.get("type") == "ResolvedRefs":
                    programmed = cond.get("status") == "True"
                    if not programmed and not reason:
                        reason = cond.get("reason", "")
                        message = cond.get("message", "")

        routes.append(HTTPRouteStatus(
            name=metadata.get("name", ""),
            namespace=metadata.get("namespace", ""),
            hostnames=hostnames,
            parent_gateway=parent_gateway,
            backend_service=backend_service,
            backend_port=backend_port,
            accepted=accepted,
            programmed=programmed,
            reason=reason,
            message=message,
        ))

    return routes


def _get_envoy_pods(config: Config) -> list[dict]:
    """Get Envoy proxy pods."""
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
        status = item.get("status", {})
        spec = item.get("spec", {})

        container_statuses = status.get("containerStatuses", [])
        ready_count = sum(1 for c in container_statuses if c.get("ready", False))

        pods.append({
            "name": metadata.get("name", ""),
            "phase": status.get("phase", "Unknown"),
            "ready": f"{ready_count}/{len(container_statuses)}",
            "node": spec.get("nodeName", ""),
            "ip": status.get("podIP", ""),
        })

    return pods


def _check_service_endpoints(config: Config, service: str, namespace: str) -> dict:
    """Check if a service has healthy endpoints."""
    result = run_kubectl(
        config,
        ["get", "endpoints", service, "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return {"exists": False, "ready": 0, "addresses": []}

    data = parse_json_output(result.stdout)
    subsets = data.get("subsets", [])

    addresses = []
    ready_count = 0

    for subset in subsets:
        for addr in subset.get("addresses", []):
            addresses.append({
                "ip": addr.get("ip", ""),
                "ready": True,
                "target": addr.get("targetRef", {}).get("name", ""),
            })
            ready_count += 1

        for addr in subset.get("notReadyAddresses", []):
            addresses.append({
                "ip": addr.get("ip", ""),
                "ready": False,
                "target": addr.get("targetRef", {}).get("name", ""),
            })

    return {"exists": True, "ready": ready_count, "addresses": addresses}


def _resolve_dns(hostname: str) -> tuple[bool, str]:
    """Resolve a hostname to check DNS configuration."""
    try:
        result = socket.gethostbyname(hostname)
        return True, result
    except socket.gaierror as e:
        return False, str(e)


def _test_http_connectivity(url: str, timeout: int = 10) -> tuple[bool, int, str]:
    """Test HTTP connectivity to a URL."""
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-o", "/dev/null",
                "-w", "%{http_code}",
                "-m", str(timeout),
                "--connect-timeout", "5",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        status_code = int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
        return status_code > 0, status_code, ""
    except subprocess.TimeoutExpired:
        return False, 0, "Connection timeout"
    except Exception as e:
        return False, 0, str(e)


@click.group()
def gateway() -> None:
    """Gateway API troubleshooting commands.

    Commands for inspecting, debugging, and testing the Envoy Gateway
    and HTTPRoute configurations for UserDeployment routing.
    """
    pass


@gateway.command()
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--unhealthy", "-u", is_flag=True, help="Show only unhealthy routes")
@click.pass_context
def routes(ctx: click.Context, namespace: str | None, unhealthy: bool) -> None:
    """List HTTPRoutes with status and backend health.

    Shows all HTTPRoutes configured for UserDeployments, their acceptance
    status, and whether backends are reachable.
    """
    config: Config = ctx.obj

    print_header("Gateway Status")

    gw = _get_gateway(config)
    if gw:
        status_color = "green" if gw.programmed and gw.accepted else "red"
        console.print(f"Gateway: {gw.namespace}/{gw.name}")
        console.print(f"Address: [{status_color}]{gw.address}[/{status_color}] ({gw.address_type})")
        console.print(f"Status: Accepted={gw.accepted}, Programmed={gw.programmed}")

        for listener in gw.listeners:
            console.print(
                f"  Listener: {listener['name']} "
                f"({listener['protocol']}:{listener['port']}) "
                f"- {listener['attached_routes']} routes"
            )
    else:
        console.print("[red]Gateway not found[/red]")
        console.print(f"[dim]Checked namespaces: {BASILICA_SYSTEM_NS}, {GATEWAY_NAMESPACE}[/dim]")

    print_header("HTTPRoutes")

    all_routes = _get_all_httproutes(config, namespace)

    if not all_routes:
        console.print("[yellow]No HTTPRoutes found[/yellow]")
        return

    if unhealthy:
        all_routes = [r for r in all_routes if not r.accepted or not r.programmed]
        if not all_routes:
            console.print("[green]All routes are healthy[/green]")
            return

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Name")
    table.add_column("Hostname", max_width=40)
    table.add_column("Backend")
    table.add_column("Accepted")
    table.add_column("Resolved")

    for route in all_routes:
        accepted_color = "green" if route.accepted else "red"
        resolved_color = "green" if route.programmed else "red"

        hostname = route.hostnames[0] if route.hostnames else "-"
        if len(hostname) > 40:
            hostname = hostname[:37] + "..."

        backend = f"{route.backend_service}:{route.backend_port}" if route.backend_service else "-"

        table.add_row(
            route.namespace,
            route.name,
            hostname,
            backend,
            f"[{accepted_color}]{route.accepted}[/{accepted_color}]",
            f"[{resolved_color}]{route.programmed}[/{resolved_color}]",
        )

    console.print(table)

    issues = [r for r in all_routes if not r.accepted or not r.programmed]
    if issues:
        print_header("Route Issues")
        for route in issues:
            console.print(f"  [red]{route.namespace}/{route.name}[/red]")
            if route.reason:
                console.print(f"    Reason: {route.reason}")
            if route.message:
                console.print(f"    Message: {route.message[:100]}")


@gateway.command()
@click.option("--namespace", "-n", help="Filter by namespace")
@click.pass_context
def endpoints(ctx: click.Context, namespace: str | None) -> None:
    """Show Envoy endpoints and their state.

    Displays the Envoy proxy pods and the service endpoints they route to.
    """
    config: Config = ctx.obj

    print_header("Envoy Proxy Pods")

    pods = _get_envoy_pods(config)
    if not pods:
        console.print("[yellow]No Envoy pods found[/yellow]")
    else:
        table = Table()
        table.add_column("Pod", style="cyan")
        table.add_column("Phase")
        table.add_column("Ready")
        table.add_column("Node")
        table.add_column("IP")

        for pod in pods:
            phase_color = "green" if pod["phase"] == "Running" else "yellow"
            table.add_row(
                pod["name"],
                f"[{phase_color}]{pod['phase']}[/{phase_color}]",
                pod["ready"],
                pod["node"],
                pod["ip"],
            )

        console.print(table)

    print_header("Backend Endpoints")

    all_routes = _get_all_httproutes(config, namespace)
    if not all_routes:
        console.print("[yellow]No HTTPRoutes to check endpoints for[/yellow]")
        return

    seen_backends: set[tuple[str, str]] = set()
    for route in all_routes:
        if route.backend_service:
            key = (route.namespace, route.backend_service)
            if key in seen_backends:
                continue
            seen_backends.add(key)

            console.print(f"\n[bold]{route.namespace}/{route.backend_service}[/bold]")

            ep_status = _check_service_endpoints(config, route.backend_service, route.namespace)
            if not ep_status["exists"]:
                console.print("  [red]Endpoints not found[/red]")
                continue

            if not ep_status["addresses"]:
                console.print("  [yellow]No endpoints available[/yellow]")
                continue

            for addr in ep_status["addresses"]:
                ready_color = "green" if addr["ready"] else "red"
                ready_str = "Ready" if addr["ready"] else "NotReady"
                console.print(
                    f"  [{ready_color}]{addr['ip']}[/{ready_color}] "
                    f"({ready_str}) -> {addr['target']}"
                )


@gateway.command("test")
@click.argument("route_or_url")
@click.option("--namespace", "-n", help="Namespace for route lookup")
@click.option("--internal", "-i", is_flag=True, help="Test internal cluster connectivity only")
@click.pass_context
def test_route(
    ctx: click.Context,
    route_or_url: str,
    namespace: str | None,
    internal: bool,
) -> None:
    """Test connectivity through a specific route.

    Can test by route name or by public URL. Tests DNS resolution,
    internal K8s routing, and external connectivity.

    Examples:
        clustermgr gateway test ud-my-app -n u-alice
        clustermgr gateway test https://my-app.deployments.basilica.ai
    """
    config: Config = ctx.obj

    print_header(f"Connectivity Test: {route_or_url}")

    if route_or_url.startswith("http"):
        url = route_or_url
        hostname = url.split("//")[1].split("/")[0] if "//" in url else url.split("/")[0]
        route = None

        all_routes = _get_all_httproutes(config)
        for r in all_routes:
            if hostname in r.hostnames:
                route = r
                break
    else:
        route_name = route_or_url
        routes = _get_all_httproutes(config, namespace)
        route = next((r for r in routes if r.name == route_name), None)

        if not route:
            console.print(f"[red]HTTPRoute '{route_name}' not found[/red]")
            return

        hostname = route.hostnames[0] if route.hostnames else None
        url = f"https://{hostname}" if hostname else None

    if route:
        console.print(Panel(
            f"[bold]Route:[/bold] {route.namespace}/{route.name}\n"
            f"[bold]Hostname:[/bold] {', '.join(route.hostnames) if route.hostnames else 'N/A'}\n"
            f"[bold]Backend:[/bold] {route.backend_service}:{route.backend_port}\n"
            f"[bold]Accepted:[/bold] {route.accepted}\n"
            f"[bold]Resolved:[/bold] {route.programmed}",
            title="Route Details",
        ))

    print_header("Tests")

    if hostname:
        dns_ok, dns_result = _resolve_dns(hostname)
        severity = Severity.HEALTHY if dns_ok else Severity.CRITICAL
        print_status("DNS Resolution", f"{hostname} -> {dns_result}", severity)
    else:
        console.print("  [dim]DNS: No hostname configured[/dim]")

    if route and route.backend_service:
        ep_status = _check_service_endpoints(config, route.backend_service, route.namespace)
        if ep_status["exists"]:
            ready = ep_status["ready"]
            total = len(ep_status["addresses"])
            severity = Severity.HEALTHY if ready > 0 else Severity.CRITICAL
            print_status("Backend Endpoints", f"{ready}/{total} ready", severity)
        else:
            print_status("Backend Endpoints", "Not found", Severity.CRITICAL)

        svc_check = run_kubectl(
            config,
            ["get", "service", route.backend_service, "-n", route.namespace],
        )
        severity = Severity.HEALTHY if svc_check.returncode == 0 else Severity.CRITICAL
        print_status("Service Exists", str(svc_check.returncode == 0), severity)

    gw = _get_gateway(config)
    if gw:
        severity = Severity.HEALTHY if gw.programmed else Severity.WARNING
        print_status("Gateway Programmed", str(gw.programmed), severity)

        if gw.address and not internal:
            gw_url = f"http://{gw.address}:8080"
            http_ok, status_code, err = _test_http_connectivity(gw_url)
            if http_ok:
                severity = Severity.HEALTHY if status_code < 500 else Severity.WARNING
                print_status("Gateway Reachable", f"HTTP {status_code}", severity)
            else:
                print_status("Gateway Reachable", err or "Failed", Severity.CRITICAL)

    if url and not internal:
        http_ok, status_code, err = _test_http_connectivity(url)
        if http_ok:
            severity = Severity.HEALTHY if status_code < 400 else Severity.WARNING
            print_status("Public URL", f"HTTP {status_code}", severity)
        else:
            print_status("Public URL", err or "Failed", Severity.CRITICAL)

    if route:
        netpol_check = run_kubectl(
            config,
            ["get", "networkpolicy", "-n", route.namespace, "-o", "json"],
        )
        if netpol_check.returncode == 0:
            data = parse_json_output(netpol_check.stdout)
            policies = [item.get("metadata", {}).get("name", "") for item in data.get("items", [])]
            has_ingress = any("ingress" in p.lower() or "envoy" in p.lower() for p in policies)
            severity = Severity.HEALTHY if has_ingress else Severity.WARNING
            print_status("NetworkPolicy (Ingress)", f"{len(policies)} policies, ingress allowed: {has_ingress}", severity)


@gateway.command()
@click.pass_context
def sync(ctx: click.Context) -> None:
    """Check if routes are synced with UserDeployments.

    Compares HTTPRoutes with UserDeployments to find orphaned routes
    or deployments missing routes.
    """
    config: Config = ctx.obj

    print_header("Route Sync Check")

    routes = _get_all_httproutes(config)
    route_map: dict[str, HTTPRouteStatus] = {}
    for route in routes:
        if route.name.startswith("ud-"):
            ud_name = route.name[3:]
            key = f"{route.namespace}/{ud_name}"
            route_map[key] = route

    result = run_kubectl(config, ["get", "userdeployments", "-A", "-o", "json"])
    if result.returncode != 0:
        console.print("[red]Failed to get UserDeployments[/red]")
        return

    data = parse_json_output(result.stdout)
    ud_map: dict[str, dict] = {}
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        name = metadata.get("name", "")
        namespace = metadata.get("namespace", "")
        key = f"{namespace}/{name}"
        ud_map[key] = item

    missing_routes: list[str] = []
    orphaned_routes: list[str] = []
    synced: list[str] = []

    for key, ud in ud_map.items():
        status = ud.get("status", {})
        state = status.get("state", "")
        if state in ("Active", "Running", "Ready"):
            if key in route_map:
                synced.append(key)
            else:
                missing_routes.append(key)

    for key, route in route_map.items():
        if key not in ud_map:
            orphaned_routes.append(f"{route.namespace}/ud-{key.split('/')[-1]}")

    console.print(f"UserDeployments checked: {len(ud_map)}")
    console.print(f"HTTPRoutes found: {len(routes)}")
    console.print(f"  [green]Synced: {len(synced)}[/green]")
    console.print(f"  [yellow]Missing routes: {len(missing_routes)}[/yellow]")
    console.print(f"  [red]Orphaned routes: {len(orphaned_routes)}[/red]")

    if missing_routes:
        print_header("Missing HTTPRoutes")
        for key in missing_routes[:10]:
            console.print(f"  [yellow]{key}[/yellow] - No HTTPRoute found")
        if len(missing_routes) > 10:
            console.print(f"  [dim]... and {len(missing_routes) - 10} more[/dim]")

    if orphaned_routes:
        print_header("Orphaned HTTPRoutes")
        for route in orphaned_routes[:10]:
            console.print(f"  [red]{route}[/red] - No UserDeployment found")
        if len(orphaned_routes) > 10:
            console.print(f"  [dim]... and {len(orphaned_routes) - 10} more[/dim]")

    if not missing_routes and not orphaned_routes:
        console.print("\n[green]All routes are in sync[/green]")
