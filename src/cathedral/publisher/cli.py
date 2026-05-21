"""`cathedral-publisher` CLI."""

from __future__ import annotations

import asyncio
import os
from typing import cast

import structlog
import typer
import uvicorn

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


@app.command("seed-cards")
def seed_cards(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
) -> None:
    """Seed `card_definitions` with the v1 launch card IDs.

    The full per-card content (eval_spec_md, source_pool, task_templates)
    is owned by the cathedral-eval-spec content repo. This CLI command
    seeds placeholder rows so a fresh database can accept submissions
    immediately while the content team finalises the per-card YAML.
    """
    configure()

    from cathedral.publisher import repository

    launch_cards = [
        ("eu-ai-act", "EU AI Act", "eu", "EU AI Act enforcement and guidance"),
    ]

    async def _run() -> None:
        conn = await connect(database_path)
        try:
            for card_id, name, juris, topic in launch_cards:
                await repository.insert_card_definition(
                    conn,
                    id=card_id,
                    display_name=name,
                    jurisdiction=juris,
                    topic=topic,
                    description=(
                        f"## {name}\n\nPlaceholder definition. Owned by cathedral-eval-spec."
                    ),
                    eval_spec_md="Placeholder eval spec — owned by cathedral-eval-spec.",
                    source_pool=[],
                    task_templates=[
                        f"Summarize material {name} developments in the last 24 hours.",
                        f"What changed in {name} this week?",
                    ],
                    scoring_rubric={
                        "source_quality_weight": 0.30,
                        "maintenance_weight": 0.20,
                        "freshness_weight": 0.15,
                        "specificity_weight": 0.15,
                        "usefulness_weight": 0.10,
                        "clarity_weight": 0.10,
                        "required_source_classes": ["official_journal", "regulator"],
                        "min_summary_chars": 40,
                        "max_summary_chars": 800,
                        "min_citations": 1,
                    },
                    refresh_cadence_hours=24,
                    status="active",
                )
                typer.echo(f"seeded {card_id}")
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command("load-eval-spec")
def load_eval_spec(
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
    repo_url: str = typer.Option(
        "https://raw.githubusercontent.com/cathedralai/cathedral-eval-spec/main",
        "--repo-url",
        help="Base URL of the cathedral-eval-spec content (raw GitHub).",
    ),
    cards: str = typer.Option(
        "eu-ai-act",
        "--cards",
        help="Comma-separated card IDs to load.",
    ),
) -> None:
    """Pull cathedral-eval-spec/<card>/card_definition.toml from the public
    repo and UPDATE existing card_definitions rows with the real content.

    Idempotent — safe to run on every deploy. Does not delete or insert,
    only updates the description/eval_spec_md/source_pool/task_templates/
    scoring_rubric/refresh_cadence_hours columns of existing rows.

    Run AFTER `seed-cards` (which creates the placeholder rows).
    """
    configure()

    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[import-not-found]
    import urllib.parse
    import urllib.request

    from cathedral.publisher import repository

    card_ids = [c.strip() for c in cards.split(",") if c.strip()]

    def _fetch_url_bytes(url: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"unsupported URL scheme: {parsed.scheme}")
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            return cast(bytes, resp.read())

    async def _run() -> None:
        conn = await connect(database_path)
        try:
            for card_id in card_ids:
                url = f"{repo_url.rstrip('/')}/{card_id}/card_definition.toml"
                typer.echo(f"fetching {url}")
                try:
                    body = await asyncio.to_thread(_fetch_url_bytes, url)
                except Exception as e:
                    typer.echo(f"  FAILED: {e}", err=True)
                    continue

                try:
                    data = tomllib.loads(body.decode("utf-8"))
                except Exception as e:
                    typer.echo(f"  FAILED to parse TOML: {e}", err=True)
                    continue

                # Map TOML structure to repository columns. The TOML uses
                # `[card]`, `[description]`, `[eval_spec]`, `[source_pool]`,
                # `[task_templates]`, `[scoring_rubric]`, `[refresh]`.
                card = data.get("card", {})
                desc = (data.get("description") or {}).get("markdown", "")
                spec = (data.get("eval_spec") or {}).get("markdown", "")
                source_pool = (data.get("source_pool") or {}).get("sources", [])
                task_templates = (data.get("task_templates") or {}).get("templates", [])
                scoring = data.get("scoring_rubric") or {}
                cadence = (data.get("refresh") or {}).get("recommended_cadence_hours", 24)

                # insert_card_definition has ON CONFLICT DO UPDATE — idempotent
                await repository.insert_card_definition(
                    conn,
                    id=card.get("id", card_id),
                    display_name=card.get("display_name", card_id),
                    jurisdiction=card.get("jurisdiction", "other"),
                    topic=card.get("topic", ""),
                    description=desc,
                    eval_spec_md=spec,
                    source_pool=source_pool,
                    task_templates=task_templates,
                    scoring_rubric=scoring,
                    refresh_cadence_hours=int(cadence),
                    status=card.get("status", "active"),
                )
                typer.echo(
                    f"  loaded {card_id}: {len(source_pool)} sources, "
                    f"{len(task_templates)} task templates"
                )
        finally:
            await conn.close()

    asyncio.run(_run())


@app.command("archive-cards")
def archive_cards(
    card_ids: list[str] = typer.Argument(  # noqa: B008
        ...,
        help="One or more card IDs to mark status='archived'.",
    ),
    database_path: str = typer.Option("data/publisher.db", "--db", "-d"),
) -> None:
    """Mark existing card_definitions rows as ``status='archived'``.

    Idempotent. Safe to run on every deploy. Cards that are already
    archived stay archived; cards that do not exist are skipped (no error,
    no insert). Used by the Docker startup sequence to deprecate cards
    that were part of an earlier launch plan: archived cards return 404
    from ``POST /v1/agents/submit`` and ``GET /v1/cards/{id}/eval-spec``.

    Example::

        cathedral-publisher archive-cards us-ai-eo uk-ai-whitepaper
    """
    configure()

    from cathedral.publisher import repository

    async def _run() -> None:
        conn = await connect(database_path)
        try:
            for card_id in card_ids:
                updated = await repository.set_card_definition_status(
                    conn, card_id=card_id, status="archived"
                )
                if updated:
                    typer.echo(f"archived {card_id}")
                else:
                    typer.echo(f"skipped {card_id} (no row)")
            await conn.commit()
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
) -> None:
    """Validate SAT launch environment without writing to the publisher DB."""
    configure()

    from cathedral.publisher.sat_preflight import run_synthetic_boolean_launch_preflight

    result = run_synthetic_boolean_launch_preflight(
        require_eval_signing_key=require_eval_signing_key,
        require_weight_signing_key=require_weight_signing_key,
    )

    detail_keys = (
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


@app.callback()
def _callback() -> None:
    """Common config (no-op; lets typer build subcommand help cleanly)."""
    _ = os.environ.get("CATHEDRAL_ENV", "")  # touch env so help docs hint at it


if __name__ == "__main__":
    app()
