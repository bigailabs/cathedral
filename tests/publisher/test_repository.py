"""Tests for cathedral.publisher.repository.

PR1 (SN39 recovery) regression coverage: ``list_eval_runs_recent`` is
the function backing ``/v1/leaderboard/recent``, and Path A validators
bucket every non-SAT row it returns into the v1 (card) bucket. The
publisher therefore MUST NOT serve schema_version < 5 rows from this
function — anything below 5 is card-era / legacy and a direct emissions
leak.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cathedral.publisher import repository as repo
from cathedral.validator.db import connect


async def _seed_submission(conn) -> str:
    """Insert one ranked ssh-probe submission and return its id.

    Mirrors the helper in ``tests/lanes/test_task_family_launch_rails.py``:
    the leaderboard read joins against ``agent_submissions`` and filters
    on ``status != 'discovery'`` + ``attestation_mode IN (...)``, so the
    fixture row must satisfy those gates to appear at all. A card
    definition is seeded first to satisfy the
    ``agent_submissions.card_id -> cards.id`` FK constraint.
    """
    await repo.insert_card_definition(
        conn,
        id="eu-ai-act",
        display_name="EU AI Act",
        jurisdiction="EU",
        topic="AI Act",
        description="legacy v1 card (PR1 leaderboard filter fixture)",
        eval_spec_md="spec",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
    )
    submitted_at = datetime(2026, 5, 18, 19, 0, 0, tzinfo=UTC)
    sub_id = "sub-pr1-leaderboard-filter"
    await repo.insert_agent_submission(
        conn,
        id=sub_id,
        miner_hotkey="5MinerPr1Filter",
        card_id="eu-ai-act",
        bundle_blob_key="bundles/sub-pr1-leaderboard-filter.zip",
        bundle_hash="0" * 64,
        bundle_size_bytes=1024,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name="PR1 Filter Miner",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint="fp-pr1-filter",
        similarity_check_passed=True,
        rejection_reason=None,
        status="ranked",
        submitted_at=submitted_at,
        submitted_at_iso="2026-05-18T19:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        discovery_only=False,
        ssh_host="203.0.113.10",
        ssh_port=22,
        ssh_user="cathedral",
    )
    return sub_id


async def _insert_eval_run_at_schema(
    conn,
    *,
    submission_id: str,
    schema_version: int,
    minute: int,
) -> str:
    """Insert one eval_runs row at the given schema version.

    ``minute`` discriminates ``ran_at`` so the rows have distinct
    timestamps and the test can assert on counts deterministically.
    """
    ran_at = datetime(2026, 5, 18, 20, minute, 0, tzinfo=UTC)
    ran_at_iso = f"2026-05-18T20:{minute:02d}:00.000Z"
    eval_id = f"00000000-0000-4000-8000-0000000005{schema_version:02d}"
    await repo.insert_eval_run(
        conn,
        id=eval_id,
        submission_id=submission_id,
        epoch=500 + schema_version,
        round_index=0,
        polaris_agent_id="ssh-hermes:5MinerPr1Filter",
        polaris_run_id=f"run-schema{schema_version}",
        task_json={"task_type": f"schema_{schema_version}"},
        output_card_json={"task_type": f"schema_{schema_version}"},
        output_card_hash="a" * 64,
        score_parts={"binary_correct": 1.0},
        weighted_score=1.0,
        ran_at=ran_at,
        ran_at_iso=ran_at_iso,
        duration_ms=42,
        errors=None,
        cathedral_signature="b64:stub",
        polaris_verified=False,
        trace_json=None,
        eval_output_schema_version=schema_version,
    )
    return eval_id


@pytest.mark.asyncio
async def test_list_eval_runs_recent_only_returns_schema_5_or_higher(tmp_path) -> None:
    """The leaderboard endpoint MUST scope reads to SAT-era rows.

    PR1 of the SN39 recovery plan adds an unconditional
    ``eval_output_schema_version >= 5`` filter to
    ``list_eval_runs_recent`` so that card-era rows
    (``eval_output_schema_version=1``, ``task_type='eu-ai-act'`` etc.)
    can never appear on ``/v1/leaderboard/recent`` again. Path A
    validators bucket every non-schema-5 row as v1 (card) primary
    incentive, which redirected real emissions to legacy card miners —
    this filter closes that leak at the source.

    The fixture inserts one row at each of schema versions 1, 3, 4, 5
    and asserts only the schema-5 row is returned. Schema versions 2
    and 4 are not currently produced by any live writer, but covering
    intermediate values keeps the filter regression observable for
    future schema additions too.
    """
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission_id = await _seed_submission(conn)
        # Insert one row at each schema version we care about.
        for minute, schema_version in enumerate((1, 3, 4, 5), start=0):
            await _insert_eval_run_at_schema(
                conn,
                submission_id=submission_id,
                schema_version=schema_version,
                minute=minute,
            )

        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=True,
            include_task_families=True,
        )

        schema_versions = sorted(r["eval_output_schema_version"] for r in rows)
        assert schema_versions == [5], (
            "list_eval_runs_recent must only return SAT-era (schema >= 5) "
            f"rows; got {schema_versions!r}"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_list_eval_runs_recent_filter_holds_on_tuple_cursor(tmp_path) -> None:
    """The schema filter must also apply on the v1.1.0 tuple-cursor branch.

    ``list_eval_runs_recent`` has two SQL branches dispatched on
    ``since_id``: ``ran_at > ?`` strict-legacy when ``since_id is None``
    (the default in the previous test in this module) and a row-value
    tuple comparison ``(ran_at, id) > (?, ?)`` when ``since_id`` is a
    string (including the empty string the v1.1.0 reads handler defaults
    to). Both branches share the interpolated ``schema_gate`` today, but
    pinning the filter explicitly under the tuple branch catches a future
    refactor that accidentally drops it from one branch.
    """
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission_id = await _seed_submission(conn)
        for minute, schema_version in enumerate((1, 3, 4, 5), start=0):
            await _insert_eval_run_at_schema(
                conn,
                submission_id=submission_id,
                schema_version=schema_version,
                minute=minute,
            )

        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repo.list_eval_runs_recent(
            conn,
            since=since,
            since_id="",  # opt into the v1.1.0 tuple-cursor branch
            include_v3=True,
            include_task_families=True,
        )

        schema_versions = sorted(r["eval_output_schema_version"] for r in rows)
        assert schema_versions == [5], (
            "tuple-cursor branch must also enforce schema >= 5; "
            f"got {schema_versions!r}"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_list_eval_runs_recent_task_family_kill_switch_excludes_schema_5(
    tmp_path,
) -> None:
    """``include_task_families=False`` excludes the schema-5 lane.

    This is the rollback semantics behind
    ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED``: turning the kill-switch off
    drops schema-5 rows. Composed with the new ``schema >= 5`` floor,
    today (no schema 6+ writer exists) the feed is empty; this test
    pins the floor + the existing exclude gate so a future refactor
    can't reintroduce a leak.
    """
    conn = await connect(str(tmp_path / "publisher.db"))
    try:
        submission_id = await _seed_submission(conn)
        for minute, schema_version in enumerate((1, 3, 4, 5), start=0):
            await _insert_eval_run_at_schema(
                conn,
                submission_id=submission_id,
                schema_version=schema_version,
                minute=minute,
            )

        since = datetime(2000, 1, 1, tzinfo=UTC)
        rows = await repo.list_eval_runs_recent(
            conn,
            since=since,
            include_v3=True,
            include_task_families=False,
        )

        schema_versions = sorted(r["eval_output_schema_version"] for r in rows)
        assert 5 not in schema_versions, (
            "include_task_families=False must exclude schema-5 rows; "
            f"got {schema_versions!r}"
        )
        # Today's row population (schemas 1/3/4/5) means the kill-switch
        # composed with the >=5 floor yields an empty feed.
        assert schema_versions == [], (
            "kill-switch + >=5 floor must yield no rows for today's schemas; "
            f"got {schema_versions!r}"
        )
    finally:
        await conn.close()
