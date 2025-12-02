"""Cert-check command for clustermgr - certificate expiry checking."""

import re
from datetime import datetime, timezone

import click
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_ansible, run_kubectl, Severity, print_status

console = Console()

# Warning thresholds in days
CRITICAL_DAYS = 7
WARNING_DAYS = 30


def _parse_cert_date(date_str: str) -> datetime | None:
    """Parse certificate date from openssl output."""
    # Format: "Jan  1 00:00:00 2024 GMT"
    try:
        return datetime.strptime(date_str.strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(date_str.strip(), "%b  %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _check_k8s_certs(config: Config) -> list[dict]:
    """Check Kubernetes API server and component certificates."""
    certs: list[dict] = []

    # Check API server certificate via kubectl
    result = run_kubectl(
        config,
        ["get", "--raw", "/healthz"],
        timeout=10,
    )

    # Get certificate info from the kubeconfig
    result = run_ansible(
        config,
        "shell",
        (
            "for cert in /var/lib/rancher/k3s/server/tls/*.crt; do "
            "echo \"CERT:$cert\"; "
            "openssl x509 -in \"$cert\" -noout -dates 2>/dev/null; "
            "done"
        ),
        timeout=30,
    )

    current_server: str | None = None
    current_cert: str | None = None
    not_before: datetime | None = None
    not_after: datetime | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif "CERT:" in line:
            # Save previous cert if exists
            if current_server and current_cert and not_after:
                certs.append({
                    "server": current_server,
                    "cert": current_cert,
                    "not_before": not_before,
                    "not_after": not_after,
                })
            current_cert = line.replace("CERT:", "").strip()
            not_before = None
            not_after = None
        elif "notBefore=" in line:
            not_before = _parse_cert_date(line.replace("notBefore=", ""))
        elif "notAfter=" in line:
            not_after = _parse_cert_date(line.replace("notAfter=", ""))

    # Save last cert
    if current_server and current_cert and not_after:
        certs.append({
            "server": current_server,
            "cert": current_cert,
            "not_before": not_before,
            "not_after": not_after,
        })

    return certs


def _check_tls_secrets(config: Config) -> list[dict]:
    """Check TLS secrets in all namespaces."""
    result = run_kubectl(
        config,
        ["get", "secrets", "-A", "-o", "json"],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    certs: list[dict] = []

    for item in data.get("items", []):
        secret_type = item.get("type", "")
        if secret_type != "kubernetes.io/tls":
            continue

        metadata = item.get("metadata", {})
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")

        # Get certificate data
        cert_data = item.get("data", {}).get("tls.crt", "")
        if not cert_data:
            continue

        # Decode and check certificate via kubectl exec (base64 + openssl)
        check_result = run_kubectl(
            config,
            [
                "get", "secret", "-n", namespace, name,
                "-o", "jsonpath={.data.tls\\.crt}"
            ],
            timeout=10,
        )

        if check_result.returncode != 0:
            continue

        # Check certificate expiry
        import subprocess
        try:
            decode = subprocess.run(
                ["base64", "-d"],
                input=check_result.stdout.encode(),
                capture_output=True,
                timeout=5,
            )
            if decode.returncode != 0:
                continue

            openssl = subprocess.run(
                ["openssl", "x509", "-noout", "-dates"],
                input=decode.stdout,
                capture_output=True,
                timeout=5,
            )
            if openssl.returncode != 0:
                continue

            not_before = None
            not_after = None
            for line in openssl.stdout.decode().split("\n"):
                if "notBefore=" in line:
                    not_before = _parse_cert_date(line.replace("notBefore=", ""))
                elif "notAfter=" in line:
                    not_after = _parse_cert_date(line.replace("notAfter=", ""))

            if not_after:
                certs.append({
                    "type": "secret",
                    "namespace": namespace,
                    "name": name,
                    "not_before": not_before,
                    "not_after": not_after,
                })
        except (subprocess.TimeoutExpired, Exception):
            continue

    return certs


def _check_wireguard_certs(config: Config) -> list[dict]:
    """Check if WireGuard keys have any expiry (they don't, but check config age)."""
    result = run_ansible(
        config,
        "shell",
        "stat -c '%Y' /etc/wireguard/wg0.conf 2>/dev/null || echo 'NONE'",
        timeout=30,
    )

    configs: list[dict] = []
    current_server: str | None = None

    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
        elif current_server and line.strip().isdigit():
            timestamp = int(line.strip())
            created = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            configs.append({
                "server": current_server,
                "type": "wireguard",
                "created": created,
            })

    return configs


def _days_until(dt: datetime) -> int:
    """Calculate days until a datetime."""
    now = datetime.now(timezone.utc)
    delta = dt - now
    return delta.days


@click.command("cert-check")
@click.option("--secrets", "-s", is_flag=True, help="Check TLS secrets in cluster")
@click.option("--wireguard", "-w", is_flag=True, help="Check WireGuard config age")
@click.option("--days", "-d", default=WARNING_DAYS, help=f"Warning threshold in days (default: {WARNING_DAYS})")
@click.pass_context
def cert_check(
    ctx: click.Context,
    secrets: bool,
    wireguard: bool,
    days: int,
) -> None:
    """Check certificate expiry dates."""
    config: Config = ctx.obj

    print_header("Certificate Expiry Check")

    all_certs: list[dict] = []
    expiring: list[dict] = []

    # Check K3s certificates
    console.print("Checking K3s certificates...")
    k3s_certs = _check_k8s_certs(config)
    for cert in k3s_certs:
        cert["type"] = "k3s"
        all_certs.append(cert)

    # Check TLS secrets
    if secrets:
        console.print("Checking TLS secrets...")
        secret_certs = _check_tls_secrets(config)
        all_certs.extend(secret_certs)

    # Check WireGuard
    if wireguard:
        console.print("Checking WireGuard configs...")
        wg_configs = _check_wireguard_certs(config)
        for wg in wg_configs:
            # WireGuard keys don't expire, but we note config age
            all_certs.append({
                "type": "wireguard",
                "server": wg["server"],
                "name": "wg0.conf",
                "created": wg["created"],
                "not_after": None,  # No expiry
            })

    if not all_certs:
        console.print("[yellow]No certificates found to check[/yellow]")
        return

    # Categorize by expiry
    now = datetime.now(timezone.utc)

    for cert in all_certs:
        not_after = cert.get("not_after")
        if not_after:
            days_left = _days_until(not_after)
            cert["days_left"] = days_left

            if days_left < 0:
                cert["status"] = "expired"
                cert["severity"] = Severity.CRITICAL
                expiring.append(cert)
            elif days_left < CRITICAL_DAYS:
                cert["status"] = "critical"
                cert["severity"] = Severity.CRITICAL
                expiring.append(cert)
            elif days_left < days:
                cert["status"] = "warning"
                cert["severity"] = Severity.WARNING
                expiring.append(cert)
            else:
                cert["status"] = "ok"
                cert["severity"] = None

    # Summary table
    table = Table(title="Certificate Status")
    table.add_column("Type", style="cyan")
    table.add_column("Name/Path")
    table.add_column("Server")
    table.add_column("Expires", justify="right")
    table.add_column("Days Left", justify="right")
    table.add_column("Status")

    # Sort by days left (expired first)
    sorted_certs = sorted(all_certs, key=lambda x: x.get("days_left", 9999))

    for cert in sorted_certs[:30]:  # Limit output
        cert_type = cert.get("type", "unknown")
        name = cert.get("name") or cert.get("cert", "").split("/")[-1]
        server = cert.get("server", cert.get("namespace", "-"))
        not_after = cert.get("not_after")

        if not_after:
            expires_str = not_after.strftime("%Y-%m-%d")
            days_left = cert.get("days_left", 0)

            if days_left < 0:
                status = "[red]EXPIRED[/red]"
                days_str = f"[red]{days_left}[/red]"
            elif days_left < CRITICAL_DAYS:
                status = "[red]CRITICAL[/red]"
                days_str = f"[red]{days_left}[/red]"
            elif days_left < days:
                status = "[yellow]WARNING[/yellow]"
                days_str = f"[yellow]{days_left}[/yellow]"
            else:
                status = "[green]OK[/green]"
                days_str = str(days_left)
        else:
            expires_str = "-"
            days_str = "-"
            status = "[dim]N/A[/dim]"

        table.add_row(cert_type, name, server, expires_str, days_str, status)

    console.print(table)

    if len(sorted_certs) > 30:
        console.print(f"[dim]... and {len(sorted_certs) - 30} more certificates[/dim]")

    # Show expiring/expired certificates
    if expiring:
        print_header("Expiring/Expired Certificates")

        expired = [c for c in expiring if c["status"] == "expired"]
        critical = [c for c in expiring if c["status"] == "critical"]
        warning = [c for c in expiring if c["status"] == "warning"]

        if expired:
            console.print("\n[bold red]EXPIRED:[/bold red]")
            for cert in expired:
                name = cert.get("name") or cert.get("cert", "").split("/")[-1]
                console.print(f"  [red]{name}[/red] on {cert.get('server', 'N/A')}")

        if critical:
            console.print(f"\n[bold red]Critical (<{CRITICAL_DAYS} days):[/bold red]")
            for cert in critical:
                name = cert.get("name") or cert.get("cert", "").split("/")[-1]
                console.print(f"  [red]{name}[/red] - {cert['days_left']} days left")

        if warning:
            console.print(f"\n[bold yellow]Warning (<{days} days):[/bold yellow]")
            for cert in warning:
                name = cert.get("name") or cert.get("cert", "").split("/")[-1]
                console.print(f"  [yellow]{name}[/yellow] - {cert['days_left']} days left")

    # Summary
    expired_count = sum(1 for c in expiring if c["status"] == "expired")
    critical_count = sum(1 for c in expiring if c["status"] == "critical")
    warning_count = sum(1 for c in expiring if c["status"] == "warning")

    console.print(f"\n[bold]Summary:[/bold] {len(all_certs)} certificates checked")
    console.print(f"  Expired: {expired_count}, Critical: {critical_count}, Warning: {warning_count}")

    if expired_count > 0 or critical_count > 0:
        console.print("\n[bold red]Action required: Renew expired/expiring certificates[/bold red]")
        console.print("  K3s certificates: Run 'k3s certificate rotate' on the server")
        console.print("  TLS secrets: Renew via cert-manager or manual update")
        ctx.exit(1)
    elif warning_count > 0:
        console.print("\n[yellow]Plan certificate renewal within the next 30 days[/yellow]")
    else:
        console.print("\n[green]All certificates are valid[/green]")
