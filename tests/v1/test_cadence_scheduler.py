"""Tests for the v1.1.0 cadence scheduler (cathedralai/cathedral#75 PR 5).

Locks the cadence-query semantics + the orchestrator's
publish-and-pass-published_artifact behaviour:

- `submissions_due_for_cadence` returns `ranked` submissions whose
  `now - max(eval_runs.ran_at) >= card.refresh_cadence_hours`
- `queued` submissions remain on the queued_submissions path (first-eval)
- discovery / rejected rows are excluded from cadence
- the orchestrator's `_maybe_publish_bundle` returns None when env
  flag is off; calls publisher when on + trace_bundle present;
  swallows publish failures (best-effort, eval still scores)
"""

from __future__ import annotations

import importlib.util as _ilu
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SAT_CARD_ID = "synthetic_boolean_v1"


# Direct module load (publisher import cycle avoidance, same dance
# as test_bundle_publisher.py + test_ssh_hermes_runner.py)
def _load_repository() -> Any:
    if "cathedral.publisher.repository" in sys.modules:
        return sys.modules["cathedral.publisher.repository"]
    path = _ROOT / "src" / "cathedral" / "publisher" / "repository.py"
    spec = _ilu.spec_from_file_location("cathedral.publisher.repository", path)
    assert spec and spec.loader
    mod = _ilu.module_from_spec(spec)
    sys.modules["cathedral.publisher.repository"] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# submissions_due_for_cadence — the load-bearing query
# --------------------------------------------------------------------------


async def _seed_card_def(conn: Any, *, card_id: str = "eu-ai-act", cadence_hours: int = 24) -> None:
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        "INSERT OR REPLACE INTO card_definitions "
        "(id, display_name, jurisdiction, topic, description, eval_spec_md, "
        " source_pool, task_templates, scoring_rubric, refresh_cadence_hours, "
        " status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            "Test Card",
            "test",
            "test",
            "test",
            "# spec",
            "[]",
            "[]",
            "{}",
            cadence_hours,
            "active",
            now,
            now,
        ),
    )
    await conn.commit()


async def _seed_submission(
    conn: Any,
    *,
    card_id: str,
    status: str,
    submitted_at: datetime,
    discovery_only: bool = False,
    attestation_mode: str = "ssh-probe",
) -> str:
    repo = _load_repository()
    sub_id = secrets.token_hex(16)
    await repo.insert_agent_submission(
        conn,
        id=sub_id,
        miner_hotkey=f"5{secrets.token_hex(23)}",
        card_id=card_id,
        bundle_blob_key=f"bundles/{sub_id}.bin",
        bundle_hash="a" * 64,
        bundle_size_bytes=4096,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name=f"agent-{sub_id[:6]}",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=secrets.token_hex(8),
        similarity_check_passed=True,
        rejection_reason=None,
        status=status,
        submitted_at=submitted_at,
        first_mover_at=None,
        attestation_mode=attestation_mode,
        attestation_verified_at=None,
        discovery_only=discovery_only,
    )
    return sub_id


async def _seed_eval_run(
    conn: Any,
    *,
    submission_id: str,
    ran_at: datetime,
    weighted_score: float = 0.8,
) -> str:
    repo = _load_repository()
    eval_id = secrets.token_hex(16)
    await repo.insert_eval_run(
        conn,
        id=eval_id,
        submission_id=submission_id,
        epoch=1,
        round_index=0,
        polaris_agent_id="ssh-hermes:test",
        polaris_run_id=f"run-{eval_id[:8]}",
        task_json={"prompt": "test"},
        output_card_json={"id": "eu-ai-act", "title": "test card"},
        output_card_hash="h" * 64,
        score_parts={},
        weighted_score=weighted_score,
        ran_at=ran_at,
        ran_at_iso=ran_at.isoformat(),
        duration_ms=100,
        errors=None,
        cathedral_signature="b64:stub",
    )
    return eval_id


@pytest.fixture
async def db(tmp_path: Path):
    from cathedral.validator.db import connect

    conn = await connect(str(tmp_path / "cadence.db"))
    yield conn
    await conn.close()


# --------------------------------------------------------------------------
# Cadence query — happy paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ranked_submission_overdue_is_returned(db, tmp_path: Path):
    """A ranked submission with an eval older than `cadence_hours` ago
    is returned by submissions_due_for_cadence."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    # Submission scored 25h ago — past the 24h cadence window
    sub_id = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=2)
    )
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(hours=25))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 1
    assert due[0]["id"] == sub_id


@pytest.mark.asyncio
async def test_ranked_submission_within_window_not_returned(db, tmp_path: Path):
    """A ranked submission with a recent eval is NOT returned —
    it's not yet due for cadence."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    sub_id = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=2)
    )
    # Eval 2h ago — well within the 24h window
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(hours=2))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 0


@pytest.mark.asyncio
async def test_queued_submission_not_returned_by_cadence(db, tmp_path: Path):
    """The cadence query is for `ranked` rows only. Queued (first-eval)
    submissions go through the queued_submissions path. This separation
    means queued rows are prioritized in the orchestrator's batch."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="queued", submitted_at=now - timedelta(days=3)
    )
    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 0


@pytest.mark.asyncio
async def test_queued_submissions_returns_sat_pending_check_not_legacy_cards(
    db, tmp_path: Path
):
    """SAT registrations land as pending_check; legacy queued cards are dead."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    legacy = await _seed_submission(
        db, card_id="eu-ai-act", status="queued", submitted_at=now - timedelta(minutes=3)
    )
    sat_pending = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="pending_check", submitted_at=now - timedelta(minutes=2)
    )
    sat_retry = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="queued", submitted_at=now - timedelta(minutes=1)
    )

    queued = await repo.queued_submissions(db, limit=10)
    ids = [row["id"] for row in queued]
    assert ids == [sat_pending, sat_retry]
    assert legacy not in ids


@pytest.mark.asyncio
async def test_discovery_submission_not_returned_by_cadence(db, tmp_path: Path):
    """Discovery rows never run evals, so they're never due for cadence
    even when they would otherwise match."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    sub_id = await _seed_submission(
        db,
        card_id=_SAT_CARD_ID,
        status="ranked",  # hypothetical — wouldn't actually happen
        submitted_at=now - timedelta(days=5),
        discovery_only=True,
        attestation_mode="unverified",
    )
    # Even old eval can't pull a discovery row in
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(days=4))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 0


@pytest.mark.asyncio
async def test_cadence_ordering_most_overdue_first(db, tmp_path: Path):
    """When multiple rows are due, the most-overdue one comes first.
    Drains backlog fairly under load."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    a = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=10)
    )
    b = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=10)
    )
    # b is older (more overdue) than a
    await _seed_eval_run(db, submission_id=a, ran_at=now - timedelta(hours=30))
    await _seed_eval_run(db, submission_id=b, ran_at=now - timedelta(hours=72))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert [r["id"] for r in due] == [b, a]


@pytest.mark.asyncio
async def test_cadence_uses_latest_eval_not_first_eval(db, tmp_path: Path):
    """When a submission has multiple eval_runs (cadence happened
    before), the query uses MAX(ran_at), not the first eval. Without
    this, a 30-day-old submission would always look overdue."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    sub_id = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=30)
    )
    # Old eval AND a recent one — recent should win, row is NOT due
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(days=28))
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(hours=2))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 0


@pytest.mark.asyncio
async def test_cadence_with_no_eval_runs_uses_submitted_at(db, tmp_path: Path):
    """Edge case: a row marked `ranked` with no eval_runs (shouldn't
    happen in production but the query handles it via COALESCE to
    submitted_at). A ranked-but-uneval'd row submitted 25h ago against
    a 24h cadence is overdue."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    sub_id = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(hours=25)
    )
    # No eval_runs

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    assert len(due) == 1
    assert due[0]["id"] == sub_id


@pytest.mark.asyncio
async def test_cadence_respects_per_card_refresh_hours(db, tmp_path: Path):
    """SAT cadence still respects the card's refresh interval."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=12)
    await _seed_card_def(db, card_id="legacy-fast-card", cadence_hours=12)
    now = datetime.now(UTC)

    sat_sub = await _seed_submission(
        db, card_id=_SAT_CARD_ID, status="ranked", submitted_at=now - timedelta(days=2)
    )
    legacy_sub = await _seed_submission(
        db, card_id="legacy-fast-card", status="ranked", submitted_at=now - timedelta(days=2)
    )
    await _seed_eval_run(db, submission_id=sat_sub, ran_at=now - timedelta(hours=13))
    await _seed_eval_run(db, submission_id=legacy_sub, ran_at=now - timedelta(hours=13))

    due = await repo.submissions_due_for_cadence(db, now=now, limit=10)
    ids = {r["id"] for r in due}
    assert sat_sub in ids
    assert legacy_sub not in ids


@pytest.mark.asyncio
async def test_cadence_limit_bounds_batch_size(db, tmp_path: Path):
    """`limit` parameter caps the batch. Used by the orchestrator to
    cap concurrent eval work."""
    repo = _load_repository()
    await _seed_card_def(db, card_id=_SAT_CARD_ID, cadence_hours=24)
    now = datetime.now(UTC)

    for _ in range(5):
        sub_id = await _seed_submission(
            db,
                card_id=_SAT_CARD_ID,
            status="ranked",
            submitted_at=now - timedelta(days=10),
        )
        await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(hours=48))

    due_2 = await repo.submissions_due_for_cadence(db, now=now, limit=2)
    assert len(due_2) == 2

    due_all = await repo.submissions_due_for_cadence(db, now=now, limit=100)
    assert len(due_all) == 5


# --------------------------------------------------------------------------
# Repository-level scored-surface defense (cadence stale-state regression)
# --------------------------------------------------------------------------
#
# These tests exercise `list_submissions_for_card` + `list_submissions_all`
# directly (no FastAPI), pinning the new defaults:
#
# - `ranked_only=True` filters in-flight rows out of the scored surface
# - rows carry the synthetic `latest_eval_at = MAX(eval_runs.ran_at)`
# - a previously ranked + currently 'evaluating' row with prior
#   current_score is invisible to scored surfaces


@pytest.mark.asyncio
async def test_list_submissions_for_card_excludes_in_flight_rows(db, tmp_path: Path):
    """A row with status='evaluating' and a prior current_score from a
    finished round must NOT be returned by the scored-surface helper."""
    repo = _load_repository()
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    ranked_id = await _seed_submission(
        db, card_id="eu-ai-act", status="ranked", submitted_at=now - timedelta(days=2)
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.8, current_rank=1 WHERE id=?",
        (ranked_id,),
    )
    await _seed_eval_run(db, submission_id=ranked_id, ran_at=now - timedelta(hours=2))

    evaluating_id = await _seed_submission(
        db,
        card_id="eu-ai-act",
        status="evaluating",
        submitted_at=now - timedelta(days=2),
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.47, current_rank=28 WHERE id=?",
        (evaluating_id,),
    )
    await db.commit()

    items = await repo.list_submissions_for_card(db, "eu-ai-act", limit=50)
    ids = [r["id"] for r in items]
    assert ranked_id in ids and evaluating_id not in ids, (
        f"scored surface must include ranked, exclude evaluating; got {ids}"
    )


@pytest.mark.asyncio
async def test_list_submissions_for_card_exposes_latest_eval_at(db, tmp_path: Path):
    """The leaderboard's `last_eval_at` field must reflect the newest
    eval_runs.ran_at, not submitted_at. The helper exposes this as a
    synthetic `latest_eval_at` column the reads layer projects out."""
    repo = _load_repository()
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    sub_id = await _seed_submission(
        db, card_id="eu-ai-act", status="ranked", submitted_at=now - timedelta(days=10)
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.7, current_rank=1 WHERE id=?",
        (sub_id,),
    )
    await db.commit()
    # Two evals: old + fresh. MAX must surface the fresh one.
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(days=8))
    await _seed_eval_run(db, submission_id=sub_id, ran_at=now - timedelta(hours=1))

    items = await repo.list_submissions_for_card(db, "eu-ai-act", limit=50)
    assert len(items) == 1
    row = items[0]
    assert "latest_eval_at" in row, "synthetic latest_eval_at column missing"
    assert row["latest_eval_at"] > row["submitted_at"], (
        f"latest_eval_at must be newer than submitted_at; "
        f"got latest={row['latest_eval_at']!r} submitted={row['submitted_at']!r}"
    )


@pytest.mark.asyncio
async def test_list_submissions_all_excludes_in_flight_rows(db, tmp_path: Path):
    """Same ranked-only filter on the cross-card listing helper."""
    repo = _load_repository()
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    ranked_id = await _seed_submission(
        db, card_id="eu-ai-act", status="ranked", submitted_at=now - timedelta(days=2)
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.5, current_rank=1 WHERE id=?",
        (ranked_id,),
    )
    queued_id = await _seed_submission(
        db, card_id="eu-ai-act", status="queued", submitted_at=now - timedelta(days=1)
    )
    await db.commit()

    items, total = await repo.list_submissions_all(db, limit=50)
    ids = [r["id"] for r in items]
    assert ranked_id in ids and queued_id not in ids, (
        f"cross-card list must surface ranked, drop queued; got {ids}"
    )
    assert total == 1, f"total must match filtered set; got {total}"


# --------------------------------------------------------------------------
# Startup repair — stranded `evaluating` rows with prior score
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_stale_evaluating_rows_restores_ranked(db, tmp_path: Path):
    """Pre-PR-#117 pattern: row stuck in `evaluating` while still
    carrying a prior `current_score`. The startup repair must flip it
    back to `ranked` so the leaderboard surfaces it again instead of
    silently hiding it."""
    repo = _load_repository()
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    stranded_id = await _seed_submission(
        db, card_id="eu-ai-act", status="evaluating", submitted_at=now - timedelta(days=2)
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.47, current_rank=28 WHERE id=?",
        (stranded_id,),
    )

    # Control rows the repair must NOT touch:
    queued_id = await _seed_submission(
        db, card_id="eu-ai-act", status="queued", submitted_at=now - timedelta(hours=1)
    )
    fresh_eval_id = await _seed_submission(
        db, card_id="eu-ai-act", status="evaluating", submitted_at=now - timedelta(hours=1)
    )
    await db.commit()

    repaired = await repo.repair_stale_evaluating_rows(db)
    assert repaired == 1, f"only the stranded row should match; got {repaired}"

    cur = await db.execute("SELECT id, status FROM agent_submissions ORDER BY id")
    rows = {r[0]: r[1] for r in await cur.fetchall()}
    assert rows[stranded_id] == "ranked", "row with prior current_score must be restored to ranked"
    assert rows[queued_id] == "queued", "queued rows must not be touched"
    assert rows[fresh_eval_id] == "evaluating", (
        "evaluating rows without a prior score (genuine first-eval) must be left alone"
    )


@pytest.mark.asyncio
async def test_repair_is_idempotent(db, tmp_path: Path):
    """Running the repair twice in a row must match zero rows the
    second time — confirms the operation is safe to call on every
    startup as a perpetual safety net."""
    repo = _load_repository()
    await _seed_card_def(db, card_id="eu-ai-act", cadence_hours=24)
    now = datetime.now(UTC)

    sid = await _seed_submission(
        db, card_id="eu-ai-act", status="evaluating", submitted_at=now - timedelta(days=1)
    )
    await db.execute(
        "UPDATE agent_submissions SET current_score=0.5, current_rank=1 WHERE id=?",
        (sid,),
    )
    await db.commit()

    assert await repo.repair_stale_evaluating_rows(db) == 1
    assert await repo.repair_stale_evaluating_rows(db) == 0
