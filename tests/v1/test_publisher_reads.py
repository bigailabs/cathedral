"""GET /v1/agents/*, /v1/cards/*, /v1/leaderboard*, /v1/merkle/* — per CONTRACTS.md §2.

These verify response SHAPES against the TypeScript mirrors in §1.

For shape validation we build Pydantic models from the TypeScript mirrors
in CONTRACTS.md §1.9-§1.13. If the implementer's response shape diverges,
the model_validate raises and the test fails citing the contract section.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from tests.v1.conftest import (
    blake3_hex,
    make_valid_bundle,
    submit_multipart,
)

# --------------------------------------------------------------------------
# Pydantic mirrors of the TypeScript types in CONTRACTS.md §1
# --------------------------------------------------------------------------


class _ScoreHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    score: float


class _AgentProfile(BaseModel):
    """Mirrors CONTRACTS.md §1.9 `AgentProfile` (frontend mirror).

    `attestation_mode` was added with the discovery-surface split: the
    frontend branches the agent profile UI on it (verified vs discovery).
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    display_name: str
    bio: str | None
    logo_url: str | None
    miner_hotkey: str
    card_id: str
    bundle_hash: str
    bundle_size_bytes: int
    status: str  # one of AgentSubmissionStatus
    current_score: float | None
    current_rank: int | None
    submitted_at: str
    attestation_mode: str  # 'polaris' | 'tee' | 'unverified'
    recent_evals: list[dict[str, Any]] = Field(default_factory=list)
    score_history: list[_ScoreHistoryEntry] = Field(default_factory=list)


class _LeaderboardEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    display_name: str
    logo_url: str | None
    miner_hotkey: str
    card_id: str
    current_score: float
    current_rank: int
    last_eval_at: str


class _EvalOutput(BaseModel):
    """Mirrors §1.10 `EvalOutput` + locked decision L8.

    `output_card_hash` is REQUIRED in the public projection (L8): the
    frontend renders it as the visible trust-chain anchor and validators
    use it to verify the cathedral signature against the byte-exact card
    that was scored.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    agent_id: str
    agent_display_name: str
    card_id: str
    output_card: dict[str, Any]
    output_card_hash: str
    weighted_score: float
    ran_at: str
    cathedral_signature: str
    merkle_epoch: int | None


class _MerkleAnchor(BaseModel):
    """Mirrors §1.13 `MerkleAnchor`."""

    model_config = ConfigDict(extra="forbid")
    epoch: int
    merkle_root: str
    eval_count: int
    computed_at: str
    on_chain_block: int | None
    on_chain_extrinsic_index: int | None
    leaf_hashes: list[str] | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _seed_one_submission(client, keypair, card_id="eu-ai-act") -> dict[str, Any]:
    bundle = make_valid_bundle(soul_md=f"# Seed for {keypair.ss58_address}\n")
    resp = submit_multipart(
        client, keypair=keypair, card_id=card_id, bundle=bundle, display_name="Seed Agent"
    )
    if resp.status_code != 202:
        pytest.skip(
            f"submit not yet implemented (got {resp.status_code}: {resp.text}) — "
            "read tests need the publisher to accept submissions first"
        )
    body = resp.json()
    body["_bundle"] = bundle
    body["_bundle_hash"] = blake3_hex(bundle)
    return body


def _validate_iso_z(value: str, *, section: str) -> None:
    assert value.endswith("Z"), (
        f"{section}: timestamps must use ISO-8601 trailing 'Z' (§9 lock #6); got {value!r}"
    )


# --------------------------------------------------------------------------
# GET /v1/agents/{id}
# --------------------------------------------------------------------------


def test_get_agent_returns_profile_shape(publisher_client, alice_keypair):
    """CONTRACTS.md §2.2 — response is AgentProfile (§1.9)."""
    seeded = _seed_one_submission(publisher_client, alice_keypair)
    agent_id = seeded["id"]

    resp = publisher_client.get(f"/v1/agents/{agent_id}")
    assert resp.status_code == 200, f"§2.2: {resp.status_code} {resp.text}"
    profile = _AgentProfile.model_validate(resp.json())
    assert profile.id == agent_id
    assert profile.bundle_hash == seeded["_bundle_hash"], (
        "§1.9 + §4.4: bundle_hash on the profile must equal blake3 of plaintext zip"
    )
    assert profile.miner_hotkey == alice_keypair.ss58_address
    _validate_iso_z(profile.submitted_at, section="§1.9 AgentProfile.submitted_at")


def test_get_agent_404_for_unknown_id(publisher_client):
    """CONTRACTS.md §2.2 — `404 agent not found`."""
    # Valid UUID format that doesn't exist.
    fake = "00000000-0000-4000-8000-000000000000"
    resp = publisher_client.get(f"/v1/agents/{fake}")
    if resp.status_code == 404:
        body = resp.json()
        assert body == {"detail": "agent not found"}, (
            f"§2.2 detail must be exactly 'agent not found'; got {body}"
        )
    else:
        # Some implementations 422 on bad UUID format; only the 404 case is
        # the contract surface here.
        assert resp.status_code in {404, 422}, (
            f"§2.2: unknown agent must be 404 (or 422 on validation), got {resp.status_code}"
        )


def test_get_agent_does_not_leak_bundle_contents(publisher_client, alice_keypair):
    """CONTRACTS.md §2.2 + ARCHITECTURE_V1 §"What Cathedral is" — the
    AgentProfile MUST NOT expose soul.md, skills, or bundle contents.

    Specifically, no field whose value contains the soul.md text we put
    in the zip is allowed.
    """
    secret_marker = "SOUL_SECRET_DO_NOT_LEAK_42"
    bundle = make_valid_bundle(soul_md=f"# {secret_marker}\nrest of soul\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
        display_name="Leakage Probe",
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not implemented: {resp.text}")
    agent_id = resp.json()["id"]
    profile = publisher_client.get(f"/v1/agents/{agent_id}").json()
    serialized = repr(profile)
    assert secret_marker not in serialized, (
        f"CONTRACTS §10 + ARCHITECTURE 'cards public, bundle private': "
        f"AgentProfile leaked the soul.md text. Found {secret_marker!r} in {serialized[:300]}"
    )
    # AgentProfile shape (§1.9) MUST NOT include a soul_md_preview field
    # — that's internal-only per §1.9 comment.
    assert "soul_md_preview" not in profile, (
        "§1.9: soul_md_preview is internal-only; never exposed on AgentProfile"
    )


# --------------------------------------------------------------------------
# GET /v1/agents (discovery feed)
# --------------------------------------------------------------------------


def test_get_agents_returns_paginated_shape(publisher_client, alice_keypair):
    """CONTRACTS.md §2.3 — `{items, total, limit, offset}`."""
    _seed_one_submission(publisher_client, alice_keypair)
    resp = publisher_client.get("/v1/agents")
    assert resp.status_code == 200, f"§2.3: {resp.status_code} {resp.text}"
    body = resp.json()
    for k in ("items", "total", "limit", "offset"):
        assert k in body, f"§2.3 response missing `{k}`; got keys {list(body)}"
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int)
    assert isinstance(body["limit"], int)
    assert isinstance(body["offset"], int)
    for item in body["items"]:
        _LeaderboardEntry.model_validate(item)


def test_get_agents_invalid_sort_returns_400(publisher_client):
    """CONTRACTS.md §2.3 — `400 invalid sort`."""
    resp = publisher_client.get("/v1/agents?sort=bogus")
    assert resp.status_code in {400, 422}, (
        f"§2.3: invalid sort must be 400/422; got {resp.status_code}: {resp.text}"
    )


# --------------------------------------------------------------------------
# GET /v1/cards/{card_id}
# --------------------------------------------------------------------------


def test_get_card_returns_definition_and_best_eval_shape(publisher_client):
    """CONTRACTS.md §2.4 — response includes card_id, best_eval (or null),
    definition{...}, agent_count, latest_eval_at."""
    resp = publisher_client.get("/v1/cards/eu-ai-act")
    assert resp.status_code == 200, f"§2.4: {resp.status_code} {resp.text}"
    body = resp.json()
    for k in ("card_id", "best_eval", "definition", "agent_count", "latest_eval_at"):
        assert k in body, f"§2.4 response missing `{k}`; got {list(body)}"
    assert body["card_id"] == "eu-ai-act"
    definition = body["definition"]
    for k in ("id", "display_name", "jurisdiction", "topic", "description", "status"):
        assert k in definition, f"§2.4 definition missing `{k}`"
    assert definition["status"] in {"active", "archived"}, (
        "§2.4: definition.status must be active|archived"
    )
    if body["best_eval"] is not None:
        _EvalOutput.model_validate(body["best_eval"])


def test_get_card_404_for_unknown(publisher_client):
    resp = publisher_client.get("/v1/cards/totally-fake-card")
    assert resp.status_code == 404, "§2.4: unknown card must be 404"
    assert resp.json() == {"detail": "card not found"}, (
        "§2.4 detail must be exactly 'card not found'"
    )


# --------------------------------------------------------------------------
# GET /v1/cards/{card_id}/feed
# --------------------------------------------------------------------------


def test_get_card_feed_returns_items_and_next_since(publisher_client):
    """CONTRACTS.md §2.7 — `{items: EvalOutput[], next_since: iso|null}`."""
    resp = publisher_client.get("/v1/cards/eu-ai-act/feed?limit=5")
    assert resp.status_code == 200, f"§2.7: {resp.status_code} {resp.text}"
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    assert "next_since" in body
    assert body["next_since"] is None or isinstance(body["next_since"], str)
    for item in body["items"]:
        _EvalOutput.model_validate(item)


def test_get_card_feed_since_filter_is_iso(publisher_client):
    """§2.7 query `since` is ISO-8601."""
    resp = publisher_client.get("/v1/cards/eu-ai-act/feed?since=2026-01-01T00:00:00.000Z")
    assert resp.status_code == 200, f"§2.7 with since must work: {resp.text}"


def test_get_card_feed_reverse_chrono_order(publisher_client):
    """CONTRACTS.md §2.7 + brief 'reverse-chrono list'."""
    resp = publisher_client.get("/v1/cards/eu-ai-act/feed?limit=50")
    if resp.status_code != 200:
        pytest.skip(f"feed endpoint not ready: {resp.status_code}")
    items = resp.json()["items"]
    if len(items) < 2:
        pytest.skip("feed has too few items to verify ordering")
    timestamps = [i["ran_at"] for i in items]
    assert timestamps == sorted(timestamps, reverse=True), (
        f"§2.7 feed must be reverse chronological by ran_at; got {timestamps}"
    )


# --------------------------------------------------------------------------
# GET /v1/cards/{card_id}/attempts  (v1.1.4 — failed-evals surface)
# --------------------------------------------------------------------------


def _seed_card_attempts(
    db_path: str,
    *,
    card_id: str,
    failed_count: int,
    successful_count: int,
    miner_hotkey: str | None = None,
) -> tuple[list[str], list[str]]:
    """Seed a mix of failed and successful eval_runs for ``card_id``.

    Returns ``(failed_eval_ids, successful_eval_ids)``. Failed evals
    carry ``_ssh_hermes_failed=true`` and ``weighted_score=0``;
    successful ones carry ``weighted_score=0.7``.

    Bypasses the submit + scoring pipeline. Real ed25519 signed
    insertion happens through the publisher in higher-level smoke
    tests; for shape-pinning purposes we open a sibling connection
    to the publisher's WAL DB (same pattern as the leaderboard
    ms-collision tests above).
    """
    import asyncio
    import secrets
    from datetime import UTC, datetime, timedelta

    from cathedral.publisher import repository
    from cathedral.validator.db import connect as connect_db

    submission_id = secrets.token_hex(16)
    hotkey = miner_hotkey or ("5SeededAttempts" + "0" * 33)
    base = datetime(2026, 5, 12, 22, 0, 0, tzinfo=UTC)

    failed_ids: list[str] = []
    successful_ids: list[str] = []

    async def _do() -> None:
        conn = await connect_db(db_path)
        try:
            await repository.insert_agent_submission(
                conn,
                id=submission_id,
                miner_hotkey=hotkey,
                card_id=card_id,
                bundle_blob_key=f"bundles/{submission_id}.bin",
                bundle_hash="0" * 64,
                bundle_size_bytes=1024,
                encryption_key_id="kek-test",
                bundle_signature="b64:stub",
                display_name="Attempts Probe",
                bio=None,
                logo_url=None,
                soul_md_preview=None,
                metadata_fingerprint=secrets.token_hex(8),
                similarity_check_passed=True,
                rejection_reason=None,
                status="ranked",
                submitted_at=base,
                submitted_at_iso="2026-05-12T22:00:00.000Z",
                first_mover_at=None,
                attestation_mode="ssh-probe",
                attestation_verified_at=None,
                discovery_only=False,
            )
            # Failed evals — oldest first so DESC ordering tests are
            # deterministic (failed ids end up at the back of the DESC list).
            for i in range(failed_count):
                rid = f"22222222-2222-4222-8222-{i:012d}"
                failed_ids.append(rid)
                ran_at = base + timedelta(seconds=i)
                ran_at_iso = (
                    ran_at.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{ran_at.microsecond // 1000:03d}"
                    + "Z"
                )
                await repository.insert_eval_run(
                    conn,
                    id=rid,
                    submission_id=submission_id,
                    epoch=0,
                    round_index=0,
                    polaris_agent_id=f"ssh-hermes:{hotkey[:12]}",
                    polaris_run_id=f"run-{i}",
                    task_json={"prompt": "demo"},
                    output_card_json={
                        "id": card_id,
                        "_ssh_hermes_failed": True,
                        "failure_code": "hermes_install_invalid",
                        "failure_detail": "publisher returned 502",
                    },
                    output_card_hash="f" * 64,
                    score_parts={},
                    weighted_score=0.0,
                    ran_at=ran_at,
                    ran_at_iso=ran_at_iso,
                    duration_ms=42,
                    errors=["hermes_install_invalid: 502"],
                    cathedral_signature="stub-sig-failed",
                )

            # Successful evals — written AFTER the failed ones so they
            # appear FIRST under ORDER BY ran_at DESC.
            for i in range(successful_count):
                rid = f"33333333-3333-4333-8333-{i:012d}"
                successful_ids.append(rid)
                ran_at = base + timedelta(seconds=failed_count + i)
                ran_at_iso = (
                    ran_at.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{ran_at.microsecond // 1000:03d}"
                    + "Z"
                )
                await repository.insert_eval_run(
                    conn,
                    id=rid,
                    submission_id=submission_id,
                    epoch=0,
                    round_index=0,
                    polaris_agent_id="polaris-agent",
                    polaris_run_id=f"ok-{i}",
                    task_json={"prompt": "demo"},
                    output_card_json={"id": card_id},
                    output_card_hash="a" * 64,
                    score_parts={"source_quality": 0.7},
                    weighted_score=0.7,
                    ran_at=ran_at,
                    ran_at_iso=ran_at_iso,
                    duration_ms=100,
                    errors=None,
                    cathedral_signature="stub-sig-success",
                )
            await conn.commit()
        finally:
            await conn.close()

    asyncio.run(_do())
    return failed_ids, successful_ids


def test_get_card_attempts_returns_failed_evals(publisher_app, tmp_path):
    """v1.1.4: `/attempts` includes rows where `_ssh_hermes_failed=true`
    and `weighted_score=0`. These are real signed attempts that the
    leaderboard's empty-state design wants to render (PR #119).
    """
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "publisher.db")
    with TestClient(publisher_app) as client:
        failed_ids, _ = _seed_card_attempts(
            db_path, card_id="eu-ai-act", failed_count=3, successful_count=0
        )
        resp = client.get("/v1/cards/eu-ai-act/attempts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        returned_ids = [item["id"] for item in body["items"]]
        assert set(failed_ids).issubset(set(returned_ids)), (
            f"/attempts must include failed evals; got {returned_ids}"
        )
        # Verify the failed-eval shape — output_card.failure_code carried.
        for item in body["items"]:
            if item["id"] in failed_ids:
                assert item["weighted_score"] == 0.0
                assert item["output_card"].get("_ssh_hermes_failed") is True
                assert item["output_card"].get("failure_code") == "hermes_install_invalid"
                assert "miner_hotkey" in item


def test_get_card_attempts_returns_successful_evals(publisher_app, tmp_path):
    """v1.1.4: `/attempts` also includes successful evals (score > 0).
    The endpoint is a superset of `/feed`, not failure-only.
    """
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "publisher.db")
    with TestClient(publisher_app) as client:
        _, successful_ids = _seed_card_attempts(
            db_path, card_id="eu-ai-act", failed_count=0, successful_count=2
        )
        resp = client.get("/v1/cards/eu-ai-act/attempts")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        returned_ids = [item["id"] for item in body["items"]]
        assert set(successful_ids).issubset(set(returned_ids)), (
            f"/attempts must include successful evals; got {returned_ids}"
        )
        # Verify the successful row carries a positive score.
        for item in body["items"]:
            if item["id"] in successful_ids:
                assert item["weighted_score"] > 0


def test_get_card_attempts_respects_limit_and_default_20(publisher_app, tmp_path):
    """v1.1.4: `?limit=N` caps the page; default is 20."""
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "publisher.db")
    with TestClient(publisher_app) as client:
        _seed_card_attempts(db_path, card_id="eu-ai-act", failed_count=25, successful_count=0)

        # Default — 20.
        resp_default = client.get("/v1/cards/eu-ai-act/attempts")
        assert resp_default.status_code == 200, resp_default.text
        body_default = resp_default.json()
        assert len(body_default["items"]) == 20, (
            f"default limit must be 20; got {len(body_default['items'])}"
        )
        assert body_default["limit"] == 20

        # Explicit limit=5.
        resp_5 = client.get("/v1/cards/eu-ai-act/attempts?limit=5")
        assert resp_5.status_code == 200
        body_5 = resp_5.json()
        assert len(body_5["items"]) == 5
        assert body_5["limit"] == 5


def test_get_card_attempts_orders_by_ran_at_desc(publisher_app, tmp_path):
    """v1.1.4: most recent attempt first (`ORDER BY ran_at DESC`)."""
    from fastapi.testclient import TestClient

    db_path = str(tmp_path / "publisher.db")
    with TestClient(publisher_app) as client:
        _seed_card_attempts(db_path, card_id="eu-ai-act", failed_count=3, successful_count=3)
        resp = client.get("/v1/cards/eu-ai-act/attempts?limit=10")
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        timestamps = [i["ran_at"] for i in items]
        assert timestamps == sorted(timestamps, reverse=True), (
            f"/attempts must be reverse chronological by ran_at; got {timestamps}"
        )


# --------------------------------------------------------------------------
# GET /v1/leaderboard
# --------------------------------------------------------------------------


def test_leaderboard_requires_card_param(publisher_client):
    """CONTRACTS.md §2.8 — `400 card parameter required`."""
    resp = publisher_client.get("/v1/leaderboard")
    assert resp.status_code in {400, 422}, (
        f"§2.8: missing card param must be 400/422; got {resp.status_code}"
    )


def test_leaderboard_returns_items_and_computed_at(publisher_client):
    resp = publisher_client.get("/v1/leaderboard?card=eu-ai-act")
    assert resp.status_code == 200, f"§2.8: {resp.text}"
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    assert "computed_at" in body and isinstance(body["computed_at"], str)
    _validate_iso_z(body["computed_at"], section="§2.8 leaderboard.computed_at")
    for entry in body["items"]:
        _LeaderboardEntry.model_validate(entry)


def test_leaderboard_unknown_card_returns_404(publisher_client):
    resp = publisher_client.get("/v1/leaderboard?card=fake-card-zzz")
    assert resp.status_code == 404, f"§2.8: unknown card must be 404, got {resp.status_code}"


def test_leaderboard_ranked_by_score_desc(publisher_client):
    """§2.8 — `ranked` means current_rank ascending, current_score descending."""
    resp = publisher_client.get("/v1/leaderboard?card=eu-ai-act&limit=200")
    if resp.status_code != 200:
        pytest.skip(f"leaderboard not ready: {resp.status_code}")
    items = resp.json()["items"]
    if len(items) < 2:
        pytest.skip("leaderboard has too few items to verify ordering")
    ranks = [i["current_rank"] for i in items]
    scores = [i["current_score"] for i in items]
    assert ranks == sorted(ranks), f"§2.8: items must be ordered by rank; got {ranks}"
    assert scores == sorted(scores, reverse=True), f"§2.8: scores must be descending; got {scores}"


def test_leaderboard_dedupes_by_hotkey_keeping_best_score():
    """A miner who submits N bundles occupies one leaderboard slot — their
    best scored card. The wall promises 'one stone per mason'; the
    leaderboard now backs that promise instead of letting repeat
    submitters take multiple bricks.
    """
    from cathedral.publisher.reads import _dedupe_leaderboard_by_hotkey

    # Score-desc order is the calling contract — `list_submissions_for_card`
    # is invoked with sort='score' upstream, so the helper trusts that.
    submissions = [
        # Mason A — 3 submissions, best is 0.94
        {
            "id": "aaa-1",
            "display_name": "AL-EU",
            "logo_url": None,
            "miner_hotkey": "5CFYaq",
            "card_id": "eu-ai-act",
            "current_score": 0.94,
            "current_rank": 1,
            "submitted_at": "2026-05-13T08:00:00.000Z",
            "status": "ranked",
        },
        {
            "id": "aaa-2",
            "display_name": "AL-EU",
            "logo_url": None,
            "miner_hotkey": "5CFYaq",
            "card_id": "eu-ai-act",
            "current_score": 0.50,
            "current_rank": 2,
            "submitted_at": "2026-05-13T09:00:00.000Z",
            "status": "ranked",
        },
        # Mason B — 1 submission
        {
            "id": "bbb-1",
            "display_name": "iota1",
            "logo_url": None,
            "miner_hotkey": "5DnvAg",
            "card_id": "eu-ai-act",
            "current_score": 0.80,
            "current_rank": 2,
            "submitted_at": "2026-05-13T07:00:00.000Z",
            "status": "ranked",
        },
        {
            "id": "aaa-3",
            "display_name": "AL-EU",
            "logo_url": None,
            "miner_hotkey": "5CFYaq",
            "card_id": "eu-ai-act",
            "current_score": 0.0,
            "current_rank": 3,
            "submitted_at": "2026-05-13T13:00:00.000Z",
            "status": "ranked",
        },
        # Still-evaluating row — must be dropped
        {
            "id": "ccc-1",
            "display_name": "pending",
            "logo_url": None,
            "miner_hotkey": "5XYZ",
            "card_id": "eu-ai-act",
            "current_score": None,
            "current_rank": None,
            "submitted_at": "2026-05-13T14:00:00.000Z",
            "status": "evaluating",
        },
    ]

    items = _dedupe_leaderboard_by_hotkey(submissions, limit=50)

    hotkeys = [i["miner_hotkey"] for i in items]
    assert hotkeys == ["5CFYaq", "5DnvAg"], (
        f"expected one entry per hotkey in score-desc order, got {hotkeys}"
    )
    a_entry = next(i for i in items if i["miner_hotkey"] == "5CFYaq")
    assert a_entry["current_score"] == 0.94, (
        f"mason A's best score should be kept (0.94), got {a_entry['current_score']}"
    )
    assert a_entry["agent_id"] == "aaa-1", (
        "mason A's best-scoring agent_id should win; first-seen wins because input is score-desc"
    )


def test_leaderboard_dedupe_respects_limit():
    """`limit` caps the number of unique masons returned."""
    from cathedral.publisher.reads import _dedupe_leaderboard_by_hotkey

    submissions = [
        {
            "id": f"agent-{i}",
            "display_name": f"mason-{i}",
            "logo_url": None,
            "miner_hotkey": f"hk-{i}",
            "card_id": "eu-ai-act",
            "current_score": 1.0 - i * 0.01,
            "current_rank": i + 1,
            "submitted_at": "2026-05-13T08:00:00.000Z",
            "status": "ranked",
        }
        for i in range(20)
    ]
    items = _dedupe_leaderboard_by_hotkey(submissions, limit=5)
    assert len(items) == 5
    assert [i["miner_hotkey"] for i in items] == [f"hk-{i}" for i in range(5)]


# --------------------------------------------------------------------------
# GET /v1/leaderboard/recent (validator pull endpoint)
# --------------------------------------------------------------------------


def test_leaderboard_recent_requires_since(publisher_client):
    """CONTRACTS.md §2.9 — `since` is REQUIRED (no default)."""
    resp = publisher_client.get("/v1/leaderboard/recent")
    assert resp.status_code in {400, 422}, (
        f"§2.9: missing `since` must be 400/422; got {resp.status_code}"
    )


def test_leaderboard_recent_returns_cross_card_evals(publisher_client):
    """§2.9 response shape `{items, next_since, merkle_epoch_latest}`."""
    resp = publisher_client.get("/v1/leaderboard/recent?since=2020-01-01T00:00:00.000Z")
    assert resp.status_code == 200, f"§2.9: {resp.text}"
    body = resp.json()
    for k in ("items", "next_since", "merkle_epoch_latest"):
        assert k in body, f"§2.9 response missing `{k}`; got {list(body)}"
    assert isinstance(body["items"], list)
    for item in body["items"]:
        _EvalOutput.model_validate(item)
    assert body["merkle_epoch_latest"] is None or isinstance(body["merkle_epoch_latest"], int)


# --------------------------------------------------------------------------
# v1.1.0 legacy-cursor compat — `?since=...` without `since_id`
# --------------------------------------------------------------------------
#
# These tests exercise the publisher's two cursor branches:
#
# * Legacy v1.0.7 mode (no ``since_id`` query param) — must use strict
#   ``WHERE ran_at > ?`` so the cursor advances cleanly past the
#   boundary timestamp once a v1.0.7 validator has set
#   ``last_seen = items[-1].ran_at``. The original v1.1.0 code defaulted
#   ``since_id`` to ``""`` and ran ``(ran_at, id) > (since, '')``, which
#   re-included every row at the boundary on every pull (every UUID is
#   ``> ''``) and stranded v1.0.7 cursors forever.
#
# * v1.1.0 tuple cursor (``since_id`` present, even ``""``) — must use
#   ``WHERE (ran_at, id) > (?, ?)`` so v1.1.0 validators thread the
#   ``next_since_ran_at`` + ``next_since_id`` pair and drain ms-collision
#   bursts without re-delivery.
#
# Both branches must produce a consistent forward-progress story over
# typical (non-ms-collision) traffic so v1.0.x and v1.1.0 validators
# pulling at the same ``since`` see the same forward-edge rows.


def _seed_eval_runs_at_same_ms(db_path: str, *, count: int, ran_at_iso: str) -> list[str]:
    """Seed `count` eval_runs all at the same ``ran_at``, bypassing the
    submit + scoring pipeline.

    Returns the list of UUIDs sorted in the lexicographic order the
    publisher's ``ORDER BY er.ran_at ASC, er.id ASC`` will scan in,
    so tests can assert pagination boundaries deterministically.

    Pattern mirrors ``tests/v1/test_discovery_surface.py`` — open a
    second aiosqlite connection to the same WAL DB after the publisher
    lifespan has run its card-definition seed.
    """
    import asyncio
    import secrets
    from datetime import UTC, datetime

    from cathedral.publisher import repository
    from cathedral.validator.db import connect as connect_db

    submission_id = secrets.token_hex(16)
    miner_hotkey = "5SeededLegacyCursor" + "0" * 28

    async def _do() -> list[str]:
        conn = await connect_db(db_path)
        try:
            await repository.insert_agent_submission(
                conn,
                id=submission_id,
                miner_hotkey=miner_hotkey,
                card_id="eu-ai-act",
                bundle_blob_key=f"bundles/{submission_id}.bin",
                bundle_hash="0" * 64,
                bundle_size_bytes=1024,
                encryption_key_id="kek-test",
                bundle_signature="b64:stub",
                display_name="Legacy Cursor Probe",
                bio=None,
                logo_url=None,
                soul_md_preview=None,
                metadata_fingerprint=secrets.token_hex(8),
                similarity_check_passed=True,
                rejection_reason=None,
                status="ranked",
                submitted_at=datetime.now(UTC),
                submitted_at_iso=ran_at_iso,
                first_mover_at=None,
                attestation_mode="polaris",
                attestation_verified_at=None,
                discovery_only=False,
            )
            await repository.update_submission_score(
                conn, submission_id, current_score=0.7, current_rank=1
            )

            # Generate ids in sorted order so test assertions don't need
            # to know UUID-v4 lexicographic ordering tricks.
            ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(count)]
            for eval_id in ids:
                await repository.insert_eval_run(
                    conn,
                    id=eval_id,
                    submission_id=submission_id,
                    epoch=0,
                    round_index=0,
                    polaris_agent_id="polaris-agent",
                    polaris_run_id="polaris-run",
                    task_json={"prompt": "demo"},
                    output_card_json={"id": "eu-ai-act", "idx": eval_id[-12:]},
                    output_card_hash="a" * 64,
                    score_parts={"source_quality": 0.5},
                    weighted_score=0.5,
                    ran_at=datetime.now(UTC),
                    ran_at_iso=ran_at_iso,
                    duration_ms=100,
                    errors=None,
                    cathedral_signature="stub-signature-not-verified-by-this-test",
                    # PR1 (SN39 recovery): list_eval_runs_recent now filters
                    # to eval_output_schema_version >= 5; seed the cursor-
                    # mechanics fixtures as SAT-shaped rows so the tests
                    # exercise pagination behaviour, not the schema gate.
                    eval_output_schema_version=5,
                )
            await conn.commit()
        finally:
            await conn.close()
        return ids

    return asyncio.run(_do())


def test_leaderboard_recent_legacy_cursor_drains_ms_collision_burst(
    publisher_app, tmp_path, monkeypatch
):
    """v1.1.0 deploy-blocker fix: a v1.0.7 validator polling with just
    ``?since=...`` (no ``since_id``) MUST be able to walk through more
    rows than ``limit`` at the same millisecond.

    Pre-fix behavior: the publisher defaulted ``since_id`` to ``""`` and
    ran ``(ran_at, id) > (since, '')``. Every non-empty UUID satisfies
    ``id > ''``, so every row at ``ran_at == since`` was re-delivered on
    every pull. A v1.0.7 cursor advancing to ``items[-1].ran_at`` got
    stuck and never escaped the boundary millisecond.

    Post-fix: legacy mode uses ``WHERE ran_at > ?`` (strict ``>``). The
    cursor advances cleanly past the boundary timestamp; subsequent
    polls return ``[]`` (or whatever is past the boundary). UPSERT on
    the validator side dedupes the boundary row when re-encountered
    via normal traffic. The audit acknowledges that >limit rows at one
    millisecond is unsolvable for a stateless single-string cursor —
    this test pins the cursor-advancement behavior, not full drain.

    PR1 (SN39 recovery): the cursor fixture inserts schema_version=5
    rows; the ``/v1/leaderboard/recent`` handler only surfaces them when
    ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED`` is on.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    ran_at_iso = "2026-05-10T12:00:00.000Z"
    db_path = str(tmp_path / "publisher.db")

    with TestClient(publisher_app) as client:
        ids = _seed_eval_runs_at_same_ms(db_path, count=250, ran_at_iso=ran_at_iso)

        # First pull from a v1.0.7 validator: only `since` passed, no
        # `since_id`. The startup cursor is 1h ago.
        since = "2026-05-10T11:00:00.000Z"
        resp = client.get(
            "/v1/leaderboard/recent",
            params={"since": since, "limit": 100},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Page is saturated → legacy next_since must be non-null.
        assert len(body["items"]) == 100, (
            f"expected first legacy page to be saturated at 100 rows; got {len(body['items'])}"
        )
        assert body["next_since"] is not None, (
            "v1.0.7 fleet stalls if legacy next_since is null on a saturated page"
        )
        first_page_ids = {item["id"] for item in body["items"]}
        # The publisher orders (ran_at, id) ASC, so first 100 ids are the
        # lexicographically first 100 of our seeded set.
        assert first_page_ids == set(ids[:100])

        # Second pull: v1.0.7 advances `last_seen = items[-1].ran_at` and
        # re-polls. Pre-fix: same 100 rows come back. Post-fix: zero rows
        # come back because strict `>` excludes the boundary millisecond.
        resp2 = client.get(
            "/v1/leaderboard/recent",
            params={"since": body["next_since"], "limit": 100},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        # The exact post-fix contract: legacy mode strict `>` returns
        # zero rows because every seeded row has ran_at == since.
        assert body2["items"] == [], (
            "Legacy cursor must advance past the boundary millisecond. "
            "If this returns the same 100 rows as the first page, the "
            "pre-fix tuple-comparison bug has regressed: "
            f"got {len(body2['items'])} rows, sample id="
            f"{(body2['items'][0]['id'] if body2['items'] else None)!r}"
        )
        assert body2["next_since"] is None, (
            f"caught-up legacy response must emit next_since=null; got {body2['next_since']!r}"
        )


def test_leaderboard_recent_tuple_cursor_drains_ms_collision_burst(
    publisher_app, tmp_path, monkeypatch
):
    """v1.1.0 tuple cursor: a validator threading
    ``since_ran_at`` + ``since_id`` MUST drain all rows at a boundary
    millisecond across pages of ``limit``.

    This is the v1.1.0 happy path the cadence eval load depends on. The
    smoke test ``test_v107_v110_back_compat`` exercises the v1.0.7 side
    of the wire; this one pins the v1.1.0-validator side against the
    real publisher (no in-memory fake).

    PR1 (SN39 recovery): the cursor fixture inserts schema_version=5
    rows; the ``/v1/leaderboard/recent`` handler only surfaces them when
    ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED`` is on.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    ran_at_iso = "2026-05-10T12:00:00.000Z"
    db_path = str(tmp_path / "publisher.db")

    with TestClient(publisher_app) as client:
        ids = _seed_eval_runs_at_same_ms(db_path, count=250, ran_at_iso=ran_at_iso)

        all_persisted: list[str] = []
        cursor_ran_at = "2026-05-10T11:00:00.000Z"
        cursor_id: str = ""
        for _ in range(10):  # 250 / 100 = 3 saturated pages + 1 short, with headroom
            resp = client.get(
                "/v1/leaderboard/recent",
                params={
                    "since_ran_at": cursor_ran_at,
                    "since_id": cursor_id,
                    "limit": 100,
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            page_ids = [item["id"] for item in body["items"]]
            all_persisted.extend(page_ids)
            if len(body["items"]) < 100:
                break
            # v1.1.0 validators read the tuple cursor fields.
            cursor_ran_at = body["next_since_ran_at"]
            cursor_id = body["next_since_id"]
            assert cursor_ran_at is not None
            assert cursor_id is not None
        else:
            pytest.fail(
                f"tuple cursor did not drain 250 ms-colliding rows in 10 "
                f"pages of 100; persisted {len(all_persisted)} ids"
            )

        assert set(all_persisted) == set(ids), (
            f"tuple cursor missed rows: persisted {len(set(all_persisted))} of {len(ids)}"
        )
        # Each row exactly once — no re-delivery under tuple cursor.
        assert len(all_persisted) == len(ids), (
            f"tuple cursor re-delivered rows: persisted {len(all_persisted)} "
            f"entries for {len(set(all_persisted))} unique ids"
        )


def test_leaderboard_recent_legacy_and_tuple_agree_on_normal_traffic(
    publisher_app, tmp_path, monkeypatch
):
    """Sanity: when ``ran_at`` values do NOT collide, the legacy cursor
    (``?since=...``) and the tuple cursor
    (``?since_ran_at=...&since_id=...``) return the same set of rows
    over consecutive pages.

    Pins forward-progress equivalence so a single subnet running a mix
    of v1.0.x and v1.1.0 validators sees the same eval feed on both
    binaries during the rollout window.

    PR1 (SN39 recovery): the cursor fixture inserts schema_version=5
    rows; the ``/v1/leaderboard/recent`` handler only surfaces them when
    ``CATHEDRAL_TASK_FAMILY_FEED_ENABLED`` is on.
    """
    import asyncio
    import secrets
    from datetime import UTC, datetime, timedelta

    from fastapi.testclient import TestClient

    from cathedral.publisher import repository
    from cathedral.validator.db import connect as connect_db

    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_FEED_ENABLED", "true")
    db_path = str(tmp_path / "publisher.db")

    with TestClient(publisher_app) as client:
        # Seed 5 rows at ms-spaced ran_ats so neither cursor mode degrades.
        base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        submission_id = secrets.token_hex(16)
        expected_ids: list[str] = []

        async def _do() -> None:
            conn = await connect_db(db_path)
            try:
                await repository.insert_agent_submission(
                    conn,
                    id=submission_id,
                    miner_hotkey="5SeededMixedCursorXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
                    card_id="eu-ai-act",
                    bundle_blob_key=f"bundles/{submission_id}.bin",
                    bundle_hash="0" * 64,
                    bundle_size_bytes=1024,
                    encryption_key_id="kek-test",
                    bundle_signature="b64:stub",
                    display_name="Mixed Cursor Probe",
                    bio=None,
                    logo_url=None,
                    soul_md_preview=None,
                    metadata_fingerprint=secrets.token_hex(8),
                    similarity_check_passed=True,
                    rejection_reason=None,
                    status="ranked",
                    submitted_at=base,
                    submitted_at_iso="2026-05-10T12:00:00.000Z",
                    first_mover_at=None,
                    attestation_mode="polaris",
                    attestation_verified_at=None,
                    discovery_only=False,
                )
                await repository.update_submission_score(
                    conn, submission_id, current_score=0.7, current_rank=1
                )
                for i in range(5):
                    rid = f"11111111-1111-4111-8111-{i:012d}"
                    expected_ids.append(rid)
                    ran_at = base + timedelta(milliseconds=i * 10)
                    ran_at_iso = (
                        ran_at.strftime("%Y-%m-%dT%H:%M:%S.")
                        + f"{ran_at.microsecond // 1000:03d}"
                        + "Z"
                    )
                    await repository.insert_eval_run(
                        conn,
                        id=rid,
                        submission_id=submission_id,
                        epoch=0,
                        round_index=0,
                        polaris_agent_id="polaris-agent",
                        polaris_run_id="polaris-run",
                        task_json={"prompt": "demo"},
                        output_card_json={"id": "eu-ai-act"},
                        output_card_hash="a" * 64,
                        score_parts={"source_quality": 0.5},
                        weighted_score=0.5,
                        ran_at=ran_at,
                        ran_at_iso=ran_at_iso,
                        duration_ms=100,
                        errors=None,
                        cathedral_signature="stub-signature-not-verified-by-this-test",
                        # PR1 (SN39 recovery): list_eval_runs_recent now
                        # filters to eval_output_schema_version >= 5; seed
                        # this cursor-equivalence fixture as SAT-shaped
                        # rows so the test exercises cursor mechanics, not
                        # the schema gate.
                        eval_output_schema_version=5,
                    )
                await conn.commit()
            finally:
                await conn.close()

        asyncio.run(_do())

        since = "2026-05-10T11:00:00.000Z"
        # Legacy cursor — single page covers all 5.
        resp_legacy = client.get(
            "/v1/leaderboard/recent",
            params={"since": since, "limit": 200},
        )
        assert resp_legacy.status_code == 200, resp_legacy.text
        legacy_ids = [item["id"] for item in resp_legacy.json()["items"]]

        # Tuple cursor — same since, explicit empty since_id.
        resp_tuple = client.get(
            "/v1/leaderboard/recent",
            params={"since_ran_at": since, "since_id": "", "limit": 200},
        )
        assert resp_tuple.status_code == 200, resp_tuple.text
        tuple_ids = [item["id"] for item in resp_tuple.json()["items"]]

        assert legacy_ids == tuple_ids, (
            "legacy and tuple cursor must agree on row set + order over "
            f"non-ms-collision traffic; got legacy={legacy_ids} "
            f"tuple={tuple_ids}"
        )
        # And both must cover the full seeded set.
        assert set(legacy_ids) >= set(expected_ids)


# --------------------------------------------------------------------------
# GET /v1/merkle/{epoch}
# --------------------------------------------------------------------------


def test_merkle_unknown_epoch_returns_404(publisher_client):
    """CONTRACTS.md §2.10 — `404 epoch not anchored`."""
    resp = publisher_client.get("/v1/merkle/999999")
    assert resp.status_code == 404, f"§2.10: {resp.status_code}"
    assert resp.json() == {"detail": "epoch not anchored"}, (
        "§2.10 detail must be exactly 'epoch not anchored'"
    )


def test_merkle_response_shape_when_present(publisher_client):
    """§2.10 — response is MerkleAnchor with leaf_hashes populated."""
    # Try epoch 1 — the contract says the merkle endpoint always returns
    # MerkleAnchor; if no anchor exists we expect 404.
    resp = publisher_client.get("/v1/merkle/1")
    if resp.status_code == 404:
        pytest.skip("no merkle anchor for epoch 1 yet — shape verified by 404 test")
    assert resp.status_code == 200, f"§2.10: {resp.status_code}"
    anchor = _MerkleAnchor.model_validate(resp.json())
    assert anchor.leaf_hashes is not None, (
        "§2.10: GET /merkle/{epoch} response MUST populate leaf_hashes (§1.13 last comment)"
    )
    # Each leaf hash is lowercase hex, 64 chars (§9 lock #4).
    for leaf in anchor.leaf_hashes:
        assert len(leaf) == 64 and leaf == leaf.lower(), (
            f"§9 lock #4: blake3 hex must be lowercase 64 chars; got {leaf!r}"
        )
    assert len(anchor.merkle_root) == 64 and anchor.merkle_root == anchor.merkle_root.lower()


# --------------------------------------------------------------------------
# GET /v1/miners/{hotkey}/agents
# --------------------------------------------------------------------------


def test_get_miner_agents_returns_items(publisher_client, alice_keypair):
    """CONTRACTS.md §2.11 — `{items: AgentProfile[]}`."""
    _seed_one_submission(publisher_client, alice_keypair)
    resp = publisher_client.get(f"/v1/miners/{alice_keypair.ss58_address}/agents")
    assert resp.status_code == 200, f"§2.11: {resp.text}"
    body = resp.json()
    assert "items" in body and isinstance(body["items"], list)
    for item in body["items"]:
        _AgentProfile.model_validate(item)


# --------------------------------------------------------------------------
# GET /v1/cards/{card_id}/eval-spec
# --------------------------------------------------------------------------


def test_get_eval_spec_returns_full_rubric(publisher_client):
    """CONTRACTS.md §2.6 — full eval spec including scoring rubric."""
    resp = publisher_client.get("/v1/cards/eu-ai-act/eval-spec")
    assert resp.status_code == 200, f"§2.6: {resp.text}"
    body = resp.json()
    for k in (
        "card_id",
        "display_name",
        "jurisdiction",
        "description_md",
        "eval_spec_md",
        "scoring_rubric",
        "task_templates",
        "source_pool",
        "refresh_cadence_hours",
    ):
        assert k in body, f"§2.6 response missing `{k}`"
    rubric = body["scoring_rubric"]
    # Six weights per §2.6.
    for w in (
        "source_quality_weight",
        "maintenance_weight",
        "freshness_weight",
        "specificity_weight",
        "usefulness_weight",
        "clarity_weight",
    ):
        assert w in rubric, f"§2.6 scoring_rubric missing `{w}`"


# --------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------


def test_health_endpoint_shape(publisher_client):
    """CONTRACTS.md §2.12."""
    resp = publisher_client.get("/health")
    # 200 ok or 503 degraded — both are contract-valid.
    assert resp.status_code in {200, 503}, f"§2.12: {resp.status_code} {resp.text}"
    body = resp.json()
    assert "status" in body
    assert body["status"] in {"ok", "degraded"}, "§2.12: status must be ok|degraded"
    assert "checks" in body and isinstance(body["checks"], dict)


# --------------------------------------------------------------------------
# Scored-surface state correctness — cadence/refresh + stale-state defense
# --------------------------------------------------------------------------
#
# Anchored on the live regression observed 2026-05-15: agent
# 497c81fa-... carried status='evaluating' + current_score=0.47 from a
# prior ranked round, and the leaderboard surfaced it as a scored entry
# with last_eval_at pointing at submitted_at rather than the most recent
# eval_runs.ran_at. Cover:
#
# 1. dedupe drops status='evaluating' even when current_score is non-null
# 2. /v1/leaderboard last_eval_at reads MAX(eval_runs.ran_at)
# 3. repository list helpers default to status='ranked' only
# 4. orchestrator cadence refresh failure keeps row ranked + score/rank


def test_dedupe_drops_evaluating_rows_with_prior_score():
    """A row carrying status='evaluating' but a non-null current_score
    from a prior ranked round must not appear on the leaderboard. This
    is the exact state the live agent (497c81fa-...) was stuck in on
    2026-05-15: prior 0.94, then a 0.0 cadence eval flipped status to
    evaluating with current_score=0.47 still on the row.
    """
    from cathedral.publisher.reads import _dedupe_leaderboard_by_hotkey

    submissions = [
        # Ranked mason — must appear
        {
            "id": "ranked-1",
            "display_name": "scored",
            "logo_url": None,
            "miner_hotkey": "5RANKED",
            "card_id": "eu-ai-act",
            "current_score": 0.80,
            "current_rank": 1,
            "submitted_at": "2026-05-13T07:00:00.000Z",
            "status": "ranked",
        },
        # Cadence-refresh-in-flight row with stale score — must be dropped
        {
            "id": "iota1-v110-final",
            "display_name": "iota1",
            "logo_url": None,
            "miner_hotkey": "5STALE",
            "card_id": "eu-ai-act",
            "current_score": 0.47,
            "current_rank": 28,
            "submitted_at": "2026-05-13T08:09:08.269Z",
            "status": "evaluating",
        },
        # First-eval queued row — also dropped
        {
            "id": "queued-1",
            "display_name": "pending",
            "logo_url": None,
            "miner_hotkey": "5QUEUED",
            "card_id": "eu-ai-act",
            "current_score": None,
            "current_rank": None,
            "submitted_at": "2026-05-14T09:00:00.000Z",
            "status": "queued",
        },
    ]
    items = _dedupe_leaderboard_by_hotkey(submissions, limit=50)
    hotkeys = [i["miner_hotkey"] for i in items]
    assert hotkeys == ["5RANKED"], (
        f"only the ranked row may appear on the leaderboard; got {hotkeys}"
    )


def test_leaderboard_entry_uses_latest_eval_at_when_present():
    """The wire-shape LeaderboardEntry.last_eval_at must reflect the
    most recent eval_runs.ran_at, not submitted_at. Without this, the
    leaderboard 'last seen' timestamp shows the moment of FIRST submit
    forever, even after dozens of cadence refreshes."""
    from cathedral.publisher.reads import _submission_to_leaderboard_entry

    sub = {
        "id": "abc",
        "display_name": "test",
        "logo_url": None,
        "miner_hotkey": "5HK",
        "card_id": "eu-ai-act",
        "current_score": 0.7,
        "current_rank": 1,
        "submitted_at": "2026-05-01T00:00:00.000Z",
        "latest_eval_at": "2026-05-14T08:12:54.509Z",
        "status": "ranked",
    }
    entry = _submission_to_leaderboard_entry(sub)
    assert entry["last_eval_at"] == "2026-05-14T08:12:54.509Z", (
        "last_eval_at must come from latest_eval_at when set"
    )


def test_leaderboard_entry_falls_back_to_submitted_at_without_eval():
    """Edge case: a fixture/row without `latest_eval_at` (e.g. dict
    constructed without the join) should still surface submitted_at
    so the wire shape is satisfied. The fall-through chain is
    latest_eval_at → submitted_at → now."""
    from cathedral.publisher.reads import _submission_to_leaderboard_entry

    sub = {
        "id": "abc",
        "display_name": "test",
        "logo_url": None,
        "miner_hotkey": "5HK",
        "card_id": "eu-ai-act",
        "current_score": 0.7,
        "current_rank": 1,
        "submitted_at": "2026-05-01T00:00:00.000Z",
        "status": "ranked",
    }
    entry = _submission_to_leaderboard_entry(sub)
    assert entry["last_eval_at"] == "2026-05-01T00:00:00.000Z"


def test_dedupe_preserves_one_per_hotkey_ranked_only():
    """Existing one-entry-per-hotkey behavior must still hold once we
    add the status='ranked' filter — the dedupe still picks the
    best-scoring ranked row per hotkey, and a same-hotkey 'evaluating'
    row never wins even if it had a higher score before."""
    from cathedral.publisher.reads import _dedupe_leaderboard_by_hotkey

    submissions = [
        # Same hotkey: an evaluating row (stale 0.94) listed first
        # because score-desc sorting upstream. The dedupe must skip
        # it and pick the ranked 0.80 row instead.
        {
            "id": "stale",
            "display_name": "agent",
            "logo_url": None,
            "miner_hotkey": "5SAME",
            "card_id": "eu-ai-act",
            "current_score": 0.94,
            "current_rank": 1,
            "submitted_at": "2026-05-10T08:00:00.000Z",
            "status": "evaluating",
        },
        {
            "id": "fresh",
            "display_name": "agent",
            "logo_url": None,
            "miner_hotkey": "5SAME",
            "card_id": "eu-ai-act",
            "current_score": 0.80,
            "current_rank": 2,
            "submitted_at": "2026-05-13T08:00:00.000Z",
            "status": "ranked",
        },
    ]
    items = _dedupe_leaderboard_by_hotkey(submissions, limit=10)
    assert len(items) == 1, f"one slot per hotkey; got {len(items)}"
    assert items[0]["agent_id"] == "fresh", "must pick the ranked row, not the stale evaluating one"
    assert items[0]["current_score"] == 0.80
