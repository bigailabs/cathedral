"""Diagnostic bundle collection command for clustermgr.

Collects comprehensive diagnostic information for escalation
and troubleshooting as specified in HTTP-503-DIAGNOSIS.md.
"""

import os
import tarfile
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console

from clustermgr.config import Config
from clustermgr.utils import print_header, run_ansible, run_kubectl

console = Console()


def _collect_kubectl_output(config: Config, bundle_dir: Path, name: str, args: list[str]) -> None:
    """Collect kubectl command output to file."""
    result = run_kubectl(config, args, timeout=60)
    output_file = bundle_dir / f"{name}.txt"
    with open(output_file, "w") as f:
        if result.returncode == 0:
            f.write(result.stdout)
        else:
            f.write(f"Error: {result.stderr}\n")


def _collect_ansible_output(config: Config, bundle_dir: Path, name: str, cmd: str, hosts: str = "k3s_server") -> None:
    """Collect ansible command output to file."""
    result = run_ansible(config, "shell", cmd, hosts=hosts, timeout=60)
    output_file = bundle_dir / f"{name}.txt"
    with open(output_file, "w") as f:
        if result.returncode == 0:
            f.write(result.stdout)
        else:
            f.write(f"Error: {result.stderr}\n")


@click.command("bundle")
@click.option("--output", "-o", default="/tmp", help="Output directory for bundle")
@click.option("--namespace", "-n", help="Focus on specific namespace")
@click.option("--quick", "-q", is_flag=True, help="Quick bundle (skip slow operations)")
@click.pass_context
def bundle(ctx: click.Context, output: str, namespace: str | None, quick: bool) -> None:
    """Collect diagnostic bundle for escalation.

    Gathers cluster state, logs, and network configuration
    into a tarball for troubleshooting or escalation to
    senior engineers.
    """
    config: Config = ctx.obj

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bundle_name = f"basilica-diag-{timestamp}"
    bundle_dir = Path(output) / bundle_name

    print_header("Diagnostic Bundle Collection")
    console.print(f"Output: {bundle_dir}")
    console.print(f"Quick mode: {quick}")
    if namespace:
        console.print(f"Focus namespace: {namespace}")

    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Collect K8s resources
    print_header("Collecting Kubernetes State")

    console.print("  Collecting nodes...")
    _collect_kubectl_output(config, bundle_dir, "nodes", ["get", "nodes", "-o", "wide"])
    _collect_kubectl_output(config, bundle_dir, "nodes-yaml", ["get", "nodes", "-o", "yaml"])

    console.print("  Collecting pods...")
    _collect_kubectl_output(config, bundle_dir, "pods", ["get", "pods", "-A", "-o", "wide"])

    console.print("  Collecting services...")
    _collect_kubectl_output(config, bundle_dir, "services", ["get", "svc", "-A"])

    console.print("  Collecting endpoints...")
    _collect_kubectl_output(config, bundle_dir, "endpoints", ["get", "endpoints", "-A"])

    console.print("  Collecting events...")
    _collect_kubectl_output(config, bundle_dir, "events", ["get", "events", "-A", "--sort-by=.lastTimestamp"])

    console.print("  Collecting HTTPRoutes...")
    _collect_kubectl_output(config, bundle_dir, "httproutes", ["get", "httproutes", "-A", "-o", "yaml"])

    console.print("  Collecting UserDeployments...")
    _collect_kubectl_output(config, bundle_dir, "userdeployments", ["get", "userdeployments", "-A", "-o", "yaml"])

    console.print("  Collecting NetworkPolicies...")
    _collect_kubectl_output(config, bundle_dir, "networkpolicies", ["get", "networkpolicies", "-A", "-o", "yaml"])

    # Collect logs
    print_header("Collecting Logs")

    console.print("  Collecting Envoy Gateway logs...")
    _collect_kubectl_output(
        config, bundle_dir, "envoy-logs",
        ["logs", "-n", "envoy-gateway-system",
         "-l", "gateway.envoyproxy.io/owning-gateway-name=basilica-gateway",
         "--tail=500", "--all-containers"],
    )

    console.print("  Collecting operator logs...")
    _collect_kubectl_output(
        config, bundle_dir, "operator-logs",
        ["logs", "-n", "basilica-system", "deployment/basilica-operator", "--tail=500"],
    )

    if namespace:
        console.print(f"  Collecting logs for namespace {namespace}...")
        _collect_kubectl_output(
            config, bundle_dir, f"namespace-{namespace}-pods",
            ["get", "pods", "-n", namespace, "-o", "wide"],
        )
        _collect_kubectl_output(
            config, bundle_dir, f"namespace-{namespace}-events",
            ["get", "events", "-n", namespace, "--sort-by=.lastTimestamp"],
        )

    # Collect network state
    print_header("Collecting Network State")

    console.print("  Collecting routing tables...")
    _collect_ansible_output(config, bundle_dir, "routes", "ip route show")

    console.print("  Collecting FDB entries...")
    _collect_ansible_output(config, bundle_dir, "fdb", "bridge fdb show dev flannel.1 2>/dev/null || echo 'N/A'")

    console.print("  Collecting neighbor entries...")
    _collect_ansible_output(config, bundle_dir, "neighbors", "ip neigh show dev flannel.1 2>/dev/null || echo 'N/A'")

    console.print("  Collecting WireGuard status...")
    _collect_ansible_output(config, bundle_dir, "wireguard", "sudo wg show wg0 2>/dev/null || echo 'N/A'")

    console.print("  Collecting interface info...")
    _collect_ansible_output(config, bundle_dir, "interfaces", "ip -d link show")

    if not quick:
        console.print("  Collecting iptables rules...")
        _collect_ansible_output(config, bundle_dir, "iptables", "sudo iptables -L -n -v 2>/dev/null || echo 'N/A'")

        console.print("  Collecting sysctl settings...")
        _collect_ansible_output(config, bundle_dir, "sysctl", "sysctl -a 2>/dev/null | grep -E '(net.core|net.ipv4|nf_conntrack)'")

    # Collect system info
    print_header("Collecting System Info")

    console.print("  Collecting memory/CPU...")
    _collect_ansible_output(config, bundle_dir, "system-resources", "free -h && echo && uptime && echo && df -h")

    # Write metadata
    metadata_file = bundle_dir / "metadata.txt"
    with open(metadata_file, "w") as f:
        f.write(f"Bundle created: {datetime.now().isoformat()}\n")
        f.write(f"Quick mode: {quick}\n")
        f.write(f"Focus namespace: {namespace or 'all'}\n")
        f.write(f"Kubeconfig: {config.kubeconfig}\n")
        f.write(f"Inventory: {config.inventory}\n")

    # Create tarball
    print_header("Creating Archive")

    tarball_path = Path(output) / f"{bundle_name}.tar.gz"
    with tarfile.open(tarball_path, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_name)

    # Cleanup directory
    import shutil
    shutil.rmtree(bundle_dir)

    # Report
    tarball_size = tarball_path.stat().st_size
    size_str = f"{tarball_size / 1_000_000:.2f} MB" if tarball_size > 1_000_000 else f"{tarball_size / 1_000:.2f} KB"

    console.print(f"\n[green]Bundle created: {tarball_path}[/green]")
    console.print(f"Size: {size_str}")
    console.print("\nTo extract: tar -xzf {tarball_path}")
    console.print("\nInclude this bundle when escalating issues.")
