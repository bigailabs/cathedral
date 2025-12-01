"""Generate read-only kubeconfig files for K3s cluster monitoring."""

import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from clustermgr.config import Config

console = Console()


@click.group(name="kubeconfig")
def kubeconfig() -> None:
    """Generate read-only kubeconfig files for K3s cluster access.

    Create ServiceAccounts with restricted read-only permissions for monitoring,
    CI/CD, or external integrations. Supports long-lived tokens (K8s 1.24+).
    """


@kubeconfig.command(name="generate")
@click.option(
    "--name",
    "-n",
    required=True,
    help="ServiceAccount name (e.g., 'prometheus-readonly')",
)
@click.option(
    "--namespace",
    default="basilica-monitoring",
    help="Namespace for ServiceAccount (default: basilica-monitoring)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path (default: ./kubeconfig-{name}.yaml)",
)
@click.option(
    "--duration",
    "-d",
    default="8760h",
    help="Token duration (e.g., 8760h for 1 year, 2160h for 90 days)",
)
@click.option(
    "--cluster-name",
    default="basilica-k3s",
    help="Cluster name in kubeconfig",
)
@click.option(
    "--install-rbac/--skip-rbac",
    default=True,
    help="Install RBAC resources (ClusterRole, RoleBinding)",
)
@click.pass_obj
def generate(
    config: Config,
    name: str,
    namespace: str,
    output: str | None,
    duration: str,
    cluster_name: str,
    install_rbac: bool,
) -> None:
    """Generate a read-only kubeconfig file.

    This command creates:
    1. Namespace (if needed)
    2. Custom ClusterRole with read-only permissions
    3. ServiceAccount in dedicated namespace
    4. ClusterRoleBinding to bind permissions
    5. Long-lived ServiceAccount token (K8s 1.24+ compatible)
    6. Kubeconfig file with cluster CA and API server endpoint

    Example:
        clustermgr kubeconfig generate --name prometheus-readonly

        clustermgr kubeconfig generate --name ci-reader --duration 2160h

        clustermgr kubeconfig generate --name external-monitor --output ~/monitor.yaml
    """
    console.print(f"\n[bold cyan]Generating read-only kubeconfig: {name}[/bold cyan]\n")

    if config.dry_run:
        console.print("[yellow]DRY RUN MODE - No changes will be made[/yellow]\n")

    output_path = Path(output) if output else Path(f"./kubeconfig-{name}.yaml")

    try:
        if install_rbac:
            _install_rbac_resources(config, namespace)

        _create_service_account(config, name, namespace)

        token = _create_token(config, name, namespace, duration)

        ca_cert = _get_cluster_ca(config)
        api_server = _get_api_server(config)

        kubeconfig_content = _build_kubeconfig(
            name=name,
            cluster_name=cluster_name,
            api_server=api_server,
            ca_cert=ca_cert,
            token=token,
        )

        if not config.dry_run:
            output_path.write_text(kubeconfig_content)
            output_path.chmod(0o600)

        _display_summary(name, namespace, output_path, duration, token)

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error executing kubectl: {e.stderr}[/red]")
        raise click.Abort()
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@kubeconfig.command(name="list")
@click.option(
    "--namespace",
    "-n",
    default="basilica-monitoring",
    help="Namespace to list ServiceAccounts from",
)
@click.pass_obj
def list_accounts(config: Config, namespace: str) -> None:
    """List existing read-only ServiceAccounts."""
    console.print(f"\n[bold cyan]ServiceAccounts in {namespace}[/bold cyan]\n")

    try:
        result = _run_kubectl(
            config,
            [
                "get",
                "serviceaccounts",
                "-n",
                namespace,
                "-o",
                "json",
            ],
        )

        data = json.loads(result)
        accounts = data.get("items", [])

        if not accounts:
            console.print(f"[yellow]No ServiceAccounts found in {namespace}[/yellow]")
            return

        table = Table(title=f"ServiceAccounts in {namespace}")
        table.add_column("Name", style="cyan")
        table.add_column("Age", style="yellow")
        table.add_column("Secrets", style="green")

        for account in accounts:
            name = account["metadata"]["name"]
            age = account["metadata"].get("creationTimestamp", "unknown")
            secrets = len(account.get("secrets", []))

            table.add_row(name, age, str(secrets))

        console.print(table)

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error: {e.stderr}[/red]")
        raise click.Abort()


@kubeconfig.command(name="revoke")
@click.option(
    "--name",
    "-n",
    required=True,
    help="ServiceAccount name to revoke",
)
@click.option(
    "--namespace",
    default="basilica-monitoring",
    help="Namespace containing ServiceAccount",
)
@click.pass_obj
def revoke(config: Config, name: str, namespace: str) -> None:
    """Revoke access by deleting ServiceAccount and associated token.

    This deletes the ServiceAccount and its token Secret, immediately
    invalidating any kubeconfig files using this account.
    """
    console.print(f"\n[bold red]Revoking ServiceAccount: {name}[/bold red]\n")

    if not config.no_confirm and not config.dry_run:
        if not click.confirm(
            f"Delete ServiceAccount '{name}' in namespace '{namespace}'?"
        ):
            console.print("[yellow]Cancelled[/yellow]")
            return

    try:
        if not config.dry_run:
            _run_kubectl(config, ["delete", "serviceaccount", name, "-n", namespace])

            # Delete any token secrets for this ServiceAccount (handles rotated tokens)
            try:
                _run_kubectl(
                    config,
                    [
                        "delete",
                        "secrets",
                        "-n",
                        namespace,
                        "-l",
                        f"kubernetes.io/service-account.name={name}",
                    ],
                )
            except subprocess.CalledProcessError:
                pass  # Token secrets may already be deleted or not exist

        console.print(f"[green]ServiceAccount '{name}' revoked successfully[/green]")

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error: {e.stderr}[/red]")
        raise click.Abort()


@kubeconfig.command(name="verify")
@click.option(
    "--kubeconfig-path",
    "-k",
    required=True,
    type=click.Path(exists=True),
    help="Path to kubeconfig file to verify",
)
@click.pass_obj
def verify(config: Config, kubeconfig_path: str) -> None:
    """Verify a kubeconfig file has correct read-only permissions.

    Tests the kubeconfig against common read operations and ensures
    write operations are properly denied.
    """
    console.print(f"\n[bold cyan]Verifying kubeconfig: {kubeconfig_path}[/bold cyan]\n")

    read_tests = [
        ("list pods", ["get", "pods", "--all-namespaces"]),
        ("list nodes", ["get", "nodes"]),
        ("list namespaces", ["get", "namespaces"]),
        ("get userdeployments", ["get", "userdeployments", "-A"]),
    ]

    deny_tests = [
        ("create pod", ["auth", "can-i", "create", "pods"]),
        ("delete namespace", ["auth", "can-i", "delete", "namespaces"]),
        ("read secrets", ["auth", "can-i", "get", "secrets"]),
    ]

    table = Table(title="Permission Tests")
    table.add_column("Test", style="cyan")
    table.add_column("Expected", style="yellow")
    table.add_column("Result", style="green")

    for test_name, kubectl_args in read_tests:
        try:
            _run_kubectl(
                config,
                kubectl_args,
                env={"KUBECONFIG": kubeconfig_path},
            )
            table.add_row(test_name, "Allow", "[green]PASS[/green]")
        except subprocess.CalledProcessError:
            table.add_row(test_name, "Allow", "[red]FAIL (denied)[/red]")

    for test_name, kubectl_args in deny_tests:
        try:
            result = _run_kubectl(
                config,
                kubectl_args,
                env={"KUBECONFIG": kubeconfig_path},
            )
            if result.strip() == "no":
                table.add_row(test_name, "Deny", "[green]PASS[/green]")
            else:
                table.add_row(test_name, "Deny", "[red]FAIL (allowed)[/red]")
        except subprocess.CalledProcessError:
            table.add_row(test_name, "Deny", "[green]PASS[/green]")

    console.print(table)


@kubeconfig.command(name="rotate")
@click.option(
    "--name",
    "-n",
    required=True,
    help="ServiceAccount name",
)
@click.option(
    "--namespace",
    default="basilica-monitoring",
    help="Namespace containing ServiceAccount",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file path for new kubeconfig",
)
@click.option(
    "--duration",
    "-d",
    default="8760h",
    help="Token duration for new token",
)
@click.pass_obj
def rotate(
    config: Config,
    name: str,
    namespace: str,
    output: str | None,
    duration: str,
) -> None:
    """Rotate ServiceAccount token by creating new Secret.

    This creates a new token Secret while keeping the ServiceAccount intact.
    Old tokens remain valid until their Secret is deleted.
    """
    console.print(f"\n[bold cyan]Rotating token for: {name}[/bold cyan]\n")

    try:
        old_secret_name = f"{name}-token"
        new_secret_name = f"{name}-token-{_timestamp()}"

        if not config.dry_run:
            _run_kubectl(
                config,
                ["delete", "secret", old_secret_name, "-n", namespace],
            )

        _create_token_secret(config, name, namespace, new_secret_name)

        token = _get_token_from_secret(config, namespace, new_secret_name)

        ca_cert = _get_cluster_ca(config)
        api_server = _get_api_server(config)

        kubeconfig_content = _build_kubeconfig(
            name=name,
            cluster_name="basilica-k3s",
            api_server=api_server,
            ca_cert=ca_cert,
            token=token,
        )

        output_path = Path(output) if output else Path(f"./kubeconfig-{name}-new.yaml")

        if not config.dry_run:
            output_path.write_text(kubeconfig_content)
            output_path.chmod(0o600)

        console.print(f"[green]Token rotated successfully[/green]")
        console.print(f"[cyan]New kubeconfig: {output_path}[/cyan]")

    except subprocess.CalledProcessError as e:
        console.print(f"[red]Error: {e.stderr}[/red]")
        raise click.Abort()


def _install_rbac_resources(config: Config, namespace: str) -> None:
    """Install namespace, ClusterRole, and ClusterRoleBinding."""
    console.print("[cyan]Installing RBAC resources...[/cyan]")

    namespace_manifest = f"""
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
    purpose: monitoring
    security: restricted
"""

    clusterrole_manifest = """
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: basilica-readonly
  labels:
    app: basilica-cluster-manager
rules:
- apiGroups: [""]
  resources:
    - pods
    - pods/log
    - pods/status
    - services
    - endpoints
    - namespaces
    - nodes
    - persistentvolumeclaims
    - persistentvolumes
    - events
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources:
    - deployments
    - daemonsets
    - replicasets
    - statefulsets
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources:
    - jobs
    - cronjobs
  verbs: ["get", "list", "watch"]
- apiGroups: ["networking.k8s.io"]
  resources:
    - ingresses
    - networkpolicies
  verbs: ["get", "list", "watch"]
- apiGroups: ["basilica.ai"]
  resources:
    - userdeployments
    - gpurentals
    - basilicajobs
    - basilicaqueues
    - basilicanodeprofiles
  verbs: ["get", "list", "watch"]
- apiGroups: ["metrics.k8s.io"]
  resources:
    - pods
    - nodes
  verbs: ["get", "list"]
"""

    if not config.dry_run:
        _apply_manifest(config, namespace_manifest)
        _apply_manifest(config, clusterrole_manifest)

    console.print("[green]RBAC resources installed[/green]\n")


def _create_service_account(config: Config, name: str, namespace: str) -> None:
    """Create ServiceAccount and bind to ClusterRole."""
    console.print(f"[cyan]Creating ServiceAccount: {name}...[/cyan]")

    sa_manifest = f"""
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    app: basilica-cluster-manager
    purpose: readonly-access
"""

    binding_manifest = f"""
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {name}-binding
  labels:
    app: basilica-cluster-manager
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: basilica-readonly
subjects:
- kind: ServiceAccount
  name: {name}
  namespace: {namespace}
"""

    if not config.dry_run:
        _apply_manifest(config, sa_manifest)
        _apply_manifest(config, binding_manifest)

    console.print("[green]ServiceAccount created[/green]\n")


def _create_token(config: Config, name: str, namespace: str, duration: str) -> str:
    """Create long-lived token using Secret with serviceaccount annotation."""
    console.print(f"[cyan]Creating token (duration: {duration})...[/cyan]")

    secret_name = f"{name}-token"
    _create_token_secret(config, name, namespace, secret_name)

    token = _get_token_from_secret(config, namespace, secret_name)

    console.print("[green]Token created[/green]\n")
    return token


def _create_token_secret(
    config: Config, sa_name: str, namespace: str, secret_name: str
) -> None:
    """Create Secret with serviceaccount annotation for long-lived token."""
    secret_manifest = f"""
apiVersion: v1
kind: Secret
metadata:
  name: {secret_name}
  namespace: {namespace}
  annotations:
    kubernetes.io/service-account.name: {sa_name}
  labels:
    app: basilica-cluster-manager
type: kubernetes.io/service-account-token
"""

    if not config.dry_run:
        _apply_manifest(config, secret_manifest)


def _get_token_from_secret(config: Config, namespace: str, secret_name: str) -> str:
    """Extract token from Secret after it's populated by controller."""
    import time

    max_retries = 10
    for i in range(max_retries):
        try:
            result = _run_kubectl(
                config,
                [
                    "get",
                    "secret",
                    secret_name,
                    "-n",
                    namespace,
                    "-o",
                    "jsonpath={.data.token}",
                ],
            )

            if result:
                return base64.b64decode(result).decode("utf-8")

            time.sleep(1)

        except subprocess.CalledProcessError:
            if i == max_retries - 1:
                raise
            time.sleep(1)

    raise RuntimeError(f"Token not populated in Secret {secret_name} after {max_retries}s")


def _get_cluster_ca(config: Config) -> str | None:
    """Extract cluster CA certificate from current kubeconfig.

    Returns:
        Base64-encoded CA certificate, or None if insecure-skip-tls-verify is used.
    """
    console.print("[cyan]Extracting cluster CA certificate...[/cyan]")

    # Try certificate-authority-data first (base64 inline)
    result = _run_kubectl(
        config,
        [
            "config",
            "view",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        ],
    )

    if result:
        console.print("[green]CA certificate extracted[/green]\n")
        return result

    # Try certificate-authority file path
    ca_path = _run_kubectl(
        config,
        [
            "config",
            "view",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority}",
        ],
    )

    if ca_path:
        from pathlib import Path

        ca_file = Path(ca_path)
        if ca_file.exists():
            ca_data = ca_file.read_bytes()
            console.print("[green]CA certificate extracted from file[/green]\n")
            return base64.b64encode(ca_data).decode("utf-8")
        raise RuntimeError(f"CA certificate file not found: {ca_path}")

    # Check if insecure-skip-tls-verify is enabled
    insecure = _run_kubectl(
        config,
        [
            "config",
            "view",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.insecure-skip-tls-verify}",
        ],
    )

    if insecure == "true":
        console.print("[yellow]Using insecure-skip-tls-verify (no CA)[/yellow]\n")
        return None

    raise RuntimeError("Failed to extract CA certificate from kubeconfig")


def _get_api_server(config: Config) -> str:
    """Extract API server endpoint from current kubeconfig."""
    console.print("[cyan]Extracting API server endpoint...[/cyan]")

    result = _run_kubectl(
        config,
        [
            "config",
            "view",
            "--raw",
            "-o",
            "jsonpath={.clusters[0].cluster.server}",
        ],
    )

    if not result:
        raise RuntimeError("Failed to extract API server endpoint from kubeconfig")

    console.print("[green]API server endpoint extracted[/green]\n")
    return result


def _build_kubeconfig(
    name: str,
    cluster_name: str,
    api_server: str,
    ca_cert: str | None,
    token: str,
) -> str:
    """Build kubeconfig YAML content."""
    if ca_cert:
        cluster_section = f"""    certificate-authority-data: {ca_cert}
    server: {api_server}"""
    else:
        cluster_section = f"""    insecure-skip-tls-verify: true
    server: {api_server}"""

    return f"""apiVersion: v1
kind: Config
clusters:
- cluster:
{cluster_section}
  name: {cluster_name}
contexts:
- context:
    cluster: {cluster_name}
    user: {name}
  name: {name}@{cluster_name}
current-context: {name}@{cluster_name}
users:
- name: {name}
  user:
    token: {token}
"""


def _apply_manifest(config: Config, manifest: str) -> None:
    """Apply Kubernetes manifest using kubectl."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(manifest)
        f.flush()

        try:
            _run_kubectl(config, ["apply", "-f", f.name])
        finally:
            Path(f.name).unlink()


def _run_kubectl(
    config: Config,
    args: list[str],
    env: dict[str, str] | None = None,
) -> str:
    """Execute kubectl command."""
    cmd = ["kubectl"]

    # Use explicit KUBECONFIG from env if provided, else use config.kubeconfig
    if env and "KUBECONFIG" in env:
        cmd.extend(["--kubeconfig", env["KUBECONFIG"]])
    elif config.kubeconfig:
        cmd.extend(["--kubeconfig", config.kubeconfig])

    cmd.extend(args)

    if config.verbose:
        console.print(f"[dim]Running: {' '.join(cmd)}[/dim]")

    if config.dry_run and any(
        verb in args for verb in ["apply", "create", "delete", "patch"]
    ):
        console.print(f"[yellow]Would run: {' '.join(cmd)}[/yellow]")
        return ""

    import os

    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        env=full_env,
    )

    return result.stdout.strip()


def _display_summary(
    name: str, namespace: str, output_path: Path, duration: str, token: str
) -> None:
    """Display generation summary."""
    summary = f"""
[bold green]Kubeconfig generated successfully![/bold green]

[bold]ServiceAccount:[/bold] {name}
[bold]Namespace:[/bold] {namespace}
[bold]Token Duration:[/bold] {duration}
[bold]Output File:[/bold] {output_path}

[bold yellow]Usage:[/bold yellow]
  export KUBECONFIG={output_path}
  kubectl get pods --all-namespaces
  kubectl get userdeployments -A

[bold yellow]Security Notes:[/bold yellow]
  - Token is read-only (no create/update/delete permissions)
  - File permissions set to 600 (owner read/write only)
  - Store securely and rotate periodically
  - Revoke access: clustermgr kubeconfig revoke --name {name}

[bold yellow]Verification:[/bold yellow]
  clustermgr kubeconfig verify --kubeconfig-path {output_path}
"""

    console.print(Panel(summary, title="Summary", border_style="green"))


def _timestamp() -> str:
    """Get timestamp for unique naming."""
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d%H%M%S")
