"""Health check command for clustermgr."""

from datetime import datetime

import click
from rich.console import Console

from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    parse_json_output,
    print_header,
    print_status,
    run_ansible,
    run_kubectl,
)

console = Console()


def check_nodes(config: Config) -> list[dict]:
    """Check K8s node status."""
    result = run_kubectl(config, ["get", "nodes", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        conditions = {c["type"]: c["status"] for c in item.get("status", {}).get("conditions", [])}
        ready = conditions.get("Ready", "Unknown")
        nodes.append({
            "name": name,
            "ready": ready == "True",
            "conditions": conditions,
        })
    return nodes


def check_wireguard_peers(config: Config) -> dict[str, list[dict]]:
    """Check WireGuard peer status on all servers."""
    result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 | grep -E 'peer|allowed|handshake|transfer'",
        timeout=30,
    )

    peers_by_server: dict[str, list[dict]] = {}
    current_server: str | None = None
    current_peer: dict | None = None

    for line in result.stdout.split("\n"):
        line = line.strip()
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            peers_by_server[current_server] = []
        elif line.startswith("peer:"):
            current_peer = {"key": line.split(": ")[1] if ": " in line else "unknown"}
            if current_server:
                peers_by_server[current_server].append(current_peer)
        elif current_peer and line.startswith("allowed ips:"):
            current_peer["allowed_ips"] = line.split(": ")[1] if ": " in line else ""
        elif current_peer and line.startswith("latest handshake:"):
            handshake_str = line.split(": ")[1] if ": " in line else ""
            current_peer["handshake"] = handshake_str
            current_peer["handshake_stale"] = (
                "minute" in handshake_str and int(handshake_str.split()[0]) > 3
            )
        elif current_peer and line.startswith("transfer:"):
            current_peer["transfer"] = line.split(": ")[1] if ": " in line else ""

    return peers_by_server


def check_iptables_drops(config: Config) -> dict[str, dict]:
    """Check iptables for rate limit drops."""
    result = run_ansible(
        config,
        "shell",
        "sudo iptables -L INPUT -n -v --line-numbers | grep -E '51820.*DROP|hashlimit.*DROP'",
        timeout=30,
    )

    drops_by_server: dict[str, dict] = {}
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        line = line.strip()
        if " | CHANGED" in line or " | SUCCESS" in line or " | FAILED" in line:
            current_server = line.split(" | ")[0].strip()
            drops_by_server[current_server] = {"has_rate_limit": False, "drops": 0}
        elif current_server and "DROP" in line and "51820" in line:
            drops_by_server[current_server]["has_rate_limit"] = True
            parts = line.split()
            for i, part in enumerate(parts):
                if part.isdigit() and i < 3:
                    drops_by_server[current_server]["drops"] = int(part)
                    break

    return drops_by_server


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Multi-layer cluster health check."""
    config: Config = ctx.obj

    print_header("K3s Cluster Health Check")
    console.print(f"Timestamp: {datetime.now().isoformat()}")

    issues: list[str] = []

    # Layer 1: Kubernetes nodes
    print_header("Layer 1: Kubernetes Nodes")
    nodes = check_nodes(config)
    if not nodes:
        print_status("API Server", "UNREACHABLE", Severity.EMERGENCY)
        issues.append("Cannot reach K8s API server")
    else:
        for node in nodes:
            severity = Severity.HEALTHY if node["ready"] else Severity.CRITICAL
            status = "Ready" if node["ready"] else "NotReady"
            print_status(node["name"], status, severity)
            if not node["ready"]:
                issues.append(f"Node {node['name']} is NotReady")

    # Layer 2: WireGuard peers
    print_header("Layer 2: WireGuard Peers")
    peers = check_wireguard_peers(config)
    if not peers:
        print_status("WireGuard", "NO DATA", Severity.WARNING)
        issues.append("Could not retrieve WireGuard peer information")
    else:
        for server, server_peers in peers.items():
            console.print(f"\n  [bold]{server}[/bold]:")
            for peer in server_peers:
                ips = peer.get("allowed_ips", "unknown")
                handshake = peer.get("handshake", "unknown")
                stale = peer.get("handshake_stale", False)
                severity = Severity.CRITICAL if stale else Severity.HEALTHY
                print_status(f"    {ips[:30]}", handshake, severity)
                if stale:
                    issues.append(f"Stale WireGuard handshake on {server} for {ips}")

    # Layer 3: iptables rate limits
    print_header("Layer 3: iptables Rate Limits")
    drops = check_iptables_drops(config)
    if not drops:
        print_status("iptables", "NO DATA", Severity.WARNING)
    else:
        for server, info in drops.items():
            if info["has_rate_limit"]:
                severity = Severity.CRITICAL if info["drops"] > 100 else Severity.WARNING
                print_status(server, f"RATE LIMIT ACTIVE - {info['drops']} drops", severity)
                issues.append(f"Rate limit rule on {server} with {info['drops']} drops")
            else:
                print_status(server, "No rate limit rules", Severity.HEALTHY)

    # Summary
    print_header("Summary")
    if issues:
        console.print(f"\n[red]Found {len(issues)} issue(s):[/red]")
        for i, issue in enumerate(issues, 1):
            console.print(f"  {i}. {issue}")
        console.print("\nRun '[cyan]clustermgr diagnose[/cyan]' for detailed analysis")
        console.print("Run '[cyan]clustermgr fix --dry-run[/cyan]' to see remediation plan")
        ctx.exit(1)
    else:
        console.print("\n[green]Cluster is healthy![/green]")
