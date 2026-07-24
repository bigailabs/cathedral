"""Bittensor wire model for a private SAT challenge."""

from __future__ import annotations

from dataclasses import asdict
from typing import ClassVar

import bittensor as bt

from .core import Challenge


class SatChallenge(bt.Synapse):
    protocol_version: int = 1
    challenge_id: str = ""
    validator_hotkey: str = ""
    miner_hotkey: str = ""
    netuid: int = 0
    round_id: int = 0
    issued_at_ms: int = 0
    expires_at_ms: int = 0
    n_vars: int = 0
    n_clauses: int = 0
    cnf_b64: str = ""
    cnf_sha256: str = ""

    assignment_b64: str = ""
    solver_name: str = ""
    miner_elapsed_ms: float = 0.0
    error: str = ""

    required_hash_fields: ClassVar[tuple[str, ...]] = (
        "protocol_version",
        "challenge_id",
        "validator_hotkey",
        "miner_hotkey",
        "netuid",
        "round_id",
        "issued_at_ms",
        "expires_at_ms",
        "n_vars",
        "n_clauses",
        "cnf_sha256",
        "assignment_b64",
        "solver_name",
        "miner_elapsed_ms",
        "error",
    )

    @classmethod
    def from_challenge(cls, challenge: Challenge) -> "SatChallenge":
        return cls(**asdict(challenge))
