"""Tests for the ``cathedral-publisher promote-pending`` operator subcommand.

Covers the batch-promote CLI added for the PR-bundle (#241 multi-active +
operator batch promote). Drives the typer app end-to-end so the wiring
of ``--tier``, ``--kind``, ``--difficulty-label``, and ``--max`` is
exercised exactly as an operator would invoke it.
"""

from __future__ import annotations

import asyncio

from typer.testing import CliRunner

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    SqliteChallengeSource,
    init_sqlite_challenge_source,
)
from cathedral.publisher.cli import app as publisher_app

_FAMILY = "synthetic_boolean_v1"


async def _seed_pending(db_path: str, rows: list[ChallengeRecord]) -> None:
    conn = await init_sqlite_challenge_source(db_path)
    try:
        source = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        for rec in rows:
            await source.upsert(rec)
    finally:
        await conn.close()


def _record(
    challenge_id: str,
    *,
    tier: int,
    difficulty_label: str | None = None,
    kind: str = "sha256_preimage",
) -> ChallengeRecord:
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=tier,
        cnf_text="p cnf 1 1\n1 0\n",
        status=CHALLENGE_STATUS_PENDING,
        audit_metadata={"kind": kind},
        difficulty_label=difficulty_label,
    )


def test_promote_pending_cli_promotes_one_labeled_row(tmp_path) -> None:
    """Single labeled row + max=10 → promote=1, active set contains it."""
    db_path = str(tmp_path / "publisher.db")
    asyncio.run(
        _seed_pending(
            db_path,
            [_record("c1", tier=1, difficulty_label="3b")],
        )
    )
    result = CliRunner().invoke(
        publisher_app,
        [
            "promote-pending",
            "--db",
            db_path,
            "--tier",
            "1",
            "--difficulty-label",
            "3b",
            "--max",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "promoted_count=1" in result.output
    assert "c1" in result.output


def test_promote_pending_cli_kind_filter_with_label(tmp_path) -> None:
    """``--kind`` narrows the batch when combined with ``--difficulty-label``."""
    db_path = str(tmp_path / "publisher.db")
    asyncio.run(
        _seed_pending(
            db_path,
            [
                _record("a", tier=1, difficulty_label="d1", kind="random_3sat"),
                _record("b", tier=1, difficulty_label="d1", kind="sha256_preimage"),
            ],
        )
    )
    result = CliRunner().invoke(
        publisher_app,
        [
            "promote-pending",
            "--db",
            db_path,
            "--tier",
            "1",
            "--difficulty-label",
            "d1",
            "--kind",
            "random_3sat",
            "--max",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    # Only the random_3sat row matches the kind filter within the label.
    assert "promoted_count=1" in result.output
    assert "promoted_ids=a" in result.output


def test_promote_pending_cli_no_label_filters_to_unlabeled_only(tmp_path) -> None:
    """No ``--difficulty-label`` means promote ONLY unlabeled rows.

    Labeled rows must NOT be promoted under nominal ``'tier'`` scope —
    that would violate the legacy one-active-per-(family, tier) rule
    for the unlabeled slot.
    """
    db_path = str(tmp_path / "publisher.db")
    asyncio.run(
        _seed_pending(
            db_path,
            [
                _record("unlabeled-1", tier=2),
                _record("labeled-1", tier=2, difficulty_label="d1"),
            ],
        )
    )
    result = CliRunner().invoke(
        publisher_app,
        ["promote-pending", "--db", db_path, "--tier", "2", "--max", "10"],
    )
    assert result.exit_code == 0, result.output
    assert "promoted_count=1" in result.output
    assert "promoted_ids=unlabeled-1" in result.output
