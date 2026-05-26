"""V1 launch types — agent submissions, eval runs, leaderboard, merkle.

Lives in its own module so the Polaris-facing wire types in
`cathedral.types` remain untouched (they cross a code-frozen boundary
with the polariscomputer repo). Everything here is internal to
cathedral + the cathedral.computer frontend mirror.

Contract reference: `cathedral-redesign/CONTRACTS.md` Section 1.9-1.13.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cathedral.types import Source

# --------------------------------------------------------------------------
# Submissions
# --------------------------------------------------------------------------


class AgentSubmissionStatus(str, Enum):
    PENDING_CHECK = "pending_check"
    QUEUED = "queued"
    EVALUATING = "evaluating"
    RANKED = "ranked"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    DISCOVERY = "discovery"
    PENDING_SOLUTION = "pending_solution"
    VALID_ATTESTATION_PENDING = "valid_attestation_pending"
    ATTEST_FAILED = "attest_failed"


class AgentSubmission(BaseModel):
    """Server-side row produced by `POST /v1/agents/submit` (Section 1.9)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    miner_hotkey: str
    card_id: str
    bundle_hash: str
    bundle_size_bytes: int
    bundle_blob_key: str
    encryption_key_id: str
    bundle_signature: str
    display_name: str = Field(min_length=1, max_length=64)
    bio: str | None = None
    logo_url: str | None = None
    soul_md_preview: str | None = None
    metadata_fingerprint: str
    similarity_check_passed: bool
    rejection_reason: str | None = None
    status: AgentSubmissionStatus
    current_score: float | None = None
    current_rank: int | None = None
    submitted_at: datetime
    first_mover_at: datetime | None = None


# --------------------------------------------------------------------------
# Eval runs and tasks
# --------------------------------------------------------------------------


class EvalTask(BaseModel):
    """Deterministic per (card_id, epoch, round_index). Section 1.11."""

    model_config = ConfigDict(extra="forbid")

    card_id: str
    epoch: int
    round_index: int
    prompt: str
    sources: list[Source] = Field(default_factory=list)
    deadline_minutes: int = 25


class EvalRun(BaseModel):
    """Section 1.10.

    `polaris_verified` is True when the eval ran on a Polaris-managed
    runtime and the manifest was fetched + verified successfully. False
    for BYO-compute miners and for failed Polaris runs. The verified
    multiplier (CONTRACTS.md §7.3) is reflected in `weighted_score`;
    this flag lets downstream consumers display the verification status
    without re-deriving it from the score.
    """

    model_config = ConfigDict(extra="forbid")

    id: UUID
    submission_id: UUID
    epoch: int
    round_index: int
    polaris_agent_id: str
    polaris_run_id: str
    task_json: dict[str, Any]
    output_card_json: dict[str, Any]
    output_card_hash: str
    score_parts: dict[str, Any]
    weighted_score: float
    ran_at: datetime
    duration_ms: int
    errors: list[str] | None = None
    cathedral_signature: str
    polaris_verified: bool = False


# --------------------------------------------------------------------------
# Leaderboard
# --------------------------------------------------------------------------


class LeaderboardEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: UUID
    display_name: str
    logo_url: str | None
    miner_hotkey: str
    card_id: str
    current_score: float
    current_rank: int
    last_eval_at: datetime


# --------------------------------------------------------------------------
# Merkle anchor
# --------------------------------------------------------------------------


class MerkleAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    epoch: int
    merkle_root: str
    eval_count: int
    computed_at: datetime
    on_chain_block: int | None = None
    on_chain_extrinsic_index: int | None = None
    leaf_hashes: list[str] | None = None


# --------------------------------------------------------------------------
# Card definitions
# --------------------------------------------------------------------------


class CardSourcePoolEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    url: str
    class_: str = Field(alias="class")
    name: str


class ScoringRubric(BaseModel):
    """Card definition scoring rubric per Section 2.6.

    Default values match `ScoreParts.weighted` in `cathedral.types`.
    """

    model_config = ConfigDict(extra="forbid")

    source_quality_weight: float = 0.30
    maintenance_weight: float = 0.20
    freshness_weight: float = 0.15
    specificity_weight: float = 0.15
    usefulness_weight: float = 0.10
    clarity_weight: float = 0.10
    required_source_classes: list[str] = Field(default_factory=list)
    min_summary_chars: int = 40
    max_summary_chars: int = 800
    min_citations: int = 1


class CardDefinition(BaseModel):
    """A row in the `card_definitions` table (Section 3.1)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    jurisdiction: str
    topic: str
    description: str
    eval_spec_md: str
    source_pool: list[dict[str, Any]]
    task_templates: list[str]
    scoring_rubric: dict[str, Any]
    refresh_cadence_hours: int = 24
    status: str = "active"
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


_SIGNATURE_EXCLUDED_KEYS = frozenset(
    {
        "signature",
        "cathedral_signature",
        # `merkle_epoch` is set AFTER signing by the weekly merkle close
        # job — it must not be part of the signed bytes or every record
        # would invalidate the moment its epoch is anchored.
        "merkle_epoch",
    }
)


def canonical_json(payload: dict[str, Any]) -> bytes:
    """Match `cathedral.types.canonical_json_for_signing` semantics.

    Drops `signature`, `cathedral_signature`, and `merkle_epoch` keys
    (CRIT-7: post-signing fields are excluded from the signed bytes).
    Sorts keys, no whitespace, UTF-8 encoded. Used for both submission
    and eval-run signing canonicalization (Section 4).
    """
    body = {k: v for k, v in payload.items() if k not in _SIGNATURE_EXCLUDED_KEYS}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
