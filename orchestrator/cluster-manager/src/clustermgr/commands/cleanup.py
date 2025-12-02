"""Cleanup command for clustermgr."""

import click
from rich.console import Console

from clustermgr.config import Config
from clustermgr.utils import confirm, parse_json_output, print_header, run_kubectl

console = Console()


@click.command()
@click.pass_context
def cleanup(ctx: click.Context) -> None:
    """Clean up CrashLoopBackOff and failed pods."""
    config: Config = ctx.obj

    print_header("Cleaning Up Failed Pods")

    result = run_kubectl(
        config,
        ["get", "pods", "-A", "-o", "json"],
        timeout=30,
    )

    if result.returncode != 0:
        console.print("[red]Failed to get pod list[/red]")
        ctx.exit(1)

    data = parse_json_output(result.stdout)
    to_delete: list[tuple[str, str, str]] = []

    for item in data.get("items", []):
        ns = item["metadata"]["namespace"]
        name = item["metadata"]["name"]

        for cs in item.get("status", {}).get("containerStatuses", []):
            waiting = cs.get("state", {}).get("waiting", {})
            reason = waiting.get("reason", "")
            if reason in ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull"):
                to_delete.append((ns, name, reason))
                break

    if not to_delete:
        console.print("[green]No pods to clean up.[/green]")
        return

    console.print(f"Found {len(to_delete)} pod(s) to delete:")
    for ns, name, reason in to_delete:
        console.print(f"  - {ns}/{name} ({reason})")

    if config.dry_run:
        console.print("\n[yellow][DRY RUN] Would delete these pods.[/yellow]")
        return

    if not config.no_confirm and not confirm(f"Delete {len(to_delete)} pod(s)?"):
        console.print("Aborted.")
        return

    for ns, name, _ in to_delete:
        result = run_kubectl(config, ["delete", "pod", "-n", ns, name])
        if result.returncode == 0:
            console.print(f"  [green]Deleted {ns}/{name}[/green]")
        else:
            console.print(f"  [red]Failed to delete {ns}/{name}[/red]")
