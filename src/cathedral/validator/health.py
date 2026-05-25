"""Validator health snapshot — surfaces what the runbook describes (issue #1)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from cathedral.chain.client import WeightStatus


class HealthSnapshot(BaseModel):
    registered: bool = False
    current_block: int = 0
    last_metagraph_at: datetime | None = None
    last_evidence_pass_at: datetime | None = None
    last_weight_set_at: datetime | None = None
    weight_status: WeightStatus | None = None
    stalled: bool = False
    claims_pending: int = 0
    claims_verifying: int = 0
    claims_verified: int = 0
    claims_rejected: int = 0
    # Which weight path the validator is running: "A" = local aggregation
    # (pull_loop + weight_loop, hardcoded burn); "B" = signed publisher
    # vector (remote_weight_loop + apply_cached_remote_vector_once, publisher-
    # controlled burn). None = uninitialised (set during lifespan startup).
    # See AGENTS.md "Two weight paths" for the full description.
    weight_path: str | None = None


@dataclass
class Health:
    snapshot: HealthSnapshot = field(default_factory=HealthSnapshot)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def update(self, **fields: object) -> None:
        async with self._lock:
            current = self.snapshot.model_dump()
            current.update(fields)
            self.snapshot = HealthSnapshot.model_validate(current)

    async def heartbeat(self, key: str) -> None:
        await self.update(**{key: datetime.now(UTC)})

    async def get(self) -> HealthSnapshot:
        async with self._lock:
            return self.snapshot.model_copy()
