"""Durable state for remote signed-weight vectors."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

REMOTE_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS validator_remote_weight_state (
    id                              INTEGER PRIMARY KEY CHECK (id = 1),
    last_accepted_policy_version    INTEGER,
    last_accepted_vector_id         TEXT,
    last_accepted_at                TEXT,
    last_applied_policy_version     INTEGER,
    last_applied_vector_id          TEXT,
    last_applied_at                 TEXT,
    cached_vector_json              TEXT,
    sidecar_json                    TEXT
);
"""


@dataclass(frozen=True)
class RemoteWeightState:
    last_accepted_policy_version: int | None = None
    last_accepted_vector_id: str | None = None
    last_accepted_at: str | None = None
    last_applied_policy_version: int | None = None
    last_applied_vector_id: str | None = None
    last_applied_at: str | None = None
    cached_vector: dict[str, Any] | None = None
    sidecar: dict[str, Any] | None = None


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(REMOTE_STATE_SCHEMA)
    await conn.commit()


async def load_state(conn: aiosqlite.Connection) -> RemoteWeightState:
    await ensure_schema(conn)
    cur = await conn.execute(
        """
        SELECT last_accepted_policy_version, last_accepted_vector_id, last_accepted_at,
               last_applied_policy_version, last_applied_vector_id, last_applied_at,
               cached_vector_json, sidecar_json
        FROM validator_remote_weight_state WHERE id = 1
        """
    )
    row = await cur.fetchone()
    if row is None:
        return RemoteWeightState()
    return RemoteWeightState(
        last_accepted_policy_version=row[0],
        last_accepted_vector_id=row[1],
        last_accepted_at=row[2],
        last_applied_policy_version=row[3],
        last_applied_vector_id=row[4],
        last_applied_at=row[5],
        cached_vector=json.loads(row[6]) if row[6] else None,
        sidecar=json.loads(row[7]) if row[7] else None,
    )


async def record_accepted(
    conn: aiosqlite.Connection,
    *,
    policy_version: int,
    vector_id: str,
    vector_payload: dict[str, Any],
    sidecar: dict[str, Any] | None = None,
) -> None:
    await ensure_schema(conn)
    now = _now_iso()
    await conn.execute(
        """
        INSERT INTO validator_remote_weight_state (
            id, last_accepted_policy_version, last_accepted_vector_id, last_accepted_at,
            last_applied_policy_version, last_applied_vector_id, last_applied_at,
            cached_vector_json, sidecar_json
        ) VALUES (1, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_accepted_policy_version = excluded.last_accepted_policy_version,
            last_accepted_vector_id = excluded.last_accepted_vector_id,
            last_accepted_at = excluded.last_accepted_at,
            cached_vector_json = excluded.cached_vector_json,
            sidecar_json = COALESCE(excluded.sidecar_json, sidecar_json)
        """,
        (
            policy_version,
            vector_id,
            now,
            json.dumps(vector_payload, sort_keys=True),
            json.dumps(sidecar, sort_keys=True) if sidecar is not None else None,
        ),
    )
    await conn.commit()


async def record_applied(
    conn: aiosqlite.Connection,
    *,
    policy_version: int,
    vector_id: str,
) -> None:
    await ensure_schema(conn)
    now = _now_iso()
    await conn.execute(
        """
        INSERT INTO validator_remote_weight_state (
            id, last_accepted_policy_version, last_accepted_vector_id, last_accepted_at,
            last_applied_policy_version, last_applied_vector_id, last_applied_at,
            cached_vector_json, sidecar_json
        ) VALUES (1, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(id) DO UPDATE SET
            last_applied_policy_version = excluded.last_applied_policy_version,
            last_applied_vector_id = excluded.last_applied_vector_id,
            last_applied_at = excluded.last_applied_at
        """,
        (policy_version, vector_id, now, policy_version, vector_id, now),
    )
    await conn.commit()


def is_replay_or_rollback(
    state: RemoteWeightState,
    *,
    candidate_policy_version: int,
    candidate_vector_id: str,
) -> bool:
    if state.last_accepted_policy_version is None:
        return False
    if candidate_policy_version < state.last_accepted_policy_version:
        return True
    return (
        candidate_policy_version == state.last_accepted_policy_version
        and candidate_vector_id != state.last_accepted_vector_id
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
