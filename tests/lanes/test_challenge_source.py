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


def _record(challenge_id: str, status: str = CHALLENGE_STATUS_ACTIVE) -> ChallengeRecord:
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=1,
        cnf_text=_TOY_CNF,
        status=status,
        audit_metadata={"note": "toy"},
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


async def test_sqlite_rejects_second_active_for_family(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso="2026-05-19T00:00:00.000Z")
        await src.upsert(_record("c1", CHALLENGE_STATUS_ACTIVE))
        with pytest.raises(ChallengeSourceError):
            await src.upsert(_record("c2", CHALLENGE_STATUS_ACTIVE))
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


async def test_env_seed_skips_locked_challenge_without_killing_startup(
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

        await _seed_synthetic_boolean_challenge_from_env(src)

        rows = await src.list_for_family(_FAMILY)
        assert [(row.challenge_id, row.status) for row in rows] == [
            (first.challenge_id, CHALLENGE_STATUS_LOCKED)
        ]
    finally:
        await conn.close()
