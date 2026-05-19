"""Regression tests for the v2 scorer cleanup rollup (cathedralai/cathedral#156).

Locks the acceptance criteria on issue #156:

- ``_first_mover_multiplier`` is not called on v2 scoring rows. The v1
  similarity-key multiplier is undefined for v2 Polaris-native deploys
  and silently degraded their weighted_score before the rollup.
- v2 rows do not carry the v1 six-dimension breakdown. The stored
  ``score_parts`` is empty so consumers cannot misread an all-zero v1
  payload as a real signal.
- A durable audit row lands BEFORE the main eval transaction so a
  rollback in the downstream score-update path leaves the audit row
  intact. Operators rely on this anchor for post-incident review.
- The audit failure counter increments per swallowed exception class
  when the audit write itself raises. The eval still scores so a
  database hiccup does not blackhole the miner's submission.
- A loud warning + a ``SCORER_MISCONFIG_GAUGE`` entry fire when
  ``CATHEDRAL_SCORER=v2`` is set without its companion config.

The tests intentionally exercise the public-facing
``score_and_sign`` entry point to keep the lock surface small; the
gating and audit decisions are observable from the function output
and from the eval_runs / eval_runs_audit tables.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import aiosqlite
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import cathedral.publisher.app  # noqa: F401  pre-warm publisher import path

# --------------------------------------------------------------------------
# Shared fixtures (mirror the layout in test_polaris_runtime_orchestrator_wiring)
# --------------------------------------------------------------------------


@pytest.fixture
async def conn() -> Any:
    from cathedral.validator.db import connect

    c = await connect(":memory:")
    yield c
    await c.close()


@pytest.fixture
def signer() -> Any:
    from cathedral.eval.scoring_pipeline import EvalSigner

    sk = Ed25519PrivateKey.generate()
    return EvalSigner(sk)


@pytest.fixture
def registry() -> Any:
    from cathedral.cards.registry import CardRegistry

    return CardRegistry.baseline()


@pytest.fixture(autouse=True)
def _reset_scorer_metrics() -> Any:
    from cathedral.eval.scorer_metrics import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


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
        "display_name": f"miner-{hotkey_seed}",
        "metadata_fingerprint": metadata_fp,
        "submitted_at": submitted_at,
        "first_mover_at": submitted_at,
    }


class _FakePublishedArtifact:
    """Minimum surface ``score_and_sign`` reads off a PublishedArtifact."""

    def __init__(self, *, manifest_hash: str, bundle_url: str, manifest_url: str) -> None:
        self.manifest_hash = manifest_hash
        self.bundle_url = bundle_url
        self.manifest_url = manifest_url


# --------------------------------------------------------------------------
# Acceptance 1: _first_mover_multiplier is not called on v2 scoring
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_path_skips_first_mover_multiplier(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v2 rows must not invoke ``_first_mover_multiplier``. The v1
    multiplier reads the metadata_fingerprint similarity key, which v2
    Polaris-native deploys do not produce meaningfully — calling it
    silently penalised v2 weighted_score before #156."""
    from cathedral.eval import scoring_pipeline
    from cathedral.eval.scoring_pipeline import score_and_sign

    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    call_count = {"n": 0}
    real_multiplier = scoring_pipeline._first_mover_multiplier

    async def _spy(*args: Any, **kwargs: Any) -> float:
        call_count["n"] += 1
        return await real_multiplier(*args, **kwargs)

    monkeypatch.setattr(scoring_pipeline, "_first_mover_multiplier", _spy)

    sub = await _insert_minimal_submission(conn, hotkey_seed="v2skip")
    artifact = _FakePublishedArtifact(
        manifest_hash="m" * 64,
        bundle_url="hippius://bundle/v2skip",
        manifest_url="hippius://manifest/v2skip",
    )
    result = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_v2",
        polaris_run_id="rid-v2skip",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=_valid_card_dict(),
        duration_ms=10,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        published_artifact=artifact,
    )

    assert call_count["n"] == 0, "v2 path must not call _first_mover_multiplier"
    assert result.multiplier == 1.0
    # The v2 row's weighted score is the raw preflight+scorer output;
    # without the v1 multiplier in the loop the eval still scores.
    cur = await conn.execute(
        "SELECT eval_output_schema_version FROM eval_runs WHERE id = ?",
        (result.eval_run_id,),
    )
    row = await cur.fetchone()
    assert row is not None and row[0] == 2


@pytest.mark.asyncio
async def test_v1_path_still_calls_first_mover_multiplier(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v1 rows must keep the first-mover delta. The rollup gate is
    additive: it must not regress the legacy path."""
    from cathedral.eval import scoring_pipeline
    from cathedral.eval.scoring_pipeline import score_and_sign

    monkeypatch.delenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", raising=False)

    call_count = {"n": 0}
    real_multiplier = scoring_pipeline._first_mover_multiplier

    async def _spy(*args: Any, **kwargs: Any) -> float:
        call_count["n"] += 1
        return await real_multiplier(*args, **kwargs)

    monkeypatch.setattr(scoring_pipeline, "_first_mover_multiplier", _spy)

    sub = await _insert_minimal_submission(conn, hotkey_seed="v1keep")
    await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_v1",
        polaris_run_id="rid-v1keep",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=_valid_card_dict(),
        duration_ms=10,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        published_artifact=None,
    )

    assert call_count["n"] == 1, "v1 path must still invoke the first-mover delta"


# --------------------------------------------------------------------------
# Acceptance 2: v2 rows do not carry misleading v1 score dimensions
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_row_score_parts_omits_v1_dimensions(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted ``score_parts`` for a v2 row must not advertise the
    six v1 dimensions, even with all-zero values — consumers reading
    those zeros could mistake them for a real per-dimension breakdown
    the v2 lane never produced."""
    from cathedral.eval.scoring_pipeline import score_and_sign

    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    sub = await _insert_minimal_submission(conn, hotkey_seed="v2dims")
    artifact = _FakePublishedArtifact(
        manifest_hash="d" * 64,
        bundle_url="hippius://bundle/v2dims",
        manifest_url="hippius://manifest/v2dims",
    )

    # Drive the early-fail path so the score_dict stays at its default.
    # An invalid card forces preflight/validation to bail; the v1 path
    # would persist the zero-valued six-dimension dict.
    bad_card = {"id": "eu-ai-act"}  # missing required fields
    result = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_v2dims",
        polaris_run_id="rid-v2dims",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=bad_card,
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        published_artifact=artifact,
    )

    cur = await conn.execute(
        "SELECT score_parts, eval_output_schema_version FROM eval_runs WHERE id = ?",
        (result.eval_run_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[1] == 2
    score_parts = json.loads(row[0])
    v1_dims = {"source_quality", "freshness", "specificity", "usefulness", "clarity", "maintenance"}
    assert v1_dims.isdisjoint(score_parts.keys()), (
        f"v2 row leaked v1 dimensions: {sorted(set(score_parts) & v1_dims)}"
    )


@pytest.mark.asyncio
async def test_v1_row_keeps_six_dimension_breakdown(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v1 path must keep the six dimensions; the v2-cleanup gate is
    additive and must not regress existing consumers."""
    from cathedral.eval.scoring_pipeline import score_and_sign

    monkeypatch.delenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", raising=False)

    sub = await _insert_minimal_submission(conn, hotkey_seed="v1dims")
    result = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_v1dims",
        polaris_run_id="rid-v1dims",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=_valid_card_dict(),
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        published_artifact=None,
    )

    cur = await conn.execute(
        "SELECT score_parts FROM eval_runs WHERE id = ?",
        (result.eval_run_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    parts = json.loads(row[0])
    for dim in (
        "source_quality",
        "freshness",
        "specificity",
        "usefulness",
        "clarity",
        "maintenance",
    ):
        assert dim in parts, f"v1 path dropped {dim}"


# --------------------------------------------------------------------------
# Acceptance 3: durable v2 audit row survives a downstream rollback
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v2_audit_row_survives_score_update_rollback(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit row is the post-incident anchor. Even when scoring
    work rolls back, the durable record of what cathedral attempted to
    score must remain in the audit table."""
    from cathedral.eval.scoring_pipeline import score_and_sign
    from cathedral.publisher import repository

    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    sub = await _insert_minimal_submission(conn, hotkey_seed="audit-rollback")
    artifact = _FakePublishedArtifact(
        manifest_hash="a" * 64,
        bundle_url="hippius://bundle/audit-rb",
        manifest_url="hippius://manifest/audit-rb",
    )

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
            polaris_agent_id="polaris-runtime:dep_audit",
            polaris_run_id="rid-audit",
            task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
            output_card_json=_valid_card_dict(),
            duration_ms=1,
            polaris_errors=[],
            registry=registry,
            signer=signer,
            published_artifact=artifact,
        )

    # No eval_runs row (scoring rolled back as expected).
    cur = await conn.execute(
        "SELECT COUNT(*) FROM eval_runs WHERE submission_id = ?",
        (sub["id"],),
    )
    assert int((await cur.fetchone())[0]) == 0

    # The audit row IS present — durable across the rollback.
    cur2 = await conn.execute(
        "SELECT manifest_hash, bundle_url, schema_version "
        "FROM eval_runs_audit WHERE submission_id = ?",
        (sub["id"],),
    )
    row = await cur2.fetchone()
    assert row is not None, "audit row must survive scoring rollback"
    assert row[0] == "a" * 64
    assert row[1] == "hippius://bundle/audit-rb"
    assert int(row[2]) == 2


# --------------------------------------------------------------------------
# Acceptance 4: audit failure increments the counter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_failure_bumps_counter_per_exception_class(
    conn: aiosqlite.Connection,
    signer: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swallowed audit exception must bump
    ``AUDIT_FAILURE_COUNTER[exception_class]`` so operators see the
    cause. Scoring keeps going so a database hiccup does not blackhole
    the miner's submission."""
    from cathedral.eval import scorer_metrics
    from cathedral.eval.scoring_pipeline import score_and_sign
    from cathedral.publisher import repository

    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    sub = await _insert_minimal_submission(conn, hotkey_seed="audit-fail")
    artifact = _FakePublishedArtifact(
        manifest_hash="f" * 64,
        bundle_url="hippius://bundle/audit-fail",
        manifest_url="hippius://manifest/audit-fail",
    )

    class _SyntheticAuditError(RuntimeError):
        pass

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise _SyntheticAuditError("audit write blew up")

    monkeypatch.setattr(repository, "record_v2_audit", _boom)

    result = await score_and_sign(
        conn,
        submission=sub,
        epoch=1,
        round_index=0,
        polaris_agent_id="polaris-runtime:dep_audit_fail",
        polaris_run_id="rid-audit-fail",
        task_json={"card_id": "eu-ai-act", "epoch": 1, "round_index": 0},
        output_card_json=_valid_card_dict(),
        duration_ms=1,
        polaris_errors=[],
        registry=registry,
        signer=signer,
        published_artifact=artifact,
    )

    assert scorer_metrics.AUDIT_FAILURE_COUNTER.get("_SyntheticAuditError") == 1, (
        "audit failure counter must be keyed by exception class name"
    )
    # The eval still scored — degraded-mode operation is the documented
    # behaviour for a swallowed audit failure.
    cur = await conn.execute(
        "SELECT COUNT(*) FROM eval_runs WHERE id = ?",
        (result.eval_run_id,),
    )
    assert int((await cur.fetchone())[0]) == 1


# --------------------------------------------------------------------------
# Acceptance 5: v2 scorer misconfiguration is loud + sets the gauge
# --------------------------------------------------------------------------


def test_check_v2_scorer_activation_warns_and_sets_gauge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CATHEDRAL_SCORER=v2`` without the companion flag must emit a
    loud warning AND bump ``SCORER_MISCONFIG_GAUGE`` so dashboards can
    alert without waiting for the first eval to hit the per-call
    probe."""
    from cathedral.eval import scorer_metrics, scoring_pipeline
    from cathedral.eval.scoring_pipeline import check_v2_scorer_activation

    captured: list[tuple[str, str, dict[str, Any]]] = []

    class _StubLogger:
        def warning(self, event: str, **kwargs: Any) -> None:
            captured.append(("warning", event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            captured.append(("info", event, kwargs))

    monkeypatch.setattr(scoring_pipeline, "logger", _StubLogger())

    monkeypatch.setenv("CATHEDRAL_SCORER", "v2")
    monkeypatch.delenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", raising=False)
    missing = check_v2_scorer_activation()

    assert "CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD" in missing
    assert scorer_metrics.SCORER_MISCONFIG_GAUGE.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD") == 1
    # Operator should be able to grep the warning by name.
    assert any(event == "scorer_v2_misconfigured" for _level, event, _ in captured), (
        f"expected a scorer_v2_misconfigured warning, got {captured}"
    )


def test_check_v2_scorer_activation_clears_gauge_when_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the operator fixes the companion flag, the next startup
    probe must clear the gauge so the alert resolves automatically."""
    from cathedral.eval import scorer_metrics
    from cathedral.eval.scoring_pipeline import check_v2_scorer_activation

    monkeypatch.setenv("CATHEDRAL_SCORER", "v2")
    monkeypatch.setenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", "true")

    missing = check_v2_scorer_activation()
    assert missing == []
    assert scorer_metrics.SCORER_MISCONFIG_GAUGE.get("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD") == 0


def test_check_v2_scorer_activation_noop_when_scorer_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators who do not opt into v2 must not see the gauge fire.
    The probe is silent for the legacy v1 path."""
    from cathedral.eval import scorer_metrics
    from cathedral.eval.scoring_pipeline import check_v2_scorer_activation

    monkeypatch.delenv("CATHEDRAL_SCORER", raising=False)
    monkeypatch.delenv("CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD", raising=False)

    missing = check_v2_scorer_activation()
    assert missing == []
    for value in scorer_metrics.SCORER_MISCONFIG_GAUGE.values():
        assert value == 0
