"""DNS diagnostics and management commands for GPU nodes.

This module provides commands to diagnose and fix DNS resolution issues
on GPU nodes connected via WireGuard. The primary issue addressed is
Flannel's MASQUERADE rules breaking return traffic for forwarded pods.

Key diagnostic areas:
- CoreDNS pod distribution and availability
- kube-dns service traffic policy configuration
- Flannel iptables MASQUERADE rules
- DNS resolution from GPU node pods

See docs/runbooks/GPU-NODE-DNS-RESOLUTION-FIX.md for detailed documentation.
"""

from dataclasses import dataclass

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    confirm,
    parse_json_output,
    print_header,
    print_status,
    run_ansible,
    run_kubectl,
)

console = Console()

COREDNS_SERVICE_IP = "10.43.0.10"
POD_CIDR = "10.42.0.0/16"
FLANNEL_FIX_COMMENT = "flannel skip forwarded pod traffic"


@dataclass
class CoreDNSPod:
    """CoreDNS pod information."""

    name: str
    node: str
    ip: str
    ready: bool
    is_gpu_node: bool


@dataclass
class DNSServiceStatus:
    """DNS service configuration status."""

    cluster_ip: str
    internal_traffic_policy: str
    endpoints_count: int


@dataclass
class FlannelRuleStatus:
    """Flannel iptables rule status."""

    server: str
    has_fix_rule: bool
    rule_position: int
    packet_count: int


@dataclass
class CoreDNSDeploymentType:
    """CoreDNS deployment type information."""

    is_daemonset: bool
    is_deployment: bool
    desired: int
    ready: int


@dataclass
class NodeDNSEndpoint:
    """DNS endpoint availability per node."""

    node_name: str
    has_local_endpoint: bool
    endpoint_ip: str | None
    is_ready: bool


def _get_nodes_without_local_dns(config: Config) -> list[str]:
    """Get nodes that have no local CoreDNS endpoint.

    When internalTrafficPolicy=Local, nodes without local endpoints
    will have DNS failures due to iptables DROP rules.
    """
    # Get all node names
    nodes_result = run_kubectl(
        config,
        ["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
        timeout=15,
    )
    if nodes_result.returncode != 0:
        return []
    all_nodes = set(nodes_result.stdout.strip().split())

    # Get nodes that have CoreDNS pods
    pods = _get_coredns_pods(config)
    nodes_with_coredns = {p.node for p in pods if p.ready}

    return sorted(all_nodes - nodes_with_coredns)


def _get_node_dns_endpoints(config: Config) -> list[NodeDNSEndpoint]:
    """Get DNS endpoint status per node.

    Checks EndpointSlice to see which nodes have local DNS endpoints.
    """
    # Get EndpointSlice for kube-dns
    result = run_kubectl(
        config,
        ["get", "endpointslices", "-n", "kube-system", "-l", "kubernetes.io/service-name=kube-dns", "-o", "json"],
        timeout=15,
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)

    # Build map of node -> endpoint info
    node_endpoints: dict[str, NodeDNSEndpoint] = {}

    for item in data.get("items", []):
        for endpoint in item.get("endpoints", []):
            node_name = endpoint.get("nodeName", "")
            if not node_name:
                continue

            addresses = endpoint.get("addresses", [])
            is_ready = endpoint.get("conditions", {}).get("ready", False)

            node_endpoints[node_name] = NodeDNSEndpoint(
                node_name=node_name,
                has_local_endpoint=True,
                endpoint_ip=addresses[0] if addresses else None,
                is_ready=is_ready,
            )

    # Get all nodes and mark those without endpoints
    nodes_result = run_kubectl(
        config,
        ["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"],
        timeout=15,
    )
    if nodes_result.returncode == 0:
        all_nodes = nodes_result.stdout.strip().split()
        for node in all_nodes:
            if node not in node_endpoints:
                node_endpoints[node] = NodeDNSEndpoint(
                    node_name=node,
                    has_local_endpoint=False,
                    endpoint_ip=None,
                    is_ready=False,
                )

    return sorted(node_endpoints.values(), key=lambda x: x.node_name)


def _get_coredns_deployment_type(config: Config) -> CoreDNSDeploymentType:
    """Detect whether CoreDNS is deployed as DaemonSet or Deployment."""
    # Check for DaemonSet first
    ds_result = run_kubectl(
        config,
        ["get", "daemonset", "coredns", "-n", "kube-system", "-o",
         "jsonpath={.status.desiredNumberScheduled}/{.status.numberReady}"],
        timeout=15,
    )
    if ds_result.returncode == 0 and "/" in ds_result.stdout:
        parts = ds_result.stdout.strip().split("/")
        if len(parts) == 2:
            desired = int(parts[0]) if parts[0] else 0
            ready = int(parts[1]) if parts[1] else 0
            return CoreDNSDeploymentType(
                is_daemonset=True,
                is_deployment=False,
                desired=desired,
                ready=ready,
            )

    # Check for Deployment
    deploy_result = run_kubectl(
        config,
        ["get", "deployment", "coredns", "-n", "kube-system", "-o",
         "jsonpath={.spec.replicas}/{.status.readyReplicas}"],
        timeout=15,
    )
    if deploy_result.returncode == 0 and "/" in deploy_result.stdout:
        parts = deploy_result.stdout.strip().split("/")
        if len(parts) == 2:
            desired = int(parts[0]) if parts[0] else 0
            ready = int(parts[1]) if parts[1] else 0
            return CoreDNSDeploymentType(
                is_daemonset=False,
                is_deployment=True,
                desired=desired,
                ready=ready,
            )

    return CoreDNSDeploymentType(
        is_daemonset=False,
        is_deployment=False,
        desired=0,
        ready=0,
    )


def _get_gpu_node_names(config: Config) -> set[str]:
    """Get names of GPU nodes (WireGuard-connected)."""
    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "jsonpath={.items[*].metadata.name}"],
        timeout=15,
    )
    if result.returncode != 0:
        return set()
    return set(result.stdout.strip().split())


def _get_coredns_pods(config: Config) -> list[CoreDNSPod]:
    """Get CoreDNS pod information."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns", "-o", "json"],
        timeout=15,
    )
    if result.returncode != 0:
        return []

    gpu_nodes = _get_gpu_node_names(config)
    data = parse_json_output(result.stdout)
    pods = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        node_name = spec.get("nodeName", "")
        ready = False
        for cond in status.get("conditions", []):
            if cond.get("type") == "Ready" and cond.get("status") == "True":
                ready = True
                break

        pods.append(CoreDNSPod(
            name=metadata.get("name", ""),
            node=node_name,
            ip=status.get("podIP", ""),
            ready=ready,
            is_gpu_node=node_name in gpu_nodes,
        ))

    return pods


def _get_dns_service_status(config: Config) -> DNSServiceStatus | None:
    """Get kube-dns service configuration."""
    result = run_kubectl(
        config,
        ["get", "svc", "kube-dns", "-n", "kube-system", "-o", "json"],
        timeout=15,
    )
    if result.returncode != 0:
        return None

    data = parse_json_output(result.stdout)
    spec = data.get("spec", {})

    endpoints_result = run_kubectl(
        config,
        ["get", "endpoints", "kube-dns", "-n", "kube-system", "-o", "json"],
        timeout=15,
    )
    endpoints_count = 0
    if endpoints_result.returncode == 0:
        ep_data = parse_json_output(endpoints_result.stdout)
        for subset in ep_data.get("subsets", []):
            endpoints_count += len(subset.get("addresses", []))

    return DNSServiceStatus(
        cluster_ip=spec.get("clusterIP", ""),
        internal_traffic_policy=spec.get("internalTrafficPolicy", "Cluster"),
        endpoints_count=endpoints_count,
    )


def _check_flannel_rules(config: Config) -> list[FlannelRuleStatus]:
    """Check Flannel MASQUERADE rules on all k3s servers."""
    result = run_ansible(
        config,
        "shell",
        f"iptables -t nat -L FLANNEL-POSTRTG -n -v --line-numbers 2>/dev/null | grep -E '{FLANNEL_FIX_COMMENT}|pkts' | head -5",
        timeout=30,
    )
    if result.returncode != 0:
        return []

    statuses = []
    current_server = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and FLANNEL_FIX_COMMENT in line:
            parts = line.split()
            position = int(parts[0]) if parts and parts[0].isdigit() else 0
            packets = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            statuses.append(FlannelRuleStatus(
                server=current_server,
                has_fix_rule=True,
                rule_position=position,
                packet_count=packets,
            ))
            current_server = None

    # Check for servers without the rule
    all_servers_result = run_ansible(
        config,
        "shell",
        "hostname",
        timeout=15,
    )
    if all_servers_result.returncode == 0:
        all_servers = set()
        for line in all_servers_result.stdout.split("\n"):
            if " | CHANGED" in line or " | SUCCESS" in line:
                all_servers.add(line.split(" | ")[0].strip())

        servers_with_rule = {s.server for s in statuses}
        for server in all_servers - servers_with_rule:
            statuses.append(FlannelRuleStatus(
                server=server,
                has_fix_rule=False,
                rule_position=0,
                packet_count=0,
            ))

    return statuses


@click.group()
def dns() -> None:
    """DNS diagnostics for GPU nodes.

    Commands to diagnose and fix DNS resolution issues on GPU nodes
    connected via WireGuard. Addresses Flannel MASQUERADE issues that
    break pod-to-pod communication through k3s servers.

    See docs/runbooks/GPU-NODE-DNS-RESOLUTION-FIX.md for details.
    """
    pass


@dns.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show CoreDNS and DNS service status.

    Displays CoreDNS pod distribution, kube-dns service configuration,
    and identifies if GPU nodes have local CoreDNS pods.
    """
    config: Config = ctx.obj

    print_header("CoreDNS Deployment")

    deploy_type = _get_coredns_deployment_type(config)
    if deploy_type.is_daemonset:
        type_str = "[green]DaemonSet[/green]"
        health_ok = deploy_type.ready == deploy_type.desired and deploy_type.desired > 0
    elif deploy_type.is_deployment:
        type_str = "[yellow]Deployment[/yellow]"
        health_ok = deploy_type.ready == deploy_type.desired and deploy_type.desired > 0
    else:
        type_str = "[red]Not Found[/red]"
        health_ok = False

    health_str = f"[green]{deploy_type.ready}/{deploy_type.desired}[/green]" if health_ok else f"[red]{deploy_type.ready}/{deploy_type.desired}[/red]"
    console.print(f"Type: {type_str}  Ready: {health_str}")

    print_header("CoreDNS Pod Status")

    pods = _get_coredns_pods(config)
    if not pods:
        console.print("[red]Failed to get CoreDNS pods[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Pod", style="cyan")
    table.add_column("Node")
    table.add_column("IP")
    table.add_column("Ready")
    table.add_column("GPU Node")

    gpu_nodes_with_coredns = 0
    for pod in pods:
        ready_str = "[green]Yes[/green]" if pod.ready else "[red]No[/red]"
        gpu_str = "[green]Yes[/green]" if pod.is_gpu_node else "[dim]No[/dim]"
        if pod.is_gpu_node and pod.ready:
            gpu_nodes_with_coredns += 1

        table.add_row(
            pod.name[:40],
            pod.node[:30],
            pod.ip,
            ready_str,
            gpu_str,
        )

    console.print(table)
    console.print(f"\nTotal pods: {len(pods)}, On GPU nodes: {gpu_nodes_with_coredns}")

    print_header("kube-dns Service")

    svc_status = _get_dns_service_status(config)
    if not svc_status:
        console.print("[red]Failed to get kube-dns service[/red]")
        ctx.exit(1)

    policy_ok = svc_status.internal_traffic_policy == "Local"
    policy_color = "green" if policy_ok else "yellow"

    svc_table = Table()
    svc_table.add_column("Property", style="cyan")
    svc_table.add_column("Value")

    svc_table.add_row("ClusterIP", svc_status.cluster_ip)
    svc_table.add_row("internalTrafficPolicy", f"[{policy_color}]{svc_status.internal_traffic_policy}[/{policy_color}]")
    svc_table.add_row("Endpoints", str(svc_status.endpoints_count))

    console.print(svc_table)

    if not policy_ok:
        console.print("\n[yellow]Recommendation: Set internalTrafficPolicy to 'Local' for GPU node reliability[/yellow]")
        console.print("Run 'clustermgr dns fix --traffic-policy' to apply")


@dns.command("iptables")
@click.pass_context
def iptables_status(ctx: click.Context) -> None:
    """Check Flannel MASQUERADE iptables rules.

    Verifies that the fix rule exists on all k3s servers to skip
    MASQUERADE for forwarded pod-to-pod traffic.
    """
    config: Config = ctx.obj

    print_header("Flannel MASQUERADE Rules")

    statuses = _check_flannel_rules(config)
    if not statuses:
        console.print("[yellow]Could not check Flannel rules[/yellow]")
        return

    table = Table()
    table.add_column("Server", style="cyan")
    table.add_column("Fix Rule Present")
    table.add_column("Position")
    table.add_column("Packets Matched")

    all_ok = True
    for s in sorted(statuses, key=lambda x: x.server):
        if s.has_fix_rule:
            present_str = "[green]Yes[/green]"
            pos_str = str(s.rule_position)
            packets_str = str(s.packet_count)
        else:
            present_str = "[red]No[/red]"
            pos_str = "-"
            packets_str = "-"
            all_ok = False

        table.add_row(s.server, present_str, pos_str, packets_str)

    console.print(table)

    if all_ok:
        console.print("\n[green]All servers have the Flannel fix rule[/green]")
    else:
        console.print("\n[red]Some servers are missing the Flannel fix rule[/red]")
        console.print("Run 'clustermgr dns fix --iptables' to apply")


@dns.command("endpoints")
@click.pass_context
def endpoints(ctx: click.Context) -> None:
    """Show DNS endpoint availability per node.

    Critical for diagnosing internalTrafficPolicy: Local issues.
    Nodes without local endpoints will have DNS failures.
    """
    config: Config = ctx.obj

    print_header("DNS Endpoints Per Node")

    svc_status = _get_dns_service_status(config)
    if svc_status:
        policy = svc_status.internal_traffic_policy
        policy_color = "green" if policy == "Local" else "yellow"
        console.print(f"kube-dns internalTrafficPolicy: [{policy_color}]{policy}[/{policy_color}]")

        if policy == "Local":
            console.print("[dim]With Local policy, nodes without local endpoints will have DNS failures[/dim]\n")

    endpoints = _get_node_dns_endpoints(config)
    if not endpoints:
        console.print("[red]Failed to get DNS endpoints[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Local Endpoint")
    table.add_column("Endpoint IP")
    table.add_column("Ready")

    nodes_without_dns = 0
    for ep in endpoints:
        if ep.has_local_endpoint:
            endpoint_str = "[green]Yes[/green]"
            ip_str = ep.endpoint_ip or "-"
            ready_str = "[green]Yes[/green]" if ep.is_ready else "[yellow]No[/yellow]"
        else:
            endpoint_str = "[red]NO[/red]"
            ip_str = "-"
            ready_str = "[red]-[/red]"
            nodes_without_dns += 1

        table.add_row(ep.node_name[:40], endpoint_str, ip_str, ready_str)

    console.print(table)

    if nodes_without_dns > 0 and svc_status and svc_status.internal_traffic_policy == "Local":
        console.print(f"\n[red]WARNING: {nodes_without_dns} node(s) have NO local DNS endpoint![/red]")
        console.print("[red]Pods on these nodes will fail DNS resolution due to iptables DROP rules.[/red]")
        console.print("\n[bold]Remediation options:[/bold]")
        console.print("  1. Deploy CoreDNS as DaemonSet (recommended): kubectl apply -f orchestrator/k8s/core/coredns-daemonset.yaml")
        console.print("  2. Scale CoreDNS Deployment and add affinity rules")
        console.print("\nRun 'clustermgr dns diagnose' for full analysis")
    elif nodes_without_dns > 0:
        console.print(f"\n[yellow]{nodes_without_dns} node(s) have no local DNS endpoint[/yellow]")
        console.print("[dim]This is only a problem if internalTrafficPolicy is set to Local[/dim]")


@dns.command("test")
@click.option("--domain", "-d", default="pypi.org", help="Domain to resolve")
@click.option("--count", "-c", default=5, help="Number of resolution attempts")
@click.pass_context
def test_resolution(ctx: click.Context, domain: str, count: int) -> None:
    """Test DNS resolution from GPU node pods.

    Finds a pod running on a GPU node and tests DNS resolution.
    Prefers user deployment pods (u-* namespaces) for testing.
    """
    config: Config = ctx.obj

    print_header(f"DNS Resolution Test: {domain}")

    gpu_nodes = _get_gpu_node_names(config)
    if not gpu_nodes:
        console.print("[yellow]No GPU nodes found[/yellow]")
        return

    console.print(f"GPU nodes: {', '.join(gpu_nodes)}\n")

    # Find a pod on a GPU node - prefer user deployment pods
    result = run_kubectl(
        config,
        ["get", "pods", "--all-namespaces", "-o", "json", "--field-selector=status.phase=Running"],
        timeout=30,
    )
    if result.returncode != 0:
        console.print("[red]Failed to list pods[/red]")
        ctx.exit(1)

    data = parse_json_output(result.stdout)
    test_pod = None
    test_namespace = None
    fallback_pod = None
    fallback_namespace = None

    for item in data.get("items", []):
        spec = item.get("spec", {})
        metadata = item.get("metadata", {})
        node_name = spec.get("nodeName", "")

        if node_name not in gpu_nodes:
            continue

        ns = metadata.get("namespace", "")
        pod_name = metadata.get("name", "")

        # Skip system namespaces entirely
        if ns.startswith("kube-") or ns in ("basilica-system", "basilica-storage"):
            continue

        # Prefer user deployment namespaces (u-*)
        if ns.startswith("u-"):
            test_pod = pod_name
            test_namespace = ns
            break

        # Keep as fallback
        if not fallback_pod:
            fallback_pod = pod_name
            fallback_namespace = ns

    if not test_pod and fallback_pod:
        test_pod = fallback_pod
        test_namespace = fallback_namespace

    if not test_pod:
        console.print("[yellow]No user pods found on GPU nodes to test[/yellow]")
        console.print("Try running a test pod: kubectl run dns-test --image=busybox:1.36 --rm -it -- nslookup pypi.org")
        return

    console.print(f"Testing from pod: {test_pod} in namespace: {test_namespace}\n")

    # Determine which DNS tool is available
    dns_tool = None
    for tool in ["getent", "nslookup", "host"]:
        check_result = run_kubectl(
            config,
            ["exec", "-n", test_namespace, test_pod, "--", "which", tool],
            timeout=5,
        )
        if check_result.returncode == 0:
            dns_tool = tool
            break

    if not dns_tool:
        console.print("[yellow]No DNS lookup tool available in pod (getent/nslookup/host)[/yellow]")
        console.print("Checking /etc/resolv.conf instead...")
        resolv_result = run_kubectl(
            config,
            ["exec", "-n", test_namespace, test_pod, "--", "cat", "/etc/resolv.conf"],
            timeout=5,
        )
        if resolv_result.returncode == 0:
            console.print(resolv_result.stdout)
            if COREDNS_SERVICE_IP in resolv_result.stdout:
                console.print(f"\n[green]DNS configured correctly (nameserver {COREDNS_SERVICE_IP})[/green]")
            else:
                console.print("\n[yellow]DNS nameserver not pointing to CoreDNS[/yellow]")
        return

    console.print(f"Using: {dns_tool}\n")

    success_count = 0
    results = []

    for i in range(count):
        if dns_tool == "getent":
            exec_result = run_kubectl(
                config,
                ["exec", "-n", test_namespace, test_pod, "--", "getent", "hosts", domain],
                timeout=10,
            )
            if exec_result.returncode == 0:
                ip = exec_result.stdout.split()[0] if exec_result.stdout.strip() else "resolved"
                success_count += 1
                results.append(f"OK: {ip}")
            else:
                results.append("FAIL: resolution failed")
        else:
            # nslookup or host
            exec_result = run_kubectl(
                config,
                ["exec", "-n", test_namespace, test_pod, "--", dns_tool, domain],
                timeout=10,
            )
            if exec_result.returncode == 0:
                ip = "resolved"
                for line in exec_result.stdout.split("\n"):
                    if "Address:" in line and ":" not in line.split("Address:")[1].strip()[:4]:
                        ip = line.split("Address:")[1].strip()
                        break
                success_count += 1
                results.append(f"OK: {ip}")
            else:
                results.append(f"FAIL: {exec_result.stderr.strip()[:40]}")

    # Display results
    success_line = f"Success: {success_count}/{count}"
    if success_count == count:
        console.print(f"[green]{success_line}[/green]")
    elif success_count == 0:
        console.print(f"[red]{success_line}[/red]")
    else:
        console.print(f"[yellow]{success_line}[/yellow]")

    for r in results:
        if r.startswith("OK:"):
            console.print(f"  [green]{r}[/green]")
        else:
            console.print(f"  [red]{r}[/red]")


@dns.command("diagnose")
@click.pass_context
def diagnose(ctx: click.Context) -> None:
    """Run comprehensive DNS diagnostics.

    Checks all components that affect DNS resolution on GPU nodes:
    - CoreDNS pod distribution
    - kube-dns service traffic policy
    - Flannel iptables rules
    - Conntrack entries (for debugging)
    """
    config: Config = ctx.obj

    print_header("DNS Comprehensive Diagnostics")

    issues: list[tuple[str, str, Severity]] = []

    # Check 1: CoreDNS pods
    console.print("Checking CoreDNS pods...")
    pods = _get_coredns_pods(config)
    gpu_nodes = _get_gpu_node_names(config)

    if not pods:
        issues.append(("CoreDNS", "No pods found", Severity.CRITICAL))
    else:
        ready_pods = [p for p in pods if p.ready]
        gpu_pods = [p for p in pods if p.is_gpu_node and p.ready]

        if len(ready_pods) < 2:
            issues.append(("CoreDNS", f"Only {len(ready_pods)} ready pod(s), recommend 3+", Severity.WARNING))

        if gpu_nodes and not gpu_pods:
            issues.append(("CoreDNS", "No CoreDNS pods on GPU nodes", Severity.WARNING))
        else:
            for gpu in gpu_nodes:
                has_local = any(p.node == gpu and p.ready for p in pods)
                if not has_local:
                    issues.append(("CoreDNS", f"GPU node {gpu[:20]} has no local CoreDNS", Severity.WARNING))

    # Check 2: kube-dns service and local endpoint coverage
    console.print("Checking kube-dns service...")
    svc_status = _get_dns_service_status(config)
    if not svc_status:
        issues.append(("kube-dns", "Service not found", Severity.CRITICAL))
    else:
        if svc_status.internal_traffic_policy != "Local":
            issues.append(("kube-dns", f"internalTrafficPolicy is '{svc_status.internal_traffic_policy}', should be 'Local'", Severity.WARNING))

        # CRITICAL: Check for nodes without local DNS endpoints when using Local policy
        if svc_status.internal_traffic_policy == "Local":
            nodes_without_dns = _get_nodes_without_local_dns(config)
            if nodes_without_dns:
                issue_desc = f"{len(nodes_without_dns)} node(s) have NO local DNS endpoint: {', '.join(nodes_without_dns[:3])}"
                if len(nodes_without_dns) > 3:
                    issue_desc += f" (+{len(nodes_without_dns) - 3} more)"
                issues.append(("kube-dns", issue_desc, Severity.CRITICAL))
                issues.append(("kube-dns", "Pods on these nodes will fail DNS - deploy CoreDNS as DaemonSet", Severity.CRITICAL))

    # Check 3: Flannel iptables rules (only relevant for Deployment mode)
    # With DaemonSet + internalTrafficPolicy: Local, DNS never crosses nodes
    deploy_type = _get_coredns_deployment_type(config)
    flannel_statuses: list[FlannelRuleStatus] = []
    if deploy_type.is_daemonset and svc_status and svc_status.internal_traffic_policy == "Local":
        console.print("Skipping Flannel check (DaemonSet + Local policy = local DNS only)")
    else:
        console.print("Checking Flannel iptables rules...")
        flannel_statuses = _check_flannel_rules(config)
        for s in flannel_statuses:
            if not s.has_fix_rule:
                # Only warn, not critical, since the fix rule was optional
                issues.append(("Flannel", f"Server {s.server} missing MASQUERADE fix rule (optional with DaemonSet)", Severity.WARNING))

    # Check 4: Conntrack (informational)
    console.print("Checking conntrack entries...")
    conntrack_result = run_ansible(
        config,
        "shell",
        "conntrack -L 2>/dev/null | grep -E '10.42.*:53' | grep -v ASSURED | head -3 || echo 'No suspicious entries'",
        hosts="k3s_server[0]",
        timeout=15,
    )
    if "dst=10.42.0" in conntrack_result.stdout and "src=10.42" in conntrack_result.stdout:
        issues.append(("Conntrack", "Found entries with incorrect SNAT (dst=10.42.0.x)", Severity.WARNING))

    # Summary
    print_header("Diagnostic Summary")

    if not issues:
        console.print("[green]No DNS issues detected[/green]")
        console.print(f"\nChecked: {len(pods)} CoreDNS pods, {len(gpu_nodes)} GPU nodes, {len(flannel_statuses)} servers")
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
        print_header("Recommended Fixes")
        console.print("Run 'clustermgr dns fix' to apply all fixes")
        console.print("Or apply individually:")
        console.print("  clustermgr dns fix --iptables      # Fix Flannel MASQUERADE rules")
        console.print("  clustermgr dns fix --traffic-policy # Set internalTrafficPolicy to Local")
        console.print("  clustermgr dns fix --verify-coredns  # Verify CoreDNS health")
        ctx.exit(1)


@dns.command("fix")
@click.option("--iptables", is_flag=True, help="Fix Flannel MASQUERADE iptables rules")
@click.option("--traffic-policy", is_flag=True, help="Set kube-dns internalTrafficPolicy to Local")
@click.option("--verify-coredns", is_flag=True, help="Verify CoreDNS health (DaemonSet or Deployment)")
@click.option("--all", "fix_all", is_flag=True, help="Apply all fixes")
@click.pass_context
def fix_dns(
    ctx: click.Context,
    iptables: bool,
    traffic_policy: bool,
    verify_coredns: bool,
    fix_all: bool,
) -> None:
    """Apply DNS fixes for GPU nodes.

    Applies fixes documented in GPU-NODE-DNS-RESOLUTION-FIX.md:
    - Flannel iptables MASQUERADE skip rule
    - kube-dns internalTrafficPolicy: Local
    - CoreDNS health verification (DaemonSet or Deployment)
    """
    config: Config = ctx.obj

    if fix_all:
        iptables = traffic_policy = verify_coredns = True

    if not any([iptables, traffic_policy, verify_coredns]):
        console.print("[yellow]No fix option specified. Use --all or specific options.[/yellow]")
        console.print("Options: --iptables, --traffic-policy, --verify-coredns, --all")
        return

    print_header("Applying DNS Fixes")

    if config.dry_run:
        console.print("[yellow][DRY RUN] Would apply the following fixes:[/yellow]")
        if iptables:
            console.print("  - Add iptables RETURN rule to FLANNEL-POSTRTG on all k3s servers")
        if traffic_policy:
            console.print("  - Patch kube-dns service with internalTrafficPolicy: Local")
        if verify_coredns:
            console.print("  - Verify CoreDNS health and node coverage")
        return

    if not config.no_confirm:
        fixes = []
        if iptables:
            fixes.append("Flannel iptables rules")
        if traffic_policy:
            fixes.append("kube-dns traffic policy")
        if verify_coredns:
            fixes.append("CoreDNS verification")

        if not confirm(f"Apply fixes: {', '.join(fixes)}?"):
            console.print("Aborted.")
            return

    # Fix 1: Flannel iptables
    if iptables:
        console.print("\n[bold]Fix 1: Flannel MASQUERADE rules[/bold]")

        fix_cmd = f"""
if iptables -t nat -C FLANNEL-POSTRTG -s {POD_CIDR} -d {POD_CIDR} -j RETURN -m comment --comment "{FLANNEL_FIX_COMMENT}" 2>/dev/null; then
    echo "Rule already exists"
else
    iptables -t nat -I FLANNEL-POSTRTG 1 -s {POD_CIDR} -d {POD_CIDR} -j RETURN -m comment --comment "{FLANNEL_FIX_COMMENT}"
    echo "Rule added"
fi
"""
        result = run_ansible(config, "shell", fix_cmd, timeout=30)

        # Parse results
        for line in result.stdout.split("\n"):
            if " | CHANGED" in line or " | SUCCESS" in line:
                server = line.split(" | ")[0].strip()
            elif "Rule added" in line:
                print_status(server, "Rule added", Severity.HEALTHY)
            elif "Rule already exists" in line:
                print_status(server, "Already configured", Severity.HEALTHY)

    # Fix 2: Traffic policy
    if traffic_policy:
        console.print("\n[bold]Fix 2: kube-dns internalTrafficPolicy[/bold]")

        result = run_kubectl(
            config,
            ["patch", "svc", "kube-dns", "-n", "kube-system",
             "-p", '{"spec":{"internalTrafficPolicy":"Local"}}'],
            timeout=15,
        )

        if result.returncode == 0:
            print_status("kube-dns", "Patched to Local", Severity.HEALTHY)
        else:
            print_status("kube-dns", f"Failed: {result.stderr}", Severity.CRITICAL)

    # Fix 3: Verify CoreDNS health
    if verify_coredns:
        console.print("\n[bold]Fix 3: CoreDNS health verification[/bold]")

        deploy_type = _get_coredns_deployment_type(config)
        pods = _get_coredns_pods(config)
        ready_pods = len([p for p in pods if p.ready])
        gpu_pods = len([p for p in pods if p.is_gpu_node and p.ready])

        if deploy_type.is_daemonset:
            console.print(f"  Deployment type: DaemonSet")
            health_ok = deploy_type.ready == deploy_type.desired and deploy_type.desired > 0
            if health_ok:
                print_status("CoreDNS", f"DaemonSet healthy: {deploy_type.ready}/{deploy_type.desired} nodes", Severity.HEALTHY)
            else:
                print_status("CoreDNS", f"DaemonSet degraded: {deploy_type.ready}/{deploy_type.desired} nodes", Severity.WARNING)
        elif deploy_type.is_deployment:
            console.print(f"  Deployment type: Deployment (legacy)")
            if deploy_type.ready < 3:
                console.print("  Scaling Deployment to 3 replicas...")
                result = run_kubectl(
                    config,
                    ["scale", "deployment", "coredns", "-n", "kube-system", "--replicas=3"],
                    timeout=30,
                )
                if result.returncode == 0:
                    print_status("CoreDNS", "Scaled to 3 replicas", Severity.HEALTHY)
                else:
                    print_status("CoreDNS", f"Failed to scale: {result.stderr}", Severity.CRITICAL)
            else:
                print_status("CoreDNS", f"Deployment healthy: {deploy_type.ready}/{deploy_type.desired}", Severity.HEALTHY)
        else:
            print_status("CoreDNS", "Not found - neither DaemonSet nor Deployment", Severity.CRITICAL)

        console.print(f"  Total ready pods: {ready_pods}, On GPU nodes: {gpu_pods}")

    console.print("\n[green]DNS fixes applied[/green]")
    console.print("Run 'clustermgr dns diagnose' to verify")
