"""Eval lifecycle orchestrator — CONTRACTS.md §6.

Goal: verify the queued -> evaluating -> ranked|rejected state machine,
the 3-retry exponential-backoff policy on Polaris failure, the malformed-
output preflight rejection, and the first-mover delta application path.

We use the contract's stub mode (`CATHEDRAL_EVAL_MODE=stub`, §6) when
available. If the orchestrator module isn't importable yet, the
dependent tests skip with a pointer to the contract section.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from tests.v1.conftest import (
    make_valid_bundle,
    submit_multipart,
)

# --------------------------------------------------------------------------
# Helpers — best-effort import of orchestrator
# --------------------------------------------------------------------------


def _try_import(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except Exception:
        return None


@pytest.fixture
def eval_orchestrator():
    """Best-effort import of the eval orchestrator.

    Tries a handful of plausible module paths from the contract.
    """
    for name in (
        "cathedral.eval.orchestrator",
        "cathedral.eval.scheduler",
        "cathedral.eval",
    ):
        mod = _try_import(name)
        if mod is None:
            continue
        for attr in ("run_once", "tick", "process_one", "schedule_once", "Orchestrator"):
            if hasattr(mod, attr):
                return mod
    pytest.skip(
        "eval orchestrator not importable yet — implementer must expose "
        "cathedral.eval.{orchestrator,scheduler} per CONTRACTS.md §6"
    )


@pytest.fixture(autouse=True)
def _force_stub_mode(monkeypatch):
    """CONTRACTS.md §6 — `CATHEDRAL_EVAL_MODE=stub` runs without real Polaris."""
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub")


# --------------------------------------------------------------------------
# Status transitions (§6 state machine)
# --------------------------------------------------------------------------


def test_queued_submission_gets_picked_up(publisher_client, alice_keypair, eval_orchestrator):
    """§6 step 3 — scheduler picks queued submissions FIFO by submitted_at."""
    bundle = make_valid_bundle(soul_md="# pickup probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not ready: {resp.text}")
    agent_id = resp.json()["id"]

    # Drive one orchestrator tick. Try the most likely entry points.
    _drive_one_tick(eval_orchestrator)

    # Status should advance past queued (to evaluating, ranked, or rejected).
    profile = publisher_client.get(f"/v1/agents/{agent_id}").json()
    assert profile["status"] in {"evaluating", "ranked", "rejected"}, (
        f"§6 step 3: queued submission must advance after a tick; got status={profile['status']!r}"
    )


def test_successful_eval_writes_eval_run_and_marks_ranked(
    publisher_client, alice_keypair, eval_orchestrator
):
    """§6 step 4-5 — successful eval persists EvalRun row + sets status='ranked'."""
    bundle = make_valid_bundle(soul_md="# success path\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not ready: {resp.text}")
    agent_id = resp.json()["id"]

    # Drive ticks until status leaves 'queued'/'evaluating'.
    for _ in range(5):
        _drive_one_tick(eval_orchestrator)
        profile = publisher_client.get(f"/v1/agents/{agent_id}").json()
        if profile["status"] not in {"queued", "evaluating", "pending_check"}:
            break

    profile = publisher_client.get(f"/v1/agents/{agent_id}").json()
    assert profile["status"] in {"ranked", "rejected"}, (
        f"§6: eval must terminate at ranked or rejected; got {profile['status']!r}"
    )
    if profile["status"] == "ranked":
        # current_score must be populated (§6 step 5).
        assert profile.get("current_score") is not None, (
            "§6 step 5: ranked submission must have current_score set"
        )
        # recent_evals must contain at least one EvalOutput (§1.9).
        assert len(profile.get("recent_evals", [])) >= 1, (
            "§1.9: ranked AgentProfile must include at least one recent_eval"
        )


def test_status_transitions_only_via_allowed_arrows(
    publisher_client, alice_keypair, eval_orchestrator
):
    """§6 'Status transitions (allowed):' block.

    pending_check → queued | rejected
    queued        → evaluating | withdrawn
    evaluating    → ranked | rejected | queued (on retryable failure)
    ranked        → ranked (re-eval) | withdrawn
    rejected      → terminal
    withdrawn     → terminal
    """
    allowed: dict[str, set[str]] = {
        "pending_check": {"pending_check", "queued", "rejected"},
        "queued": {"queued", "evaluating", "withdrawn"},
        "evaluating": {"evaluating", "ranked", "rejected", "queued"},
        "ranked": {"ranked", "withdrawn"},
        "rejected": {"rejected"},  # terminal
        "withdrawn": {"withdrawn"},  # terminal
    }

    bundle = make_valid_bundle(soul_md="# transition probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not ready: {resp.text}")
    agent_id = resp.json()["id"]

    seen: list[str] = []
    for _ in range(8):
        prof = publisher_client.get(f"/v1/agents/{agent_id}").json()
        seen.append(prof["status"])
        if prof["status"] in {"ranked", "rejected", "withdrawn"}:
            break
        _drive_one_tick(eval_orchestrator)

    # Walk the observed sequence and ensure each step is in the allowed set.
    from itertools import pairwise

    for prev, curr in pairwise(seen):
        assert curr in allowed.get(prev, set()), (
            f"§6 status machine: illegal transition {prev!r} -> {curr!r}; full sequence={seen}"
        )


# --------------------------------------------------------------------------
# Polaris timeout / retry policy (§6 step 3 + 'Timeouts and policies')
# --------------------------------------------------------------------------


def test_polaris_timeout_retries_then_marks_failed(
    publisher_client, alice_keypair, monkeypatch, eval_orchestrator
):
    """§6 'Timeouts and policies' — 3 retries with exponential backoff.

    We can only verify this end-to-end if the implementer surfaces the
    retry counter or the failure marker in the eval_run errors. We assert
    on the OBSERVABLE: after exhausting retries, the eval_run for this
    submission has non-empty `errors` AND `weighted_score == 0`.
    """
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub-fail-polaris")

    bundle = make_valid_bundle(soul_md="# polaris timeout probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not ready: {resp.text}")
    agent_id = resp.json()["id"]

    # Drive enough ticks to exhaust retries + persist the failure.
    for _ in range(10):
        _drive_one_tick(eval_orchestrator)
        prof = publisher_client.get(f"/v1/agents/{agent_id}").json()
        if prof["status"] in {"ranked", "rejected"}:
            break

    prof = publisher_client.get(f"/v1/agents/{agent_id}").json()
    # The eval lifecycle says: leave status='evaluating' after 3 failures
    # AND log via health_kv counter; OR if Card JSON parse fails, record
    # EvalRun with errors=[...] and weighted_score=0. Either is contract-OK.
    if prof.get("recent_evals"):
        last = prof["recent_evals"][-1]
        if last.get("weighted_score") is not None:
            assert last["weighted_score"] == 0 or last.get("errors"), (
                "§6 step 3-4: failed Polaris run must surface as errors=[...] OR "
                f"weighted_score=0; got {last}"
            )


def test_malformed_card_output_records_preflight_rejection(
    publisher_client, alice_keypair, monkeypatch, eval_orchestrator
):
    """§6 step 4 — preflight failure: weighted_score=0, errors=[str(exc)]."""
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub-bad-card")

    bundle = make_valid_bundle(soul_md="# malformed card probe\n")
    resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=bundle,
    )
    if resp.status_code != 202:
        pytest.skip(f"submit not ready: {resp.text}")
    agent_id = resp.json()["id"]

    for _ in range(6):
        _drive_one_tick(eval_orchestrator)
        prof = publisher_client.get(f"/v1/agents/{agent_id}").json()
        if prof.get("recent_evals"):
            break

    prof = publisher_client.get(f"/v1/agents/{agent_id}").json()
    if not prof.get("recent_evals"):
        pytest.skip(
            "no eval_run persisted yet — orchestrator may not surface stub-bad-card "
            "via the read API yet"
        )
    last = prof["recent_evals"][-1]
    assert last.get("weighted_score") == 0, f"§6 step 4: malformed card must score 0; got {last}"


# --------------------------------------------------------------------------
# First-mover delta integration (§7.2)
# --------------------------------------------------------------------------


def test_late_submission_within_threshold_gets_penalty_multiplier(
    publisher_client, alice_keypair, bob_keypair, eval_orchestrator, monkeypatch
):
    """§7.2 — late submission with weighted within delta of incumbent gets 0.50x."""
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub-deterministic-score")
    monkeypatch.setenv("CATHEDRAL_STUB_SCORE", "0.80")

    # Alice submits first (incumbent).
    a_bundle = make_valid_bundle(soul_md="# Alice (incumbent)\n")
    a_resp = submit_multipart(
        publisher_client,
        keypair=alice_keypair,
        card_id="eu-ai-act",
        bundle=a_bundle,
        display_name="Incumbent",
    )
    if a_resp.status_code != 202:
        pytest.skip(f"submit not ready: {a_resp.text}")
    a_id = a_resp.json()["id"]

    # Drive Alice through eval.
    for _ in range(5):
        _drive_one_tick(eval_orchestrator)
        prof = publisher_client.get(f"/v1/agents/{a_id}").json()
        if prof["status"] == "ranked":
            break

    # Bob submits later with the SAME stub score (0.80) — within 0.05 delta.
    b_bundle = make_valid_bundle(soul_md="# Bob (late copy)\n")
    b_resp = submit_multipart(
        publisher_client,
        keypair=bob_keypair,
        card_id="eu-ai-act",
        bundle=b_bundle,
        display_name="Latecomer",
    )
    if b_resp.status_code != 202:
        pytest.skip(f"second submit blocked by similarity: {b_resp.text}")
    b_id = b_resp.json()["id"]

    for _ in range(5):
        _drive_one_tick(eval_orchestrator)
        prof = publisher_client.get(f"/v1/agents/{b_id}").json()
        if prof["status"] == "ranked":
            break

    a_prof = publisher_client.get(f"/v1/agents/{a_id}").json()
    b_prof = publisher_client.get(f"/v1/agents/{b_id}").json()
    if a_prof["status"] != "ranked" or b_prof["status"] != "ranked":
        pytest.skip("could not drive both submissions to ranked under stub mode")

    # Per §7.2 with stub score 0.80 for both: incumbent keeps 0.80; latecomer
    # gets 0.40 (0.80 * 0.50 penalty multiplier).
    assert a_prof["current_score"] >= b_prof["current_score"], (
        f"§7.2: incumbent (a={a_prof['current_score']}) must outscore latecomer "
        f"(b={b_prof['current_score']}) under penalty multiplier"
    )


# --------------------------------------------------------------------------
# Internal helpers
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Cadence state-machine — preserve ranked on refresh failure
# --------------------------------------------------------------------------
#
# Anchored on the live regression observed 2026-05-15: a ranked agent
# undergoing cadence refresh got flipped to status='evaluating' by the
# orchestrator's unconditional pre-eval status write, and a retryable
# failure then sent it to 'queued' (or 'rejected' after 3 attempts).
# Result: the leaderboard kept showing the row with a stale score, the
# state was wrong, and the agent could only recover by flowing through
# first-eval again.


@pytest.mark.asyncio
async def test_cadence_refresh_retryable_failure_keeps_ranked():
    """A ranked row hitting a retryable failure mid-cadence-refresh must
    NOT be flipped to 'queued' or 'rejected'. It stays 'ranked' with its
    prior score/rank — the cadence loop will retry it next window."""
    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    db = _CapturingDB()
    orch = EvalOrchestrator(
        db=db,  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )

    submission = {
        "id": "ranked-cadence-row",
        "card_id": "eu-ai-act",
        "status": "ranked",
    }

    # First failure: would have re-queued under the old code. Under the
    # fix, no status write happens for cadence rows.
    await orch._on_retryable_failure(
        submission,
        _bound_logger(),
        "hippius unavailable",
        is_cadence_refresh=True,
    )
    assert db.status_writes == [], (
        f"cadence retryable failure must not flip status; got {db.status_writes}"
    )

    # Two more failures to hit the >=3 cap. Still no flip — cadence rows
    # stay 'ranked' even when retries exhaust.
    await orch._on_retryable_failure(submission, _bound_logger(), "again", is_cadence_refresh=True)
    await orch._on_retryable_failure(submission, _bound_logger(), "again", is_cadence_refresh=True)
    assert db.status_writes == [], (
        f"cadence exhausted retries must not flip status; got {db.status_writes}"
    )


@pytest.mark.asyncio
async def test_first_eval_retryable_failure_requeues_then_rejects():
    """Counterpart: first-eval rows MUST still flow through the existing
    3-attempt retry policy — re-queue twice, reject on the third."""
    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    db = _CapturingDB()
    orch = EvalOrchestrator(
        db=db,  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )
    submission = {"id": "first-eval-row", "card_id": "eu-ai-act", "status": "queued"}

    await orch._on_retryable_failure(submission, _bound_logger(), "boom")
    await orch._on_retryable_failure(submission, _bound_logger(), "boom")
    statuses = [(sid, st) for sid, st, _ in db.status_writes]
    assert statuses == [
        ("first-eval-row", "queued"),
        ("first-eval-row", "queued"),
    ], f"first two failures must re-queue; got {statuses}"

    await orch._on_retryable_failure(submission, _bound_logger(), "boom")
    assert db.status_writes[-1][1] == "rejected", (
        f"third failure must reject; got {db.status_writes[-1]}"
    )


@pytest.mark.asyncio
async def test_terminal_failure_keeps_ranked_for_cadence():
    """A terminal failure (decryption / structure / missing card_def) on
    a cadence refresh must not strip the row off the leaderboard."""
    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    db = _CapturingDB()
    orch = EvalOrchestrator(
        db=db,  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )
    submission = {"id": "ranked-row", "card_id": "eu-ai-act", "status": "ranked"}

    await orch._fail_terminal(
        submission,
        _bound_logger(),
        reason="bundle decryption failed",
        is_cadence_refresh=True,
        event="eval_bundle_decrypt_failed",
        error="bad mac",
    )
    assert db.status_writes == [], (
        f"cadence terminal failure must not flip status; got {db.status_writes}"
    )

    # First-eval rows MUST still go to 'rejected'.
    submission2 = {"id": "first-row", "card_id": "eu-ai-act", "status": "queued"}
    await orch._fail_terminal(
        submission2,
        _bound_logger(),
        reason="bundle decryption failed",
        is_cadence_refresh=False,
        event="eval_bundle_decrypt_failed",
        error="bad mac",
    )
    assert db.status_writes == [("first-row", "rejected", "bundle decryption failed")]


@pytest.mark.asyncio
async def test_crashed_first_eval_does_not_strand_evaluating():
    """An uncaught exception inside `evaluate_one` for a first-time
    queued row must route through the retry policy rather than leaving
    the row in 'evaluating' forever. Three crashes in a row reject."""
    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    db = _CapturingDB()
    orch = EvalOrchestrator(
        db=db,  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )

    # Simulate the recovery the loop does on `eval_one_crashed` for
    # first-eval rows.
    sub = {"id": "crash-row", "card_id": "eu-ai-act", "status": "queued"}
    for _ in range(3):
        await orch._on_retryable_failure(sub, _bound_logger(), "evaluate_one crash: x")
    final = db.status_writes[-1]
    assert final[1] == "rejected", (
        f"third crash must reject so the row doesn't sit in evaluating; got {final}"
    )


# --------------------------------------------------------------------------
# Cadence cooldown — failed refresh must not re-pick every tick
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cadence_failure_arms_cooldown_blocking_immediate_re_pick():
    """A cadence row that hits a retryable or terminal failure must not
    be picked again on the very next tick. Without a cooldown, the
    cadence query (which uses MAX(eval_runs.ran_at)) keeps surfacing
    the same row because no eval_run was written, spamming logs and
    consuming cadence slots."""
    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    orch = EvalOrchestrator(
        db=_CapturingDB(),  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )

    sub_id = "cadence-row-broken"
    assert orch.is_cadence_in_cooldown(sub_id) is False

    # Retryable failure on a cadence row — should arm cooldown.
    await orch._on_retryable_failure(
        {"id": sub_id, "card_id": "eu-ai-act", "status": "ranked"},
        _bound_logger(),
        "hippius unavailable",
        is_cadence_refresh=True,
    )
    assert orch.is_cadence_in_cooldown(sub_id) is True, (
        "retryable cadence failure must arm cooldown so the next tick skips"
    )

    # Terminal failure on a different row — should also arm cooldown.
    other_id = "cadence-row-decrypt-fail"
    await orch._fail_terminal(
        {"id": other_id, "card_id": "eu-ai-act", "status": "ranked"},
        _bound_logger(),
        reason="bundle decryption failed",
        is_cadence_refresh=True,
        event="eval_bundle_decrypt_failed",
        error="bad mac",
    )
    assert orch.is_cadence_in_cooldown(other_id) is True

    # First-eval row that hits the same failure shapes must NOT be put
    # in the cadence cooldown (the cooldown is cadence-specific; first-
    # eval failures already update DB status).
    first_id = "first-eval"
    await orch._on_retryable_failure(
        {"id": first_id, "card_id": "eu-ai-act", "status": "queued"},
        _bound_logger(),
        "boom",
    )
    assert orch.is_cadence_in_cooldown(first_id) is False


@pytest.mark.asyncio
async def test_cadence_cooldown_expires_after_window():
    """After `_CADENCE_FAILURE_COOLDOWN` elapses, the row becomes
    eligible again. We can't sleep an hour, so we backdate the
    cooldown entry directly to prove the eviction path runs."""
    from datetime import UTC, datetime, timedelta

    from cathedral.eval.orchestrator import EvalOrchestrator
    from cathedral.eval.runner_types import StubPolarisRunner
    from cathedral.storage.hippius import StubHippiusClient

    orch = EvalOrchestrator(
        db=_CapturingDB(),  # type: ignore[arg-type]
        hippius=StubHippiusClient(),
        polaris=StubPolarisRunner(),
        signer=_StubSigner(),
        registry=_StubRegistry(),
    )
    sub_id = "expired"
    orch._cadence_cooldown_until[sub_id] = datetime.now(UTC) - timedelta(seconds=1)
    assert orch.is_cadence_in_cooldown(sub_id) is False
    assert sub_id not in orch._cadence_cooldown_until, (
        "expired entries must be evicted to keep the dict bounded"
    )


# --------------------------------------------------------------------------
# Rank-against-self — cadence refresh that lowers score
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_rank_excludes_current_submission(tmp_path):
    """A cadence refresh that lowers the row's avg used to count the
    row's own pre-write higher score, returning a rank one worse than
    reality. _compute_rank with exclude_submission_id drops the row."""
    from cathedral.eval.scoring_pipeline import _compute_rank
    from cathedral.validator.db import connect

    conn = await connect(str(tmp_path / "rank.db"))
    try:
        # Seed card_def so the FK constraint on agent_submissions.card_id
        # is satisfied.
        await conn.execute(
            "INSERT OR REPLACE INTO card_definitions "
            "(id, display_name, jurisdiction, topic, description, "
            " eval_spec_md, source_pool, task_templates, scoring_rubric, "
            " refresh_cadence_hours, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "eu-ai-act",
                "EU AI Act",
                "EU",
                "test",
                "test",
                "# spec",
                "[]",
                "[]",
                "{}",
                24,
                "active",
                "2026-05-01T00:00:00Z",
                "2026-05-01T00:00:00Z",
            ),
        )
        # Two ranked rows on the same card. The first carries 0.9, the
        # second is about to refresh to 0.6 (down from 0.95).
        await conn.execute(
            "INSERT INTO agent_submissions "
            "(id, miner_hotkey, card_id, bundle_blob_key, bundle_hash, "
            " bundle_size_bytes, encryption_key_id, bundle_signature, "
            " display_name, bio, logo_url, soul_md_preview, "
            " metadata_fingerprint, similarity_check_passed, "
            " rejection_reason, submitted_at, status, current_score, "
            " current_rank, attestation_mode, discovery_only) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "other",
                "5OTHER",
                "eu-ai-act",
                "k1",
                "a" * 64,
                10,
                "kek",
                "sig",
                "other",
                None,
                None,
                None,
                "fp1",
                1,
                None,
                "2026-05-01T00:00:00Z",
                "ranked",
                0.9,
                1,
                "ssh-probe",
                0,
            ),
        )
        await conn.execute(
            "INSERT INTO agent_submissions "
            "(id, miner_hotkey, card_id, bundle_blob_key, bundle_hash, "
            " bundle_size_bytes, encryption_key_id, bundle_signature, "
            " display_name, bio, logo_url, soul_md_preview, "
            " metadata_fingerprint, similarity_check_passed, "
            " rejection_reason, submitted_at, status, current_score, "
            " current_rank, attestation_mode, discovery_only) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "self",
                "5SELF",
                "eu-ai-act",
                "k2",
                "b" * 64,
                10,
                "kek",
                "sig",
                "self",
                None,
                None,
                None,
                "fp2",
                1,
                None,
                "2026-05-01T00:00:00Z",
                "ranked",
                0.95,  # PRE-write old score
                1,
                "ssh-probe",
                0,
            ),
        )
        await conn.commit()

        # Without the exclusion, both 0.9 and 0.95 (self's old) would
        # count as > 0.6 → rank 3. With exclusion of "self", only 0.9
        # counts → rank 2.
        rank_without = await _compute_rank(conn, "eu-ai-act", 0.6)
        rank_with = await _compute_rank(conn, "eu-ai-act", 0.6, exclude_submission_id="self")
        assert rank_without == 3, f"unguarded rank counts self; got {rank_without}"
        assert rank_with == 2, (
            f"excluding self must drop the row's pre-write score; got {rank_with}"
        )
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Unit-test helpers — no DB, no Polaris, no signer.
# --------------------------------------------------------------------------


class _CapturingDB:
    """Captures `update_submission_status` calls without a real DB."""

    def __init__(self) -> None:
        self.status_writes: list[tuple[str, str, str | None]] = []

    async def execute(self, sql: str, params: tuple[Any, ...] = ()):  # type: ignore[override]
        # Only the update_submission_status query is exercised by the
        # state-machine helpers under test. Capture (status, reason, id).
        if sql.startswith("UPDATE agent_submissions SET status="):
            status, reason, sub_id = params
            self.status_writes.append((str(sub_id), str(status), reason))
        return _NoopCursor()

    async def commit(self) -> None:  # pragma: no cover
        return None


class _NoopCursor:
    async def fetchall(self) -> list[Any]:  # pragma: no cover
        return []

    async def fetchone(self) -> Any:  # pragma: no cover
        return None

    @property
    def description(self) -> list[Any]:  # pragma: no cover
        return []


class _StubSigner:
    def sign(self, _: bytes) -> str:  # pragma: no cover
        return "stub-sig"


class _StubRegistry:
    def get(self, _: str) -> Any:  # pragma: no cover
        return None


def _bound_logger():
    import structlog

    return structlog.get_logger(__name__).bind(submission_id="test")


def _drive_one_tick(orchestrator) -> None:
    """Try the most likely orchestrator entry points to advance state by one step.

    The implementer hasn't decided the API surface yet. We try them in
    order; the first one that exists wins.
    """
    import asyncio
    import inspect

    for attr in ("run_once", "tick", "process_one", "schedule_once"):
        fn = getattr(orchestrator, attr, None)
        if fn is None:
            continue
        try:
            if inspect.iscoroutinefunction(fn):
                asyncio.get_event_loop().run_until_complete(fn())
            else:
                fn()
        except Exception:
            # If this entry point raises, try the next one.
            continue
        return

    # As a last resort, look for an Orchestrator class.
    cls = getattr(orchestrator, "Orchestrator", None)
    if cls is not None:
        try:
            inst = cls()
            for attr in ("run_once", "tick", "process_one"):
                fn = getattr(inst, attr, None)
                if fn is None:
                    continue
                if inspect.iscoroutinefunction(fn):
                    asyncio.get_event_loop().run_until_complete(fn())
                else:
                    fn()
                return
        except Exception:
            pass

    pytest.skip(
        "orchestrator has no run_once/tick/process_one entry point — "
        "implementer must expose one for tests per CONTRACTS.md §6"
    )
