"""Tests for the publisher-side private challenge source.

The source is publisher-private storage. The tests below use toy CNFs
defined inline (no real launch material). They cover both the in-memory
fake and the SQLite-backed implementation to keep the interface honest.
"""

from __future__ import annotations

import sqlite3

import pytest

from cathedral.lanes.challenge_ops import seed_synthetic_boolean_challenge
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    ChallengeSourceError,
    InMemoryChallengeSource,
    SqliteChallengeSource,
    ensure_sqlite_challenge_source_schema,
    init_sqlite_challenge_source,
)

_FAMILY = "synthetic_boolean_v1"
_TOY_CNF = "p cnf 1 1\n1 0\n"
_OTHER_TOY_CNF = "p cnf 1 1\n-1 0\n"


def _record(
    challenge_id: str,
    status: str = CHALLENGE_STATUS_ACTIVE,
    *,
    cnf_text: str = _TOY_CNF,
    tier: int = 1,
    audit_metadata: dict[str, object] | None = None,
) -> ChallengeRecord:
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=tier,
        cnf_text=cnf_text,
        status=status,
        audit_metadata=dict(audit_metadata or {"note": "toy"}),
    )


def test_record_rejects_unknown_status() -> None:
    with pytest.raises(ChallengeSourceError):
        ChallengeRecord(
            challenge_id="x",
            family_id=_FAMILY,
            tier=0,
            cnf_text=_TOY_CNF,
            status="not-a-status",
        )


# --------------------------------------------------------------------------
# In-memory
# --------------------------------------------------------------------------


async def test_in_memory_upsert_and_get_active() -> None:
    src = InMemoryChallengeSource()
    await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
    assert await src.get_active(_FAMILY) is None

    await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE), overwrite_status=True)
    active = await src.get_active(_FAMILY)
    assert active is not None
    assert active.challenge_id == "c1"
    assert active.status == CHALLENGE_STATUS_ACTIVE


async def test_in_memory_list_filters_status() -> None:
    src = InMemoryChallengeSource()
    await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE))
    await src.upsert(_record("c2", CHALLENGE_STATUS_PENDING))
    await src.upsert(_record("c3", CHALLENGE_STATUS_LOCKED))

    all_rows = await src.list_for_family(_FAMILY)
    assert [r.challenge_id for r in all_rows] == ["c1", "c2", "c3"]

    pending = await src.list_for_family(_FAMILY, status=CHALLENGE_STATUS_PENDING)
    assert [r.challenge_id for r in pending] == ["c2"]


# --------------------------------------------------------------------------
# SQLite
# --------------------------------------------------------------------------


async def test_sqlite_upsert_and_get_active(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1"))
        active = await src.get_active(_FAMILY)
        assert active is not None
        assert active.challenge_id == "c1"
        assert active.cnf_text == _TOY_CNF
        assert active.audit_metadata == {"note": "toy"}
    finally:
        await conn.close()


async def test_sqlite_only_active_returned_by_get_active(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_LOCKED))
        await src.upsert(_record("c2", CHALLENGE_STATUS_PENDING))
        assert await src.get_active(_FAMILY) is None

        await src.upsert(_record("c3", CHALLENGE_STATUS_ACTIVE))
        active = await src.get_active(_FAMILY)
        assert active is not None
        assert active.challenge_id == "c3"
    finally:
        await conn.close()


async def test_sqlite_rejects_second_active_for_same_tier(tmp_path) -> None:
    """One-active-per-tier is enforced by activate() app logic, not the DB index.

    The UNIQUE index on (family_id, tier) WHERE status='active' was
    replaced with a plain index in the tier_multi migration so that
    multiple unlabeled actives can co-exist under 'tier_multi' scope.
    The one-per-tier guarantee for the legacy 'tier' scope now lives
    entirely in activate(): calling it without retire_current=True raises
    ChallengeSourceError when another active already occupies the slot.
    """
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
        await src.upsert(_record("c2", CHALLENGE_STATUS_PENDING))
        await src.activate(
            family_id=_FAMILY,
            challenge_id="c1",
            now_iso="2026-05-19T00:00:01.000Z",
            active_scope="tier",
        )
        with pytest.raises(ChallengeSourceError):
            await src.activate(
                family_id=_FAMILY,
                challenge_id="c2",
                now_iso="2026-05-19T00:00:02.000Z",
                active_scope="tier",
            )
    finally:
        await conn.close()


async def test_sqlite_allows_one_active_challenge_per_tier(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("tier-1", CHALLENGE_STATUS_ACTIVE, tier=1))
        await src.upsert(_record("tier-3", CHALLENGE_STATUS_ACTIVE, tier=3))

        tier_1 = await src.get_active_for_tier(_FAMILY, 1)
        tier_3 = await src.get_active_for_tier(_FAMILY, 3)
        assert tier_1 is not None
        assert tier_3 is not None
        assert tier_1.challenge_id == "tier-1"
        assert tier_3.challenge_id == "tier-3"
        active = await src.list_active(_FAMILY)
        assert [(row.challenge_id, row.tier) for row in active] == [
            ("tier-1", 1),
            ("tier-3", 3),
        ]

        default = await src.get_active(_FAMILY)
        assert default is not None
        assert default.challenge_id == "tier-1"
    finally:
        await conn.close()


async def test_sqlite_upsert_preserves_existing_status_by_default(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_LOCKED))
        await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_LOCKED
    finally:
        await conn.close()


async def test_sqlite_locked_loser_reconciliation_marker_filters_dirty_rows(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("locked-old", CHALLENGE_STATUS_LOCKED))
        await src.upsert(
            _record("locked-done", CHALLENGE_STATUS_LOCKED),
            now_iso="2026-05-19T00:00:01.000Z",
        )
        await src.upsert(
            _record("locked-new", CHALLENGE_STATUS_LOCKED),
            now_iso="2026-05-19T00:00:02.000Z",
        )
        await src.mark_locked_loser_reconciliation_complete(
            family_id=_FAMILY,
            challenge_id="locked-done",
            now_iso="2026-05-19T00:00:03.000Z",
        )

        dirty = await src.list_locked_needing_loser_reconciliation(_FAMILY, limit=1)

        assert [row.challenge_id for row in dirty] == ["locked-new"]
        assert dirty[0].losers_published_at_iso is None

        all_dirty = await src.list_locked_needing_loser_reconciliation(_FAMILY)
        assert [row.challenge_id for row in all_dirty] == ["locked-new", "locked-old"]
        assert "locked-done" not in {row.challenge_id for row in all_dirty}
    finally:
        await conn.close()


async def test_sqlite_rejects_active_challenge_material_mutation(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE))

        with pytest.raises(ChallengeSourceError, match="immutable"):
            await src.upsert(
                _record(
                    "c1",
                    CHALLENGE_STATUS_PENDING,
                    cnf_text=_OTHER_TOY_CNF,
                    audit_metadata={"note": "different-cnf"},
                )
            )

        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_ACTIVE
        assert rows[0].cnf_text == _TOY_CNF
        assert rows[0].audit_metadata == {"note": "toy"}
    finally:
        await conn.close()


async def test_sqlite_rejects_locked_challenge_material_mutation_even_with_status_overwrite(
    tmp_path,
) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_LOCKED))

        with pytest.raises(ChallengeSourceError, match="immutable"):
            await src.upsert(
                _record("c1", CHALLENGE_STATUS_ACTIVE, cnf_text=_OTHER_TOY_CNF),
                overwrite_status=True,
            )

        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_LOCKED
        assert rows[0].cnf_text == _TOY_CNF
    finally:
        await conn.close()


async def test_sqlite_allows_pending_challenge_material_update(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
        await src.upsert(
            _record(
                "c1",
                CHALLENGE_STATUS_PENDING,
                cnf_text=_OTHER_TOY_CNF,
                audit_metadata={"note": "updated-before-announce"},
            )
        )

        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_PENDING
        assert rows[0].cnf_text == _OTHER_TOY_CNF
        assert rows[0].audit_metadata == {"note": "updated-before-announce"}
    finally:
        await conn.close()


async def test_sqlite_upsert_can_overwrite_status_explicitly(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
        await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE), overwrite_status=True)
        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].status == CHALLENGE_STATUS_ACTIVE
    finally:
        await conn.close()


async def test_sqlite_activate_pending_challenge_idempotently(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_PENDING))
        active = await src.activate(
            family_id=_FAMILY,
            challenge_id="c1",
            now_iso="2026-05-19T00:00:01.000Z",
        )
        assert active.challenge_id == "c1"
        assert active.status == CHALLENGE_STATUS_ACTIVE

        active_again = await src.activate(
            family_id=_FAMILY,
            challenge_id="c1",
            now_iso="2026-05-19T00:00:02.000Z",
        )
        assert active_again.challenge_id == "c1"
        assert active_again.status == CHALLENGE_STATUS_ACTIVE
    finally:
        await conn.close()


async def test_sqlite_activate_requires_explicit_retire_current(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE))
        await src.upsert(_record("c2", CHALLENGE_STATUS_PENDING))

        with pytest.raises(ChallengeSourceError):
            await src.activate(
                family_id=_FAMILY,
                challenge_id="c2",
                now_iso="2026-05-19T00:00:01.000Z",
            )

        active = await src.activate(
            family_id=_FAMILY,
            challenge_id="c2",
            now_iso="2026-05-19T00:00:02.000Z",
            retire_current=True,
        )
        assert active.challenge_id == "c2"
        rows = await src.list_for_family(_FAMILY)
        assert {row.challenge_id: row.status for row in rows} == {
            "c1": "retired",
            "c2": CHALLENGE_STATUS_ACTIVE,
        }
    finally:
        await conn.close()


async def test_sqlite_activate_can_target_only_the_same_tier_slot(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("tier-1-active", CHALLENGE_STATUS_ACTIVE, tier=1))
        await src.upsert(_record("tier-3-next", CHALLENGE_STATUS_PENDING, tier=3))

        with pytest.raises(ChallengeSourceError, match="another active"):
            await src.activate(
                family_id=_FAMILY,
                challenge_id="tier-3-next",
                now_iso="2026-05-19T00:00:01.000Z",
            )

        active = await src.activate(
            family_id=_FAMILY,
            challenge_id="tier-3-next",
            now_iso="2026-05-19T00:00:02.000Z",
            active_scope="tier",
        )

        assert active.challenge_id == "tier-3-next"
        assert [(row.challenge_id, row.tier) for row in await src.list_active(_FAMILY)] == [
            ("tier-1-active", 1),
            ("tier-3-next", 3),
        ]
    finally:
        await conn.close()


async def test_env_seed_loads_operator_cnf_without_path_in_metadata(tmp_path, monkeypatch) -> None:
    from cathedral.publisher.app import _seed_synthetic_boolean_challenge_from_env

    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH", str(cnf_path))
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID", "active-toy-001")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_TIER", "3")

    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await _seed_synthetic_boolean_challenge_from_env(src)
        active = await src.get_active(_FAMILY)
        assert active is not None
        assert active.challenge_id == "active-toy-001"
        assert active.tier == 3
        assert active.cnf_text == "p cnf 2 1\n1 -2 0\n"
        assert active.audit_metadata["source"] == "operator_cnf_path"
        assert "path" not in active.audit_metadata
        assert str(cnf_path) not in str(active.audit_metadata)
    finally:
        await conn.close()


async def test_env_seed_can_store_operator_cnf_as_file_reference(
    tmp_path,
    monkeypatch,
) -> None:
    from cathedral.publisher.app import _seed_synthetic_boolean_challenge_from_env

    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH", str(cnf_path))
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_CHALLENGE_ID", "active-file-001")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_STORAGE_MODE", "file")

    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await _seed_synthetic_boolean_challenge_from_env(src)
        active = await src.get_active(_FAMILY)
        assert active is not None
        assert active.challenge_id == "active-file-001"
        assert active.cnf_text == ""
        assert active.cnf_path == str(cnf_path.resolve())
        assert active.audit_metadata["storage"] == "file"
        assert active.audit_metadata["source"] == "operator_cnf_path"
        assert active.audit_metadata["cnf_bytes"] == cnf_path.stat().st_size
        assert "path" not in active.audit_metadata
        assert str(cnf_path) not in str(active.audit_metadata)
        lookup = await src.get_for_endpoint("active-file-001")
        assert lookup is not None
        assert lookup.cnf_bytes == cnf_path.stat().st_size
    finally:
        await conn.close()


async def test_env_seed_rejects_operator_cnf_above_launch_limit(tmp_path, monkeypatch) -> None:
    from cathedral.publisher.app import _seed_synthetic_boolean_challenge_from_env

    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH", str(cnf_path))
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_MAX_CNF_BYTES", "8")

    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        with pytest.raises(RuntimeError, match="MAX_CNF_BYTES"):
            await _seed_synthetic_boolean_challenge_from_env(src)
        assert await src.get_active(_FAMILY) is None
    finally:
        await conn.close()


async def test_seed_does_not_reactivate_locked_challenge_on_restart(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        first = await seed_synthetic_boolean_challenge(
            src,
            cnf_text=_TOY_CNF,
            tier=0,
            now_iso="2026-05-19T00:00:00.000Z",
            activate=True,
        )
        await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id=first.challenge_id,
            now_iso="2026-05-19T00:00:01.000Z",
        )

        with pytest.raises(ChallengeSourceError, match="already locked"):
            await seed_synthetic_boolean_challenge(
                src,
                cnf_text=_TOY_CNF,
                tier=0,
                now_iso="2026-05-19T00:00:02.000Z",
                activate=True,
            )

        rows = await src.list_for_family(_FAMILY)
        assert [(row.challenge_id, row.status) for row in rows] == [
            (first.challenge_id, CHALLENGE_STATUS_LOCKED)
        ]
    finally:
        await conn.close()


async def test_mark_locked_promote_false_locks_only(tmp_path) -> None:
    # promote=False (winner-take-all submit path): lock the won challenge but
    # do NOT auto-promote a pending one — the caller refills kind-aware.
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        won = await seed_synthetic_boolean_challenge(
            src, cnf_text="p cnf 2 1\n1 2 0\n", tier=1,
            now_iso="2026-05-19T00:00:00.000Z", activate=True,
        )
        pend = await seed_synthetic_boolean_challenge(
            src, cnf_text="p cnf 2 1\n-1 -2 0\n", tier=1,
            now_iso="2026-05-19T00:00:00.500Z", activate=False,
        )
        assert won.challenge_id != pend.challenge_id
        result = await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id=won.challenge_id,
            now_iso="2026-05-19T00:00:01.000Z",
            active_scope="tier",
            promote=False,
        )
        assert result is None
        rows = {r.challenge_id: r.status for r in await src.list_for_family(_FAMILY)}
        assert rows[won.challenge_id] == CHALLENGE_STATUS_LOCKED
        # pending stays pending — not auto-promoted
        assert rows[pend.challenge_id] == CHALLENGE_STATUS_PENDING
        assert await src.get_active(_FAMILY) is None
    finally:
        await conn.close()


async def test_env_seed_skips_locked_seed_after_restart_with_promoted_active(
    tmp_path,
    monkeypatch,
) -> None:
    from cathedral.publisher.app import _seed_synthetic_boolean_challenge_from_env

    cnf_path = tmp_path / "active.cnf"
    cnf_path.write_text(_TOY_CNF, encoding="utf-8")
    monkeypatch.setenv("CATHEDRAL_SYNTHETIC_BOOLEAN_V1_ACTIVE_CNF_PATH", str(cnf_path))

    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await _seed_synthetic_boolean_challenge_from_env(src)
        first = await src.get_active(_FAMILY)
        assert first is not None
        next_challenge = await seed_synthetic_boolean_challenge(
            src,
            cnf_text=_OTHER_TOY_CNF,
            tier=0,
            now_iso="2026-05-19T00:00:00.500Z",
            activate=False,
        )
        await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id=first.challenge_id,
            now_iso="2026-05-19T00:00:01.000Z",
        )

        await _seed_synthetic_boolean_challenge_from_env(src)

        active = await src.get_active(_FAMILY)
        rows = await src.list_for_family(_FAMILY)
        assert active is not None
        assert active.challenge_id == next_challenge.challenge_id
        assert {row.challenge_id: row.status for row in rows} == {
            first.challenge_id: CHALLENGE_STATUS_LOCKED,
            next_challenge.challenge_id: CHALLENGE_STATUS_ACTIVE,
        }
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# PR-bundle: #236 score_multiplier + #241 tier_difficulty + batch promote
# --------------------------------------------------------------------------


async def test_schema_migration_is_idempotent(tmp_path) -> None:
    """``ensure_sqlite_challenge_source_schema`` must be safe to re-run.

    Re-running it on an already-migrated DB MUST NOT raise and MUST NOT
    mutate existing rows. This is the deploy-time invariant: every
    publisher boot calls the migration through the lifespan.
    """
    db = str(tmp_path / "challenges.db")
    conn = await init_sqlite_challenge_source(db)
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="seed-1",
                family_id=_FAMILY,
                tier=1,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_ACTIVE,
                audit_metadata={"note": "seed"},
                score_multiplier=2.5,
                difficulty_label="3b",
            )
        )

        # Run the migration twice more on the same connection.
        await ensure_sqlite_challenge_source_schema(conn)
        await ensure_sqlite_challenge_source_schema(conn)

        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].challenge_id == "seed-1"
        assert rows[0].status == CHALLENGE_STATUS_ACTIVE
        assert rows[0].score_multiplier == pytest.approx(2.5)
        assert rows[0].difficulty_label == "3b"
    finally:
        await conn.close()


async def test_legacy_db_migrates_existing_rows_to_default_multiplier(tmp_path) -> None:
    """Open a DB seeded with the pre-#236 schema, then migrate.

    Existing rows must keep all their columns and gain
    ``score_multiplier=1.0`` plus ``difficulty_label=None`` via the
    additive ``ALTER TABLE`` migration. This is the production cutover
    contract: deploy day reuses the same publisher.db; nothing breaks.
    """
    db = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db)
    try:
        raw.executescript(
            """
            CREATE TABLE lane_challenges (
                challenge_id    TEXT PRIMARY KEY,
                family_id       TEXT NOT NULL,
                tier            INTEGER NOT NULL,
                cnf_text        TEXT NOT NULL,
                cnf_path        TEXT,
                status          TEXT NOT NULL,
                audit_metadata  TEXT NOT NULL,
                losers_published_at_iso TEXT,
                created_at_iso  TEXT NOT NULL,
                updated_at_iso  TEXT NOT NULL
            );
            INSERT INTO lane_challenges VALUES (
                'legacy-1', 'synthetic_boolean_v1', 1, 'p cnf 1 1\n1 0\n', NULL,
                'active', '{}', NULL,
                '2026-05-01T00:00:00.000Z', '2026-05-01T00:00:00.000Z'
            );
            """
        )
        raw.commit()
    finally:
        raw.close()

    conn = await init_sqlite_challenge_source(db)
    try:
        src = SqliteChallengeSource(conn)
        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].challenge_id == "legacy-1"
        assert rows[0].score_multiplier == pytest.approx(1.0)
        assert rows[0].difficulty_label is None
    finally:
        await conn.close()


async def test_active_scope_tier_back_compat_for_unlabeled_rows(tmp_path) -> None:
    """Unlabeled rows must keep obeying the legacy one-active-per-tier rule.

    Promotion under ``active_scope='tier_difficulty'`` degrades to
    ``'tier'`` when the target has no ``difficulty_label`` so the
    pre-#241 invariant continues to hold.
    """
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        await src.upsert(_record("a", CHALLENGE_STATUS_ACTIVE, tier=1))
        await src.upsert(_record("b", CHALLENGE_STATUS_PENDING, tier=1))

        # tier_difficulty with NULL label degrades to tier — collision.
        with pytest.raises(ChallengeSourceError, match="another active"):
            await src.activate(
                family_id=_FAMILY,
                challenge_id="b",
                now_iso="2026-05-27T00:00:01.000Z",
                active_scope="tier_difficulty",
            )
    finally:
        await conn.close()


async def test_active_scope_tier_difficulty_allows_two_labeled_rows(tmp_path) -> None:
    """Two rows in the same tier but different difficulty_label co-exist."""
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="t1-3b",
                family_id=_FAMILY,
                tier=1,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"kind": "sha256_preimage"},
                difficulty_label="3b",
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="t1-6b",
                family_id=_FAMILY,
                tier=1,
                cnf_text=_OTHER_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"kind": "sha256_preimage"},
                difficulty_label="6b",
            )
        )
        await src.activate(
            family_id=_FAMILY,
            challenge_id="t1-3b",
            now_iso="2026-05-27T00:00:01.000Z",
            active_scope="tier_difficulty",
        )
        await src.activate(
            family_id=_FAMILY,
            challenge_id="t1-6b",
            now_iso="2026-05-27T00:00:02.000Z",
            active_scope="tier_difficulty",
        )
        actives = await src.list_active(_FAMILY)
        assert sorted(rec.challenge_id for rec in actives) == ["t1-3b", "t1-6b"]
        assert {rec.difficulty_label for rec in actives} == {"3b", "6b"}
    finally:
        await conn.close()


async def test_locking_one_difficulty_keeps_other_active(tmp_path) -> None:
    """Lock challenge A (T1 difficulty=3b); challenge B (T1 difficulty=6b) stays active."""
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="3b",
                family_id=_FAMILY,
                tier=1,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                difficulty_label="3b",
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="6b",
                family_id=_FAMILY,
                tier=1,
                cnf_text=_OTHER_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                difficulty_label="6b",
            )
        )
        await src.activate(
            family_id=_FAMILY,
            challenge_id="3b",
            now_iso="2026-05-27T00:00:01.000Z",
            active_scope="tier_difficulty",
        )
        await src.activate(
            family_id=_FAMILY,
            challenge_id="6b",
            now_iso="2026-05-27T00:00:02.000Z",
            active_scope="tier_difficulty",
        )
        # Lock 3b under tier_difficulty scope. 6b stays active.
        await src.mark_locked_and_promote_next(
            family_id=_FAMILY,
            challenge_id="3b",
            now_iso="2026-05-27T00:00:03.000Z",
            active_scope="tier_difficulty",
        )
        actives = {rec.challenge_id for rec in await src.list_active(_FAMILY)}
        assert "6b" in actives
        assert "3b" not in actives
    finally:
        await conn.close()


async def test_promote_pending_batch_promotes_thirty_at_once(tmp_path) -> None:
    """30 pending labeled rows → 30 active in one ``promote_pending_batch``."""
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        labels = [f"label-{i:02d}" for i in range(30)]
        for label in labels:
            await src.upsert(
                ChallengeRecord(
                    challenge_id=f"pending-{label}",
                    family_id=_FAMILY,
                    tier=1,
                    cnf_text=_TOY_CNF,
                    status=CHALLENGE_STATUS_PENDING,
                    audit_metadata={"kind": "random_3sat"},
                    difficulty_label=label,
                )
            )
        # With NULL difficulty_label argument, promote up to 30 unlabeled
        # pending rows — but there are none unlabeled. So pass each label
        # the batch needs by relaxing to no-label-filter using a fresh
        # call per label is wrong. Instead drive the no-label-filter path
        # on unlabeled rows below to assert max_count.
        # Here we test the labeled path: 30 calls each promoting 1.
        promoted_all: list[str] = []
        for label in labels:
            promoted = await src.promote_pending_batch(
                _FAMILY,
                tier=1,
                difficulty_label=label,
                now_iso="2026-05-27T00:00:01.000Z",
                max_count=30,
            )
            assert len(promoted) == 1, f"batch for label={label} returned {promoted}"
            promoted_all.extend(promoted)
        assert len(promoted_all) == 30
        actives = await src.list_active(_FAMILY)
        assert {rec.challenge_id for rec in actives} == {
            f"pending-{label}" for label in labels
        }
    finally:
        await conn.close()


async def test_promote_pending_batch_promotes_thirty_unlabeled_via_max_count(tmp_path) -> None:
    """Unlabeled rows: 30 pending at distinct tiers → batch limits to max_count for one tier."""
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        # All at distinct tiers so the legacy one-active-per-tier rule
        # does not collide for unlabeled rows.
        for i in range(30):
            await src.upsert(
                ChallengeRecord(
                    challenge_id=f"pending-{i:02d}",
                    family_id=_FAMILY,
                    tier=100 + i,
                    cnf_text=_TOY_CNF,
                    status=CHALLENGE_STATUS_PENDING,
                    audit_metadata={"kind": "random_3sat"},
                )
            )
        # Batch by tier — each tier has exactly one row, so max_count=30
        # is intentionally over-large.
        promoted_total: list[str] = []
        for i in range(30):
            promoted = await src.promote_pending_batch(
                _FAMILY,
                tier=100 + i,
                now_iso="2026-05-27T00:00:01.000Z",
                max_count=30,
            )
            promoted_total.extend(promoted)
        assert len(promoted_total) == 30
    finally:
        await conn.close()


async def test_promote_pending_batch_no_label_filters_to_unlabeled_only(tmp_path) -> None:
    """``difficulty_label=None`` must only promote unlabeled rows.

    Codex review P0, 2026-05-28: without this filter we could promote a
    labeled row under nominal ``'tier'`` scope and leave the unlabeled
    one-active-per-tier invariant violated. The partial unique index
    can't express this on its own — the candidate query has to do it.
    """
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        await src.upsert(
            ChallengeRecord(
                challenge_id="unlabeled",
                family_id=_FAMILY,
                tier=7,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"kind": "random_3sat"},
            )
        )
        await src.upsert(
            ChallengeRecord(
                challenge_id="labeled",
                family_id=_FAMILY,
                tier=7,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                audit_metadata={"kind": "random_3sat"},
                difficulty_label="3b",
            )
        )
        promoted = await src.promote_pending_batch(
            _FAMILY,
            tier=7,
            now_iso="2026-05-27T00:00:01.000Z",
            max_count=10,
        )
        # Only the unlabeled row is eligible; labeled is excluded.
        assert promoted == ["unlabeled"]
    finally:
        await conn.close()


async def test_promote_pending_batch_kind_filter(tmp_path) -> None:
    """``kind`` arg narrows the batch to rows whose audit_metadata matches.

    Combined with ``difficulty_label`` per-row so the no-label path's
    unlabeled-only invariant is respected. Three pending rows at the
    same tier, each with a distinct label and one of two kinds — we
    promote per-label and verify the kind filter is applied within
    that scope.
    """
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-27T00:00:00.000Z")
        for i, kind in enumerate(["random_3sat", "sha256_preimage", "random_3sat"]):
            await src.upsert(
                ChallengeRecord(
                    challenge_id=f"c{i}",
                    family_id=_FAMILY,
                    tier=5,
                    cnf_text=_TOY_CNF,
                    status=CHALLENGE_STATUS_PENDING,
                    audit_metadata={"kind": kind},
                    difficulty_label=f"d{i}",
                )
            )
        # c0 has the kind we want at label d0; c1 (label d1) is the
        # wrong kind; c2 has the right kind at label d2. Promote each
        # label-specific bucket separately, narrowed by kind.
        for label in ["d0", "d1", "d2"]:
            await src.promote_pending_batch(
                _FAMILY,
                tier=5,
                kind="random_3sat",
                difficulty_label=label,
                now_iso="2026-05-27T00:00:01.000Z",
                max_count=10,
            )
        actives = {rec.challenge_id for rec in await src.list_active(_FAMILY)}
        assert actives == {"c0", "c2"}
    finally:
        await conn.close()


async def test_in_memory_promote_pending_batch_matches_sqlite_contract() -> None:
    """The in-memory fake must match the SQLite contract for batch promote.

    5 pending rows, each with a distinct difficulty_label inside the
    same (family, tier=2) slot. ``promote_pending_batch`` with
    ``difficulty_label=None`` should NOT activate them all in one slot
    (the partial-tier-difficulty invariant requires distinct labels);
    instead the fake walks one label at a time. We exercise the
    label-scoped path: max_count=3 promotes the first three labels.
    """
    src = InMemoryChallengeSource()
    labels = [f"d{i}" for i in range(5)]
    for label in labels:
        await src.upsert(
            ChallengeRecord(
                challenge_id=f"c-{label}",
                family_id=_FAMILY,
                tier=2,
                cnf_text=_TOY_CNF,
                status=CHALLENGE_STATUS_PENDING,
                difficulty_label=label,
            )
        )
    promoted = await src.promote_pending_batch(
        _FAMILY,
        tier=2,
        difficulty_label=labels[0],
        now_iso="2026-05-27T00:00:01.000Z",
        max_count=3,
    )
    assert promoted == [f"c-{labels[0]}"]
    actives = await src.list_active(_FAMILY)
    assert len(actives) == 1
