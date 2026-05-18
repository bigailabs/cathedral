"""Signed-payload versioning constants for the eval wire shape.

Lives in its own module so cross-branch tests can import the
``_SIGNED_KEYS_BY_VERSION`` map and ``_card_excerpt`` projection
without dragging the full publisher import chain
(scoring_pipeline → publisher.repository → publisher.app →
orchestrator → scoring_pipeline).

CROSS-BRANCH CONTRACT: the keysets in ``_SIGNED_KEYS_BY_VERSION``
MUST match the same map in ``src/cathedral/validator/pull_loop.py``
(see ``_SIGNED_KEYS_BY_VERSION`` on the validator-compat branch). The
validator dispatches verification by ``eval_output_schema_version``
on each wire item; if our publisher signs a different field set than
the validator verifies, signatures fail at runtime.

Coordinated under cathedralai/cathedral#75 PR 4 +
feature/v1-1-0-validator-compat. Any change to v1 OR v2 here MUST
land in lockstep with the validator branch.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------
# Signed-payload keysets per version
# --------------------------------------------------------------------------

_SIGNED_KEYS_BY_VERSION: dict[int, frozenset[str]] = {
    # v1 — the legacy CONTRACTS.md §1.10 + L8 shape.
    1: frozenset(
        {
            "id",
            "agent_id",
            "agent_display_name",
            "card_id",
            "output_card",
            "output_card_hash",
            "weighted_score",
            "polaris_verified",
            "ran_at",
        }
    ),
    # v2 — cathedralai/cathedral#75 PR 4. Drops output_card,
    # output_card_hash, polaris_verified. Adds eval_card_excerpt and
    # eval_artifact_manifest_hash. Same answer locked on issue #75.
    #
    # `eval_output_schema_version` is NOT in this set: it's a routing
    # hint validators read to pick the dispatcher entry, not part of
    # the signed bytes. Tampering with the version field routes
    # verification to the wrong keyset and the byte-level mismatch on
    # the underlying fields fails the signature.
    2: frozenset(
        {
            "id",
            "agent_id",
            "agent_display_name",
            "card_id",
            "eval_card_excerpt",
            "eval_artifact_manifest_hash",
            "weighted_score",
            "ran_at",
        }
    ),
    # v3, v3.0 bug_isolation_v1 benchmark lane. Cathedral prompts the
    # miner via Hermes, miner returns a structured isolation claim,
    # Cathedral scores statically against a hidden oracle on Railway
    # and signs the result. No card_id in the signed bytes: v3 rows
    # are not regulatory cards and do not route through the v1 card
    # registry. `challenge_id` is signed (so validators can verify
    # which corpus row was scored) but is hashed in public read
    # surfaces to slow Discord-style answer-sharing across miners.
    3: frozenset(
        {
            "id",
            "agent_id",
            "agent_display_name",
            "miner_hotkey",
            "task_type",
            "challenge_id",
            "challenge_id_public",
            "epoch_salt",
            "weighted_score",
            "score_parts",
            "claim",
            "ran_at",
        }
    ),
    # v5, generic Task Family lane rows. Used first by
    # synthetic_boolean_v1. The signed row intentionally omits raw
    # problem payloads and submitted answers. Validators authenticate
    # score and hashes; publisher-side private storage owns reveal and
    # audit material.
    5: frozenset(
        {
            "id",
            "agent_id",
            "agent_display_name",
            "miner_hotkey",
            "task_type",
            "task_id_public",
            "epoch_salt",
            "difficulty_tier",
            "weighted_score",
            "score_parts",
            "answer_hash",
            "verifier_details_hash",
            "rejection_reason",
            "ran_at",
        }
    ),
}


# --------------------------------------------------------------------------
# v2 card-excerpt projection
# --------------------------------------------------------------------------

# Field set kept in the v2 excerpt. Anything the site renders on
# /jobs/[id]/feed (cathedral-site/src/pages/jobs/[id]/feed.astro) or
# treats as a failure marker. Long-form fields (what_changed,
# why_it_matters, action_notes, risks, citations) move to the
# artifact bundle.
_EXCERPT_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "title",
        "summary",
        "jurisdiction",
        "topic",
        "confidence",
        "last_refreshed_at",
        "no_legal_advice",
        # Failure markers — cathedral-site/feed.astro renders these
        # specially. Must survive the excerpt projection so the v2
        # wire shape supports the same honest-failure UI.
        "_polaris_unreachable",
        "_ssh_hermes_failed",
        "failure_code",
    }
)


def card_excerpt(output_card_json: dict[str, Any]) -> dict[str, Any]:
    """Project the full Card down to the v2 wire excerpt.

    PR 4 splits the legacy ``output_card`` blob (which carries ALL
    Card fields including the long-form ``what_changed`` /
    ``why_it_matters`` / ``citations``) into:

    - ``eval_card_excerpt``: small subset rendered by the site and
      covered by the v2 signed payload (~1KB)
    - ``eval_artifact_manifest_hash``: anchors the full Hermes
      forensic bundle (state.db slice, request dumps, etc.) via a
      single hash in the signed bytes
    - ``eval_artifact_bundle_url``: unsigned envelope pointer to the
      bundle blob in Hippius

    The excerpt keeps every field the site renders on
    ``/jobs/[id]/feed`` and ``/jobs/[id]/index``. Citations + the
    long-form sections move to the bundle (the full output_card_json
    is preserved in the eval_runs row, so a v1.0.x validator pulling
    in the dual-publish window still gets the legacy output_card via
    the v1 projection).
    """
    return {k: output_card_json[k] for k in _EXCERPT_FIELDS if k in output_card_json}


__all__ = [
    "_SIGNED_KEYS_BY_VERSION",
    "card_excerpt",
]
