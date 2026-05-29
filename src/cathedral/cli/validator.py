"""`cathedral-validator` CLI."""

from __future__ import annotations

import asyncio
import json
import os

import typer
import uvicorn

from cathedral.logging import configure
from cathedral.validator.db import connect

app = typer.Typer(no_args_is_help=True, help="Cathedral subnet validator")


@app.command()
def serve(
    config: str = typer.Option("config/testnet.toml", "--config", "-c"),
    json_logs: bool = typer.Option(True, "--json-logs/--no-json-logs"),
    log_level: str = typer.Option("info"),
) -> None:
    """Run the validator HTTP server with all background loops."""
    configure(level=log_level.upper(), json_logs=json_logs)
    from cathedral.config import ValidatorSettings, resolve_validator_config_path
    from cathedral.validator import from_settings

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)
    application = from_settings(config)
    uvicorn.run(
        application,
        host=settings.http.listen_host,
        port=settings.http.listen_port,
        log_level=log_level,
    )


@app.command()
def migrate(
    config: str = typer.Option("config/testnet.toml", "--config", "-c"),
) -> None:
    """Initialize the sqlite schema. Idempotent."""
    configure()
    from cathedral.config import ValidatorSettings, resolve_validator_config_path

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)

    async def _run() -> None:
        conn = await connect(settings.storage.database_path)
        await conn.close()

    asyncio.run(_run())
    typer.echo(f"schema ready at {settings.storage.database_path}")


@app.command("sat-launch-preflight")
def sat_launch_preflight(
    config: str = typer.Option("config/mainnet.toml", "--config", "-c"),
    require_zero_local_sat_weight: bool = typer.Option(
        False,
        "--require-zero-local-sat-weight/--allow-local-sat-weight",
        help="Require local synthetic_boolean_v1 blending to remain 0.0.",
    ),
) -> None:
    """Validate validator SAT launch config without touching Bittensor."""
    configure()
    from cathedral.config import ValidatorSettings, resolve_validator_config_path
    from cathedral.validator.launch_preflight import run_validator_sat_launch_preflight

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)
    result = run_validator_sat_launch_preflight(
        settings,
        require_zero_local_sat_weight=require_zero_local_sat_weight,
    )

    detail_keys = (
        "network",
        "netuid",
        "validator_hotkey",
        "local_sat_weight",
        "weights_disabled",
        "weights_interval_secs",
        "forced_burn_percentage",
    )
    for key in detail_keys:
        typer.echo(f"{key}: {result.details[key]}")
    for warning in result.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)
    typer.echo("Validator SAT launch preflight passed")


@app.command("chain-launch-preflight")
def chain_launch_preflight(
    config: str = typer.Option("config/mainnet.toml", "--config", "-c"),
) -> None:
    """Inspect live Bittensor validator launch state without set_weights."""
    configure()
    from cathedral.chain import BittensorChain
    from cathedral.config import ValidatorSettings, resolve_validator_config_path
    from cathedral.validator.chain_launch_preflight import (
        run_validator_chain_launch_preflight,
    )

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)

    async def _run() -> None:
        chain = BittensorChain(
            network=settings.network.name,
            netuid=settings.network.netuid,
            wallet_name=settings.network.wallet_name,
            wallet_hotkey=settings.network.validator_hotkey,
            wallet_path=settings.network.wallet_path,
        )
        result = await run_validator_chain_launch_preflight(settings, chain)
        typer.echo(json.dumps(result.details, indent=2, sort_keys=True))
        for warning in result.warnings:
            typer.echo(f"WARNING: {warning}", err=True)
        if result.errors:
            for error in result.errors:
                typer.echo(f"ERROR: {error}", err=True)
            raise typer.Exit(1)
        typer.echo("Validator chain launch preflight passed")

    asyncio.run(_run())


@app.command("pull")
def pull(
    config: str = typer.Option("config/testnet.toml", "--config", "-c"),
    publisher_url: str = typer.Option("https://api.cathedral.computer", "--publisher-url"),
    public_key_env: str = typer.Option(
        "CATHEDRAL_PUBLIC_KEY_HEX",
        "--public-key-env",
        help="Env var holding the cathedral signing public key (32-byte hex).",
    ),
    interval_secs: float = typer.Option(30.0, "--interval-secs"),
) -> None:
    """V1 pull loop — poll publisher's leaderboard/recent + set weights.

    This is the canonical V1 validator loop:
        publisher /v1/leaderboard/recent
            -> verify cathedral signature
            -> persist to local sqlite (pulled_eval_runs)
        weight_loop
            -> reads rolling 30-day score per hotkey
            -> set Bittensor weights
    """
    configure()

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    from cathedral.chain import BittensorChain
    from cathedral.config import ValidatorSettings, resolve_validator_config_path
    from cathedral.validator.health import Health
    from cathedral.validator.pull_loop import run_pull_loop
    from cathedral.validator.weight_loop import run_weight_loop

    pubkey_hex = os.environ.get(public_key_env)
    if not pubkey_hex:
        raise typer.BadParameter(f"env var {public_key_env} not set")
    pubkey = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)

    async def _run() -> None:
        conn = await connect(settings.storage.database_path)
        health = Health()
        chain = BittensorChain(
            network=settings.network.name,
            netuid=settings.network.netuid,
            wallet_name=settings.network.wallet_name,
            wallet_hotkey=settings.network.validator_hotkey,
            wallet_path=settings.network.wallet_path,
        )
        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(
                run_pull_loop(
                    conn=conn,
                    publisher_url=publisher_url,
                    cathedral_public_key=pubkey,
                    health=health,
                    interval_secs=interval_secs,
                    stop=stop,
                )
            ),
            asyncio.create_task(
                run_weight_loop(
                    conn,
                    chain,
                    health,
                    interval_secs=settings.weights.interval_secs,
                    disabled=settings.weights.disabled,
                    burn_uid=settings.weights.burn_uid,
                    forced_burn_percentage=settings.weights.forced_burn_percentage,
                    v3_bug_isolation_weight=settings.weights.v3_bug_isolation_weight,
                    task_family_weights=settings.weights.task_family_weights,
                    stop=stop,
                )
            ),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            stop.set()
            await conn.close()

    asyncio.run(_run())


if __name__ == "__main__":
    app()
