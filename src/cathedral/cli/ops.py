"""`cathedral` operator CLI - health, weights, registration, chain check."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="Cathedral operator CLI")


@app.callback()
def _root() -> None:
    """Cathedral operator CLI - inspect health, weights, registration, chain."""


@app.command()
def health(validator_url: str = typer.Option("http://127.0.0.1:9333")) -> None:
    """Print the validator health snapshot as JSON."""
    body = asyncio.run(_get(f"{validator_url.rstrip('/')}/health"))
    typer.echo(json.dumps(body, indent=2, default=str))


@app.command()
def weights(validator_url: str = typer.Option("http://127.0.0.1:9333")) -> None:
    """Print the current weight-setting status as a single word."""
    body = asyncio.run(_get(f"{validator_url.rstrip('/')}/health"))
    typer.echo(body.get("weight_status") or "unknown")


@app.command()
def registration(validator_url: str = typer.Option("http://127.0.0.1:9333")) -> None:
    """Confirm the validator's hotkey is on the metagraph."""
    body = asyncio.run(_get(f"{validator_url.rstrip('/')}/health"))
    typer.echo(f"registered: {bool(body.get('registered'))}")


@app.command(name="chain-check")
def chain_check(
    config: str = typer.Option("config/testnet.toml", "--config", "-c"),
) -> None:
    """Smoke-test the Bittensor chain connection without starting the validator.

    Reads the validator config, opens a Subtensor connection, prints the
    current block, the validator's registration status, and the size of the
    metagraph. Exits non-zero on any failure.
    """
    from cathedral.chain import BittensorChain  # heavy import
    from cathedral.config import ValidatorSettings, resolve_validator_config_path

    config = resolve_validator_config_path(config)
    settings = ValidatorSettings.from_toml(config)
    chain = BittensorChain(
        network=settings.network.name,
        netuid=settings.network.netuid,
        wallet_name=settings.network.wallet_name,
        wallet_hotkey=settings.network.validator_hotkey,
        wallet_path=settings.network.wallet_path,
    )

    async def _run() -> None:
        block = await chain.current_block()
        registered = await chain.is_registered()
        mg = await chain.metagraph()
        typer.echo(
            json.dumps(
                {
                    "network": settings.network.name,
                    "netuid": settings.network.netuid,
                    "wallet_hotkey": settings.network.validator_hotkey,
                    "current_block": block,
                    "registered": registered,
                    "metagraph_block": mg.block,
                    "metagraph_size": len(mg.miners),
                },
                indent=2,
            )
        )

    asyncio.run(_run())


@app.command(name="sat-seed-challenge")
def sat_seed_challenge(
    database_path: str = typer.Option(
        "data/publisher.db",
        "--database-path",
        "--db",
        help="Publisher SQLite database path.",
    ),
    cnf_path: str = typer.Option(
        ...,
        "--cnf-path",
        help="Private DIMACS CNF path, or '-' to read from stdin.",
    ),
    challenge_id: str | None = typer.Option(
        None,
        "--challenge-id",
        help="Optional stable challenge id. Defaults to sat-<sha256-prefix>.",
    ),
    tier: int = typer.Option(0, "--tier", min=0),
    activate: bool = typer.Option(False, "--activate/--queue"),
    retire_current: bool = typer.Option(
        False,
        "--retire-current",
        help="When activating, retire a different active challenge first.",
    ),
    storage_mode: str = typer.Option(
        "sqlite_text",
        "--storage-mode",
        help="CNF storage mode: sqlite_text or file.",
    ),
) -> None:
    """Seed a private synthetic_boolean_v1 CNF into the publisher DB."""
    normalized_storage_mode = _normalize_sat_storage_mode(storage_mode)
    if normalized_storage_mode == "file" and cnf_path == "-":
        raise typer.BadParameter("--storage-mode=file requires --cnf-path to be a file")
    cnf_text = None if normalized_storage_mode == "file" else _read_cnf_runtime_input(cnf_path)
    result = asyncio.run(
        _seed_sat_challenge_async(
            database_path=database_path,
            cnf_text=cnf_text,
            cnf_path=None if cnf_path == "-" else cnf_path,
            challenge_id=challenge_id,
            tier=tier,
            activate=activate,
            retire_current=retire_current,
            input_source="operator_cnf_stdin" if cnf_path == "-" else "operator_cnf_path",
            storage_mode=normalized_storage_mode,
        )
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command(name="sat-activate-challenge")
def sat_activate_challenge(
    database_path: str = typer.Option(
        "data/publisher.db",
        "--database-path",
        "--db",
        help="Publisher SQLite database path.",
    ),
    challenge_id: str = typer.Option(..., "--challenge-id"),
    retire_current: bool = typer.Option(False, "--retire-current"),
) -> None:
    """Activate a queued synthetic_boolean_v1 challenge by id."""
    result = asyncio.run(
        _activate_sat_challenge_async(
            database_path=database_path,
            challenge_id=challenge_id,
            retire_current=retire_current,
        )
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


async def _get(url: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        body: dict[str, object] = r.json()
        return body


def _read_cnf_runtime_input(cnf_path: str) -> str:
    if cnf_path == "-":
        return typer.get_text_stream("stdin").read()
    return Path(cnf_path).expanduser().read_text(encoding="utf-8")


def _normalize_sat_storage_mode(storage_mode: str) -> str:
    normalized = storage_mode.strip().lower().replace("-", "_")
    if normalized in {"", "sqlite", "sqlite_text"}:
        return "sqlite_text"
    if normalized in {"file", "file_backed", "filesystem"}:
        return "file"
    raise typer.BadParameter("--storage-mode must be sqlite_text or file")


async def _seed_sat_challenge_async(
    *,
    database_path: str,
    cnf_text: str | None,
    cnf_path: str | None,
    challenge_id: str | None,
    tier: int,
    activate: bool,
    retire_current: bool,
    input_source: str,
    storage_mode: str = "sqlite_text",
) -> dict[str, object]:
    from cathedral.lanes.challenge_lock import SQLITE_SCHEMA as CHALLENGE_LOCK_SCHEMA
    from cathedral.lanes.challenge_ops import seed_synthetic_boolean_challenge
    from cathedral.lanes.challenge_source import (
        SQLITE_SCHEMA as CHALLENGE_SOURCE_SCHEMA,
    )
    from cathedral.lanes.challenge_source import (
        SqliteChallengeSource,
        init_sqlite_challenge_source,
    )

    conn = await init_sqlite_challenge_source(database_path)
    try:
        await conn.executescript(CHALLENGE_LOCK_SCHEMA)
        await conn.executescript(CHALLENGE_SOURCE_SCHEMA)
        await conn.commit()
        source = SqliteChallengeSource(conn)
        if storage_mode == "file":
            if not cnf_path:
                raise typer.BadParameter("--storage-mode=file requires --cnf-path")
            from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID
            from cathedral.publisher.sat_file_challenges import (
                build_synthetic_boolean_file_challenge_record,
            )

            record = build_synthetic_boolean_file_challenge_record(
                cnf_path=cnf_path,
                tier=tier,
                challenge_id=challenge_id,
                source=input_source,
            )
            await source.upsert(record, now_iso=_now_ms_iso())
            if activate:
                record = await source.activate(
                    family_id=FAMILY_ID,
                    challenge_id=record.challenge_id,
                    now_iso=_now_ms_iso(),
                    retire_current=retire_current,
                )
        else:
            assert cnf_text is not None
            record = await seed_synthetic_boolean_challenge(
                source,
                cnf_text=cnf_text,
                tier=tier,
                now_iso=_now_ms_iso(),
                challenge_id=challenge_id,
                activate=activate,
                retire_current=retire_current,
                input_source=input_source,
            )
        return _challenge_result(record)
    finally:
        await conn.close()


async def _activate_sat_challenge_async(
    *,
    database_path: str,
    challenge_id: str,
    retire_current: bool,
) -> dict[str, object]:
    from cathedral.lanes.challenge_source import (
        SqliteChallengeSource,
        init_sqlite_challenge_source,
    )
    from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID

    conn = await init_sqlite_challenge_source(database_path)
    try:
        source = SqliteChallengeSource(conn)
        record = await source.activate(
            family_id=FAMILY_ID,
            challenge_id=challenge_id,
            now_iso=_now_ms_iso(),
            retire_current=retire_current,
        )
        return _challenge_result(record)
    finally:
        await conn.close()


def _challenge_result(record: object) -> dict[str, object]:
    audit = record.audit_metadata
    return {
        "challenge_id": record.challenge_id,
        "family_id": record.family_id,
        "tier": record.tier,
        "status": record.status,
        "storage": audit.get("storage", "sqlite_text"),
        "cnf_sha256": audit.get("cnf_sha256"),
        "num_vars": audit.get("num_vars"),
        "num_clauses": audit.get("num_clauses"),
    }


def _now_ms_iso() -> str:
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


if __name__ == "__main__":
    app()
