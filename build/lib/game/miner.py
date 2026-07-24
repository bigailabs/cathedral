"""Miner environments — each is a local stand-in for a miner's SSH-connected
host: a workspace the publisher runs a solver in, a liveness heartbeat, and a
(maybe-absent) provisioned compute slot for attested work.

A MinerEnv holds only what a real miner controls: its identity, its solver
behavior/speed, whether it keeps heartbeating, and whether it holds an
attestable compute slot. The publisher (publisher.py) does everything else.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

WORKERS_DIR = Path(__file__).resolve().parent / "_workers"


@dataclass
class ComputeSlot:
    """A provisioned, attestable compute slot. `image` is the pinned solver
    image the attested box runs; `pubkey_b64` is the run's e2e key. A miner with
    no real slot has an unpullable image or no key, and attestation fails."""
    image: str
    pubkey_b64: str

    @property
    def provisioned(self) -> bool:
        return bool(self.image) and "unattestable" not in self.image and bool(self.pubkey_b64)


@dataclass
class MinerEnv:
    hotkey: str
    coldkey: str
    archetype: str
    behavior: str               # honest | slow | cheat | copy  (drives runner.py)
    delay_ms: float             # simulated solver effort -> host-measured speed
    heartbeats: bool            # False => goes dark (liveness gate trips)
    slot: ComputeSlot
    copies_from: str | None = None      # for the copier: whose answer it steals
    workspace: Path = field(default=None)  # set by ensure_workspace()

    def ensure_workspace(self) -> Path:
        self.workspace = WORKERS_DIR / self.hotkey
        self.workspace.mkdir(parents=True, exist_ok=True)
        return self.workspace

    def heartbeat_age_s(self, now_s: int) -> float:
        """Seconds since this miner's last heartbeat, as the publisher would
        observe it. A dark miner reports an unboundedly stale heartbeat."""
        return float("inf") if not self.heartbeats else 0.0


def _slot(image: str, has_key: bool) -> ComputeSlot:
    key = base64.b64encode(hashlib.sha256(image.encode()).digest()).decode() if has_key else ""
    return ComputeSlot(image=image, pubkey_b64=key)


def default_roster() -> list[MinerEnv]:
    """The seven archetypes from GAME_SPEC.md — one per gate, plus a Sybil pair."""
    return [
        MinerEnv("honest_fast", "ck_fast", "honest_fast", "honest", 5.0, True,
                 _slot("sha256:goodsolver-fast", True)),
        MinerEnv("honest_slow", "ck_slow", "honest_slow", "slow", 220.0, True,
                 _slot("sha256:goodsolver-slow", True)),
        MinerEnv("cheater_wrong", "ck_cheat", "cheater_wrong", "cheat", 2.0, True,
                 _slot("sha256:goodsolver-cheat", True)),
        MinerEnv("copier", "ck_copy", "copier", "copy", 2.0, True,
                 _slot("sha256:goodsolver-copy", True), copies_from="honest_fast"),
        MinerEnv("dead", "ck_dead", "dead", "honest", 5.0, False,
                 _slot("sha256:goodsolver-dead", True)),
        MinerEnv("unprovisioned", "ck_unprov", "unprovisioned", "honest", 8.0, True,
                 _slot("sha256:unattestable", False)),
        MinerEnv("sybil_a", "ck_sybil", "sybil_a", "honest", 10.0, True,
                 _slot("sha256:goodsolver-sa", True)),
        MinerEnv("sybil_b", "ck_sybil", "sybil_b", "honest", 10.0, True,
                 _slot("sha256:goodsolver-sb", True)),
    ]
