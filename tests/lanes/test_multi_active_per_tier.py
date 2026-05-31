"""Tests for the tier_multi gated N-active-per-tier feature.

Mirrors the style and fixtures of test_challenge_source.py. Every case
runs against both InMemoryChallengeSource and a real SQLite DB so the
schema migration (UNIQUE→non-unique index) is exercised on the SQLite
path.
"""

from __future__ import annotations

import pytest

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_PENDING,
    CHALLENGE_STATUS_RETIRED,
    ChallengeRecord,
    ChallengeSourceError,
    InMemoryChallengeSource,
    SqliteChallengeSource,
    init_sqlite_challenge_source,
)

_FAMILY = "synthetic_boolean_v1"
_TOY_CNF = "p cnf 1 1\n1 0\n"
_NOW = "2026-05-30T00:00:00.000Z"


def _pending(challenge_id: str, *, tier: int = 1, cnf_text: str = _TOY_CNF) -> ChallengeRecord:
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=tier,
        cnf_text=cnf_text,
        status=CHALLENGE_STATUS_PENDING,
        audit_metadata={"note": "toy"},
    )


# --------------------------------------------------------------------------
# Shared helpers — run the same logic against either source type
# --------------------------------------------------------------------------


async def _seed_pending(source, ids: list[str], tier: int = 1) -> None:
    for i, cid in enumerate(ids):
        rec = _pending(cid, tier=tier, cnf_text=f"p cnf 1 1\n{i + 1} 0\n")
        await source.upsert(rec)


# --------------------------------------------------------------------------
# (a) tier_multi activate keeps MULTIPLE challenges active in the same tier
# --------------------------------------------------------------------------


async def _case_tier_multi_keeps_multiple_active(source) -> None:
    await _seed_pending(source, ["a", "b", "c"], tier=2)

    await source.activate(
        family_id=_FAMILY,
        challenge_id="a",
        now_iso=_NOW,
        active_scope="tier_multi",
    )
    await source.activate(
        family_id=_FAMILY,
        challenge_id="b",
        now_iso=_NOW,
        active_scope="tier_multi",
    )
    await source.activate(
        family_id=_FAMILY,
        challenge_id="c",
        now_iso=_NOW,
        active_scope="tier_multi",
    )

    actives = await source.list_active(_FAMILY)
    active_ids = {rec.challenge_id for rec in actives}
    assert active_ids == {"a", "b", "c"}
    assert all(rec.status == CHALLENGE_STATUS_ACTIVE for rec in actives)
    assert all(rec.tier == 2 for rec in actives)


async def test_in_memory_tier_multi_keeps_multiple_active() -> None:
    src = InMemoryChallengeSource()
    await _case_tier_multi_keeps_multiple_active(src)


async def test_sqlite_tier_multi_keeps_multiple_active(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_tier_multi_keeps_multiple_active(src)
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# (b) default tier scope still retires — exactly one active per tier
# --------------------------------------------------------------------------


async def _case_tier_scope_still_retires(source) -> None:
    await _seed_pending(source, ["x", "y"], tier=3)

    await source.activate(
        family_id=_FAMILY,
        challenge_id="x",
        now_iso=_NOW,
        active_scope="tier",
    )
    actives = await source.list_active(_FAMILY)
    assert [r.challenge_id for r in actives] == ["x"]

    # Activating y with retire_current=True should retire x
    await source.activate(
        family_id=_FAMILY,
        challenge_id="y",
        now_iso=_NOW,
        active_scope="tier",
        retire_current=True,
    )
    actives = await source.list_active(_FAMILY)
    assert [r.challenge_id for r in actives] == ["y"]

    all_rows = await source.list_for_family(_FAMILY)
    statuses = {r.challenge_id: r.status for r in all_rows}
    assert statuses["x"] == CHALLENGE_STATUS_RETIRED
    assert statuses["y"] == CHALLENGE_STATUS_ACTIVE


async def _case_tier_scope_no_retire_raises(source) -> None:
    await _seed_pending(source, ["p", "q"], tier=4)

    await source.activate(
        family_id=_FAMILY,
        challenge_id="p",
        now_iso=_NOW,
        active_scope="tier",
    )
    with pytest.raises(ChallengeSourceError, match="another active"):
        await source.activate(
            family_id=_FAMILY,
            challenge_id="q",
            now_iso=_NOW,
            active_scope="tier",
        )

    actives = await source.list_active(_FAMILY)
    assert [r.challenge_id for r in actives] == ["p"]


async def test_in_memory_tier_scope_retires() -> None:
    src = InMemoryChallengeSource()
    await _case_tier_scope_still_retires(src)


async def test_sqlite_tier_scope_retires(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_tier_scope_still_retires(src)
    finally:
        await conn.close()


async def test_in_memory_tier_scope_no_retire_raises() -> None:
    src = InMemoryChallengeSource()
    await _case_tier_scope_no_retire_raises(src)


async def test_sqlite_tier_scope_no_retire_raises(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_tier_scope_no_retire_raises(src)
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# (c) promote_pending_batch(multi=True, max_count=N) yields N concurrent actives
# --------------------------------------------------------------------------


async def _case_promote_batch_multi(source) -> None:
    n = 5
    ids = [f"m{i}" for i in range(n)]
    await _seed_pending(source, ids, tier=7)

    promoted = await source.promote_pending_batch(
        _FAMILY,
        tier=7,
        now_iso=_NOW,
        max_count=n,
        multi=True,
    )

    assert len(promoted) == n
    assert set(promoted) == set(ids)

    actives = await source.list_active(_FAMILY)
    assert {r.challenge_id for r in actives} == set(ids)
    assert all(r.tier == 7 for r in actives)
    assert all(r.status == CHALLENGE_STATUS_ACTIVE for r in actives)


async def test_in_memory_promote_batch_multi() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_batch_multi(src)


async def test_sqlite_promote_batch_multi(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_batch_multi(src)
    finally:
        await conn.close()


async def _case_promote_batch_multi_respects_max_count(source) -> None:
    """max_count=3 out of 5 pending rows → exactly 3 promoted."""
    ids = [f"r{i}" for i in range(5)]
    await _seed_pending(source, ids, tier=8)

    promoted = await source.promote_pending_batch(
        _FAMILY,
        tier=8,
        now_iso=_NOW,
        max_count=3,
        multi=True,
    )

    assert len(promoted) == 3

    actives = await source.list_active(_FAMILY)
    assert len(actives) == 3
    assert set(promoted) <= set(ids)


async def test_in_memory_promote_batch_multi_max_count() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_batch_multi_respects_max_count(src)


async def test_sqlite_promote_batch_multi_max_count(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_batch_multi_respects_max_count(src)
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# (d) multi=False (default) keeps the existing single-active-per-tier contract
# --------------------------------------------------------------------------


async def _case_promote_batch_no_multi_stops_at_one(source) -> None:
    """Without multi=True, promote_pending_batch yields at most one unlabeled active per tier."""
    ids = [f"s{i}" for i in range(4)]
    await _seed_pending(source, ids, tier=9)

    promoted = await source.promote_pending_batch(
        _FAMILY,
        tier=9,
        now_iso=_NOW,
        max_count=4,
        multi=False,
    )

    assert len(promoted) == 1
    actives = await source.list_active(_FAMILY)
    assert len(actives) == 1


async def test_in_memory_promote_batch_no_multi_stops_at_one() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_batch_no_multi_stops_at_one(src)


async def test_sqlite_promote_batch_no_multi_stops_at_one(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_batch_no_multi_stops_at_one(src)
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Index migration: verify the non-unique index lets multiple rows through
# --------------------------------------------------------------------------


async def test_sqlite_index_migration_allows_multiple_unlabeled_actives(tmp_path) -> None:
    """After migration, multiple unlabeled actives in the same tier must not hit a DB error.

    This is the key invariant that required dropping UNIQUE from
    idx_lane_challenges_one_active_per_family_tier. The app-level 'tier'
    scope still retires, but 'tier_multi' must be able to co-exist with
    the index gone unique.
    """
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        ids = [f"idx-{i}" for i in range(3)]
        await _seed_pending(src, ids, tier=10)

        for cid in ids:
            await src.activate(
                family_id=_FAMILY,
                challenge_id=cid,
                now_iso=_NOW,
                active_scope="tier_multi",
            )

        actives = await src.list_active(_FAMILY)
        assert {r.challenge_id for r in actives} == set(ids)
    finally:
        await conn.close()


async def test_sqlite_schema_idempotent_after_tier_multi_migration(tmp_path) -> None:
    """ensure_sqlite_challenge_source_schema is still idempotent after the index change."""
    from cathedral.lanes.challenge_source import ensure_sqlite_challenge_source_schema

    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        # Run migration twice more — must not raise or corrupt data
        await ensure_sqlite_challenge_source_schema(conn)
        await ensure_sqlite_challenge_source_schema(conn)

        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await src.upsert(_pending("idempotent-check", tier=1))
        rows = await src.list_for_family(_FAMILY)
        assert len(rows) == 1
        assert rows[0].challenge_id == "idempotent-check"
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# (e) promote_to_target — fills tier to N active, idempotent, deficit-aware
# --------------------------------------------------------------------------


def _pending_with_kind(
    challenge_id: str, *, tier: int = 1, kind: str | None = None
) -> ChallengeRecord:
    audit: dict[str, object] = {"note": "toy"}
    if kind is not None:
        audit["kind"] = kind
    return ChallengeRecord(
        challenge_id=challenge_id,
        family_id=_FAMILY,
        tier=tier,
        cnf_text=_TOY_CNF,
        status=CHALLENGE_STATUS_PENDING,
        audit_metadata=audit,
    )


async def _case_promote_to_target_zero_active_promotes_n(source) -> None:
    """With 0 active and target=5, promotes all 5 pending rows."""
    ids = [f"t5-{i}" for i in range(7)]
    for cid in ids:
        await source.upsert(_pending(cid, tier=11))

    promoted = await source.promote_to_target(_FAMILY, tier=11, target=5, now_iso=_NOW)

    assert len(promoted) == 5
    assert set(promoted) <= set(ids)
    actives = await source.list_active(_FAMILY)
    assert len([r for r in actives if r.tier == 11]) == 5


async def test_in_memory_promote_to_target_zero_active() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_to_target_zero_active_promotes_n(src)


async def test_sqlite_promote_to_target_zero_active(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_to_target_zero_active_promotes_n(src)
    finally:
        await conn.close()


async def _case_promote_to_target_idempotent(source) -> None:
    """Calling promote_to_target again when already at target promotes 0."""
    ids = [f"idem-{i}" for i in range(7)]
    for cid in ids:
        await source.upsert(_pending(cid, tier=12))

    first = await source.promote_to_target(_FAMILY, tier=12, target=5, now_iso=_NOW)
    assert len(first) == 5

    second = await source.promote_to_target(_FAMILY, tier=12, target=5, now_iso=_NOW)
    assert second == []

    actives = await source.list_active(_FAMILY)
    assert len([r for r in actives if r.tier == 12]) == 5


async def test_in_memory_promote_to_target_idempotent() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_to_target_idempotent(src)


async def test_sqlite_promote_to_target_idempotent(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_to_target_idempotent(src)
    finally:
        await conn.close()


async def _case_promote_to_target_deficit(source) -> None:
    """With 2 already active and target=5, promotes exactly 3 more."""
    all_ids = [f"def-{i}" for i in range(8)]
    for cid in all_ids:
        await source.upsert(_pending(cid, tier=13))

    # Pre-activate 2
    first_two = all_ids[:2]
    await source.promote_pending_batch(
        _FAMILY, tier=13, now_iso=_NOW, max_count=2, multi=True
    )
    actives_before = [r for r in await source.list_active(_FAMILY) if r.tier == 13]
    assert len(actives_before) == 2

    promoted = await source.promote_to_target(_FAMILY, tier=13, target=5, now_iso=_NOW)
    assert len(promoted) == 3

    actives_after = [r for r in await source.list_active(_FAMILY) if r.tier == 13]
    assert len(actives_after) == 5
    # The 2 that were already active remain active
    pre_ids = {r.challenge_id for r in actives_before}
    post_ids = {r.challenge_id for r in actives_after}
    assert pre_ids <= post_ids


async def test_in_memory_promote_to_target_deficit() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_to_target_deficit(src)


async def test_sqlite_promote_to_target_deficit(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_to_target_deficit(src)
    finally:
        await conn.close()


async def _case_promote_to_target_kind_filter(source) -> None:
    """kind filter: only counts/produces the requested kind."""
    # Seed 3 with kind="hard" and 4 with kind="easy" in same tier
    for i in range(3):
        await source.upsert(_pending_with_kind(f"hard-{i}", tier=14, kind="hard"))
    for i in range(4):
        await source.upsert(_pending_with_kind(f"easy-{i}", tier=14, kind="easy"))

    # Ask for target=2 of kind="hard"
    promoted = await source.promote_to_target(
        _FAMILY, tier=14, target=2, now_iso=_NOW, kind="hard"
    )
    assert len(promoted) == 2
    assert all(p.startswith("hard-") for p in promoted)

    # Total actives in tier: 2 hard only
    actives = [r for r in await source.list_active(_FAMILY) if r.tier == 14]
    assert len(actives) == 2
    assert all((r.audit_metadata or {}).get("kind") == "hard" for r in actives)

    # Calling again with target=2 kind="hard" is idempotent
    second = await source.promote_to_target(
        _FAMILY, tier=14, target=2, now_iso=_NOW, kind="hard"
    )
    assert second == []


async def test_in_memory_promote_to_target_kind_filter() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_to_target_kind_filter(src)


async def test_sqlite_promote_to_target_kind_filter(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_to_target_kind_filter(src)
    finally:
        await conn.close()


async def _case_promote_to_target_default_one(source) -> None:
    """target=1 with 0 active promotes exactly 1 (default-config case)."""
    ids = [f"one-{i}" for i in range(3)]
    for cid in ids:
        await source.upsert(_pending(cid, tier=15))

    promoted = await source.promote_to_target(_FAMILY, tier=15, target=1, now_iso=_NOW)
    assert len(promoted) == 1

    actives = [r for r in await source.list_active(_FAMILY) if r.tier == 15]
    assert len(actives) == 1


async def test_in_memory_promote_to_target_default_one() -> None:
    src = InMemoryChallengeSource()
    await _case_promote_to_target_default_one(src)


async def test_sqlite_promote_to_target_default_one(tmp_path) -> None:
    conn = await init_sqlite_challenge_source(str(tmp_path / "challenges.db"))
    try:
        src = SqliteChallengeSource(conn, now_iso=_NOW)
        await _case_promote_to_target_default_one(src)
    finally:
        await conn.close()
