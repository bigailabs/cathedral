from __future__ import annotations

from dataclasses import fields

import pytest

from cathedral.lanes.challenge_receipts import (
    RECEIPT_STATUS_EXPIRED,
    RECEIPT_STATUS_INVALID,
    RECEIPT_STATUS_UNVERIFIED,
    RECEIPT_STATUS_VALID,
    RECEIPT_STATUS_VERIFYING,
    ChallengeReceipt,
    ChallengeReceiptError,
    SqliteChallengeReceiptStore,
)
from cathedral.lanes.challenge_receipts import (
    SQLITE_SCHEMA as RECEIPT_SCHEMA,
)
from cathedral.validator.db import connect


async def _store(tmp_path):
    conn = await connect(str(tmp_path / "receipts.db"))
    await conn.executescript(RECEIPT_SCHEMA)
    await conn.commit()
    return conn, SqliteChallengeReceiptStore(conn)


async def _receipt(
    store: SqliteChallengeReceiptStore,
    *,
    submission_id: str,
    received_at_iso: str,
):
    return await store.record_receipt(
        family_id="synthetic_boolean_v1",
        challenge_id="sat-001",
        submission_id=submission_id,
        miner_hotkey=f"5Miner{submission_id[-1].upper()}",
        received_at_iso=received_at_iso,
        answer_hash=f"answer-{submission_id}",
        recorded_at_iso="2026-05-20T00:00:00.000Z",
    )


async def _mark_valid(store: SqliteChallengeReceiptStore, submission_id: str):
    return await store.update_status(
        family_id="synthetic_boolean_v1",
        challenge_id="sat-001",
        submission_id=submission_id,
        status=RECEIPT_STATUS_VALID,
        now_iso="2026-05-20T00:00:01.000Z",
        verifier_details_hash=f"details-{submission_id}",
    )


@pytest.mark.asyncio
async def test_later_valid_waits_for_earlier_unverified_receipt(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:02.000Z",
        )
        await _mark_valid(store, "sub-b")

        earlier = await store.get(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
        )
        assert earlier is not None
        assert earlier.status == RECEIPT_STATUS_UNVERIFIED
        assert await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        ) is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_earlier_valid_beats_later_valid_even_if_later_resolves_first(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:02.000Z",
        )
        await _mark_valid(store, "sub-b")
        assert await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        ) is None

        await _mark_valid(store, "sub-a")
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-a"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_later_valid_wins_after_all_earlier_receipts_resolve_invalid(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:02.000Z",
        )
        await _mark_valid(store, "sub-b")
        invalid = await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_INVALID,
            now_iso="2026-05-20T00:00:03.000Z",
            rejection_reason="solution_unsatisfied",
        )

        assert invalid.status == RECEIPT_STATUS_INVALID
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_expired_earlier_receipt_does_not_block_later_valid(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:02.000Z",
        )
        await _mark_valid(store, "sub-b")
        expired = await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_EXPIRED,
            now_iso="2026-05-20T00:01:30.000Z",
            rejection_reason="receipt_expired",
        )

        assert expired.status == RECEIPT_STATUS_EXPIRED
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_expire_unresolved_before_unblocks_later_valid(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_VERIFYING,
            now_iso="2026-05-20T00:00:01.500Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:05.000Z",
        )
        await _mark_valid(store, "sub-b")

        expired = await store.expire_unresolved_before(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            cutoff_received_at_iso="2026-05-20T00:00:04.000Z",
            now_iso="2026-05-20T00:01:30.000Z",
            rejection_reason="receipt_timed_out",
        )

        assert expired == 1
        earlier = await store.get(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
        )
        assert earlier is not None
        assert earlier.status == RECEIPT_STATUS_EXPIRED
        assert earlier.rejection_reason == "receipt_timed_out"
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_verifying_receipt_blocks_later_valid_until_resolved(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:02.000Z",
        )
        verifying = await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_VERIFYING,
            now_iso="2026-05-20T00:00:01.500Z",
        )
        assert verifying.status == RECEIPT_STATUS_VERIFYING
        await _mark_valid(store, "sub-b")
        assert await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        ) is None

        await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_INVALID,
            now_iso="2026-05-20T00:00:03.000Z",
            rejection_reason="bad_answer",
        )
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_received_at_ties_break_by_sqlite_insert_order(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _receipt(
            store,
            submission_id="sub-b",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _mark_valid(store, "sub-b")
        assert await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        ) is None

        await store.update_status(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
            submission_id="sub-a",
            status=RECEIPT_STATUS_INVALID,
            now_iso="2026-05-20T00:00:03.000Z",
            rejection_reason="bad_answer",
        )
        winner = await store.select_winner(
            family_id="synthetic_boolean_v1",
            challenge_id="sat-001",
        )
        assert winner is not None
        assert winner.submission_id == "sub-b"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_terminal_receipt_cannot_be_reopened(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        await _mark_valid(store, "sub-a")

        with pytest.raises(ChallengeReceiptError, match="cannot transition"):
            await store.update_status(
                family_id="synthetic_boolean_v1",
                challenge_id="sat-001",
                submission_id="sub-a",
                status=RECEIPT_STATUS_INVALID,
                now_iso="2026-05-20T00:00:04.000Z",
                rejection_reason="late_reject",
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_duplicate_receipt_is_idempotent_unless_immutable_fields_change(tmp_path) -> None:
    conn, store = await _store(tmp_path)
    try:
        first = await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        second = await _receipt(
            store,
            submission_id="sub-a",
            received_at_iso="2026-05-20T00:00:01.000Z",
        )
        assert second == first

        with pytest.raises(ChallengeReceiptError, match="different immutable fields"):
            await store.record_receipt(
                family_id="synthetic_boolean_v1",
                challenge_id="sat-001",
                submission_id="sub-a",
                miner_hotkey="5MinerA",
                received_at_iso="2026-05-20T00:00:01.000Z",
                answer_hash="changed-answer-hash",
                recorded_at_iso="2026-05-20T00:00:00.000Z",
            )
    finally:
        await conn.close()


def test_receipt_model_has_no_raw_payload_fields() -> None:
    field_names = {field.name for field in fields(ChallengeReceipt)}
    assert "answer_hash" in field_names
    assert field_names.isdisjoint(
        {
            "answer",
            "answer_payload",
            "cnf",
            "cnf_text",
            "raw_answer",
            "raw_stdout",
            "stdout",
        }
    )
