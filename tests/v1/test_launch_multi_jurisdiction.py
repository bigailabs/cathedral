"""Regression tests pinning the v1 launch surface to the multi-jurisdiction set.

The v1 launch started with a single card (`eu-ai-act`) after the
v1.1.18 collapse. That card saturated: a "template farmer" cohort
produces minimum-viable cards that pass schema without producing
insight, and the single-card surface lets copyists fast-follow within
one refresh cycle. Multi-jurisdiction coverage (`us-ai-eo`,
`uk-ai-whitepaper`, `singapore-pdpc`, `japan-meti-mic`) stratifies the
population without redesigning the scorer.

These tests guard against:

- `CardRegistry.baseline()` losing any of the five jurisdictions
- `_V1_LAUNCH_CARDS` shrinking or losing the multi-jurisdiction set
- the `set_card_definition_status` archival helper regressing (still
  used by the `archive-cards` CLI for future deprecations)
- the active-card 404 gate on `/v1/cards/{card_id}/*` surfaces
  regressing

If any of the launch-set assertions fail, do not silently "fix the
test"; check whether the launch surface really changed and update the
issue tracker first.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cathedral.cards.registry import CardRegistry
from cathedral.publisher import repository
from cathedral.publisher.app import _V1_DEPRECATED_CARD_IDS, _V1_LAUNCH_CARDS
from cathedral.validator.db import connect as connect_db

LAUNCH_CARD_IDS: tuple[str, ...] = (
    "eu-ai-act",
    "us-ai-eo",
    "uk-ai-whitepaper",
    "singapore-pdpc",
    "japan-meti-mic",
)


def test_registry_baseline_covers_all_launch_jurisdictions() -> None:
    baseline = CardRegistry.baseline()
    ids = tuple(e.card_id for e in baseline.entries)
    assert set(ids) == set(LAUNCH_CARD_IDS), (
        f"v1 launch ships the five-jurisdiction set; baseline registry "
        f"must contain exactly those entries, got {ids}"
    )
    # Every entry must declare at least one required source class so the
    # source_quality dimension has something to credit.
    for entry in baseline.entries:
        assert entry.required_source_classes, (
            f"{entry.card_id} has empty required_source_classes; the "
            f"source_quality coverage bonus collapses to 0"
        )
        assert entry.refresh_cadence_hours > 0


def test_v1_launch_cards_covers_all_launch_jurisdictions() -> None:
    ids = tuple(c["id"] for c in _V1_LAUNCH_CARDS)
    assert set(ids) == set(LAUNCH_CARD_IDS), (
        f"_V1_LAUNCH_CARDS drives seed-cards on container start. v1 "
        f"launch ships the five-jurisdiction set; got {ids}"
    )
    # Every launch row must carry the four fields seed-cards reads.
    for card in _V1_LAUNCH_CARDS:
        assert {"id", "display_name", "jurisdiction", "topic"} <= card.keys()


def test_deprecated_card_ids_is_empty_on_multi_jurisdiction_launch() -> None:
    """The archival list is empty in the reopened launch. The tuple
    stays so future deprecations can drop one line back into it without
    re-introducing the helper."""
    assert _V1_DEPRECATED_CARD_IDS == (), (
        f"_V1_DEPRECATED_CARD_IDS must be empty while all launch cards "
        f"are active; got {_V1_DEPRECATED_CARD_IDS}"
    )


def test_archive_helper_marks_row_archived_idempotent(tmp_path: Path) -> None:
    """`set_card_definition_status` still works for future deprecations.

    Uses a non-launch card_id so the test does not pretend any of the
    launch cards are archived.
    """

    db_path = tmp_path / "publisher.db"
    card_id = "test-deprecated-track"

    async def _run() -> None:
        conn = await connect_db(str(db_path))
        try:
            await repository.insert_card_definition(
                conn,
                id=card_id,
                display_name="Test deprecated track",
                jurisdiction="other",
                topic="deprecated",
                description="x",
                eval_spec_md="x",
                source_pool=[],
                task_templates=[],
                scoring_rubric={},
                refresh_cadence_hours=24,
                status="active",
            )
            await conn.commit()

            updated = await repository.set_card_definition_status(
                conn, card_id=card_id, status="archived"
            )
            await conn.commit()
            assert updated is True

            row = await repository.get_card_definition(conn, card_id)
            assert row is not None
            assert row["status"] == "archived"

            # Second archive is a no-op (already archived).
            updated2 = await repository.set_card_definition_status(
                conn, card_id=card_id, status="archived"
            )
            await conn.commit()
            assert updated2 is True

            # Missing card: returns False, no insert.
            updated3 = await repository.set_card_definition_status(
                conn, card_id="never-seeded", status="archived"
            )
            await conn.commit()
            assert updated3 is False
            assert await repository.get_card_definition(conn, "never-seeded") is None
        finally:
            await conn.close()

    asyncio.run(_run())


def test_archived_card_status_routes_through_submit_check(tmp_path: Path) -> None:
    """The submit pipeline's card-status gate (``card_def['status'] !=
    'active'``) is the production trust posture for archived cards.

    Asserts the exact contract the gate depends on: archived rows still
    return from ``get_card_definition`` (so the gate can see them) but
    with ``status='archived'``, which triggers the HTTP 404 raise at
    ``publisher/submit.py``.
    """

    db_path = tmp_path / "publisher.db"

    async def _run() -> None:
        conn = await connect_db(str(db_path))
        try:
            await repository.insert_card_definition(
                conn,
                id="test-deprecated-track",
                display_name="Test deprecated track",
                jurisdiction="other",
                topic="deprecated",
                description="x",
                eval_spec_md="x",
                source_pool=[],
                task_templates=[],
                scoring_rubric={},
                refresh_cadence_hours=24,
                status="archived",
            )
            await conn.commit()

            row = await repository.get_card_definition(conn, "test-deprecated-track")
            assert row is not None, (
                "archived rows must remain readable so the submit gate "
                "can see them and return 404 (not silently 'card not found')"
            )
            assert row["status"] != "active", (
                f"archived card must not have status=active; got "
                f"{row['status']!r}. The submit gate compares != 'active'."
            )
        finally:
            await conn.close()

    asyncio.run(_run())


# --------------------------------------------------------------------------
# HTTP-level archived-card 404 behavior
# --------------------------------------------------------------------------


async def _seed_archived_test_card_via_ctx(ctx: Any) -> None:
    """Seed an archived non-launch row using the publisher app's own
    aiosqlite connection (`ctx.db`), so we hit the same DB the live
    endpoint reads from."""
    await repository.insert_card_definition(
        ctx.db,
        id="test-deprecated-track",
        display_name="Test deprecated track",
        jurisdiction="other",
        topic="deprecated",
        description="Test-only deprecated card.",
        eval_spec_md="deprecated",
        source_pool=[],
        task_templates=[],
        scoring_rubric={},
        refresh_cadence_hours=24,
        status="archived",
    )
    await ctx.db.commit()


def test_eval_spec_endpoint_returns_404_for_archived_card(publisher_client) -> None:
    """``GET /v1/cards/{id}/eval-spec`` must return 404 for archived
    cards, mirroring the submit gate at ``publisher/submit.py``.

    Without this, archived cards keep advertising their eval-spec
    content via the public endpoint even though new submits return 404,
    which would lead miners to build against cards they cannot actually
    submit to.
    """
    ctx = publisher_client.app.state.ctx
    asyncio.run(_seed_archived_test_card_via_ctx(ctx))

    resp = publisher_client.get("/v1/cards/test-deprecated-track/eval-spec")
    assert resp.status_code == 404, (
        f"archived card must 404 from eval-spec, got {resp.status_code}: {resp.text}"
    )
    assert "card not active" in resp.text or "card not found" in resp.text

    # Sanity check: an active launch card still returns 200 from the
    # same endpoint.
    resp_ok = publisher_client.get("/v1/cards/eu-ai-act/eval-spec")
    assert resp_ok.status_code == 200, (
        f"active eu-ai-act must still serve eval-spec, got {resp_ok.status_code}: {resp_ok.text}"
    )


def test_eval_spec_endpoint_returns_404_for_unknown_card(publisher_client) -> None:
    """Sanity guard: never-seeded card_ids still 404 (the archived-card
    gate is additive, not a regression of the existing
    'card not found' path)."""
    resp = publisher_client.get("/v1/cards/never-seeded-ever/eval-spec")
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------
# All five launch cards must be active across every public surface
# --------------------------------------------------------------------------


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_launch_card_eval_spec_endpoint_returns_200(publisher_client, card_id: str) -> None:
    """Every launch card_id must serve ``/v1/cards/{id}/eval-spec`` 200.

    Catches accidental drift between `_V1_LAUNCH_CARDS` (the seed list)
    and what the publisher actually exposes on disk after startup.
    """
    resp = publisher_client.get(f"/v1/cards/{card_id}/eval-spec")
    assert resp.status_code == 200, (
        f"launch card {card_id} must serve eval-spec, got {resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# Archived cards must 404 across every /v1/cards/{card_id}/* surface
# --------------------------------------------------------------------------


# Every /v1/cards/{card_id}/* route that surfaces card content. If a new
# route lands and forgets to call get_active_card_definition_or_404, the
# parametrised test below will fail on it once it's added here.
_PUBLIC_CARD_SUBPATHS: list[str] = [
    "",  # GET /v1/cards/{card_id} (summary)
    "/eval-spec",
    "/history",
    "/feed",
    "/attempts",
    "/discovery",
    "/discovery/count",
]


@pytest.mark.parametrize("subpath", _PUBLIC_CARD_SUBPATHS)
def test_archived_card_404s_across_all_public_subpaths(publisher_client, subpath: str) -> None:
    """Every /v1/cards/{card_id}/* surface must 404 on archived cards.

    Without the shared `get_active_card_definition_or_404` helper, each
    route was independently checking only existence (not status), so an
    archived row would still serve summary/history/feed/discovery
    content even though submit and eval-spec correctly rejected it.
    """
    ctx = publisher_client.app.state.ctx
    asyncio.run(_seed_archived_test_card_via_ctx(ctx))

    resp = publisher_client.get(f"/v1/cards/test-deprecated-track{subpath}")
    assert resp.status_code == 404, (
        f"GET /v1/cards/test-deprecated-track{subpath} must return 404 "
        f"for archived card, got {resp.status_code}: {resp.text}"
    )


def test_leaderboard_404s_for_archived_card(publisher_client) -> None:
    """`GET /v1/leaderboard?card=<archived>` must 404 too: archived
    cards should not appear anywhere a miner or the site might look."""
    ctx = publisher_client.app.state.ctx
    asyncio.run(_seed_archived_test_card_via_ctx(ctx))

    resp = publisher_client.get("/v1/leaderboard", params={"card": "test-deprecated-track"})
    assert resp.status_code == 404, resp.text
