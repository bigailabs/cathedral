"""Sqlite schema, connection, and migrations.

The validator is a single writer; readers are tolerated. WAL mode keeps the
HTTP path lock-free against the verification worker.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_hotkey TEXT NOT NULL,
    owner_wallet TEXT NOT NULL,
    work_unit TEXT NOT NULL,
    polaris_agent_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','verifying','verified','rejected')),
    rejection_reason TEXT,
    submitted_at TEXT NOT NULL,
    verified_at TEXT,
    UNIQUE(miner_hotkey, work_unit, polaris_agent_id)
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);

CREATE TABLE IF NOT EXISTS evidence_bundles (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
    bundle_json TEXT NOT NULL,
    filtered_usage_count INTEGER NOT NULL,
    verified_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    claim_id INTEGER PRIMARY KEY REFERENCES claims(id) ON DELETE CASCADE,
    miner_hotkey TEXT NOT NULL,
    source_quality REAL NOT NULL,
    freshness REAL NOT NULL,
    specificity REAL NOT NULL,
    usefulness REAL NOT NULL,
    clarity REAL NOT NULL,
    maintenance REAL NOT NULL,
    weighted REAL NOT NULL,
    scored_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_hotkey_time ON scores(miner_hotkey, scored_at DESC);

CREATE TABLE IF NOT EXISTS health_kv (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Cards live on Cathedral (not Polaris). One row per (card_id, miner_hotkey)
-- — the miner's latest verified version of that card. The validator writes
-- here on every verified claim; cathedral.computer reads from here to
-- display "what miner X currently says about card Y."
CREATE TABLE IF NOT EXISTS cards (
    card_id TEXT NOT NULL,
    miner_hotkey TEXT NOT NULL,
    polaris_agent_id TEXT NOT NULL,
    owner_wallet TEXT NOT NULL,
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    card_json TEXT NOT NULL,
    weighted_score REAL NOT NULL,
    last_refreshed_at TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    PRIMARY KEY (card_id, miner_hotkey)
);
CREATE INDEX IF NOT EXISTS idx_cards_card_id ON cards(card_id);
CREATE INDEX IF NOT EXISTS idx_cards_verified_at ON cards(verified_at DESC);

-- ----------------------------------------------------------------------
-- V1 launch tables (CONTRACTS.md Section 3)
-- ----------------------------------------------------------------------

-- Curated card metadata. Populated by ops from the cathedral-eval-spec
-- content repo. Read by every public read endpoint and by the eval
-- task generator.
CREATE TABLE IF NOT EXISTS card_definitions (
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
);

-- Miner-uploaded agent submissions. Bundles live encrypted in Hippius.
--
-- `attestation_mode` branches at intake (see cathedral.publisher.submit):
--   * 'polaris'        — Cathedral re-runs eval on a Polaris-managed
--                        runtime (legacy cathedral-runtime image). No
--                        miner-side attestation needed at submission time.
--                        Kept as a backup during v2 migration.
--   * 'polaris-deploy' — v2 paid. Polaris deploys the canonical Hermes
--                        runtime against the miner's bundle and Cathedral
--                        drives /chat. Manifest is fetched + verified;
--                        no per-task attestation needed because the
--                        deployment is the unit of trust.
--   * 'ssh-probe'      — v2 free. Miner runs Hermes themselves on any
--                        host with SSH access; Cathedral SSHs in, queries
--                        the local /chat, leaves. No Polaris attestation
--                        chain, no 1.10x verified-runtime multiplier.
--                        `ssh_host`, `ssh_port`, `ssh_user`, `hermes_port`
--                        come in via the submit-starter form.
--   * 'tee'            — Miner attached a TEE attestation document
--                        (Nitro/TDX/SEV-SNP). `attestation_blob` carries
--                        the raw bytes; `attestation_type` carries the
--                        verifier label; `attestation_verified_at`
--                        records when Cathedral verified it.
--   * 'unverified'     — Discovery-only. No eval is run, no score
--                        persisted. `status` is 'discovery';
--                        `discovery_only` is true.
CREATE TABLE IF NOT EXISTS agent_submissions (
    id                       TEXT PRIMARY KEY,
    miner_hotkey             TEXT NOT NULL,
    card_id                  TEXT NOT NULL REFERENCES card_definitions(id),
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
    status                   TEXT NOT NULL CHECK (status IN
                               ('pending_check','queued','evaluating',
                                'ranked','rejected','withdrawn',
                                'discovery')),
    current_score            REAL,
    current_rank             INTEGER,
    first_mover_at           TEXT,
    attestation_mode         TEXT NOT NULL DEFAULT 'polaris'
                             CHECK (attestation_mode IN
                               ('bundle','polaris','polaris-deploy','ssh-probe','tee','unverified')),
    attestation_type         TEXT,
    attestation_blob         BLOB,
    attestation_verified_at  TEXT,
    discovery_only           INTEGER NOT NULL DEFAULT 0,
    -- ssh-probe coordinates (free tier). Nullable; only set when
    -- attestation_mode='ssh-probe'. See ssh_probe_runner.SshProbeRunner.
    ssh_host                 TEXT,
    ssh_port                 INTEGER,
    ssh_user                 TEXT,
    -- DEPRECATED v1.1.0 (cathedralai/cathedral#75, PR #77). New rows
    -- write NULL; v1.0.x rows keep their port value. Removable in
    -- v1.2.0 once all historical rows have aged out of the 30-day
    -- rolling window. Search marker: `removable-in-v1-2-0`.
    hermes_port              INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_unique
    ON agent_submissions(miner_hotkey, card_id, bundle_hash);
CREATE INDEX IF NOT EXISTS idx_agent_card_status
    ON agent_submissions(card_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_card_score
    ON agent_submissions(card_id, current_score DESC);
CREATE INDEX IF NOT EXISTS idx_agent_first_mover
    ON agent_submissions(card_id, first_mover_at);

-- Each individual eval execution.
--
-- `polaris_verified` is true when the eval ran on a Polaris-managed
-- runtime (manifest fetched and verified). False for BYO-compute miners
-- and for failed Polaris runs. The verified-runtime multiplier
-- (CONTRACTS.md §7.3) is applied at scoring time before the row is
-- inserted; this column persists the verification status for downstream
-- audit and frontend display.
CREATE TABLE IF NOT EXISTS eval_runs (
    id                  TEXT PRIMARY KEY,
    submission_id       TEXT NOT NULL REFERENCES agent_submissions(id) ON DELETE CASCADE,
    epoch               INTEGER NOT NULL,
    round_index         INTEGER NOT NULL,
    polaris_agent_id    TEXT NOT NULL,
    polaris_run_id      TEXT NOT NULL,
    task_json           TEXT NOT NULL,
    output_card_json    TEXT NOT NULL,
    output_card_hash    TEXT NOT NULL,
    score_parts         TEXT NOT NULL,
    weighted_score      REAL NOT NULL,
    ran_at              TEXT NOT NULL,
    duration_ms         INTEGER NOT NULL,
    errors              TEXT,
    cathedral_signature TEXT NOT NULL,
    polaris_verified    INTEGER NOT NULL DEFAULT 0,
    polaris_attestation TEXT,
    -- v1.1.0 eval-output schema split (cathedralai/cathedral#75 PR 4).
    -- Populated alongside output_card_json during the 48h dual-publish
    -- window. After cutover (CATHEDRAL_EMIT_V2_SIGNED_PAYLOAD=true)
    -- new rows still set output_card_json + output_card_hash for
    -- back-compat reads on a hypothetical v1.0.x validator pulling
    -- mid-cutover. v1.2.0 will drop the legacy columns.
    eval_card_excerpt           TEXT,
    eval_artifact_manifest_hash TEXT,
    eval_artifact_bundle_url    TEXT,
    eval_artifact_manifest_url  TEXT,
    eval_output_schema_version  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_eval_submission_time
    ON eval_runs(submission_id, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_epoch ON eval_runs(epoch);
CREATE INDEX IF NOT EXISTS idx_eval_card_score
    ON eval_runs(submission_id, weighted_score DESC);
CREATE INDEX IF NOT EXISTS idx_eval_ran_at ON eval_runs(ran_at DESC);
-- v1.1.0: composite ascending index covering the leaderboard/recent
-- tuple-cursor scan (WHERE (ran_at, id) > (?, ?) ORDER BY ran_at, id).
-- See 2026-05-12-track-3-pull-cursor-audit.md and
-- list_eval_runs_recent() in cathedral.publisher.repository.
CREATE INDEX IF NOT EXISTS idx_eval_ran_at_id
    ON eval_runs(ran_at ASC, id ASC);

-- Weekly Merkle anchors of eval results.
CREATE TABLE IF NOT EXISTS merkle_anchors (
    epoch                    INTEGER PRIMARY KEY,
    merkle_root              TEXT NOT NULL,
    eval_count               INTEGER NOT NULL,
    leaf_hashes_json         TEXT NOT NULL,
    computed_at              TEXT NOT NULL,
    on_chain_block           INTEGER,
    on_chain_extrinsic_index INTEGER
);

CREATE TABLE IF NOT EXISTS eval_run_to_epoch (
    eval_run_id TEXT PRIMARY KEY REFERENCES eval_runs(id) ON DELETE CASCADE,
    epoch       INTEGER NOT NULL REFERENCES merkle_anchors(epoch)
);
CREATE INDEX IF NOT EXISTS idx_eval_run_epoch ON eval_run_to_epoch(epoch);

-- v2 scorer audit log (cathedralai/cathedral#156).
-- Durable forensic trail for v2-shaped eval rows. Written in its own
-- commit boundary BEFORE the main eval_runs INSERT so the row survives
-- any rollback in downstream scoring/persistence. Intentionally has no
-- foreign key on eval_runs.id: the audit row anchors what we INTENDED
-- to score even when the eval row never lands.
CREATE TABLE IF NOT EXISTS eval_runs_audit (
    audit_id           TEXT PRIMARY KEY,
    eval_run_id        TEXT NOT NULL,
    submission_id      TEXT NOT NULL,
    miner_hotkey       TEXT NOT NULL,
    card_id            TEXT NOT NULL,
    schema_version     INTEGER NOT NULL,
    manifest_hash      TEXT,
    bundle_url         TEXT,
    weighted_score_pre REAL,
    written_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_audit_run
    ON eval_runs_audit(eval_run_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_audit_submission
    ON eval_runs_audit(submission_id, written_at DESC);
"""


async def connect(database_path: str) -> aiosqlite.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(database_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SCHEMA)
    await _apply_migrations(conn)
    await conn.commit()
    return conn


async def _apply_migrations(conn: aiosqlite.Connection) -> None:
    """Idempotent column additions for tables created by an earlier schema.

    `CREATE TABLE IF NOT EXISTS` is a no-op when the table already exists,
    so new columns must be added via ALTER TABLE here. Each block guards
    on the current column set so re-running the connect() bootstrap is safe.
    """
    # eval_runs.polaris_verified — added for the BYO-compute flow. Defaults
    # to 0 (false) for any rows inserted before the column existed; the
    # scoring pipeline writes the real value going forward.
    cur = await conn.execute("PRAGMA table_info(eval_runs)")
    cols = {row[1] for row in await cur.fetchall()}
    if "polaris_verified" not in cols:
        await conn.execute(
            "ALTER TABLE eval_runs ADD COLUMN polaris_verified INTEGER NOT NULL DEFAULT 0"
        )
    # eval_runs.polaris_attestation — added for the Tier A Polaris-runtime
    # flow. Nullable JSON blob; only populated when the runner returned a
    # verified attestation. Existing rows stay NULL.
    if "polaris_attestation" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN polaris_attestation TEXT")

    # eval_runs.trace_json — added for the v2 Polaris-native Hermes flow.
    # Nullable JSON blob holding the Hermes-emitted trace (tool_calls,
    # model_calls, source_fetches, agentic_loop_depth, start_at, end_at).
    # Stored as an UNSIGNED sidecar: it does NOT participate in the
    # canonical signed bytes (cathedral_signature is computed before the
    # trace is attached), so old validators verify v2 rows unchanged.
    # Promoted to signed in v2.1 once the schema is stable.
    if "trace_json" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN trace_json TEXT")

    # eval_runs.polaris_manifest — v2 deploy runner pulls the signed
    # manifest after the chat round trips so it can be persisted
    # alongside the trace. Nullable; only populated for v2 runs.
    if "polaris_manifest" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN polaris_manifest TEXT")

    # v1.1.0 eval-output schema split (cathedralai/cathedral#75 PR 4).
    # Splits the legacy `output_card_json` blob into three addressable
    # fields so the validator pull layer can reference the (small)
    # excerpt, the (signed) manifest hash, and the (unsigned) bundle
    # URL independently.
    #
    # The split is additive: legacy rows keep their `output_card_json`,
    # `output_card_hash` columns; new rows populate both legacy AND new
    # fields during the 48h dual-publish window so a CATHEDRAL_EMIT_V2
    # flip is reversible without re-running evals.
    #
    # `eval_output_schema_version` is the routing hint the validator's
    # `_SIGNED_KEYS_BY_VERSION` dispatcher (validator-compat branch)
    # reads to pick the right key set. Defaults to 1 so historical rows
    # verify under the v1 keyset.
    if "eval_card_excerpt" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN eval_card_excerpt TEXT")
    if "eval_artifact_manifest_hash" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN eval_artifact_manifest_hash TEXT")
    if "eval_artifact_bundle_url" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN eval_artifact_bundle_url TEXT")
    if "eval_artifact_manifest_url" not in cols:
        await conn.execute("ALTER TABLE eval_runs ADD COLUMN eval_artifact_manifest_url TEXT")
    if "eval_output_schema_version" not in cols:
        await conn.execute(
            "ALTER TABLE eval_runs ADD COLUMN eval_output_schema_version INTEGER NOT NULL DEFAULT 1"
        )

    # agent_submissions: attestation_mode branching (polaris/tee/unverified).
    # Existing rows default to 'polaris' so back-compat with pre-attestation
    # miners holds — they were always on the verified path. SQLite cannot
    # add a column WITH a CHECK constraint in ALTER TABLE, so the constraint
    # is enforced at the application layer for existing tables; new tables
    # created via SCHEMA above carry the CHECK natively.
    cur = await conn.execute("PRAGMA table_info(agent_submissions)")
    sub_cols = {row[1] for row in await cur.fetchall()}
    if "attestation_mode" not in sub_cols:
        await conn.execute(
            "ALTER TABLE agent_submissions ADD COLUMN attestation_mode "
            "TEXT NOT NULL DEFAULT 'polaris'"
        )
    if "attestation_type" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN attestation_type TEXT")
    if "attestation_blob" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN attestation_blob BLOB")
    if "attestation_verified_at" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN attestation_verified_at TEXT")
    if "discovery_only" not in sub_cols:
        await conn.execute(
            "ALTER TABLE agent_submissions ADD COLUMN discovery_only INTEGER NOT NULL DEFAULT 0"
        )

    # agent_submissions.attestation_mode CHECK constraint widening.
    # SQLite cannot ALTER a CHECK in place; tables created before v2
    # carry the old constraint `IN ('polaris','tee','unverified')` which
    # rejects 'polaris-deploy' and 'ssh-probe' inserts with
    # "CHECK constraint failed". Detection: read sqlite_master for the
    # table's CREATE statement and look for the narrow tuple. If found,
    # rebuild the table with the widened constraint.
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_submissions'"
    )
    row = await cur.fetchone()
    create_sql = (row[0] if row else "") or ""
    needs_widen = "attestation_mode IN" in create_sql and "'polaris-deploy'" not in create_sql
    if needs_widen:
        await _widen_attestation_mode_check(conn)

    # ssh-probe free-tier coordinates. Only populated when
    # attestation_mode='ssh-probe'. The publisher's submit-starter form
    # collects these; the SshProbeRunner reads them off the submission row.
    # Re-read columns in case the table was just rebuilt above.
    cur = await conn.execute("PRAGMA table_info(agent_submissions)")
    sub_cols = {row[1] for row in await cur.fetchall()}
    if "ssh_host" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN ssh_host TEXT")
    if "ssh_port" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN ssh_port INTEGER")
    if "ssh_user" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN ssh_user TEXT")
    # DEPRECATED v1.1.0 (cathedralai/cathedral#75, PR #77). The column is
    # kept so v1.0.x historical rows preserve their port value through
    # the transition; new rows always write NULL. Removable in v1.2.0
    # once all historical rows have aged out of the 30-day rolling
    # window. Search marker: `removable-in-v1-2-0`.
    if "hermes_port" not in sub_cols:
        await conn.execute("ALTER TABLE agent_submissions ADD COLUMN hermes_port INTEGER")

    # v1.1.0: composite ascending index for the tuple-cursor leaderboard
    # scan. SQLite's `CREATE INDEX IF NOT EXISTS` is idempotent, so it's
    # safe to run on every connect. Existing publisher DBs upgrading from
    # v1.0.x only have idx_eval_ran_at (DESC); the new ASC composite is
    # required for efficient row-value comparison scans.
    # See 2026-05-12-track-3-pull-cursor-audit.md.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_ran_at_id ON eval_runs(ran_at ASC, id ASC)"
    )

    # Tier A drain (cathedralai/cathedral#70). When the publisher boots
    # with CATHEDRAL_ENABLE_POLARIS_DEPLOY unset/false, sweep any
    # in-flight Tier A submissions to 'rejected' so the eval orchestrator
    # doesn't keep retrying them against a runner that's gated off at
    # the submit boundary. Existing 'ranked' rows are untouched — their
    # scores are real receipts. Idempotent: re-running matches zero rows
    # on a clean DB. Re-entry: flipping the flag back to true does NOT
    # un-reject these rows; miners resubmit if they want to retry.
    import os as _os

    _tier_a_enabled = _os.environ.get("CATHEDRAL_ENABLE_POLARIS_DEPLOY", "").lower() == "true"
    if not _tier_a_enabled:
        await conn.execute(
            "UPDATE agent_submissions "
            "SET status = 'rejected', "
            "    rejection_reason = 'tier_a_disabled_for_v1' "
            "WHERE status IN ('queued', 'evaluating') "
            "  AND attestation_mode IN ('polaris', 'polaris-deploy')"
        )


async def _widen_attestation_mode_check(conn: aiosqlite.Connection) -> None:
    """Rebuild agent_submissions with the widened attestation_mode CHECK.

    SQLite does not support ALTER TABLE to modify a CHECK constraint, so
    we follow the canonical 12-step alter procedure:
    https://www.sqlite.org/lang_altertable.html#otheralter

    1. Disable foreign keys (we have none on agent_submissions)
    2. CREATE TABLE agent_submissions_new with the new schema
    3. INSERT INTO ... SELECT FROM the old table
    4. DROP the old table
    5. RENAME the new table to the canonical name
    6. Recreate indexes

    The new schema mirrors the canonical SCHEMA above. Any columns the
    old table had but the new schema doesn't are dropped silently; any
    new columns the new schema has get NULL defaults. Caller is
    expected to follow up with the column ALTERs for ssh_*.
    """
    import structlog as _structlog

    _log = _structlog.get_logger(__name__)
    _log.info("widen_attestation_mode_check_start")

    # Step 2: create the replacement table. Carries the full new
    # constraint set including 'polaris-deploy' and 'ssh-probe', plus
    # the ssh_* columns inlined. Keep column order matching the
    # canonical SCHEMA so the INSERT...SELECT works without column
    # listing on the SELECT side.
    await conn.execute(
        """
        CREATE TABLE agent_submissions_new (
            id                       TEXT PRIMARY KEY,
            miner_hotkey             TEXT NOT NULL,
            card_id                  TEXT NOT NULL REFERENCES card_definitions(id),
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
            status                   TEXT NOT NULL CHECK (status IN
                                       ('pending_check','queued','evaluating',
                                        'ranked','rejected','withdrawn',
                                        'discovery')),
            current_score            REAL,
            current_rank             INTEGER,
            first_mover_at           TEXT,
            attestation_mode         TEXT NOT NULL DEFAULT 'polaris'
                                     CHECK (attestation_mode IN
                                       ('bundle','polaris','polaris-deploy','ssh-probe','tee','unverified')),
            attestation_type         TEXT,
            attestation_blob         BLOB,
            attestation_verified_at  TEXT,
            discovery_only           INTEGER NOT NULL DEFAULT 0,
            ssh_host                 TEXT,
            ssh_port                 INTEGER,
            ssh_user                 TEXT,
            -- DEPRECATED v1.1.0 (#75, PR #77). Removable in v1.2.0.
            -- Search marker: `removable-in-v1-2-0`.
            hermes_port              INTEGER
        )
        """
    )

    # Step 3: copy. The old table may or may not have ssh_* columns
    # depending on which migration ran first; the new table has them,
    # so we list the carry-over columns explicitly.
    cur = await conn.execute("PRAGMA table_info(agent_submissions)")
    old_cols = {row[1] for row in await cur.fetchall()}
    carry_cols = [
        "id",
        "miner_hotkey",
        "card_id",
        "bundle_blob_key",
        "bundle_hash",
        "bundle_size_bytes",
        "encryption_key_id",
        "bundle_signature",
        "display_name",
        "bio",
        "logo_url",
        "soul_md_preview",
        "metadata_fingerprint",
        "similarity_check_passed",
        "rejection_reason",
        "submitted_at",
        "status",
        "current_score",
        "current_rank",
        "first_mover_at",
        "attestation_mode",
        "attestation_type",
        "attestation_blob",
        "attestation_verified_at",
        "discovery_only",
    ]
    # ssh_* may not exist on the old table — only carry them if so.
    for col in ("ssh_host", "ssh_port", "ssh_user", "hermes_port"):
        if col in old_cols:
            carry_cols.append(col)
    cols_sql = ", ".join(carry_cols)
    await conn.execute(
        f"INSERT INTO agent_submissions_new ({cols_sql}) SELECT {cols_sql} FROM agent_submissions"
    )

    # Step 4: drop the old table (drops associated indexes too).
    await conn.execute("DROP TABLE agent_submissions")

    # Step 5: rename.
    await conn.execute("ALTER TABLE agent_submissions_new RENAME TO agent_submissions")

    # Step 6: recreate indexes (matches canonical SCHEMA).
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_unique "
        "ON agent_submissions(miner_hotkey, card_id, bundle_hash)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_card_status ON agent_submissions(card_id, status)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_card_score "
        "ON agent_submissions(card_id, current_score DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_first_mover "
        "ON agent_submissions(card_id, first_mover_at)"
    )

    _log.info("widen_attestation_mode_check_done")
