from __future__ import annotations

import sqlite3

import pytest

from cathedral.validator.db import connect


@pytest.mark.asyncio
async def test_status_widen_preserves_v13_sat_order_columns(tmp_path) -> None:
    db_path = tmp_path / "validator.db"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(
            """
            CREATE TABLE card_definitions (
                id              TEXT PRIMARY KEY,
                display_name    TEXT NOT NULL,
                jurisdiction    TEXT NOT NULL,
                topic           TEXT NOT NULL,
                description     TEXT NOT NULL,
                eval_spec_md    TEXT NOT NULL,
                source_pool     TEXT NOT NULL,
                task_templates  TEXT NOT NULL,
                scoring_rubric  TEXT NOT NULL,
                refresh_cadence_hours INTEGER NOT NULL DEFAULT 24,
                status          TEXT NOT NULL CHECK (status IN ('active','archived')),
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        raw.execute(
            """
            INSERT INTO card_definitions (
                id, display_name, jurisdiction, topic, description,
                eval_spec_md, source_pool, task_templates, scoring_rubric,
                refresh_cadence_hours, status, created_at, updated_at
            ) VALUES (
                'synthetic_boolean_v1', 'SAT', 'global', 'sat', 'sat',
                '', '', '[]', '{}', 24, 'active',
                '2026-05-28T00:00:00.000Z', '2026-05-28T00:00:00.000Z'
            )
            """
        )
        raw.execute(
            """
            CREATE TABLE agent_submissions (
                id                       TEXT PRIMARY KEY,
                miner_hotkey             TEXT NOT NULL,
                card_id                  TEXT NOT NULL,
                bundle_blob_key          TEXT NOT NULL,
                bundle_hash              TEXT NOT NULL,
                bundle_size_bytes        INTEGER NOT NULL,
                encryption_key_id        TEXT NOT NULL,
                bundle_signature         TEXT NOT NULL,
                display_name             TEXT NOT NULL,
                bio                      TEXT,
                logo_url                 TEXT,
                soul_md_preview          TEXT,
                metadata_fingerprint     TEXT NOT NULL,
                similarity_check_passed  INTEGER NOT NULL,
                rejection_reason         TEXT,
                submitted_at             TEXT NOT NULL,
                sat_challenge_id         TEXT,
                seq_no                   INTEGER,
                status                   TEXT NOT NULL CHECK (status IN
                                           ('pending_check','queued','evaluating',
                                            'ranked','rejected','withdrawn',
                                            'discovery')),
                current_score            REAL,
                current_rank             INTEGER,
                first_mover_at           TEXT,
                attestation_mode         TEXT NOT NULL DEFAULT 'polaris'
                                         CHECK (attestation_mode IN
                                           ('bundle','polaris','polaris-deploy',
                                            'ssh-probe','tee','unverified')),
                attestation_type         TEXT,
                attestation_blob         BLOB,
                attestation_verified_at  TEXT,
                discovery_only           INTEGER NOT NULL DEFAULT 0,
                ssh_host                 TEXT,
                ssh_port                 INTEGER,
                ssh_user                 TEXT,
                hermes_port              INTEGER
            )
            """
        )
        # Same miner/card/bundle across two SAT challenges is valid in V3.
        # The status-CHECK rebuild must not recreate the old global unique
        # index transiently, or this migration fails.
        for idx, challenge_id in enumerate(("sat-a", "sat-b"), start=1):
            raw.execute(
                """
                INSERT INTO agent_submissions (
                    id, miner_hotkey, card_id, bundle_blob_key, bundle_hash,
                    bundle_size_bytes, encryption_key_id, bundle_signature,
                    display_name, metadata_fingerprint, similarity_check_passed,
                    submitted_at, sat_challenge_id, seq_no, status,
                    attestation_mode, discovery_only
                ) VALUES (?, 'hk', 'synthetic_boolean_v1', '', 'bundle',
                          0, '', 'sig', 'miner', '', 1,
                          '2026-05-28T00:00:00.000Z', ?, ?, 'ranked',
                          'ssh-probe', 0)
                """,
                (f"sub-{idx}", challenge_id, idx),
            )
        raw.commit()
    finally:
        raw.close()

    conn = await connect(str(db_path))
    try:
        cur = await conn.execute("PRAGMA table_info(agent_submissions)")
        cols = {row[1] for row in await cur.fetchall()}
        assert {"sat_challenge_id", "seq_no"} <= cols

        cur = await conn.execute(
            "SELECT id, sat_challenge_id, seq_no FROM agent_submissions ORDER BY id"
        )
        rows = await cur.fetchall()
        assert rows == [("sub-1", "sat-a", 1), ("sub-2", "sat-b", 2)]

        # The widened status is actually accepted after the rebuild.
        await conn.execute(
            "UPDATE agent_submissions SET status = 'pending_solution' WHERE id = 'sub-1'"
        )
        await conn.commit()
    finally:
        await conn.close()
