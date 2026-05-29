"""Sqlite reads/writes for v1 launch tables.

Wraps the raw aiosqlite calls used by submit/reads/eval/merkle into
typed helpers. Keeps the SQL in one place so the per-table indexes
(CONTRACTS.md Section 3) stay obvious.

The publisher is the SOLE writer to `card_definitions`,
`agent_submissions`, `eval_runs`, and `merkle_anchors`. The validator
binary only reads from these via the publisher's HTTP API; it never
opens the publisher's sqlite file.
"""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

# --------------------------------------------------------------------------
# Card definitions
# --------------------------------------------------------------------------


async def insert_card_definition(
    conn: aiosqlite.Connection,
    *,
    id: str,
    display_name: str,
    jurisdiction: str,
    topic: str,
    description: str,
    eval_spec_md: str,
    source_pool: list[dict[str, Any]],
    task_templates: list[str],
    scoring_rubric: dict[str, Any],
    refresh_cadence_hours: int = 24,
    status: str = "active",
) -> None:
    now = datetime.now(UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO card_definitions (
            id, display_name, jurisdiction, topic, description,
            eval_spec_md, source_pool, task_templates, scoring_rubric,
            refresh_cadence_hours, status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            display_name=excluded.display_name,
            jurisdiction=excluded.jurisdiction,
            topic=excluded.topic,
            description=excluded.description,
            eval_spec_md=excluded.eval_spec_md,
            source_pool=excluded.source_pool,
            task_templates=excluded.task_templates,
            scoring_rubric=excluded.scoring_rubric,
            refresh_cadence_hours=excluded.refresh_cadence_hours,
            status=excluded.status,
            updated_at=excluded.updated_at
        """,
        (
            id,
            display_name,
            jurisdiction,
            topic,
            description,
            eval_spec_md,
            json.dumps(source_pool),
            json.dumps(task_templates),
            json.dumps(scoring_rubric),
            refresh_cadence_hours,
            status,
            now,
            now,
        ),
    )
    await conn.commit()


async def get_card_definition(conn: aiosqlite.Connection, card_id: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        """
        SELECT id, display_name, jurisdiction, topic, description,
               eval_spec_md, source_pool, task_templates, scoring_rubric,
               refresh_cadence_hours, status, created_at, updated_at
        FROM card_definitions WHERE id = ?
        """,
        (card_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_card_def(row)


async def set_card_definition_status(
    conn: aiosqlite.Connection,
    *,
    card_id: str,
    status: str,
) -> bool:
    """Update a card_definitions row's status. Returns True if a row was
    updated, False if no matching row existed. Idempotent.

    Used by `cathedral-publisher archive-cards` to mark deprecated cards
    so submit and eval-spec endpoints return 404. Does not commit; the
    caller is responsible for the transaction boundary.
    """
    cur = await conn.execute(
        "UPDATE card_definitions SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (status, card_id),
    )
    return (cur.rowcount or 0) > 0


async def list_card_definitions(
    conn: aiosqlite.Connection, *, only_active: bool = True
) -> list[dict[str, Any]]:
    if only_active:
        cur = await conn.execute(
            "SELECT id, display_name, jurisdiction, topic, description, "
            "eval_spec_md, source_pool, task_templates, scoring_rubric, "
            "refresh_cadence_hours, status, created_at, updated_at "
            "FROM card_definitions WHERE status='active'"
        )
    else:
        cur = await conn.execute(
            "SELECT id, display_name, jurisdiction, topic, description, "
            "eval_spec_md, source_pool, task_templates, scoring_rubric, "
            "refresh_cadence_hours, status, created_at, updated_at "
            "FROM card_definitions"
        )
    rows = await cur.fetchall()
    return [_row_to_card_def(r) for r in rows]


def _row_to_card_def(row: aiosqlite.Row | tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "display_name": row[1],
        "jurisdiction": row[2],
        "topic": row[3],
        "description": row[4],
        "eval_spec_md": row[5],
        "source_pool": json.loads(row[6]),
        "task_templates": json.loads(row[7]),
        "scoring_rubric": json.loads(row[8]),
        "refresh_cadence_hours": int(row[9]),
        "status": row[10],
        "created_at": row[11],
        "updated_at": row[12],
    }


# --------------------------------------------------------------------------
# Agent submissions
# --------------------------------------------------------------------------


async def insert_agent_submission(
    conn: aiosqlite.Connection,
    *,
    id: str,
    miner_hotkey: str,
    card_id: str,
    bundle_blob_key: str,
    bundle_hash: str,
    bundle_size_bytes: int,
    encryption_key_id: str,
    bundle_signature: str,
    display_name: str,
    bio: str | None,
    logo_url: str | None,
    soul_md_preview: str | None,
    metadata_fingerprint: str,
    similarity_check_passed: bool,
    rejection_reason: str | None,
    status: str,
    submitted_at: datetime,
    first_mover_at: datetime | None,
    submitted_at_iso: str | None = None,
    attestation_mode: str = "polaris",
    attestation_type: str | None = None,
    attestation_blob: bytes | None = None,
    attestation_verified_at: datetime | None = None,
    discovery_only: bool = False,
    ssh_host: str | None = None,
    ssh_port: int | None = None,
    ssh_user: str | None = None,
    hermes_port: int | None = None,
) -> None:
    """Insert an `agent_submissions` row.

    `submitted_at_iso` is the canonical wire-format timestamp (ms precision,
    trailing 'Z'). When omitted, derived from `submitted_at` (and may carry
    a `+00:00` offset rather than `Z`).

    The attestation fields default to the back-compat ``polaris`` mode so
    existing call sites are unaffected. Callers explicitly write
    ``attestation_mode='tee'`` (+ blob / type / verified_at) when a
    miner submitted a TEE attestation, and ``attestation_mode='unverified'``
    + ``discovery_only=True`` for discovery submissions.

    The ssh_* fields are only populated when ``attestation_mode='ssh-probe'``
    (v2 free tier). They tell the SshProbeRunner how to reach the miner's
    long-lived Hermes process. Nullable for every other mode.
    """
    await conn.execute(
        """
        INSERT INTO agent_submissions (
            id, miner_hotkey, card_id, bundle_blob_key, bundle_hash,
            bundle_size_bytes, encryption_key_id, bundle_signature,
            display_name, bio, logo_url, soul_md_preview,
            metadata_fingerprint, similarity_check_passed,
            rejection_reason, submitted_at, status, first_mover_at,
            attestation_mode, attestation_type, attestation_blob,
            attestation_verified_at, discovery_only,
            ssh_host, ssh_port, ssh_user, hermes_port
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            id,
            miner_hotkey,
            card_id,
            bundle_blob_key,
            bundle_hash,
            bundle_size_bytes,
            encryption_key_id,
            bundle_signature,
            display_name,
            bio,
            logo_url,
            soul_md_preview,
            metadata_fingerprint,
            1 if similarity_check_passed else 0,
            rejection_reason,
            submitted_at_iso or _to_z(submitted_at),
            status,
            _to_z(first_mover_at) if first_mover_at else None,
            attestation_mode,
            attestation_type,
            attestation_blob,
            _to_z(attestation_verified_at) if attestation_verified_at else None,
            1 if discovery_only else 0,
            ssh_host,
            ssh_port,
            ssh_user,
            hermes_port,
        ),
    )
    await conn.commit()


def _to_z(dt: datetime) -> str:
    """Render a datetime as ISO-8601 UTC with trailing 'Z' (CONTRACTS.md §9 lock #6)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


async def get_agent_submission(
    conn: aiosqlite.Connection, submission_id: str
) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM agent_submissions WHERE id = ?", (submission_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_submission(row, cur.description)


async def list_submissions_by_hotkey(
    conn: aiosqlite.Connection, hotkey: str
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM agent_submissions WHERE miner_hotkey = ? ORDER BY submitted_at DESC",
        (hotkey,),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


async def list_submissions_for_card(
    conn: aiosqlite.Connection,
    card_id: str,
    *,
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
    verified_only: bool = True,
    ranked_only: bool = True,
) -> list[dict[str, Any]]:
    """List submissions for a card.

    `verified_only=True` (default) restricts to attested attestation
    modes and excludes discovery rows. The public read endpoints all
    rely on this filter so unverified submissions never appear on the
    leaderboard, card overview, or recent-eval feeds.

    `ranked_only=True` (default) further restricts to ``status='ranked'``
    so cadence-refresh rows that briefly transition (or used to transition
    pre-state-machine-fix) cannot leak onto scored surfaces with stale
    scores. Pre-fix, a cadence row would flip to 'evaluating' on the
    refresh tick while still carrying its prior `current_score`; the
    leaderboard would surface that score with no fresh eval underneath
    it. With ``ranked_only=True`` the row is only surfaced once
    `score_and_sign` has committed a fresh value (which re-asserts
    status='ranked').

    Each returned row carries a synthetic `latest_eval_at` column —
    ``MAX(eval_runs.ran_at)`` for the submission, falling back to
    ``NULL`` when no eval_runs row exists yet. Scored surfaces should
    prefer this over `submitted_at` for the leaderboard's last-active
    field.
    """
    if sort == "score":
        order = "sub.current_score DESC NULLS LAST, sub.submitted_at DESC"
    elif sort == "recent":
        order = "sub.submitted_at DESC"
    elif sort == "oldest":
        order = "sub.submitted_at ASC"
    else:
        raise ValueError(f"invalid sort: {sort}")
    status_clause = (
        "sub.status = 'ranked'" if ranked_only else "sub.status IN ('queued','evaluating','ranked')"
    )
    if verified_only:
        where = (
            f"WHERE sub.card_id = ? AND {status_clause} "
            f"AND sub.attestation_mode IN "
            f"('polaris','polaris-deploy','ssh-probe','tee','bundle') "
            f"AND sub.discovery_only = 0"
        )
    else:
        where = f"WHERE sub.card_id = ? AND {status_clause}"
    cur = await conn.execute(
        f"SELECT sub.*, MAX(er.ran_at) AS latest_eval_at "
        f"FROM agent_submissions sub "
        f"LEFT JOIN eval_runs er ON er.submission_id = sub.id "
        f"{where} "
        f"GROUP BY sub.id "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        (card_id, limit, offset),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


async def list_submissions_all(
    conn: aiosqlite.Connection,
    *,
    sort: str = "score",
    limit: int = 50,
    offset: int = 0,
    verified_only: bool = True,
    ranked_only: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """List all submissions across cards (paginated, with total).

    Same verified_only + ranked_only semantics as
    `list_submissions_for_card`: defaults to the public scored surface
    (attested + ranked). Each row carries a synthetic `latest_eval_at`
    column from ``MAX(eval_runs.ran_at)``.
    """
    if sort == "score":
        order = "sub.current_score DESC NULLS LAST, sub.submitted_at DESC"
    elif sort == "recent":
        order = "sub.submitted_at DESC"
    elif sort == "oldest":
        order = "sub.submitted_at ASC"
    else:
        raise ValueError(f"invalid sort: {sort}")
    status_clause = (
        "sub.status = 'ranked'" if ranked_only else "sub.status IN ('queued','evaluating','ranked')"
    )
    if verified_only:
        where = (
            f"WHERE {status_clause} "
            "AND sub.attestation_mode IN "
            "('polaris','polaris-deploy','ssh-probe','tee','bundle') "
            "AND sub.discovery_only = 0"
        )
    else:
        where = f"WHERE {status_clause}"
    cur = await conn.execute(
        f"SELECT sub.*, MAX(er.ran_at) AS latest_eval_at "
        f"FROM agent_submissions sub "
        f"LEFT JOIN eval_runs er ON er.submission_id = sub.id "
        f"{where} "
        f"GROUP BY sub.id "
        f"ORDER BY {order} LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cur.fetchall()
    # COUNT(*) needs the same WHERE as the SELECT but no join — distinct
    # submissions only, since the join would multiply rows.
    # Rewrite ``sub.``-prefixed where to the plain-table form for the
    # COUNT, which does not join.
    where_no_alias = where.replace("sub.", "")
    cur2 = await conn.execute(f"SELECT COUNT(*) FROM agent_submissions {where_no_alias}")
    total_row = await cur2.fetchone()
    total = int(total_row[0]) if total_row else 0
    return [_row_to_submission(r, cur.description) for r in rows], total


# --------------------------------------------------------------------------
# Discovery surface (unverified submissions)
# --------------------------------------------------------------------------


async def list_discovery_submissions_for_card(
    conn: aiosqlite.Connection,
    card_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List unverified / discovery-only submissions for a card.

    The inverse of the verified leaderboard surface: only rows with
    ``attestation_mode='unverified'`` and ``status='discovery'``. Ordered
    by ``submitted_at DESC`` because discovery rows carry no score.
    """
    cur = await conn.execute(
        "SELECT * FROM agent_submissions WHERE card_id = ? "
        "AND attestation_mode = 'unverified' AND status = 'discovery' "
        "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
        (card_id, limit, offset),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


async def count_discovery_submissions_for_card(conn: aiosqlite.Connection, card_id: str) -> int:
    cur = await conn.execute(
        "SELECT COUNT(*) FROM agent_submissions WHERE card_id = ? "
        "AND attestation_mode = 'unverified' AND status = 'discovery'",
        (card_id,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def list_discovery_submissions_recent(
    conn: aiosqlite.Connection,
    *,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Cross-card discovery feed for the top-level /research page."""
    cur = await conn.execute(
        "SELECT * FROM agent_submissions "
        "WHERE attestation_mode = 'unverified' AND status = 'discovery' "
        "ORDER BY submitted_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


async def count_verified_agents_for_card(conn: aiosqlite.Connection, card_id: str) -> int:
    """Count verified (non-discovery, attested) submissions for a card.

    Used by `/v1/cards/{card_id}` `agent_count` so the public card overview
    reflects only verified agents — the same surface the leaderboard does.
    """
    cur = await conn.execute(
        "SELECT COUNT(*) FROM agent_submissions WHERE card_id = ? "
        "AND status IN ('queued','evaluating','ranked') "
        "AND attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle') "
        "AND discovery_only = 0",
        (card_id,),
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def repair_stale_evaluating_rows(conn: aiosqlite.Connection) -> int:
    """Restore rows stranded in ``status='evaluating'`` with a prior score.

    Pre-PR #117 the orchestrator flipped every selected row to
    ``evaluating`` before calling the runner, including previously
    ranked cadence-refresh rows. A retryable failure or process crash
    would leave the row stuck in ``evaluating`` while still carrying
    the previous round's ``current_score``. The PR #117 read-model
    filter drops those rows from public scored surfaces, which prevents
    future leakage but also hides any existing stranded rows — a
    previously ranked agent silently disappears off the leaderboard
    after deploy.

    This repair re-asserts ``status='ranked'`` on any row that carries
    a non-null ``current_score`` while sitting in ``evaluating``. Such
    a row proves a prior round committed a score; the next cadence tick
    will pick it up again and either commit a fresh score (keeping it
    ranked) or signal degradation through the rolling-average path.

    Idempotent and self-disabling — once the live data is clean, the
    UPDATE matches zero rows. Returns the number of rows repaired so
    the caller can log it.
    """
    cur = await conn.execute(
        "UPDATE agent_submissions SET status='ranked' "
        "WHERE status='evaluating' AND current_score IS NOT NULL"
    )
    await conn.commit()
    return int(cur.rowcount or 0)


async def update_submission_status(
    conn: aiosqlite.Connection,
    submission_id: str,
    *,
    status: str,
    rejection_reason: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE agent_submissions SET status=?, rejection_reason=COALESCE(?, rejection_reason) "
        "WHERE id=?",
        (status, rejection_reason, submission_id),
    )
    await conn.commit()


async def update_submission_score(
    conn: aiosqlite.Connection,
    submission_id: str,
    *,
    current_score: float,
    current_rank: int,
    commit: bool = True,
) -> None:
    await conn.execute(
        "UPDATE agent_submissions SET current_score=?, current_rank=?, status='ranked' WHERE id=?",
        (current_score, current_rank, submission_id),
    )
    if commit:
        await conn.commit()


async def find_existing_bundle_hash(
    conn: aiosqlite.Connection, card_id: str, bundle_hash: str
) -> dict[str, Any] | None:
    """Section 7.1 check #1: same bundle for same card by ANY hotkey."""
    cur = await conn.execute(
        "SELECT * FROM agent_submissions "
        "WHERE card_id = ? AND bundle_hash = ? AND status != 'withdrawn' LIMIT 1",
        (card_id, bundle_hash),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_submission(row, cur.description)


async def find_metadata_fingerprint_collision(
    conn: aiosqlite.Connection,
    card_id: str,
    metadata_fingerprint: str,
    miner_hotkey: str,
) -> dict[str, Any] | None:
    """Section 7.1 check #4: fingerprint clash from a DIFFERENT hotkey."""
    cur = await conn.execute(
        "SELECT * FROM agent_submissions "
        "WHERE card_id = ? AND metadata_fingerprint = ? "
        "AND miner_hotkey != ? AND status != 'withdrawn' LIMIT 1",
        (card_id, metadata_fingerprint, miner_hotkey),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_submission(row, cur.description)


async def list_recent_display_names(
    conn: aiosqlite.Connection, card_id: str, since: datetime
) -> list[tuple[str, str]]:
    """Returns `(submission_id, display_name)` for fuzzy collision check."""
    cur = await conn.execute(
        "SELECT id, display_name FROM agent_submissions "
        "WHERE card_id = ? AND submitted_at >= ? "
        "AND status IN ('queued','evaluating','ranked')",
        (card_id, since.isoformat()),
    )
    rows = await cur.fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


async def list_recent_display_names_excluding_hotkey(
    conn: aiosqlite.Connection,
    card_id: str,
    since: datetime,
    exclude_hotkey: str,
) -> list[tuple[str, str]]:
    """Returns `(submission_id, display_name)` for the fuzzy collision check,
    skipping rows that belong to ``exclude_hotkey``.

    The fuzzy display_name dedupe is meant to stop OTHER miners from
    squatting an existing display_name within the 7-day window. A miner
    resubmitting under their own hotkey should not be blocked by their
    own prior submissions — the same-hotkey + same-bundle dedupe is
    already enforced by the UNIQUE index on (miner_hotkey, bundle_hash).
    """
    cur = await conn.execute(
        "SELECT id, display_name FROM agent_submissions "
        "WHERE card_id = ? AND submitted_at >= ? "
        "AND miner_hotkey != ? "
        "AND status IN ('queued','evaluating','ranked')",
        (card_id, since.isoformat(), exclude_hotkey),
    )
    rows = await cur.fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


async def first_mover_for_fingerprint(
    conn: aiosqlite.Connection, card_id: str, metadata_fingerprint: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM agent_submissions "
        "WHERE card_id = ? AND metadata_fingerprint = ? "
        "AND first_mover_at IS NOT NULL "
        "ORDER BY first_mover_at ASC LIMIT 1",
        (card_id, metadata_fingerprint),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_submission(row, cur.description)


def _row_to_submission(
    row: aiosqlite.Row | tuple[Any, ...],
    description: list[tuple[Any, ...]] | Any,
) -> dict[str, Any]:
    cols = [d[0] for d in description]
    out = dict(zip(cols, row, strict=False))
    if "similarity_check_passed" in out:
        out["similarity_check_passed"] = bool(out["similarity_check_passed"])
    if "discovery_only" in out:
        out["discovery_only"] = bool(out["discovery_only"])
    return out


# --------------------------------------------------------------------------
# Eval runs
# --------------------------------------------------------------------------


async def insert_eval_run(
    conn: aiosqlite.Connection,
    *,
    id: str,
    submission_id: str,
    epoch: int,
    round_index: int,
    polaris_agent_id: str,
    polaris_run_id: str,
    task_json: dict[str, Any],
    output_card_json: dict[str, Any],
    output_card_hash: str,
    score_parts: dict[str, Any],
    weighted_score: float,
    ran_at: datetime,
    duration_ms: int,
    errors: list[str] | None,
    cathedral_signature: str,
    ran_at_iso: str | None = None,
    polaris_verified: bool = False,
    polaris_attestation: dict[str, Any] | None = None,
    trace_json: dict[str, Any] | None = None,
    polaris_manifest: dict[str, Any] | None = None,
    eval_card_excerpt: dict[str, Any] | None = None,
    eval_artifact_manifest_hash: str | None = None,
    eval_artifact_bundle_url: str | None = None,
    eval_artifact_manifest_url: str | None = None,
    eval_output_schema_version: int = 1,
    commit: bool = True,
) -> None:
    """Insert an eval_runs row.

    `ran_at_iso` is the canonical wire-format timestamp (ms precision,
    trailing 'Z'). It MUST match the value the cathedral signature was
    computed over, otherwise downstream verifiers will reject. When omitted,
    falls back to `ran_at.isoformat()` (legacy callers).

    `polaris_verified` is True when the eval ran on a Polaris-managed
    runtime and the manifest verified. False for BYO-compute miners. The
    1.10x multiplier is already applied to weighted_score upstream; this
    flag persists the verification status for audit + frontend display.

    `polaris_attestation` is the Polaris-signed proof of execution
    (legacy Tier A flow — cathedral-runtime image). Stored as JSON so
    future verifiers can re-check the Ed25519 signature without re-running
    the eval. None for BYO-compute and legacy stub paths.

    `trace_json` is the Hermes-emitted structured trace from the v2
    Polaris-native deploy flow (tool_calls + model_calls + ...). Stored
    as an UNSIGNED sidecar — not part of cathedral_signature bytes —
    so old validators verify v2 rows unchanged. Promoted to signed in
    v2.1 once the schema settles.

    `polaris_manifest` is the verified manifest pulled from
    `/api/cathedral/v1/agents/{id}/manifest` after the v2 deploy. Stored
    so future audits can re-verify the Ed25519 signature.

    v1.1.0 eval-output schema split (cathedralai/cathedral#75 PR 4):

    - ``eval_card_excerpt`` — the small Card subset rendered by the site
      and covered by the v2 signed payload. Populated during the 48h
      dual-publish window alongside ``output_card_json``.
    - ``eval_artifact_manifest_hash`` — blake3 hex of the canonical-JSON
      manifest produced by ``EvalArtifactPublisher.publish()`` (PR 3).
      Signed under v2; covers the Hermes forensic trail by hash.
    - ``eval_artifact_bundle_url`` / ``eval_artifact_manifest_url`` —
      Hippius s3:// URIs for the encrypted bundle and signed manifest.
      Live in the UNSIGNED response envelope (per Q4 contract — URLs
      rotate as Hippius CIDs garbage-collect; we anchor integrity on
      the manifest hash, not the URL identity).
    - ``eval_output_schema_version`` — routing hint the validator's
      ``_SIGNED_KEYS_BY_VERSION`` dispatcher reads to pick the right
      key set. Defaults to 1 so historical rows verify under the v1
      keyset; v1.1.0 publisher writes 2 when
      ``CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true``.

    ``commit`` (default True): when False, the INSERT stays inside the
    caller's transaction; ``score_and_sign`` pairs this with
    ``update_submission_score(..., commit=False)`` and a single commit
    (cathedralai/cathedral#69).
    """
    await conn.execute(
        """
        INSERT INTO eval_runs (
            id, submission_id, epoch, round_index, polaris_agent_id,
            polaris_run_id, task_json, output_card_json, output_card_hash,
            score_parts, weighted_score, ran_at, duration_ms, errors,
            cathedral_signature, polaris_verified, polaris_attestation,
            trace_json, polaris_manifest,
            eval_card_excerpt, eval_artifact_manifest_hash,
            eval_artifact_bundle_url, eval_artifact_manifest_url,
            eval_output_schema_version
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )
        """,
        (
            id,
            submission_id,
            epoch,
            round_index,
            polaris_agent_id,
            polaris_run_id,
            json.dumps(task_json),
            json.dumps(output_card_json),
            output_card_hash,
            json.dumps(score_parts),
            weighted_score,
            ran_at_iso or ran_at.isoformat(),
            duration_ms,
            json.dumps(errors) if errors is not None else None,
            cathedral_signature,
            1 if polaris_verified else 0,
            json.dumps(polaris_attestation) if polaris_attestation is not None else None,
            json.dumps(trace_json) if trace_json is not None else None,
            json.dumps(polaris_manifest) if polaris_manifest is not None else None,
            json.dumps(eval_card_excerpt) if eval_card_excerpt is not None else None,
            eval_artifact_manifest_hash,
            eval_artifact_bundle_url,
            eval_artifact_manifest_url,
            int(eval_output_schema_version),
        ),
    )
    if commit:
        await conn.commit()


async def list_eval_runs_for_submission(
    conn: aiosqlite.Connection, submission_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM eval_runs WHERE submission_id = ? ORDER BY ran_at DESC LIMIT ?",
        (submission_id, limit),
    )
    rows = await cur.fetchall()
    return [_row_to_eval_run(r, cur.description) for r in rows]


async def list_attempts_for_card(
    conn: aiosqlite.Connection,
    card_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Recent eval_runs for a card INCLUDING failed ones.

    Counterpart to ``list_eval_runs_for_card`` which is gated to the
    verified leaderboard surface (`polaris`/`tee`, score > 0 is not
    enforced but discovery is). The miner-onboard UX needs a public
    surface for failed attempts so the leaderboard empty-state can
    surface real network activity even before any agent scores above
    zero (cathedralai/cathedral PR #119).

    Returns rows where:

    - submission is non-discovery (status != 'discovery',
      discovery_only=0) — discovery rows never enter the eval queue,
      so they never have eval_runs, but defense-in-depth keeps the
      join consistent with every other public read;
    - attestation_mode is any of the live tiers (`polaris`, `tee`,
      `polaris-deploy`, `ssh-probe`, `bundle`) — the same set
      ``submissions_due_for_cadence`` admits;
    - ANY ``weighted_score`` (including 0 from
      ``_ssh_hermes_failed=true`` rows);

    Ordered ``ran_at DESC`` so the most recent attempt is first.
    """
    cur = await conn.execute(
        """
        SELECT er.* FROM eval_runs er
        JOIN agent_submissions sub ON sub.id = er.submission_id
        WHERE sub.card_id = ?
          AND sub.status != 'discovery'
          AND sub.attestation_mode IN ('polaris', 'polaris-deploy', 'ssh-probe', 'tee', 'bundle')
          AND sub.discovery_only = 0
        ORDER BY er.ran_at DESC LIMIT ?
        """,
        (card_id, limit),
    )
    rows = await cur.fetchall()
    return [_row_to_eval_run(r, cur.description) for r in rows]


async def list_eval_runs_for_card(
    conn: aiosqlite.Connection,
    card_id: str,
    *,
    since: datetime | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Used for `/v1/cards/{id}/feed` and `/v1/cards/{id}/history`.

    Joined against `agent_submissions` and filtered to the verified
    surface — discovery rows can never produce eval_runs in the first
    place (status='discovery' never enters the eval queue), but the
    join is gated for defense-in-depth.
    """
    if since:
        cur = await conn.execute(
            """
            SELECT er.* FROM eval_runs er
            JOIN agent_submissions sub ON sub.id = er.submission_id
            WHERE sub.card_id = ? AND er.ran_at >= ?
              AND sub.status != 'discovery'
              AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
              AND sub.discovery_only = 0
            ORDER BY er.ran_at DESC LIMIT ?
            """,
            (card_id, since.isoformat(), limit),
        )
    else:
        cur = await conn.execute(
            """
            SELECT er.* FROM eval_runs er
            JOIN agent_submissions sub ON sub.id = er.submission_id
            WHERE sub.card_id = ?
              AND sub.status != 'discovery'
              AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
              AND sub.discovery_only = 0
            ORDER BY er.ran_at DESC LIMIT ?
            """,
            (card_id, limit),
        )
    rows = await cur.fetchall()
    return [_row_to_eval_run(r, cur.description) for r in rows]


async def list_eval_runs_recent(
    conn: aiosqlite.Connection,
    *,
    since: datetime,
    since_id: str | None = None,
    limit: int = 200,
    include_v3: bool = True,
    include_task_families: bool = True,
) -> list[dict[str, Any]]:
    """Cross-card recent feed used by the validator pull loop AND the
    public `/v1/leaderboard/recent` endpoint.

    Two cursor shapes, dispatched on ``since_id``:

    * **v1.1.0 tuple cursor** (``since_id`` is a string, including
      ``""``). Predicate ``WHERE (ran_at, id) > (?, ?)`` over the
      composite total order ``(ran_at, id)``. Used by v1.1.0 validators
      that thread the ``next_since_ran_at`` + ``next_since_id`` pair
      back through subsequent pulls. No row is ever returned twice and
      no row is ever skipped, even when many rows share the same
      millisecond ``ran_at`` (the cadence eval case).
    * **v1.0.7 legacy cursor** (``since_id is None``). Predicate
      ``WHERE ran_at > ?`` — strict `>` on the single timestamp, no
      tuple comparison. Used when a v1.0.7 validator polls without the
      ``since_id`` query param. Strict `>` is the cleaner of the two
      legacy operators (audit Risk 2 belt-and-suspenders): every
      non-collision tick advances the cursor cleanly past the boundary
      row, no UPSERT thrashing. The audit acknowledges that >limit
      rows at one millisecond is unsolvable for a stateless
      single-string cursor regardless of operator; v1.0.7's fleet is
      expected to PM2-cycle to v1.1.0 before cadence sweeps land.

    Both branches normalize the ``since`` datetime to the canonical
    millisecond-precision Z form (matching ``scoring_pipeline._ms_iso``,
    which is the format every stored ``eval_runs.ran_at`` carries). The
    naive ``datetime.isoformat()`` renders ``+00:00`` instead of ``Z``,
    and string comparisons of ``+00:00`` vs ``.000Z`` flip in the
    opposite direction of the intended chronological semantics. Without
    this normalization, both the tuple and legacy cursors degrade at
    the boundary millisecond.

    Ordering on ``(ran_at ASC, id ASC)`` in both branches. The composite
    index ``idx_eval_ran_at_id`` covers this access pattern.

    Joined against ``agent_submissions`` and gated on the verified
    surface so unverified discovery submissions never appear on the
    leaderboard feed. Discovery rows never produce eval_runs at all, but
    the join is defense-in-depth in case a future code path inserts one.
    """
    since_str = _ms_z(since)
    # PR1 (SN39 recovery): unconditional SAT-era inclusion filter. Card-era
    # rows (schema_version=1, task_type 'eu-ai-act'/'us-ai-eo'/etc.) were
    # being bucketed by Path A validators as v1 primary incentive and paid
    # out on chain despite zero SAT work. Scoping the recent-feed to
    # schema >= 5 closes that leak at the source for
    # ``/v1/leaderboard/recent`` and the validator pull loop.
    schema_gates: list[str] = ["er.eval_output_schema_version >= 5"]
    if not include_v3:
        schema_gates.append("er.eval_output_schema_version != 3")
    if not include_task_families:
        schema_gates.append("er.eval_output_schema_version != 5")
    schema_gate = "AND " + " AND ".join(schema_gates)
    if since_id is None:
        # Legacy v1.0.7-style cursor: strict `>` on ran_at only.
        cur = await conn.execute(
            f"""
            SELECT er.* FROM eval_runs er
            JOIN agent_submissions sub ON sub.id = er.submission_id
            WHERE er.ran_at > ?
              {schema_gate}
              AND sub.status != 'discovery'
              AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
              AND sub.discovery_only = 0
            ORDER BY er.ran_at ASC, er.id ASC LIMIT ?
            """,
            (since_str, limit),
        )
    else:
        # v1.1.0 tuple cursor: row-value comparison over (ran_at, id).
        cur = await conn.execute(
            f"""
            SELECT er.* FROM eval_runs er
            JOIN agent_submissions sub ON sub.id = er.submission_id
            WHERE (er.ran_at, er.id) > (?, ?)
              {schema_gate}
              AND sub.status != 'discovery'
              AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
              AND sub.discovery_only = 0
            ORDER BY er.ran_at ASC, er.id ASC LIMIT ?
            """,
            (since_str, since_id, limit),
        )
    rows = await cur.fetchall()
    return [_row_to_eval_run(r, cur.description) for r in rows]


def _ms_z(dt: datetime) -> str:
    """Render a datetime in the same millisecond-precision Z form that
    ``scoring_pipeline._ms_iso`` writes to every stored ``ran_at``.

    Stored rows look like ``2026-05-10T12:00:00.000Z``; the cursor
    comparison must use the same shape or ASCII string compares flip
    (``+`` < ``.`` < ``Z``, so a ``+00:00`` cursor compares as strictly
    less than any ``.NNNZ`` row even when the underlying instants are
    equal).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    s = dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    return s + "Z"


async def list_eval_runs_in_window(
    conn: aiosqlite.Connection, *, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM eval_runs WHERE ran_at >= ? AND ran_at < ? ORDER BY id ASC",
        (start.isoformat(), end.isoformat()),
    )
    rows = await cur.fetchall()
    return [_row_to_eval_run(r, cur.description) for r in rows]


async def rolling_avg_score(
    conn: aiosqlite.Connection, submission_id: str, *, days: int = 30
) -> float | None:
    """30-day rolling average of weighted scores for a submission."""
    from datetime import timedelta

    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = await conn.execute(
        "SELECT AVG(weighted_score) FROM eval_runs WHERE submission_id = ? AND ran_at >= ?",
        (submission_id, since),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


async def best_eval_run_for_card(conn: aiosqlite.Connection, card_id: str) -> dict[str, Any] | None:
    """Best (highest-scoring) eval run for a card — verified surface only.

    Discovery / unverified submissions never enter the eval queue, so
    they never have eval_runs rows. But the join is still gated on
    attestation_mode for defense-in-depth: the public `best_eval` field
    on `/v1/cards/{id}` must reflect only attested runs.
    """
    cur = await conn.execute(
        """
        SELECT er.* FROM eval_runs er
        JOIN agent_submissions sub ON sub.id = er.submission_id
        WHERE sub.card_id = ?
          AND sub.status != 'discovery'
          AND sub.attestation_mode IN ('polaris','polaris-deploy','ssh-probe','tee','bundle')
          AND sub.discovery_only = 0
        ORDER BY er.weighted_score DESC, er.ran_at DESC LIMIT 1
        """,
        (card_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_eval_run(row, cur.description)


async def incumbent_best_score(
    conn: aiosqlite.Connection,
    card_id: str,
    submitted_before: datetime,
) -> float | None:
    """Section 7.2: max weighted_score for any prior submission on this card."""
    cur = await conn.execute(
        """
        SELECT MAX(er.weighted_score) FROM eval_runs er
        JOIN agent_submissions sub ON sub.id = er.submission_id
        WHERE sub.card_id = ? AND sub.submitted_at < ?
        """,
        (card_id, submitted_before.isoformat()),
    )
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return float(row[0])


async def queued_submissions(conn: aiosqlite.Connection, limit: int = 4) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT * FROM agent_submissions "
        "WHERE card_id='synthetic_boolean_v1' "
        "AND status IN ('pending_check','queued') "
        "ORDER BY submitted_at ASC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


async def submissions_due_for_cadence(
    conn: aiosqlite.Connection,
    *,
    now: datetime,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Pick `ranked` submissions whose cadence window has expired.

    cathedralai/cathedral#75 PR 5: each card has a
    `refresh_cadence_hours` (defaults 24). A `ranked` submission is
    due for a fresh eval when `now - max(eval_runs.ran_at) >=
    card.refresh_cadence_hours`. Each cadence tick produces a new
    eval_runs row tied to the same submission_id — 1:N over time.

    The query joins agent_submissions → card_definitions for the
    cadence, then LEFT JOINs eval_runs for the latest ran_at per
    submission. Sorted by "most overdue first" so a backlog drains
    in priority order.

    `discovery` rows are excluded (they never run evals). `rejected`
    rows are excluded (the cadence loop doesn't resurrect them; a
    miner resubmits to re-enter). Only `ranked` is in scope here;
    `queued` is handled by ``queued_submissions`` (first-eval path).
    """
    now_iso = now.isoformat()
    cur = await conn.execute(
        """
        SELECT sub.*,
               COALESCE(MAX(er.ran_at), sub.submitted_at) AS _last_eval_ran_at,
               cd.refresh_cadence_hours AS _refresh_cadence_hours
        FROM agent_submissions sub
        JOIN card_definitions cd ON cd.id = sub.card_id
        LEFT JOIN eval_runs er ON er.submission_id = sub.id
        WHERE sub.status = 'ranked'
          AND sub.card_id = 'synthetic_boolean_v1'
          AND sub.attestation_mode IN ('polaris', 'polaris-deploy', 'ssh-probe', 'tee', 'bundle')
          AND sub.discovery_only = 0
        GROUP BY sub.id
        HAVING (
            julianday(?) - julianday(COALESCE(MAX(er.ran_at), sub.submitted_at))
        ) * 24.0 >= cd.refresh_cadence_hours
        ORDER BY _last_eval_ran_at ASC
        LIMIT ?
        """,
        (now_iso, limit),
    )
    rows = await cur.fetchall()
    return [_row_to_submission(r, cur.description) for r in rows]


def _row_to_eval_run(
    row: aiosqlite.Row | tuple[Any, ...],
    description: list[tuple[Any, ...]] | Any,
) -> dict[str, Any]:
    cols = [d[0] for d in description]
    out = dict(zip(cols, row, strict=False))
    out["task_json"] = json.loads(out["task_json"])
    out["output_card_json"] = json.loads(out["output_card_json"])
    out["score_parts"] = json.loads(out["score_parts"])
    if out.get("errors"):
        out["errors"] = json.loads(out["errors"])
    if out.get("polaris_attestation"):
        out["polaris_attestation"] = json.loads(out["polaris_attestation"])
    return out


# --------------------------------------------------------------------------
# Merkle anchors
# --------------------------------------------------------------------------


async def insert_merkle_anchor(
    conn: aiosqlite.Connection,
    *,
    epoch: int,
    merkle_root: str,
    eval_count: int,
    leaf_hashes: list[str],
    computed_at: datetime,
    on_chain_block: int | None = None,
    on_chain_extrinsic_index: int | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO merkle_anchors (
            epoch, merkle_root, eval_count, leaf_hashes_json,
            computed_at, on_chain_block, on_chain_extrinsic_index
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(epoch) DO UPDATE SET
            merkle_root=excluded.merkle_root,
            eval_count=excluded.eval_count,
            leaf_hashes_json=excluded.leaf_hashes_json,
            computed_at=excluded.computed_at,
            on_chain_block=excluded.on_chain_block,
            on_chain_extrinsic_index=excluded.on_chain_extrinsic_index
        """,
        (
            epoch,
            merkle_root,
            eval_count,
            json.dumps(leaf_hashes),
            computed_at.isoformat(),
            on_chain_block,
            on_chain_extrinsic_index,
        ),
    )
    await conn.commit()


async def get_merkle_anchor(conn: aiosqlite.Connection, epoch: int) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT epoch, merkle_root, eval_count, leaf_hashes_json, "
        "computed_at, on_chain_block, on_chain_extrinsic_index "
        "FROM merkle_anchors WHERE epoch = ?",
        (epoch,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "epoch": int(row[0]),
        "merkle_root": str(row[1]),
        "eval_count": int(row[2]),
        "leaf_hashes": json.loads(row[3]),
        "computed_at": row[4],
        "on_chain_block": int(row[5]) if row[5] is not None else None,
        "on_chain_extrinsic_index": int(row[6]) if row[6] is not None else None,
    }


async def latest_merkle_epoch(conn: aiosqlite.Connection) -> int | None:
    cur = await conn.execute("SELECT MAX(epoch) FROM merkle_anchors")
    row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


async def link_eval_runs_to_epoch(
    conn: aiosqlite.Connection, eval_run_ids: list[str], epoch: int
) -> None:
    if not eval_run_ids:
        return
    await conn.executemany(
        "INSERT OR REPLACE INTO eval_run_to_epoch (eval_run_id, epoch) VALUES (?, ?)",
        [(rid, epoch) for rid in eval_run_ids],
    )
    await conn.commit()


# --------------------------------------------------------------------------
# PR5 (solve-on-submit) helpers
# --------------------------------------------------------------------------


async def upsert_sat_registration(
    conn: aiosqlite.Connection,
    *,
    miner_hotkey: str,
    card_id: str,
    bundle_hash: str,
    bundle_size_bytes: int,
    bundle_signature: str,
    display_name: str,
    bio: str | None,
    ssh_host: str,
    ssh_port: int,
    ssh_user: str,
    submitted_at_iso: str,
    challenge_id: str | None = None,
    initial_status: str = "pending_check",
) -> str:
    """Upsert an ``agent_submissions`` row for the SAT lane.

    If a row already exists for ``(miner_hotkey, card_id, bundle_hash)``
    (the UNIQUE index), update the SSH coordinates + display fields and
    bump the timestamp; otherwise insert a new row in ``initial_status``.
    Returns the row id.

    Used by the PR5 solve-on-submit path so a re-POST from the same
    hotkey doesn't 409 — re-submission is a normal flow once miners
    iterate on their solver setup.

    Caller MUST hold ``ctx.db_write_lock`` because this performs a
    read-then-write sequence on the shared connection.
    """
    from uuid import uuid4

    sat_challenge_id = (challenge_id or "").strip() or None
    if sat_challenge_id is None:
        cur = await conn.execute(
            "SELECT id, status, sat_challenge_id, seq_no FROM agent_submissions "
            "WHERE miner_hotkey = ? AND card_id = ? AND bundle_hash = ? "
            "AND sat_challenge_id IS NULL LIMIT 1",
            (miner_hotkey, card_id, bundle_hash),
        )
    else:
        cur = await conn.execute(
            "SELECT id, status, sat_challenge_id, seq_no FROM agent_submissions "
            "WHERE miner_hotkey = ? AND card_id = ? AND bundle_hash = ? "
            "AND sat_challenge_id = ? LIMIT 1",
            (miner_hotkey, card_id, bundle_hash, sat_challenge_id),
        )
    row = await cur.fetchone()
    if row is not None:
        existing_id = str(row[0])
        existing_status = str(row[1])
        existing_seq_no = row[3]
        # Don't downgrade a ranked/valid row back to a registration state.
        # Non-final rows move to the caller's requested initial status;
        # PR5 uses this to keep no-solution rows out of the eval queue.
        next_status = existing_status
        if existing_status not in {"ranked", "valid_attestation_pending"}:
            next_status = initial_status
        seq_update = ""
        params: list[Any] = [
            ssh_host,
            ssh_port,
            ssh_user,
            display_name,
            bio,
            bundle_signature,
            bundle_size_bytes,
            submitted_at_iso,
            next_status,
        ]
        if sat_challenge_id is not None and existing_seq_no is None:
            seq_no = await _next_sat_submission_seq_no(conn, sat_challenge_id)
            seq_update = ", sat_challenge_id = ?, seq_no = ? "
            params.extend([sat_challenge_id, seq_no])
        params.append(existing_id)
        await conn.execute(
            "UPDATE agent_submissions "
            "SET ssh_host = ?, ssh_port = ?, ssh_user = ?, "
            "    display_name = ?, bio = COALESCE(?, bio), "
            "    bundle_signature = ?, bundle_size_bytes = ?, "
            f"    submitted_at = ?, status = ? {seq_update}"
            "WHERE id = ?",
            params,
        )
        return existing_id

    new_id = str(uuid4())
    seq_no = (
        await _next_sat_submission_seq_no(conn, sat_challenge_id)
        if sat_challenge_id is not None
        else None
    )
    await conn.execute(
        """
        INSERT INTO agent_submissions (
            id, miner_hotkey, card_id, bundle_blob_key, bundle_hash,
            bundle_size_bytes, encryption_key_id, bundle_signature,
            display_name, bio, logo_url, soul_md_preview,
            metadata_fingerprint, similarity_check_passed,
            rejection_reason, submitted_at, status, first_mover_at,
            sat_challenge_id, seq_no,
            attestation_mode, attestation_type, attestation_blob,
            attestation_verified_at, discovery_only,
            ssh_host, ssh_port, ssh_user, hermes_port
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id,
            miner_hotkey,
            card_id,
            "",
            bundle_hash,
            bundle_size_bytes,
            "",
            bundle_signature,
            display_name,
            bio,
            None,
            None,
            "",
            1,
            None,
            submitted_at_iso,
            initial_status,
            None,
            sat_challenge_id,
            seq_no,
            "ssh-probe",
            None,
            None,
            None,
            0,
            ssh_host,
            ssh_port,
            ssh_user,
            None,
        ),
    )
    return new_id


async def _next_sat_submission_seq_no(
    conn: aiosqlite.Connection,
    challenge_id: str,
) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(seq_no), 0) + 1 "
        "FROM agent_submissions WHERE sat_challenge_id = ?",
        (challenge_id,),
    )
    row = await cur.fetchone()
    return int(row[0] or 1)


@dataclass
class RankedSolveResult:
    """Outcome of :func:`record_ranked_solve`.

    ``solve_rank`` is the operator's 1-indexed position among valid solvers of
    this challenge (publisher receipt order). ``newly_recorded`` is False when
    the hotkey had already solved this challenge — the prior rank is returned
    and no second row is written (idempotent re-submit).
    """

    solve_rank: int
    newly_recorded: bool


async def record_ranked_solve(
    conn: aiosqlite.Connection,
    *,
    family_id: str,
    challenge_id: str,
    miner_hotkey: str,
    eval_run_id: str,
    weighted_score: float,
    solved_at_iso: str,
) -> RankedSolveResult:
    """Record a valid solve in the open-window ledger and return its rank.

    Open-window analogue of :func:`atomic_claim_winner`: instead of locking a
    single winner, every valid solver is appended with a monotonic
    ``solve_rank``. Idempotent per ``(family, challenge, miner_hotkey)`` — a
    re-submit returns the existing rank without writing a second row.

    MUST be called while holding the publisher write lock (``ctx.db_write_lock``),
    the same contract as ``atomic_claim_winner``: rank assignment reads the
    current count then inserts, so it is only race-free under that serialization.
    """
    cur = await conn.execute(
        "SELECT solve_rank FROM lane_challenge_solves "
        "WHERE family_id = ? AND challenge_id = ? AND miner_hotkey = ?",
        (family_id, challenge_id, miner_hotkey),
    )
    existing = await cur.fetchone()
    if existing is not None:
        return RankedSolveResult(solve_rank=int(existing[0]), newly_recorded=False)

    cur = await conn.execute(
        "SELECT COUNT(*) FROM lane_challenge_solves WHERE family_id = ? AND challenge_id = ?",
        (family_id, challenge_id),
    )
    count_row = await cur.fetchone()
    rank = int(count_row[0] if count_row else 0) + 1

    await conn.execute(
        "INSERT INTO lane_challenge_solves ("
        "family_id, challenge_id, miner_hotkey, eval_run_id, solve_rank, "
        "weighted_score, solved_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            family_id,
            challenge_id,
            miner_hotkey,
            eval_run_id,
            rank,
            float(weighted_score),
            solved_at_iso,
        ),
    )
    await conn.commit()
    return RankedSolveResult(solve_rank=rank, newly_recorded=True)


@dataclass
class AtomicWinnerResult:
    """Outcome of :func:`atomic_claim_winner`.

    ``won`` is True iff this call wrote the winning eval_run + lock row.
    ``won=False`` means another caller beat us to it; ``existing_winner``
    carries the row data so the handler can build the 409 body.
    """

    won: bool
    eval_run_id: str
    existing_winner_hotkey: str | None = None
    existing_winner_won_at_iso: str | None = None


async def atomic_claim_winner(
    conn: aiosqlite.Connection,
    *,
    family_id: str,
    challenge_id: str,
    miner_hotkey: str,
    submission_id: str,
    cnf_sha256: str,
    dimacs_solution_sha256: str,
    ran_at_iso: str,
    signed_row: dict[str, Any],
    epoch: int,
    round_index: int,
    time_limit_seconds: int,
    weighted_score: float = 1.0,
    attestation_status: str = "pending",
    dimacs_solution: str | None = None,
) -> AtomicWinnerResult:
    """Atomically claim the round winner for the SAT lane.

    Uses ``BEGIN IMMEDIATE`` to upgrade to a write-lock before reading
    the active-challenge state, then:

    1. Re-checks ``lane_challenges.status='active'`` and the requested
       ``challenge_id`` matches. If not, rolls back and returns
       ``AtomicWinnerResult(won=False, ...)``.
    2. Inserts the winning ``eval_runs`` row with the already-signed
       schema-5 task-family payload. ``attestation_status='pending'``
       means the async SSH attest worker may audit the endpoint later,
       but valid mathematical work enters the signed payment path now.
    3. Inserts the winner row into ``lane_challenge_winners`` via
       ``INSERT OR IGNORE``. If another caller already claimed it
       (the PRIMARY KEY collides), rolls back the eval_run insert and
       returns ``won=False`` with the existing winner's hotkey.
    4. Flips ``lane_challenges.status`` to ``locked``.
    5. Updates ``agent_submissions`` to ``status='ranked'`` +
       ``current_score=1.0`` so signed remote weights can pick it up
       immediately.
    6. Commits.

    Caller MUST hold ``ctx.db_write_lock`` so a separate code path on
    the shared connection (eval_loop, weight policy producer) does not
    interleave with our explicit transaction. The asyncio lock + the
    ``BEGIN IMMEDIATE`` give us a single writer + an explicit
    SQLite-level write lock for defense in depth.
    """
    eval_run_id = str(signed_row["id"])
    task_json, output_card_json, output_card_hash = _task_family_storage_from_signed_row(
        signed_row,
        family_id=family_id,
        challenge_id=challenge_id,
        cnf_sha256=cnf_sha256,
        dimacs_solution_sha256=dimacs_solution_sha256,
        time_limit_seconds=time_limit_seconds,
        miner_hotkey=miner_hotkey,
    )

    # The shared publisher connection also serves older implicit-transaction
    # helpers. Under thread-heavy TestClient traffic, sqlite can still report
    # an open implicit transaction when this explicit CAS begins. The caller
    # holds ctx.db_write_lock, so it is safe to close that prior transaction
    # boundary before taking the real BEGIN IMMEDIATE lock.
    if conn.in_transaction:
        await conn.commit()

    # BEGIN IMMEDIATE upgrades to a write-lock straight away. Together
    # with the asyncio lock the caller is holding, this serializes
    # concurrent POSTs that want to lock the same round.
    await conn.execute("BEGIN IMMEDIATE")
    try:
        # Step 1: re-check the active challenge. Reading inside the
        # write transaction guarantees the row state is stable until
        # our commit/rollback.
        cur = await conn.execute(
            "SELECT status FROM lane_challenges "
            "WHERE family_id = ? AND challenge_id = ?",
            (family_id, challenge_id),
        )
        row = await cur.fetchone()
        if row is None or str(row[0]) != "active":
            await conn.rollback()
            # Look up who already won so the handler can include it in
            # the 409 body. Read outside the transaction since the
            # response is informational.
            existing = await _existing_winner(conn, family_id, challenge_id)
            return AtomicWinnerResult(
                won=False,
                eval_run_id=eval_run_id,
                existing_winner_hotkey=existing[0] if existing else None,
                existing_winner_won_at_iso=existing[1] if existing else None,
            )

        # Step 3 first (CAS via INSERT OR IGNORE on PRIMARY KEY) — if
        # another caller is in the same transaction window, the PK
        # collision is the canonical "I lost" signal.
        await conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_winners ("
            "family_id, challenge_id, miner_hotkey, eval_run_id, "
            "weighted_score, won_at_iso) VALUES (?, ?, ?, ?, ?, ?)",
            (
                family_id,
                challenge_id,
                miner_hotkey,
                eval_run_id,
                float(signed_row["weighted_score"]),
                ran_at_iso,
            ),
        )
        winner_cur = await conn.execute(
            "SELECT miner_hotkey, eval_run_id, won_at_iso "
            "FROM lane_challenge_winners "
            "WHERE family_id = ? AND challenge_id = ?",
            (family_id, challenge_id),
        )
        winner_row = await winner_cur.fetchone()
        if winner_row is None:
            # Should be impossible after INSERT OR IGNORE — treat as a
            # programming error.
            await conn.rollback()
            raise RuntimeError(
                "lane_challenge_winners row missing after INSERT OR IGNORE"
            )
        existing_hotkey = str(winner_row[0])
        existing_eval_run_id = str(winner_row[1])
        existing_won_at_iso = str(winner_row[2])
        if existing_eval_run_id != eval_run_id:
            # Another caller won. Roll back so no eval_run row is
            # written for this attempt; the caller can write a losing
            # eval_run separately.
            await conn.rollback()
            return AtomicWinnerResult(
                won=False,
                eval_run_id=eval_run_id,
                existing_winner_hotkey=existing_hotkey,
                existing_winner_won_at_iso=existing_won_at_iso,
            )

        # We are the winner. Step 2: insert the eval_run.
        await conn.execute(
            """
            INSERT INTO eval_runs (
                id, submission_id, epoch, round_index, polaris_agent_id,
                polaris_run_id, task_json, output_card_json, output_card_hash,
                score_parts, weighted_score, ran_at, duration_ms, errors,
                cathedral_signature, polaris_verified, polaris_attestation,
                trace_json, polaris_manifest,
                eval_card_excerpt, eval_artifact_manifest_hash,
                eval_artifact_bundle_url, eval_artifact_manifest_url,
                eval_output_schema_version, attestation_status
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                eval_run_id,
                submission_id,
                int(epoch),
                int(round_index),
                f"direct-submit:{miner_hotkey[:12]}",
                f"{family_id}:{eval_run_id}",
                json.dumps(task_json, sort_keys=True),
                json.dumps(output_card_json, sort_keys=True),
                output_card_hash,
                json.dumps(dict(signed_row["score_parts"])),
                float(signed_row["weighted_score"]),
                ran_at_iso,
                0,
                None,
                str(signed_row["cathedral_signature"]),
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                int(signed_row.get("eval_output_schema_version") or 5),
                attestation_status,
            ),
        )

        # Step 4: lock the challenge.
        await conn.execute(
            "UPDATE lane_challenges "
            "SET status = 'locked', losers_published_at_iso = NULL, "
            "    updated_at_iso = ? "
            "WHERE family_id = ? AND challenge_id = ? AND status = 'active'",
            (ran_at_iso, family_id, challenge_id),
        )

        # Step 5: valid solve is payable immediately. The SAT attest
        # worker can still audit the SSH endpoint later, but no valid
        # mathematical answer waits on SSH before entering weights.
        await conn.execute(
            "UPDATE agent_submissions "
            "SET status = 'ranked', current_score = ?, rejection_reason = NULL "
            "WHERE id = ?",
            (float(signed_row["weighted_score"]), submission_id),
        )

        # Step 6 (issue #242): retain the raw DIMACS body in the audit
        # sidecar. Inside the SAME transaction as the eval_runs INSERT
        # above, so the audit row and the verdict either both land or
        # neither does. Opt-in via the `dimacs_solution` kwarg so the
        # ssh-pushed path (which does not have the body at claim time)
        # can omit it; the direct-submit path in submit.py passes it.
        if dimacs_solution is not None:
            await conn.execute(
                "INSERT INTO eval_run_solutions ("
                "eval_run_id, dimacs_solution, body_sha256, stored_at"
                ") VALUES (?, ?, ?, ?)",
                (
                    eval_run_id,
                    dimacs_solution,
                    dimacs_solution_sha256,
                    ran_at_iso,
                ),
            )

        await conn.commit()
        return AtomicWinnerResult(
            won=True,
            eval_run_id=eval_run_id,
        )
    except Exception:
        with suppress(Exception):
            await conn.rollback()
        raise


async def _existing_winner(
    conn: aiosqlite.Connection, family_id: str, challenge_id: str
) -> tuple[str, str] | None:
    cur = await conn.execute(
        "SELECT miner_hotkey, won_at_iso FROM lane_challenge_winners "
        "WHERE family_id = ? AND challenge_id = ?",
        (family_id, challenge_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return (str(row[0]), str(row[1]))


async def insert_losing_eval_run(
    conn: aiosqlite.Connection,
    *,
    submission_id: str,
    challenge_id: str,
    cnf_sha256: str,
    dimacs_solution_sha256: str,
    error_code: str,
    ran_at_iso: str,
    signed_row: dict[str, Any],
    epoch: int,
    round_index: int,
    time_limit_seconds: int,
    miner_hotkey: str,
) -> str:
    """Write a losing/invalid ``eval_runs`` row for leaderboard transparency.

    Used when a solve-POST either:
    - has invalid DIMACS (the spec wants a row with score 0 + error), or
    - arrives after the round was already locked.

    Losing rows are still signed schema-5 rows with ``weighted_score=0``
    so validators and the public dashboard can authenticate the attempt.
    The companion submission is marked ``rejected`` so the legacy SSH
    scheduler never probes an invalid/no-winning answer. Caller MUST
    hold the db_write_lock.
    """
    eval_run_id = str(signed_row["id"])
    task_json, output_card_json, output_card_hash = _task_family_storage_from_signed_row(
        signed_row,
        family_id="synthetic_boolean_v1",
        challenge_id=challenge_id,
        cnf_sha256=cnf_sha256,
        dimacs_solution_sha256=dimacs_solution_sha256,
        time_limit_seconds=time_limit_seconds,
        miner_hotkey=miner_hotkey,
    )
    await conn.execute(
        """
        INSERT INTO eval_runs (
            id, submission_id, epoch, round_index, polaris_agent_id,
            polaris_run_id, task_json, output_card_json, output_card_hash,
            score_parts, weighted_score, ran_at, duration_ms, errors,
            cathedral_signature, polaris_verified, polaris_attestation,
            trace_json, polaris_manifest,
            eval_card_excerpt, eval_artifact_manifest_hash,
            eval_artifact_bundle_url, eval_artifact_manifest_url,
            eval_output_schema_version, attestation_status
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?
        )
        """,
        (
            eval_run_id,
            submission_id,
            int(epoch),
            int(round_index),
            f"direct-submit:{miner_hotkey[:12]}",
            f"synthetic_boolean_v1:{eval_run_id}",
            json.dumps(task_json, sort_keys=True),
            json.dumps(output_card_json, sort_keys=True),
            output_card_hash,
            json.dumps(dict(signed_row["score_parts"])),
            float(signed_row["weighted_score"]),
            ran_at_iso,
            0,
            json.dumps([error_code]),
            str(signed_row["cathedral_signature"]),
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            int(signed_row.get("eval_output_schema_version") or 5),
            # Losing rows don't need attestation — the score is already
            # 0 and the normal ranking keeps them out. Mark
            # 'not_applicable' so the attest worker doesn't pick them up.
            "not_applicable",
        ),
    )
    await conn.execute(
        "UPDATE agent_submissions "
        "SET status = 'rejected', current_score = 0.0, rejection_reason = ? "
        "WHERE id = ?",
        (error_code, submission_id),
    )
    await conn.commit()
    return eval_run_id


# --------------------------------------------------------------------------
# eval_run_solutions sidecar — see issue #242.
#
# The publisher historically stored only the SHA-256 of submitted DIMACS
# solutions. That made post-hoc audit/dispute resolution impossible (a
# hash is one-way). This sidecar table retains the raw body next to the
# eval_runs row that scored it, with ON DELETE CASCADE so the audit
# record disappears with the verdict if a verdict is ever expunged.
#
# Bodies are written ONLY via `insert_eval_run_solution`, inside the
# same db_write_lock + transaction scope as the parent eval_runs INSERT,
# so either both rows land or neither does. Reads go via the dedicated
# bearer-gated audit endpoint (`/v1/audit/eval-runs/{id}/solution`) —
# the public projection in `list_eval_runs_recent` does NOT join here.
# --------------------------------------------------------------------------

EVAL_RUN_SOLUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_run_solutions (
    eval_run_id     TEXT PRIMARY KEY REFERENCES eval_runs(id) ON DELETE CASCADE,
    dimacs_solution TEXT NOT NULL,
    body_sha256     TEXT NOT NULL,
    stored_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_run_solutions_stored_at
    ON eval_run_solutions(stored_at);
"""


# --------------------------------------------------------------------------
# rules_versions — signed v1.3 rules documents.
#
# PR1 for the decentralized SAT lane adds the durable table but does not make
# validators consume it yet. At most one row may be active. The publisher
# writes rules through cathedral.publisher.rules.publish_new_rules(), which
# computes version_id before signing so the body has no autoincrement/signature
# circularity.
# --------------------------------------------------------------------------

RULES_VERSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS rules_versions (
    version_id               INTEGER PRIMARY KEY,
    body_json                TEXT NOT NULL,
    body_sha256              TEXT NOT NULL,
    cathedral_sig            TEXT NOT NULL,
    key_id                   TEXT NOT NULL,
    canonicalization_version TEXT NOT NULL,
    published_at             TEXT NOT NULL,
    active                   INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_versions_one_active
    ON rules_versions(active)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_rules_versions_published_at
    ON rules_versions(published_at DESC);
"""


# --------------------------------------------------------------------------
# v1.3 decentralized SAT lane audit/shadow tables.
#
# `eval_run_solutions` remains the PR #243 raw-body sidecar. Validator-local
# verdicts and legacy-vs-decentralized weight diffs live in their own tables so
# later reward-engine work can shadow without mutating the payment path.
# --------------------------------------------------------------------------

V13_DECENTRALIZED_LANE_SCHEMA = """
CREATE TABLE IF NOT EXISTS validator_verdicts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    validator_hotkey      TEXT NOT NULL,
    challenge_id          TEXT NOT NULL,
    seq_no                INTEGER NOT NULL,
    submission_id         TEXT,
    eval_run_id           TEXT,
    dimacs_sha256         TEXT NOT NULL,
    valid                 INTEGER NOT NULL CHECK(valid IN (0, 1)),
    score                 REAL NOT NULL DEFAULT 0.0,
    rejection_reason      TEXT,
    verifier_version      TEXT NOT NULL,
    verifier_details_hash TEXT,
    reported_at           TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    UNIQUE(validator_hotkey, challenge_id, seq_no, verifier_version)
);
CREATE INDEX IF NOT EXISTS idx_validator_verdicts_challenge_seq
    ON validator_verdicts(challenge_id, seq_no);
CREATE INDEX IF NOT EXISTS idx_validator_verdicts_reported_at
    ON validator_verdicts(reported_at DESC);

CREATE TABLE IF NOT EXISTS shadow_weight_diffs (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    rules_version_id            INTEGER,
    validator_hotkey            TEXT NOT NULL,
    network                     TEXT NOT NULL,
    netuid                      INTEGER NOT NULL,
    legacy_vector_sha256        TEXT NOT NULL,
    decentralized_vector_sha256 TEXT NOT NULL,
    abs_diff_sum                REAL NOT NULL,
    max_diff                    REAL NOT NULL,
    legacy_weight_count         INTEGER NOT NULL,
    decentralized_weight_count  INTEGER NOT NULL,
    blocker                     INTEGER NOT NULL DEFAULT 0 CHECK(blocker IN (0, 1)),
    details_json                TEXT NOT NULL DEFAULT '{}',
    computed_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_weight_diffs_computed_at
    ON shadow_weight_diffs(computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_shadow_weight_diffs_blocker
    ON shadow_weight_diffs(blocker, computed_at DESC);
"""


# --------------------------------------------------------------------------
# lane_challenge_solves — open-window (PAR-2) ranked solve ledger.
#
# Replaces the single-winner lane_challenge_winners lock for the open-window
# scoring model: EVERY valid solve is recorded with a monotonic solve_rank
# (publisher receipt order) instead of only the first. UNIQUE(family, challenge,
# miner_hotkey) makes a re-submit idempotent (one scored solve per hotkey per
# challenge). Used only when open-window scoring is enabled; the legacy winner
# lock stays intact behind the flag so live behavior and contract tests are
# unchanged until the cutover.
# --------------------------------------------------------------------------

LANE_CHALLENGE_SOLVES_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_challenge_solves (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id      TEXT NOT NULL,
    challenge_id   TEXT NOT NULL,
    miner_hotkey   TEXT NOT NULL,
    eval_run_id    TEXT NOT NULL,
    solve_rank     INTEGER NOT NULL,
    weighted_score REAL NOT NULL DEFAULT 1.0,
    solved_at_iso  TEXT NOT NULL,
    UNIQUE(family_id, challenge_id, miner_hotkey)
);
CREATE INDEX IF NOT EXISTS idx_lane_challenge_solves_challenge
    ON lane_challenge_solves(family_id, challenge_id, solve_rank);
"""


async def insert_eval_run_solution(
    conn: aiosqlite.Connection,
    *,
    eval_run_id: str,
    dimacs_solution: str,
    body_sha256: str,
    stored_at: str,
) -> None:
    """Persist the raw DIMACS body for an eval_run.

    Must be called inside the SAME ``db_write_lock`` context that wraps
    the parent ``eval_runs`` INSERT (see ``submit.py`` direct-submit
    path). The helper does NOT manage its own transaction — it issues a
    bare INSERT and lets the surrounding caller commit, so the sidecar
    row lands in the same transactional scope as the verdict.

    The body is written verbatim (UTF-8 string) — we DO NOT
    re-canonicalise. ``body_sha256`` should mirror the value already
    stored in ``eval_runs`` / ``agent_submissions`` as
    ``miner_solution_sha256`` so a later audit can independently confirm
    the body has not drifted from the hash the publisher signed against.

    Caller MUST hold ``ctx.db_write_lock`` — the helper writes on the
    publisher's shared connection and must not interleave with other
    writers (eval_loop, weight policy producer).
    """
    await conn.execute(
        "INSERT INTO eval_run_solutions ("
        "eval_run_id, dimacs_solution, body_sha256, stored_at"
        ") VALUES (?, ?, ?, ?)",
        (eval_run_id, dimacs_solution, body_sha256, stored_at),
    )


async def get_eval_run_solution(
    conn: aiosqlite.Connection,
    *,
    eval_run_id: str,
) -> dict[str, str] | None:
    """Read the sidecar body for an eval_run, or None if missing/pruned.

    Used by the bearer-gated audit endpoint. NEVER call from a public
    read path — bodies are private (see issue #242).
    """
    cur = await conn.execute(
        "SELECT eval_run_id, dimacs_solution, body_sha256, stored_at "
        "FROM eval_run_solutions WHERE eval_run_id = ?",
        (eval_run_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return {
        "eval_run_id": str(row[0]),
        "dimacs_solution": str(row[1]),
        "body_sha256": str(row[2]),
        "stored_at": str(row[3]),
    }


def _task_family_storage_from_signed_row(
    signed_row: dict[str, Any],
    *,
    family_id: str,
    challenge_id: str,
    cnf_sha256: str,
    dimacs_solution_sha256: str,
    time_limit_seconds: int,
    miner_hotkey: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Build eval_runs storage JSON from a signed schema-5 row."""
    import blake3

    from cathedral.v1_types import canonical_json

    output_card_json = {
        "task_type": family_id,
        "task_id_public": signed_row["task_id_public"],
        "difficulty_tier": signed_row["difficulty_tier"],
        "weighted_score": signed_row["weighted_score"],
        "rejection_reason": signed_row.get("rejection_reason"),
        "worker_owner_hotkey": miner_hotkey,
    }
    output_card_hash = blake3.blake3(canonical_json(output_card_json)).hexdigest()
    task_json = {
        "task_type": family_id,
        "task_id_public": signed_row["task_id_public"],
        "epoch_salt": signed_row["epoch_salt"],
        "difficulty_tier": signed_row["difficulty_tier"],
        "time_limit_seconds": int(time_limit_seconds),
        "answer_hash": signed_row["answer_hash"],
        "verifier_details_hash": signed_row["verifier_details_hash"],
        "challenge_id": challenge_id,
        "cnf_sha256": cnf_sha256,
        "miner_solution_sha256": dimacs_solution_sha256,
    }
    return task_json, output_card_json, output_card_hash


async def list_pending_sat_attest(
    conn: aiosqlite.Connection,
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """List eval_runs awaiting async SAT attestation.

    Joined against ``agent_submissions`` so the attest worker has the
    ssh coordinates it needs. Ordered oldest-first so the queue drains
    in arrival order.
    """
    cur = await conn.execute(
        """
        SELECT er.id, er.submission_id, er.task_json, er.ran_at,
               sub.miner_hotkey, sub.ssh_host, sub.ssh_port, sub.ssh_user,
               sub.card_id
        FROM eval_runs er
        JOIN agent_submissions sub ON sub.id = er.submission_id
        WHERE er.attestation_status = 'pending'
        ORDER BY er.ran_at ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cur.fetchall()
    return [
        {
            "eval_run_id": str(r[0]),
            "submission_id": str(r[1]),
            "task_json": json.loads(r[2]) if r[2] else {},
            "ran_at_iso": str(r[3]),
            "miner_hotkey": str(r[4]),
            "ssh_host": str(r[5]) if r[5] else None,
            "ssh_port": int(r[6]) if r[6] is not None else None,
            "ssh_user": str(r[7]) if r[7] else None,
            "card_id": str(r[8]),
        }
        for r in rows
    ]


async def mark_sat_attest_passed(
    conn: aiosqlite.Connection,
    *,
    eval_run_id: str,
) -> None:
    """Mark a pending eval_run as attested.

    Direct solve rows are already signed and ranked before this worker
    runs; attest only updates the audit status.
    """
    await conn.execute(
        "UPDATE eval_runs "
        "SET attestation_status = 'attested' "
        "WHERE id = ? AND attestation_status = 'pending'",
        (eval_run_id,),
    )
    await conn.execute(
        "UPDATE agent_submissions SET status = 'ranked' "
        "WHERE id = (SELECT submission_id FROM eval_runs WHERE id = ?) "
        "  AND status IN ('valid_attestation_pending', 'ranked')",
        (eval_run_id,),
    )
    await conn.commit()


async def mark_sat_attest_failed(
    conn: aiosqlite.Connection,
    *,
    eval_run_id: str,
    reason: str,
) -> None:
    """Mark a pending eval_run as attest-failed without revoking payment.

    The direct solve was already mathematically verified and signed. SSH
    attest is an audit signal, not a payment gate; mutating weighted_score
    would also invalidate the existing schema-5 signature.
    """
    await conn.execute(
        "UPDATE eval_runs "
        "SET attestation_status = 'failed', errors = ? "
        "WHERE id = ? AND attestation_status = 'pending'",
        (json.dumps([f"attest_failed: {reason}"]), eval_run_id),
    )
    await conn.commit()
