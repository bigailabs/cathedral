"""Tests for the publisher-side private challenge source.

The source is publisher-private storage. The tests below use toy CNFs
defined inline (no real launch material). They cover both the in-memory
fake and the SQLite-backed implementation to keep the interface honest.
"""

from __future__ import annotations

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
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE))
        with pytest.raises(ChallengeSourceError):
            await src.upsert(_record("c2", CHALLENGE_STATUS_ACTIVE))
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
