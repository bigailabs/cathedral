"""Flannel VXLAN diagnostics commands for clustermgr.

These commands help diagnose HTTP 503 errors caused by Flannel VXLAN
routing issues between Envoy pods and user pods on GPU nodes.

Key components inspected:
- flannel.1 interface status and MAC addresses
- FDB (Forwarding Database) entries for VXLAN tunneling
- Neighbor (ARP) entries for VTEP IP addresses
- Routes through flannel.1 for pod CIDRs
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
    run_ansible,
    run_kubectl,
)

console = Console()


@dataclass
class FlannelInterface:
    """Flannel interface status on a node."""

    node: str
    exists: bool
    mac: str
    mtu: int
    state: str
    rx_bytes: int
    tx_bytes: int
    rx_dropped: int
    tx_dropped: int
    vni: int


@dataclass
class FDBEntry:
    """Forwarding Database entry for VXLAN."""

    mac: str
    destination: str
    node_name: str
    is_permanent: bool


@dataclass
class NeighborEntry:
    """ARP/neighbor entry for VTEP."""

    vtep_ip: str
    mac: str
    state: str
    node_name: str


@dataclass
class FlannelRoute:
    """Route through flannel.1."""

    pod_cidr: str
    via: str
    device: str
    node_name: str
    is_onlink: bool


@dataclass
class GPUNodeInfo:
    """GPU node information from K8s."""

    name: str
    wg_ip: str
    pod_cidr: str
    flannel_mac: str
    flannel_public_ip: str


def _get_gpu_nodes(config: Config) -> list[GPUNodeInfo]:
    """Get GPU node information from K8s including Flannel annotations."""
    result = run_kubectl(
        config,
        ["get", "nodes", "-l", "basilica.ai/wireguard=true", "-o", "json"],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    nodes = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        annotations = metadata.get("annotations", {})
        spec = item.get("spec", {})
        status = item.get("status", {})

        pod_cidr = spec.get("podCIDR", "")
        if not pod_cidr:
            continue

        wg_ip = ""
        for addr in status.get("addresses", []):
            if addr.get("type") == "InternalIP":
                wg_ip = addr.get("address", "")
                break

        backend_data = annotations.get("flannel.alpha.coreos.com/backend-data", "{}")
        flannel_mac = ""
        try:
            import json
            bd = json.loads(backend_data)
            flannel_mac = bd.get("VtepMAC", "")
        except (json.JSONDecodeError, TypeError):
            pass

        flannel_public_ip = annotations.get("flannel.alpha.coreos.com/public-ip", "")

        nodes.append(GPUNodeInfo(
            name=metadata.get("name", ""),
            wg_ip=wg_ip,
            pod_cidr=pod_cidr,
            flannel_mac=flannel_mac,
            flannel_public_ip=flannel_public_ip,
        ))

    return nodes


def _parse_flannel_interface(output: str, node: str) -> FlannelInterface:
    """Parse flannel.1 interface information from ip command output."""
    exists = "flannel.1" in output and "INTERFACE_NOT_FOUND" not in output
    if not exists:
        return FlannelInterface(
            node=node,
            exists=False,
            mac="",
            mtu=0,
            state="DOWN",
            rx_bytes=0,
            tx_bytes=0,
            rx_dropped=0,
            tx_dropped=0,
            vni=0,
        )

    mac_match = re.search(r"link/ether\s+([0-9a-f:]+)", output)
    mac = mac_match.group(1) if mac_match else ""

    mtu_match = re.search(r"mtu\s+(\d+)", output)
    mtu = int(mtu_match.group(1)) if mtu_match else 0

    state = "UP" if ",UP" in output or "state UP" in output or "state UNKNOWN" in output else "DOWN"

    # Parse stats from /sys/class/net/flannel.1/statistics/ output
    # The command outputs: rx_bytes, tx_bytes, rx_dropped, tx_dropped on separate lines
    lines = output.strip().split("\n")
    stats_lines = [l.strip() for l in lines if l.strip().isdigit()]

    rx_bytes = int(stats_lines[0]) if len(stats_lines) > 0 else 0
    tx_bytes = int(stats_lines[1]) if len(stats_lines) > 1 else 0
    rx_dropped = int(stats_lines[2]) if len(stats_lines) > 2 else 0
    tx_dropped = int(stats_lines[3]) if len(stats_lines) > 3 else 0

    vni_match = re.search(r"id\s+(\d+)", output)
    vni = int(vni_match.group(1)) if vni_match else 1

    return FlannelInterface(
        node=node,
        exists=exists,
        mac=mac,
        mtu=mtu,
        state=state,
        rx_bytes=rx_bytes,
        tx_bytes=tx_bytes,
        rx_dropped=rx_dropped,
        tx_dropped=tx_dropped,
        vni=vni,
    )


def _get_flannel_interfaces(config: Config) -> list[FlannelInterface]:
    """Get flannel.1 interface status from all servers."""
    # Use simpler command to avoid segfault on some systems
    result = run_ansible(
        config,
        "shell",
        "ip link show flannel.1 2>/dev/null && cat /sys/class/net/flannel.1/statistics/rx_bytes /sys/class/net/flannel.1/statistics/tx_bytes /sys/class/net/flannel.1/statistics/rx_dropped /sys/class/net/flannel.1/statistics/tx_dropped 2>/dev/null || echo 'INTERFACE_NOT_FOUND'",
        timeout=30,
    )
    if result.returncode != 0:
        return []

    interfaces = []
    current_node = None
    current_output = []

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            if current_node and current_output:
                iface = _parse_flannel_interface("\n".join(current_output), current_node)
                interfaces.append(iface)
            current_node = line.split(" | ")[0].strip()
            current_output = []
        elif current_node:
            current_output.append(line)

    if current_node and current_output:
        iface = _parse_flannel_interface("\n".join(current_output), current_node)
        interfaces.append(iface)

    return interfaces


def _get_fdb_entries(config: Config) -> dict[str, list[FDBEntry]]:
    """Get FDB entries for flannel.1 from all servers."""
    result = run_ansible(
        config,
        "shell",
        "bridge fdb show dev flannel.1 2>/dev/null | head -50",
        timeout=30,
    )
    if result.returncode != 0:
        return {}

    entries_by_node: dict[str, list[FDBEntry]] = {}
    current_node = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_node = line.split(" | ")[0].strip()
            entries_by_node[current_node] = []
        elif current_node and line.strip():
            parts = line.strip().split()
            if len(parts) >= 3 and parts[1] == "dst":
                mac = parts[0]
                destination = parts[2]
                is_permanent = "permanent" in line

                entries_by_node[current_node].append(FDBEntry(
                    mac=mac,
                    destination=destination,
                    node_name="",
                    is_permanent=is_permanent,
                ))

    return entries_by_node


def _get_neighbor_entries(config: Config) -> dict[str, list[NeighborEntry]]:
    """Get neighbor/ARP entries for flannel.1 from all servers."""
    result = run_ansible(
        config,
        "shell",
        "ip neigh show dev flannel.1 2>/dev/null | head -50",
        timeout=30,
    )
    if result.returncode != 0:
        return {}

    entries_by_node: dict[str, list[NeighborEntry]] = {}
    current_node = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_node = line.split(" | ")[0].strip()
            entries_by_node[current_node] = []
        elif current_node and line.strip():
            parts = line.strip().split()
            if len(parts) >= 4 and parts[1] == "lladdr":
                vtep_ip = parts[0]
                mac = parts[2]
                state = parts[3] if len(parts) > 3 else "UNKNOWN"

                entries_by_node[current_node].append(NeighborEntry(
                    vtep_ip=vtep_ip,
                    mac=mac,
                    state=state,
                    node_name="",
                ))

    return entries_by_node


def _get_flannel_routes(config: Config) -> dict[str, list[FlannelRoute]]:
    """Get routes through flannel.1 from all servers."""
    result = run_ansible(
        config,
        "shell",
        "ip route show | grep 'dev flannel.1' | head -50",
        timeout=30,
    )
    if result.returncode != 0:
        return {}

    routes_by_node: dict[str, list[FlannelRoute]] = {}
    current_node = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_node = line.split(" | ")[0].strip()
            routes_by_node[current_node] = []
        elif current_node and line.strip():
            parts = line.strip().split()
            if len(parts) >= 3:
                pod_cidr = parts[0]
                via = ""
                device = "flannel.1"
                is_onlink = "onlink" in line

                for i, part in enumerate(parts):
                    if part == "via" and i + 1 < len(parts):
                        via = parts[i + 1]

                routes_by_node[current_node].append(FlannelRoute(
                    pod_cidr=pod_cidr,
                    via=via,
                    device=device,
                    node_name="",
                    is_onlink=is_onlink,
                ))

    return routes_by_node


@click.group()
def flannel() -> None:
    """Flannel VXLAN diagnostics commands.

    Commands for diagnosing Flannel overlay network issues that can
    cause HTTP 503 errors when routing traffic between Envoy pods
    and user pods on GPU nodes.

    Key areas diagnosed:
    - flannel.1 interface health
    - FDB (Forwarding Database) entries
    - Neighbor/ARP entries for VTEPs
    - Pod CIDR routes through flannel.1
    """
    pass


@flannel.command("status")
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show flannel.1 interface status on all servers.

    Displays the VXLAN interface status including MAC address, MTU,
    packet statistics, and dropped packet counts.
    """
    config: Config = ctx.obj

    print_header("Flannel Interface Status")

    interfaces = _get_flannel_interfaces(config)
    if not interfaces:
        console.print("[red]Failed to get Flannel interface status[/red]")
        ctx.exit(1)

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("State")
    table.add_column("MAC")
    table.add_column("MTU")
    table.add_column("RX Bytes")
    table.add_column("TX Bytes")
    table.add_column("Dropped")

    issues_found = False

    for iface in interfaces:
        if not iface.exists:
            table.add_row(
                iface.node,
                "[red]MISSING[/red]",
                "-", "-", "-", "-", "-",
            )
            issues_found = True
            continue

        state_color = "green" if iface.state == "UP" else "red"
        state_str = f"[{state_color}]{iface.state}[/{state_color}]"

        total_dropped = iface.rx_dropped + iface.tx_dropped
        if total_dropped > 1000:
            dropped_str = f"[red]{total_dropped}[/red]"
            issues_found = True
        elif total_dropped > 100:
            dropped_str = f"[yellow]{total_dropped}[/yellow]"
        else:
            dropped_str = f"[green]{total_dropped}[/green]"

        def format_bytes(b: int) -> str:
            if b > 1_000_000_000:
                return f"{b / 1_000_000_000:.1f}G"
            if b > 1_000_000:
                return f"{b / 1_000_000:.1f}M"
            if b > 1_000:
                return f"{b / 1_000:.1f}K"
            return str(b)

        table.add_row(
            iface.node,
            state_str,
            iface.mac,
            str(iface.mtu),
            format_bytes(iface.rx_bytes),
            format_bytes(iface.tx_bytes),
            dropped_str,
        )

    console.print(table)

    if issues_found:
        ctx.exit(1)


@flannel.command("fdb")
@click.option("--node", "-n", help="Filter by specific node")
@click.pass_context
def fdb(ctx: click.Context, node: str | None) -> None:
    """Inspect FDB (Forwarding Database) entries for VXLAN.

    Shows the MAC-to-destination mappings used by Flannel VXLAN
    to route traffic to remote nodes. Each GPU node should have
    an FDB entry mapping its flannel.1 MAC to its WireGuard IP.
    """
    config: Config = ctx.obj

    print_header("Flannel FDB Entries")

    gpu_nodes = _get_gpu_nodes(config)
    gpu_by_mac: dict[str, GPUNodeInfo] = {n.flannel_mac: n for n in gpu_nodes if n.flannel_mac}
    gpu_by_ip: dict[str, GPUNodeInfo] = {n.wg_ip: n for n in gpu_nodes if n.wg_ip}

    entries_by_node = _get_fdb_entries(config)

    if node:
        entries_by_node = {k: v for k, v in entries_by_node.items() if k == node}

    if not entries_by_node:
        console.print("[yellow]No FDB entries found[/yellow]")
        return

    print_header("GPU Nodes (Expected FDB Entries)")
    if gpu_nodes:
        for gpu in gpu_nodes:
            console.print(f"  {gpu.name[:30]}: MAC={gpu.flannel_mac or 'N/A'}, WG={gpu.wg_ip}")
    else:
        console.print("  [dim]No GPU nodes found with WireGuard labels[/dim]")

    for server, entries in entries_by_node.items():
        print_header(f"FDB on {server}")

        if not entries:
            console.print("  [dim]No FDB entries[/dim]")
            continue

        table = Table()
        table.add_column("MAC", style="cyan")
        table.add_column("Destination")
        table.add_column("GPU Node")
        table.add_column("Permanent")

        for entry in entries:
            gpu = gpu_by_mac.get(entry.mac) or gpu_by_ip.get(entry.destination)
            node_name = gpu.name[:20] if gpu else "[dim]-[/dim]"
            perm_str = "[green]Yes[/green]" if entry.is_permanent else "[dim]No[/dim]"

            table.add_row(
                entry.mac,
                entry.destination,
                node_name,
                perm_str,
            )

        console.print(table)

    print_header("Missing FDB Entries")
    all_fdb_macs: set[str] = set()
    for entries in entries_by_node.values():
        all_fdb_macs.update(e.mac for e in entries)

    missing = [gpu for gpu in gpu_nodes if gpu.flannel_mac and gpu.flannel_mac not in all_fdb_macs]
    if missing:
        for gpu in missing:
            console.print(f"  [red]Missing:[/red] {gpu.name} (MAC={gpu.flannel_mac}, WG={gpu.wg_ip})")
        ctx.exit(1)
    else:
        console.print("  [green]All GPU nodes have FDB entries[/green]")


@flannel.command("neighbors")
@click.option("--node", "-n", help="Filter by specific node")
@click.pass_context
def neighbors(ctx: click.Context, node: str | None) -> None:
    """Check neighbor/ARP entries for VTEP IPs.

    Shows the neighbor table entries for flannel.1 interface.
    These entries map VTEP IPs (pod network gateway IPs like 10.42.X.0)
    to MAC addresses for VXLAN encapsulation.
    """
    config: Config = ctx.obj

    print_header("Flannel Neighbor Entries")

    gpu_nodes = _get_gpu_nodes(config)
    gpu_by_cidr: dict[str, GPUNodeInfo] = {}
    for gpu in gpu_nodes:
        if gpu.pod_cidr:
            vtep_ip = gpu.pod_cidr.replace("/24", "").rsplit(".", 1)[0] + ".0"
            gpu_by_cidr[vtep_ip] = gpu

    entries_by_node = _get_neighbor_entries(config)

    if node:
        entries_by_node = {k: v for k, v in entries_by_node.items() if k == node}

    if not entries_by_node:
        console.print("[yellow]No neighbor entries found[/yellow]")
        return

    print_header("Expected VTEP Entries (GPU Nodes)")
    for vtep_ip, gpu in gpu_by_cidr.items():
        console.print(f"  {vtep_ip} -> {gpu.flannel_mac or 'N/A'} ({gpu.name[:25]})")

    for server, entries in entries_by_node.items():
        print_header(f"Neighbors on {server}")

        if not entries:
            console.print("  [dim]No neighbor entries[/dim]")
            continue

        table = Table()
        table.add_column("VTEP IP", style="cyan")
        table.add_column("MAC")
        table.add_column("State")
        table.add_column("GPU Node")

        for entry in entries:
            gpu = gpu_by_cidr.get(entry.vtep_ip)
            node_name = gpu.name[:20] if gpu else "[dim]-[/dim]"

            state_color = "green" if entry.state == "PERMANENT" else "yellow"
            state_str = f"[{state_color}]{entry.state}[/{state_color}]"

            mac_ok = gpu and gpu.flannel_mac == entry.mac
            mac_color = "green" if mac_ok else "red"
            mac_str = f"[{mac_color}]{entry.mac}[/{mac_color}]"

            table.add_row(
                entry.vtep_ip,
                mac_str,
                state_str,
                node_name,
            )

        console.print(table)

    print_header("Missing Neighbor Entries")
    all_vteps: set[str] = set()
    for entries in entries_by_node.values():
        all_vteps.update(e.vtep_ip for e in entries)

    missing = [vtep for vtep in gpu_by_cidr if vtep not in all_vteps]
    if missing:
        for vtep in missing:
            gpu = gpu_by_cidr[vtep]
            console.print(f"  [red]Missing:[/red] {vtep} for {gpu.name}")
        ctx.exit(1)
    else:
        console.print("  [green]All GPU nodes have neighbor entries[/green]")


@flannel.command("routes")
@click.option("--node", "-n", help="Filter by specific node")
@click.pass_context
def routes(ctx: click.Context, node: str | None) -> None:
    """Verify pod CIDR routes through flannel.1.

    Shows routes for pod networks that should go through the
    flannel.1 VXLAN interface. Each GPU node's pod CIDR should
    have a route via flannel.1 with the 'onlink' flag.
    """
    config: Config = ctx.obj

    print_header("Flannel Routes")

    gpu_nodes = _get_gpu_nodes(config)
    expected_cidrs = {gpu.pod_cidr for gpu in gpu_nodes if gpu.pod_cidr}

    routes_by_node = _get_flannel_routes(config)

    if node:
        routes_by_node = {k: v for k, v in routes_by_node.items() if k == node}

    if not routes_by_node:
        console.print("[yellow]No Flannel routes found[/yellow]")
        return

    print_header("Expected Routes (GPU Node Pod CIDRs)")
    for gpu in gpu_nodes:
        if gpu.pod_cidr:
            console.print(f"  {gpu.pod_cidr} -> {gpu.name[:30]}")

    for server, server_routes in routes_by_node.items():
        print_header(f"Routes on {server}")

        if not server_routes:
            console.print("  [dim]No flannel.1 routes[/dim]")
            continue

        table = Table()
        table.add_column("Pod CIDR", style="cyan")
        table.add_column("Via")
        table.add_column("Device")
        table.add_column("Onlink")

        for route in server_routes:
            is_gpu_route = route.pod_cidr in expected_cidrs
            cidr_color = "green" if is_gpu_route else "dim"
            cidr_str = f"[{cidr_color}]{route.pod_cidr}[/{cidr_color}]"

            onlink_str = "[green]Yes[/green]" if route.is_onlink else "[dim]No[/dim]"

            table.add_row(
                cidr_str,
                route.via or "-",
                route.device,
                onlink_str,
            )

        console.print(table)

    print_header("Missing Routes")
    all_routed_cidrs: set[str] = set()
    for server_routes in routes_by_node.values():
        all_routed_cidrs.update(r.pod_cidr for r in server_routes)

    missing = expected_cidrs - all_routed_cidrs
    if missing:
        for cidr in missing:
            gpu = next((g for g in gpu_nodes if g.pod_cidr == cidr), None)
            name = gpu.name[:30] if gpu else "unknown"
            console.print(f"  [red]Missing:[/red] {cidr} ({name})")
        ctx.exit(1)
    else:
        console.print("  [green]All GPU node pod CIDRs have routes[/green]")


@flannel.command("test")
@click.option("--gpu-node", "-g", help="Specific GPU node to test")
@click.pass_context
def test(ctx: click.Context, gpu_node: str | None) -> None:
    """Test VXLAN connectivity to GPU nodes.

    Verifies that VXLAN encapsulated traffic can reach GPU nodes
    by testing ping connectivity to pod network gateway IPs.
    """
    config: Config = ctx.obj

    print_header("Flannel VXLAN Connectivity Test")

    gpu_nodes = _get_gpu_nodes(config)
    if gpu_node:
        gpu_nodes = [g for g in gpu_nodes if gpu_node in g.name]

    if not gpu_nodes:
        console.print("[yellow]No GPU nodes found to test[/yellow]")
        return

    console.print(f"Testing connectivity to {len(gpu_nodes)} GPU node(s)...")

    for gpu in gpu_nodes:
        vtep_ip = gpu.pod_cidr.replace("/24", "").rsplit(".", 1)[0] + ".0"

        result = run_ansible(
            config,
            "shell",
            f"ping -c 2 -W 2 {vtep_ip} 2>&1 | tail -2",
            hosts="k3s_server[0]",
            timeout=30,
        )

        success = result.returncode == 0 and "0% packet loss" in result.stdout
        severity = Severity.HEALTHY if success else Severity.CRITICAL

        if success:
            rtt_match = re.search(r"rtt.*?=\s*([\d.]+)/([\d.]+)", result.stdout)
            rtt = f"{rtt_match.group(2)}ms" if rtt_match else "OK"
            print_status(f"{gpu.name[:30]} ({vtep_ip})", rtt, severity)
        else:
            print_status(f"{gpu.name[:30]} ({vtep_ip})", "UNREACHABLE", severity)


@flannel.command("diagnose")
@click.pass_context
def diagnose(ctx: click.Context) -> None:
    """Comprehensive Flannel health check.

    Runs all Flannel diagnostics and provides a summary of issues
    that could cause HTTP 503 errors for UserDeployments.
    """
    config: Config = ctx.obj

    print_header("Flannel Comprehensive Diagnostics")

    issues: list[tuple[str, str, Severity]] = []

    console.print("Checking flannel.1 interfaces...")
    interfaces = _get_flannel_interfaces(config)
    for iface in interfaces:
        if not iface.exists:
            issues.append((iface.node, "flannel.1 interface missing", Severity.CRITICAL))
        elif iface.state != "UP":
            issues.append((iface.node, f"flannel.1 state is {iface.state}", Severity.CRITICAL))
        elif iface.rx_dropped + iface.tx_dropped > 1000:
            issues.append((
                iface.node,
                f"High dropped packets: {iface.rx_dropped + iface.tx_dropped}",
                Severity.WARNING,
            ))

    console.print("Checking GPU node information...")
    gpu_nodes = _get_gpu_nodes(config)
    for gpu in gpu_nodes:
        if not gpu.flannel_mac:
            issues.append((gpu.name, "Missing flannel.1 MAC in K8s annotations", Severity.WARNING))
        if not gpu.pod_cidr:
            issues.append((gpu.name, "Missing pod CIDR", Severity.CRITICAL))

    console.print("Checking FDB entries...")
    fdb_entries = _get_fdb_entries(config)
    all_fdb_macs: set[str] = set()
    for entries in fdb_entries.values():
        all_fdb_macs.update(e.mac for e in entries)

    for gpu in gpu_nodes:
        if gpu.flannel_mac and gpu.flannel_mac not in all_fdb_macs:
            issues.append((gpu.name, f"Missing FDB entry for MAC {gpu.flannel_mac}", Severity.CRITICAL))

    console.print("Checking neighbor entries...")
    neighbor_entries = _get_neighbor_entries(config)
    all_vteps: set[str] = set()
    for entries in neighbor_entries.values():
        all_vteps.update(e.vtep_ip for e in entries)

    for gpu in gpu_nodes:
        if gpu.pod_cidr:
            vtep_ip = gpu.pod_cidr.replace("/24", "").rsplit(".", 1)[0] + ".0"
            if vtep_ip not in all_vteps:
                issues.append((gpu.name, f"Missing neighbor entry for {vtep_ip}", Severity.CRITICAL))

    console.print("Checking routes...")
    routes = _get_flannel_routes(config)
    all_routed_cidrs: set[str] = set()
    for server_routes in routes.values():
        all_routed_cidrs.update(r.pod_cidr for r in server_routes)

    for gpu in gpu_nodes:
        if gpu.pod_cidr and gpu.pod_cidr not in all_routed_cidrs:
            issues.append((gpu.name, f"Missing route for {gpu.pod_cidr}", Severity.CRITICAL))

    print_header("Diagnostic Summary")

    if not issues:
        console.print("[green]No Flannel issues detected[/green]")
        console.print(f"\nChecked {len(interfaces)} server(s), {len(gpu_nodes)} GPU node(s)")
        return

    critical = [i for i in issues if i[2] == Severity.CRITICAL]
    warnings = [i for i in issues if i[2] == Severity.WARNING]

    console.print(f"Found {len(issues)} issue(s): {len(critical)} critical, {len(warnings)} warnings\n")

    table = Table()
    table.add_column("Node", style="cyan")
    table.add_column("Issue")
    table.add_column("Severity")

    for node, issue, severity in issues:
        sev_color = "red" if severity == Severity.CRITICAL else "yellow"
        table.add_row(
            node[:25],
            issue,
            f"[{sev_color}]{severity.value}[/{sev_color}]",
        )

    console.print(table)

    if critical:
        print_header("Remediation Steps")
        console.print("Run 'clustermgr fix' to attempt automatic remediation")
        console.print("Or manually fix using:")
        console.print("  - Missing FDB: bridge fdb add <MAC> dev flannel.1 dst <WG_IP>")
        console.print("  - Missing neighbor: ip neigh add <VTEP_IP> lladdr <MAC> dev flannel.1")
        console.print("  - Missing route: ip route add <POD_CIDR> via <VTEP_IP> dev flannel.1 onlink")
        ctx.exit(1)


@flannel.command("mac-duplicates")
@click.pass_context
def mac_duplicates(ctx: click.Context) -> None:
    """Check for duplicate VtepMAC addresses across nodes.

    Duplicate MACs cause intermittent connectivity issues because
    VXLAN traffic gets routed to the wrong node. GPU nodes onboarded
    before v1.7.0 may have conflicting MACs.
    """
    config: Config = ctx.obj

    print_header("Duplicate VtepMAC Detection")

    gpu_nodes = _get_gpu_nodes(config)
    if not gpu_nodes:
        console.print("[yellow]No GPU nodes found[/yellow]")
        return

    # Group nodes by MAC
    mac_to_nodes: dict[str, list[GPUNodeInfo]] = {}
    for gpu in gpu_nodes:
        if gpu.flannel_mac:
            if gpu.flannel_mac not in mac_to_nodes:
                mac_to_nodes[gpu.flannel_mac] = []
            mac_to_nodes[gpu.flannel_mac].append(gpu)

    # Find duplicates
    duplicates = {mac: nodes for mac, nodes in mac_to_nodes.items() if len(nodes) > 1}

    if not duplicates:
        console.print(f"[green]No duplicate MACs found among {len(gpu_nodes)} GPU nodes[/green]")
        return

    console.print(f"[red]Found {len(duplicates)} duplicate MAC(s)![/red]\n")

    for mac, nodes in duplicates.items():
        print_header(f"Duplicate MAC: {mac}")

        table = Table()
        table.add_column("Node", style="cyan")
        table.add_column("WireGuard IP")
        table.add_column("Pod CIDR")

        for node in nodes:
            table.add_row(node.name[:30], node.wg_ip, node.pod_cidr)

        console.print(table)

    print_header("Resolution")
    console.print("For each conflicting node (except one), regenerate the VtepMAC:")
    console.print("")
    console.print("  1. Generate deterministic MAC from node name:")
    console.print("     NODE_NAME=$(hostname)")
    console.print("     HASH=$(echo -n \"$NODE_NAME\" | sha256sum | cut -c1-10)")
    console.print("     NEW_MAC=$(printf \"02:%s:%s:%s:%s:%s\" \"${HASH:0:2}\" \"${HASH:2:2}\" \"${HASH:4:2}\" \"${HASH:6:2}\" \"${HASH:8:2}\")")
    console.print("")
    console.print("  2. Recreate flannel.1 with new MAC")
    console.print("  3. Update K8s node annotation")
    console.print("  4. Update FDB/neighbor entries on K3s servers")
    console.print("")
    console.print("See FLANNEL-VXLAN-TROUBLESHOOTING.md for detailed steps")

    ctx.exit(1)


@flannel.command("capture")
@click.option("--interface", "-i", default="flannel.1", help="Interface to capture on")
@click.option("--count", "-c", default=50, help="Number of packets to capture")
@click.option("--filter", "-f", "pcap_filter", default="", help="tcpdump filter expression")
@click.option("--server", "-s", default="k3s_server[0]", help="Server to run capture on")
@click.pass_context
def capture(
    ctx: click.Context,
    interface: str,
    count: int,
    pcap_filter: str,
    server: str,
) -> None:
    """Capture packets on Flannel interface for debugging.

    Runs tcpdump on the specified interface to help diagnose
    VXLAN encapsulation and routing issues.
    """
    config: Config = ctx.obj

    print_header(f"Packet Capture on {interface}")

    console.print(f"Server: {server}")
    console.print(f"Interface: {interface}")
    console.print(f"Count: {count} packets")
    if pcap_filter:
        console.print(f"Filter: {pcap_filter}")

    console.print("\nCapturing...")

    cmd = f"sudo timeout 30 tcpdump -i {interface} -nn -c {count}"
    if pcap_filter:
        cmd += f" {pcap_filter}"
    cmd += " 2>&1"

    result = run_ansible(
        config,
        "shell",
        cmd,
        hosts=server,
        timeout=60,
    )

    if result.returncode != 0:
        console.print("[red]Capture failed[/red]")
        if result.stderr:
            console.print(result.stderr)
        ctx.exit(1)

    # Parse and display output
    in_output = False
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            in_output = True
            continue
        if in_output and line.strip():
            # Colorize common patterns
            text = line.strip()
            if "ICMP" in text:
                console.print(f"[cyan]{text}[/cyan]")
            elif "UDP" in text:
                console.print(f"[yellow]{text}[/yellow]")
            elif "ARP" in text:
                console.print(f"[green]{text}[/green]")
            elif "packets captured" in text or "packets received" in text:
                console.print(f"\n[bold]{text}[/bold]")
            else:
                console.print(text)


@flannel.command("vxlan-capture")
@click.option("--count", "-c", default=20, help="Number of packets to capture")
@click.option("--server", "-s", default="k3s_server[0]", help="Server to run capture on")
@click.pass_context
def vxlan_capture(
    ctx: click.Context,
    count: int,
    server: str,
) -> None:
    """Capture VXLAN encapsulated traffic (UDP 8472).

    Captures VXLAN-encapsulated packets on wg0 interface to verify
    that traffic is being properly tunneled through WireGuard.
    """
    config: Config = ctx.obj

    print_header("VXLAN Encapsulated Traffic Capture")

    console.print(f"Server: {server}")
    console.print(f"Capturing {count} packets on wg0 (UDP port 8472)")

    console.print("\nCapturing...")

    cmd = f"sudo timeout 30 tcpdump -i wg0 -nn udp port 8472 -c {count} 2>&1"

    result = run_ansible(
        config,
        "shell",
        cmd,
        hosts=server,
        timeout=60,
    )

    if result.returncode != 0:
        console.print("[red]Capture failed[/red]")
        if result.stderr:
            console.print(result.stderr)
        ctx.exit(1)

    in_output = False
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            in_output = True
            continue
        if in_output and line.strip():
            text = line.strip()
            if "packets captured" in text or "packets received" in text:
                console.print(f"\n[bold]{text}[/bold]")
            else:
                console.print(text)
