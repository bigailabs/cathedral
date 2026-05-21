"""Publisher service — public submission API + read endpoints + merkle.

This is the api.cathedral.computer process. It owns the only mutating
endpoint in v1 (`POST /v1/agents/submit`) plus all public read endpoints.

The eval orchestrator (`cathedral.eval`) and the Merkle anchor job
(`cathedral.publisher.merkle`) run as background tasks inside the same
FastAPI process. The validator binary (`cathedral-validator`) is a
separate process that pulls the publisher's `/v1/leaderboard/recent`
endpoint and writes Bittensor weights.
"""

from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name in {"build_publisher_app", "from_settings"}:
        from cathedral.publisher.app import build_publisher_app, from_settings

        return {"build_publisher_app": build_publisher_app, "from_settings": from_settings}[name]
    raise AttributeError(name)


__all__ = ["build_publisher_app", "from_settings"]
