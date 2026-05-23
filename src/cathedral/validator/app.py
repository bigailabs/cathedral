"""FastAPI app + lifespan that wires the worker, weight loop, and watchdog."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite
import structlog
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, FastAPI, HTTPException, status

from cathedral.cards.registry import CardRegistry
from cathedral.chain import BittensorChain, Chain
from cathedral.evidence import EvidenceCollector, HttpPolarisFetcher
from cathedral.types import PolarisAgentClaim
from cathedral.validator import cards as cards_store
from cathedral.validator import pull_loop, queue, remote_weight_loop, weight_loop, worker
from cathedral.validator.auth import make_bearer_dep
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

        if ctx.settings.remote_weight_source.enabled and ctx.remote_weight_public_key is None:
            logger.error(
                "remote_weight_source_enabled_but_key_missing",
                env=ctx.settings.remote_weight_source.public_key_env,
            )
            raise RuntimeError(
                "remote_weight_source.enabled requires "
                f"{ctx.settings.remote_weight_source.public_key_env}; refusing local fallback"
            )

        if ctx.settings.remote_weight_source.enabled:
            remote_fetch_task = asyncio.create_task(
                remote_weight_loop.run_remote_weight_loop(
                    conn,
                    ctx.health,
                    publisher_url=ctx.settings.remote_weight_source.url,
                    public_key=ctx.remote_weight_public_key,
                    expected_key_id=ctx.settings.remote_weight_source.key_id,
                    network=ctx.settings.network.name,
                    netuid=ctx.settings.network.netuid,
                    poll_interval_secs=ctx.settings.remote_weight_source.poll_interval_secs,
                    request_timeout_secs=ctx.settings.remote_weight_source.request_timeout_secs,
                    stop=stop,
                )
            )
            initial_backfill_complete.set()
            weight_task = asyncio.create_task(
                weight_loop.run_weight_loop(
                    conn,
                    ctx.chain,
                    ctx.health,
                    interval_secs=ctx.settings.weights.interval_secs,
                    disabled=ctx.settings.weights.disabled,
                    stop=stop,
                    initial_backfill_complete=initial_backfill_complete,
                    remote_weight_apply=lambda: remote_weight_loop.apply_cached_remote_vector_once(
                        conn,
                        ctx.chain,
                        ctx.health,
                        public_key=ctx.remote_weight_public_key,
                        expected_key_id=ctx.settings.remote_weight_source.key_id,
                        network=ctx.settings.network.name,
                        netuid=ctx.settings.network.netuid,
                        disabled=ctx.settings.weights.disabled,
                    ),
                )
            )
        else:
            remote_fetch_task = None
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
            asyncio.create_task(
                worker.run_worker(
                    conn,
                    ctx.collector,
                    ctx.registry,
                    ctx.health,
                    poll_interval_secs=ctx.settings.worker.poll_interval_secs,
                    max_concurrent=ctx.settings.worker.max_concurrent_verifications,
                    stop=stop,
                )
            ),
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
        if remote_fetch_task is not None:
            tasks.append(remote_fetch_task)
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
    bearer_dep = make_bearer_dep(ctx.bearer)

    @app.get("/health", response_model=HealthSnapshot)
    async def get_health() -> HealthSnapshot:
        return await ctx.health.get()

    @app.post("/v1/claim", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(bearer_dep)])
    async def post_claim(claim: PolarisAgentClaim) -> dict[str, int | str]:
        try:
            claim_id = await queue.insert_claim(app.state.db, claim)
        except aiosqlite.IntegrityError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"id": claim_id, "status": "pending"}

    @app.get("/v1/cards/{card_id}")
    async def get_card(card_id: str) -> dict:
        """Return the highest-scoring verified version of `card_id`.

        Public read - cards are public information. Used by
        cathedral.computer to display the canonical view of each card.
        404 if no miner has produced a verified version yet.
        """
        row = await cards_store.best_card(app.state.db, card_id)
        if row is None:
            raise HTTPException(status_code=404, detail="card not found")
        return row

    @app.get("/v1/cards/{card_id}/history")
    async def get_card_history(card_id: str) -> list[dict]:
        """Return all verified versions of `card_id` across miners,
        newest verification first. Used by cathedral.computer to show
        which miners are maintaining a card and how their entries
        compare."""
        return await cards_store.card_history(app.state.db, card_id)

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

    remote_weight_pubkey: Ed25519PublicKey | None = None
    if settings.remote_weight_source.enabled:
        rw_hex = os.environ.get(settings.remote_weight_source.public_key_env, "").strip()
        if rw_hex:
            try:
                remote_weight_pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(rw_hex))
            except ValueError as exc:
                raise RuntimeError(
                    f"{settings.remote_weight_source.public_key_env} must be "
                    "a 32-byte Ed25519 public-key hex"
                ) from exc
        else:
            logger.error(
                "remote_weight_public_key_missing",
                env=settings.remote_weight_source.public_key_env,
            )
            raise RuntimeError(
                "remote_weight_source.enabled requires "
                f"{settings.remote_weight_source.public_key_env}; refusing local fallback"
            )

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
        remote_weight_public_key=remote_weight_pubkey,
    )
    return build_app(ctx)
