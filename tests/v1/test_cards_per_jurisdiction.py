"""Per-card-id preflight + scorer smoke tests for the multi-jurisdiction launch.

For each of the five launch cards (`eu-ai-act`, `us-ai-eo`,
`uk-ai-whitepaper`, `singapore-pdpc`, `japan-meti-mic`) verify:

- the card_id is recognised by ``CardRegistry.baseline().lookup``
- the ``required_source_classes`` tuple is non-empty
- a known-good payload (one citation matching the card's required
  source class) passes preflight
- a known-bad payload (empty citations) fails preflight with
  ``NoCitationsError``
- a known-bad payload (no_legal_advice=False) fails preflight with
  ``MissingNoLegalAdviceMarkerError``

The tests are parametrised across card_ids; one parametrise per
assertion rather than 5x duplicated bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cathedral.cards import preflight
from cathedral.cards.preflight import (
    MissingNoLegalAdviceMarkerError,
    NoCitationsError,
)
from cathedral.cards.registry import CardRegistry, RegistryEntry
from cathedral.cards.score import score_card
from cathedral.types import Card, Jurisdiction, Source

LAUNCH_CARD_IDS: tuple[str, ...] = (
    "eu-ai-act",
    "us-ai-eo",
    "uk-ai-whitepaper",
    "singapore-pdpc",
    "japan-meti-mic",
)


def _registry_entry(card_id: str) -> RegistryEntry:
    entry = CardRegistry.baseline().lookup(card_id)
    assert entry is not None, f"{card_id} missing from CardRegistry.baseline()"
    return entry


def _good_card(card_id: str) -> Card:
    """Build a minimal preflight-passing card for the given card_id.

    Uses the card's first ``required_source_class`` for the single
    citation so the source_quality scorer credits both the official
    base ratio and the coverage bonus for this required class.
    """
    entry = _registry_entry(card_id)
    juris = entry.jurisdiction
    primary_class = entry.required_source_classes[0]
    return Card(
        id=card_id,
        jurisdiction=juris,
        topic=entry.topic,
        worker_owner_hotkey="5HotKey",
        polaris_agent_id="agt_1",
        title=f"{card_id} update",
        summary=(
            "A summary of the most material developments in this "
            "jurisdiction over the last refresh window."
        ),
        what_changed="Recent updates tightened expectations. " * 5,
        why_it_matters="Affects compliance across the regulated population. " * 4,
        action_notes="Review your obligations this week.",
        risks="Material penalties for non-compliance.",
        citations=[
            Source(
                url=f"https://primary.example/{card_id}",
                **{"class": primary_class},
                fetched_at=datetime.now(UTC),
                status=200,
                content_hash="deadbeef",
            )
        ],
        confidence=0.8,
        no_legal_advice=True,
        last_refreshed_at=datetime.now(UTC),
        refresh_cadence_hours=entry.refresh_cadence_hours,
    )


# --------------------------------------------------------------------------
# Registry shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_card_id_is_in_baseline_registry(card_id: str) -> None:
    entry = CardRegistry.baseline().lookup(card_id)
    assert entry is not None
    assert entry.card_id == card_id


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_required_source_classes_non_empty(card_id: str) -> None:
    entry = _registry_entry(card_id)
    assert entry.required_source_classes, (
        f"{card_id} has empty required_source_classes; the source_quality "
        f"coverage bonus collapses to 0"
    )
    assert entry.refresh_cadence_hours > 0


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_jurisdiction_enum_value_resolves(card_id: str) -> None:
    entry = _registry_entry(card_id)
    # Jurisdiction must be a member of the enum (Pydantic-side cast
    # will fail in production if a card_id maps to a non-enum string).
    assert isinstance(entry.jurisdiction, Jurisdiction)


# --------------------------------------------------------------------------
# Preflight pass / fail
# --------------------------------------------------------------------------


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_good_card_passes_preflight(card_id: str) -> None:
    preflight(_good_card(card_id))


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_empty_citations_fails_preflight(card_id: str) -> None:
    bad = _good_card(card_id).model_copy(update={"citations": []})
    with pytest.raises(NoCitationsError):
        preflight(bad)


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_missing_no_legal_advice_fails_preflight(card_id: str) -> None:
    bad = _good_card(card_id).model_copy(update={"no_legal_advice": False})
    with pytest.raises(MissingNoLegalAdviceMarkerError):
        preflight(bad)


# --------------------------------------------------------------------------
# Scorer smoke
# --------------------------------------------------------------------------


@pytest.mark.parametrize("card_id", LAUNCH_CARD_IDS)
def test_good_card_scores_above_baseline(card_id: str) -> None:
    """A preflight-passing card with the required source class cited
    should score positively across all dimensions and reach a healthy
    weighted score. Exact thresholds are loose so that future scorer
    tweaks do not break this test; the point is the new card_ids enter
    the scorer cleanly, not to re-pin existing weights."""
    entry = _registry_entry(card_id)
    parts = score_card(_good_card(card_id), entry)
    assert parts.source_quality > 0.0
    assert parts.freshness > 0.0
    assert parts.weighted() > 0.4
