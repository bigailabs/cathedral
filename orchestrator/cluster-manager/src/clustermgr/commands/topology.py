"""Network topology discovery and visualization command for clustermgr."""

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import click
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from clustermgr.config import Config
from clustermgr.utils import run_ansible, run_cmd

console = Console()


class ConnectionHealth(Enum):
    """Health status for network connections."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass
class WireGuardPeer:
    """WireGuard peer information."""

    public_key: str
    endpoint: str
    allowed_ips: list[str]
    latest_handshake: int  # Unix timestamp, 0 if never
    rx_bytes: int
    tx_bytes: int
    persistent_keepalive: int
    node_name: str = ""  # Node name from WireGuard config comment

    @property
    def endpoint_ip(self) -> str:
        """Extract just the IP from endpoint (removes port)."""
        if not self.endpoint:
            return ""
        return self.endpoint.split(":")[0]

    @property
    def handshake_age_seconds(self) -> int:
        if self.latest_handshake == 0:
            return -1  # Never connected
        return int(time.time()) - self.latest_handshake

    @property
    def health(self) -> ConnectionHealth:
        age = self.handshake_age_seconds
        if age < 0:
            return ConnectionHealth.CRITICAL
        if age > 300:
            return ConnectionHealth.CRITICAL
        if age > 180:
            return ConnectionHealth.WARNING
        return ConnectionHealth.HEALTHY

    @property
    def handshake_display(self) -> str:
        age = self.handshake_age_seconds
        if age < 0:
            return "never"
        if age < 60:
            return f"{age}s ago"
        if age < 3600:
            return f"{age // 60}m ago"
        return f"{age // 3600}h ago"

    @property
    def rx_display(self) -> str:
        return _format_bytes(self.rx_bytes)

    @property
    def tx_display(self) -> str:
        return _format_bytes(self.tx_bytes)


@dataclass
class InterfaceStats:
    """Network interface statistics."""

    rx_bytes: int = 0
    tx_bytes: int = 0
    rx_packets: int = 0
    tx_packets: int = 0
    rx_errors: int = 0
    tx_errors: int = 0
    rx_dropped: int = 0
    tx_dropped: int = 0


@dataclass
class NodeTopology:
    """Network topology for a single node."""

    name: str
    host: str
    role: str  # "server" or "agent"
    wg_interface: str = "wg0"
    wg_listen_port: int = 51820
    wg_public_key: str = ""
    peers: list[WireGuardPeer] = field(default_factory=list)
    interface_stats: InterfaceStats = field(default_factory=InterfaceStats)
    latency_ms: float = -1.0
    iptables_drops: int = 0
    error: str = ""


def _format_bytes(num_bytes: int) -> str:
    """Format bytes to human-readable string."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KiB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MiB"
    return f"{num_bytes / (1024 * 1024 * 1024):.1f} GiB"


def _parse_wg_dump(output: str) -> tuple[str, list[WireGuardPeer]]:
    """Parse output from 'wg show wg0 dump'.

    Format:
    <private-key> <public-key> <listen-port> <fwmark>
    <public-key> <preshared-key> <endpoint> <allowed-ips> <latest-handshake> <rx> <tx> <keepalive>
    """
    lines = output.strip().split("\n")
    if not lines:
        return "", []

    public_key = ""
    peers: list[WireGuardPeer] = []

    for i, line in enumerate(lines):
        parts = line.split("\t")
        if i == 0 and len(parts) >= 2:
            # Interface line: private-key, public-key, listen-port, fwmark
            public_key = parts[1] if len(parts) > 1 else ""
        elif len(parts) >= 8:
            # Peer line
            try:
                peer = WireGuardPeer(
                    public_key=parts[0],
                    endpoint=parts[2] if parts[2] != "(none)" else "",
                    allowed_ips=parts[3].split(",") if parts[3] else [],
                    latest_handshake=int(parts[4]) if parts[4] != "0" else 0,
                    rx_bytes=int(parts[5]),
                    tx_bytes=int(parts[6]),
                    persistent_keepalive=int(parts[7]) if parts[7] != "off" else 0,
                )
                peers.append(peer)
            except (ValueError, IndexError):
                continue

    return public_key, peers


def _parse_interface_stats(output: str) -> InterfaceStats:
    """Parse output from 'ip -s -s link show wg0'."""
    stats = InterfaceStats()

    # Match RX line with bytes and packets
    rx_match = re.search(r"RX:\s+bytes\s+packets.*?\n\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", output)
    if rx_match:
        stats.rx_bytes = int(rx_match.group(1))
        stats.rx_packets = int(rx_match.group(2))
        stats.rx_errors = int(rx_match.group(3))
        stats.rx_dropped = int(rx_match.group(4))

    # Match TX line with bytes and packets
    tx_match = re.search(r"TX:\s+bytes\s+packets.*?\n\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", output)
    if tx_match:
        stats.tx_bytes = int(tx_match.group(1))
        stats.tx_packets = int(tx_match.group(2))
        stats.tx_errors = int(tx_match.group(3))
        stats.tx_dropped = int(tx_match.group(4))

    return stats


def _parse_iptables_drops(output: str) -> int:
    """Parse iptables output for WireGuard drop count."""
    total_drops = 0
    for line in output.split("\n"):
        if "51820" in line and "DROP" in line:
            parts = line.split()
            for part in parts[:5]:
                if part.isdigit():
                    total_drops += int(part)
                    break
    return total_drops


def _parse_wg_config_peer_names(output: str) -> dict[str, str]:
    """Parse WireGuard config file to extract peer names from comments.

    Config format:
    [Peer]
    # Node: <node-id>
    PublicKey = <key>

    Returns dict mapping public_key -> node_name
    """
    peer_names: dict[str, str] = {}
    current_node_name = ""

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("# Node:"):
            current_node_name = line.split(":", 1)[1].strip()
        elif line.startswith("PublicKey") and current_node_name:
            key = line.split("=", 1)[1].strip() if "=" in line else ""
            if key:
                peer_names[key] = current_node_name
            current_node_name = ""

    return peer_names


def _collect_node_topology(config: Config, node_name: str, host: str, role: str) -> NodeTopology:
    """Collect topology information from a single node."""
    topology = NodeTopology(name=node_name, host=host, role=role)

    # Collect WireGuard dump
    wg_result = run_ansible(
        config,
        "shell",
        "sudo wg show wg0 dump 2>/dev/null || echo 'WG_ERROR'",
        hosts=node_name,
        timeout=15,
    )

    wg_output = _extract_ansible_output(wg_result.stdout, node_name)
    if "WG_ERROR" not in wg_output and wg_output.strip():
        topology.wg_public_key, topology.peers = _parse_wg_dump(wg_output)

    # Collect peer names from WireGuard config
    wg_config_result = run_ansible(
        config,
        "shell",
        "sudo cat /etc/wireguard/wg0.conf 2>/dev/null || echo 'CONFIG_ERROR'",
        hosts=node_name,
        timeout=15,
    )

    config_output = _extract_ansible_output(wg_config_result.stdout, node_name)
    if "CONFIG_ERROR" not in config_output:
        peer_names = _parse_wg_config_peer_names(config_output)
        for peer in topology.peers:
            if peer.public_key in peer_names:
                peer.node_name = peer_names[peer.public_key]

    # Collect interface stats
    stats_result = run_ansible(
        config,
        "shell",
        "ip -s -s link show wg0 2>/dev/null || echo 'STATS_ERROR'",
        hosts=node_name,
        timeout=15,
    )

    stats_output = _extract_ansible_output(stats_result.stdout, node_name)
    if "STATS_ERROR" not in stats_output:
        topology.interface_stats = _parse_interface_stats(stats_output)

    # Collect iptables drops
    ipt_result = run_ansible(
        config,
        "shell",
        "sudo iptables -L INPUT -n -v 2>/dev/null | grep -E '51820.*DROP' || true",
        hosts=node_name,
        timeout=15,
    )

    ipt_output = _extract_ansible_output(ipt_result.stdout, node_name)
    topology.iptables_drops = _parse_iptables_drops(ipt_output)

    return topology


def _extract_ansible_output(full_output: str, node_name: str) -> str:
    """Extract command output for a specific node from Ansible output."""
    lines = full_output.split("\n")
    output_lines: list[str] = []
    capturing = False

    for line in lines:
        if node_name in line and ("CHANGED" in line or "SUCCESS" in line):
            capturing = True
            continue
        if capturing:
            if " | " in line and ("CHANGED" in line or "SUCCESS" in line or "FAILED" in line):
                break
            output_lines.append(line)

    return "\n".join(output_lines).strip()


def _parse_inventory_hosts(config: Config) -> list[tuple[str, str, str]]:
    """Parse inventory file to get host information.

    Returns list of (name, host, role) tuples.
    """
    hosts: list[tuple[str, str, str]] = []
    current_group = ""

    try:
        with open(config.inventory) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Group header
                if line.startswith("[") and line.endswith("]"):
                    group = line[1:-1].split(":")[0]
                    if group in ("k3s_server", "k3s_agents"):
                        current_group = group
                    else:
                        current_group = ""
                    continue

                # Host line
                if current_group and not line.startswith("["):
                    parts = line.split()
                    if parts:
                        name = parts[0]
                        host = ""
                        for part in parts[1:]:
                            if part.startswith("ansible_host="):
                                host = part.split("=")[1]
                                break
                        role = "server" if current_group == "k3s_server" else "agent"
                        hosts.append((name, host, role))
    except FileNotFoundError:
        pass

    return hosts


def collect_cluster_topology(config: Config) -> list[NodeTopology]:
    """Collect topology information from all cluster nodes."""
    hosts = _parse_inventory_hosts(config)
    topologies: list[NodeTopology] = []

    for name, host, role in hosts:
        topology = _collect_node_topology(config, name, host, role)
        topologies.append(topology)

    return topologies


def _health_color(health: ConnectionHealth) -> str:
    """Get Rich color for health status."""
    return {
        ConnectionHealth.HEALTHY: "green",
        ConnectionHealth.WARNING: "yellow",
        ConnectionHealth.CRITICAL: "red",
        ConnectionHealth.UNKNOWN: "dim",
    }.get(health, "white")


def _health_symbol(health: ConnectionHealth) -> str:
    """Get symbol for health status."""
    return {
        ConnectionHealth.HEALTHY: "[OK]",
        ConnectionHealth.WARNING: "[!]",
        ConnectionHealth.CRITICAL: "[X]",
        ConnectionHealth.UNKNOWN: "[?]",
    }.get(health, "[ ]")


def display_topology_tree(topologies: list[NodeTopology]) -> None:
    """Display topology as a tree structure."""
    tree = Tree("[bold cyan]Cluster Network Topology[/bold cyan]")

    servers = [t for t in topologies if t.role == "server"]
    agents = [t for t in topologies if t.role == "agent"]

    # Control Plane section
    cp_branch = tree.add("[bold]Control Plane (k3s_server)[/bold]")
    for node in servers:
        node_health = _calculate_node_health(node)
        color = _health_color(node_health)
        symbol = _health_symbol(node_health)
        node_branch = cp_branch.add(
            f"[{color}]{symbol}[/{color}] [bold]{node.name}[/bold] ({node.host})"
        )

        # WireGuard info
        wg_info = f"wg0: {len(node.peers)} peers"
        if node.interface_stats.rx_errors > 0 or node.interface_stats.tx_errors > 0:
            wg_info += f" [yellow](errors: rx={node.interface_stats.rx_errors}, tx={node.interface_stats.tx_errors})[/yellow]"
        node_branch.add(wg_info)

        # Peers
        if node.peers:
            peers_branch = node_branch.add("Peers:")
            for peer in node.peers:
                peer_color = _health_color(peer.health)
                peer_symbol = _health_symbol(peer.health)
                # Build peer identifier: node name or allowed IPs
                peer_id = peer.node_name if peer.node_name else ", ".join(peer.allowed_ips[:2])
                if not peer.node_name and len(peer.allowed_ips) > 2:
                    peer_id += f" (+{len(peer.allowed_ips) - 2})"
                # Add public IP if available
                endpoint_info = f" @ {peer.endpoint_ip}" if peer.endpoint_ip else ""
                peers_branch.add(
                    f"[{peer_color}]{peer_symbol}[/{peer_color}] [bold]{peer_id}[/bold]{endpoint_info} "
                    f"(handshake: {peer.handshake_display}, rx: {peer.rx_display}, tx: {peer.tx_display})"
                )

    # Agents section
    if agents:
        agent_branch = tree.add("[bold]Worker Nodes (k3s_agents)[/bold]")
        for node in agents:
            node_health = _calculate_node_health(node)
            color = _health_color(node_health)
            symbol = _health_symbol(node_health)
            node_item = agent_branch.add(
                f"[{color}]{symbol}[/{color}] [bold]{node.name}[/bold] ({node.host})"
            )

            if node.peers:
                wg_info = f"wg0: {len(node.peers)} peers"
                node_item.add(wg_info)

    console.print(tree)


def display_topology_table(topologies: list[NodeTopology]) -> None:
    """Display topology as detailed tables."""
    # Summary table
    summary = Table(title="Node Summary")
    summary.add_column("Node", style="cyan")
    summary.add_column("Role")
    summary.add_column("Host")
    summary.add_column("Peers", justify="right")
    summary.add_column("RX", justify="right")
    summary.add_column("TX", justify="right")
    summary.add_column("Errors", justify="right")
    summary.add_column("IPT Drops", justify="right")
    summary.add_column("Health")

    for node in topologies:
        health = _calculate_node_health(node)
        color = _health_color(health)
        total_errors = node.interface_stats.rx_errors + node.interface_stats.tx_errors

        summary.add_row(
            node.name,
            node.role,
            node.host,
            str(len(node.peers)),
            _format_bytes(node.interface_stats.rx_bytes),
            _format_bytes(node.interface_stats.tx_bytes),
            str(total_errors) if total_errors > 0 else "-",
            str(node.iptables_drops) if node.iptables_drops > 0 else "-",
            f"[{color}]{health.value.upper()}[/{color}]",
        )

    console.print(summary)

    # Peer details table
    console.print()
    peers_table = Table(title="WireGuard Peer Details")
    peers_table.add_column("Node", style="cyan")
    peers_table.add_column("Peer Name", max_width=40)
    peers_table.add_column("Public IP")
    peers_table.add_column("WG IP", max_width=20)
    peers_table.add_column("Handshake")
    peers_table.add_column("RX", justify="right")
    peers_table.add_column("TX", justify="right")
    peers_table.add_column("Health")

    for node in topologies:
        for peer in node.peers:
            health_color = _health_color(peer.health)
            # Get first allowed IP (WireGuard tunnel IP)
            wg_ip = peer.allowed_ips[0] if peer.allowed_ips else "-"
            peers_table.add_row(
                node.name,
                peer.node_name or peer.public_key[:16] + "...",
                peer.endpoint_ip or "-",
                wg_ip,
                peer.handshake_display,
                peer.rx_display,
                peer.tx_display,
                f"[{health_color}]{peer.health.value.upper()}[/{health_color}]",
            )

    console.print(peers_table)


def display_connection_matrix(topologies: list[NodeTopology]) -> None:
    """Display a connection matrix showing peer relationships."""
    # Build a map of public keys to node names (cluster nodes)
    key_to_node: dict[str, str] = {}
    for node in topologies:
        if node.wg_public_key:
            key_to_node[node.wg_public_key] = node.name

    console.print("\n[bold cyan]Connection Matrix[/bold cyan]")
    console.print("Shows which nodes are peered with which others\n")

    for node in topologies:
        if not node.peers:
            continue

        console.print(f"[bold]{node.name}[/bold] ({node.role}):")
        for peer in node.peers:
            # Determine peer display name
            cluster_node = key_to_node.get(peer.public_key)
            if cluster_node:
                peer_display = cluster_node
            elif peer.node_name:
                peer_display = peer.node_name
            else:
                peer_display = "external"

            color = _health_color(peer.health)
            symbol = _health_symbol(peer.health)
            wg_ip = peer.allowed_ips[0] if peer.allowed_ips else "?"
            endpoint_info = f" @ {peer.endpoint_ip}" if peer.endpoint_ip else ""
            console.print(
                f"  [{color}]{symbol}[/{color}] -> [bold]{peer_display}[/bold]{endpoint_info} "
                f"({wg_ip}) [dim]handshake: {peer.handshake_display}[/dim]"
            )
        console.print()


def _calculate_node_health(node: NodeTopology) -> ConnectionHealth:
    """Calculate overall health for a node based on its peers."""
    if node.error:
        return ConnectionHealth.CRITICAL

    if not node.peers:
        return ConnectionHealth.UNKNOWN

    # Check if any peer is critical
    has_critical = any(p.health == ConnectionHealth.CRITICAL for p in node.peers)
    has_warning = any(p.health == ConnectionHealth.WARNING for p in node.peers)

    # Check iptables drops (active rate limiting is critical)
    if node.iptables_drops > 1000:
        return ConnectionHealth.CRITICAL
    if node.iptables_drops > 100:
        has_warning = True

    # Check interface errors - WireGuard can have high error counts from
    # invalid packets (port scans, malformed data) which are harmless.
    # Only flag dropped packets as concerning (actual data loss).
    total_dropped = node.interface_stats.rx_dropped + node.interface_stats.tx_dropped
    if total_dropped > 1000:
        return ConnectionHealth.CRITICAL
    if total_dropped > 100:
        has_warning = True

    if has_critical:
        return ConnectionHealth.CRITICAL
    if has_warning:
        return ConnectionHealth.WARNING
    return ConnectionHealth.HEALTHY


def display_health_summary(topologies: list[NodeTopology]) -> None:
    """Display a summary of cluster health."""
    total_nodes = len(topologies)
    total_peers = sum(len(t.peers) for t in topologies)

    healthy = sum(1 for t in topologies if _calculate_node_health(t) == ConnectionHealth.HEALTHY)
    warning = sum(1 for t in topologies if _calculate_node_health(t) == ConnectionHealth.WARNING)
    critical = sum(1 for t in topologies if _calculate_node_health(t) == ConnectionHealth.CRITICAL)

    total_rx = sum(t.interface_stats.rx_bytes for t in topologies)
    total_tx = sum(t.interface_stats.tx_bytes for t in topologies)
    total_errors = sum(
        t.interface_stats.rx_errors + t.interface_stats.tx_errors for t in topologies
    )
    total_drops = sum(t.iptables_drops for t in topologies)

    console.print("\n[bold cyan]=== Health Summary ===[/bold cyan]")
    console.print(f"Timestamp: {datetime.now().isoformat()}")
    console.print()
    console.print(f"Nodes: {total_nodes} total, [green]{healthy} healthy[/green], ", end="")
    console.print(f"[yellow]{warning} warning[/yellow], [red]{critical} critical[/red]")
    console.print(f"WireGuard Peers: {total_peers} total connections")
    console.print(f"Total Traffic: RX {_format_bytes(total_rx)}, TX {_format_bytes(total_tx)}")

    if total_errors > 0:
        console.print(f"[yellow]Interface Errors: {total_errors}[/yellow]")
    if total_drops > 0:
        console.print(f"[red]IPTables Drops: {total_drops}[/red]")

    if critical > 0:
        console.print("\n[red]Action Required: Some nodes have critical connectivity issues.[/red]")
        console.print("Run 'clustermgr diagnose' for detailed analysis.")


@click.command()
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["tree", "table", "matrix", "all"]),
    default="all",
    help="Output format",
)
@click.pass_context
def topology(ctx: click.Context, output_format: str) -> None:
    """Display cluster network topology with WireGuard connectivity status.

    Discovers all nodes in the cluster, their WireGuard peers, and displays
    health metrics including handshake age, transfer stats, and interface errors.

    Health Status:
      [OK]  - Healthy: handshake < 3 minutes
      [!]   - Warning: handshake 3-5 minutes or minor errors
      [X]   - Critical: handshake > 5 minutes or major issues
    """
    config: Config = ctx.obj

    console.print("[bold cyan]=== Cluster Network Topology ===[/bold cyan]")
    console.print("Collecting topology from cluster nodes...\n")

    topologies = collect_cluster_topology(config)

    if not topologies:
        console.print("[red]Failed to collect topology information.[/red]")
        console.print("Check that the inventory file exists and nodes are reachable.")
        ctx.exit(1)

    if output_format in ("tree", "all"):
        display_topology_tree(topologies)

    if output_format in ("table", "all"):
        console.print()
        display_topology_table(topologies)

    if output_format in ("matrix", "all"):
        display_connection_matrix(topologies)

    display_health_summary(topologies)
