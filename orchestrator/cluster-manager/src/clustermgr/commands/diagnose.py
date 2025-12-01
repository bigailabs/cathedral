"""Diagnose command for clustermgr."""

import re
from datetime import datetime

import click
from rich.console import Console

from clustermgr.commands.health import check_iptables_drops, check_nodes
from clustermgr.config import Config
from clustermgr.utils import (
    Severity,
    print_header,
    print_status,
    run_ansible,
    run_kubectl,
)

console = Console()

# Thresholds for interface issues
DROPPED_PACKETS_WARNING = 100
DROPPED_PACKETS_CRITICAL = 1000


def diagnose_502(config: Config) -> list[dict]:
    """Diagnose 502 Bad Gateway errors."""
    findings: list[dict] = []

    print_header("Diagnosing 502 Bad Gateway Issues")

    console.print("\n  Testing kubectl logs on all nodes...")
    nodes = check_nodes(config)

    for node in nodes:
        result = run_kubectl(
            config,
            [
                "get",
                "pods",
                "-A",
                "--field-selector",
                f"spec.nodeName={node['name']}",
                "-o",
                "jsonpath={.items[0].metadata.namespace}/{.items[0].metadata.name}",
            ],
            timeout=10,
        )
        if result.returncode == 0 and "/" in result.stdout:
            ns, pod = result.stdout.strip().split("/", 1)
            log_result = run_kubectl(
                config,
                ["logs", "-n", ns, pod, "--tail=1"],
                timeout=10,
            )
            if log_result.returncode != 0 and "502" in log_result.stderr:
                findings.append({
                    "type": "502_error",
                    "node": node["name"],
                    "pod": f"{ns}/{pod}",
                    "error": log_result.stderr[:200],
                })
                print_status(node["name"], "502 Bad Gateway", Severity.CRITICAL)
            else:
                print_status(node["name"], "OK", Severity.HEALTHY)

    return findings


def diagnose_connectivity(config: Config) -> list[dict]:
    """Test bidirectional connectivity."""
    findings: list[dict] = []

    print_header("Testing Bidirectional Connectivity")

    result = run_ansible(
        config,
        "shell",
        (
            "for ip in $(sudo wg show wg0 | grep 'allowed ips' | grep '10.200' | "
            "awk '{print $3}' | cut -d'/' -f1 | head -5); do "
            'echo -n "$ip: "; ping -c 1 -W 2 $ip 2>&1 | grep -E \'bytes from|100%\' | head -1; done'
        ),
        timeout=60,
    )

    current_server: str | None = None
    for line in result.stdout.split("\n"):
        line = line.strip()
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and ":" in line and "10.200" in line:
            ip = line.split(":")[0].strip()
            status = line.split(":", 1)[1].strip() if ":" in line else ""
            if "100%" in status or "Unreachable" in status:
                findings.append({
                    "type": "ping_failed",
                    "server": current_server,
                    "target": ip,
                })
                print_status(f"{current_server} -> {ip}", "FAILED", Severity.CRITICAL)
            elif "bytes from" in status:
                print_status(f"{current_server} -> {ip}", "OK", Severity.HEALTHY)

    return findings


def diagnose_interface_health(config: Config) -> list[dict]:
    """Check WireGuard interface for dropped packets and errors.

    Dropped packets indicate the kernel is discarding traffic, which can cause:
    - Intermittent connectivity issues
    - Slow or failed pod scheduling on remote nodes
    - Timeout errors in kubelet communication
    """
    findings: list[dict] = []

    print_header("Checking WireGuard Interface Health")

    result = run_ansible(
        config,
        "shell",
        "ip -s link show wg0 2>/dev/null || echo 'NO_WG'",
        timeout=30,
    )

    current_server: str | None = None
    current_output: list[str] = []

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            # Process previous server's output
            if current_server and current_output:
                _process_interface_stats(
                    current_server, "\n".join(current_output), findings
                )
            current_server = line.split(" | ")[0].strip()
            current_output = []
        elif current_server:
            current_output.append(line)

    # Process last server
    if current_server and current_output:
        _process_interface_stats(current_server, "\n".join(current_output), findings)

    return findings


def _process_interface_stats(server: str, output: str, findings: list[dict]) -> None:
    """Parse interface stats and add findings for issues."""
    if "NO_WG" in output:
        findings.append({
            "type": "interface_missing",
            "server": server,
            "issue": "WireGuard interface wg0 not found",
        })
        print_status(server, "wg0 interface missing", Severity.CRITICAL)
        return

    # Parse RX stats: bytes packets errors dropped
    rx_match = re.search(
        r"RX:\s+bytes\s+packets\s+errors\s+dropped.*?\n\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        output,
    )
    # Parse TX stats: bytes packets errors dropped
    tx_match = re.search(
        r"TX:\s+bytes\s+packets\s+errors\s+dropped.*?\n\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        output,
    )

    rx_dropped = int(rx_match.group(4)) if rx_match else 0
    tx_dropped = int(tx_match.group(4)) if tx_match else 0
    rx_packets = int(rx_match.group(2)) if rx_match else 0
    tx_packets = int(tx_match.group(2)) if tx_match else 0
    total_dropped = rx_dropped + tx_dropped
    total_packets = rx_packets + tx_packets

    # Calculate drop rate for context
    drop_rate = (total_dropped / total_packets * 100) if total_packets > 0 else 0

    if total_dropped >= DROPPED_PACKETS_CRITICAL:
        findings.append({
            "type": "dropped_packets",
            "server": server,
            "rx_dropped": rx_dropped,
            "tx_dropped": tx_dropped,
            "total_packets": total_packets,
            "severity": "critical",
        })
        print_status(
            server,
            f"CRITICAL: {total_dropped} dropped packets ({drop_rate:.2f}% loss)",
            Severity.CRITICAL,
        )
    elif total_dropped >= DROPPED_PACKETS_WARNING:
        findings.append({
            "type": "dropped_packets",
            "server": server,
            "rx_dropped": rx_dropped,
            "tx_dropped": tx_dropped,
            "total_packets": total_packets,
            "severity": "warning",
        })
        print_status(
            server,
            f"WARNING: {total_dropped} dropped packets ({drop_rate:.2f}% loss)",
            Severity.WARNING,
        )
    else:
        print_status(server, f"OK ({total_dropped} dropped)", Severity.HEALTHY)


# Remediation guidance for each issue type
REMEDIATION_GUIDANCE: dict[str, dict[str, str]] = {
    "dropped_packets": {
        "description": "Packets are being dropped by the kernel on the WireGuard interface",
        "causes": (
            "- TX queue overflow (high traffic bursts)\n"
            "- Network buffer exhaustion\n"
            "- CPU unable to process packets fast enough\n"
            "- MTU mismatch causing fragmentation issues"
        ),
        "actions": (
            "1. Check system load: 'uptime' and 'top' on affected servers\n"
            "2. Increase TX queue length: 'ip link set wg0 txqueuelen 1000'\n"
            "3. Check for MTU issues: 'ping -M do -s 1392 <peer_ip>'\n"
            "4. Monitor with: 'watch -n1 ip -s link show wg0'\n"
            "5. If persistent, consider increasing network buffers:\n"
            "   sysctl -w net.core.wmem_max=16777216\n"
            "   sysctl -w net.core.rmem_max=16777216"
        ),
    },
    "rate_limit": {
        "description": "iptables rate limiting is blocking WireGuard handshakes",
        "causes": (
            "- Overly aggressive rate limit rules on UDP port 51820\n"
            "- Rules blocking legitimate WireGuard key renegotiation"
        ),
        "actions": (
            "1. Run 'clustermgr fix' to remove rate limit rules\n"
            "2. Or manually: iptables -D INPUT -p udp --dport 51820 ... -j DROP\n"
            "3. Save changes: iptables-save > /etc/iptables.rules.v4"
        ),
    },
    "ping_failed": {
        "description": "Cannot reach WireGuard peer via ICMP ping",
        "causes": (
            "- WireGuard tunnel not established\n"
            "- Firewall blocking traffic\n"
            "- Routing misconfiguration"
        ),
        "actions": (
            "1. Check WireGuard status: 'wg show wg0'\n"
            "2. Verify handshake: look for 'latest handshake' timestamp\n"
            "3. Restart WireGuard: 'systemctl restart wg-quick@wg0'\n"
            "4. Check routing: 'ip route get <peer_ip>'"
        ),
    },
    "502_error": {
        "description": "Kubelet returning 502 Bad Gateway errors",
        "causes": (
            "- Kubelet unreachable on remote node\n"
            "- WireGuard tunnel down or unstable\n"
            "- API server cannot proxy to kubelet"
        ),
        "actions": (
            "1. Check node status: 'kubectl get nodes'\n"
            "2. Check WireGuard: 'clustermgr wg status'\n"
            "3. Restart WireGuard: 'clustermgr wg restart'\n"
            "4. Check kubelet: 'systemctl status kubelet' on the node"
        ),
    },
    "interface_missing": {
        "description": "WireGuard interface wg0 does not exist",
        "causes": (
            "- WireGuard service not running\n"
            "- WireGuard not installed\n"
            "- Configuration file missing"
        ),
        "actions": (
            "1. Check service: 'systemctl status wg-quick@wg0'\n"
            "2. Start service: 'systemctl start wg-quick@wg0'\n"
            "3. Verify config: 'cat /etc/wireguard/wg0.conf'\n"
            "4. Re-deploy via Ansible if needed"
        ),
    },
}


def _print_remediation_guidance(issue_types: set[str]) -> None:
    """Print remediation guidance for found issues."""
    print_header("Remediation Guidance")

    for issue_type in issue_types:
        if issue_type not in REMEDIATION_GUIDANCE:
            continue

        guidance = REMEDIATION_GUIDANCE[issue_type]
        console.print(f"\n[bold yellow]{issue_type}[/bold yellow]")
        console.print(f"  [dim]{guidance['description']}[/dim]\n")
        console.print("  [bold]Possible causes:[/bold]")
        for line in guidance["causes"].split("\n"):
            console.print(f"  {line}")
        console.print("\n  [bold]Recommended actions:[/bold]")
        for line in guidance["actions"].split("\n"):
            console.print(f"  {line}")


@click.command()
@click.pass_context
def diagnose(ctx: click.Context) -> None:
    """Run comprehensive diagnostics."""
    config: Config = ctx.obj

    print_header("Cluster Diagnostics")
    console.print(f"Timestamp: {datetime.now().isoformat()}")

    all_findings: list[dict] = []

    # Run all diagnostic checks
    all_findings.extend(diagnose_502(config))
    all_findings.extend(diagnose_connectivity(config))
    all_findings.extend(diagnose_interface_health(config))

    # Check iptables
    print_header("Checking iptables Rules")
    drops = check_iptables_drops(config)
    for server, info in drops.items():
        if info["has_rate_limit"]:
            all_findings.append({
                "type": "rate_limit",
                "server": server,
                "drops": info["drops"],
            })
            print_status(server, f"Rate limit with {info['drops']} drops", Severity.CRITICAL)
        else:
            print_status(server, "No rate limit rules", Severity.HEALTHY)

    # Summary
    print_header("Diagnostic Summary")
    if all_findings:
        console.print(f"\n[red]Found {len(all_findings)} issue(s):[/red]")

        by_type: dict[str, list[dict]] = {}
        for f in all_findings:
            t = f["type"]
            by_type.setdefault(t, []).append(f)

        for issue_type, items in by_type.items():
            console.print(f"\n  [yellow]{issue_type}[/yellow] ({len(items)} occurrences):")
            for item in items[:3]:
                details = ", ".join(f"{k}={v}" for k, v in item.items() if k != "type")
                console.print(f"    - {details}")
            if len(items) > 3:
                console.print(f"    ... and {len(items) - 3} more")

        # Print remediation guidance
        _print_remediation_guidance(set(by_type.keys()))

        console.print(
            "\n[bold]Quick fix:[/bold] "
            "Run 'clustermgr fix --dry-run' to see automated remediation plan"
        )
        ctx.exit(1)
    else:
        console.print("\n[green]No issues found![/green]")
