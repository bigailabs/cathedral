"""Orchestrator + scoring-pipeline wiring tests for the Polaris-runtime path.

These tests pin the integration glue between `PolarisRuntimeRunner`
and the rest of the eval pipeline:

1. `_resolve_polaris_runner_from_env` returns `PolarisRuntimeRunner`
   when `CATHEDRAL_EVAL_MODE=polaris`.
2. A valid attestation flowing through `score_and_sign` flips the
   `polaris_verified` flag on the persisted `eval_runs` row.
3. The persisted attestation JSON round-trips through the public
   `EvalOutput` projection so downstream verifiers can re-check the
   Ed25519 signature without re-running the eval.
4. The 1.10x quality multiplier per CONTRACTS §7.3 applies when a
   non-None attestation is supplied (Tier A) and is omitted when the
   runner returns no attestation (BYO-compute Tier B).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Pre-warm the publisher import path so the circular import between
# `cathedral.eval.orchestrator` and `cathedral.publisher.app` resolves
# in the same order it does at production startup. Tests that touch
# orchestrator-level functions must do this before the first
# `from cathedral.eval.orchestrator import ...` call.
import cathedral.publisher.app  # noqa: F401  pre-warm

# --------------------------------------------------------------------------
# Test 1 — env-mode dispatch
# --------------------------------------------------------------------------


def test_resolve_polaris_runner_from_env_polaris_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CATHEDRAL_EVAL_MODE=polaris` -> `PolarisRuntimeRunner` is returned."""
    from cathedral.eval.orchestrator import _resolve_polaris_runner_from_env
    from cathedral.eval.polaris_runner import (
        PolarisRunnerError,
        PolarisRuntimeRunner,
    )

    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "polaris")
    monkeypatch.setenv("POLARIS_CATHEDRAL_RUNTIME_SUBMISSION_ID", "sub_cathedral_runtime_v1")
    monkeypatch.setenv("POLARIS_API_TOKEN", "test-token")
    # Provide a 32-byte hex Ed25519 public key.
    public_key_hex = "11" * 32
    monkeypatch.setenv("POLARIS_ATTESTATION_PUBLIC_KEY", public_key_hex)

    # The Polaris-mode resolver needs the publisher ctx for the HippiusClient.
    # Stub one with the minimum surface used by HippiusPresignedUrlResolver.
    class _StubCtx:
        def __init__(self) -> None:
            class _StubHippius:
                def presigned_get_url(self, key: str, *, expires_in: int = 3600) -> str:
                    return f"https://r2.example.invalid/{key}?exp={expires_in}"

            self.hippius = _StubHippius()
            self.db = None  # not used by dispatch
            self.signer = None
            self.registry = None

    monkeypatch.setattr(
        "cathedral.publisher.app.latest_ctx",
        lambda: _StubCtx(),
    )

    runner = _resolve_polaris_runner_from_env()
    assert isinstance(runner, PolarisRuntimeRunner)

    # `polaris-runtime` is also accepted as an alias.
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "polaris-runtime")
    runner2 = _resolve_polaris_runner_from_env()
    assert isinstance(runner2, PolarisRuntimeRunner)

    # Missing attestation key -> hard failure (no silent fallback).
    monkeypatch.delenv("POLARIS_ATTESTATION_PUBLIC_KEY")
    with pytest.raises(PolarisRunnerError):
        _resolve_polaris_runner_from_env()


def test_resolve_polaris_runner_other_modes_still_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding the `polaris` mode must not break existing dispatch."""
    from cathedral.eval.orchestrator import _resolve_polaris_runner_from_env
    from cathedral.eval.polaris_runner import (
        BundleCardRunner,
        FailingStubPolarisRunner,
        HttpPolarisRunner,
        MalformedStubPolarisRunner,
        StubPolarisRunner,
    )

    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub")
    assert isinstance(_resolve_polaris_runner_from_env(), StubPolarisRunner)

    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub-fail-polaris")
    assert isinstance(_resolve_polaris_runner_from_env(), FailingStubPolarisRunner)

    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "stub-bad-card")
    assert isinstance(_resolve_polaris_runner_from_env(), MalformedStubPolarisRunner)

    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "bundle")
    assert isinstance(_resolve_polaris_runner_from_env(), BundleCardRunner)

    # Default / legacy: HttpPolarisRunner.
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "http-polaris")
    assert isinstance(_resolve_polaris_runner_from_env(), HttpPolarisRunner)


def test_ssh_probe_v2_invalid_stdout_cap_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Runtime parser must match SAT preflight's warn-and-default behavior."""
    from cathedral.eval.orchestrator import _resolve_polaris_runner_for_mode
    from cathedral.eval.ssh_hermes_runner import (
        DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES,
        SshHermesRunner,
    )

    monkeypatch.setenv("CATHEDRAL_PROBER_VERSION", "v2")
    ssh_key_path = tmp_path / "cathedral_probe_ed25519"
    ssh_key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(ssh_key_path))
    monkeypatch.setenv("CATHEDRAL_BUNDLE_OUTPUT_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("CATHEDRAL_TASK_FAMILY_STDOUT_MAX_BYTES", "8MB")

    runner = _resolve_polaris_runner_for_mode("ssh-probe")

    assert isinstance(runner, SshHermesRunner)
    assert (
        runner.config.task_family_stdout_limit_bytes
        == DEFAULT_TASK_FAMILY_STDOUT_LIMIT_BYTES
    )


def test_ssh_probe_v2_env_dispatch_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Runtime env parsing must accept the same trimmed values as preflight."""
    from cathedral.eval.orchestrator import _resolve_polaris_runner_from_env
    from cathedral.eval.ssh_hermes_runner import SshHermesRunner

    ssh_key_path = tmp_path / "cathedral_probe_ed25519"
    ssh_key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(ssh_key_path))
    monkeypatch.setenv("CATHEDRAL_BUNDLE_OUTPUT_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("CATHEDRAL_EVAL_MODE", "ssh-probe ")
    monkeypatch.setenv("CATHEDRAL_PROBER_VERSION", "v2 ")

    runner = _resolve_polaris_runner_from_env()

    assert isinstance(runner, SshHermesRunner)


# --------------------------------------------------------------------------
# Tests 2+3 — score_and_sign persists attestation + flips polaris_verified
# --------------------------------------------------------------------------


@pytest.fixture
async def conn() -> Any:
    """Fresh aiosqlite connection with the schema applied."""
    from cathedral.validator.db import connect

    c = await connect(":memory:")
    yield c
    await c.close()


@pytest.fixture
def signer() -> Any:
    """In-memory Ed25519 signer matching `EvalSigner`'s contract."""
    from cathedral.eval.scoring_pipeline import EvalSigner

    sk = Ed25519PrivateKey.generate()
    return EvalSigner(sk)


@pytest.fixture
def registry() -> Any:
    from cathedral.cards.registry import CardRegistry

    return CardRegistry.baseline()


def _valid_card_dict() -> dict[str, Any]:
    iso = "2026-05-10T10:00:00.000Z"
    return {
        "id": "eu-ai-act",
        "jurisdiction": "eu",
        "topic": "EU AI Act",
        "worker_owner_hotkey": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "polaris_agent_id": "polaris-runtime:dep_abc",
        "title": "EU AI Act update",
        "summary": "Substantive policy summary.",
        "what_changed": "GPAI obligations live since 2025-08-02.",
        "why_it_matters": "Providers face up to 3% turnover fines.",
        "action_notes": "Map deployments to Annex III categories.",
        "risks": "Penalties phase in alongside obligations.",
        "citations": [
            {
                "url": "https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
                "class": "official_journal",
                "fetched_at": iso,
                "status": 200,
                "content_hash": "a" * 64,
            }
        ],
        "confidence": 0.72,
        "no_legal_advice": True,
        "last_refreshed_at": iso,
        "refresh_cadence_hours": 24,
    }


async def _ensure_card_definition(c: aiosqlite.Connection) -> None:
    """Seed the FK target for `agent_submissions.card_id`."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await c.execute(
        """
        INSERT OR IGNORE INTO card_definitions (
            id, display_name, jurisdiction, topic, description,
            eval_spec_md, source_pool, task_templates, scoring_rubric,
            refresh_cadence_hours, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "eu-ai-act",
            "EU AI Act",
            "eu",
            "ai-regulation",
            "EU AI Act regulatory tracking",
            "# Eval spec",
            json.dumps([]),
            json.dumps([]),
            json.dumps({}),
            24,
            "active",
            now,
            now,
        ),
    )
    await c.commit()


async def _insert_minimal_submission(
    c: aiosqlite.Connection, *, hotkey_seed: str = "alice"
) -> dict[str, Any]:
    """Insert just enough of an `agent_submissions` row to score against.

    Uses `hotkey_seed` to make repeat inserts unique on the
    (miner_hotkey, card_id, bundle_hash) unique-index target.
    """
    await _ensure_card_definition(c)
    submitted_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    sub_id = str(uuid4())
    miner_hotkey = f"5MinerHotkey{hotkey_seed}".ljust(48, "0")
    bundle_hash = f"{hotkey_seed}".ljust(64, "f")
    metadata_fp = f"fp{hotkey_seed}".ljust(64, "0")
    await c.execute(
        """
        INSERT INTO agent_submissions (
            id, miner_hotkey, card_id, bundle_hash, bundle_size_bytes,
            bundle_blob_key, encryption_key_id, bundle_signature,
            display_name, bio, logo_url, soul_md_preview,
            metadata_fingerprint, similarity_check_passed,
            status, submitted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sub_id,
            miner_hotkey,
            "eu-ai-act",
            bundle_hash,
            1024,
            "agents/sub.bin.enc",
            "kek_id_v1",
            "sigsig",
            f"miner-{hotkey_seed}",
            None,
            None,
            None,
            metadata_fp,
            1,
            "evaluating",
            submitted_at,
        ),
    )
    await c.commit()
    return {
        "id": sub_id,
        "miner_hotkey": miner_hotkey,
        "card_id": "eu-ai-act",
        "bundle_hash": bundle_hash,
        "metadata_fingerprint": metadata_fp,
        "display_name": f"miner-{hotkey_seed}",
        "submitted_at": submitted_at,
    }


@pytest.mark.asyncio
async def test_attestation_persists_and_flips_polaris_verified(
    conn: aiosqlite.Connection, signer: Any, registry: Any
) -> None:
    """A non-None attestation must (a) set polaris_verified=true on the row,
    (b) round-trip through the JSON column, and (c) apply the 1.10x quality
    multiplier per CONTRACTS §7.3.
    """
    from cathedral.eval.scoring_pipeline import score_and_sign

    sub = await _insert_minimal_submission(conn, hotkey_seed="alice")

    attestation = {
        "version": "polaris-v1",
        "payload": {
            "submission_id": "sub_cathedral_runtime_v1",
            "task_id": "cathedral-eu-ai-act-e1r0",
            "task_hash": "ab" * 32,
            "output_hash": "cd" * 32,
            "deployment_id": "dep_abc",
            "completed_at": "2026-05-10T10:01:23.456Z",
        },
        "signature": "Zm9vYmFy",  # opaque to score_and_sign
        "public_key": "11" * 32,
    }
    card = _valid_card_dict()

    # Polaris-verified path.
    polaris = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_abc",
        polaris_run_id="cathedral-eu-ai-act-e1r0",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=card,
        duration_ms=1234,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=attestation,
    )
    assert polaris.polaris_verified is True
    assert polaris.polaris_attestation == attestation

    # Read it back from sqlite and confirm the column is hydrated.
    cur = await conn.execute(
        "SELECT polaris_verified, polaris_attestation FROM eval_runs WHERE id = ?",
        (polaris.eval_run_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1
    stored = json.loads(row[1])
    assert stored == attestation

    # BYO-compute baseline — no attestation, no Polaris agent id.
    sub2 = await _insert_minimal_submission(conn, hotkey_seed="bob")
    byo = await score_and_sign(
        conn,
        submission=sub2,
        epoch=1,
        round_index=0,
        polaris_agent_id="",  # BYO-compute path
        polaris_run_id="bundle-eu-ai-act-e1r0",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=card,
        duration_ms=12,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=None,
    )
    assert byo.polaris_verified is False
    assert byo.polaris_attestation is None

    # Since v1.1.0 every scored v1 submission uses the 1.00x runtime
    # multiplier regardless of Polaris-verified vs BYO path; BYO Box
    # (ssh-probe) is the only live mining path. Identical card+registry
    # therefore yields identical weighted_score on both paths. The
    # attestation row is still hydrated (asserted above) so downstream
    # surfaces can show verified vs not, but the score does not diverge.
    assert polaris.weighted_score == byo.weighted_score, (
        f"v1.1.0+: 1.00x multiplier on both paths means scores must match "
        f"(verified={polaris.weighted_score}, byo={byo.weighted_score})"
    )


@pytest.mark.asyncio
async def test_eval_output_projection_carries_attestation(
    conn: aiosqlite.Connection, signer: Any, registry: Any
) -> None:
    """The public EvalOutput projection exposes `polaris_attestation`.

    Validators and the frontend can replay the Ed25519 verification
    without re-running the eval.
    """
    from cathedral.eval.scoring_pipeline import score_and_sign
    from cathedral.publisher.reads import _eval_run_to_output
    from cathedral.publisher.repository import list_eval_runs_for_submission

    sub = await _insert_minimal_submission(conn, hotkey_seed="charlie")
    attestation = {
        "version": "polaris-v1",
        "payload": {
            "submission_id": "sub_x",
            "task_id": "tid",
            "task_hash": "ab" * 32,
            "output_hash": "cd" * 32,
            "deployment_id": "dep",
            "completed_at": "2026-05-10T10:00:00.000Z",
        },
        "signature": "AAAA",
        "public_key": "22" * 32,
    }
    await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep",
        polaris_run_id="rid",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=_valid_card_dict(),
        duration_ms=42,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=attestation,
    )

    runs = await list_eval_runs_for_submission(conn, sub["id"])
    assert len(runs) == 1
    projection = _eval_run_to_output(
        runs[0],
        {
            "id": sub["id"],
            "display_name": sub["display_name"],
            "card_id": sub["card_id"],
        },
    )
    assert projection["polaris_verified"] is True
    assert projection["polaris_attestation"] == attestation


@pytest.mark.asyncio
async def test_score_and_sign_rollbacks_when_submission_score_update_fails(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cathedralai/cathedral#69: eval_runs insert + submission score update
    share one commit. If the score update fails, the eval_run must not
    remain visible (no orphan signed row without matching submission state).
    """
    from cathedral.eval.scoring_pipeline import score_and_sign
    from cathedral.publisher import repository

    sub = await _insert_minimal_submission(conn, hotkey_seed="atomic-rollback")

    cur = await conn.execute(
        "SELECT COUNT(*) FROM eval_runs WHERE submission_id = ?",
        (sub["id"],),
    )
    assert int((await cur.fetchone())[0]) == 0

    real_update = repository.update_submission_score

    async def _raise_on_deferred_commit(
        c: aiosqlite.Connection,
        submission_id: str,
        *,
        current_score: float,
        current_rank: int,
        commit: bool = True,
    ) -> None:
        if not commit:
            raise RuntimeError("simulated score persistence failure")
        await real_update(
            c, submission_id, current_score=current_score, current_rank=current_rank, commit=commit
        )

    monkeypatch.setattr(repository, "update_submission_score", _raise_on_deferred_commit)

    with pytest.raises(RuntimeError, match="simulated score persistence failure"):
        await score_and_sign(
            conn,
            submission=sub,
            epoch=1,
            round_index=0,
            polaris_agent_id="polaris-runtime:dep",
            polaris_run_id="rid-atomic",
            task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
            output_card_json=_valid_card_dict(),
            duration_ms=1,
            polaris_errors=[],
            registry=registry,
            signer=signer,
            polaris_attestation=None,
        )

    cur2 = await conn.execute(
        "SELECT COUNT(*) FROM eval_runs WHERE submission_id = ?",
        (sub["id"],),
    )
    assert int((await cur2.fetchone())[0]) == 0


@pytest.mark.asyncio
async def test_zero_all_scores_killswitch_forces_weighted_zero(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator kill switch: CATHEDRAL_ZERO_ALL_SCORES=true must force every
    signed row's weighted_score to 0.0 regardless of what the scorer
    computed. This is the publisher-side equivalent of 100% burn,
    effective without any validator update because all rolling averages
    collapse to zero and chain/weights.apply_burn routes to burn_uid.
    """
    from cathedral.eval.scoring_pipeline import score_and_sign

    sub = await _insert_minimal_submission(conn, hotkey_seed="killswitch")
    card = _valid_card_dict()

    # Baseline: with the env unset, the same card scores nonzero.
    monkeypatch.delenv("CATHEDRAL_ZERO_ALL_SCORES", raising=False)
    baseline = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="",
        polaris_run_id="killswitch-baseline",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=card,
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=None,
    )
    assert baseline.weighted_score > 0.0, (
        "test fixture invariant: the baseline card must produce a positive "
        "score so the kill switch has something to clamp"
    )

    # Engage the kill switch on a fresh submission. weighted_score must be
    # exactly 0.0 even though the scorer would have produced > 0.
    sub2 = await _insert_minimal_submission(conn, hotkey_seed="killswitch_on")
    monkeypatch.setenv("CATHEDRAL_ZERO_ALL_SCORES", "true")
    zeroed = await score_and_sign(
        conn,
        submission=sub2,
        epoch=1,
        round_index=0,
        polaris_agent_id="",
        polaris_run_id="killswitch-zeroed",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=card,
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=None,
    )
    assert zeroed.weighted_score == 0.0
    assert zeroed.weighted_score_pre_multiplier == 0.0

    # Read back from sqlite: the persisted row must also be zero so
    # validators pulling the row see weighted_score=0.
    cur = await conn.execute(
        "SELECT weighted_score FROM eval_runs WHERE id = ?",
        (zeroed.eval_run_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert float(row[0]) == 0.0

    # Flip back off: a fresh submission scores normally again, proving
    # the switch is fully reversible without a code change.
    sub3 = await _insert_minimal_submission(conn, hotkey_seed="killswitch_off")
    monkeypatch.setenv("CATHEDRAL_ZERO_ALL_SCORES", "false")
    restored = await score_and_sign(
        conn,
        submission=sub3,
        epoch=1,
        round_index=0,
        polaris_agent_id="",
        polaris_run_id="killswitch-restored",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=card,
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        polaris_attestation=None,
    )
    assert restored.weighted_score > 0.0
