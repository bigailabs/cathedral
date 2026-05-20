"""Publisher-private SAT challenge receipt store.

This module records the publisher's first-submitted ordering key for
Task Family challenge answers. It is intentionally separate from the
final winner lock: a receipt is created before verification finishes,
then its status advances as the verifier resolves it.

Receipt statuses:

* ``unverified``: receipt exists, verifier has not started.
* ``verifying``: verifier owns the receipt but has not resolved it.
* ``valid``: verifier accepted the answer.
* ``invalid``: verifier rejected the answer.
* ``expired``: receipt can no longer win.

Winner selection is conservative. For one ``(family_id, challenge_id)``,
the earliest ``valid`` receipt can win only when every earlier receipt is
resolved ``invalid`` or ``expired``. Any earlier ``unverified`` or
``verifying`` receipt blocks later valid receipts.

Leak boundary: this store is publisher-private and hash-only. It stores
``answer_hash`` plus optional receipt-auth metadata, not raw stdout,
raw answers, CNF bodies, or public feed projections.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiosqlite

RECEIPT_STATUS_UNVERIFIED = "unverified"
RECEIPT_STATUS_VERIFYING = "verifying"
RECEIPT_STATUS_VALID = "valid"
RECEIPT_STATUS_INVALID = "invalid"
RECEIPT_STATUS_EXPIRED = "expired"

RECEIPT_STATUSES = frozenset(
    {
        RECEIPT_STATUS_UNVERIFIED,
        RECEIPT_STATUS_VERIFYING,
        RECEIPT_STATUS_VALID,
        RECEIPT_STATUS_INVALID,
        RECEIPT_STATUS_EXPIRED,
    }
)
_NON_WINNING_RESOLVED_STATUSES = frozenset(
    {
        RECEIPT_STATUS_INVALID,
        RECEIPT_STATUS_EXPIRED,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        RECEIPT_STATUS_VALID,
        RECEIPT_STATUS_INVALID,
        RECEIPT_STATUS_EXPIRED,
    }
)


class ChallengeReceiptError(Exception):
    """Raised on receipt schema or state-machine violations."""


@dataclass(frozen=True)
class ChallengeReceipt:
    family_id: str
    challenge_id: str
    submission_id: str
    miner_hotkey: str
    received_at_iso: str
    answer_hash: str
    status: str
    created_at_iso: str
    updated_at_iso: str
    nonce: str | None = None
    miner_signature: str | None = None
    rejection_reason: str | None = None
    verifier_details_hash: str | None = None
    resolved_at_iso: str | None = None
    eval_run_id: str | None = None
    signed_row: dict[str, Any] | None = None
    trace_json: dict[str, Any] | None = None
    duration_ms: int | None = None
    epoch: int | None = None
    round_index: int | None = None

    def __post_init__(self) -> None:
        if self.status not in RECEIPT_STATUSES:
            raise ChallengeReceiptError(f"unknown receipt status: {self.status!r}")
        required = {
            "family_id": self.family_id,
            "challenge_id": self.challenge_id,
            "submission_id": self.submission_id,
            "miner_hotkey": self.miner_hotkey,
            "received_at_iso": self.received_at_iso,
            "answer_hash": self.answer_hash,
            "created_at_iso": self.created_at_iso,
            "updated_at_iso": self.updated_at_iso,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ChallengeReceiptError(f"missing receipt field(s): {', '.join(missing)}")


class ChallengeReceiptStore(Protocol):
    async def record_receipt(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        miner_hotkey: str,
        received_at_iso: str,
        answer_hash: str,
        recorded_at_iso: str,
        nonce: str | None = None,
        miner_signature: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        """Record a receipt in ``unverified`` state and return the row."""
        ...

    async def attach_result(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        eval_run_id: str,
        signed_row: dict[str, Any],
        trace_json: dict[str, Any] | None,
        duration_ms: int,
        epoch: int,
        round_index: int,
        now_iso: str,
        commit: bool = True,
    ) -> ChallengeReceipt:
        """Attach the private result payload needed for delayed finalization."""
        ...

    async def update_status(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        status: str,
        now_iso: str,
        rejection_reason: str | None = None,
        verifier_details_hash: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        """Advance one receipt through the state machine."""
        ...

    async def expire_unresolved_before(
        self,
        *,
        family_id: str,
        challenge_id: str,
        cutoff_received_at_iso: str,
        now_iso: str,
        rejection_reason: str,
        commit: bool = True,
    ) -> int:
        """Expire unresolved receipts older than the receipt-time cutoff."""
        ...

    async def get(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
    ) -> ChallengeReceipt | None:
        """Return one receipt by key."""
        ...

    async def list_for_challenge(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> list[ChallengeReceipt]:
        """Return receipts in first-submitted order."""
        ...

    async def select_winner(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> ChallengeReceipt | None:
        """Return the first-submitted eligible valid receipt, if any."""
        ...


def select_first_submitted_winner_from_ordered(
    receipts: Sequence[ChallengeReceipt],
) -> ChallengeReceipt | None:
    """Select a winner from receipts already sorted by receipt order."""
    for receipt in receipts:
        if receipt.status in _NON_WINNING_RESOLVED_STATUSES:
            continue
        if receipt.status == RECEIPT_STATUS_VALID:
            return receipt
        return None
    return None


class InMemoryChallengeReceiptStore:
    """Process-local fake for tests and publisher dry-runs."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], ChallengeReceipt] = {}
        self._order: dict[tuple[str, str, str], int] = {}
        self._next_order = 0

    async def record_receipt(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        miner_hotkey: str,
        received_at_iso: str,
        answer_hash: str,
        recorded_at_iso: str,
        nonce: str | None = None,
        miner_signature: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        key = (family_id, challenge_id, submission_id)
        existing = self._rows.get(key)
        if existing is not None:
            _assert_same_receipt(
                existing,
                miner_hotkey=miner_hotkey,
                received_at_iso=received_at_iso,
                answer_hash=answer_hash,
                nonce=nonce,
                miner_signature=miner_signature,
            )
            return existing

        receipt = ChallengeReceipt(
            family_id=family_id,
            challenge_id=challenge_id,
            submission_id=submission_id,
            miner_hotkey=miner_hotkey,
            received_at_iso=received_at_iso,
            answer_hash=answer_hash,
            status=RECEIPT_STATUS_UNVERIFIED,
            created_at_iso=recorded_at_iso,
            updated_at_iso=recorded_at_iso,
            nonce=nonce,
            miner_signature=miner_signature,
        )
        self._rows[key] = receipt
        self._order[key] = self._next_order
        self._next_order += 1
        return receipt

    async def update_status(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        status: str,
        now_iso: str,
        rejection_reason: str | None = None,
        verifier_details_hash: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        key = (family_id, challenge_id, submission_id)
        current = self._rows.get(key)
        if current is None:
            raise ChallengeReceiptError("receipt not found")
        updated = _advance_receipt(
            current,
            status=status,
            now_iso=now_iso,
            rejection_reason=rejection_reason,
            verifier_details_hash=verifier_details_hash,
        )
        self._rows[key] = updated
        return updated

    async def expire_unresolved_before(
        self,
        *,
        family_id: str,
        challenge_id: str,
        cutoff_received_at_iso: str,
        now_iso: str,
        rejection_reason: str,
        commit: bool = True,
    ) -> int:
        expired = 0
        for key, current in list(self._rows.items()):
            if key[0] != family_id or key[1] != challenge_id:
                continue
            if current.status in _TERMINAL_STATUSES:
                continue
            if current.received_at_iso >= cutoff_received_at_iso:
                continue
            self._rows[key] = _advance_receipt(
                current,
                status=RECEIPT_STATUS_EXPIRED,
                now_iso=now_iso,
                rejection_reason=rejection_reason,
                verifier_details_hash=current.verifier_details_hash,
            )
            expired += 1
        return expired

    async def attach_result(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        eval_run_id: str,
        signed_row: dict[str, Any],
        trace_json: dict[str, Any] | None,
        duration_ms: int,
        epoch: int,
        round_index: int,
        now_iso: str,
        commit: bool = True,
    ) -> ChallengeReceipt:
        key = (family_id, challenge_id, submission_id)
        current = self._rows.get(key)
        if current is None:
            raise ChallengeReceiptError("receipt not found")
        updated = _with_result_payload(
            current,
            eval_run_id=eval_run_id,
            signed_row=signed_row,
            trace_json=trace_json,
            duration_ms=duration_ms,
            epoch=epoch,
            round_index=round_index,
            now_iso=now_iso,
        )
        self._rows[key] = updated
        return updated

    async def get(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
    ) -> ChallengeReceipt | None:
        return self._rows.get((family_id, challenge_id, submission_id))

    async def list_for_challenge(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> list[ChallengeReceipt]:
        rows = [
            receipt
            for key, receipt in self._rows.items()
            if key[0] == family_id and key[1] == challenge_id
        ]
        return sorted(
            rows,
            key=lambda receipt: (receipt.received_at_iso, self._order[_key(receipt)]),
        )

    async def select_winner(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> ChallengeReceipt | None:
        return select_first_submitted_winner_from_ordered(
            await self.list_for_challenge(family_id=family_id, challenge_id=challenge_id)
        )


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS lane_challenge_receipts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id             TEXT NOT NULL,
    challenge_id          TEXT NOT NULL,
    submission_id         TEXT NOT NULL,
    miner_hotkey          TEXT NOT NULL,
    received_at_iso       TEXT NOT NULL,
    answer_hash           TEXT NOT NULL,
    nonce                 TEXT,
    miner_signature       TEXT,
    status                TEXT NOT NULL CHECK (
        status IN ('unverified', 'verifying', 'valid', 'invalid', 'expired')
    ),
    eval_run_id           TEXT,
    signed_row_json       TEXT,
    trace_json            TEXT,
    duration_ms           INTEGER,
    epoch                 INTEGER,
    round_index           INTEGER,
    rejection_reason      TEXT,
    verifier_details_hash TEXT,
    created_at_iso        TEXT NOT NULL,
    updated_at_iso        TEXT NOT NULL,
    resolved_at_iso       TEXT,
    UNIQUE (family_id, challenge_id, submission_id)
);
CREATE INDEX IF NOT EXISTS idx_lane_challenge_receipts_order
    ON lane_challenge_receipts(family_id, challenge_id, received_at_iso, id);
CREATE INDEX IF NOT EXISTS idx_lane_challenge_receipts_status
    ON lane_challenge_receipts(family_id, challenge_id, status);
"""


async def init_sqlite_challenge_receipt_store(database_path: str) -> aiosqlite.Connection:
    """Open the publisher-private receipt SQLite and apply schema."""
    conn = await aiosqlite.connect(database_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.executescript(SQLITE_SCHEMA)
    await conn.commit()
    return conn


class SqliteChallengeReceiptStore:
    """SQLite-backed receipt store.

    The publisher is expected to be the sole writer. The SQLite ``id`` is
    used only as a durable same-timestamp tie breaker; it is not exposed
    in the receipt model.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def record_receipt(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        miner_hotkey: str,
        received_at_iso: str,
        answer_hash: str,
        recorded_at_iso: str,
        nonce: str | None = None,
        miner_signature: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        await self._conn.execute(
            "INSERT OR IGNORE INTO lane_challenge_receipts ("
            "family_id, challenge_id, submission_id, miner_hotkey, "
            "received_at_iso, answer_hash, nonce, miner_signature, status, "
            "created_at_iso, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                family_id,
                challenge_id,
                submission_id,
                miner_hotkey,
                received_at_iso,
                answer_hash,
                nonce,
                miner_signature,
                RECEIPT_STATUS_UNVERIFIED,
                recorded_at_iso,
                recorded_at_iso,
            ),
        )
        if commit:
            await self._conn.commit()
        existing = await self.get(
            family_id=family_id,
            challenge_id=challenge_id,
            submission_id=submission_id,
        )
        if existing is None:
            raise ChallengeReceiptError("receipt row missing immediately after insert")
        _assert_same_receipt(
            existing,
            miner_hotkey=miner_hotkey,
            received_at_iso=received_at_iso,
            answer_hash=answer_hash,
            nonce=nonce,
            miner_signature=miner_signature,
        )
        return existing

    async def update_status(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        status: str,
        now_iso: str,
        rejection_reason: str | None = None,
        verifier_details_hash: str | None = None,
        commit: bool = True,
    ) -> ChallengeReceipt:
        current = await self.get(
            family_id=family_id,
            challenge_id=challenge_id,
            submission_id=submission_id,
        )
        if current is None:
            raise ChallengeReceiptError("receipt not found")
        updated = _advance_receipt(
            current,
            status=status,
            now_iso=now_iso,
            rejection_reason=rejection_reason,
            verifier_details_hash=verifier_details_hash,
        )
        await self._conn.execute(
            "UPDATE lane_challenge_receipts SET status = ?, rejection_reason = ?, "
            "verifier_details_hash = ?, updated_at_iso = ?, resolved_at_iso = ? "
            "WHERE family_id = ? AND challenge_id = ? AND submission_id = ?",
            (
                updated.status,
                updated.rejection_reason,
                updated.verifier_details_hash,
                updated.updated_at_iso,
                updated.resolved_at_iso,
                family_id,
                challenge_id,
                submission_id,
            ),
        )
        if commit:
            await self._conn.commit()
        return updated

    async def attach_result(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
        eval_run_id: str,
        signed_row: dict[str, Any],
        trace_json: dict[str, Any] | None,
        duration_ms: int,
        epoch: int,
        round_index: int,
        now_iso: str,
        commit: bool = True,
    ) -> ChallengeReceipt:
        current = await self.get(
            family_id=family_id,
            challenge_id=challenge_id,
            submission_id=submission_id,
        )
        if current is None:
            raise ChallengeReceiptError("receipt not found")
        updated = _with_result_payload(
            current,
            eval_run_id=eval_run_id,
            signed_row=signed_row,
            trace_json=trace_json,
            duration_ms=duration_ms,
            epoch=epoch,
            round_index=round_index,
            now_iso=now_iso,
        )
        await self._conn.execute(
            "UPDATE lane_challenge_receipts SET eval_run_id = ?, signed_row_json = ?, "
            "trace_json = ?, duration_ms = ?, epoch = ?, round_index = ?, updated_at_iso = ? "
            "WHERE family_id = ? AND challenge_id = ? AND submission_id = ?",
            (
                updated.eval_run_id,
                json.dumps(updated.signed_row, sort_keys=True),
                json.dumps(updated.trace_json, sort_keys=True)
                if updated.trace_json is not None
                else None,
                updated.duration_ms,
                updated.epoch,
                updated.round_index,
                updated.updated_at_iso,
                family_id,
                challenge_id,
                submission_id,
            ),
        )
        if commit:
            await self._conn.commit()
        return updated

    async def expire_unresolved_before(
        self,
        *,
        family_id: str,
        challenge_id: str,
        cutoff_received_at_iso: str,
        now_iso: str,
        rejection_reason: str,
        commit: bool = True,
    ) -> int:
        cur = await self._conn.execute(
            "UPDATE lane_challenge_receipts SET status = ?, rejection_reason = ?, "
            "updated_at_iso = ?, resolved_at_iso = ? "
            "WHERE family_id = ? AND challenge_id = ? "
            "AND status IN (?, ?) AND received_at_iso < ?",
            (
                RECEIPT_STATUS_EXPIRED,
                rejection_reason,
                now_iso,
                now_iso,
                family_id,
                challenge_id,
                RECEIPT_STATUS_UNVERIFIED,
                RECEIPT_STATUS_VERIFYING,
                cutoff_received_at_iso,
            ),
        )
        if commit:
            await self._conn.commit()
        return int(cur.rowcount if cur.rowcount is not None else 0)

    async def get(
        self,
        *,
        family_id: str,
        challenge_id: str,
        submission_id: str,
    ) -> ChallengeReceipt | None:
        cur = await self._conn.execute(
            "SELECT family_id, challenge_id, submission_id, miner_hotkey, "
            "received_at_iso, answer_hash, status, created_at_iso, updated_at_iso, "
            "nonce, miner_signature, rejection_reason, verifier_details_hash, "
            "resolved_at_iso, eval_run_id, signed_row_json, trace_json, duration_ms, "
            "epoch, round_index FROM lane_challenge_receipts "
            "WHERE family_id = ? AND challenge_id = ? AND submission_id = ? LIMIT 1",
            (family_id, challenge_id, submission_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return _row_to_receipt(row)

    async def list_for_challenge(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> list[ChallengeReceipt]:
        cur = await self._conn.execute(
            "SELECT family_id, challenge_id, submission_id, miner_hotkey, "
            "received_at_iso, answer_hash, status, created_at_iso, updated_at_iso, "
            "nonce, miner_signature, rejection_reason, verifier_details_hash, "
            "resolved_at_iso, eval_run_id, signed_row_json, trace_json, duration_ms, "
            "epoch, round_index FROM lane_challenge_receipts "
            "WHERE family_id = ? AND challenge_id = ? "
            "ORDER BY received_at_iso ASC, id ASC",
            (family_id, challenge_id),
        )
        rows = await cur.fetchall()
        return [_row_to_receipt(row) for row in rows]

    async def select_winner(
        self,
        *,
        family_id: str,
        challenge_id: str,
    ) -> ChallengeReceipt | None:
        return select_first_submitted_winner_from_ordered(
            await self.list_for_challenge(family_id=family_id, challenge_id=challenge_id)
        )


def _key(receipt: ChallengeReceipt) -> tuple[str, str, str]:
    return (receipt.family_id, receipt.challenge_id, receipt.submission_id)


def _assert_same_receipt(
    receipt: ChallengeReceipt,
    *,
    miner_hotkey: str,
    received_at_iso: str,
    answer_hash: str,
    nonce: str | None,
    miner_signature: str | None,
) -> None:
    if (
        receipt.miner_hotkey != miner_hotkey
        or receipt.received_at_iso != received_at_iso
        or receipt.answer_hash != answer_hash
        or receipt.nonce != nonce
        or receipt.miner_signature != miner_signature
    ):
        raise ChallengeReceiptError("receipt already exists with different immutable fields")


def _advance_receipt(
    receipt: ChallengeReceipt,
    *,
    status: str,
    now_iso: str,
    rejection_reason: str | None,
    verifier_details_hash: str | None,
) -> ChallengeReceipt:
    if status not in RECEIPT_STATUSES:
        raise ChallengeReceiptError(f"unknown receipt status: {status!r}")
    if receipt.status == status:
        return receipt
    if receipt.status in _TERMINAL_STATUSES:
        raise ChallengeReceiptError(
            f"cannot transition receipt from {receipt.status!r} to {status!r}"
        )
    if receipt.status == RECEIPT_STATUS_VERIFYING and status == RECEIPT_STATUS_UNVERIFIED:
        raise ChallengeReceiptError("cannot transition verifying receipt back to unverified")

    resolved_at_iso = now_iso if status in _TERMINAL_STATUSES else None
    next_rejection = rejection_reason if status in _NON_WINNING_RESOLVED_STATUSES else None
    return ChallengeReceipt(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
        miner_hotkey=receipt.miner_hotkey,
        received_at_iso=receipt.received_at_iso,
        answer_hash=receipt.answer_hash,
        status=status,
        created_at_iso=receipt.created_at_iso,
        updated_at_iso=now_iso,
        nonce=receipt.nonce,
        miner_signature=receipt.miner_signature,
        rejection_reason=next_rejection,
        verifier_details_hash=verifier_details_hash,
        resolved_at_iso=resolved_at_iso,
        eval_run_id=receipt.eval_run_id,
        signed_row=receipt.signed_row,
        trace_json=receipt.trace_json,
        duration_ms=receipt.duration_ms,
        epoch=receipt.epoch,
        round_index=receipt.round_index,
    )


def _with_result_payload(
    receipt: ChallengeReceipt,
    *,
    eval_run_id: str,
    signed_row: dict[str, Any],
    trace_json: dict[str, Any] | None,
    duration_ms: int,
    epoch: int,
    round_index: int,
    now_iso: str,
) -> ChallengeReceipt:
    return ChallengeReceipt(
        family_id=receipt.family_id,
        challenge_id=receipt.challenge_id,
        submission_id=receipt.submission_id,
        miner_hotkey=receipt.miner_hotkey,
        received_at_iso=receipt.received_at_iso,
        answer_hash=receipt.answer_hash,
        status=receipt.status,
        created_at_iso=receipt.created_at_iso,
        updated_at_iso=now_iso,
        nonce=receipt.nonce,
        miner_signature=receipt.miner_signature,
        rejection_reason=receipt.rejection_reason,
        verifier_details_hash=receipt.verifier_details_hash,
        resolved_at_iso=receipt.resolved_at_iso,
        eval_run_id=eval_run_id,
        signed_row=dict(signed_row),
        trace_json=dict(trace_json) if trace_json is not None else None,
        duration_ms=int(duration_ms),
        epoch=int(epoch),
        round_index=int(round_index),
    )


def _row_to_receipt(row: Sequence[object]) -> ChallengeReceipt:
    signed_row_raw = row[15] if len(row) > 15 else None
    trace_raw = row[16] if len(row) > 16 else None
    return ChallengeReceipt(
        family_id=str(row[0]),
        challenge_id=str(row[1]),
        submission_id=str(row[2]),
        miner_hotkey=str(row[3]),
        received_at_iso=str(row[4]),
        answer_hash=str(row[5]),
        status=str(row[6]),
        created_at_iso=str(row[7]),
        updated_at_iso=str(row[8]),
        nonce=str(row[9]) if row[9] is not None else None,
        miner_signature=str(row[10]) if row[10] is not None else None,
        rejection_reason=str(row[11]) if row[11] is not None else None,
        verifier_details_hash=str(row[12]) if row[12] is not None else None,
        resolved_at_iso=str(row[13]) if row[13] is not None else None,
        eval_run_id=str(row[14]) if len(row) > 14 and row[14] is not None else None,
        signed_row=json.loads(str(signed_row_raw)) if signed_row_raw else None,
        trace_json=json.loads(str(trace_raw)) if trace_raw else None,
        duration_ms=int(row[17]) if len(row) > 17 and row[17] is not None else None,
        epoch=int(row[18]) if len(row) > 18 and row[18] is not None else None,
        round_index=int(row[19]) if len(row) > 19 and row[19] is not None else None,
    )


__all__ = [
    "RECEIPT_STATUSES",
    "RECEIPT_STATUS_EXPIRED",
    "RECEIPT_STATUS_INVALID",
    "RECEIPT_STATUS_UNVERIFIED",
    "RECEIPT_STATUS_VALID",
    "RECEIPT_STATUS_VERIFYING",
    "SQLITE_SCHEMA",
    "ChallengeReceipt",
    "ChallengeReceiptError",
    "ChallengeReceiptStore",
    "InMemoryChallengeReceiptStore",
    "SqliteChallengeReceiptStore",
    "init_sqlite_challenge_receipt_store",
    "select_first_submitted_winner_from_ordered",
]
