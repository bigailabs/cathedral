"""Publisher-side private challenge source for Task Family lanes.

A *challenge source* is the publisher's local store of currently-active
private challenge material: the raw CNF (for boolean lanes), tier label,
status flags, and any publisher-only audit metadata. It is the layer
that the launch loop reads from when it needs to publish "the active
challenge" and the layer that gets locked after the receipt state
machine selects the first-submitted valid solution.

This module deliberately does NOT contain real launch corpora. It only
defines:

* :class:`ChallengeRecord` -- the typed shape the launch loop consumes.
* :class:`ChallengeSource` -- abstract interface with the three
  operations the launch loop needs.
* :class:`InMemoryChallengeSource` -- a fake source backed by a list,
  used by tests and the publisher dry-run path.
* :class:`SqliteChallengeSource` -- a file-backed source using the same
  aiosqlite + WAL pattern as the rest of the publisher. Suitable for
  the first local launch.

The Supabase-backed source is deferred. The interface here is
deliberately narrow so a future ``SupabaseChallengeSource`` can drop in
without changing the lane, the launch loop, or the verifier.

Wire safety: this module is publisher-private. Nothing here is signed,
nothing reaches a public projection without first being routed through
:func:`cathedral.lanes.sign.build_signed_task_family_row`, which drops
raw CNF and answers. The leak guard in
``tests/lanes/test_public_repo_leak_guard.py`` continues to assert that
nothing private surfaces on the wire.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiosqlite

CHALLENGE_STATUS_PENDING = "pending"
CHALLENGE_STATUS_ACTIVE = "active"
CHALLENGE_STATUS_LOCKED = "locked"
CHALLENGE_STATUS_RETIRED = "retired"

_ALLOWED_STATUSES = frozenset(
    {
        CHALLENGE_STATUS_PENDING,
        CHALLENGE_STATUS_ACTIVE,
        CHALLENGE_STATUS_LOCKED,
        CHALLENGE_STATUS_RETIRED,
    }
)


class ChallengeSourceError(Exception):
    """Raised by source implementations for non-validation failures."""


@dataclass(frozen=True)
class ChallengeRecord:
    """One private challenge row.

    ``cnf_text`` and ``cnf_path`` are publisher-private. Adapters must
    not echo either to a public projection. Text-backed rows carry the
    DIMACS body in ``cnf_text``; file-backed rows carry a local path in
    ``cnf_path`` and leave ``cnf_text`` empty. Treat this dataclass the
    way you would treat a Supabase row: it crosses the publisher
    boundary but never reaches a miner-visible feed.
    """

    challenge_id: str
    family_id: str
    tier: int
    cnf_text: str
    status: str
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    cnf_path: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_STATUSES:
            raise ChallengeSourceError(f"unknown challenge status: {self.status!r}")


class ChallengeSource(Protocol):
    """Minimal interface for a publisher-side private challenge store.

    Implementations must support concurrent readers but serialize writes
    enough that ``mark_locked`` is atomic; SQLite's transactional UPDATE
    is sufficient.
    """

    async def get_active(self, family_id: str) -> ChallengeRecord | None:
        """Return the currently active challenge for the family, or None."""
        ...

    async def list_for_family(
        self, family_id: str, *, status: str | None = None
    ) -> list[ChallengeRecord]:
        """Return all challenges for the family, optionally filtered by status."""
        ...

    async def upsert(self, record: ChallengeRecord, *, overwrite_status: bool = False) -> None:
        """Insert or update a challenge row."""
        ...

    async def activate(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        retire_current: bool = False,
    ) -> ChallengeRecord:
        """Activate one queued challenge for a family."""
        ...

    async def mark_locked_and_promote_next(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        manage_transaction: bool = True,
    ) -> ChallengeRecord | None:
        """Lock the solved challenge and activate the next pending row."""
        ...


# --------------------------------------------------------------------------
# In-memory fake (tests + dry-runs)
# --------------------------------------------------------------------------


class InMemoryChallengeSource:
    """Process-local fake. Not thread-safe across event loops; one
    publisher process owns it. Tests use this to exercise the launch
    loop without touching disk."""

    def __init__(self) -> None:
        self._rows: dict[str, ChallengeRecord] = {}

    async def get_active(self, family_id: str) -> ChallengeRecord | None:
        for rec in self._rows.values():
            if rec.family_id == family_id and rec.status == CHALLENGE_STATUS_ACTIVE:
                return rec
        return None

    async def list_for_family(
        self, family_id: str, *, status: str | None = None
    ) -> list[ChallengeRecord]:
        out: list[ChallengeRecord] = []
        for rec in self._rows.values():
            if rec.family_id != family_id:
                continue
            if status is not None and rec.status != status:
                continue
            out.append(rec)
        return sorted(out, key=lambda r: r.challenge_id)

    async def upsert(self, record: ChallengeRecord, *, overwrite_status: bool = False) -> None:
        current = self._rows.get(record.challenge_id)
        if current is not None and not overwrite_status:
            self._rows[record.challenge_id] = ChallengeRecord(
                challenge_id=record.challenge_id,
                family_id=record.family_id,
                tier=record.tier,
                cnf_text=record.cnf_text,
                status=current.status,
                audit_metadata=record.audit_metadata,
                cnf_path=record.cnf_path,
            )
            return
        self._rows[record.challenge_id] = record

    async def activate(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        retire_current: bool = False,
    ) -> ChallengeRecord:
        target = self._rows.get(challenge_id)
        if target is None or target.family_id != family_id:
            raise ChallengeSourceError("challenge not found")
        if target.status not in {CHALLENGE_STATUS_PENDING, CHALLENGE_STATUS_ACTIVE}:
            raise ChallengeSourceError("challenge is not activatable")

        active = await self.get_active(family_id)
        if active is not None and active.challenge_id != challenge_id:
            if not retire_current:
                raise ChallengeSourceError("another active challenge exists")
            self._rows[active.challenge_id] = ChallengeRecord(
                challenge_id=active.challenge_id,
                family_id=active.family_id,
                tier=active.tier,
                cnf_text=active.cnf_text,
                status=CHALLENGE_STATUS_RETIRED,
                audit_metadata=active.audit_metadata,
                cnf_path=active.cnf_path,
            )

        activated = ChallengeRecord(
            challenge_id=target.challenge_id,
            family_id=target.family_id,
            tier=target.tier,
            cnf_text=target.cnf_text,
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata=target.audit_metadata,
            cnf_path=target.cnf_path,
        )
        self._rows[challenge_id] = activated
        return activated

    async def mark_locked_and_promote_next(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        manage_transaction: bool = True,
    ) -> ChallengeRecord | None:
        current = self._rows.get(challenge_id)
        if current is not None and current.family_id == family_id:
            self._rows[challenge_id] = ChallengeRecord(
                challenge_id=current.challenge_id,
                family_id=current.family_id,
                tier=current.tier,
                cnf_text=current.cnf_text,
                status=CHALLENGE_STATUS_LOCKED,
                audit_metadata=current.audit_metadata,
                cnf_path=current.cnf_path,
            )

        if await self.get_active(family_id) is not None:
            return None

        pending = await self.list_for_family(family_id, status=CHALLENGE_STATUS_PENDING)
        if not pending:
            return None
        return await self.activate(
            family_id=family_id,
            challenge_id=pending[0].challenge_id,
            now_iso=now_iso,
        )


# --------------------------------------------------------------------------
# SQLite-backed source (first-launch default)
# --------------------------------------------------------------------------


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_challenges (
    challenge_id    TEXT PRIMARY KEY,
    family_id       TEXT NOT NULL,
    tier            INTEGER NOT NULL,
    cnf_text        TEXT NOT NULL,
    cnf_path        TEXT,
    status          TEXT NOT NULL CHECK (status IN ('pending','active','locked','retired')),
    audit_metadata  TEXT NOT NULL,
    created_at_iso  TEXT NOT NULL,
    updated_at_iso  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lane_challenges_family_status
    ON lane_challenges(family_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lane_challenges_one_active_per_family
    ON lane_challenges(family_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS lane_challenge_fetch_tokens (
    challenge_id              TEXT PRIMARY KEY,
    fetch_token               TEXT NOT NULL,
    minted_at_iso             TEXT NOT NULL,
    announced_time_limit_secs INTEGER NOT NULL
);
"""


async def init_sqlite_challenge_source(database_path: str) -> aiosqlite.Connection:
    """Open the publisher-side private challenge SQLite and apply schema.

    Uses WAL the same way the rest of the publisher does. Caller owns
    the connection lifecycle. The database file is publisher-private
    and never ships with the public repo.
    """
    conn = await aiosqlite.connect(database_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await ensure_sqlite_challenge_source_schema(conn)
    return conn


async def ensure_sqlite_challenge_source_schema(conn: aiosqlite.Connection) -> None:
    """Apply the challenge-source schema and lightweight additive migrations."""
    await conn.executescript(SQLITE_SCHEMA)
    cur = await conn.execute("PRAGMA table_info(lane_challenges)")
    columns = {str(row[1]) for row in await cur.fetchall()}
    if "cnf_path" not in columns:
        await conn.execute("ALTER TABLE lane_challenges ADD COLUMN cnf_path TEXT")
    await conn.commit()


class SqliteChallengeSource:
    """File-backed challenge source. Wraps an :mod:`aiosqlite` connection.

    Single writer (the publisher); concurrent readers are tolerated by
    the WAL pragma. The store does NOT carry credentials, miner hotkeys,
    or solver output. It carries the CNF the publisher chose to make
    active, plus tier and audit metadata.
    """

    def __init__(self, conn: aiosqlite.Connection, *, now_iso: str | None = None) -> None:
        self._conn = conn
        # ``now_iso`` is publisher-supplied. The class does NOT read the
        # host clock; the launch loop passes the current epoch timestamp
        # when seeding rows.
        self._default_now_iso = now_iso

    async def get_active(self, family_id: str) -> ChallengeRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata "
            "FROM lane_challenges WHERE family_id = ? AND status = ? LIMIT 1",
            (family_id, CHALLENGE_STATUS_ACTIVE),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def get_for_endpoint(self, challenge_id: str) -> EndpointLookup | None:
        """Single-row read for the public CNF endpoint.

        Returns CNF storage material plus the two fields the route needs
        to decide whether to serve: ``status`` (must be ``active`` or
        ``locked``) and ``updated_at_iso`` (last status flip, used to
        compute the post-lock grace window). Returns ``None`` on miss;
        the caller responds 404 the same way for unknown ids and
        disallowed statuses so the endpoint never becomes an existence
        oracle.
        """
        cur = await self._conn.execute(
            "SELECT cnf_text, cnf_path, status, updated_at_iso "
            "FROM lane_challenges WHERE challenge_id = ? LIMIT 1",
            (challenge_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cnf_text, cnf_path, status, updated_at_iso = row
        return EndpointLookup(
            cnf_text=str(cnf_text),
            cnf_path=str(cnf_path) if cnf_path else None,
            status=str(status),
            updated_at_iso=str(updated_at_iso),
        )

    async def list_for_family(
        self, family_id: str, *, status: str | None = None
    ) -> list[ChallengeRecord]:
        if status is None:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                "status, audit_metadata "
                "FROM lane_challenges WHERE family_id = ? ORDER BY challenge_id",
                (family_id,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                "status, audit_metadata "
                "FROM lane_challenges WHERE family_id = ? AND status = ? "
                "ORDER BY challenge_id",
                (family_id, status),
            )
        rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def upsert(
        self,
        record: ChallengeRecord,
        *,
        now_iso: str | None = None,
        overwrite_status: bool = False,
    ) -> None:
        ts = now_iso or self._default_now_iso or "1970-01-01T00:00:00.000Z"
        try:
            if overwrite_status:
                upsert_sql = """
                    INSERT INTO lane_challenges (
                        challenge_id, family_id, tier, cnf_text, cnf_path, status,
                        audit_metadata, created_at_iso, updated_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(challenge_id) DO UPDATE SET
                        family_id=excluded.family_id,
                        tier=excluded.tier,
                        cnf_text=excluded.cnf_text,
                        cnf_path=excluded.cnf_path,
                        status=excluded.status,
                        audit_metadata=excluded.audit_metadata,
                        updated_at_iso=excluded.updated_at_iso
                    """
            else:
                upsert_sql = """
                    INSERT INTO lane_challenges (
                        challenge_id, family_id, tier, cnf_text, cnf_path, status,
                        audit_metadata, created_at_iso, updated_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(challenge_id) DO UPDATE SET
                        family_id=excluded.family_id,
                        tier=excluded.tier,
                        cnf_text=excluded.cnf_text,
                        cnf_path=excluded.cnf_path,
                        audit_metadata=excluded.audit_metadata,
                        updated_at_iso=excluded.updated_at_iso
                    """
            await self._conn.execute(
                upsert_sql,
                (
                    record.challenge_id,
                    record.family_id,
                    record.tier,
                    record.cnf_text,
                    record.cnf_path,
                    record.status,
                    json.dumps(record.audit_metadata, sort_keys=True),
                    ts,
                    ts,
                ),
            )
            await self._conn.commit()
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise ChallengeSourceError("challenge source constraint violation") from exc

    async def activate(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        retire_current: bool = False,
    ) -> ChallengeRecord:
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            target = await self._fetch_one_for_update(family_id, challenge_id)
            if target is None:
                raise ChallengeSourceError("challenge not found")
            if target.status not in {CHALLENGE_STATUS_PENDING, CHALLENGE_STATUS_ACTIVE}:
                raise ChallengeSourceError("challenge is not activatable")

            active = await self._fetch_active_for_update(family_id)
            if active is not None and active.challenge_id != challenge_id:
                if not retire_current:
                    raise ChallengeSourceError("another active challenge exists")
                await self._conn.execute(
                    "UPDATE lane_challenges SET status = ?, updated_at_iso = ? "
                    "WHERE family_id = ? AND challenge_id = ?",
                    (
                        CHALLENGE_STATUS_RETIRED,
                        now_iso,
                        family_id,
                        active.challenge_id,
                    ),
                )

            await self._conn.execute(
                "UPDATE lane_challenges SET status = ?, updated_at_iso = ? "
                "WHERE family_id = ? AND challenge_id = ?",
                (
                    CHALLENGE_STATUS_ACTIVE,
                    now_iso,
                    family_id,
                    challenge_id,
                ),
            )
            await self._conn.commit()
        except ChallengeSourceError:
            await self._conn.rollback()
            raise
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise ChallengeSourceError("challenge source constraint violation") from exc

        activated = await self.get_active(family_id)
        if activated is None or activated.challenge_id != challenge_id:
            raise ChallengeSourceError("challenge activation failed")
        return activated

    async def mark_locked_and_promote_next(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        manage_transaction: bool = True,
    ) -> ChallengeRecord | None:
        try:
            if manage_transaction:
                await self._conn.execute("BEGIN IMMEDIATE")
            await self._conn.execute(
                "UPDATE lane_challenges SET status = ?, updated_at_iso = ? "
                "WHERE family_id = ? AND challenge_id = ? AND status = ?",
                (
                    CHALLENGE_STATUS_LOCKED,
                    now_iso,
                    family_id,
                    challenge_id,
                    CHALLENGE_STATUS_ACTIVE,
                ),
            )

            active = await self._fetch_active_for_update(family_id)
            if active is not None:
                if manage_transaction:
                    await self._conn.commit()
                return None

            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                "status, audit_metadata "
                "FROM lane_challenges WHERE family_id = ? AND status = ? "
                "ORDER BY created_at_iso ASC, challenge_id ASC LIMIT 1",
                (family_id, CHALLENGE_STATUS_PENDING),
            )
            row = await cur.fetchone()
            if row is None:
                if manage_transaction:
                    await self._conn.commit()
                return None

            pending = _row_to_record(row)
            await self._conn.execute(
                "UPDATE lane_challenges SET status = ?, updated_at_iso = ? "
                "WHERE family_id = ? AND challenge_id = ?",
                (
                    CHALLENGE_STATUS_ACTIVE,
                    now_iso,
                    family_id,
                    pending.challenge_id,
                ),
            )
            if manage_transaction:
                await self._conn.commit()
            return ChallengeRecord(
                challenge_id=pending.challenge_id,
                family_id=pending.family_id,
                tier=pending.tier,
                cnf_text=pending.cnf_text,
                cnf_path=pending.cnf_path,
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata=pending.audit_metadata,
            )
        except aiosqlite.IntegrityError as exc:
            if manage_transaction:
                await self._conn.rollback()
            raise ChallengeSourceError("challenge source constraint violation") from exc
        except Exception:
            if manage_transaction:
                await self._conn.rollback()
            raise

    async def _fetch_one_for_update(
        self, family_id: str, challenge_id: str
    ) -> ChallengeRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata "
            "FROM lane_challenges WHERE family_id = ? AND challenge_id = ? LIMIT 1",
            (family_id, challenge_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def _fetch_active_for_update(self, family_id: str) -> ChallengeRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata "
            "FROM lane_challenges WHERE family_id = ? AND status = ? LIMIT 1",
            (family_id, CHALLENGE_STATUS_ACTIVE),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)


@dataclass(frozen=True)
class EndpointLookup:
    """Minimal projection of ``lane_challenges`` for the public CNF endpoint.

    The endpoint only needs the storage pointer/body it serves and the
    two fields that decide whether to serve at all. Returning a narrow
    type (rather than a full :class:`ChallengeRecord`) keeps the
    cardinal-sin surface small: no audit metadata, no family id, no tier
    flow through this path.
    """

    cnf_text: str
    cnf_path: str | None
    status: str
    updated_at_iso: str


@dataclass(frozen=True)
class FetchTokenRecord:
    """One row in ``lane_challenge_fetch_tokens``.

    Publisher-private. Never logged, never serialized to a public
    projection. The token is the secret that turns ``challenge_id`` from
    a deterministic CNF hash into something unguessable on the wire.
    """

    challenge_id: str
    fetch_token: str
    minted_at_iso: str
    announced_time_limit_secs: int


class SqliteFetchTokenStore:
    """Side-table store for per-challenge announcement tokens.

    The orchestrator calls :meth:`mint_if_absent` at prompt-issuance time
    so every miner in the same batch ends up with the same token (the
    SQL is ``INSERT OR IGNORE``). The endpoint calls :meth:`get` to
    validate ``?t=`` against the row before serving CNF. The store
    deliberately knows nothing about status or grace; status checks live
    on the source row, not on the token.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def mint_if_absent(
        self,
        challenge_id: str,
        *,
        fetch_token: str,
        minted_at_iso: str,
        announced_time_limit_secs: int,
    ) -> FetchTokenRecord:
        """Insert a token row iff one does not already exist for this id.

        Idempotent across a batch and across retries. The caller passes
        a freshly-generated token; if a row already exists it is left
        intact and returned. This guarantees every miner pulled in the
        same announcement window sees the same token.
        """
        if announced_time_limit_secs <= 0:
            raise ChallengeSourceError("announced_time_limit_secs must be positive")
        await self._conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_fetch_tokens "
            "(challenge_id, fetch_token, minted_at_iso, announced_time_limit_secs) "
            "VALUES (?, ?, ?, ?)",
            (challenge_id, fetch_token, minted_at_iso, int(announced_time_limit_secs)),
        )
        await self._conn.commit()
        existing = await self.get(challenge_id)
        if existing is None:
            raise ChallengeSourceError("fetch token row vanished after insert")
        return existing

    async def get(self, challenge_id: str) -> FetchTokenRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, fetch_token, minted_at_iso, announced_time_limit_secs "
            "FROM lane_challenge_fetch_tokens WHERE challenge_id = ? LIMIT 1",
            (challenge_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cid, token, minted, secs = row
        return FetchTokenRecord(
            challenge_id=str(cid),
            fetch_token=str(token),
            minted_at_iso=str(minted),
            announced_time_limit_secs=int(secs),
        )


def _row_to_record(row: Sequence[Any]) -> ChallengeRecord:
    challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_json = row
    audit: dict[str, Any]
    try:
        audit = json.loads(audit_json) if audit_json else {}
    except json.JSONDecodeError:
        audit = {}
    return ChallengeRecord(
        challenge_id=str(challenge_id),
        family_id=str(family_id),
        tier=int(tier),
        cnf_text=str(cnf_text),
        cnf_path=str(cnf_path) if cnf_path else None,
        status=str(status),
        audit_metadata=audit,
    )


__all__ = [
    "CHALLENGE_STATUS_ACTIVE",
    "CHALLENGE_STATUS_LOCKED",
    "CHALLENGE_STATUS_PENDING",
    "CHALLENGE_STATUS_RETIRED",
    "SQLITE_SCHEMA",
    "ChallengeRecord",
    "ChallengeSource",
    "ChallengeSourceError",
    "EndpointLookup",
    "FetchTokenRecord",
    "InMemoryChallengeSource",
    "SqliteChallengeSource",
    "SqliteFetchTokenStore",
    "ensure_sqlite_challenge_source_schema",
    "init_sqlite_challenge_source",
]
