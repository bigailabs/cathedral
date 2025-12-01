"""Firewall command for clustermgr - iptables audit and analysis."""

import re

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import print_header, print_status, run_ansible, Severity

console = Console()

# Known problematic rule patterns
PROBLEMATIC_PATTERNS = [
    {
        "pattern": r"-p udp.*--dport 51820.*-j DROP",
        "name": "WireGuard rate-limit DROP",
        "severity": Severity.CRITICAL,
        "description": "Blocks WireGuard handshakes causing connectivity issues",
        "fix": "Remove this rule to restore WireGuard connectivity",
    },
    {
        "pattern": r"-p udp.*--dport 51820.*-m limit.*-j DROP",
        "name": "WireGuard rate-limit with counter",
        "severity": Severity.CRITICAL,
        "description": "Rate limiting WireGuard can block key renegotiation",
        "fix": "Remove rate limiting on WireGuard port 51820",
    },
    {
        "pattern": r"-A INPUT -j DROP",
        "name": "Default DROP on INPUT",
        "severity": Severity.WARNING,
        "description": "May block legitimate traffic if rules are incomplete",
        "fix": "Ensure all required ports are explicitly allowed before DROP",
    },
    {
        "pattern": r"-A FORWARD -j DROP",
        "name": "Default DROP on FORWARD",
        "severity": Severity.WARNING,
        "description": "May block pod-to-pod traffic across nodes",
        "fix": "Ensure Flannel/CNI traffic is allowed before DROP",
    },
]

# Required ports for K3s + WireGuard
REQUIRED_PORTS = [
    {"port": 6443, "proto": "tcp", "desc": "Kubernetes API server"},
    {"port": 10250, "proto": "tcp", "desc": "Kubelet API"},
    {"port": 51820, "proto": "udp", "desc": "WireGuard VPN"},
    {"port": 8472, "proto": "udp", "desc": "Flannel VXLAN"},
    {"port": 2379, "proto": "tcp", "desc": "etcd clients"},
    {"port": 2380, "proto": "tcp", "desc": "etcd peers"},
]


def _get_iptables_rules(config: Config) -> dict[str, dict]:
    """Get iptables rules from all servers."""
    result = run_ansible(
        config,
        "shell",
        "sudo iptables -S 2>/dev/null | head -100",
        timeout=30,
    )

    servers: dict[str, dict] = {}
    current_server: str | None = None
    current_rules: list[str] = []

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            if current_server and current_rules:
                servers[current_server] = {"rules": current_rules}
            current_server = line.split(" | ")[0].strip()
            current_rules = []
        elif current_server and line.strip().startswith("-"):
            current_rules.append(line.strip())

    if current_server and current_rules:
        servers[current_server] = {"rules": current_rules}

    return servers


def _analyze_rules(rules: list[str]) -> dict:
    """Analyze iptables rules for issues."""
    issues: list[dict] = []
    port_status: dict[int, bool] = {}

    for rule in rules:
        # Check for problematic patterns
        for pattern_info in PROBLEMATIC_PATTERNS:
            if re.search(pattern_info["pattern"], rule):
                issues.append({
                    "rule": rule,
                    "name": pattern_info["name"],
                    "severity": pattern_info["severity"],
                    "description": pattern_info["description"],
                    "fix": pattern_info["fix"],
                })

        # Check if required ports are allowed
        for port_info in REQUIRED_PORTS:
            port = port_info["port"]
            proto = port_info["proto"]
            if f"--dport {port}" in rule and f"-p {proto}" in rule and "-j ACCEPT" in rule:
                port_status[port] = True

    return {
        "issues": issues,
        "port_status": port_status,
        "rule_count": len(rules),
    }


def _get_drop_counters(config: Config) -> dict[str, int]:
    """Get packet drop counters from iptables."""
    result = run_ansible(
        config,
        "shell",
        "sudo iptables -L -v -n 2>/dev/null | grep -E 'DROP|REJECT' | awk '{print $1}' | head -10",
        timeout=30,
    )

    servers: dict[str, int] = {}
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            servers[current_server] = 0
        elif current_server and line.strip().isdigit():
            servers[current_server] += int(line.strip())

    return servers


@click.command()
@click.option("--check-ports", "-p", is_flag=True, help="Check if required ports are allowed")
@click.option("--show-rules", "-r", is_flag=True, help="Show all iptables rules")
@click.option("--drops", "-d", is_flag=True, help="Show DROP rule counters")
@click.pass_context
def firewall(
    ctx: click.Context,
    check_ports: bool,
    show_rules: bool,
    drops: bool,
) -> None:
    """Audit iptables rules for potential issues."""
    config: Config = ctx.obj

    print_header("Firewall Audit")

    servers = _get_iptables_rules(config)
    if not servers:
        console.print("[red]Failed to retrieve iptables rules[/red]")
        ctx.exit(1)

    all_issues: list[dict] = []
    missing_ports: dict[str, list[int]] = {}

    # Analyze each server
    for server, data in servers.items():
        analysis = _analyze_rules(data["rules"])
        data["analysis"] = analysis

        for issue in analysis["issues"]:
            issue["server"] = server
            all_issues.append(issue)

        # Track missing required ports
        if check_ports:
            missing = []
            for port_info in REQUIRED_PORTS:
                if port_info["port"] not in analysis["port_status"]:
                    missing.append(port_info["port"])
            if missing:
                missing_ports[server] = missing

    # Summary table
    table = Table(title="Server Firewall Status")
    table.add_column("Server", style="cyan")
    table.add_column("Rules", justify="right")
    table.add_column("Issues", justify="right")
    table.add_column("Status")

    for server, data in servers.items():
        analysis = data["analysis"]
        issue_count = len(analysis["issues"])

        if issue_count == 0:
            status = "[green]OK[/green]"
        elif any(i["severity"] == Severity.CRITICAL for i in analysis["issues"]):
            status = "[red]CRITICAL[/red]"
        else:
            status = "[yellow]WARNING[/yellow]"

        table.add_row(
            server,
            str(analysis["rule_count"]),
            str(issue_count) if issue_count > 0 else "-",
            status,
        )

    console.print(table)

    # Show issues
    if all_issues:
        print_header("Detected Issues")
        for issue in all_issues:
            severity_color = "red" if issue["severity"] == Severity.CRITICAL else "yellow"
            console.print(f"\n[{severity_color}][{issue['severity'].name}][/{severity_color}] {issue['server']}")
            console.print(f"  [bold]{issue['name']}[/bold]")
            console.print(f"  Rule: [dim]{issue['rule'][:80]}...[/dim]" if len(issue['rule']) > 80 else f"  Rule: [dim]{issue['rule']}[/dim]")
            console.print(f"  {issue['description']}")
            console.print(f"  [green]Fix:[/green] {issue['fix']}")

    # Show required ports status
    if check_ports:
        print_header("Required Ports Check")
        port_table = Table()
        port_table.add_column("Port", justify="right")
        port_table.add_column("Protocol")
        port_table.add_column("Description")
        port_table.add_column("Status")

        for port_info in REQUIRED_PORTS:
            # Check if any server is missing this port
            missing_on = [s for s, ports in missing_ports.items() if port_info["port"] in ports]
            if missing_on:
                status = f"[yellow]Missing on: {', '.join(missing_on)}[/yellow]"
            else:
                status = "[green]Allowed[/green]"

            port_table.add_row(
                str(port_info["port"]),
                port_info["proto"].upper(),
                port_info["desc"],
                status,
            )

        console.print(port_table)

    # Show DROP counters
    if drops:
        print_header("DROP Rule Counters")
        drop_counters = _get_drop_counters(config)

        for server, count in sorted(drop_counters.items()):
            if count > 1000:
                print_status(server, f"{count:,} packets dropped", Severity.WARNING)
            elif count > 0:
                print_status(server, f"{count:,} packets dropped", Severity.HEALTHY)
            else:
                print_status(server, "No drops", Severity.HEALTHY)

    # Show all rules if requested
    if show_rules:
        print_header("All iptables Rules")
        for server, data in servers.items():
            console.print(f"\n[bold cyan]{server}[/bold cyan]")
            for rule in data["rules"][:50]:
                console.print(f"  {rule}")
            if len(data["rules"]) > 50:
                console.print(f"  [dim]... and {len(data['rules']) - 50} more rules[/dim]")

    # Summary
    if all_issues:
        critical = sum(1 for i in all_issues if i["severity"] == Severity.CRITICAL)
        warnings = len(all_issues) - critical
        console.print(f"\n[bold]Summary:[/bold] {critical} critical, {warnings} warnings")
        if critical > 0:
            console.print("[bold]Run 'clustermgr fix' to remediate critical issues[/bold]")
        ctx.exit(1)
    else:
        console.print("\n[green]No firewall issues detected[/green]")
