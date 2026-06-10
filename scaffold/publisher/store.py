"""SQLite storage for the thin publisher — additive idempotent migrations, a
single write lock (the production DB discipline: issue #109 tuple cursor, durable
backfill marker, no destructive migrations).

Tables:
  eval_runs        — the signed feed rows validators pull (the frozen surface).
  lane_challenges  — Lane A SAT challenges (CNF held server-side).
  agent_submissions— Lane A submit ledger (dedup, rate-limit, per-challenge lock).
  arena_solvers    — Lane S solver registry.
  arena_instances  — Lane I breaker instances.
  schema_migrations— applied migration ids (idempotency marker).

Cursor: rows are pulled by the strict tuple (ran_at, id) > (since_ran_at,
since_id) ordering — the exact semantics released validators use.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from typing import Any

# Each migration is (id, SQL). Migrations are ADDITIVE and idempotent (CREATE IF
# NOT EXISTS / ALTER guarded by a try). Never edit an applied migration — append.
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
        -- Durable key/value watermark for the live-state seeder (G1): the
        -- newest live (ran_at, id) cursor consumed, so re-runs resume instead
        -- of re-pulling. Idempotency marker, never destructive.
        CREATE TABLE IF NOT EXISTS seed_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """),
    ("0008_lane_challenges_source", """
        -- Challenge provenance for the seeder's mirrored board (G1): a mirrored
        -- live challenge has cnf_source='external' (CNF body lives on the live
        -- publisher / fetched lazily), while a locally-minted challenge (G2)
        -- has cnf_source='local' with the CNF held in cnf_text. cnf_url records
        -- the external active-cnf path for lazy fetch. Both default to a value
        -- that preserves pre-migration rows ('local').
        ALTER TABLE lane_challenges ADD COLUMN cnf_source TEXT NOT NULL DEFAULT 'local';
    """),
    ("0009_lane_challenges_cnf_url", """
        ALTER TABLE lane_challenges ADD COLUMN cnf_url TEXT;
    """),
    ("0010_lane_challenge_solves", """
        -- Distinct-solver ledger powering G2 solved-based retirement (live v6:
        -- retire after >=64 distinct solvers). One row per (challenge, hotkey)
        -- solve; COUNT(DISTINCT miner_hotkey) drives retirement.
        CREATE TABLE IF NOT EXISTS lane_challenge_solves (
            challenge_id TEXT NOT NULL,
            miner_hotkey TEXT NOT NULL,
            solved_at_iso TEXT NOT NULL,
            PRIMARY KEY (challenge_id, miner_hotkey)
        );
    """),
    ("0011_lane_challenges_updated_at", """
        -- Age-based retirement (G2) needs a mutable timestamp distinct from
        -- created_at_iso; default to a sentinel and backfill on write.
        ALTER TABLE lane_challenges ADD COLUMN updated_at_iso TEXT;
    """),
]


class Store:
    """A single-connection SQLite store guarded by one write lock. All writes go
    through `write()` (BEGIN IMMEDIATE), giving the atomic claim discipline the
    monolith relies on for winner-take-all without a separate process."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def migrate(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {r["id"] for r in self._conn.execute("SELECT id FROM schema_migrations")}
            for mid, sql in _MIGRATIONS:
                if mid in applied:
                    continue
                self._conn.executescript(sql)
                self._conn.execute(
                    "INSERT INTO schema_migrations(id, applied_at) VALUES (?, datetime('now'))",
                    (mid,),
                )
            self._conn.commit()

    # ---- low-level access -------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def write(self, fn):
        """Run fn(conn) inside BEGIN IMMEDIATE under the write lock; commit on
        success, rollback on error. fn returns whatever it wants."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- feed rows --------------------------------------------------------
    def insert_row(self, row: dict[str, Any]) -> None:
        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO eval_runs "
                "(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (row["id"], row["ran_at"], int(row["eval_output_schema_version"]),
                 row["miner_hotkey"], row["task_type"], json.dumps(row)),
            )
        self.write(_do)

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
        ordered ascending — exactly the released validator's pull semantics."""
        if since_ran_at is None:
            rows = self.query(
                "SELECT row_json FROM eval_runs ORDER BY ran_at ASC, id ASC LIMIT ?",
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
