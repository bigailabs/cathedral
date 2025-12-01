"""Main CLI entry point for clustermgr."""

import click
from rich.console import Console

from clustermgr import __version__
from clustermgr.commands import (
    audit_pods,
    bundle,
    cert_check,
    cleanup,
    deployments,
    diagnose,
    envoy,
    etcd,
    events,
    firewall,
    fix,
    flannel,
    fuse_troubleshoot,
    gateway,
    health,
    kubeconfig,
    latency_matrix,
    logs,
    maintenance,
    mesh_test,
    mtu,
    namespace,
    netpol,
    node_pressure,
    pod_troubleshoot,
    resources,
    scaling,
    topology,
    ud,
    wg,
)
from clustermgr.config import Config

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="clustermgr")
@click.option(
    "--kubeconfig",
    envvar="KUBECONFIG",
    help="Path to kubeconfig file",
)
@click.option(
    "--inventory",
    "-i",
    type=click.Path(exists=True),
    help="Ansible inventory file",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be done without making changes",
)
@click.option(
    "--no-confirm",
    "-y",
    is_flag=True,
    help="Skip confirmation prompts",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show verbose output",
)
@click.pass_context
def main(
    ctx: click.Context,
    kubeconfig: str | None,
    inventory: str | None,
    dry_run: bool,
    no_confirm: bool,
    verbose: bool,
) -> None:
    """Basilica K3s + WireGuard Cluster Management Tool.

    A CLI tool for diagnosing and managing K3s clusters with WireGuard VPN.
    Designed to automate common operational tasks identified during incident response.

    \b
    Examples:
      clustermgr health              Check cluster health
      clustermgr diagnose            Run comprehensive diagnostics
      clustermgr wg status           Show WireGuard peer status
      clustermgr fix --dry-run       Preview remediation actions
      clustermgr fix                 Execute remediation with confirmation
      clustermgr cleanup             Remove CrashLoopBackOff pods
    """
    ctx.ensure_object(dict)

    config = Config(
        dry_run=dry_run,
        verbose=verbose,
        no_confirm=no_confirm,
    )

    if kubeconfig:
        config.kubeconfig = kubeconfig
    if inventory:
        from pathlib import Path

        config.inventory = Path(inventory)

    ctx.obj = config


# Register commands - Core
main.add_command(health)
main.add_command(diagnose)
main.add_command(resources)
main.add_command(deployments)
main.add_command(events)

# Register commands - Network
main.add_command(topology)
main.add_command(wg)
main.add_command(flannel)
main.add_command(firewall)
main.add_command(mtu)
main.add_command(mesh_test)
main.add_command(latency_matrix)

# Register commands - Troubleshooting
main.add_command(pod_troubleshoot)
main.add_command(node_pressure)
main.add_command(fuse_troubleshoot)

# Register commands - Security
main.add_command(audit_pods)
main.add_command(cert_check)
main.add_command(kubeconfig)

# Register commands - Operations
main.add_command(fix)
main.add_command(cleanup)
main.add_command(logs)
main.add_command(bundle)

# Register commands - Maintenance
main.add_command(maintenance)
main.add_command(scaling)
main.add_command(etcd)

# Register commands - UserDeployment Management
main.add_command(ud)
main.add_command(gateway)
main.add_command(envoy)
main.add_command(netpol)
main.add_command(namespace)


if __name__ == "__main__":
    main()
