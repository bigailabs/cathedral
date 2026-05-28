"""`cathedral-publisher` CLI."""

from __future__ import annotations

import asyncio
import json
import os

import structlog
import typer
import uvicorn

from cathedral.lanes.synthetic_boolean_v1 import DEFAULT_TIME_LIMIT_SECONDS
from cathedral.logging import configure
from cathedral.validator.db import connect

logger = structlog.get_logger(__name__)


app = typer.Typer(no_args_is_help=True, help="Cathedral publisher (api.cathedral.computer)")


@app.command()
def serve(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(9444, "--port"),
    json_logs: bool = typer.Option(True, "--json-logs/--no-json-logs"),
    log_level: str = typer.Option("info"),
) -> None:
    """Run the publisher HTTP server with the eval orchestrator background loop."""
    configure(level=log_level.upper(), json_logs=json_logs)
    from cathedral.publisher import from_settings

    application = from_settings(database_path)
    uvicorn.run(application, host=host, port=port, log_level=log_level, access_log=False)


@app.command()
def migrate(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
) -> None:
    """Initialize the sqlite schema. Idempotent — safe to run on every deploy."""
    configure()

    async def _run() -> None:
        conn = await connect(database_path)
        await conn.close()

    asyncio.run(_run())
    typer.echo(f"schema ready at {database_path}")


@app.command("merkle-close")
def merkle_close(
    epoch: int = typer.Option(..., "--epoch", "-e", help="ISO calendar epoch (year * 100 + week)"),
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    on_chain: bool = typer.Option(False, "--on-chain", help="Submit anchor to chain"),
    network: str = typer.Option("finney", "--network"),
    wallet_name: str = typer.Option("default", "--wallet-name"),
    wallet_hotkey: str = typer.Option("default", "--wallet-hotkey"),
) -> None:
    """Compute Merkle root for an epoch, persist anchor, optionally submit on-chain."""
    configure()
    from cathedral.publisher import merkle as merkle_mod

    async def _run() -> None:
        conn = await connect(database_path)
        try:
            anchorer = None
            if on_chain:
                from cathedral.chain.anchor import BittensorAnchorer

                anchorer = BittensorAnchorer(
                    network=network,
                    wallet_name=wallet_name,
                    wallet_hotkey=wallet_hotkey,
                )
            result = await merkle_mod.close_epoch(conn, epoch, anchorer=anchorer)
            typer.echo(
                f"epoch={epoch} root={result['merkle_root']} "
                f"eval_count={result['eval_count']} "
                f"on_chain_block={result['on_chain_block']}"
            )
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command("sat-launch-preflight")
def sat_launch_preflight(
    require_eval_signing_key: bool = typer.Option(
        True,
        "--require-eval-signing-key/--no-require-eval-signing-key",
        help="Require CATHEDRAL_EVAL_SIGNING_KEY to be present and well-formed.",
    ),
    require_weight_signing_key: bool = typer.Option(
        True,
        "--require-weight-signing-key/--no-require-weight-signing-key",
        help="Require CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY for signed remote weights.",
    ),
    require_runtime_env: bool = typer.Option(
        True,
        "--require-runtime-env/--no-require-runtime-env",
        help="Require SAT feed, SSH/Hermes, prober v2, and public base URL env.",
    ),
) -> None:
    """Validate SAT launch environment without writing to the publisher DB."""
    configure()

    from cathedral.publisher.sat_preflight import run_synthetic_boolean_launch_preflight

    result = run_synthetic_boolean_launch_preflight(
        require_eval_signing_key=require_eval_signing_key,
        require_weight_signing_key=require_weight_signing_key,
        require_runtime_env=require_runtime_env,
    )

    detail_keys = (
        "task_family_feed_enabled",
        "task_family_ids",
        "eval_mode",
        "prober_version",
        "public_base_url",
        "storage_mode",
        "max_cnf_bytes_enforced",
        "challenge_id",
        "tier",
        "num_vars",
        "num_clauses",
        "cnf_file_bytes",
        "cnf_sha256",
    )
    for key in detail_keys:
        if key in result.details:
            typer.echo(f"{key}: {result.details[key]}")
    for warning in result.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)
    typer.echo("SAT launch preflight passed")


@app.command("remote-weight-vector-preflight")
def remote_weight_vector_preflight(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
) -> None:
    """Build and self-verify one signed remote weight vector from the DB."""
    configure()

    from cathedral.publisher.remote_weight_preflight import (
        run_publisher_remote_weight_preflight,
    )

    result = asyncio.run(run_publisher_remote_weight_preflight(database_path))
    typer.echo(json.dumps(result.details, indent=2, sort_keys=True))
    for warning in result.warnings:
        typer.echo(f"WARNING: {warning}", err=True)
    if result.errors:
        for error in result.errors:
            typer.echo(f"ERROR: {error}", err=True)
        raise typer.Exit(1)
    typer.echo("Publisher remote weight vector preflight passed")


@app.command("sat-active-challenge-status")
def sat_active_challenge_status(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    verify_cnf_hash: bool = typer.Option(
        False,
        "--verify-cnf-hash/--no-verify-cnf-hash",
        help="Stream the active file-backed CNF and compare it with audit metadata.",
    ),
) -> None:
    """Print private-safe active SAT challenge status from the publisher DB."""
    configure()

    from cathedral.publisher.sat_status import active_sat_challenge_status_from_db

    result = asyncio.run(
        active_sat_challenge_status_from_db(
            database_path,
            verify_file_hash=verify_cnf_hash,
        )
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("sat-active-cnf-probe")
def sat_active_cnf_probe(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    public_base_url: str = typer.Option(
        "",
        "--public-base-url",
        help="Public publisher base URL. Defaults to CATHEDRAL_PUBLIC_BASE_URL.",
    ),
    timeout_secs: float = typer.Option(300.0, "--timeout-secs", min=0.1),
    min_bytes_per_second: float = typer.Option(
        0.0,
        "--min-bytes-per-second",
        min=0.0,
    ),
    announced_time_limit_secs: int = typer.Option(
        DEFAULT_TIME_LIMIT_SECONDS, "--announced-time-limit-secs", min=1
    ),
) -> None:
    """Fetch the active SAT CNF through the public URL and verify its hash."""
    configure()

    from cathedral.publisher.sat_cnf_probe import probe_active_sat_cnf_url_from_db

    result = asyncio.run(
        probe_active_sat_cnf_url_from_db(
            database_path,
            public_base_url=public_base_url or os.environ.get("CATHEDRAL_PUBLIC_BASE_URL", ""),
            timeout_secs=timeout_secs,
            min_bytes_per_second=min_bytes_per_second,
            announced_time_limit_secs=announced_time_limit_secs,
        )
    )
    typer.echo(json.dumps(result, indent=2, sort_keys=True))
    if not result.get("ok"):
        raise typer.Exit(1)


@app.command("promote-pending")
def promote_pending(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    tier: int = typer.Option(..., "--tier", help="Required: tier whose pending rows to promote."),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Optional: narrow to rows whose audit_metadata.kind matches.",
    ),
    difficulty_label: str | None = typer.Option(
        None,
        "--difficulty-label",
        help="Optional: narrow to rows with this difficulty_label "
        "(uses active_scope='tier_difficulty').",
    ),
    max_count: int = typer.Option(
        30, "--max", min=1, help="Maximum number of rows to promote."
    ),
    family_id: str = typer.Option(
        "synthetic_boolean_v1",
        "--family-id",
        help="Task Family identifier; defaults to SAT.",
    ),
) -> None:
    """Promote up to N pending challenges into the active set in one call.

    Operator entry point for issue #241 multi-active rollouts. When
    ``--difficulty-label`` is set the promotion runs under
    ``active_scope='tier_difficulty'`` so labeled rows can share a tier
    slot; otherwise it falls back to ``active_scope='tier'`` which
    preserves the legacy one-active-per-(family, tier) invariant for
    unlabeled rows. Prints the promoted ids plus the resulting active
    set for the filter so the operator can audit the outcome.
    """
    configure()

    async def _run() -> int:
        from cathedral.lanes.challenge_source import (
            SqliteChallengeSource,
            init_sqlite_challenge_source,
        )

        conn = await init_sqlite_challenge_source(database_path)
        try:
            source = SqliteChallengeSource(conn)
            from datetime import UTC, datetime

            now_iso = (
                datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{datetime.now(UTC).microsecond // 1000:03d}"
                + "Z"
            )
            promoted = await source.promote_pending_batch(
                family_id,
                tier=tier,
                kind=kind,
                difficulty_label=difficulty_label,
                now_iso=now_iso,
                max_count=max_count,
            )
            actives = [
                rec
                for rec in await source.list_active(family_id)
                if rec.tier == tier
                and (difficulty_label is None or rec.difficulty_label == difficulty_label)
                and (kind is None or (rec.audit_metadata or {}).get("kind") == kind)
            ]
            typer.echo(f"promoted_count={len(promoted)}")
            typer.echo("promoted_ids=" + ",".join(promoted))
            typer.echo(
                "active_ids=" + ",".join(rec.challenge_id for rec in actives)
            )
            return 0
        finally:
            await conn.close()

    code = asyncio.run(_run())
    if code != 0:
        raise typer.Exit(code)


@app.callback()
def _callback() -> None:
    """Common config (no-op; lets typer build subcommand help cleanly)."""
    _ = os.environ.get("CATHEDRAL_ENV", "")  # touch env so help docs hint at it


if __name__ == "__main__":
    app()
