"""FastAPI app + lifespan that wires the pull loop, weight loop, and watchdog."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI

from cathedral.cards.registry import CardRegistry
from cathedral.chain import BittensorChain, Chain
from cathedral.evidence import EvidenceCollector, HttpPolarisFetcher
from cathedral.validator import pull_loop, weight_loop
from cathedral.validator.config_runtime import RuntimeContext
from cathedral.validator.db import connect
from cathedral.validator.health import Health, HealthSnapshot
from cathedral.validator.stall import run_stall_watchdog

logger = structlog.get_logger(__name__)


def build_app(ctx: RuntimeContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = await connect(ctx.settings.storage.database_path)
        app.state.db = conn
        app.state.health = ctx.health
        app.state.bearer = ctx.bearer

        stop = asyncio.Event()
        # Shared signal: pull_loop sets this after its first fully-drained
        # catch-up pass; weight_loop awaits it before the first set_weights.
        # Prevents a freshly-upgraded validator from publishing a vector
        # computed off a half-hydrated 7-day window. Set once, never reset.
        initial_backfill_complete = asyncio.Event()

        # Single weight path: pull_loop feeds pulled SAT scores into the local
        # weight_loop (PAR-2 + hardcoded burn) which drives chain.set_weights.
        # The signed-publisher-vector relay (Path B) was removed; this is the
        # only path the validator runs.
        await ctx.health.update(weight_path="A")

        weight_task = asyncio.create_task(
            weight_loop.run_weight_loop(
                conn,
                ctx.chain,
                ctx.health,
                interval_secs=ctx.settings.weights.interval_secs,
                disabled=ctx.settings.weights.disabled,
                burn_uid=ctx.settings.weights.burn_uid,
                forced_burn_percentage=ctx.settings.weights.forced_burn_percentage,
                v3_bug_isolation_weight=ctx.settings.weights.v3_bug_isolation_weight,
                task_family_weights=ctx.settings.weights.task_family_weights,
                stop=stop,
                initial_backfill_complete=initial_backfill_complete,
            )
        )

        tasks = [
            weight_task,
            asyncio.create_task(
                run_stall_watchdog(
                    conn,
                    ctx.health,
                    after_secs=ctx.settings.stall.after_secs,
                    stop=stop,
                )
            ),
        ]
        if ctx.cathedral_public_key is not None:
            tasks.append(
                asyncio.create_task(
                    pull_loop.run_pull_loop(
                        conn=conn,
                        publisher_url=ctx.settings.publisher.url,
                        cathedral_public_key=ctx.cathedral_public_key,
                        health=ctx.health,
                        interval_secs=ctx.settings.publisher.pull_interval_secs,
                        api_token=ctx.publisher_api_token,
                        stop=stop,
                        initial_backfill_complete=initial_backfill_complete,
                    )
                )
            )
        else:
            logger.warning(
                "pull_loop_disabled",
                reason="cathedral_public_key not configured",
            )
            # No pull loop wired - unblock weight_loop immediately so it
            # doesn't hang on the backfill signal that will never fire.
            initial_backfill_complete.set()
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await conn.close()
            if ctx.fetcher_close is not None:
                await ctx.fetcher_close()

    app = FastAPI(title="Cathedral Validator", lifespan=lifespan)

    @app.get("/health", response_model=HealthSnapshot)
    async def get_health() -> HealthSnapshot:
        return await ctx.health.get()

    return app


def from_settings(settings_path: str) -> FastAPI:
    """Production builder - used by `cathedral-validator serve`."""
    from cathedral.config import (  # local import for CLI speed
        ValidatorSettings,
        resolve_validator_config_path,
    )

    settings_path = resolve_validator_config_path(settings_path)
    settings = ValidatorSettings.from_toml(settings_path)
    bearer = os.environ.get(settings.http.bearer_token_env)
    if not bearer:
        raise RuntimeError(f"missing bearer token: env {settings.http.bearer_token_env} not set")

    pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(settings.polaris.public_key_hex))
    fetcher = HttpPolarisFetcher(settings.polaris.base_url, settings.polaris.fetch_timeout_secs)
    collector = EvidenceCollector(fetcher, pubkey)
    chain: Chain = BittensorChain(
        network=settings.network.name,
        netuid=settings.network.netuid,
        wallet_name=settings.network.wallet_name,
        wallet_hotkey=settings.network.validator_hotkey,
        wallet_path=settings.network.wallet_path,
    )

    cathedral_pubkey: Ed25519PublicKey | None = None
    cathedral_pubkey_hex = os.environ.get(settings.publisher.public_key_env)
    if cathedral_pubkey_hex:
        cathedral_pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(cathedral_pubkey_hex))
    else:
        logger.warning(
            "cathedral_public_key_missing",
            env=settings.publisher.public_key_env,
            hint="pull_loop will be disabled - set the env var to enable",
        )

    publisher_api_token: str | None = None
    if settings.publisher.api_token_env:
        publisher_api_token = os.environ.get(settings.publisher.api_token_env)

    ctx = RuntimeContext(
        settings=settings,
        bearer=bearer,
        chain=chain,
        collector=collector,
        registry=CardRegistry.baseline(),
        health=Health(),
        cathedral_public_key=cathedral_pubkey,
        publisher_api_token=publisher_api_token,
        fetcher_close=fetcher.aclose,
    )
    return build_app(ctx)
