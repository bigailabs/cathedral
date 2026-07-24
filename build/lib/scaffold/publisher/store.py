"""Storage for the thin publisher — dual backend (SQLite | Postgres) behind ONE
interface so app.py and the loops are dialect-agnostic.

Backends
--------
* **SQLite** (default, offline, all gates): a single connection behind one write
  lock — the production DB discipline (issue #109 tuple cursor, durable backfill
  marker, no destructive migrations). `write()` opens BEGIN IMMEDIATE.
* **Postgres** (set DATABASE_URL=postgres[ql]://… or pass that as database_path):
  a psycopg2 connection pool. MVCC means readers never block writers and the
  wedge class dies, so NO global write lock — each `write()` takes a pooled
  connection and runs a normal transaction. This is the keystone fix.

The trick that keeps the rest of the codebase untouched: app.py / scoring.py /
refill.py / seed_live.py all write SQLite-flavoured SQL inline through
`conn.execute(sql, params)` (cursor with .rowcount / .fetchone(), rows indexable
by name and position). In Postgres mode the pooled connection is wrapped by
`_PgConn`, which translates that SQLite SQL to Postgres on the fly:
  *  ?            -> %s             (paramstyle)
  *  INSERT OR IGNORE INTO t(...)   -> INSERT INTO t(...) ... ON CONFLICT DO NOTHING
  *  INSERT OR REPLACE INTO t(cols) -> INSERT ... ON CONFLICT (pk) DO UPDATE SET …
  *  datetime('now')               -> now()
The translation is intentionally narrow — only the forms this codebase actually
emits — and is unit-pinned by postgres_verify.py against a real database.

Tables
------
  eval_runs          — the signed feed rows validators pull (the frozen surface).
  lane_challenges    — Lane A SAT challenges (CNF held server-side).
  agent_submissions  — Lane A submit ledger (dedup, rate-limit, per-challenge lock).
  arena_solvers      — Lane S solver registry.
  arena_instances    — Lane I breaker instances.
  lane_challenge_solves — distinct-solver ledger (retirement + weight composition).
  submit_signatures  — replay dedup.
  seed_state         — durable live-seed watermark.
  schema_migrations  — applied migration ids (idempotency marker).

Cursor: rows are pulled by the strict tuple (ran_at, id) > (since_ran_at,
since_id) ordering — the exact semantics released validators use.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from typing import Any


def _postgres_statement_timeout_ms() -> int:
    """Bound pathological read queries when configured.

    This intentionally returns only a validated integer so the caller can use a
    tiny f-string for the PostgreSQL SET statement without carrying user text
    into SQL.
    """
    raw = (os.environ.get("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS") or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# Migrations.
#
# SQLite migrations stay exactly as released (additive, idempotent). Postgres
# needs the same logical schema with portable DDL — kept as a parallel list so a
# faithful SQLite migration is never mutated. The two produce equivalent tables;
# both are CREATE … IF NOT EXISTS / ADD COLUMN IF NOT EXISTS so re-runs are no-ops.
# Never edit an applied migration — append.
# ---------------------------------------------------------------------------
_MIGRATIONS: list[tuple[str, str]] = [
    ("0001_eval_runs", """
        CREATE TABLE IF NOT EXISTS eval_runs (
            id TEXT PRIMARY KEY,
            ran_at TEXT NOT NULL,
            eval_output_schema_version INTEGER NOT NULL,
            miner_hotkey TEXT NOT NULL,
            task_type TEXT NOT NULL,
            row_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eval_runs_cursor ON eval_runs(ran_at, id);
    """),
    ("0002_lane_challenges", """
        CREATE TABLE IF NOT EXISTS lane_challenges (
            challenge_id TEXT PRIMARY KEY,
            family_id TEXT NOT NULL,
            tier INTEGER NOT NULL,
            cnf_text TEXT NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            cnf_bytes INTEGER NOT NULL,
            num_vars INTEGER NOT NULL,
            num_clauses INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            score_multiplier REAL NOT NULL DEFAULT 1.0,
            difficulty_label TEXT,
            designated_solver_digest TEXT,
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0003_agent_submissions", """
        CREATE TABLE IF NOT EXISTS agent_submissions (
            id TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            sat_challenge_id TEXT NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            current_score REAL,
            seq_no INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            signature TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub_hotkey_chal
            ON agent_submissions(miner_hotkey, sat_challenge_id);
    """),
    ("0004_arena_solvers", """
        CREATE TABLE IF NOT EXISTS arena_solvers (
            source_sha256 TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            container_digest TEXT NOT NULL,
            owner_hotkey TEXT NOT NULL,
            registered_round INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0005_arena_instances", """
        CREATE TABLE IF NOT EXISTS arena_instances (
            instance_id TEXT PRIMARY KEY,
            owner_hotkey TEXT NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            submitted_round INTEGER NOT NULL,
            quarantine_until_round INTEGER NOT NULL,
            min_batch_score REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0006_replay_dedup", """
        CREATE TABLE IF NOT EXISTS submit_signatures (
            signature TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );
    """),
    ("0007_seed_state", """
        CREATE TABLE IF NOT EXISTS seed_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """),
    ("0008_lane_challenges_source", """
        ALTER TABLE lane_challenges ADD COLUMN cnf_source TEXT NOT NULL DEFAULT 'local';
    """),
    ("0009_lane_challenges_cnf_url", """
        ALTER TABLE lane_challenges ADD COLUMN cnf_url TEXT;
    """),
    ("0010_lane_challenge_solves", """
        CREATE TABLE IF NOT EXISTS lane_challenge_solves (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            solved_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
    """),
    ("0011_lane_challenges_updated_at", """
        ALTER TABLE lane_challenges ADD COLUMN updated_at_iso TEXT;
    """),
    ("0012_arena_instances_payout", """
        ALTER TABLE arena_instances ADD COLUMN cnf_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE arena_instances ADD COLUMN last_paid_round INTEGER;
        ALTER TABLE arena_instances ADD COLUMN paid_at_iso TEXT;
    """),
    ("0013_weight_policy_state", """
        CREATE TABLE IF NOT EXISTS weight_policy_state (
            id INTEGER PRIMARY KEY,
            last_policy_version INTEGER NOT NULL,
            updated_at_iso TEXT NOT NULL
        );
    """),
    ("0015_eval_runs_miner_idx", """
        CREATE INDEX IF NOT EXISTS idx_eval_runs_miner_ran_at ON eval_runs(miner_hotkey, ran_at);
    """),
    # 0016: per-miner unique challenges (CATHEDRAL_PERMINER_ENABLED).
    # Safe to apply unconditionally — the table is never read/written when the
    # flag is off (flag-off = zero behaviour change).
    ("0016_per_miner_solves", """
        CREATE TABLE IF NOT EXISTS per_miner_solves (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            difficulty_weight REAL NOT NULL DEFAULT 0.0,
            verified INTEGER NOT NULL DEFAULT 0,
            solved_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_epoch_hotkey
            ON per_miner_solves(epoch, miner_hotkey);
    """),
    # 0017: index on lane_challenge_solves.solved_at_iso so compose_scores
    # (SELECT DISTINCT miner_hotkey, challenge_id WHERE solved_at_iso > ?)
    # uses an index scan instead of a full table scan. Without this index the
    # weight-vector build takes 3-5s on large tables (full scan every 60s TTL).
    ("0017_lane_challenge_solves_solved_at_idx", """
        CREATE INDEX IF NOT EXISTS idx_lane_challenge_solves_solved_at
            ON lane_challenge_solves(solved_at_iso);
    """),
    # 0018: per-miner evidence ledgers. Attempts are every accepted/rejected
    # assigned submission; witnesses keep the accepted solution body so audit
    # family assignments can be decoded/replayed later.
    ("0018_per_miner_evidence", """
        CREATE TABLE IF NOT EXISTS per_miner_attempts (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            dimacs_solution_sha256 TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            recorded_at_iso TEXT NOT NULL,
            signature TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_challenge_hotkey
            ON per_miner_attempts(challenge_id, miner_hotkey);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_epoch_hotkey
            ON per_miner_attempts(epoch, miner_hotkey);

        CREATE TABLE IF NOT EXISTS per_miner_assignments (
            challenge_id TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            difficulty_weight REAL NOT NULL DEFAULT 0.0,
            assigned_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_assignments_epoch_hotkey
            ON per_miner_assignments(epoch, miner_hotkey);

        CREATE TABLE IF NOT EXISTS per_miner_witnesses (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            dimacs_solution_sha256 TEXT NOT NULL,
            answer_hash TEXT NOT NULL,
            dimacs_solution TEXT NOT NULL,
            recorded_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
    """),
    # 0019: operator-side metadata for structured audit CNFs. These rows are not
    # public board data; they bind a live CNF to its decode map/manifest hash so
    # accepted witnesses can be replayed by an operator process.
    ("0019_audit_challenge_manifests", """
        CREATE TABLE IF NOT EXISTS audit_challenge_manifests (
            challenge_id TEXT PRIMARY KEY,
            cnf_sha256 TEXT NOT NULL,
            manifest_json TEXT,
            decode_map_json TEXT,
            source_path TEXT,
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0020_eval_runs_legacy_columns", """
        ALTER TABLE eval_runs ADD COLUMN id TEXT;
        ALTER TABLE eval_runs ADD COLUMN ran_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN eval_output_schema_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE eval_runs ADD COLUMN miner_hotkey TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN task_type TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN row_json TEXT NOT NULL DEFAULT '{}';
        CREATE INDEX IF NOT EXISTS idx_eval_runs_cursor ON eval_runs(ran_at, id);
        CREATE INDEX IF NOT EXISTS idx_eval_runs_miner_ran_at ON eval_runs(miner_hotkey, ran_at);
    """),
    ("0021_coldkey_map", """
        CREATE TABLE IF NOT EXISTS coldkey_map (
            hotkey TEXT PRIMARY KEY,
            coldkey TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_coldkey_map_coldkey
            ON coldkey_map(coldkey);
    """),
    # Off-chain TEE GPU capacity intake for provider handoff. These tables are
    # intentionally not scoring inputs: validators never read them.
    ("0022_tee_gpu_capacity", """
        CREATE TABLE IF NOT EXISTS tee_gpu_capacity (
            capacity_id TEXT PRIMARY KEY,
            provider_ref TEXT NOT NULL DEFAULT '',
            owner_hotkey TEXT NOT NULL,
            node_id TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '',
            endpoint_url TEXT NOT NULL DEFAULT '',
            agent_api TEXT NOT NULL DEFAULT '',
            public_ip TEXT NOT NULL DEFAULT '',
            gpu_short_ref TEXT NOT NULL,
            gpu_model TEXT NOT NULL DEFAULT '',
            gpu_count INTEGER NOT NULL,
            gpu_memory_gb INTEGER,
            tee_kind TEXT NOT NULL DEFAULT '',
            tdx_claimed INTEGER NOT NULL DEFAULT 0,
            gpu_cc_claimed INTEGER NOT NULL DEFAULT 0,
            hourly_cost REAL NOT NULL DEFAULT 0.0,
            currency TEXT NOT NULL DEFAULT 'USD',
            attestation_digest TEXT NOT NULL DEFAULT '',
            attestation_json TEXT NOT NULL DEFAULT '{}',
            health_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            preflight_status TEXT NOT NULL DEFAULT 'needs_review',
            preflight_json TEXT NOT NULL DEFAULT '{}',
            chutes_validator_hotkey TEXT NOT NULL DEFAULT '',
            chutes_server_name TEXT NOT NULL DEFAULT '',
            chutes_server_id TEXT NOT NULL DEFAULT '',
            chutes_status TEXT NOT NULL DEFAULT '',
            emissions_eligible INTEGER NOT NULL DEFAULT 0 CHECK (emissions_eligible = 0),
            admin_note TEXT NOT NULL DEFAULT '',
            created_at_iso TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL,
            last_heartbeat_iso TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tee_gpu_capacity_owner_node
            ON tee_gpu_capacity(owner_hotkey, node_id);
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_status
            ON tee_gpu_capacity(status);
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_owner
            ON tee_gpu_capacity(owner_hotkey);
    """),
    ("0023_tee_gpu_capacity_events", """
        CREATE TABLE IF NOT EXISTS tee_gpu_capacity_events (
            id TEXT PRIMARY KEY,
            capacity_id TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            event_json TEXT NOT NULL DEFAULT '{}',
            created_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_events_capacity
            ON tee_gpu_capacity_events(capacity_id, created_at_iso);
    """),
    ("0024_tee_gpu_capacity_authorization", """
        ALTER TABLE tee_gpu_capacity ADD COLUMN operator_use_authorized INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tee_gpu_capacity ADD COLUMN authorization_version TEXT NOT NULL DEFAULT '';
        ALTER TABLE tee_gpu_capacity ADD COLUMN authorization_digest TEXT NOT NULL DEFAULT '';
        ALTER TABLE tee_gpu_capacity ADD COLUMN authorization_json TEXT NOT NULL DEFAULT '{}';
    """),
    ("0025_solver_attestation", """
        CREATE TABLE IF NOT EXISTS attest_nonces (
            nonce TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            issued_at_iso TEXT NOT NULL,
            expires_at_iso TEXT NOT NULL,
            used_at_iso TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attest_nonces_miner_challenge
            ON attest_nonces(miner_hotkey, challenge_id);
        CREATE TABLE IF NOT EXISTS attestations (
            id TEXT PRIMARY KEY,
            eval_run_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            solver_digest TEXT NOT NULL,
            multiplier REAL NOT NULL,
            verified_at_iso TEXT NOT NULL,
            quote_digest TEXT NOT NULL DEFAULT '',
            receipt_digest TEXT NOT NULL DEFAULT '',
            verifier_backend TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_attestations_eval_run
            ON attestations(eval_run_id);
        CREATE INDEX IF NOT EXISTS idx_attestations_miner_challenge
            ON attestations(miner_hotkey, challenge_id);
        ALTER TABLE eval_runs ADD COLUMN attested INTEGER NOT NULL DEFAULT 0;
    """),
    ("0026_attest_nonce_pubkey", """
        ALTER TABLE attest_nonces ADD COLUMN miner_pubkey_b64 TEXT NOT NULL DEFAULT '';
    """),
    ("0027_per_miner_time_indexes", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_solved_at
            ON per_miner_solves(solved_at_iso);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_recorded_at
            ON per_miner_attempts(recorded_at_iso);
    """),
    ("0028_metagraph_hotkeys", """
        CREATE TABLE IF NOT EXISTS metagraph_hotkeys (
            network TEXT NOT NULL,
            netuid INTEGER NOT NULL,
            hotkey TEXT NOT NULL,
            uid INTEGER,
            coldkey TEXT NOT NULL DEFAULT '',
            block INTEGER,
            updated_at_iso TEXT NOT NULL,
            PRIMARY KEY (network, netuid, hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_metagraph_hotkeys_fresh
            ON metagraph_hotkeys(network, netuid, updated_at_iso);
    """),
    ("0029_signed_weight_vectors", """
        CREATE TABLE IF NOT EXISTS signed_weight_vectors (
            id TEXT PRIMARY KEY,
            generated_at_iso TEXT NOT NULL,
            policy_version INTEGER NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signed_weight_vectors_updated_at
            ON signed_weight_vectors(updated_at_iso);
    """),
    # 0030: durable submit admission (RELIABILITY_UPGRADE_PLAN Phase 4/5). Extend
    # the EXISTING per_miner_attempts ledger in place (do NOT stand up a parallel
    # submit_attempts table). All columns are additive + nullable/defaulted so the
    # legacy inline writes (which only name the original columns) keep working and
    # the durable-admission path is purely opt-in (CATHEDRAL_SUBMIT_ASYNC_ENABLED).
    #   idempotency_key  = sha256(hotkey + challenge_id + dimacs_solution_sha256)
    #   received_at_iso  = server time at submit handler entry (fairness order)
    #   verified_at_iso  = worker completion time (NOT the fairness clock)
    #   solution_body    = raw DIMACS held until verified (retention job compacts)
    #   lock_*/attempt_count = FOR UPDATE SKIP LOCKED worker claim bookkeeping
    ("0030_submit_admission", """
        ALTER TABLE per_miner_attempts ADD COLUMN idempotency_key TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN received_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN verified_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN challenge_kind TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN solution_body TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN solve_rank INTEGER;
        ALTER TABLE per_miner_attempts ADD COLUMN weighted_score REAL;
        ALTER TABLE per_miner_attempts ADD COLUMN eval_run_id TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE per_miner_attempts ADD COLUMN next_attempt_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN locked_by TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN locked_until_iso TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_per_miner_attempts_idem
            ON per_miner_attempts(idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_status_received
            ON per_miner_attempts(status, received_at_iso);
    """),
    # 0031: per-miner (pm-*) durable async admission (TRACK 1). The pm-* submit
    # lane regenerates its own CNF deterministically from the miner's mapped
    # scoring identity (NOT the raw hotkey, which may be a delegated child key),
    # so the worker needs that identity to re-materialize the instance off the
    # request path. assignment_identity is additive + nullable: the inline pm
    # writers never set it, and the public lane never needs it. shadow_* hold the
    # terminal verdict the SHADOW worker WOULD have written (the live ledger is
    # untouched in shadow) so go-live can prove async-vs-inline parity.
    ("0031_pm_submit_admission", """
        ALTER TABLE per_miner_attempts ADD COLUMN assignment_identity TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN shadow_status TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN shadow_rejection_reason TEXT;
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_kind_status_received
            ON per_miner_attempts(challenge_kind, status, received_at_iso);
    """),
    # 0032: repair old live SQLite volumes whose schema_migrations table says
    # 0002_lane_challenges was applied, but whose lane_challenges table predates
    # the current miner-facing shape. Duplicate-column errors are ignored by
    # _sqlite_exec_migration, so this is safe on healthy databases too.
    ("0032_lane_challenges_shape_repair", """
        ALTER TABLE lane_challenges ADD COLUMN family_id TEXT NOT NULL DEFAULT 'synthetic_boolean_v1';
        ALTER TABLE lane_challenges ADD COLUMN tier INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE lane_challenges ADD COLUMN cnf_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN cnf_sha256 TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN cnf_bytes INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN num_vars INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN num_clauses INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
        ALTER TABLE lane_challenges ADD COLUMN score_multiplier REAL NOT NULL DEFAULT 1.0;
        ALTER TABLE lane_challenges ADD COLUMN difficulty_label TEXT;
        ALTER TABLE lane_challenges ADD COLUMN designated_solver_digest TEXT;
        ALTER TABLE lane_challenges ADD COLUMN created_at_iso TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN cnf_source TEXT NOT NULL DEFAULT 'local';
        ALTER TABLE lane_challenges ADD COLUMN cnf_url TEXT;
        ALTER TABLE lane_challenges ADD COLUMN updated_at_iso TEXT;
    """),
    ("0033_async_verify_workers", """
        CREATE TABLE IF NOT EXISTS async_verify_workers (
            worker_id TEXT PRIMARY KEY,
            service_role TEXT NOT NULL DEFAULT '',
            started_at_iso TEXT NOT NULL,
            last_seen_at_iso TEXT NOT NULL,
            last_tick_at_iso TEXT,
            last_processed_at_iso TEXT,
            last_error TEXT,
            processed_total INTEGER NOT NULL DEFAULT 0,
            last_batch_size INTEGER NOT NULL DEFAULT 0,
            running INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_async_verify_workers_seen
            ON async_verify_workers(last_seen_at_iso);
    """),
    ("0034_per_miner_score_window_index", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_verified_time_hotkey
            ON per_miner_solves(verified, solved_at_iso, miner_hotkey);
    """),
    ("0035_per_miner_miner_window_index", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_hotkey_verified_time
            ON per_miner_solves(miner_hotkey, verified, solved_at_iso);
    """),
    # 0036: V2 off-chain solution manifest intake. Miners submit a tiny signed
    # manifest pointing at a decentralized/blob solution artifact; workers verify
    # the blob asynchronously in later phases. This table is isolated from the
    # current payout ledger so phase 1/2 can be tested without reward impact.
    ("0036_solution_manifests", """
        CREATE TABLE IF NOT EXISTS solution_manifests (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            assignment_encoding TEXT NOT NULL,
            solution_cid TEXT NOT NULL,
            solution_sha256 TEXT NOT NULL,
            solution_bytes INTEGER NOT NULL DEFAULT 0,
            cnf_sha256 TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            rejection_reason TEXT,
            submitted_at TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            verified_at_iso TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at_iso TEXT,
            locked_by TEXT,
            locked_until_iso TEXT,
            epoch INTEGER,
            tier INTEGER,
            seq INTEGER,
            assignment_identity TEXT,
            weighted_score REAL,
            answer_hash TEXT,
            verifier_details_hash TEXT,
            last_error TEXT,
            signature TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_solution_manifests_status_received
            ON solution_manifests(status, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_solution_manifests_hotkey_challenge
            ON solution_manifests(miner_hotkey, challenge_id);
    """),
    ("0037_v2_shadow_v1_submits", """
        CREATE TABLE IF NOT EXISTS v2_shadow_v1_submits (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            solution_sha256 TEXT NOT NULL,
            solution_bytes INTEGER NOT NULL DEFAULT 0,
            solution_cid TEXT NOT NULL,
            request_sha256 TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            rejection_reason TEXT,
            submitted_at TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            signature TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mirror',
            form_json TEXT NOT NULL DEFAULT '{}',
            headers_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submits_received
            ON v2_shadow_v1_submits(received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submits_hotkey_challenge
            ON v2_shadow_v1_submits(miner_hotkey, challenge_id);
    """),
    ("0038_v2_shadow_v1_submit_meta", """
        CREATE TABLE IF NOT EXISTS v2_shadow_v1_submit_meta (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL DEFAULT '',
            miner_hotkey TEXT NOT NULL DEFAULT '',
            challenge_id TEXT NOT NULL DEFAULT '',
            card_id TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT '',
            edge_received_at_iso TEXT NOT NULL DEFAULT '',
            received_at_iso TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mirror-meta',
            original_content_length INTEGER NOT NULL DEFAULT 0,
            original_body_bytes INTEGER NOT NULL DEFAULT 0,
            dimacs_solution_bytes INTEGER NOT NULL DEFAULT 0,
            field_count INTEGER NOT NULL DEFAULT 0,
            signature_present INTEGER NOT NULL DEFAULT 0,
            content_type TEXT NOT NULL DEFAULT '',
            parse_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submit_meta_received
            ON v2_shadow_v1_submit_meta(received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submit_meta_hotkey_challenge
            ON v2_shadow_v1_submit_meta(miner_hotkey, challenge_id);
    """),
    ("0039_v2_submit_events", """
        CREATE TABLE IF NOT EXISTS v2_submit_events (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            assignment_encoding TEXT NOT NULL,
            assignment_sha256 TEXT NOT NULL,
            assignment_b64 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'verified',
            rejection_reason TEXT,
            eligibility_status TEXT NOT NULL DEFAULT 'unknown_beta',
            received_at_iso TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            verified_at_iso TEXT,
            signature TEXT NOT NULL,
            submit_token_id TEXT NOT NULL DEFAULT '',
            weighted_score REAL NOT NULL DEFAULT 0.0,
            answer_hash TEXT NOT NULL DEFAULT '',
            verifier_details_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_status_received
            ON v2_submit_events(status, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_hotkey_challenge
            ON v2_submit_events(miner_hotkey, challenge_id);
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_epoch
            ON v2_submit_events(epoch, status);
    """),
    ("0040_v2_submit_events_solver_meta", """
        ALTER TABLE v2_submit_events ADD COLUMN solver_id TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN solver_hash TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN image_url TEXT;
    """),
    ("0041_v2_submit_events_challenge_kind", """
        ALTER TABLE v2_submit_events ADD COLUMN challenge_kind TEXT;
    """),
    ("0041b_v2_submit_events_async_verify", """
        ALTER TABLE v2_submit_events ADD COLUMN locked_by TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN locked_until_iso TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN last_error TEXT;
    """),
    ("0042_v2_cnf_store", """
        CREATE TABLE IF NOT EXISTS v2_cnf_store (
            challenge_id TEXT PRIMARY KEY,
            cnf_sha256 TEXT NOT NULL,
            cnf_zlib BLOB NOT NULL,
            created_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_store_created ON v2_cnf_store(created_at_iso);
    """),
    ("0043_solution_manifests_inline", """
        ALTER TABLE solution_manifests ADD COLUMN solution_inline BLOB;
    """),
    ("0044_v2_worker_heartbeat", """
        CREATE TABLE IF NOT EXISTS v2_worker_heartbeat (
            key TEXT PRIMARY KEY,
            worker_id TEXT,
            beat_at_iso TEXT
        );
    """),
    ("0045_external_score_reports", """
        CREATE TABLE IF NOT EXISTS external_score_reports (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 0,
            generated_at_iso TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            score_count INTEGER NOT NULL DEFAULT 0,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_external_score_reports_source_received
            ON external_score_reports(source, received_at_iso);

        CREATE TABLE IF NOT EXISTS external_score_entries (
            report_id TEXT NOT NULL,
            source TEXT NOT NULL,
            epoch INTEGER NOT NULL DEFAULT 0,
            miner_hotkey TEXT NOT NULL,
            uid INTEGER,
            score REAL NOT NULL,
            quality REAL,
            latency REAL,
            validity REAL,
            tasks_scored INTEGER NOT NULL DEFAULT 0,
            confidence REAL,
            generated_at_iso TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (report_id, miner_hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_external_score_entries_source_received
            ON external_score_entries(source, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_external_score_entries_hotkey_received
            ON external_score_entries(miner_hotkey, received_at_iso);
    """),
    ("0046_v2_cnf_artifacts", """
        CREATE TABLE IF NOT EXISTS v2_cnf_artifacts (
            challenge_id TEXT PRIMARY KEY,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            n_vars INTEGER NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            cnf_bytes INTEGER NOT NULL,
            artifact_key TEXT NOT NULL,
            artifact_url TEXT NOT NULL,
            published_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_artifacts_key
            ON v2_cnf_artifacts(artifact_key);
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_artifacts_epoch
            ON v2_cnf_artifacts(epoch, tier, seq);

        CREATE TABLE IF NOT EXISTS v2_cnf_epoch_readiness (
            epoch INTEGER PRIMARY KEY,
            next_epoch INTEGER NOT NULL,
            expected_current INTEGER NOT NULL,
            published_current INTEGER NOT NULL,
            expected_next INTEGER NOT NULL,
            published_next INTEGER NOT NULL,
            ready INTEGER NOT NULL DEFAULT 0,
            updated_at_iso TEXT NOT NULL
        );
    """),
    ("0047_external_score_audience", """
        ALTER TABLE external_score_reports ADD COLUMN network TEXT;
        ALTER TABLE external_score_reports ADD COLUMN netuid INTEGER;
        CREATE INDEX IF NOT EXISTS idx_external_score_reports_audience_epoch
            ON external_score_reports(source, network, netuid, epoch);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_external_score_reports_audience_epoch
            ON external_score_reports(source, network, netuid, epoch)
            WHERE network IS NOT NULL AND netuid IS NOT NULL;
    """),
]

# Postgres DDL — the same logical schema, portable. REAL->DOUBLE PRECISION,
# ADD COLUMN guarded by IF NOT EXISTS (Postgres supports it), executescript-style
# multi-statement blocks split on ';'. Indexes IF NOT EXISTS.
_MIGRATIONS_PG: list[tuple[str, str]] = [
    ("0001_eval_runs", """
        CREATE TABLE IF NOT EXISTS eval_runs (
            id TEXT PRIMARY KEY,
            ran_at TEXT NOT NULL,
            eval_output_schema_version INTEGER NOT NULL,
            miner_hotkey TEXT NOT NULL,
            task_type TEXT NOT NULL,
            row_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eval_runs_cursor ON eval_runs(ran_at, id);
    """),
    ("0002_lane_challenges", """
        CREATE TABLE IF NOT EXISTS lane_challenges (
            challenge_id TEXT PRIMARY KEY,
            family_id TEXT NOT NULL,
            tier INTEGER NOT NULL,
            cnf_text TEXT NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            cnf_bytes INTEGER NOT NULL,
            num_vars INTEGER NOT NULL,
            num_clauses INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            score_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            difficulty_label TEXT,
            designated_solver_digest TEXT,
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0003_agent_submissions", """
        CREATE TABLE IF NOT EXISTS agent_submissions (
            id TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            sat_challenge_id TEXT NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            current_score DOUBLE PRECISION,
            seq_no INTEGER NOT NULL,
            submitted_at TEXT NOT NULL,
            signature TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sub_hotkey_chal
            ON agent_submissions(miner_hotkey, sat_challenge_id);
    """),
    ("0004_arena_solvers", """
        CREATE TABLE IF NOT EXISTS arena_solvers (
            source_sha256 TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            container_digest TEXT NOT NULL,
            owner_hotkey TEXT NOT NULL,
            registered_round INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0005_arena_instances", """
        CREATE TABLE IF NOT EXISTS arena_instances (
            instance_id TEXT PRIMARY KEY,
            owner_hotkey TEXT NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            submitted_round INTEGER NOT NULL,
            quarantine_until_round INTEGER NOT NULL,
            min_batch_score DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0006_replay_dedup", """
        CREATE TABLE IF NOT EXISTS submit_signatures (
            signature TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );
    """),
    ("0007_seed_state", """
        CREATE TABLE IF NOT EXISTS seed_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """),
    ("0008_lane_challenges_source", """
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_source TEXT NOT NULL DEFAULT 'local';
    """),
    ("0009_lane_challenges_cnf_url", """
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_url TEXT;
    """),
    ("0010_lane_challenge_solves", """
        CREATE TABLE IF NOT EXISTS lane_challenge_solves (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            solved_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
    """),
    ("0011_lane_challenges_updated_at", """
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS updated_at_iso TEXT;
    """),
    ("0012_arena_instances_payout", """
        ALTER TABLE arena_instances ADD COLUMN IF NOT EXISTS cnf_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE arena_instances ADD COLUMN IF NOT EXISTS last_paid_round INTEGER;
        ALTER TABLE arena_instances ADD COLUMN IF NOT EXISTS paid_at_iso TEXT;
    """),
    ("0013_weight_policy_state", """
        CREATE TABLE IF NOT EXISTS weight_policy_state (
            id INTEGER PRIMARY KEY,
            last_policy_version INTEGER NOT NULL,
            updated_at_iso TEXT NOT NULL
        );
    """),
    ("0014_weight_policy_version_bigint", """
        ALTER TABLE weight_policy_state ALTER COLUMN last_policy_version TYPE BIGINT;
    """),
    ("0015_eval_runs_miner_idx", """
        CREATE INDEX IF NOT EXISTS idx_eval_runs_miner_ran_at ON eval_runs(miner_hotkey, ran_at);
    """),
    # 0016: per-miner unique challenges (CATHEDRAL_PERMINER_ENABLED).
    ("0016_per_miner_solves", """
        CREATE TABLE IF NOT EXISTS per_miner_solves (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            difficulty_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            verified INTEGER NOT NULL DEFAULT 0,
            solved_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_epoch_hotkey
            ON per_miner_solves(epoch, miner_hotkey);
    """),
    # 0017: index on lane_challenge_solves.solved_at_iso so compose_scores
    # (SELECT DISTINCT miner_hotkey, challenge_id WHERE solved_at_iso > ?)
    # uses an index scan instead of a full table scan.
    ("0017_lane_challenge_solves_solved_at_idx", """
        CREATE INDEX IF NOT EXISTS idx_lane_challenge_solves_solved_at
            ON lane_challenge_solves(solved_at_iso);
    """),
    ("0018_per_miner_evidence", """
        CREATE TABLE IF NOT EXISTS per_miner_attempts (
            id TEXT PRIMARY KEY,
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            dimacs_solution_sha256 TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            recorded_at_iso TEXT NOT NULL,
            signature TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_challenge_hotkey
            ON per_miner_attempts(challenge_id, miner_hotkey);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_epoch_hotkey
            ON per_miner_attempts(epoch, miner_hotkey);

        CREATE TABLE IF NOT EXISTS per_miner_assignments (
            challenge_id TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            difficulty_weight DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            assigned_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_per_miner_assignments_epoch_hotkey
            ON per_miner_assignments(epoch, miner_hotkey);

        CREATE TABLE IF NOT EXISTS per_miner_witnesses (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            dimacs_solution_sha256 TEXT NOT NULL,
            answer_hash TEXT NOT NULL,
            dimacs_solution TEXT NOT NULL,
            recorded_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
    """),
    ("0019_audit_challenge_manifests", """
        CREATE TABLE IF NOT EXISTS audit_challenge_manifests (
            challenge_id TEXT PRIMARY KEY,
            cnf_sha256 TEXT NOT NULL,
            manifest_json TEXT,
            decode_map_json TEXT,
            source_path TEXT,
            created_at_iso TEXT NOT NULL
        );
    """),
    ("0020_eval_runs_legacy_columns", """
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS id TEXT;
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS ran_at TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS eval_output_schema_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS miner_hotkey TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT '';
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS row_json TEXT NOT NULL DEFAULT '{}';
        CREATE INDEX IF NOT EXISTS idx_eval_runs_cursor ON eval_runs(ran_at, id);
        CREATE INDEX IF NOT EXISTS idx_eval_runs_miner_ran_at ON eval_runs(miner_hotkey, ran_at);
    """),
    ("0021_coldkey_map", """
        CREATE TABLE IF NOT EXISTS coldkey_map (
            hotkey TEXT PRIMARY KEY,
            coldkey TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_coldkey_map_coldkey
            ON coldkey_map(coldkey);
    """),
    # Off-chain TEE GPU capacity intake for provider handoff. These tables are
    # intentionally not scoring inputs: validators never read them.
    ("0022_tee_gpu_capacity", """
        CREATE TABLE IF NOT EXISTS tee_gpu_capacity (
            capacity_id TEXT PRIMARY KEY,
            provider_ref TEXT NOT NULL DEFAULT '',
            owner_hotkey TEXT NOT NULL,
            node_id TEXT NOT NULL,
            region TEXT NOT NULL DEFAULT '',
            endpoint_url TEXT NOT NULL DEFAULT '',
            agent_api TEXT NOT NULL DEFAULT '',
            public_ip TEXT NOT NULL DEFAULT '',
            gpu_short_ref TEXT NOT NULL,
            gpu_model TEXT NOT NULL DEFAULT '',
            gpu_count INTEGER NOT NULL,
            gpu_memory_gb INTEGER,
            tee_kind TEXT NOT NULL DEFAULT '',
            tdx_claimed INTEGER NOT NULL DEFAULT 0,
            gpu_cc_claimed INTEGER NOT NULL DEFAULT 0,
            hourly_cost DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            currency TEXT NOT NULL DEFAULT 'USD',
            attestation_digest TEXT NOT NULL DEFAULT '',
            attestation_json TEXT NOT NULL DEFAULT '{}',
            health_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            preflight_status TEXT NOT NULL DEFAULT 'needs_review',
            preflight_json TEXT NOT NULL DEFAULT '{}',
            chutes_validator_hotkey TEXT NOT NULL DEFAULT '',
            chutes_server_name TEXT NOT NULL DEFAULT '',
            chutes_server_id TEXT NOT NULL DEFAULT '',
            chutes_status TEXT NOT NULL DEFAULT '',
            emissions_eligible INTEGER NOT NULL DEFAULT 0 CHECK (emissions_eligible = 0),
            admin_note TEXT NOT NULL DEFAULT '',
            created_at_iso TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL,
            last_heartbeat_iso TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tee_gpu_capacity_owner_node
            ON tee_gpu_capacity(owner_hotkey, node_id);
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_status
            ON tee_gpu_capacity(status);
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_owner
            ON tee_gpu_capacity(owner_hotkey);
    """),
    ("0023_tee_gpu_capacity_events", """
        CREATE TABLE IF NOT EXISTS tee_gpu_capacity_events (
            id TEXT PRIMARY KEY,
            capacity_id TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            event_json TEXT NOT NULL DEFAULT '{}',
            created_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tee_gpu_capacity_events_capacity
            ON tee_gpu_capacity_events(capacity_id, created_at_iso);
    """),
    ("0024_tee_gpu_capacity_authorization", """
        ALTER TABLE tee_gpu_capacity ADD COLUMN IF NOT EXISTS operator_use_authorized INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tee_gpu_capacity ADD COLUMN IF NOT EXISTS authorization_version TEXT NOT NULL DEFAULT '';
        ALTER TABLE tee_gpu_capacity ADD COLUMN IF NOT EXISTS authorization_digest TEXT NOT NULL DEFAULT '';
        ALTER TABLE tee_gpu_capacity ADD COLUMN IF NOT EXISTS authorization_json TEXT NOT NULL DEFAULT '{}';
    """),
    ("0025_solver_attestation", """
        CREATE TABLE IF NOT EXISTS attest_nonces (
            nonce TEXT PRIMARY KEY,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            issued_at_iso TEXT NOT NULL,
            expires_at_iso TEXT NOT NULL,
            used_at_iso TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_attest_nonces_miner_challenge
            ON attest_nonces(miner_hotkey, challenge_id);
        CREATE TABLE IF NOT EXISTS attestations (
            id TEXT PRIMARY KEY,
            eval_run_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            solver_digest TEXT NOT NULL,
            multiplier DOUBLE PRECISION NOT NULL,
            verified_at_iso TEXT NOT NULL,
            quote_digest TEXT NOT NULL DEFAULT '',
            receipt_digest TEXT NOT NULL DEFAULT '',
            verifier_backend TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_attestations_eval_run
            ON attestations(eval_run_id);
        CREATE INDEX IF NOT EXISTS idx_attestations_miner_challenge
            ON attestations(miner_hotkey, challenge_id);
        ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS attested INTEGER NOT NULL DEFAULT 0;
    """),
    ("0026_attest_nonce_pubkey", """
        ALTER TABLE attest_nonces ADD COLUMN IF NOT EXISTS miner_pubkey_b64 TEXT NOT NULL DEFAULT '';
    """),
    ("0027_per_miner_time_indexes", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_solved_at
            ON per_miner_solves(solved_at_iso);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_recorded_at
            ON per_miner_attempts(recorded_at_iso);
    """),
    ("0028_metagraph_hotkeys", """
        CREATE TABLE IF NOT EXISTS metagraph_hotkeys (
            network TEXT NOT NULL,
            netuid INTEGER NOT NULL,
            hotkey TEXT NOT NULL,
            uid INTEGER,
            coldkey TEXT NOT NULL DEFAULT '',
            block BIGINT,
            updated_at_iso TEXT NOT NULL,
            PRIMARY KEY (network, netuid, hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_metagraph_hotkeys_fresh
            ON metagraph_hotkeys(network, netuid, updated_at_iso);
    """),
    ("0029_signed_weight_vectors", """
        CREATE TABLE IF NOT EXISTS signed_weight_vectors (
            id TEXT PRIMARY KEY,
            generated_at_iso TEXT NOT NULL,
            policy_version BIGINT NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signed_weight_vectors_updated_at
            ON signed_weight_vectors(updated_at_iso);
    """),
    # 0030: durable submit admission — see SQLite 0030. NULL idempotency_key on
    # legacy rows is fine: Postgres unique indexes treat NULLs as distinct, so the
    # inline writers that never set the column never collide.
    ("0030_submit_admission", """
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS received_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS verified_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS challenge_kind TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS solution_body TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS solve_rank INTEGER;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS weighted_score DOUBLE PRECISION;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS eval_run_id TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS next_attempt_at_iso TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS locked_by TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS locked_until_iso TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_per_miner_attempts_idem
            ON per_miner_attempts(idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_status_received
            ON per_miner_attempts(status, received_at_iso);
    """),
    # 0031: per-miner (pm-*) durable async admission - see SQLite 0031. Columns
    # are additive + nullable so the inline pm writers (which never name them)
    # keep working; the async pm lane is purely opt-in (CATHEDRAL_PM_SUBMIT_ASYNC_ENABLED).
    ("0031_pm_submit_admission", """
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS assignment_identity TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS shadow_status TEXT;
        ALTER TABLE per_miner_attempts ADD COLUMN IF NOT EXISTS shadow_rejection_reason TEXT;
        CREATE INDEX IF NOT EXISTS idx_per_miner_attempts_kind_status_received
            ON per_miner_attempts(challenge_kind, status, received_at_iso);
    """),
    ("0032_lane_challenges_shape_repair", """
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'synthetic_boolean_v1';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS tier INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_text TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_sha256 TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_bytes BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS num_vars INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS num_clauses INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS score_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1.0;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS difficulty_label TEXT;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS designated_solver_digest TEXT;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS created_at_iso TEXT NOT NULL DEFAULT '';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_source TEXT NOT NULL DEFAULT 'local';
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS cnf_url TEXT;
        ALTER TABLE lane_challenges ADD COLUMN IF NOT EXISTS updated_at_iso TEXT;
    """),
    ("0033_async_verify_workers", """
        CREATE TABLE IF NOT EXISTS async_verify_workers (
            worker_id TEXT PRIMARY KEY,
            service_role TEXT NOT NULL DEFAULT '',
            started_at_iso TEXT NOT NULL,
            last_seen_at_iso TEXT NOT NULL,
            last_tick_at_iso TEXT,
            last_processed_at_iso TEXT,
            last_error TEXT,
            processed_total INTEGER NOT NULL DEFAULT 0,
            last_batch_size INTEGER NOT NULL DEFAULT 0,
            running INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_async_verify_workers_seen
            ON async_verify_workers(last_seen_at_iso);
    """),
    ("0034_per_miner_score_window_index", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_verified_time_hotkey
            ON per_miner_solves(verified, solved_at_iso, miner_hotkey);
    """),
    ("0035_per_miner_miner_window_index", """
        CREATE INDEX IF NOT EXISTS idx_per_miner_solves_hotkey_verified_time
            ON per_miner_solves(miner_hotkey, verified, solved_at_iso);
    """),
    ("0036_solution_manifests", """
        CREATE TABLE IF NOT EXISTS solution_manifests (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            assignment_encoding TEXT NOT NULL,
            solution_cid TEXT NOT NULL,
            solution_sha256 TEXT NOT NULL,
            solution_bytes BIGINT NOT NULL DEFAULT 0,
            cnf_sha256 TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            rejection_reason TEXT,
            submitted_at TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            verified_at_iso TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at_iso TEXT,
            locked_by TEXT,
            locked_until_iso TEXT,
            epoch BIGINT,
            tier INTEGER,
            seq INTEGER,
            assignment_identity TEXT,
            weighted_score DOUBLE PRECISION,
            answer_hash TEXT,
            verifier_details_hash TEXT,
            last_error TEXT,
            signature TEXT NOT NULL,
            manifest_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_solution_manifests_status_received
            ON solution_manifests(status, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_solution_manifests_hotkey_challenge
            ON solution_manifests(miner_hotkey, challenge_id);
    """),
    ("0037_v2_shadow_v1_submits", """
        CREATE TABLE IF NOT EXISTS v2_shadow_v1_submits (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            solution_sha256 TEXT NOT NULL,
            solution_bytes BIGINT NOT NULL DEFAULT 0,
            solution_cid TEXT NOT NULL,
            request_sha256 TEXT NOT NULL DEFAULT '',
            content_type TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'received',
            rejection_reason TEXT,
            submitted_at TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            signature TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mirror',
            form_json TEXT NOT NULL DEFAULT '{}',
            headers_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submits_received
            ON v2_shadow_v1_submits(received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submits_hotkey_challenge
            ON v2_shadow_v1_submits(miner_hotkey, challenge_id);
    """),
    ("0038_v2_shadow_v1_submit_meta", """
        CREATE TABLE IF NOT EXISTS v2_shadow_v1_submit_meta (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL DEFAULT '',
            miner_hotkey TEXT NOT NULL DEFAULT '',
            challenge_id TEXT NOT NULL DEFAULT '',
            card_id TEXT NOT NULL DEFAULT '',
            submitted_at TEXT NOT NULL DEFAULT '',
            edge_received_at_iso TEXT NOT NULL DEFAULT '',
            received_at_iso TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mirror-meta',
            original_content_length BIGINT NOT NULL DEFAULT 0,
            original_body_bytes BIGINT NOT NULL DEFAULT 0,
            dimacs_solution_bytes BIGINT NOT NULL DEFAULT 0,
            field_count INTEGER NOT NULL DEFAULT 0,
            signature_present INTEGER NOT NULL DEFAULT 0,
            content_type TEXT NOT NULL DEFAULT '',
            parse_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submit_meta_received
            ON v2_shadow_v1_submit_meta(received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_shadow_v1_submit_meta_hotkey_challenge
            ON v2_shadow_v1_submit_meta(miner_hotkey, challenge_id);
    """),
    ("0039_v2_submit_events", """
        CREATE TABLE IF NOT EXISTS v2_submit_events (
            id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            miner_hotkey TEXT NOT NULL,
            challenge_id TEXT NOT NULL,
            card_id TEXT NOT NULL,
            epoch BIGINT NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            assignment_encoding TEXT NOT NULL,
            assignment_sha256 TEXT NOT NULL,
            assignment_b64 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'verified',
            rejection_reason TEXT,
            eligibility_status TEXT NOT NULL DEFAULT 'unknown_beta',
            received_at_iso TEXT NOT NULL,
            submitted_at TEXT NOT NULL,
            verified_at_iso TEXT,
            signature TEXT NOT NULL,
            submit_token_id TEXT NOT NULL DEFAULT '',
            weighted_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            answer_hash TEXT NOT NULL DEFAULT '',
            verifier_details_hash TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_status_received
            ON v2_submit_events(status, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_hotkey_challenge
            ON v2_submit_events(miner_hotkey, challenge_id);
        CREATE INDEX IF NOT EXISTS idx_v2_submit_events_epoch
            ON v2_submit_events(epoch, status);
    """),
    ("0040_v2_submit_events_solver_meta", """
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS solver_id TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS solver_hash TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS image_url TEXT;
    """),
    ("0041_v2_submit_events_challenge_kind", """
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS challenge_kind TEXT;
    """),
    ("0041b_v2_submit_events_async_verify", """
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS locked_by TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS locked_until_iso TEXT;
        ALTER TABLE v2_submit_events ADD COLUMN IF NOT EXISTS last_error TEXT;
    """),
    ("0042_v2_cnf_store", """
        CREATE TABLE IF NOT EXISTS v2_cnf_store (
            challenge_id TEXT PRIMARY KEY,
            cnf_sha256 TEXT NOT NULL,
            cnf_zlib BYTEA NOT NULL,
            created_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_store_created ON v2_cnf_store(created_at_iso);
    """),
    ("0043_solution_manifests_inline", """
        ALTER TABLE solution_manifests ADD COLUMN IF NOT EXISTS solution_inline BYTEA;
    """),
    ("0044_v2_worker_heartbeat", """
        CREATE TABLE IF NOT EXISTS v2_worker_heartbeat (
            key TEXT PRIMARY KEY,
            worker_id TEXT,
            beat_at_iso TEXT
        );
    """),
    ("0045_external_score_reports", """
        CREATE TABLE IF NOT EXISTS external_score_reports (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            epoch BIGINT NOT NULL DEFAULT 0,
            generated_at_iso TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            report_sha256 TEXT NOT NULL,
            score_count INTEGER NOT NULL DEFAULT 0,
            report_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_external_score_reports_source_received
            ON external_score_reports(source, received_at_iso);

        CREATE TABLE IF NOT EXISTS external_score_entries (
            report_id TEXT NOT NULL,
            source TEXT NOT NULL,
            epoch BIGINT NOT NULL DEFAULT 0,
            miner_hotkey TEXT NOT NULL,
            uid INTEGER,
            score DOUBLE PRECISION NOT NULL,
            quality DOUBLE PRECISION,
            latency DOUBLE PRECISION,
            validity DOUBLE PRECISION,
            tasks_scored INTEGER NOT NULL DEFAULT 0,
            confidence DOUBLE PRECISION,
            generated_at_iso TEXT NOT NULL,
            received_at_iso TEXT NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (report_id, miner_hotkey)
        );
        CREATE INDEX IF NOT EXISTS idx_external_score_entries_source_received
            ON external_score_entries(source, received_at_iso);
        CREATE INDEX IF NOT EXISTS idx_external_score_entries_hotkey_received
            ON external_score_entries(miner_hotkey, received_at_iso);
    """),
    ("0046_v2_cnf_artifacts", """
        CREATE TABLE IF NOT EXISTS v2_cnf_artifacts (
            challenge_id TEXT PRIMARY KEY,
            epoch BIGINT NOT NULL,
            tier INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            n_vars INTEGER NOT NULL,
            cnf_sha256 TEXT NOT NULL,
            cnf_bytes BIGINT NOT NULL,
            artifact_key TEXT NOT NULL,
            artifact_url TEXT NOT NULL,
            published_at_iso TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_artifacts_key
            ON v2_cnf_artifacts(artifact_key);
        CREATE INDEX IF NOT EXISTS idx_v2_cnf_artifacts_epoch
            ON v2_cnf_artifacts(epoch, tier, seq);

        CREATE TABLE IF NOT EXISTS v2_cnf_epoch_readiness (
            epoch BIGINT PRIMARY KEY,
            next_epoch BIGINT NOT NULL,
            expected_current BIGINT NOT NULL,
            published_current BIGINT NOT NULL,
            expected_next BIGINT NOT NULL,
            published_next BIGINT NOT NULL,
            ready INTEGER NOT NULL DEFAULT 0,
            updated_at_iso TEXT NOT NULL
        );
    """),
    ("0047_external_score_audience", """
        ALTER TABLE external_score_reports ADD COLUMN IF NOT EXISTS network TEXT;
        ALTER TABLE external_score_reports ADD COLUMN IF NOT EXISTS netuid INTEGER;
        CREATE INDEX IF NOT EXISTS idx_external_score_reports_audience_epoch
            ON external_score_reports(source, network, netuid, epoch);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_external_score_reports_audience_epoch
            ON external_score_reports(source, network, netuid, epoch)
            WHERE network IS NOT NULL AND netuid IS NOT NULL;
    """),
]

# Conflict targets for INSERT OR REPLACE / INSERT OR IGNORE upserts that name no
# explicit ON CONFLICT clause. SQLite resolves OR REPLACE / OR IGNORE against the
# table's primary key; Postgres needs the column(s) named.
_PK_BY_TABLE: dict[str, str] = {
    "eval_runs": "id",
    "lane_challenges": "challenge_id",
    "agent_submissions": "id",
    "arena_solvers": "source_sha256",
    "arena_instances": "instance_id",
    "submit_signatures": "signature",
    "seed_state": "key",
    "lane_challenge_solves": "challenge_id, miner_hotkey",
    "weight_policy_state": "id",
    "schema_migrations": "id",
    "per_miner_solves": "challenge_id, miner_hotkey",
    "per_miner_attempts": "id",
    "per_miner_assignments": "challenge_id",
    "per_miner_witnesses": "challenge_id, miner_hotkey",
    "audit_challenge_manifests": "challenge_id",
    "coldkey_map": "hotkey",
    "metagraph_hotkeys": "network, netuid, hotkey",
    "signed_weight_vectors": "id",
    "external_score_reports": "id",
    "external_score_entries": "report_id, miner_hotkey",
    "solution_manifests": "id",
    "tee_gpu_capacity": "capacity_id",
    "tee_gpu_capacity_events": "id",
    "attest_nonces": "nonce",
    "attestations": "id",
    "v2_worker_heartbeat": "key",
    "v2_cnf_artifacts": "challenge_id",
    "v2_cnf_epoch_readiness": "epoch",
}


def _is_postgres_dsn(s: str | None) -> bool:
    return bool(s) and s.split("://", 1)[0] in ("postgres", "postgresql")


# ===========================================================================
# Postgres dialect translation
# ===========================================================================
_INSERT_OR_IGNORE_RE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+(\w+)", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    """Translate the narrow set of SQLite SQL forms this codebase emits into
    Postgres. Pure string rewrite; deterministic; pinned by postgres_verify.py."""
    s = sql

    # datetime('now') -> now()   (only literal used in this codebase)
    s = re.sub(r"datetime\(\s*'now'\s*\)", "now()", s, flags=re.IGNORECASE)

    # INSERT OR REPLACE INTO t(cols) VALUES(...) -> upsert on the table PK.
    m = _INSERT_OR_REPLACE_RE.search(s)
    if m:
        table = m.group(1)
        cols = [c.strip() for c in m.group(2).split(",")]
        pk = _PK_BY_TABLE.get(table, "")
        pk_cols = {c.strip() for c in pk.split(",")} if pk else set()
        set_cols = [c for c in cols if c not in pk_cols]
        body = s[m.end():]  # everything after the column list — the VALUES(...) part
        assignments = ", ".join(f"{c}=excluded.{c}" for c in set_cols)
        head = f"INSERT INTO {table}({m.group(2)})"
        if pk and assignments:
            s = f"{head}{body} ON CONFLICT ({pk}) DO UPDATE SET {assignments}"
        else:
            s = f"{head}{body} ON CONFLICT DO NOTHING"

    # INSERT OR IGNORE INTO t(...) -> INSERT INTO t(...) ... ON CONFLICT DO NOTHING.
    # Only add the clause if the statement does not already name ON CONFLICT.
    m = _INSERT_OR_IGNORE_RE.search(s)
    if m:
        s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s, count=1, flags=re.IGNORECASE)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # ?-style placeholders -> %s. Safe because none of this codebase's SQL
    # contains a literal '?' inside a string literal.
    s = s.replace("?", "%s")
    return s


class _PgCursorWrapper:
    """Wraps a psycopg2 cursor so callers see a sqlite3-style cursor: rowcount,
    fetchone()/fetchall() returning name-AND-position-indexable rows, and
    iteration. psycopg2 DictRow already supports both row[0] and row['col']."""

    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class _PgConn:
    """A dialect-translating wrapper around a pooled psycopg2 connection. Exposes
    the sqlite3 `conn.execute(sql, params)` surface the codebase uses so the inline
    SQLite SQL in app.py / scoring.py / refill.py / seed_live.py runs unchanged."""

    def __init__(self, raw):
        self._raw = raw  # psycopg2 connection (DictCursor factory)

    def execute(self, sql: str, params: tuple = ()):
        cur = self._raw.cursor()
        cur.execute(_translate_sql(sql), params)
        return _PgCursorWrapper(cur)

    def executescript(self, sql: str):
        # Migrations only — split on ';' and run each non-empty statement.
        cur = self._raw.cursor()
        for stmt in sql.split(";"):
            if stmt.strip():
                cur.execute(_translate_sql(stmt))
        return _PgCursorWrapper(cur)


def _sqlite_exec_migration(conn: sqlite3.Connection, sql: str) -> None:
    """Run migration statements; tolerate already-applied ADD COLUMN drift."""
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            stmt_upper = stmt.upper()
            if "duplicate column name" in msg:
                continue
            if "no such column" in msg and stmt_upper.startswith("CREATE INDEX"):
                continue
            raise


class Store:
    """Dual-backend store. SQLite (single conn + RLock, BEGIN IMMEDIATE) or
    Postgres (pool, MVCC, no global lock). Backend chosen by the connection
    string: a postgres[ql]:// DSN (passed as `path` or via DATABASE_URL) selects
    Postgres; anything else is a SQLite file path."""

    def __init__(self, path: str, *, prefer_env_database_url: bool = True) -> None:
        # DATABASE_URL wins when set and looks like Postgres — that is how the
        # deployed publisher (server.py passes CATHEDRAL_DB_PATH) flips to PG
        # without changing app construction. V2/beta stacks can pass
        # prefer_env_database_url=False so a separate explicit path/DSN cannot
        # accidentally fall back to the live DATABASE_URL.
        env_url = os.environ.get("DATABASE_URL") if prefer_env_database_url else None
        dsn = path if _is_postgres_dsn(path) else (env_url if _is_postgres_dsn(env_url) else None)
        self.backend = "postgres" if dsn else "sqlite"
        self.path = dsn or path

        if self.backend == "postgres":
            self._lock = None
            self._init_postgres(dsn)
        else:
            self._lock = threading.RLock()
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # ---- postgres setup ---------------------------------------------------
    def _init_postgres(self, dsn: str) -> None:
        import psycopg2.extras
        import psycopg2.pool

        minconn = int(os.environ.get("CATHEDRAL_PG_POOL_MIN", "1"))
        maxconn = int(os.environ.get("CATHEDRAL_PG_POOL_MAX", "10"))
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn, maxconn, dsn, cursor_factory=psycopg2.extras.DictCursor,
            connect_timeout=int(os.environ.get("CATHEDRAL_PG_CONNECT_TIMEOUT", "10")),
            keepalives=int(os.environ.get("CATHEDRAL_PG_KEEPALIVES", "1")),
            keepalives_idle=int(os.environ.get("CATHEDRAL_PG_KEEPALIVES_IDLE", "30")),
            keepalives_interval=int(os.environ.get("CATHEDRAL_PG_KEEPALIVES_INTERVAL", "10")),
            keepalives_count=int(os.environ.get("CATHEDRAL_PG_KEEPALIVES_COUNT", "3")),
        )

    def migrate(self) -> None:
        if self.backend == "postgres":
            self._migrate_postgres()
            return
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {r["id"] for r in self._conn.execute("SELECT id FROM schema_migrations")}
            for mid, sql in _MIGRATIONS:
                if mid in applied:
                    continue
                _sqlite_exec_migration(self._conn, sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(id, applied_at) VALUES (?, datetime('now'))",
                    (mid,),
                )
            self._conn.commit()

    def _migrate_postgres(self) -> None:
        raw = self._getconn()
        try:
            cur = raw.cursor()
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
            cur.execute("SELECT id FROM schema_migrations")
            applied = {r[0] for r in cur.fetchall()}
            for mid, sql in _MIGRATIONS_PG:
                if mid in applied:
                    continue
                for stmt in sql.split(";"):
                    if stmt.strip():
                        cur.execute(stmt)
                cur.execute(
                    "INSERT INTO schema_migrations(id, applied_at) VALUES (%s, now())",
                    (mid,))
            raw.commit()
        except Exception:
            raw.rollback()
            raise
        finally:
            self._putconn(raw)

    # ---- pooled-connection hygiene ----------------------------------------
    def _getconn(self):
        """Borrow a pooled psycopg2 connection with a guaranteed-clean
        transaction state.

        A prior borrower that failed to roll back an aborted transaction (for
        example an error path whose own rollback() raised on a broken socket)
        would otherwise leave the connection in a failed transaction. Every
        subsequent query on it then raises InFailedSqlTransaction ("current
        transaction is aborted, commands ignored until end of transaction
        block") until the connection is rolled back — which is exactly the wedge
        that freezes weights refresh and stops on-chain updates. A rollback here
        is a cheap no-op on a clean connection and resets an aborted one, so no
        poisoned connection can ever be reused."""
        raw = self._pool.getconn()
        try:
            raw.rollback()
        except Exception:
            # The connection itself is unusable — discard it and take another.
            try:
                self._pool.putconn(raw, close=True)
            except Exception:
                pass
            raw = self._pool.getconn()
            try:
                raw.rollback()
            except Exception:
                pass
        return raw

    def _putconn(self, raw) -> None:
        """Return a connection to the pool only after clearing any lingering
        transaction state, so an aborted transaction can never be handed to the
        next borrower even if an error path above forgot (or failed) to roll
        back."""
        try:
            raw.rollback()
        except Exception:
            # Broken connection — discard it rather than poison the pool.
            try:
                self._pool.putconn(raw, close=True)
            except Exception:
                pass
            return
        self._pool.putconn(raw)


    # ---- low-level access -------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list:
        if self.backend == "postgres":
            raw = self._getconn()
            try:
                cur = raw.cursor()
                timeout_ms = _postgres_statement_timeout_ms()
                if timeout_ms > 0:
                    cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                cur.execute(_translate_sql(sql), params)
                rows = cur.fetchall()
                raw.commit()  # release any read snapshot promptly
                return rows
            except Exception:
                raw.rollback()
                raise
            finally:
                self._putconn(raw)
        with self._lock:
            return list(self._conn.execute(sql, params))

    def write(self, fn):
        """Run fn(conn) in a transaction; commit on success, rollback on error.

        SQLite: under the single write lock with BEGIN IMMEDIATE (the atomic
        winner-take-all claim discipline). Postgres: a pooled connection with a
        normal transaction — MVCC removes the need for a global lock (the wedge
        fix). fn returns whatever it wants; the wrapped conn translates dialect."""
        if self.backend == "postgres":
            raw = self._getconn()
            try:
                result = fn(_PgConn(raw))
                raw.commit()
                return result
            except Exception:
                raw.rollback()
                raise
            finally:
                self._putconn(raw)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _advisory_lock_key(name: str) -> int:
        key = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
        return key & ((1 << 63) - 1)

    @contextlib.contextmanager
    def advisory_lock(self, name: str):
        """Best-effort cross-process lock for singleton background jobs.

        SQLite runs in one process and already serializes writes. Postgres uses a
        session-level advisory lock held on a pooled connection for the context.
        The caller gets True only on the process that owns the lock.
        """
        if self.backend != "postgres":
            yield True
            return

        key = self._advisory_lock_key(name)
        raw = self._pool.getconn()
        acquired = False
        # Set when this connection hits an error while it may be holding (or
        # attempting to hold) the advisory lock. A "dirty" connection is
        # discarded rather than returned to the pool: reusing a connection
        # whose unlock failed (e.g. the session was left in a broken/aborted
        # state by whatever raised inside the `with` body, such as a
        # statement-timeout mid-batch) would silently hand some unrelated
        # future caller a session that still holds this advisory lock at the
        # PG session level -- the lock would then never be released until
        # that pooled connection happens to be closed, which is exactly the
        # "no worker anywhere can acquire it" prod incident this guards
        # against. Discarding forces PG to tear the backend down immediately,
        # freeing every lock it holds.
        dirty = False
        try:
            cur = raw.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            acquired = bool(cur.fetchone()[0])
            raw.commit()
            yield acquired
        except Exception:
            dirty = True
            try:
                raw.rollback()
            except Exception:
                pass
            raise
        finally:
            if acquired and not dirty:
                try:
                    cur = raw.cursor()
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    raw.commit()
                except Exception:
                    dirty = True
                    try:
                        raw.rollback()
                    except Exception:
                        pass
            if dirty:
                try:
                    self._pool.putconn(raw, close=True)
                except Exception:
                    pass
            else:
                self._pool.putconn(raw)

    def steal_stale_advisory_lock(self, name: str, idle_secs: int = 180) -> int:
        """Best-effort self-healing takeover of a stale advisory-lock holder.

        Postgres only -- SQLite is single-process, so a lock can never go
        stale across processes there; always a no-op on that backend.

        This exists for the case `advisory_lock`'s own cleanup cannot cover:
        the holder's *process* is gone outright (container replaced on
        deploy, OOM-kill, hard node failure) before any Python cleanup code
        runs, so the PG backend session just sits idle, still holding the
        lock, until something else notices.

        Both guardrails below must hold before anything is terminated:
          1. The PG backend holding this advisory key has been `idle` (not
             running a query) for more than `idle_secs`.
          2. The v2_worker_heartbeat row for this lock `name` is absent, or
             its last beat is older than `idle_secs`.
        Condition 1 alone is not enough: a healthy worker's lock-holding
        session is routinely idle between ticks. Condition 2 is what proves
        the *worker*, not just the connection, is actually gone -- so a lock
        whose heartbeat is fresh is never touched, even if this happens to
        run mid-idle-gap.

        Always best-effort: any error here (including the heartbeat lookup)
        is swallowed and reported as "0 stolen" so a problem in the steal
        path itself can never add a NEW way to stall verification.

        Returns the number of backends terminated (normally 0).
        """
        if self.backend != "postgres":
            return 0
        key = self._advisory_lock_key(name)
        # pg_locks represents a single-bigint advisory-lock key (the
        # pg_advisory_lock(bigint) overload, as used by advisory_lock() above)
        # split across two columns: classid holds the high 32 bits, objid the
        # low 32 bits, and objsubid=1 marks it as the single-key form (vs. 2
        # for the two-int32-key overload). Matching objid alone would miss
        # the high bits of our 63-bit key.
        classid = key >> 32
        objid = key & 0xFFFFFFFF
        raw = self._pool.getconn()
        try:
            cur = raw.cursor()
            cur.execute(
                """
                SELECT pg_terminate_backend(l.pid)
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.locktype = 'advisory'
                  AND l.classid = %s
                  AND l.objid = %s
                  AND l.objsubid = 1
                  AND a.pid <> pg_backend_pid()
                  AND a.state = 'idle'
                  AND a.state_change < now() - (%s * interval '1 second')
                  AND NOT EXISTS (
                      SELECT 1 FROM v2_worker_heartbeat h
                      WHERE h.key = %s
                        AND h.beat_at_iso::timestamptz > now() - (%s * interval '1 second')
                  )
                """,
                (classid, objid, idle_secs, name, idle_secs),
            )
            rows = cur.fetchall()
            raw.commit()
            return sum(1 for r in rows if r and r[0])
        except Exception:
            try:
                raw.rollback()
            except Exception:
                pass
            return 0
        finally:
            self._pool.putconn(raw)

    def write_v2_worker_heartbeat(self, key: str, worker_id: str, beat_at_iso: str) -> None:
        """Best-effort liveness beat for a v2 singleton background worker.

        Powers steal_stale_advisory_lock's second guardrail: a lock is only
        stolen if BOTH the PG session is idle-stale AND this heartbeat is
        stale/missing. `key` should be the same lock `name` passed to
        advisory_lock so the two line up. Never raises -- a heartbeat write
        failure must not stall (or crash) verification.
        """
        try:
            self.write(lambda conn: conn.execute(
                "INSERT OR REPLACE INTO v2_worker_heartbeat(key, worker_id, beat_at_iso) "
                "VALUES (?, ?, ?)",
                (key, worker_id, beat_at_iso),
            ))
        except Exception:
            pass

    def close(self) -> None:
        if self.backend == "postgres":
            self._pool.closeall()
            return
        with self._lock:
            self._conn.close()

    # ---- feed rows --------------------------------------------------------
    def insert_row(self, row: dict[str, Any]) -> int:
        """Insert one feed row (OR IGNORE). Returns 1 if newly inserted, 0 if it
        already existed — callers count insertions from rowcount instead of
        bracketing every row with COUNT(*) table scans."""
        def _do(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO eval_runs "
                "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["id"], row["ran_at"], int(row["eval_output_schema_version"]),
                 row["miner_hotkey"], row["task_type"], json.dumps(row)),
            )
            return int(cur.rowcount or 0)
        return self.write(_do)

    # ---- seed watermark (G1) ---------------------------------------------
    def get_seed_state(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM seed_state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_seed_state(self, key: str, value: str) -> None:
        def _do(conn):
            conn.execute(
                "INSERT INTO seed_state(key, value, updated_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value),
            )
        self.write(_do)

    def count_rows(self) -> int:
        return self.query("SELECT COUNT(*) AS n FROM eval_runs")[0]["n"]

    def recent_rows(
        self, since_ran_at: str | None, since_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Tuple-cursor pull: strict (ran_at, id) > (since_ran_at, since_id),
        ordered ascending — exactly the released validator's pull semantics.

        When no cursor is provided (first call with no `since`), returns the
        NEWEST rows ordered descending so callers see current activity rather
        than the oldest rows from the beginning of the dataset.
        """
        if since_ran_at is None:
            rows = self.query(
                "SELECT row_json FROM eval_runs ORDER BY ran_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = self.query(
                "SELECT row_json FROM eval_runs "
                "WHERE (ran_at > ?) OR (ran_at = ? AND id > ?) "
                "ORDER BY ran_at ASC, id ASC LIMIT ?",
                (since_ran_at, since_ran_at, since_id or "", limit),
            )
        return [json.loads(r["row_json"]) for r in rows]


def new_uuid() -> str:
    return str(uuid.uuid4())
