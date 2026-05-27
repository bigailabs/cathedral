from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from cathedral.publisher import repository
from cathedral.publisher.cli import app as publisher_app
from cathedral.publisher.remote_weight_preflight import (
    run_publisher_remote_weight_preflight,
)
from cathedral.validator.db import connect


def _signing_seed() -> str:
    return "11" * 32


def _env() -> dict[str, str]:
    return {
        "CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY": _signing_seed(),
        "CATHEDRAL_WEIGHT_POLICY_NETWORK": "finney",
        "CATHEDRAL_WEIGHT_POLICY_NETUID": "39",
        "CATHEDRAL_WEIGHT_POLICY_KEY_ID": "cathedral-weight-policy",
        "CATHEDRAL_WEIGHT_POLICY_BURN_UID": "204",
        "CATHEDRAL_WEIGHT_POLICY_FORCED_BURN_PERCENTAGE": "85.0",
        "CATHEDRAL_WEIGHT_POLICY_VALID_FOR_SECS": "86400",
    }


def _fresh_issued_at() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def test_remote_weight_preflight_builds_private_safe_signed_vector(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_ranked_score(db_path))

    result = asyncio.run(
        run_publisher_remote_weight_preflight(
            str(db_path),
            env=_env(),
            issued_at=_fresh_issued_at(),
        )
    )
    payload = json.dumps(result.details, sort_keys=True)

    assert result.ok
    assert result.errors == ()
    assert result.details["network"] == "finney"
    assert result.details["netuid"] == 39
    assert result.details["weight_entries"] == 1
    assert result.details["has_signature"] is True
    assert result.details["policy_metadata"]["ranked_hotkeys"] == 1
    assert "hk-a" not in payload


def test_remote_weight_preflight_rejects_missing_signing_key(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_ranked_score(db_path))

    result = asyncio.run(
        run_publisher_remote_weight_preflight(
            str(db_path),
            env={},
            issued_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
        )
    )

    assert not result.ok
    assert result.errors == ("CATHEDRAL_WEIGHT_POLICY_SIGNING_KEY is required",)


def test_remote_weight_preflight_warns_on_empty_vector(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_empty_schema(db_path))

    result = asyncio.run(
        run_publisher_remote_weight_preflight(
            str(db_path),
            env=_env(),
            issued_at=_fresh_issued_at(),
        )
    )

    assert result.ok
    assert result.details["weight_entries"] == 0
    assert (
        "signed vector has no miner weight entries; burn fallback must be intentional"
        in result.warnings
    )


def test_publisher_cli_remote_weight_preflight_prints_status_without_hotkeys(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_seed_ranked_score(db_path))

    result = CliRunner().invoke(
        publisher_app,
        ["remote-weight-vector-preflight", "--db", str(db_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "Publisher remote weight vector preflight passed" in result.output
    assert '"weight_entries": 1' in result.output
    assert "hk-a" not in result.output


async def _seed_empty_schema(db_path) -> None:
    conn = await connect(str(db_path))
    await conn.close()


async def _seed_ranked_score(db_path) -> None:
    conn = await connect(str(db_path))
    try:
        await repository.insert_card_definition(
            conn,
            id="eu-ai-act",
            display_name="EU AI Act",
            jurisdiction="EU",
            topic="AI policy",
            description="desc",
            eval_spec_md="spec",
            source_pool=[],
            task_templates=[],
            scoring_rubric={},
        )
        await repository.insert_agent_submission(
            conn,
            id="agent-a",
            miner_hotkey="hk-a",
            card_id="eu-ai-act",
            bundle_blob_key="blob-a",
            bundle_hash="hash-a",
            bundle_size_bytes=10,
            encryption_key_id="key",
            bundle_signature="sig",
            display_name="Agent A",
            bio=None,
            logo_url=None,
            soul_md_preview=None,
            metadata_fingerprint="fp-a",
            similarity_check_passed=True,
            rejection_reason=None,
            status="queued",
            submitted_at=datetime(2026, 5, 21, 12, 0, tzinfo=UTC),
            first_mover_at=None,
        )
        await repository.update_submission_score(
            conn,
            "agent-a",
            current_score=0.72,
            current_rank=1,
        )
    finally:
        await conn.close()
