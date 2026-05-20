"""Runtime context — bundles all dependencies the FastAPI lifespan needs.

Kept separate from `app.py` so tests can build a context with mocks without
importing `BittensorChain` (heavy import).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cathedral.cards.registry import CardRegistry
from cathedral.chain import Chain
from cathedral.config import ValidatorSettings
from cathedral.evidence import EvidenceCollector
from cathedral.validator.health import Health


@dataclass
class RuntimeContext:
    settings: ValidatorSettings
    bearer: str
    chain: Chain
    collector: EvidenceCollector
    registry: CardRegistry
    health: Health
    cathedral_public_key: Ed25519PublicKey | None = None
    publisher_api_token: str | None = None
    fetcher_close: Callable[[], Awaitable[None]] | None = None
    remote_weight_public_key: Ed25519PublicKey | None = None
