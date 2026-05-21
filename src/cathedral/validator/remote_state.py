"""Durable validator state for the remote weight source (issue #155).

Survives validator restart so the durable rollback-protection rule
holds across reboots:

* ``last_accepted_policy_version`` + ``last_accepted_vector_id`` +
  ``last_accepted_at`` capture the most recent vector the validator
  passed signature + invariant checks for.
* ``last_applied_policy_version`` + ``last_applied_at`` capture the
  most recent vector the validator actually shipped to chain. These
  diverge from the "accepted" pair only when set_weights returned
  non-HEALTHY (e.g. blocked by stake) - the row still counts as
  accepted but never made it on-chain.

A single-row table keeps the bookkeeping trivial. The remote weight
loop reads this on every iteration to enforce:

1. monotonic rollback protection (refuse policy_version <
   last_accepted_policy_version, and refuse a different vector_id at
   the same policy_version)
2. idempotent chain submission (if vector_id == last_applied_vector_id
   AND last_applied_policy_version == last_accepted_policy_version, do
   not resubmit)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiosqlite

REMOTE_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS validator_remote_weight_state (
    -- Single-row table; we pin id=1.
    id                              INTEGER PRIMARY KEY CHECK (id = 1),
    last_accepted_policy_version    INTEGER,
    last_accepted_vector_id         TEXT,
    last_accepted_at                TEXT,
    last_applied_policy_version     INTEGER,
    last_applied_vector_id          TEXT,
    last_applied_at                 TEXT,
    cached_vector_json              TEXT,
    -- Free-form sidecar so we can record richer context (e.g. last
    -- rejection reason) without another migration. Application-layer
    -- only; the loop does not branch on its contents.
    sidecar_json                    TEXT
);
"""


@dataclass(frozen=True)
class RemoteWeightState:
    """Durable bookkeeping for the validator's remote weight source.

    All fields are nullable for the bootstrap path (a freshly-initialized
    validator has never accepted or applied any vector). The remote
    weight loop refuses set_weights when there is no accepted vector;
    that is the "remote startup with no vector refuses set_weights"
    branch from issue #155.
    """

    last_accepted_policy_version: int | None = None
    last_accepted_vector_id: str | None = None
    last_accepted_at: str | None = None
    last_applied_policy_version: int | None = None
    last_applied_vector_id: str | None = None
    last_applied_at: str | None = None
    cached_vector: dict[str, Any] | None = None
    sidecar: dict[str, Any] | None = None


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    """Idempotently create the remote-state table on ``conn``."""
    await conn.executescript(REMOTE_STATE_SCHEMA)
    cur = await conn.execute("PRAGMA table_info(validator_remote_weight_state)")
    columns = {str(row[1]) for row in await cur.fetchall()}
    if "cached_vector_json" not in columns:
        await conn.execute(
            "ALTER TABLE validator_remote_weight_state ADD COLUMN cached_vector_json TEXT"
        )
    await conn.commit()


async def load_state(conn: aiosqlite.Connection) -> RemoteWeightState:
    """Return the single-row state, or an all-None state if missing."""
    await ensure_schema(conn)
    cur = await conn.execute(
        "SELECT last_accepted_policy_version, last_accepted_vector_id, last_accepted_at, "
        "last_applied_policy_version, last_applied_vector_id, last_applied_at, "
        "cached_vector_json, sidecar_json "
        "FROM validator_remote_weight_state WHERE id = 1"
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
    vector_payload: dict[str, Any] | None = None,
    sidecar: dict[str, Any] | None = None,
) -> None:
    """Persist that a vector was accepted (signature + invariants OK).

    Does NOT touch the ``applied`` columns - apply is recorded separately
    after the chain call succeeds. Sidecar is replaced wholesale when
    provided; pass ``None`` to leave it alone.
    """
    await ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    sidecar_json = json.dumps(sidecar, sort_keys=True) if sidecar is not None else None
    vector_json = json.dumps(vector_payload, sort_keys=True) if vector_payload is not None else None

    # UPSERT with COALESCE so we never clobber the existing applied
    # columns when only recording an accept.
    await conn.execute(
        """
        INSERT INTO validator_remote_weight_state (
            id, last_accepted_policy_version, last_accepted_vector_id, last_accepted_at,
            last_applied_policy_version, last_applied_vector_id, last_applied_at,
            cached_vector_json, sidecar_json
        ) VALUES (1, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            last_accepted_policy_version = excluded.last_accepted_policy_version,
            last_accepted_vector_id      = excluded.last_accepted_vector_id,
            last_accepted_at             = excluded.last_accepted_at,
            cached_vector_json           = COALESCE(
                excluded.cached_vector_json, cached_vector_json
            ),
            sidecar_json                 = COALESCE(excluded.sidecar_json, sidecar_json)
        """,
        (policy_version, vector_id, now, vector_json, sidecar_json),
    )
    await conn.commit()


async def record_applied(
    conn: aiosqlite.Connection,
    *,
    policy_version: int,
    vector_id: str,
) -> None:
    """Persist that the accepted vector was successfully shipped to chain.

    Assumes ``record_accepted`` already ran for this vector. Idempotent
    - re-applying the same (policy_version, vector_id) only updates the
    timestamp.
    """
    await ensure_schema(conn)
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    await conn.execute(
        """
        INSERT INTO validator_remote_weight_state (
            id, last_accepted_policy_version, last_accepted_vector_id, last_accepted_at,
            last_applied_policy_version, last_applied_vector_id, last_applied_at,
            cached_vector_json, sidecar_json
        ) VALUES (1, ?, ?, ?, ?, ?, ?, NULL, NULL)
        ON CONFLICT(id) DO UPDATE SET
            last_applied_policy_version = excluded.last_applied_policy_version,
            last_applied_vector_id      = excluded.last_applied_vector_id,
            last_applied_at             = excluded.last_applied_at
        """,
        (policy_version, vector_id, now, policy_version, vector_id, now),
    )
    await conn.commit()


def is_rollback(state: RemoteWeightState, *, candidate_policy_version: int) -> bool:
    """True iff accepting ``candidate_policy_version`` would be a rollback.

    A first-ever accept (state.last_accepted_policy_version is None) is
    never a rollback. Strict less-than is the legacy helper rule; the
    remote loop uses ``is_replay_or_rollback`` for the stronger vector
    id check.
    """
    if state.last_accepted_policy_version is None:
        return False
    return candidate_policy_version < state.last_accepted_policy_version


def is_replay_or_rollback(
    state: RemoteWeightState,
    *,
    candidate_policy_version: int,
    candidate_vector_id: str,
) -> bool:
    """True if accepting this vector would replay old policy state.

    A policy_version older than the last accepted version is always a
    rollback. An equal policy_version is allowed only for the exact same
    vector_id, which lets a publisher re-serve the same artifact without
    letting a different vector masquerade under the same version.
    """
    if state.last_accepted_policy_version is None:
        return False
    if candidate_policy_version < state.last_accepted_policy_version:
        return True
    return (
        candidate_policy_version == state.last_accepted_policy_version
        and candidate_vector_id != state.last_accepted_vector_id
    )


def already_applied(
    state: RemoteWeightState,
    *,
    candidate_policy_version: int,
    candidate_vector_id: str,
) -> bool:
    """True iff the validator already shipped this exact vector to chain.

    Both policy_version and vector_id must match. A publisher that
    re-serves the same vector_id under a new policy_version (allowed in
    principle though not expected) will still trigger one chain submit
    per policy_version bump.
    """
    return (
        state.last_applied_policy_version == candidate_policy_version
        and state.last_applied_vector_id == candidate_vector_id
    )
