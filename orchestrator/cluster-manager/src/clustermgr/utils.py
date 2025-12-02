"""Utility functions for clustermgr."""

import json
import os
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any

from rich.console import Console

from clustermgr.config import Config

console = Console()


class Severity(Enum):
    """Severity levels for status messages."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"


def run_cmd(
    cmd: list[str],
    timeout: int = 30,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
            env=run_env,
        )
        return result
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "Command timed out")
    except subprocess.CalledProcessError as e:
        return subprocess.CompletedProcess(cmd, e.returncode, e.stdout or "", e.stderr or "")


def run_ansible(
    config: Config,
    module: str,
    args: str,
    hosts: str = "k3s_server",
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run an Ansible ad-hoc command."""
    cmd = [
        "ansible",
        hosts,
        "-i",
        str(config.inventory),
        "-m",
        module,
        "-a",
        args,
    ]
    return run_cmd(cmd, timeout=timeout, check=False)


def run_kubectl(
    config: Config,
    args: list[str],
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run a kubectl command."""
    cmd = ["kubectl"] + args
    return run_cmd(cmd, timeout=timeout, check=False, env={"KUBECONFIG": config.kubeconfig})


def parse_json_output(output: str) -> dict[str, Any]:
    """Parse JSON output from kubectl."""
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {}


def print_header(text: str) -> None:
    """Print a section header."""
    console.print(f"\n[bold cyan]=== {text} ===[/bold cyan]")


def print_status(label: str, status: str, severity: Severity) -> None:
    """Print a status line with color-coded severity."""
    color_map = {
        Severity.HEALTHY: "green",
        Severity.WARNING: "yellow",
        Severity.CRITICAL: "red",
        Severity.EMERGENCY: "bold red",
    }
    color = color_map.get(severity, "white")
    console.print(f"  {label}: [{color}]{status}[/{color}]")


def confirm(message: str) -> bool:
    """Ask for user confirmation."""
    while True:
        response = console.input(f"\n[yellow]{message} [y/N]: [/yellow]").strip().lower()
        if response in ("y", "yes"):
            return True
        if response in ("n", "no", ""):
            return False
