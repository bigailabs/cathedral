"""Audit-pods command for clustermgr - security audit of pod configurations."""

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl, Severity, print_status

console = Console()

# Security checks and their severity
SECURITY_CHECKS = {
    "privileged": {
        "description": "Container runs in privileged mode",
        "severity": Severity.CRITICAL,
        "risk": "Full host access, can escape container",
    },
    "host_network": {
        "description": "Pod uses host network",
        "severity": Severity.WARNING,
        "risk": "Can access all host network interfaces",
    },
    "host_pid": {
        "description": "Pod shares host PID namespace",
        "severity": Severity.WARNING,
        "risk": "Can see and signal host processes",
    },
    "host_ipc": {
        "description": "Pod shares host IPC namespace",
        "severity": Severity.WARNING,
        "risk": "Can access host IPC resources",
    },
    "run_as_root": {
        "description": "Container runs as root",
        "severity": Severity.WARNING,
        "risk": "Higher impact if container is compromised",
    },
    "no_read_only_root": {
        "description": "Root filesystem is writable",
        "severity": Severity.WARNING,
        "risk": "Attacker can modify container files",
    },
    "capabilities_added": {
        "description": "Extra capabilities added",
        "severity": Severity.WARNING,
        "risk": "Container has elevated privileges",
    },
    "no_resource_limits": {
        "description": "No resource limits set",
        "severity": Severity.WARNING,
        "risk": "Container can consume unbounded resources",
    },
    "host_path": {
        "description": "Mounts host path",
        "severity": Severity.WARNING,
        "risk": "Can access host filesystem",
    },
    "docker_socket": {
        "description": "Mounts Docker socket",
        "severity": Severity.CRITICAL,
        "risk": "Full control over Docker daemon",
    },
}


def _audit_pod(pod: dict) -> list[dict]:
    """Audit a single pod for security issues."""
    issues: list[dict] = []
    spec = pod.get("spec", {})

    # Check host namespaces
    if spec.get("hostNetwork"):
        issues.append({"check": "host_network", **SECURITY_CHECKS["host_network"]})
    if spec.get("hostPID"):
        issues.append({"check": "host_pid", **SECURITY_CHECKS["host_pid"]})
    if spec.get("hostIPC"):
        issues.append({"check": "host_ipc", **SECURITY_CHECKS["host_ipc"]})

    # Check volumes
    for vol in spec.get("volumes", []):
        if "hostPath" in vol:
            path = vol["hostPath"].get("path", "")
            if path == "/var/run/docker.sock":
                issues.append({"check": "docker_socket", **SECURITY_CHECKS["docker_socket"], "path": path})
            else:
                issues.append({"check": "host_path", **SECURITY_CHECKS["host_path"], "path": path})

    # Check containers
    for container in spec.get("containers", []) + spec.get("initContainers", []):
        container_name = container.get("name", "")
        security_context = container.get("securityContext", {})

        # Privileged mode
        if security_context.get("privileged"):
            issues.append({
                "check": "privileged",
                **SECURITY_CHECKS["privileged"],
                "container": container_name,
            })

        # Run as root
        run_as_user = security_context.get("runAsUser")
        run_as_non_root = security_context.get("runAsNonRoot")
        if run_as_user == 0 or (run_as_user is None and not run_as_non_root):
            issues.append({
                "check": "run_as_root",
                **SECURITY_CHECKS["run_as_root"],
                "container": container_name,
            })

        # Read-only root filesystem
        if not security_context.get("readOnlyRootFilesystem"):
            issues.append({
                "check": "no_read_only_root",
                **SECURITY_CHECKS["no_read_only_root"],
                "container": container_name,
            })

        # Added capabilities
        capabilities = security_context.get("capabilities", {})
        added = capabilities.get("add", [])
        if added:
            issues.append({
                "check": "capabilities_added",
                **SECURITY_CHECKS["capabilities_added"],
                "container": container_name,
                "capabilities": added,
            })

        # Resource limits
        resources = container.get("resources", {})
        if not resources.get("limits"):
            issues.append({
                "check": "no_resource_limits",
                **SECURITY_CHECKS["no_resource_limits"],
                "container": container_name,
            })

    return issues


def _get_pods(config: Config, namespace: str | None) -> list[dict]:
    """Get all pods."""
    cmd = ["get", "pods", "-o", "json"]
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    return data.get("items", [])


@click.command("audit-pods")
@click.option("--namespace", "-n", help="Namespace to audit (default: all)")
@click.option("--severity", "-s", type=click.Choice(["all", "critical", "warning"]),
              default="all", help="Filter by severity")
@click.option("--check", "-c", help="Run specific check only")
@click.option("--exclude-system", "-e", is_flag=True, help="Exclude kube-system namespace")
@click.option("--verbose", "-v", is_flag=True, help="Show detailed findings")
@click.pass_context
def audit_pods(
    ctx: click.Context,
    namespace: str | None,
    severity: str,
    check: str | None,
    exclude_system: bool,
    verbose: bool,
) -> None:
    """Security audit of pod configurations."""
    config: Config = ctx.obj

    print_header("Pod Security Audit")

    pods = _get_pods(config, namespace)
    if not pods:
        console.print("[yellow]No pods found[/yellow]")
        return

    # Filter system namespaces
    if exclude_system:
        pods = [p for p in pods if p.get("metadata", {}).get("namespace") not in ("kube-system", "kube-public", "kube-node-lease")]

    console.print(f"Auditing {len(pods)} pods...")

    # Audit each pod
    all_findings: list[dict] = []
    pod_findings: dict[str, list[dict]] = {}

    for pod in pods:
        metadata = pod.get("metadata", {})
        pod_ns = metadata.get("namespace", "")
        pod_name = metadata.get("name", "")
        pod_key = f"{pod_ns}/{pod_name}"

        issues = _audit_pod(pod)

        # Filter by check
        if check:
            issues = [i for i in issues if i["check"] == check]

        # Filter by severity
        if severity == "critical":
            issues = [i for i in issues if i["severity"] == Severity.CRITICAL]
        elif severity == "warning":
            issues = [i for i in issues if i["severity"] == Severity.WARNING]

        if issues:
            pod_findings[pod_key] = issues
            for issue in issues:
                issue["pod"] = pod_key
                all_findings.append(issue)

    # Summary by check type
    by_check: dict[str, int] = {}
    for f in all_findings:
        by_check[f["check"]] = by_check.get(f["check"], 0) + 1

    # Summary table
    if by_check:
        print_header("Findings Summary")
        summary_table = Table()
        summary_table.add_column("Check", style="cyan")
        summary_table.add_column("Count", justify="right")
        summary_table.add_column("Severity")
        summary_table.add_column("Risk")

        for check_name, count in sorted(by_check.items(), key=lambda x: -x[1]):
            check_info = SECURITY_CHECKS.get(check_name, {})
            sev = check_info.get("severity", Severity.WARNING)
            sev_color = "red" if sev == Severity.CRITICAL else "yellow"

            summary_table.add_row(
                check_name,
                str(count),
                f"[{sev_color}]{sev.name}[/{sev_color}]",
                check_info.get("risk", ""),
            )

        console.print(summary_table)

    # Pod-level findings
    if pod_findings:
        print_header("Pod Findings")

        critical_pods = {k: v for k, v in pod_findings.items()
                        if any(i["severity"] == Severity.CRITICAL for i in v)}
        warning_pods = {k: v for k, v in pod_findings.items()
                       if k not in critical_pods}

        # Show critical first
        if critical_pods:
            console.print("\n[bold red]Critical Issues:[/bold red]")
            for pod_key, issues in list(critical_pods.items())[:10]:
                console.print(f"  [red]{pod_key}[/red]")
                if verbose:
                    for issue in issues:
                        if issue["severity"] == Severity.CRITICAL:
                            console.print(f"    - {issue['description']}")

            if len(critical_pods) > 10:
                console.print(f"  ... and {len(critical_pods) - 10} more")

        # Show warnings
        if warning_pods and (verbose or not critical_pods):
            console.print("\n[bold yellow]Warning Issues:[/bold yellow]")
            for pod_key, issues in list(warning_pods.items())[:10]:
                console.print(f"  [yellow]{pod_key}[/yellow]")
                if verbose:
                    for issue in issues:
                        console.print(f"    - {issue['description']}")

            if len(warning_pods) > 10:
                console.print(f"  ... and {len(warning_pods) - 10} more")

    # Final summary
    critical_count = sum(1 for f in all_findings if f["severity"] == Severity.CRITICAL)
    warning_count = len(all_findings) - critical_count
    affected_pods = len(pod_findings)

    console.print(f"\n[bold]Summary:[/bold] {len(all_findings)} issues in {affected_pods} pods")
    console.print(f"  Critical: {critical_count}, Warnings: {warning_count}")

    if critical_count > 0:
        console.print("\n[bold red]Action required: Critical security issues found[/bold red]")
        ctx.exit(1)
    elif warning_count > 0:
        console.print("\n[yellow]Review recommended: Security warnings found[/yellow]")
    else:
        console.print("\n[green]No security issues found[/green]")
