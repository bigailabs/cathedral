"""NetworkPolicy diagnostics commands for clustermgr."""

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
    run_kubectl,
)

console = Console()

REQUIRED_POLICIES = [
    "default-deny-all",
    "allow-dns",
    "allow-internet-egress",
    "allow-ingress-from-envoy",
]

COREDNS_SERVICE_IP = "10.43.0.10"


@dataclass
class NetworkPolicyInfo:
    """Information about a NetworkPolicy."""

    name: str
    namespace: str
    pod_selector: dict
    policy_types: list[str]
    ingress_rules: int
    egress_rules: int


def _get_tenant_namespaces(config: Config) -> list[str]:
    """Get all tenant namespaces (u-* prefixed)."""
    result = run_kubectl(config, ["get", "namespaces", "-o", "json"])
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    namespaces = []

    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if name.startswith("u-"):
            namespaces.append(name)

    return sorted(namespaces)


def _get_namespace_policies(config: Config, namespace: str) -> list[NetworkPolicyInfo]:
    """Get all NetworkPolicies in a namespace."""
    result = run_kubectl(
        config,
        ["get", "networkpolicy", "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    policies = []

    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})

        ingress_rules = len(spec.get("ingress", []))
        egress_rules = len(spec.get("egress", []))

        policies.append(NetworkPolicyInfo(
            name=metadata.get("name", ""),
            namespace=metadata.get("namespace", ""),
            pod_selector=spec.get("podSelector", {}),
            policy_types=spec.get("policyTypes", []),
            ingress_rules=ingress_rules,
            egress_rules=egress_rules,
        ))

    return policies


def _check_required_policies(policies: list[NetworkPolicyInfo]) -> dict[str, bool]:
    """Check if all required policies exist."""
    policy_names = {p.name for p in policies}
    return {req: req in policy_names for req in REQUIRED_POLICIES}


def _test_dns_connectivity(config: Config, namespace: str) -> tuple[bool, str]:
    """Test DNS connectivity from a pod in the namespace."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return False, "Failed to list pods"

    data = parse_json_output(result.stdout)
    pods = data.get("items", [])

    running_pod = None
    for pod in pods:
        phase = pod.get("status", {}).get("phase", "")
        if phase == "Running":
            running_pod = pod.get("metadata", {}).get("name", "")
            break

    if not running_pod:
        return False, "No running pods found"

    result = run_kubectl(
        config,
        [
            "exec", "-n", namespace, running_pod, "--",
            "nslookup", "kubernetes.default.svc.cluster.local", COREDNS_SERVICE_IP,
        ],
        timeout=15,
    )

    if result.returncode == 0 and "Address" in result.stdout:
        return True, "DNS resolution working"

    if "command not found" in result.stderr.lower():
        result = run_kubectl(
            config,
            [
                "exec", "-n", namespace, running_pod, "--",
                "cat", "/etc/resolv.conf",
            ],
            timeout=10,
        )
        if result.returncode == 0:
            return True, "DNS config present (nslookup not available)"

    return False, result.stderr or "DNS resolution failed"


def _test_egress_connectivity(config: Config, namespace: str) -> tuple[bool, str]:
    """Test internet egress from a pod in the namespace."""
    result = run_kubectl(
        config,
        ["get", "pods", "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        return False, "Failed to list pods"

    data = parse_json_output(result.stdout)
    pods = data.get("items", [])

    running_pod = None
    for pod in pods:
        phase = pod.get("status", {}).get("phase", "")
        if phase == "Running":
            running_pod = pod.get("metadata", {}).get("name", "")
            break

    if not running_pod:
        return False, "No running pods found"

    result = run_kubectl(
        config,
        [
            "exec", "-n", namespace, running_pod, "--",
            "wget", "-q", "-O", "-", "--timeout=5", "http://ifconfig.me",
        ],
        timeout=15,
    )

    if result.returncode == 0:
        return True, f"Egress working (IP: {result.stdout.strip()[:20]})"

    result = run_kubectl(
        config,
        [
            "exec", "-n", namespace, running_pod, "--",
            "curl", "-s", "-m", "5", "http://ifconfig.me",
        ],
        timeout=15,
    )

    if result.returncode == 0:
        return True, f"Egress working (IP: {result.stdout.strip()[:20]})"

    return False, "Egress blocked or tools unavailable"


def _test_ingress_from_envoy(config: Config, namespace: str) -> tuple[bool, str]:
    """Check if ingress from Envoy is allowed."""
    policies = _get_namespace_policies(config, namespace)

    for policy in policies:
        if "envoy" in policy.name.lower() or "ingress" in policy.name.lower():
            if "Ingress" in policy.policy_types and policy.ingress_rules > 0:
                return True, f"Ingress allowed via {policy.name}"

    return False, "No ingress policy for Envoy found"


@click.group()
def netpol() -> None:
    """NetworkPolicy diagnostics commands.

    Commands for auditing, testing, and verifying NetworkPolicies
    in tenant namespaces to ensure proper security isolation.
    """
    pass


@netpol.command()
@click.option("--namespace", "-n", help="Audit specific namespace")
@click.option("--fix", "-f", is_flag=True, help="Show commands to fix missing policies")
@click.pass_context
def audit(ctx: click.Context, namespace: str | None, fix: bool) -> None:
    """Audit NetworkPolicies across tenant namespaces.

    Checks if all required policies (default-deny, allow-dns,
    allow-internet, allow-ingress) are present in each namespace.
    """
    config: Config = ctx.obj

    print_header("NetworkPolicy Audit")

    if namespace:
        namespaces = [namespace]
    else:
        namespaces = _get_tenant_namespaces(config)

    if not namespaces:
        console.print("[yellow]No tenant namespaces found[/yellow]")
        return

    console.print(f"Auditing {len(namespaces)} namespace(s)...")

    compliant = 0
    non_compliant = 0
    issues: list[tuple[str, list[str]]] = []

    table = Table()
    table.add_column("Namespace", style="cyan")
    table.add_column("Policies")
    table.add_column("default-deny")
    table.add_column("allow-dns")
    table.add_column("allow-internet")
    table.add_column("allow-envoy")

    for ns in namespaces:
        policies = _get_namespace_policies(config, ns)
        required_status = _check_required_policies(policies)

        all_present = all(required_status.values())
        if all_present:
            compliant += 1
        else:
            non_compliant += 1
            missing = [k for k, v in required_status.items() if not v]
            issues.append((ns, missing))

        def status_cell(present: bool) -> str:
            return "[green]Yes[/green]" if present else "[red]No[/red]"

        table.add_row(
            ns,
            str(len(policies)),
            status_cell(required_status.get("default-deny-all", False)),
            status_cell(required_status.get("allow-dns", False)),
            status_cell(required_status.get("allow-internet-egress", False)),
            status_cell(required_status.get("allow-ingress-from-envoy", False)),
        )

    console.print(table)

    console.print(f"\nCompliant: [green]{compliant}[/green], Non-compliant: [red]{non_compliant}[/red]")

    if issues and fix:
        print_header("Fix Commands")
        console.print("Run these commands to apply missing policies:\n")

        for ns, missing in issues:
            console.print(f"[bold]{ns}:[/bold]")
            for policy in missing:
                template_file = f"user-namespace-{policy.replace('_', '-')}-template.yaml"
                console.print(
                    f"  kubectl apply -f orchestrator/k8s/networking/policies/{template_file} "
                    f"| sed 's/TENANT_NAMESPACE/{ns}/g' | kubectl apply -f -"
                )


@netpol.command("test")
@click.argument("namespace")
@click.pass_context
def test_namespace(ctx: click.Context, namespace: str) -> None:
    """Test DNS, egress, and ingress for a namespace.

    Performs live connectivity tests from pods in the specified
    namespace to verify NetworkPolicies are working correctly.
    """
    config: Config = ctx.obj

    print_header(f"NetworkPolicy Tests: {namespace}")

    policies = _get_namespace_policies(config, namespace)
    console.print(f"Found {len(policies)} NetworkPolicy(ies)")

    for policy in policies:
        types_str = ", ".join(policy.policy_types) if policy.policy_types else "None"
        console.print(f"  - {policy.name}: {types_str} (I:{policy.ingress_rules}, E:{policy.egress_rules})")

    print_header("Connectivity Tests")

    dns_ok, dns_msg = _test_dns_connectivity(config, namespace)
    severity = Severity.HEALTHY if dns_ok else Severity.CRITICAL
    print_status("DNS Resolution", dns_msg, severity)

    egress_ok, egress_msg = _test_egress_connectivity(config, namespace)
    severity = Severity.HEALTHY if egress_ok else Severity.WARNING
    print_status("Internet Egress", egress_msg, severity)

    ingress_ok, ingress_msg = _test_ingress_from_envoy(config, namespace)
    severity = Severity.HEALTHY if ingress_ok else Severity.WARNING
    print_status("Envoy Ingress", ingress_msg, severity)

    print_header("Summary")

    all_ok = dns_ok and egress_ok and ingress_ok
    if all_ok:
        console.print("[green]All connectivity tests passed[/green]")
    else:
        console.print("[yellow]Some tests failed - check NetworkPolicy configuration[/yellow]")

        required_status = _check_required_policies(policies)
        missing = [k for k, v in required_status.items() if not v]
        if missing:
            console.print(f"[red]Missing required policies: {', '.join(missing)}[/red]")


@netpol.command()
@click.pass_context
def coverage(ctx: click.Context) -> None:
    """Check NetworkPolicy coverage across all tenant namespaces.

    Shows a summary of which namespaces have complete policy coverage
    and which are missing required policies.
    """
    config: Config = ctx.obj

    print_header("NetworkPolicy Coverage")

    namespaces = _get_tenant_namespaces(config)

    if not namespaces:
        console.print("[yellow]No tenant namespaces found[/yellow]")
        return

    full_coverage = 0
    partial_coverage = 0
    no_coverage = 0

    coverage_data: list[tuple[str, int, int]] = []

    for ns in namespaces:
        policies = _get_namespace_policies(config, ns)
        required_status = _check_required_policies(policies)

        present_count = sum(1 for v in required_status.values() if v)
        total_required = len(REQUIRED_POLICIES)

        coverage_data.append((ns, present_count, total_required))

        if present_count == total_required:
            full_coverage += 1
        elif present_count > 0:
            partial_coverage += 1
        else:
            no_coverage += 1

    console.print(f"Total namespaces: {len(namespaces)}")
    console.print(f"  [green]Full coverage: {full_coverage}[/green]")
    console.print(f"  [yellow]Partial coverage: {partial_coverage}[/yellow]")
    console.print(f"  [red]No coverage: {no_coverage}[/red]")

    if partial_coverage > 0 or no_coverage > 0:
        print_header("Namespaces Needing Attention")

        table = Table()
        table.add_column("Namespace", style="cyan")
        table.add_column("Coverage")
        table.add_column("Status")

        for ns, present, total in coverage_data:
            if present < total:
                pct = (present / total) * 100
                if present == 0:
                    status = "[red]Missing all policies[/red]"
                else:
                    status = f"[yellow]Missing {total - present} policy(ies)[/yellow]"

                table.add_row(
                    ns,
                    f"{present}/{total} ({pct:.0f}%)",
                    status,
                )

        console.print(table)

    print_header("Required Policies")
    for policy in REQUIRED_POLICIES:
        console.print(f"  - {policy}")


@netpol.command()
@click.argument("namespace")
@click.pass_context
def details(ctx: click.Context, namespace: str) -> None:
    """Show detailed NetworkPolicy configuration for a namespace.

    Displays all policies with their selectors, ingress/egress rules,
    and effective coverage.
    """
    config: Config = ctx.obj

    print_header(f"NetworkPolicy Details: {namespace}")

    result = run_kubectl(
        config,
        ["get", "networkpolicy", "-n", namespace, "-o", "json"],
    )
    if result.returncode != 0:
        console.print(f"[red]Failed to get policies: {result.stderr}[/red]")
        return

    data = parse_json_output(result.stdout)
    items = data.get("items", [])

    if not items:
        console.print("[yellow]No NetworkPolicies found[/yellow]")
        return

    for item in items:
        metadata = item.get("metadata", {})
        spec = item.get("spec", {})

        name = metadata.get("name", "")
        console.print(f"\n[bold cyan]{name}[/bold cyan]")

        pod_selector = spec.get("podSelector", {})
        match_labels = pod_selector.get("matchLabels", {})
        if match_labels:
            labels_str = ", ".join(f"{k}={v}" for k, v in match_labels.items())
            console.print(f"  Pod Selector: {labels_str}")
        else:
            console.print("  Pod Selector: [dim](all pods)[/dim]")

        policy_types = spec.get("policyTypes", [])
        console.print(f"  Policy Types: {', '.join(policy_types) if policy_types else 'None'}")

        ingress_rules = spec.get("ingress", [])
        if ingress_rules:
            console.print(f"  Ingress Rules: {len(ingress_rules)}")
            for i, rule in enumerate(ingress_rules):
                from_rules = rule.get("from", [])
                ports = rule.get("ports", [])

                sources = []
                for f in from_rules:
                    if "namespaceSelector" in f:
                        ns_labels = f["namespaceSelector"].get("matchLabels", {})
                        if ns_labels:
                            sources.append(f"ns:{list(ns_labels.values())[0]}")
                        else:
                            sources.append("ns:*")
                    if "podSelector" in f:
                        pod_labels = f["podSelector"].get("matchLabels", {})
                        if pod_labels:
                            sources.append(f"pod:{list(pod_labels.values())[0]}")
                    if "ipBlock" in f:
                        cidr = f["ipBlock"].get("cidr", "")
                        sources.append(f"ip:{cidr}")

                port_str = ", ".join(f"{p.get('port', '*')}/{p.get('protocol', 'TCP')}" for p in ports) if ports else "*"
                console.print(f"    [{i+1}] From: {', '.join(sources) if sources else 'any'} -> Ports: {port_str}")

        egress_rules = spec.get("egress", [])
        if egress_rules:
            console.print(f"  Egress Rules: {len(egress_rules)}")
            for i, rule in enumerate(egress_rules):
                to_rules = rule.get("to", [])
                ports = rule.get("ports", [])

                destinations = []
                for t in to_rules:
                    if "namespaceSelector" in t:
                        ns_labels = t["namespaceSelector"].get("matchLabels", {})
                        if ns_labels:
                            destinations.append(f"ns:{list(ns_labels.values())[0]}")
                        else:
                            destinations.append("ns:*")
                    if "podSelector" in t:
                        pod_labels = t["podSelector"].get("matchLabels", {})
                        if pod_labels:
                            destinations.append(f"pod:{list(pod_labels.values())[0]}")
                    if "ipBlock" in t:
                        cidr = t["ipBlock"].get("cidr", "")
                        except_cidrs = t["ipBlock"].get("except", [])
                        dest = f"ip:{cidr}"
                        if except_cidrs:
                            dest += f" (except: {', '.join(except_cidrs)})"
                        destinations.append(dest)

                port_str = ", ".join(f"{p.get('port', '*')}/{p.get('protocol', 'TCP')}" for p in ports) if ports else "*"
                console.print(f"    [{i+1}] To: {', '.join(destinations) if destinations else 'any'} -> Ports: {port_str}")
