"""Configuration management for clustermgr."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_kubeconfig() -> str:
    return os.path.expanduser("~/.kube/k3s-basilica-config")


def _default_inventory() -> Path:
    return Path(__file__).parent.parent.parent.parent / "ansible" / "inventories" / "production.ini"


def _default_ansible_dir() -> Path:
    return Path(__file__).parent.parent.parent.parent / "ansible"


@dataclass
class Config:
    """Tool configuration."""

    kubeconfig: str = field(default_factory=_default_kubeconfig)
    inventory: Path = field(default_factory=_default_inventory)
    ansible_dir: Path = field(default_factory=_default_ansible_dir)
    dry_run: bool = False
    verbose: bool = False
    no_confirm: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.inventory, str):
            self.inventory = Path(self.inventory)
        if isinstance(self.ansible_dir, str):
            self.ansible_dir = Path(self.ansible_dir)
