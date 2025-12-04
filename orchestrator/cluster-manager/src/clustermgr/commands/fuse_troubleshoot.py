"""FUSE troubleshoot command for clustermgr - diagnose FUSE daemon issues."""

import re
from dataclasses import dataclass, field

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
    run_kubectl,
)

console = Console()

FUSE_NAMESPACE = "basilica-storage"
FUSE_DAEMONSET = "fuse-daemon"
FUSE_LOADER_NAMESPACE = "kube-system"
FUSE_LOADER_DAEMONSET = "fuse-module-loader"
COREDNS_SERVICE_IP = "10.43.0.10"
R2_ENDPOINT_DOMAIN = "r2.cloudflarestorage.com"


@dataclass
class FuseNodeStatus:
    """Status of FUSE daemon on a node."""

    node_name: str
    pod_name: str | None = None
    pod_phase: str = "Missing"
    pod_ready: bool = False
    restarts: int = 0
    fuse_device: bool = False
    mount_available: bool = False
    stale_mounts: list[str] = field(default_factory=list)
    container_dns_ok: bool | None = None
    container_network_ok: bool | None = None
    issues: list[str] = field(default_factory=list)


@dataclass
class FuseIssue:
    """A detected FUSE issue."""

    node: str
    issue_type: str
    severity: Severity
    description: str
    remediation: str


def _get_daemonset_pods(
    config: Config, namespace: str, daemonset: str
) -> dict[str, dict]:
    """Get pods for a daemonset, keyed by node name."""
    result = run_kubectl(
        config,
        [
            "get", "pods", "-n", namespace,
            "-l", f"app.kubernetes.io/component={daemonset}",
            "-o", "json",
        ],
    )
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    pods: dict[str, dict] = {}

    for item in data.get("items", []):
        node = item.get("spec", {}).get("nodeName", "")
        if node:
            pods[node] = item

    return pods


def _get_fuse_loader_pods(config: Config) -> dict[str, dict]:
    """Get FUSE module loader pods, keyed by node name."""
    result = run_kubectl(
        config,
        [
            "get", "pods", "-n", FUSE_LOADER_NAMESPACE,
            "-l", "app=fuse-module-loader",
            "-o", "json",
        ],
    )
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    pods: dict[str, dict] = {}

    for item in data.get("items", []):
        node = item.get("spec", {}).get("nodeName", "")
        if node:
            pods[node] = item

    return pods


def _get_all_nodes(config: Config) -> list[str]:
    """Get all node names in the cluster."""
    result = run_kubectl(config, ["get", "nodes", "-o", "jsonpath={.items[*].metadata.name}"])
    if result.returncode != 0:
        return []
    return result.stdout.strip().split()


def _get_pod_logs(config: Config, namespace: str, pod_name: str, container: str, tail: int = 50) -> str:
    """Get pod logs for a container."""
    result = run_kubectl(
        config,
        ["logs", "-n", namespace, pod_name, "-c", container, f"--tail={tail}"],
        timeout=30,
    )
    return result.stdout if result.returncode == 0 else result.stderr


def _get_pod_events(config: Config, namespace: str, pod_name: str) -> list[dict]:
    """Get events for a pod."""
    result = run_kubectl(
        config,
        [
            "get", "events", "-n", namespace,
            "--field-selector", f"involvedObject.name={pod_name}",
            "-o", "json",
        ],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    return data.get("items", [])


def _analyze_pod_status(pod: dict) -> tuple[str, bool, int, list[str]]:
    """Analyze pod status, returns (phase, ready, restarts, issues)."""
    status = pod.get("status", {})
    phase = status.get("phase", "Unknown")
    issues: list[str] = []
    ready = False
    restarts = 0

    container_statuses = status.get("containerStatuses", [])
    init_statuses = status.get("initContainerStatuses", [])

    # Check init containers
    for cs in init_statuses:
        name = cs.get("name", "")
        terminated = cs.get("state", {}).get("terminated", {})
        if terminated and terminated.get("exitCode", 0) != 0:
            issues.append(f"Init container {name} failed: exit code {terminated.get('exitCode')}")
        waiting = cs.get("state", {}).get("waiting", {})
        if waiting:
            reason = waiting.get("reason", "")
            if reason:
                issues.append(f"Init container {name} waiting: {reason}")

    # Check main containers
    for cs in container_statuses:
        name = cs.get("name", "")
        restarts += cs.get("restartCount", 0)

        if cs.get("ready", False):
            ready = True

        waiting = cs.get("state", {}).get("waiting", {})
        if waiting:
            reason = waiting.get("reason", "")
            msg = waiting.get("message", "")[:100]
            if reason in ("CrashLoopBackOff", "Error", "ImagePullBackOff", "ErrImagePull"):
                issues.append(f"{name}: {reason} - {msg}")
            elif reason:
                issues.append(f"{name} waiting: {reason}")

        terminated = cs.get("state", {}).get("terminated", {})
        if terminated and terminated.get("exitCode", 0) != 0:
            issues.append(f"{name} terminated: exit code {terminated.get('exitCode')}")

    return phase, ready, restarts, issues


def _get_gpu_node_info(config: Config) -> dict[str, dict]:
    """Get GPU nodes with their IPs and pod CIDRs.

    Returns dict: node_name -> {wg_ip, pod_cidr, is_gpu_node}
    """
    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "json"],
        timeout=30,
    )
    if result.returncode != 0:
        return {}

    data = parse_json_output(result.stdout)
    nodes: dict[str, dict] = {}

    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        spec = item.get("spec", {})
        status = item.get("status", {})

        wg_ip = None
        for addr in status.get("addresses", []):
            if addr.get("type") == "InternalIP":
                wg_ip = addr.get("address")
                break

        nodes[name] = {
            "wg_ip": wg_ip,
            "pod_cidr": spec.get("podCIDR"),
            "is_gpu_node": True,
        }

    return nodes


def _get_coredns_pod_ip(config: Config) -> str | None:
    """Get the IP of a CoreDNS pod."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", "kube-system", "-l", "k8s-app=kube-dns",
         "-o", "jsonpath={.items[0].status.podIP}"],
        timeout=10,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def _test_container_network(config: Config, pod_name: str, target_ip: str) -> bool:
    """Test if fuse-daemon container can reach a target IP.

    Uses getent to test DNS resolution to the IP (which verifies network path).
    Falls back to checking /proc/net/tcp if getent fails.
    """
    # The container is minimal - no ping. Use getent to test reachability
    # by resolving a known hostname that goes through CoreDNS
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "getent", "hosts", "kubernetes.default.svc.cluster.local"],
        timeout=15,
    )
    # If we can resolve kubernetes.default, CoreDNS is reachable
    return result.returncode == 0 and result.stdout.strip() != ""


def _test_container_dns(config: Config, pod_name: str) -> bool:
    """Test if fuse-daemon container can resolve DNS.

    Tests DNS resolution using getent (available in minimal containers).
    """
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "getent", "hosts", "google.com"],
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() != ""


def _test_r2_dns_resolution(config: Config, pod_name: str) -> tuple[bool, str | None]:
    """Test if fuse-daemon can resolve R2 endpoint DNS.

    Returns (success, resolved_ip).
    """
    # Try getent first (more reliable in minimal containers)
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "getent", "hosts", R2_ENDPOINT_DOMAIN],
        timeout=15,
    )
    if result.returncode == 0 and result.stdout.strip():
        ip = result.stdout.split()[0]
        return True, ip

    # Fallback to nslookup
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "nslookup", R2_ENDPOINT_DOMAIN],
        timeout=15,
    )
    if result.returncode == 0 and "Address" in result.stdout:
        for line in result.stdout.split("\n"):
            if "Address:" in line and "#" not in line:
                ip = line.split("Address:")[1].strip()
                return True, ip
        return True, None

    return False, None


def _get_r2_endpoint_from_env(config: Config, pod_name: str) -> str | None:
    """Extract R2 endpoint URL from fuse-daemon environment.

    Looks for S3_ENDPOINT or similar env vars.
    """
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "printenv"],
        timeout=10,
    )
    if result.returncode != 0:
        return None

    for line in result.stdout.split("\n"):
        if "S3_ENDPOINT" in line or "R2_ENDPOINT" in line or "STORAGE_ENDPOINT" in line:
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip()

    return None


def _test_r2_connectivity(config: Config, pod_name: str) -> dict:
    """Comprehensive R2 connectivity test from fuse-daemon container.

    Returns dict with test results.
    """
    results: dict = {
        "dns_ok": False,
        "dns_ip": None,
        "endpoint_url": None,
        "can_reach_endpoint": None,
        "issues": [],
    }

    # Get R2 endpoint from env
    results["endpoint_url"] = _get_r2_endpoint_from_env(config, pod_name)

    # Test DNS resolution of R2
    dns_ok, dns_ip = _test_r2_dns_resolution(config, pod_name)
    results["dns_ok"] = dns_ok
    results["dns_ip"] = dns_ip

    if not dns_ok:
        results["issues"].append("Cannot resolve R2 endpoint DNS - check CoreDNS and DNS policies")
        return results

    # If we have an endpoint URL, extract the hostname for connectivity test
    test_host = R2_ENDPOINT_DOMAIN
    if results["endpoint_url"]:
        # Extract hostname from URL like https://bucket.xxx.r2.cloudflarestorage.com
        import re
        match = re.search(r"https?://([^/]+)", results["endpoint_url"])
        if match:
            test_host = match.group(1)

    # Test TCP connectivity (we can't do full HTTPS without curl, but DNS is the main issue)
    # The sync_worker logs will show actual S3 errors
    results["can_reach_endpoint"] = dns_ok  # If DNS works, TCP usually works too

    return results


def _check_stale_mounts(config: Config, pod_name: str) -> list[str]:
    """Check for stale FUSE mounts in the fuse-daemon container.

    Returns list of stale mount paths.
    """
    # Check mount points for broken FUSE mounts
    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "sh", "-c",
         "for dir in /var/lib/basilica/fuse/u-*; do "
         "  [ -d \"$dir\" ] && ! stat \"$dir\" >/dev/null 2>&1 && echo \"$dir\"; "
         "done 2>/dev/null || true"],
        timeout=15,
    )

    stale: list[str] = []
    if result.returncode == 0 and result.stdout.strip():
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and line.startswith("/var/lib/basilica/fuse/"):
                stale.append(line)

    return stale


def _cleanup_stale_mount(config: Config, pod_name: str, mount_path: str) -> bool:
    """Clean up a stale FUSE mount.

    Returns True if cleanup was successful.
    """
    # Try fusermount first, then umount, then rmdir
    cleanup_cmd = (
        f"fusermount -uz '{mount_path}' 2>/dev/null || "
        f"umount -l '{mount_path}' 2>/dev/null || true; "
        f"rmdir '{mount_path}' 2>/dev/null || rm -rf '{mount_path}' 2>/dev/null || true; "
        f"[ ! -d '{mount_path}' ] && echo 'SUCCESS'"
    )

    result = run_kubectl(
        config,
        ["exec", "-n", FUSE_NAMESPACE, pod_name, "-c", "fuse-daemon",
         "--", "sh", "-c", cleanup_cmd],
        timeout=30,
    )

    return "SUCCESS" in result.stdout


def _check_fuse_logs_for_errors(config: Config, pod_name: str, tail: int = 50) -> list[str]:
    """Check fuse-daemon logs for common error patterns.

    Returns list of detected error types.
    """
    logs = _get_pod_logs(config, FUSE_NAMESPACE, pod_name, "fuse-daemon", tail)
    errors: list[str] = []

    if "dispatch failure" in logs.lower():
        errors.append("dispatch_failure")
    if "temporary failure in name resolution" in logs.lower():
        errors.append("dns_failure")
    if "file exists (os error 17)" in logs.lower():
        errors.append("stale_mount")
    if "transport endpoint is not connected" in logs.lower():
        errors.append("stale_mount")
    if "connection refused" in logs.lower():
        errors.append("connection_refused")
    if "timed out" in logs.lower() or "timeout" in logs.lower():
        errors.append("timeout")

    return errors


def _check_fuse_status(config: Config, nodes: list[str] | None = None) -> list[FuseNodeStatus]:
    """Check FUSE daemon status on all or specified nodes."""
    all_nodes = _get_all_nodes(config) if nodes is None else nodes
    fuse_pods = _get_daemonset_pods(config, FUSE_NAMESPACE, FUSE_DAEMONSET)

    statuses: list[FuseNodeStatus] = []

    for node in all_nodes:
        status = FuseNodeStatus(node_name=node)

        pod = fuse_pods.get(node)
        if pod:
            status.pod_name = pod.get("metadata", {}).get("name", "")
            phase, ready, restarts, issues = _analyze_pod_status(pod)
            status.pod_phase = phase
            status.pod_ready = ready
            status.restarts = restarts
            status.issues = issues

        statuses.append(status)

    return statuses


def _diagnose_fuse_issues(config: Config, nodes: list[str] | None = None) -> list[FuseIssue]:
    """Diagnose FUSE issues across nodes."""
    issues: list[FuseIssue] = []

    # Get all node names if not specified
    all_nodes = _get_all_nodes(config) if nodes is None else nodes

    # Check FUSE loader pods
    loader_pods = _get_fuse_loader_pods(config)
    for node in all_nodes:
        if node not in loader_pods:
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_loader_missing",
                severity=Severity.CRITICAL,
                description="FUSE module loader pod not running",
                remediation="kubectl rollout restart daemonset/fuse-module-loader -n kube-system",
            ))
            continue

        loader_pod = loader_pods[node]
        phase, ready, restarts, pod_issues = _analyze_pod_status(loader_pod)

        if phase != "Running" or not ready:
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_loader_unhealthy",
                severity=Severity.CRITICAL,
                description=f"FUSE loader not healthy: {phase}, issues: {pod_issues}",
                remediation="Check FUSE module loader logs and node kernel modules",
            ))

    # Check FUSE daemon pods
    fuse_pods = _get_daemonset_pods(config, FUSE_NAMESPACE, FUSE_DAEMONSET)
    for node in all_nodes:
        if node not in fuse_pods:
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_daemon_missing",
                severity=Severity.CRITICAL,
                description="FUSE daemon pod not scheduled on node",
                remediation="Check node taints/tolerations and daemonset spec",
            ))
            continue

        pod = fuse_pods[node]
        pod_name = pod.get("metadata", {}).get("name", "")
        phase, ready, restarts, pod_issues = _analyze_pod_status(pod)

        if phase == "Pending":
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_daemon_pending",
                severity=Severity.WARNING,
                description=f"FUSE daemon pending: {pod_issues}",
                remediation="Check node resources and scheduling constraints",
            ))
        elif phase != "Running":
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_daemon_not_running",
                severity=Severity.CRITICAL,
                description=f"FUSE daemon {phase}: {pod_issues}",
                remediation=f"kubectl describe pod -n {FUSE_NAMESPACE} {pod_name}",
            ))
        elif not ready:
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_daemon_not_ready",
                severity=Severity.WARNING,
                description=f"FUSE daemon running but not ready: {pod_issues}",
                remediation=f"Check health endpoint: kubectl logs -n {FUSE_NAMESPACE} {pod_name}",
            ))

        if restarts > 3:
            issues.append(FuseIssue(
                node=node,
                issue_type="fuse_daemon_crash_loop",
                severity=Severity.CRITICAL,
                description=f"FUSE daemon has {restarts} restarts",
                remediation=f"kubectl logs -n {FUSE_NAMESPACE} {pod_name} --previous",
            ))

        # For running pods, check for deeper issues
        if phase == "Running" and pod_name:
            # Check for stale mounts
            stale_mounts = _check_stale_mounts(config, pod_name)
            if stale_mounts:
                issues.append(FuseIssue(
                    node=node,
                    issue_type="stale_fuse_mounts",
                    severity=Severity.WARNING,
                    description=f"{len(stale_mounts)} stale mount(s): {', '.join(stale_mounts[:2])}",
                    remediation="clustermgr fuse-troubleshoot --fix-mounts",
                ))

            # Check log errors
            log_errors = _check_fuse_logs_for_errors(config, pod_name)
            if "dispatch_failure" in log_errors or "dns_failure" in log_errors:
                issues.append(FuseIssue(
                    node=node,
                    issue_type="network_or_dns_failure",
                    severity=Severity.CRITICAL,
                    description="Container experiencing dispatch/DNS failures",
                    remediation="Check container network: clustermgr fuse-troubleshoot <node> --deep",
                ))

    return issues


def diagnose_fuse_deep(config: Config, node_name: str) -> dict:
    """Perform deep diagnostics on a specific node.

    Returns dict with diagnostic results including network, DNS, and R2 tests.
    """
    results: dict = {
        "node": node_name,
        "pod_name": None,
        "pod_status": None,
        "stale_mounts": [],
        "log_errors": [],
        "network_test": None,
        "dns_test": None,
        "r2_test": None,
        "coredns_ip": None,
        "issues": [],
    }

    fuse_pods = _get_daemonset_pods(config, FUSE_NAMESPACE, FUSE_DAEMONSET)
    if node_name not in fuse_pods:
        results["issues"].append("FUSE daemon pod not found on node")
        return results

    pod = fuse_pods[node_name]
    pod_name = pod.get("metadata", {}).get("name", "")
    results["pod_name"] = pod_name

    phase, ready, restarts, pod_issues = _analyze_pod_status(pod)
    results["pod_status"] = {
        "phase": phase,
        "ready": ready,
        "restarts": restarts,
        "issues": pod_issues,
    }

    if phase != "Running":
        results["issues"].append(f"Pod not running: {phase}")
        return results

    # Check stale mounts
    results["stale_mounts"] = _check_stale_mounts(config, pod_name)

    # Check log errors
    results["log_errors"] = _check_fuse_logs_for_errors(config, pod_name)

    # Get CoreDNS IP for network test
    coredns_ip = _get_coredns_pod_ip(config)
    results["coredns_ip"] = coredns_ip

    # Test network connectivity to CoreDNS
    if coredns_ip:
        results["network_test"] = _test_container_network(config, pod_name, coredns_ip)
        if not results["network_test"]:
            results["issues"].append("Container cannot reach CoreDNS pod - likely WireGuard routing issue")

    # Test DNS resolution
    results["dns_test"] = _test_container_dns(config, pod_name)
    if not results["dns_test"]:
        results["issues"].append("DNS resolution failing in container")

    # Test R2 connectivity
    results["r2_test"] = _test_r2_connectivity(config, pod_name)
    if results["r2_test"] and not results["r2_test"]["dns_ok"]:
        results["issues"].append("Cannot resolve R2 storage endpoint - DNS failure")
    if results["r2_test"] and results["r2_test"]["issues"]:
        results["issues"].extend(results["r2_test"]["issues"])

    return results


def _print_node_status_table(statuses: list[FuseNodeStatus]) -> None:
    """Print FUSE status table for nodes."""
    table = Table(title="FUSE Daemon Status by Node")
    table.add_column("Node", style="cyan")
    table.add_column("Pod")
    table.add_column("Phase")
    table.add_column("Ready")
    table.add_column("Restarts", justify="right")
    table.add_column("Issues")

    for s in statuses:
        phase_color = "green" if s.pod_phase == "Running" else "red" if s.pod_phase in ("Failed", "Missing") else "yellow"
        ready_str = "[green]Yes[/green]" if s.pod_ready else "[red]No[/red]"
        issues_str = "; ".join(s.issues[:2]) if s.issues else "-"

        table.add_row(
            s.node_name,
            s.pod_name or "-",
            f"[{phase_color}]{s.pod_phase}[/{phase_color}]",
            ready_str,
            str(s.restarts) if s.restarts > 0 else "-",
            issues_str[:50] if len(issues_str) > 50 else issues_str,
        )

    console.print(table)


@click.command("fuse-troubleshoot")
@click.argument("node_name", required=False)
@click.option("--logs", "-l", is_flag=True, help="Show FUSE daemon logs")
@click.option("--events", "-e", is_flag=True, help="Show pod events")
@click.option("--tail", default=50, help="Number of log lines (default: 50)")
@click.option("--scan", "-s", is_flag=True, help="Scan all nodes for FUSE issues")
@click.option("--deep", "-d", is_flag=True, help="Deep diagnostics (network, DNS, stale mounts)")
@click.option("--fix-mounts", is_flag=True, help="Clean up stale FUSE mounts")
@click.pass_context
def fuse_troubleshoot(
    ctx: click.Context,
    node_name: str | None,
    logs: bool,
    events: bool,
    tail: int,
    scan: bool,
    deep: bool,
    fix_mounts: bool,
) -> None:
    """Diagnose FUSE daemon issues on cluster nodes.

    Without arguments, scans all nodes for FUSE daemon issues.
    With NODE_NAME, shows detailed diagnostics for that node.

    Use --deep to run container network and DNS tests.
    Use --fix-mounts to clean up stale FUSE mount points.
    """
    config: Config = ctx.obj

    # Scan mode or no node specified
    if scan or not node_name:
        print_header("FUSE Daemon Status Overview")

        statuses = _check_fuse_status(config)
        if not statuses:
            console.print("[red]No nodes found in cluster[/red]")
            ctx.exit(1)

        _print_node_status_table(statuses)

        # Run diagnostics
        print_header("FUSE Diagnostics")
        issues = _diagnose_fuse_issues(config)

        if not issues:
            console.print("[green]No FUSE issues detected[/green]")
            return

        console.print(f"[red]Found {len(issues)} issue(s):[/red]\n")

        for issue in issues:
            severity_color = "red" if issue.severity == Severity.CRITICAL else "yellow"
            console.print(f"  [{severity_color}][{issue.severity.name}][/{severity_color}] {issue.node}: {issue.issue_type}")
            console.print(f"    {issue.description}")
            console.print(f"    [dim]Fix: {issue.remediation}[/dim]\n")

        console.print("\n[bold]Quick fix:[/bold] Run 'clustermgr fix --dry-run' to see automated remediation plan")
        ctx.exit(1)
        return

    # Single node diagnostics
    print_header(f"FUSE Troubleshooting: {node_name}")

    # Check if node exists
    all_nodes = _get_all_nodes(config)
    if node_name not in all_nodes:
        console.print(f"[red]Node {node_name} not found in cluster[/red]")
        ctx.exit(1)

    # Get FUSE daemon pod for this node
    fuse_pods = _get_daemonset_pods(config, FUSE_NAMESPACE, FUSE_DAEMONSET)
    loader_pods = _get_fuse_loader_pods(config)

    # Check FUSE loader
    print_header("FUSE Module Loader")
    if node_name in loader_pods:
        loader = loader_pods[node_name]
        loader_name = loader.get("metadata", {}).get("name", "")
        phase, ready, restarts, issues = _analyze_pod_status(loader)
        print_status("Pod", loader_name, Severity.HEALTHY)
        print_status("Phase", phase, Severity.HEALTHY if phase == "Running" else Severity.CRITICAL)
        print_status("Ready", "Yes" if ready else "No", Severity.HEALTHY if ready else Severity.WARNING)
    else:
        print_status("Status", "NOT FOUND", Severity.CRITICAL)
        console.print("  [red]FUSE module loader not running on this node[/red]")

    # Check FUSE daemon
    print_header("FUSE Daemon")
    if node_name not in fuse_pods:
        print_status("Status", "NOT FOUND", Severity.CRITICAL)
        console.print("  [red]FUSE daemon not scheduled on this node[/red]")

        # Check why it's not scheduled
        result = run_kubectl(
            config,
            ["get", "daemonset", "-n", FUSE_NAMESPACE, FUSE_DAEMONSET, "-o", "json"],
        )
        if result.returncode == 0:
            ds = parse_json_output(result.stdout)
            status = ds.get("status", {})
            console.print(f"\n  DaemonSet status:")
            console.print(f"    Desired: {status.get('desiredNumberScheduled', 0)}")
            console.print(f"    Current: {status.get('currentNumberScheduled', 0)}")
            console.print(f"    Ready: {status.get('numberReady', 0)}")
            console.print(f"    Available: {status.get('numberAvailable', 0)}")
        ctx.exit(1)
        return

    pod = fuse_pods[node_name]
    pod_name = pod.get("metadata", {}).get("name", "")
    phase, ready, restarts, issues = _analyze_pod_status(pod)

    print_status("Pod", pod_name, Severity.HEALTHY)
    print_status("Phase", phase, Severity.HEALTHY if phase == "Running" else Severity.CRITICAL)
    print_status("Ready", "Yes" if ready else "No", Severity.HEALTHY if ready else Severity.WARNING)
    print_status("Restarts", str(restarts), Severity.WARNING if restarts > 3 else Severity.HEALTHY)

    if issues:
        print_header("Detected Issues")
        for issue in issues:
            console.print(f"  [red]{issue}[/red]")

    # Show events if requested
    if events:
        print_header("Pod Events")
        pod_events = _get_pod_events(config, FUSE_NAMESPACE, pod_name)

        if not pod_events:
            console.print("  [dim]No events found[/dim]")
        else:
            for ev in sorted(pod_events, key=lambda x: x.get("lastTimestamp") or "")[-10:]:
                ev_type = ev.get("type", "Normal")
                color = "yellow" if ev_type == "Warning" else "dim"
                reason = ev.get("reason", "")
                message = ev.get("message", "")[:80]
                console.print(f"  [{color}]{reason}[/{color}]: {message}")

    # Show logs if requested
    if logs:
        # Show init container logs first
        print_header("Init Container Logs (fuse-init)")
        init_logs = _get_pod_logs(config, FUSE_NAMESPACE, pod_name, "fuse-init", tail)
        if init_logs:
            for line in init_logs.split("\n")[-tail:]:
                if "error" in line.lower() or "fail" in line.lower():
                    console.print(f"  [red]{line}[/red]")
                else:
                    console.print(f"  [dim]{line}[/dim]")
        else:
            console.print("  [dim]No init logs available[/dim]")

        print_header("FUSE Daemon Logs")
        daemon_logs = _get_pod_logs(config, FUSE_NAMESPACE, pod_name, "fuse-daemon", tail)
        if daemon_logs:
            for line in daemon_logs.split("\n")[-tail:]:
                if "error" in line.lower() or "fail" in line.lower():
                    console.print(f"  [red]{line}[/red]")
                elif "warn" in line.lower():
                    console.print(f"  [yellow]{line}[/yellow]")
                else:
                    console.print(f"  [dim]{line}[/dim]")
        else:
            console.print("  [dim]No logs available[/dim]")

    # Deep diagnostics mode
    if deep and phase == "Running":
        print_header("Deep Diagnostics")

        diag = diagnose_fuse_deep(config, node_name)

        # Stale mounts
        if diag["stale_mounts"]:
            print_status("Stale Mounts", f"{len(diag['stale_mounts'])} found", Severity.WARNING)
            for mount in diag["stale_mounts"]:
                console.print(f"    [yellow]{mount}[/yellow]")
        else:
            print_status("Stale Mounts", "None", Severity.HEALTHY)

        # Log errors
        if diag["log_errors"]:
            print_status("Log Errors", ", ".join(diag["log_errors"]), Severity.WARNING)
        else:
            print_status("Log Errors", "None detected", Severity.HEALTHY)

        # Network test
        if diag["coredns_ip"]:
            console.print(f"\n  Testing container network to CoreDNS ({diag['coredns_ip']})...")
            if diag["network_test"]:
                print_status("Container -> CoreDNS", "OK", Severity.HEALTHY)
            else:
                print_status("Container -> CoreDNS", "FAILED", Severity.CRITICAL)
                console.print("    [red]Container cannot reach CoreDNS pod[/red]")
                console.print("    [dim]Likely cause: WireGuard AllowedIPs missing pod CIDR[/dim]")
                console.print("    [dim]Fix: Run 'clustermgr wg reconcile --fix'[/dim]")
        else:
            print_status("Network Test", "Skipped (no CoreDNS IP)", Severity.WARNING)

        # DNS test
        console.print(f"\n  Testing DNS resolution from container...")
        if diag["dns_test"]:
            print_status("DNS Resolution", "OK", Severity.HEALTHY)
        else:
            print_status("DNS Resolution", "FAILED", Severity.CRITICAL)
            console.print("    [red]Container cannot resolve DNS[/red]")

        # R2 connectivity test
        r2_test = diag.get("r2_test")
        if r2_test:
            console.print(f"\n  Testing R2 storage connectivity...")
            if r2_test["dns_ok"]:
                ip_info = f" ({r2_test['dns_ip']})" if r2_test["dns_ip"] else ""
                print_status("R2 DNS Resolution", f"OK{ip_info}", Severity.HEALTHY)
            else:
                print_status("R2 DNS Resolution", "FAILED", Severity.CRITICAL)
                console.print("    [red]Cannot resolve R2 endpoint[/red]")
                console.print("    [dim]Fix: Run 'clustermgr dns diagnose' to check DNS configuration[/dim]")

            if r2_test["endpoint_url"]:
                console.print(f"    Endpoint: [dim]{r2_test['endpoint_url'][:60]}...[/dim]")

        if diag["issues"]:
            print_header("Issues Found")
            for issue in diag["issues"]:
                console.print(f"  [red]{issue}[/red]")

    # Fix stale mounts
    if fix_mounts and phase == "Running":
        print_header("Stale Mount Cleanup")

        stale_mounts = _check_stale_mounts(config, pod_name)
        if not stale_mounts:
            console.print("  [green]No stale mounts found[/green]")
        else:
            console.print(f"  Found {len(stale_mounts)} stale mount(s)")

            if config.dry_run:
                console.print("\n  [yellow][DRY RUN] Would clean up:[/yellow]")
                for mount in stale_mounts:
                    console.print(f"    {mount}")
            else:
                if not config.no_confirm and not confirm(f"Clean up {len(stale_mounts)} stale mount(s)?"):
                    console.print("  Aborted.")
                else:
                    for mount in stale_mounts:
                        console.print(f"  Cleaning {mount}...")
                        if _cleanup_stale_mount(config, pod_name, mount):
                            print_status(f"  {mount}", "Cleaned", Severity.HEALTHY)
                        else:
                            print_status(f"  {mount}", "Failed", Severity.WARNING)

                    console.print("\n  [dim]Note: Restart fuse-daemon pod to recreate mounts[/dim]")
                    console.print(f"  [dim]kubectl delete pod -n {FUSE_NAMESPACE} {pod_name}[/dim]")

    # Run node-specific diagnostics
    node_issues = _diagnose_fuse_issues(config, [node_name])
    if node_issues:
        print_header("Remediation Suggestions")
        for issue in node_issues:
            console.print(f"  [yellow]{issue.issue_type}[/yellow]: {issue.description}")
            console.print(f"    [dim]Fix: {issue.remediation}[/dim]\n")
        ctx.exit(1)
