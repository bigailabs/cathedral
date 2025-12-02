"""Fix/remediation command for clustermgr."""

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

import click
from rich.console import Console

from clustermgr.commands.diagnose import diagnose_interface_health
from clustermgr.commands.fuse_troubleshoot import (
    FUSE_DAEMONSET,
    FUSE_LOADER_DAEMONSET,
    FUSE_LOADER_NAMESPACE,
    FUSE_NAMESPACE,
    _diagnose_fuse_issues,
)
from clustermgr.commands.health import (
    check_iptables_drops,
    check_wireguard_peers,
    health,
)
from clustermgr.commands.wg import check_reconcile_needed
from clustermgr.config import Config
from clustermgr.utils import confirm, parse_json_output, print_header, run_ansible, run_kubectl

console = Console()

REQUIRED_NETPOLS = [
    "default-deny-all",
    "allow-dns",
    "allow-internet-egress",
    "allow-ingress-from-envoy",
]


@dataclass
class RemediationStep:
    """A single remediation step."""

    name: str
    description: str
    command: list[str]
    impact: int
    reversible: bool


def plan_remediation(config: Config) -> list[RemediationStep]:
    """Analyze cluster state and create remediation plan."""
    steps: list[RemediationStep] = []

    # Check for dropped packets on WireGuard interface
    interface_findings = diagnose_interface_health(config)
    servers_with_drops: list[str] = []
    for finding in interface_findings:
        if finding.get("type") == "dropped_packets":
            servers_with_drops.append(finding["server"])

    if servers_with_drops:
        steps.append(
            RemediationStep(
                name="Optimize WireGuard network buffers",
                description=(
                    f"Increase TX queue and network buffers on: "
                    f"{', '.join(servers_with_drops)}"
                ),
                command=[
                    "ansible",
                    ",".join(servers_with_drops),
                    "-m",
                    "shell",
                    "-a",
                    (
                        "ip link set wg0 txqueuelen 1000 && "
                        "sysctl -w net.core.wmem_max=16777216 && "
                        "sysctl -w net.core.rmem_max=16777216 && "
                        "sysctl -w net.core.netdev_max_backlog=5000"
                    ),
                ],
                impact=3,
                reversible=True,
            )
        )

    # Check for rate limit rules
    drops = check_iptables_drops(config)
    for server, info in drops.items():
        if info["has_rate_limit"]:
            steps.append(
                RemediationStep(
                    name=f"Remove rate limit on {server}",
                    description=f"Remove iptables rate limit rule (currently {info['drops']} drops)",
                    command=[
                        "ansible",
                        server,
                        "-m",
                        "shell",
                        "-a",
                        (
                            "while sudo iptables -D INPUT -p udp --dport 51820 -m hashlimit "
                            "--hashlimit-name wireguard_handshake --hashlimit-mode srcip "
                            "--hashlimit-above 10/minute --hashlimit-burst 5 "
                            "-m comment --comment 'Rate limit WireGuard handshakes' -j DROP 2>/dev/null; "
                            "do :; done && "
                            "sudo iptables-save > /etc/iptables.rules.v4"
                        ),
                    ],
                    impact=5,
                    reversible=True,
                )
            )

    # Check for stale WireGuard handshakes
    peers = check_wireguard_peers(config)
    servers_with_stale: set[str] = set()
    for server, server_peers in peers.items():
        for peer in server_peers:
            if peer.get("handshake_stale"):
                servers_with_stale.add(server)

    if servers_with_stale:
        steps.append(
            RemediationStep(
                name="Restart WireGuard",
                description=(
                    f"Restart WireGuard on servers with stale handshakes: "
                    f"{', '.join(servers_with_stale)}"
                ),
                command=[
                    "ansible",
                    ",".join(servers_with_stale),
                    "-m",
                    "shell",
                    "-a",
                    "sudo systemctl restart wg-quick@wg0",
                ],
                impact=7,
                reversible=False,
            )
        )

    # Check for WireGuard peer reconciliation needs
    peers_needing_reconcile = check_reconcile_needed(config)
    if peers_needing_reconcile:
        peer_names = [p.node_name for p in peers_needing_reconcile]
        steps.append(
            RemediationStep(
                name="Reconcile WireGuard peer AllowedIPs",
                description=(
                    f"Add missing pod CIDRs to AllowedIPs for: "
                    f"{', '.join(peer_names[:3])}{'...' if len(peer_names) > 3 else ''}"
                ),
                command=[
                    "clustermgr",
                    "wg",
                    "reconcile",
                    "--fix",
                    "-y",
                ],
                impact=4,
                reversible=True,
            )
        )

    # Check for FUSE daemon issues
    fuse_issues = _diagnose_fuse_issues(config)
    fuse_loader_restart_needed = False
    fuse_daemon_restart_needed = False
    nodes_with_stale_mounts: list[str] = []

    for issue in fuse_issues:
        if issue.issue_type in ("fuse_loader_missing", "fuse_loader_unhealthy"):
            fuse_loader_restart_needed = True
        if issue.issue_type in (
            "fuse_daemon_missing",
            "fuse_daemon_not_running",
            "fuse_daemon_not_ready",
            "fuse_daemon_crash_loop",
        ):
            fuse_daemon_restart_needed = True
        if issue.issue_type == "stale_fuse_mounts":
            nodes_with_stale_mounts.append(issue.node)

    # Handle stale FUSE mounts
    if nodes_with_stale_mounts:
        for node in nodes_with_stale_mounts:
            steps.append(
                RemediationStep(
                    name=f"Clean stale FUSE mounts on {node}",
                    description="Remove stale FUSE mount points and restart fuse-daemon",
                    command=[
                        "clustermgr",
                        "fuse-troubleshoot",
                        node,
                        "--fix-mounts",
                        "-y",
                    ],
                    impact=3,
                    reversible=False,
                )
            )

    if fuse_loader_restart_needed:
        steps.append(
            RemediationStep(
                name="Restart FUSE module loader",
                description="Restart fuse-module-loader DaemonSet to reload FUSE kernel module",
                command=[
                    "kubectl",
                    "rollout",
                    "restart",
                    f"daemonset/{FUSE_LOADER_DAEMONSET}",
                    "-n",
                    FUSE_LOADER_NAMESPACE,
                ],
                impact=4,
                reversible=False,
            )
        )

    if fuse_daemon_restart_needed:
        steps.append(
            RemediationStep(
                name="Restart FUSE daemon",
                description="Restart fuse-daemon DaemonSet to recover unhealthy pods",
                command=[
                    "kubectl",
                    "rollout",
                    "restart",
                    f"daemonset/{FUSE_DAEMONSET}",
                    "-n",
                    FUSE_NAMESPACE,
                ],
                impact=5,
                reversible=False,
            )
        )

    # Check for CrashLoopBackOff pods
    result = run_kubectl(
        config,
        ["get", "pods", "-A", "-o", "json"],
        timeout=30,
    )
    if result.returncode == 0:
        data = parse_json_output(result.stdout)
        crashloop_pods: list[str] = []
        for item in data.get("items", []):
            for cs in item.get("status", {}).get("containerStatuses", []):
                waiting = cs.get("state", {}).get("waiting", {})
                if waiting.get("reason") == "CrashLoopBackOff":
                    ns = item["metadata"]["namespace"]
                    name = item["metadata"]["name"]
                    crashloop_pods.append(f"{ns}/{name}")

        if crashloop_pods:
            steps.append(
                RemediationStep(
                    name="Delete CrashLoopBackOff pods",
                    description=f"Delete {len(crashloop_pods)} pods in CrashLoopBackOff state",
                    command=[
                        "kubectl",
                        "delete",
                        "pod",
                        "-A",
                        "--field-selector=status.phase!=Running",
                        "--force",
                        "--grace-period=0",
                    ],
                    impact=3,
                    reversible=False,
                )
            )

    # Check for UserDeployment issues
    steps.extend(_check_userdeployment_issues(config))

    # Check for missing NetworkPolicies in tenant namespaces
    steps.extend(_check_missing_netpols(config))

    # Check for orphaned HTTPRoutes
    steps.extend(_check_orphaned_routes(config))

    # Check for Flannel VXLAN issues (HTTP 503 root cause)
    steps.extend(_check_flannel_issues(config))

    return steps


def _check_userdeployment_issues(config: Config) -> list[RemediationStep]:
    """Check for UserDeployment issues that can be remediated."""
    steps: list[RemediationStep] = []

    result = run_kubectl(config, ["get", "userdeployments", "-A", "-o", "json"])
    if result.returncode != 0:
        return steps

    data = parse_json_output(result.stdout)
    failed_uds: list[tuple[str, str, str]] = []
    pending_uds: list[tuple[str, str]] = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        name = metadata.get("name", "")
        namespace = metadata.get("namespace", "")
        state = status.get("state", "")
        message = status.get("message", "")

        if state == "Failed":
            failed_uds.append((namespace, name, message))
        elif state in ("Pending", "Creating"):
            result = run_kubectl(
                config,
                ["get", "pods", "-n", namespace, "-l", f"app={name}", "-o", "json"],
            )
            if result.returncode == 0:
                pod_data = parse_json_output(result.stdout)
                pods = pod_data.get("items", [])
                for pod in pods:
                    for cs in pod.get("status", {}).get("containerStatuses", []):
                        waiting = cs.get("state", {}).get("waiting", {})
                        reason = waiting.get("reason", "")
                        if reason in ("ImagePullBackOff", "ErrImagePull"):
                            failed_uds.append((namespace, name, f"Image pull failed: {waiting.get('message', '')[:50]}"))
                            break
                    else:
                        continue
                    break
                else:
                    pending_uds.append((namespace, name))

    if failed_uds:
        for ns, name, msg in failed_uds[:5]:
            steps.append(
                RemediationStep(
                    name=f"Restart failed UserDeployment {ns}/{name}",
                    description=f"Delete and recreate pods: {msg[:60]}",
                    command=[
                        "kubectl",
                        "delete",
                        "pods",
                        "-n",
                        ns,
                        "-l",
                        f"app={name}",
                        "--grace-period=30",
                    ],
                    impact=3,
                    reversible=False,
                )
            )

    return steps


def _check_missing_netpols(config: Config) -> list[RemediationStep]:
    """Check for tenant namespaces missing required NetworkPolicies."""
    steps: list[RemediationStep] = []

    result = run_kubectl(config, ["get", "namespaces", "-o", "json"])
    if result.returncode != 0:
        return steps

    data = parse_json_output(result.stdout)
    namespaces_missing_policies: list[tuple[str, list[str]]] = []

    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if not name.startswith("u-"):
            continue

        result = run_kubectl(
            config,
            ["get", "networkpolicy", "-n", name, "-o", "json"],
        )
        if result.returncode != 0:
            namespaces_missing_policies.append((name, REQUIRED_NETPOLS.copy()))
            continue

        policy_data = parse_json_output(result.stdout)
        existing = {p.get("metadata", {}).get("name", "") for p in policy_data.get("items", [])}
        missing = [p for p in REQUIRED_NETPOLS if p not in existing]

        if missing:
            namespaces_missing_policies.append((name, missing))

    if namespaces_missing_policies:
        namespaces_str = ", ".join(ns for ns, _ in namespaces_missing_policies[:3])
        if len(namespaces_missing_policies) > 3:
            namespaces_str += f" (+{len(namespaces_missing_policies) - 3} more)"

        steps.append(
            RemediationStep(
                name="Apply missing NetworkPolicies",
                description=f"Namespaces missing policies: {namespaces_str}",
                command=[
                    "clustermgr",
                    "netpol",
                    "audit",
                    "--fix",
                ],
                impact=2,
                reversible=True,
            )
        )

    return steps


def _check_orphaned_routes(config: Config) -> list[RemediationStep]:
    """Check for orphaned HTTPRoutes without matching UserDeployments."""
    steps: list[RemediationStep] = []

    result = run_kubectl(config, ["get", "httproutes", "-A", "-o", "json"])
    if result.returncode != 0:
        return steps

    routes_data = parse_json_output(result.stdout)
    route_map: dict[str, str] = {}
    for item in routes_data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        namespace = item.get("metadata", {}).get("namespace", "")
        if name.startswith("ud-"):
            ud_name = name[3:]
            route_map[f"{namespace}/{ud_name}"] = f"{namespace}/{name}"

    result = run_kubectl(config, ["get", "userdeployments", "-A", "-o", "json"])
    if result.returncode != 0:
        return steps

    ud_data = parse_json_output(result.stdout)
    ud_keys = set()
    for item in ud_data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        namespace = item.get("metadata", {}).get("namespace", "")
        ud_keys.add(f"{namespace}/{name}")

    orphaned = [route for key, route in route_map.items() if key not in ud_keys]

    if orphaned:
        for route in orphaned[:3]:
            ns, name = route.split("/")
            steps.append(
                RemediationStep(
                    name=f"Delete orphaned HTTPRoute {route}",
                    description="HTTPRoute has no matching UserDeployment",
                    command=[
                        "kubectl",
                        "delete",
                        "httproute",
                        name,
                        "-n",
                        ns,
                    ],
                    impact=2,
                    reversible=False,
                )
            )

    return steps


def _check_flannel_issues(config: Config) -> list[RemediationStep]:
    """Check for Flannel VXLAN issues that cause HTTP 503 errors.

    Inspects FDB entries, neighbor entries, and routes to ensure
    Flannel VXLAN can route traffic to GPU nodes.
    """
    steps: list[RemediationStep] = []

    # Import flannel functions
    try:
        from clustermgr.commands.flannel import (
            _check_gpu_node_flannel_via_ssh,
            _get_fdb_entries,
            _get_flannel_routes,
            _get_gpu_node_ssh_info,
            _get_gpu_nodes,
            _get_neighbor_entries,
        )
    except ImportError:
        return steps

    gpu_nodes = _get_gpu_nodes(config)
    if not gpu_nodes:
        return steps

    # Check for missing/DOWN flannel.1 on GPU nodes (root cause of HTTP 503)
    gpu_ssh_info = _get_gpu_node_ssh_info(config)
    for gpu in gpu_ssh_info:
        if not gpu.get("public_ip"):
            continue

        status = _check_gpu_node_flannel_via_ssh(
            gpu["public_ip"],
            gpu.get("ssh_user", "shadeform"),
        )

        if not status.get("reachable", False):
            continue

        if not status.get("exists", False) or status.get("state") != "UP":
            steps.append(
                RemediationStep(
                    name=f"Recover flannel.1 on {gpu['name'][:20]}",
                    description="GPU node flannel.1 is missing/DOWN - restart K3s and update routes",
                    command=[
                        "clustermgr",
                        "flannel",
                        "gpu-recover",
                        "--node",
                        gpu["name"],
                        "--restart-k3s",
                        "-y",
                    ],
                    impact=6,
                    reversible=False,
                )
            )

    # Check for missing FDB entries
    fdb_entries = _get_fdb_entries(config)
    all_fdb_macs: set[str] = set()
    for entries in fdb_entries.values():
        all_fdb_macs.update(e.mac for e in entries)

    missing_fdb: list[tuple[str, str, str]] = []
    for gpu in gpu_nodes:
        if gpu.flannel_mac and gpu.flannel_mac not in all_fdb_macs:
            missing_fdb.append((gpu.name, gpu.flannel_mac, gpu.wg_ip))

    if missing_fdb:
        for node_name, mac, wg_ip in missing_fdb[:3]:
            steps.append(
                RemediationStep(
                    name=f"Add FDB entry for {node_name[:20]}",
                    description=f"Add missing FDB: {mac} -> {wg_ip}",
                    command=[
                        "ansible",
                        "k3s_server",
                        "-m",
                        "shell",
                        "-a",
                        f"bridge fdb replace {mac} dev flannel.1 dst {wg_ip} self permanent",
                    ],
                    impact=4,
                    reversible=True,
                )
            )

    # Check for missing neighbor entries
    neighbor_entries = _get_neighbor_entries(config)
    all_vteps: set[str] = set()
    for entries in neighbor_entries.values():
        all_vteps.update(e.vtep_ip for e in entries)

    missing_neighbors: list[tuple[str, str, str]] = []
    for gpu in gpu_nodes:
        if gpu.pod_cidr and gpu.flannel_mac:
            vtep_ip = gpu.pod_cidr.replace("/24", "").rsplit(".", 1)[0] + ".0"
            if vtep_ip not in all_vteps:
                missing_neighbors.append((gpu.name, vtep_ip, gpu.flannel_mac))

    if missing_neighbors:
        for node_name, vtep_ip, mac in missing_neighbors[:3]:
            steps.append(
                RemediationStep(
                    name=f"Add neighbor entry for {node_name[:20]}",
                    description=f"Add missing neighbor: {vtep_ip} -> {mac}",
                    command=[
                        "ansible",
                        "k3s_server",
                        "-m",
                        "shell",
                        "-a",
                        f"ip neigh replace {vtep_ip} lladdr {mac} dev flannel.1 nud permanent",
                    ],
                    impact=4,
                    reversible=True,
                )
            )

    # Check for missing routes
    routes = _get_flannel_routes(config)
    all_routed_cidrs: set[str] = set()
    for node_routes in routes.values():
        all_routed_cidrs.update(r.pod_cidr for r in node_routes)

    missing_routes: list[tuple[str, str]] = []
    for gpu in gpu_nodes:
        if gpu.pod_cidr and gpu.pod_cidr not in all_routed_cidrs:
            vtep_ip = gpu.pod_cidr.replace("/24", "").rsplit(".", 1)[0] + ".0"
            missing_routes.append((gpu.pod_cidr, vtep_ip))

    if missing_routes:
        for pod_cidr, vtep_ip in missing_routes[:3]:
            steps.append(
                RemediationStep(
                    name=f"Add Flannel route for {pod_cidr}",
                    description=f"Add missing route: {pod_cidr} via {vtep_ip}",
                    command=[
                        "ansible",
                        "k3s_server",
                        "-m",
                        "shell",
                        "-a",
                        f"ip route replace {pod_cidr} via {vtep_ip} dev flannel.1 onlink",
                    ],
                    impact=4,
                    reversible=True,
                )
            )

    return steps


@click.command()
@click.pass_context
def fix(ctx: click.Context) -> None:
    """Auto-fix common cluster issues with safety guards."""
    config: Config = ctx.obj

    print_header("Cluster Remediation")
    console.print(f"Timestamp: {datetime.now().isoformat()}")

    # Create remediation plan
    console.print("\nAnalyzing cluster state...")
    steps = plan_remediation(config)

    if not steps:
        console.print("\n[green]No issues found that require remediation.[/green]")
        return

    # Show plan
    print_header("Remediation Plan")
    total_impact = sum(s.impact for s in steps)

    for i, step in enumerate(steps, 1):
        reversible = (
            "[green]reversible[/green]" if step.reversible else "[red]NOT reversible[/red]"
        )
        console.print(f"\n  {i}. [bold]{step.name}[/bold]")
        console.print(f"     {step.description}")
        console.print(f"     Impact: {step.impact}/10, {reversible}")

    console.print(f"\n  Total impact score: {total_impact}")

    if config.dry_run:
        console.print("\n[yellow][DRY RUN] No changes will be made.[/yellow]")
        console.print("Remove --dry-run flag to execute remediation.")
        return

    # Confirm execution
    if not config.no_confirm:
        if not confirm(f"Execute {len(steps)} remediation step(s)?"):
            console.print("Aborted.")
            return

    # Execute steps
    print_header("Executing Remediation")

    for i, step in enumerate(steps, 1):
        console.print(f"\n[{i}/{len(steps)}] {step.name}...")

        # Build command with inventory
        cmd = step.command.copy()
        if cmd[0] == "ansible":
            cmd.insert(2, "-i")
            cmd.insert(3, str(config.inventory))

        env = os.environ.copy()
        env["KUBECONFIG"] = config.kubeconfig

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode == 0:
                console.print("  [green]Success[/green]")
                if config.verbose:
                    console.print(result.stdout[:500])
            else:
                console.print(f"  [red]Failed: {result.stderr[:200]}[/red]")
        except subprocess.TimeoutExpired:
            console.print("  [red]Timeout[/red]")
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    # Final health check
    print_header("Post-Remediation Health Check")
    ctx.invoke(health)
