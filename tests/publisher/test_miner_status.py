"""Tests for GET /v1/miners/{hotkey}/submissions — miner-facing status feed.

Covers:
- Scored (ranked) submission surfaces weighted_score and status.
- Rejected submission surfaces rejection_reason.
- Unknown hotkey returns count=0 with empty items (not an error).
- limit query param is respected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from cathedral.publisher.app import build_app
from cathedral.publisher import repository as repo


_HOTKEY = "5MinerStatusTest000000000000000000000000000000000"
_HOTKEY_UNKNOWN = "5UnknownHotkey000000000000000000000000000000000"
_FAMILY = "synthetic_boolean_v1"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    app = build_app(database_path=str(tmp_path / "publisher.db"))
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers to seed data directly into the DB via the app's live connection.
# build_app wires the DB into app.state.ctx.db — reach it through the app.
# ---------------------------------------------------------------------------


async def _seed_submission(
    app_state,
    *,
    sub_id: str,
    hotkey: str,
    status: str,
    rejection_reason: str | None = None,
) -> None:
    conn = app_state.ctx.db
    # bundle_hash must be unique per (miner_hotkey, card_id, bundle_hash).
    # Derive it from sub_id so each seeded row has a distinct hash.
    import hashlib
    bundle_hash = hashlib.sha256(sub_id.encode()).hexdigest()
    await repo.insert_agent_submission(
        conn,
        id=sub_id,
        miner_hotkey=hotkey,
        card_id=_FAMILY,
        bundle_blob_key=f"bundles/{sub_id}.zip",
        bundle_hash=bundle_hash,
        bundle_size_bytes=512,
        encryption_key_id="kek-test",
        bundle_signature="b64:stub",
        display_name=f"miner-{sub_id}",
        bio=None,
        logo_url=None,
        soul_md_preview=None,
        metadata_fingerprint=f"fp-{sub_id}",
        similarity_check_passed=True,
        rejection_reason=rejection_reason,
        status=status,
        submitted_at=datetime(2026, 5, 30, 10, 0, 0, tzinfo=UTC),
        submitted_at_iso="2026-05-30T10:00:00.000Z",
        first_mover_at=None,
        attestation_mode="ssh-probe",
        ssh_host="203.0.113.1",
        ssh_port=22,
        ssh_user="cathedral",
    )


async def _seed_eval_run(
    app_state,
    *,
    run_id: str,
    sub_id: str,
    weighted_score: float,
    rejection_reason: str | None = None,
    challenge_id: str = "sat-t1-test-001",
    tier: int = 1,
    solve_rank: int | None = None,
) -> None:
    conn = app_state.ctx.db
    task_json: dict = {
        "challenge_id": challenge_id,
        "difficulty_tier": tier,
    }
    if solve_rank is not None:
        task_json["solve_rank"] = solve_rank

    output_card_json: dict = {}
    if rejection_reason is not None:
        output_card_json["rejection_reason"] = rejection_reason

    await repo.insert_eval_run(
        conn,
        id=run_id,
        submission_id=sub_id,
        epoch=1,
        round_index=0,
        polaris_agent_id="agent-stub",
        polaris_run_id="polaris-stub",
        task_json=task_json,
        output_card_json=output_card_json,
        output_card_hash="ab" * 32,
        score_parts={"binary_correct": weighted_score},
        weighted_score=weighted_score,
        ran_at=datetime(2026, 5, 30, 11, 0, 0, tzinfo=UTC),
        ran_at_iso="2026-05-30T11:00:00.000Z",
        duration_ms=500,
        errors=None,
        cathedral_signature="stub-sig",
        eval_output_schema_version=5,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_hotkey_returns_empty(client: TestClient) -> None:
    """An unknown hotkey must return count=0, not an error."""
    resp = client.get(f"/v1/miners/{_HOTKEY_UNKNOWN}/submissions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hotkey"] == _HOTKEY_UNKNOWN
    assert body["count"] == 0
    assert body["items"] == []


def test_unknown_hotkey_via_canonical_prefix(client: TestClient) -> None:
    """Same check via /api/cathedral prefix (dual-mount sanity)."""
    resp = client.get(f"/api/cathedral/v1/miners/{_HOTKEY_UNKNOWN}/submissions")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_ranked_submission_surfaces_score(client: TestClient) -> None:
    """A scored (ranked) submission must carry weighted_score and status='ranked'."""
    app = client.app
    import asyncio

    sub_id = "sub-ranked-001"

    asyncio.get_event_loop().run_until_complete(
        _seed_submission(app.state, sub_id=sub_id, hotkey=_HOTKEY, status="ranked")
    )
    asyncio.get_event_loop().run_until_complete(
        _seed_eval_run(
            app.state,
            run_id="run-ranked-001",
            sub_id=sub_id,
            weighted_score=0.85,
            challenge_id="sat-t1-test-001",
            tier=1,
            solve_rank=2,
        )
    )

    resp = client.get(f"/v1/miners/{_HOTKEY}/submissions")
    assert resp.status_code == 200
    body = resp.json()
    assert body["hotkey"] == _HOTKEY
    assert body["count"] >= 1

    item = next(i for i in body["items"] if i["submission_id"] == sub_id)
    assert item["status"] == "ranked"
    assert item["weighted_score"] == pytest.approx(0.85)
    assert item["solve_rank"] == 2
    assert item["tier"] == 1
    assert item["challenge_id"] == "sat-t1-test-001"
    assert item["rejection_reason"] is None
    assert item["ran_at"] is not None
    assert item["created_at"] is not None


def test_rejected_submission_surfaces_reason(client: TestClient) -> None:
    """A rejected submission must carry rejection_reason."""
    app = client.app
    import asyncio

    sub_id = "sub-rejected-001"
    reason = "invalid dimacs solution"

    asyncio.get_event_loop().run_until_complete(
        _seed_submission(
            app.state,
            sub_id=sub_id,
            hotkey=_HOTKEY,
            status="rejected",
            rejection_reason=reason,
        )
    )

    resp = client.get(f"/v1/miners/{_HOTKEY}/submissions")
    assert resp.status_code == 200
    body = resp.json()
    item = next(i for i in body["items"] if i["submission_id"] == sub_id)
    assert item["status"] == "rejected"
    assert item["rejection_reason"] == reason
    assert item["weighted_score"] is None


def test_both_submissions_returned_together(client: TestClient) -> None:
    """Seeding two different submissions for one hotkey must show both."""
    app = client.app
    import asyncio

    sub_ranked = "sub-both-ranked"
    sub_rejected = "sub-both-rejected"

    asyncio.get_event_loop().run_until_complete(
        _seed_submission(app.state, sub_id=sub_ranked, hotkey=_HOTKEY, status="ranked")
    )
    asyncio.get_event_loop().run_until_complete(
        _seed_eval_run(
            app.state,
            run_id="run-both-ranked",
            sub_id=sub_ranked,
            weighted_score=0.9,
        )
    )
    asyncio.get_event_loop().run_until_complete(
        _seed_submission(
            app.state,
            sub_id=sub_rejected,
            hotkey=_HOTKEY,
            status="rejected",
            rejection_reason="bad answer",
        )
    )

    resp = client.get(f"/v1/miners/{_HOTKEY}/submissions")
    assert resp.status_code == 200
    body = resp.json()

    ids = {i["submission_id"] for i in body["items"]}
    assert sub_ranked in ids
    assert sub_rejected in ids

    ranked_item = next(i for i in body["items"] if i["submission_id"] == sub_ranked)
    rejected_item = next(i for i in body["items"] if i["submission_id"] == sub_rejected)

    assert ranked_item["weighted_score"] == pytest.approx(0.9)
    assert rejected_item["rejection_reason"] == "bad answer"
    assert rejected_item["weighted_score"] is None


def test_limit_param_is_respected(client: TestClient) -> None:
    """limit=1 must return at most 1 item."""
    app = client.app
    import asyncio

    for i in range(3):
        asyncio.get_event_loop().run_until_complete(
            _seed_submission(
                app.state,
                sub_id=f"sub-limit-{i:03d}",
                hotkey=_HOTKEY,
                status="pending_solution",
            )
        )

    resp = client.get(f"/v1/miners/{_HOTKEY}/submissions?limit=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert len(body["items"]) == 1


def test_limit_out_of_range_returns_error(client: TestClient) -> None:
    """limit=0 is below ge=1; the app validation handler returns 400 (CONTRACTS §9 lock #3)."""
    resp = client.get(f"/v1/miners/{_HOTKEY}/submissions?limit=0")
    # The app's _validation_handler normalises FastAPI 422s to 400 per contract.
    assert resp.status_code == 400
    assert "detail" in resp.json()
