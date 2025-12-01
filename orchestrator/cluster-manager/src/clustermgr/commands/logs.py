"""Logs command for clustermgr."""

import click
from rich.console import Console

from clustermgr.config import Config
from clustermgr.utils import print_header, run_ansible

console = Console()


@click.command()
@click.option("-n", "--lines", default=50, help="Number of lines to show")
@click.pass_context
def logs(ctx: click.Context, lines: int) -> None:
    """Show recent K3s logs from all servers."""
    config: Config = ctx.obj

    print_header(f"Recent K3s Logs (last {lines} lines)")

    result = run_ansible(
        config,
        "shell",
        f"sudo journalctl -u k3s* --no-pager -n {lines} | tail -{lines}",
        timeout=60,
    )

    current_server: str | None = None
    for line in result.stdout.split("\n"):
        if " | CHANGED" in line or " | SUCCESS" in line:
            current_server = line.split(" | ")[0].strip()
            console.print(f"\n[cyan]=== {current_server} ===[/cyan]")
        elif current_server and line.strip():
            line_lower = line.lower()
            if "error" in line_lower or "fatal" in line_lower:
                console.print(f"[red]{line}[/red]")
            elif "warn" in line_lower:
                console.print(f"[yellow]{line}[/yellow]")
            else:
                console.print(line)
