"""Events command for clustermgr - filtered cluster events."""

import click
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table

from clustermgr.config import Config
from clustermgr.utils import parse_json_output, print_header, run_kubectl

console = Console()


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse Kubernetes timestamp."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_age(ts: datetime | None) -> str:
    """Format timestamp as age string."""
    if not ts:
        return "-"
    now = datetime.now(timezone.utc)
    delta = now - ts
    seconds = int(delta.total_seconds())

    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _get_events(
    config: Config,
    namespace: str | None = None,
    event_type: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get cluster events."""
    cmd = ["get", "events"]
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")
    cmd.extend(["-o", "json", "--sort-by=.lastTimestamp"])

    result = run_kubectl(config, cmd)
    if result.returncode != 0:
        return []

    data = parse_json_output(result.stdout)
    events = []

    for item in data.get("items", []):
        ev_type = item.get("type", "Normal")

        if event_type and ev_type.lower() != event_type.lower():
            continue

        metadata = item.get("metadata", {})
        involved = item.get("involvedObject", {})

        last_ts = _parse_timestamp(item.get("lastTimestamp", ""))
        first_ts = _parse_timestamp(item.get("firstTimestamp", ""))

        events.append({
            "namespace": metadata.get("namespace", ""),
            "type": ev_type,
            "reason": item.get("reason", ""),
            "object": f"{involved.get('kind', '')}/{involved.get('name', '')}",
            "message": item.get("message", ""),
            "count": item.get("count", 1),
            "first_seen": first_ts,
            "last_seen": last_ts,
            "source": item.get("source", {}).get("component", ""),
        })

    # Sort by last_seen descending, limit results
    events.sort(key=lambda e: e["last_seen"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return events[:limit]


@click.command()
@click.option("--namespace", "-n", help="Filter by namespace")
@click.option("--type", "-t", "event_type", type=click.Choice(["normal", "warning"]),
              help="Filter by event type")
@click.option("--warnings", "-w", is_flag=True, help="Show only warnings (shortcut for -t warning)")
@click.option("--limit", "-l", default=50, help="Maximum events to show (default: 50)")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all events (no limit)")
@click.pass_context
def events(
    ctx: click.Context,
    namespace: str | None,
    event_type: str | None,
    warnings: bool,
    limit: int,
    show_all: bool,
) -> None:
    """Show filtered cluster events for incident triage."""
    config: Config = ctx.obj

    if warnings:
        event_type = "warning"

    if show_all:
        limit = 1000

    print_header("Cluster Events")

    evs = _get_events(config, namespace, event_type, limit)

    if not evs:
        console.print("[yellow]No events found[/yellow]")
        return

    # Summary counts
    warning_count = sum(1 for e in evs if e["type"] == "Warning")
    normal_count = len(evs) - warning_count

    console.print(f"Showing {len(evs)} event(s): ", end="")
    console.print(f"[yellow]{warning_count} warnings[/yellow], ", end="")
    console.print(f"[green]{normal_count} normal[/green]")

    table = Table()
    table.add_column("Age", style="dim", width=6)
    table.add_column("Type", width=8)
    table.add_column("Namespace", style="cyan", width=15)
    table.add_column("Object", max_width=35)
    table.add_column("Reason", width=20)
    table.add_column("Message", max_width=50)
    table.add_column("Count", justify="right", width=5)

    for ev in evs:
        type_color = "yellow" if ev["type"] == "Warning" else "green"

        # Truncate message for display
        message = ev["message"]
        if len(message) > 50:
            message = message[:47] + "..."

        # Truncate object for display
        obj = ev["object"]
        if len(obj) > 35:
            obj = "..." + obj[-32:]

        count_str = str(ev["count"]) if ev["count"] > 1 else ""

        table.add_row(
            _format_age(ev["last_seen"]),
            f"[{type_color}]{ev['type']}[/{type_color}]",
            ev["namespace"],
            obj,
            ev["reason"],
            message,
            count_str,
        )

    console.print(table)

    # Show repeated warnings summary
    repeated = [e for e in evs if e["count"] > 5 and e["type"] == "Warning"]
    if repeated:
        console.print(f"\n[bold yellow]Repeated warnings (count > 5):[/bold yellow]")
        for ev in repeated[:5]:
            console.print(f"  [{ev['count']}x] {ev['namespace']}/{ev['object']}: {ev['reason']}")
