"""Wire types shared by validator, miner, and tests.

Every record that crosses a process boundary is defined here. Downstream
modules import these and never invent parallel definitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------


class Hotkey(str):
    """Bittensor hotkey ss58. Wrapped str for type clarity."""

    __slots__ = ()


# --------------------------------------------------------------------------
# Claim — issue #2 wire format
# --------------------------------------------------------------------------


class ClaimVersion(str, Enum):
    V1 = "v1"


class PolarisAgentClaim(BaseModel):
    """`cathedral.polaris_agent_claim.v1`.

    Submitted by miners to `POST /v1/claim`. The validator queues the claim,
    optionally fetches Polaris evidence by identifier (manifest, runs, usage),
    verifies signatures, and scores the card carried in `card_payload`.

    `polaris_agent_id` is OPTIONAL. When present, cathedral fetches and
    verifies the Polaris manifest, and the score gets a verified-runtime
    multiplier (CONTRACTS.md §7.3). When absent, the agent is BYO-compute
    and only the hotkey signature is verified — the card still scores,
    just without the multiplier. This unblocks miners who run Hermes (or
    any compatible runtime) on infrastructure they own.

    Cards live on Cathedral, not Polaris. Miners submit the card body inline
    so Cathedral never has to hop back to Polaris for artifact bytes — and
    so cards remain available even if Polaris is unreachable. The
    `polaris_artifact_ids` field stays for backward compatibility with
    earlier-spec miners; if `card_payload` is null, the worker falls back
    to decoding from the first artifact whose `report_hash` parses.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(default="cathedral.polaris_agent_claim.v1")
    version: ClaimVersion = ClaimVersion.V1
    miner_hotkey: str
    owner_wallet: str
    work_unit: str
    polaris_agent_id: str | None = None
    polaris_deployment_id: str | None = None
    polaris_run_ids: list[str] = Field(default_factory=list)
    polaris_artifact_ids: list[str] = Field(default_factory=list)
    # Card payload — Cathedral-side storage of the work product. Miners
    # produce this from Hermes (or any compatible runtime) and submit it
    # inline. The id, worker_owner_hotkey, and polaris_agent_id fields
    # may be omitted from the payload — the worker fills them from the
    # surrounding claim context before validating against Card.
    card_payload: dict | None = None
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("miner_hotkey", "owner_wallet", "work_unit")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must be non-empty")
        return v

    @field_validator("polaris_agent_id")
    @classmethod
    def _polaris_agent_id_non_empty_when_present(cls, v: str | None) -> str | None:
        # Optional field: None means BYO-compute. When supplied, must be
        # a non-empty identifier (empty-string is treated as a coding bug,
        # not a valid "no Polaris" signal — use None for that).
        if v is not None and not v:
            raise ValueError("polaris_agent_id must be non-empty when present")
        return v


# --------------------------------------------------------------------------
# Polaris records — what the validator pulls and verifies
# --------------------------------------------------------------------------


class PolarisManifest(BaseModel):
    """Per CONTRACTS.md L3, runtime_image and runtime_mode allow Cathedral to
    verify that a miner's claimed agent is actually running the canonical
    Hermes image in card-mode. Both default None for backward compatibility
    with manifests signed before these fields existed; canonicalization on
    both sides uses exclude_none=True so the absent fields don't affect the
    signature payload."""

    model_config = ConfigDict(extra="allow")

    polaris_agent_id: str
    owner_wallet: str
    created_at: datetime
    schema_: str = Field(alias="schema")
    runtime_image: str | None = None
    runtime_mode: str | None = None
    signature: str


class PolarisRunRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    run_id: str
    polaris_agent_id: str
    started_at: datetime
    ended_at: datetime | None = None
    outcome: str
    signature: str


class PolarisArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    artifact_id: str
    polaris_agent_id: str
    run_id: str | None = None
    content_url: str
    content_hash: str
    report_hash: str | None = None
    signature: str


class ConsumerClass(str, Enum):
    EXTERNAL = "external"
    CREATOR = "creator"
    PLATFORM = "platform"
    TEST = "test"
    SELF_LOOP = "self_loop"

    def counts_for_rewards(self) -> bool:
        return self is ConsumerClass.EXTERNAL


class PolarisUsageRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    usage_id: str
    polaris_agent_id: str
    consumer: ConsumerClass
    consumer_wallet: str | None = None
    used_at: datetime
    flagged: bool = False
    refunded: bool = False
    signature: str


class EvidenceBundle(BaseModel):
    """The verified payload Cathedral hands to the card scorer.

    `manifest` is None when the claim is BYO-compute (no `polaris_agent_id`
    on the claim). Downstream consumers MUST guard for this case: there is
    no owner_wallet to compare for self-loop detection and no runtime_image
    to verify, so those checks are skipped.
    """

    manifest: PolarisManifest | None = None
    runs: list[PolarisRunRecord] = Field(default_factory=list)
    artifacts: list[PolarisArtifactRecord] = Field(default_factory=list)
    usage: list[PolarisUsageRecord] = Field(default_factory=list)
    verified_at: datetime
    filtered_usage_count: int = 0


# --------------------------------------------------------------------------
# Cards — issue #3
# --------------------------------------------------------------------------


class Jurisdiction(str, Enum):
    """Per CONTRACTS.md L2. Five jurisdictions are live at launch: EU
    (eu-ai-act), US (us-ai-eo), UK (uk-ai-whitepaper), SG
    (singapore-pdpc), JP (japan-meti-mic). The remaining values stay
    available for follow-up launch tracks and so old DB rows and
    historical evidence remain decodable."""

    EU = "eu"
    US = "us"
    UK = "uk"
    CA = "ca"
    AU = "au"
    IN = "in"
    BR = "br"
    SG = "sg"
    JP = "jp"
    OTHER = "other"


class SourceClass(str, Enum):
    GOVERNMENT = "government"
    REGULATOR = "regulator"
    COURT = "court"
    PARLIAMENT = "parliament"
    LAW_TEXT = "law_text"
    OFFICIAL_JOURNAL = "official_journal"
    SECONDARY_ANALYSIS = "secondary_analysis"
    OTHER = "other"


OFFICIAL_SOURCE_CLASSES: frozenset[SourceClass] = frozenset(
    {
        SourceClass.GOVERNMENT,
        SourceClass.REGULATOR,
        SourceClass.COURT,
        SourceClass.PARLIAMENT,
        SourceClass.LAW_TEXT,
        SourceClass.OFFICIAL_JOURNAL,
    }
)


class Source(BaseModel):
    url: str
    class_: SourceClass = Field(alias="class")
    fetched_at: datetime
    status: int
    content_hash: str

    model_config = ConfigDict(populate_by_name=True)


class Card(BaseModel):
    id: str
    jurisdiction: Jurisdiction
    topic: str
    worker_owner_hotkey: str
    polaris_agent_id: str
    title: str
    summary: str
    what_changed: str
    why_it_matters: str
    action_notes: str
    risks: str
    citations: list[Source]
    confidence: float = Field(ge=0.0, le=1.0)
    no_legal_advice: bool
    last_refreshed_at: datetime
    refresh_cadence_hours: int = Field(gt=0)


class ScoreParts(BaseModel):
    """Six dimensions per issue #3. Each in [0, 1]."""

    source_quality: float = 0.0
    freshness: float = 0.0
    specificity: float = 0.0
    usefulness: float = 0.0
    clarity: float = 0.0
    maintenance: float = 0.0

    def weighted(self) -> float:
        """Default weighting: source quality and maintenance lead."""
        return (
            0.30 * self.source_quality
            + 0.20 * self.maintenance
            + 0.15 * self.freshness
            + 0.15 * self.specificity
            + 0.10 * self.usefulness
            + 0.10 * self.clarity
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def canonical_json_for_signing(record: dict[str, Any]) -> bytes:
    """Stable bytes used as the signature payload.

    Drops the `signature` key and serializes with sorted keys + no whitespace.
    Both Polaris (signer) and Cathedral (verifier) must agree on this exact
    canonicalization or all verification fails.

    Mirrors `polaris.services.cathedral_signing.canonical_json_for_signing`
    exactly: no exclude_none on raw dicts. The exclude_none behavior happens
    one layer up, in the Pydantic-model path (verify_manifest etc. call
    model_dump(by_alias=True, mode="json", exclude_none=True)). That asymmetry
    matches Polaris: Pydantic models drop None fields before signing/verifying;
    raw dicts pass through verbatim.
    """
    import json

    payload = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
