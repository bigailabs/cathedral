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
from typing import Any, Literal, Protocol

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

ActiveChallengeScope = Literal["family", "tier", "tier_difficulty"]
_ALLOWED_ACTIVE_SCOPES = frozenset({"family", "tier", "tier_difficulty"})


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

    ``score_multiplier`` (default ``1.0``) is the per-challenge payout
    weight applied by ``weight_policy`` when aggregating Task Family
    rows per hotkey (issue #236). ``difficulty_label`` (default
    ``None``) is the operator-set bucket that lets multiple challenges
    share a ``(family, tier)`` slot under
    ``active_scope='tier_difficulty'`` (issue #241). Both are publisher
    metadata and surface on the public projections.
    """

    challenge_id: str
    family_id: str
    tier: int
    cnf_text: str
    status: str
    audit_metadata: dict[str, Any] = field(default_factory=dict)
    cnf_path: str | None = None
    losers_published_at_iso: str | None = None
    score_multiplier: float = 1.0
    difficulty_label: str | None = None

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
        """Return the default active challenge for the family, or None."""
        ...

    async def get_active_for_tier(self, family_id: str, tier: int) -> ChallengeRecord | None:
        """Return the currently active challenge for one family+tier slot, or None."""
        ...

    async def list_active(self, family_id: str) -> list[ChallengeRecord]:
        """Return all active challenges for the family, ordered by tier then id."""
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
        active_scope: ActiveChallengeScope = "family",
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
        active_scope: ActiveChallengeScope = "family",
    ) -> ChallengeRecord | None:
        """Lock the solved challenge and activate the next pending row."""
        ...

    async def promote_pending_batch(
        self,
        family_id: str,
        *,
        tier: int,
        now_iso: str,
        max_count: int,
        kind: str | None = None,
        difficulty_label: str | None = None,
    ) -> list[str]:
        """Promote up to ``max_count`` pending rows in one operation."""
        ...

    async def list_locked_needing_loser_reconciliation(
        self, family_id: str, *, limit: int = 32
    ) -> list[ChallengeRecord]:
        """Return locked challenges whose loser rows have not been marked done."""
        ...

    async def mark_locked_loser_reconciliation_complete(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
    ) -> None:
        """Durably mark locked-challenge loser publication complete."""
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
        active = await self.list_active(family_id)
        return active[0] if active else None

    async def get_active_for_tier(self, family_id: str, tier: int) -> ChallengeRecord | None:
        active = [
            rec
            for rec in self._rows.values()
            if rec.family_id == family_id
            and rec.tier == int(tier)
            and rec.status == CHALLENGE_STATUS_ACTIVE
        ]
        return sorted(active, key=lambda r: r.challenge_id)[0] if active else None

    async def list_active(self, family_id: str) -> list[ChallengeRecord]:
        return sorted(
            [
                rec
                for rec in self._rows.values()
                if rec.family_id == family_id and rec.status == CHALLENGE_STATUS_ACTIVE
            ],
            key=lambda r: (r.tier, r.challenge_id),
        )

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
        if (
            current is not None
            and current.status != CHALLENGE_STATUS_PENDING
            and not _challenge_material_matches(current, record)
        ):
            raise ChallengeSourceError(
                "challenge material is immutable once the challenge is active"
            )
        if current is not None and not overwrite_status:
            self._rows[record.challenge_id] = ChallengeRecord(
                challenge_id=record.challenge_id,
                family_id=record.family_id,
                tier=record.tier,
                cnf_text=record.cnf_text,
                status=current.status,
                audit_metadata=record.audit_metadata,
                cnf_path=record.cnf_path,
                losers_published_at_iso=current.losers_published_at_iso,
                score_multiplier=record.score_multiplier,
                difficulty_label=record.difficulty_label,
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
        active_scope: ActiveChallengeScope = "family",
    ) -> ChallengeRecord:
        target = self._rows.get(challenge_id)
        if target is None or target.family_id != family_id:
            raise ChallengeSourceError("challenge not found")
        if target.status not in {CHALLENGE_STATUS_PENDING, CHALLENGE_STATUS_ACTIVE}:
            raise ChallengeSourceError("challenge is not activatable")
        if active_scope not in _ALLOWED_ACTIVE_SCOPES:
            raise ChallengeSourceError(
                "active_scope must be 'family', 'tier', or 'tier_difficulty'"
            )

        # 'tier_difficulty' on an unlabeled target degrades to 'tier'
        # so legacy data retains the one-active-per-tier invariant.
        effective_scope: str = active_scope
        if effective_scope == "tier_difficulty" and target.difficulty_label is None:
            effective_scope = "tier"

        if effective_scope == "tier_difficulty":
            active_rows = [
                rec
                for rec in await self.list_active(family_id)
                if rec.tier == target.tier
                and rec.difficulty_label == target.difficulty_label
                and rec.challenge_id != challenge_id
            ]
        elif effective_scope == "tier":
            active_rows = [
                rec
                for rec in await self.list_active(family_id)
                if rec.tier == target.tier and rec.challenge_id != challenge_id
            ]
        else:
            active_rows = [
                rec
                for rec in await self.list_active(family_id)
                if rec.challenge_id != challenge_id
            ]
        if active_rows:
            if not retire_current:
                raise ChallengeSourceError("another active challenge exists")
            for active in active_rows:
                self._rows[active.challenge_id] = ChallengeRecord(
                    challenge_id=active.challenge_id,
                    family_id=active.family_id,
                    tier=active.tier,
                    cnf_text=active.cnf_text,
                    status=CHALLENGE_STATUS_RETIRED,
                    audit_metadata=active.audit_metadata,
                    cnf_path=active.cnf_path,
                    losers_published_at_iso=active.losers_published_at_iso,
                    score_multiplier=active.score_multiplier,
                    difficulty_label=active.difficulty_label,
                )

        activated = ChallengeRecord(
            challenge_id=target.challenge_id,
            family_id=target.family_id,
            tier=target.tier,
            cnf_text=target.cnf_text,
            status=CHALLENGE_STATUS_ACTIVE,
            audit_metadata=target.audit_metadata,
            cnf_path=target.cnf_path,
            losers_published_at_iso=target.losers_published_at_iso,
            score_multiplier=target.score_multiplier,
            difficulty_label=target.difficulty_label,
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
        active_scope: ActiveChallengeScope = "family",
    ) -> ChallengeRecord | None:
        if active_scope not in _ALLOWED_ACTIVE_SCOPES:
            raise ChallengeSourceError(
                "active_scope must be 'family', 'tier', or 'tier_difficulty'"
            )
        current = self._rows.get(challenge_id)
        current_tier = current.tier if current is not None else None
        current_difficulty = current.difficulty_label if current is not None else None
        if current is not None and current.family_id == family_id:
            self._rows[challenge_id] = ChallengeRecord(
                challenge_id=current.challenge_id,
                family_id=current.family_id,
                tier=current.tier,
                cnf_text=current.cnf_text,
                status=CHALLENGE_STATUS_LOCKED,
                audit_metadata=current.audit_metadata,
                cnf_path=current.cnf_path,
                losers_published_at_iso=None,
                score_multiplier=current.score_multiplier,
                difficulty_label=current.difficulty_label,
            )

        # 'tier_difficulty' on an unlabeled locked row degrades to 'tier'
        # so legacy rows keep the original 1-per-tier semantics.
        effective_scope: str = active_scope
        if effective_scope == "tier_difficulty" and current_difficulty is None:
            effective_scope = "tier"

        if effective_scope == "tier_difficulty" and current_tier is not None:
            existing = [
                rec
                for rec in await self.list_active(family_id)
                if rec.tier == current_tier and rec.difficulty_label == current_difficulty
            ]
            if existing:
                return None
            pending = [
                rec
                for rec in await self.list_for_family(
                    family_id, status=CHALLENGE_STATUS_PENDING
                )
                if rec.tier == current_tier and rec.difficulty_label == current_difficulty
            ]
        elif effective_scope == "tier" and current_tier is not None:
            if await self.get_active_for_tier(family_id, current_tier) is not None:
                return None
            pending = [
                rec
                for rec in await self.list_for_family(
                    family_id, status=CHALLENGE_STATUS_PENDING
                )
                if rec.tier == current_tier
            ]
        else:
            if await self.get_active(family_id) is not None:
                return None
            pending = await self.list_for_family(family_id, status=CHALLENGE_STATUS_PENDING)
        if not pending:
            return None
        return await self.activate(
            family_id=family_id,
            challenge_id=pending[0].challenge_id,
            now_iso=now_iso,
            active_scope=active_scope,
        )

    async def promote_pending_batch(
        self,
        family_id: str,
        *,
        tier: int,
        now_iso: str,
        max_count: int,
        kind: str | None = None,
        difficulty_label: str | None = None,
    ) -> list[str]:
        """Match :meth:`SqliteChallengeSource.promote_pending_batch` exactly.

        ``difficulty_label=None`` filters to unlabeled candidates ONLY
        so the legacy one-active-per-tier rule continues to hold.
        Stops iterating on the first ``ChallengeSourceError`` so the
        unlabeled "tier" scope yields at most one promotion per call
        — same as the SQLite path.
        """
        max_count = max(0, int(max_count))
        if max_count == 0:
            return []
        scope: ActiveChallengeScope = (
            "tier_difficulty" if difficulty_label is not None else "tier"
        )
        candidates: list[ChallengeRecord] = []
        for rec in await self.list_for_family(family_id, status=CHALLENGE_STATUS_PENDING):
            if rec.tier != int(tier):
                continue
            if difficulty_label is None:
                if rec.difficulty_label is not None:
                    continue
            elif rec.difficulty_label != difficulty_label:
                continue
            if kind is not None:
                audit_kind = (rec.audit_metadata or {}).get("kind")
                if audit_kind != kind:
                    continue
            candidates.append(rec)
            if len(candidates) >= max_count:
                break

        promoted: list[str] = []
        for cand in candidates:
            try:
                await self.activate(
                    family_id=family_id,
                    challenge_id=cand.challenge_id,
                    now_iso=now_iso,
                    active_scope=scope,
                )
            except ChallengeSourceError:
                break
            promoted.append(cand.challenge_id)
        return promoted

    async def list_locked_needing_loser_reconciliation(
        self, family_id: str, *, limit: int = 32
    ) -> list[ChallengeRecord]:
        rows = [
            rec
            for rec in self._rows.values()
            if rec.family_id == family_id
            and rec.status == CHALLENGE_STATUS_LOCKED
            and not rec.losers_published_at_iso
        ]
        return sorted(rows, key=lambda r: r.challenge_id, reverse=True)[: max(1, int(limit))]

    async def mark_locked_loser_reconciliation_complete(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
    ) -> None:
        current = self._rows.get(challenge_id)
        if current is None or current.family_id != family_id:
            raise ChallengeSourceError("challenge not found")
        if current.status != CHALLENGE_STATUS_LOCKED:
            raise ChallengeSourceError("challenge is not locked")
        self._rows[challenge_id] = ChallengeRecord(
            challenge_id=current.challenge_id,
            family_id=current.family_id,
            tier=current.tier,
            cnf_text=current.cnf_text,
            status=current.status,
            audit_metadata=current.audit_metadata,
            cnf_path=current.cnf_path,
            losers_published_at_iso=now_iso,
            score_multiplier=current.score_multiplier,
            difficulty_label=current.difficulty_label,
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
    losers_published_at_iso TEXT,
    created_at_iso  TEXT NOT NULL,
    updated_at_iso  TEXT NOT NULL,
    score_multiplier REAL NOT NULL DEFAULT 1.0,
    difficulty_label TEXT
);
CREATE INDEX IF NOT EXISTS idx_lane_challenges_family_status
    ON lane_challenges(family_id, status);

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
    """Apply the challenge-source schema and lightweight additive migrations.

    Idempotent: safe to call on every connection open. Migrations are
    purely additive (``ALTER TABLE ... ADD COLUMN`` with defaults, or
    ``CREATE INDEX IF NOT EXISTS``), so existing rows keep their values
    and existing queries continue to work. The
    one-active-per-(family, tier) unique index is rebuilt as a partial
    index over ``difficulty_label IS NULL`` so unlabeled rows retain
    the legacy invariant while labeled rows can share a tier slot.
    """
    await conn.executescript(SQLITE_SCHEMA)
    cur = await conn.execute("PRAGMA table_info(lane_challenges)")
    columns = {str(row[1]) for row in await cur.fetchall()}
    if "cnf_path" not in columns:
        await conn.execute("ALTER TABLE lane_challenges ADD COLUMN cnf_path TEXT")
    if "losers_published_at_iso" not in columns:
        await conn.execute("ALTER TABLE lane_challenges ADD COLUMN losers_published_at_iso TEXT")
    # PR-bundled additive migrations (#236 + #241). SQLite does not allow
    # adding a NOT NULL column without a default, so the score multiplier
    # column carries DEFAULT 1.0 — existing rows materialize as 1.0 and
    # keep producing the same weight contribution as before.
    if "score_multiplier" not in columns:
        await conn.execute(
            "ALTER TABLE lane_challenges ADD COLUMN score_multiplier REAL NOT NULL DEFAULT 1.0"
        )
    if "difficulty_label" not in columns:
        await conn.execute("ALTER TABLE lane_challenges ADD COLUMN difficulty_label TEXT")
    await conn.execute("DROP INDEX IF EXISTS idx_lane_challenges_one_active_per_family")
    # The legacy tier-only unique active index is being narrowed to apply
    # only to unlabeled rows so labeled rows can share a tier slot. Drop
    # any pre-existing (unconditional) version first so the rebuild picks
    # up the partial-index predicate even on already-migrated DBs.
    await conn.execute(
        "DROP INDEX IF EXISTS idx_lane_challenges_one_active_per_family_tier"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lane_challenges_one_active_per_family_tier "
        "ON lane_challenges(family_id, tier) "
        "WHERE status = 'active' AND difficulty_label IS NULL"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_lane_challenges_one_active_per_family_tier_difficulty "
        "ON lane_challenges(family_id, tier, difficulty_label) "
        "WHERE status = 'active' AND difficulty_label IS NOT NULL"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_challenges_tier_diff_status "
        "ON lane_challenges(family_id, tier, difficulty_label, status)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lane_challenges_locked_losers "
        "ON lane_challenges(family_id, status, losers_published_at_iso, updated_at_iso)"
    )
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
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata, "
            "score_multiplier, difficulty_label "
            "FROM lane_challenges WHERE family_id = ? AND status = ? "
            "ORDER BY tier ASC, challenge_id ASC LIMIT 1",
            (family_id, CHALLENGE_STATUS_ACTIVE),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def get_active_for_tier(self, family_id: str, tier: int) -> ChallengeRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata, "
            "score_multiplier, difficulty_label "
            "FROM lane_challenges WHERE family_id = ? AND tier = ? AND status = ? "
            "ORDER BY challenge_id ASC LIMIT 1",
            (family_id, int(tier), CHALLENGE_STATUS_ACTIVE),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def list_active(self, family_id: str) -> list[ChallengeRecord]:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata, "
            "score_multiplier, difficulty_label "
            "FROM lane_challenges WHERE family_id = ? AND status = ? "
            "ORDER BY tier ASC, challenge_id ASC",
            (family_id, CHALLENGE_STATUS_ACTIVE),
        )
        rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def get_for_endpoint(self, challenge_id: str) -> EndpointLookup | None:
        """Single-row read for the public CNF endpoint.

        Returns CNF storage material, the fields the route needs to
        decide whether to serve, and the announced CNF digest used to
        reject mutable file-backed rows whose bytes changed after
        seeding. Returns ``None`` on miss; the caller responds 404 the
        same way for unknown ids and disallowed statuses so the endpoint
        never becomes an existence oracle.
        """
        cur = await self._conn.execute(
            "SELECT cnf_text, cnf_path, status, updated_at_iso, audit_metadata "
            "FROM lane_challenges WHERE challenge_id = ? LIMIT 1",
            (challenge_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        cnf_text, cnf_path, status, updated_at_iso, audit_json = row
        try:
            audit = json.loads(str(audit_json)) if audit_json else {}
        except json.JSONDecodeError:
            audit = {}
        if isinstance(audit, dict):
            cnf_sha256 = audit.get("cnf_sha256")
            cnf_bytes = _positive_audit_int(audit.get("cnf_bytes"))
            max_cnf_bytes = _positive_audit_int(audit.get("max_cnf_bytes"))
        else:
            cnf_sha256 = None
            cnf_bytes = None
            max_cnf_bytes = None
        return EndpointLookup(
            cnf_text=str(cnf_text),
            cnf_path=str(cnf_path) if cnf_path else None,
            status=str(status),
            updated_at_iso=str(updated_at_iso),
            cnf_sha256=str(cnf_sha256) if cnf_sha256 else None,
            cnf_bytes=cnf_bytes,
            max_cnf_bytes=max_cnf_bytes,
        )

    async def list_for_family(
        self, family_id: str, *, status: str | None = None
    ) -> list[ChallengeRecord]:
        if status is None:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                "status, audit_metadata, score_multiplier, difficulty_label "
                "FROM lane_challenges WHERE family_id = ? ORDER BY challenge_id",
                (family_id,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                "status, audit_metadata, score_multiplier, difficulty_label "
                "FROM lane_challenges WHERE family_id = ? AND status = ? "
                "ORDER BY challenge_id",
                (family_id, status),
            )
        rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def list_locked_needing_loser_reconciliation(
        self, family_id: str, *, limit: int = 32
    ) -> list[ChallengeRecord]:
        limit = max(1, int(limit))
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, "
            "audit_metadata, score_multiplier, difficulty_label, "
            "losers_published_at_iso "
            "FROM lane_challenges "
            "WHERE family_id = ? AND status = ? AND losers_published_at_iso IS NULL "
            "ORDER BY updated_at_iso DESC, challenge_id DESC LIMIT ?",
            (family_id, CHALLENGE_STATUS_LOCKED, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def mark_locked_loser_reconciliation_complete(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
    ) -> None:
        cur = await self._conn.execute(
            "UPDATE lane_challenges "
            "SET losers_published_at_iso = ? "
            "WHERE family_id = ? AND challenge_id = ? AND status = ?",
            (
                now_iso,
                family_id,
                challenge_id,
                CHALLENGE_STATUS_LOCKED,
            ),
        )
        await self._conn.commit()
        if int(cur.rowcount or 0) != 1:
            raise ChallengeSourceError("locked challenge not found")

    async def upsert(
        self,
        record: ChallengeRecord,
        *,
        now_iso: str | None = None,
        overwrite_status: bool = False,
    ) -> None:
        ts = now_iso or self._default_now_iso or "1970-01-01T00:00:00.000Z"
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            current_cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, "
                "audit_metadata, score_multiplier, difficulty_label "
                "FROM lane_challenges WHERE challenge_id = ? LIMIT 1",
                (record.challenge_id,),
            )
            current_row = await current_cur.fetchone()
            if current_row is not None:
                current = _row_to_record(current_row)
                if (
                    current.status != CHALLENGE_STATUS_PENDING
                    and not _challenge_material_matches(current, record)
                ):
                    # Once a challenge has been announced, fetch tokens,
                    # receipts, and lock rows all refer to this exact private
                    # material. Reusing a custom challenge id for another CNF
                    # must fail instead of mutating the solved/active target.
                    raise ChallengeSourceError(
                        "challenge material is immutable once the challenge is active"
                    )
            if overwrite_status:
                upsert_sql = """
                    INSERT INTO lane_challenges (
                        challenge_id, family_id, tier, cnf_text, cnf_path, status,
                        audit_metadata, losers_published_at_iso,
                        score_multiplier, difficulty_label,
                        created_at_iso, updated_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(challenge_id) DO UPDATE SET
                        family_id=excluded.family_id,
                        tier=excluded.tier,
                        cnf_text=excluded.cnf_text,
                        cnf_path=excluded.cnf_path,
                        status=excluded.status,
                        audit_metadata=excluded.audit_metadata,
                        losers_published_at_iso=excluded.losers_published_at_iso,
                        score_multiplier=excluded.score_multiplier,
                        difficulty_label=excluded.difficulty_label,
                        updated_at_iso=excluded.updated_at_iso
                    """
            else:
                upsert_sql = """
                    INSERT INTO lane_challenges (
                        challenge_id, family_id, tier, cnf_text, cnf_path, status,
                        audit_metadata, losers_published_at_iso,
                        score_multiplier, difficulty_label,
                        created_at_iso, updated_at_iso
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(challenge_id) DO UPDATE SET
                        family_id=excluded.family_id,
                        tier=excluded.tier,
                        cnf_text=excluded.cnf_text,
                        cnf_path=excluded.cnf_path,
                        audit_metadata=excluded.audit_metadata,
                        score_multiplier=excluded.score_multiplier,
                        difficulty_label=excluded.difficulty_label,
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
                    record.losers_published_at_iso,
                    float(record.score_multiplier),
                    record.difficulty_label,
                    ts,
                    ts,
                ),
            )
            await self._conn.commit()
        except ChallengeSourceError:
            await self._conn.rollback()
            raise
        except aiosqlite.IntegrityError as exc:
            await self._conn.rollback()
            raise ChallengeSourceError("challenge source constraint violation") from exc
        except Exception:
            await self._conn.rollback()
            raise

    async def activate(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        retire_current: bool = False,
        active_scope: ActiveChallengeScope = "family",
    ) -> ChallengeRecord:
        try:
            if active_scope not in _ALLOWED_ACTIVE_SCOPES:
                raise ChallengeSourceError(
                    "active_scope must be 'family', 'tier', or 'tier_difficulty'"
                )
            await self._conn.execute("BEGIN IMMEDIATE")
            target = await self._fetch_one_for_update(family_id, challenge_id)
            if target is None:
                raise ChallengeSourceError("challenge not found")
            if target.status not in {CHALLENGE_STATUS_PENDING, CHALLENGE_STATUS_ACTIVE}:
                raise ChallengeSourceError("challenge is not activatable")

            # 'tier_difficulty' on an unlabeled target degrades to 'tier'
            # so legacy data retains the one-active-per-tier invariant.
            effective_scope: str = active_scope
            if effective_scope == "tier_difficulty" and target.difficulty_label is None:
                effective_scope = "tier"

            active_rows = [
                row
                for row in await self._fetch_active_rows_for_update(
                    family_id,
                    tier=(
                        target.tier
                        if effective_scope in {"tier", "tier_difficulty"}
                        else None
                    ),
                    difficulty_label=(
                        target.difficulty_label
                        if effective_scope == "tier_difficulty"
                        else None
                    ),
                    match_difficulty=effective_scope == "tier_difficulty",
                )
                if row.challenge_id != challenge_id
            ]
            if active_rows:
                if not retire_current:
                    raise ChallengeSourceError("another active challenge exists")
                for active in active_rows:
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

        activated = await self._fetch_one_for_update(family_id, challenge_id)
        if (
            activated is None
            or activated.challenge_id != challenge_id
            or activated.status != CHALLENGE_STATUS_ACTIVE
        ):
            raise ChallengeSourceError("challenge activation failed")
        return activated

    async def mark_locked_and_promote_next(
        self,
        *,
        family_id: str,
        challenge_id: str,
        now_iso: str,
        manage_transaction: bool = True,
        active_scope: ActiveChallengeScope = "family",
    ) -> ChallengeRecord | None:
        try:
            if active_scope not in _ALLOWED_ACTIVE_SCOPES:
                raise ChallengeSourceError(
                    "active_scope must be 'family', 'tier', or 'tier_difficulty'"
                )
            if manage_transaction:
                await self._conn.execute("BEGIN IMMEDIATE")
            target = await self._fetch_one_for_update(family_id, challenge_id)
            target_tier = target.tier if target is not None else None
            target_difficulty = target.difficulty_label if target is not None else None
            # 'tier_difficulty' on an unlabeled locked row degrades to
            # 'tier' so legacy data continues to use the 1-per-tier
            # invariant.
            effective_scope: str = active_scope
            if effective_scope == "tier_difficulty" and target_difficulty is None:
                effective_scope = "tier"
            await self._conn.execute(
                "UPDATE lane_challenges SET status = ?, losers_published_at_iso = NULL, "
                "updated_at_iso = ? "
                "WHERE family_id = ? AND challenge_id = ? AND status = ?",
                (
                    CHALLENGE_STATUS_LOCKED,
                    now_iso,
                    family_id,
                    challenge_id,
                    CHALLENGE_STATUS_ACTIVE,
                ),
            )

            active = await self._fetch_active_for_update(
                family_id,
                tier=(
                    target_tier
                    if effective_scope in {"tier", "tier_difficulty"}
                    else None
                ),
                difficulty_label=(
                    target_difficulty if effective_scope == "tier_difficulty" else None
                ),
                match_difficulty=effective_scope == "tier_difficulty",
            )
            if active is not None:
                if manage_transaction:
                    await self._conn.commit()
                return None

            if effective_scope == "tier_difficulty" and target_tier is not None:
                cur = await self._conn.execute(
                    "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                    "status, audit_metadata, score_multiplier, difficulty_label "
                    "FROM lane_challenges "
                    "WHERE family_id = ? AND status = ? AND tier = ? "
                    "AND difficulty_label IS ? "
                    "ORDER BY created_at_iso ASC, challenge_id ASC LIMIT 1",
                    (
                        family_id,
                        CHALLENGE_STATUS_PENDING,
                        int(target_tier),
                        target_difficulty,
                    ),
                )
            elif effective_scope == "tier" and target_tier is not None:
                cur = await self._conn.execute(
                    "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                    "status, audit_metadata, score_multiplier, difficulty_label "
                    "FROM lane_challenges "
                    "WHERE family_id = ? AND status = ? AND tier = ? "
                    "ORDER BY created_at_iso ASC, challenge_id ASC LIMIT 1",
                    (family_id, CHALLENGE_STATUS_PENDING, int(target_tier)),
                )
            else:
                cur = await self._conn.execute(
                    "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, "
                    "status, audit_metadata, score_multiplier, difficulty_label "
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
                "UPDATE lane_challenges SET status = ?, losers_published_at_iso = NULL, "
                "updated_at_iso = ? "
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
                score_multiplier=pending.score_multiplier,
                difficulty_label=pending.difficulty_label,
            )
        except aiosqlite.IntegrityError as exc:
            if manage_transaction:
                await self._conn.rollback()
            raise ChallengeSourceError("challenge source constraint violation") from exc
        except Exception:
            if manage_transaction:
                await self._conn.rollback()
            raise

    async def promote_pending_batch(
        self,
        family_id: str,
        *,
        tier: int,
        now_iso: str,
        max_count: int,
        kind: str | None = None,
        difficulty_label: str | None = None,
    ) -> list[str]:
        """Promote up to ``max_count`` pending rows in one transaction.

        Filters by ``tier`` (required), and optionally narrows by
        ``kind`` (matched against ``audit_metadata['kind']``) and
        ``difficulty_label``. When ``difficulty_label`` is provided we
        use ``active_scope='tier_difficulty'`` and pick only rows whose
        ``difficulty_label`` matches. When it is ``None`` we use
        ``active_scope='tier'`` and pick ONLY unlabeled rows
        (``difficulty_label IS NULL``) — this is the production
        invariant the partial unique index can't express by itself:
        without that filter we could promote a labeled row under
        tier-scope and leave the unlabeled-active invariant violated.
        (Codex review P0, 2026-05-28.)

        After candidate selection the batch falls through to
        :meth:`activate` per row so the *same* scope-guard runs as it
        would on the single-row path. ``activate`` is internally
        transactional, so this method does NOT wrap the loop in
        ``BEGIN IMMEDIATE`` — the per-row transactions provide the
        commit boundary, and a per-row failure rolls back only the
        offending row instead of the whole batch.
        """
        max_count = max(0, int(max_count))
        if max_count == 0:
            return []
        # Narrow the candidate query to only rows whose difficulty_label
        # matches the requested invariant. ``None`` means "unlabeled
        # only" so the legacy one-active-per-tier rule continues to
        # hold; an explicit label means "exact match" so labeled rows
        # share the tier without colliding.
        if difficulty_label is not None:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, "
                "audit_metadata, score_multiplier, difficulty_label "
                "FROM lane_challenges "
                "WHERE family_id = ? AND status = ? AND tier = ? "
                "AND difficulty_label = ? "
                "ORDER BY created_at_iso ASC, challenge_id ASC LIMIT ?",
                (
                    family_id,
                    CHALLENGE_STATUS_PENDING,
                    int(tier),
                    difficulty_label,
                    # Over-pull modestly so the kind filter can still find
                    # ``max_count`` matches when some rows are non-matching.
                    max_count * 4,
                ),
            )
        else:
            cur = await self._conn.execute(
                "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, "
                "audit_metadata, score_multiplier, difficulty_label "
                "FROM lane_challenges "
                "WHERE family_id = ? AND status = ? AND tier = ? "
                "AND difficulty_label IS NULL "
                "ORDER BY created_at_iso ASC, challenge_id ASC LIMIT ?",
                (
                    family_id,
                    CHALLENGE_STATUS_PENDING,
                    int(tier),
                    max_count * 4,
                ),
            )
        rows = await cur.fetchall()
        candidates: list[ChallengeRecord] = []
        for raw in rows:
            rec = _row_to_record(raw)
            if kind is not None:
                audit_kind = (rec.audit_metadata or {}).get("kind")
                if audit_kind != kind:
                    continue
            candidates.append(rec)
            if len(candidates) >= max_count:
                break

        scope: ActiveChallengeScope = (
            "tier_difficulty" if difficulty_label is not None else "tier"
        )
        promoted: list[str] = []
        for cand in candidates:
            try:
                await self.activate(
                    family_id=family_id,
                    challenge_id=cand.challenge_id,
                    now_iso=now_iso,
                    active_scope=scope,
                )
            except ChallengeSourceError:
                # Most likely "another active challenge exists" — under
                # ``'tier'`` scope only one unlabeled row can occupy a
                # slot at a time. We stop iterating because subsequent
                # candidates would hit the same guard.
                break
            promoted.append(cand.challenge_id)
        return promoted

    async def _fetch_one_for_update(
        self, family_id: str, challenge_id: str
    ) -> ChallengeRecord | None:
        cur = await self._conn.execute(
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, audit_metadata, "
            "score_multiplier, difficulty_label "
            "FROM lane_challenges WHERE family_id = ? AND challenge_id = ? LIMIT 1",
            (family_id, challenge_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_record(row)

    async def _fetch_active_for_update(
        self,
        family_id: str,
        *,
        tier: int | None = None,
        difficulty_label: str | None = None,
        match_difficulty: bool = False,
    ) -> ChallengeRecord | None:
        rows = await self._fetch_active_rows_for_update(
            family_id,
            tier=tier,
            difficulty_label=difficulty_label,
            match_difficulty=match_difficulty,
        )
        return rows[0] if rows else None

    async def _fetch_active_rows_for_update(
        self,
        family_id: str,
        *,
        tier: int | None = None,
        difficulty_label: str | None = None,
        match_difficulty: bool = False,
    ) -> list[ChallengeRecord]:
        base = (
            "SELECT challenge_id, family_id, tier, cnf_text, cnf_path, status, "
            "audit_metadata, score_multiplier, difficulty_label "
            "FROM lane_challenges WHERE family_id = ? AND status = ?"
        )
        params: list[Any] = [family_id, CHALLENGE_STATUS_ACTIVE]
        if tier is not None:
            base += " AND tier = ?"
            params.append(int(tier))
        if match_difficulty:
            # IS ? matches NULL too, so unlabeled rows still pair correctly
            # when the locked row was itself unlabeled.
            base += " AND difficulty_label IS ?"
            params.append(difficulty_label)
        base += " ORDER BY tier ASC, challenge_id ASC"
        cur = await self._conn.execute(base, tuple(params))
        rows = await cur.fetchall()
        return [_row_to_record(row) for row in rows]

@dataclass(frozen=True)
class EndpointLookup:
    """Minimal projection of ``lane_challenges`` for the public CNF endpoint.

    The endpoint only needs the storage pointer/body it serves, the
    fields that decide whether to serve, and the announced CNF digest/size
    used to reject mutable file-backed rows whose bytes changed after
    seeding. Returning a narrow type (rather than a full
    :class:`ChallengeRecord`) keeps the cardinal-sin surface small: no
    raw audit metadata, no family id, no tier flow through this path.
    """

    cnf_text: str
    cnf_path: str | None
    status: str
    updated_at_iso: str
    cnf_sha256: str | None = None
    cnf_bytes: int | None = None
    max_cnf_bytes: int | None = None


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
    """Build a :class:`ChallengeRecord` from a SELECT row.

    Tolerates two SELECT projections so callers don't have to recheck
    column order at every call site:

    * 7-column legacy ``(challenge_id, family_id, tier, cnf_text,
      cnf_path, status, audit_metadata)`` — only the row tests still
      use this shape directly.
    * 9-column post-#236/#241 ``(... audit_metadata, score_multiplier,
      difficulty_label)``.
    * 10-column loser-reconciliation projection that appends
      ``losers_published_at_iso`` after the 9-column block.

    The variant is detected by the trailing slot types: the 9-column
    projection's ``rest`` is ``[float, str | None]``; the 10-column
    projection's ``rest`` is ``[float, str | None, str | None]``.
    """
    challenge_id = row[0]
    family_id = row[1]
    tier = row[2]
    cnf_text = row[3]
    cnf_path = row[4]
    status = row[5]
    audit_json = row[6]
    score_multiplier: float = 1.0
    difficulty_label: str | None = None
    losers_published_at_iso: str | None = None
    rest = list(row[7:])
    if rest:
        head = rest[0]
        if isinstance(head, (int, float)) and not isinstance(head, bool):
            # 9- or 10-column post-#236 projection.
            score_multiplier = float(head)
            if len(rest) > 1:
                difficulty_label = (
                    str(rest[1]) if rest[1] is not None else None
                )
            if len(rest) > 2:
                losers_published_at_iso = (
                    str(rest[2]) if rest[2] is not None else None
                )
        else:
            # Legacy 8-column projection (audit + losers only).
            losers_published_at_iso = str(head) if head is not None else None
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
        losers_published_at_iso=losers_published_at_iso,
        score_multiplier=score_multiplier,
        difficulty_label=difficulty_label,
    )


def _challenge_material_matches(existing: ChallengeRecord, incoming: ChallengeRecord) -> bool:
    """True when an upsert is an idempotent rewrite of private material.

    Status is deliberately ignored: callers may still activate/retire through
    the state machine. CNF bytes/path, family, tier, and audit metadata are the
    immutable material that public announcements, fetch tokens, and receipts
    bind to after a row leaves ``pending``.
    """
    return (
        existing.challenge_id == incoming.challenge_id
        and existing.family_id == incoming.family_id
        and existing.tier == incoming.tier
        and existing.cnf_text == incoming.cnf_text
        and existing.cnf_path == incoming.cnf_path
        and existing.audit_metadata == incoming.audit_metadata
    )


def _positive_audit_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


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
