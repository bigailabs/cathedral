"""Operator helpers for private Task Family challenge rows.

These helpers are publisher-side only. They accept runtime CNF input,
derive stable metadata, and write rows to the private SQLite challenge
store without recording local file paths, corpus labels, planted
solutions, or timing data.
"""

from __future__ import annotations

import hashlib

from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    CHALLENGE_STATUS_LOCKED,
    CHALLENGE_STATUS_PENDING,
    ChallengeRecord,
    ChallengeSourceError,
    SqliteChallengeSource,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID
from cathedral.lanes.synthetic_boolean_v1.dimacs import parse_dimacs_cnf_metadata


def challenge_id_for_cnf_sha256(cnf_sha256: str) -> str:
    return f"sat-{cnf_sha256[:16]}"


def challenge_id_for_cnf(cnf_text: str) -> str:
    digest = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    return challenge_id_for_cnf_sha256(digest)


def build_synthetic_boolean_challenge_record(
    *,
    cnf_text: str,
    tier: int,
    challenge_id: str | None = None,
    status: str = CHALLENGE_STATUS_PENDING,
    source: str = "operator_cnf",
) -> ChallengeRecord:
    parsed = parse_dimacs_cnf_metadata(cnf_text)
    if not parsed.ok:
        raise ChallengeSourceError(
            f"synthetic_boolean_v1 CNF is not valid DIMACS: {parsed.rejection_reason}"
        )
    digest = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    return ChallengeRecord(
        challenge_id=challenge_id or challenge_id_for_cnf(cnf_text),
        family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
        tier=max(0, int(tier)),
        cnf_text=cnf_text,
        status=status,
        audit_metadata={
            "source": source,
            "cnf_sha256": digest,
            "num_vars": parsed.num_vars,
            "num_clauses": parsed.num_clauses,
        },
    )


async def seed_synthetic_boolean_challenge(
    source: SqliteChallengeSource,
    *,
    cnf_text: str,
    tier: int,
    now_iso: str,
    challenge_id: str | None = None,
    activate: bool = False,
    retire_current: bool = False,
    input_source: str = "operator_cnf",
) -> ChallengeRecord:
    """Insert a private SAT challenge and optionally activate it.

    Activation is idempotent for the same challenge id. If another row
    is active, activation fails unless ``retire_current`` is true.
    """
    record = build_synthetic_boolean_challenge_record(
        cnf_text=cnf_text,
        tier=tier,
        challenge_id=challenge_id,
        status=CHALLENGE_STATUS_PENDING,
        source=input_source,
    )
    await source.upsert(record, now_iso=now_iso)
    if not activate:
        return record
    existing = await source.list_for_family(SYNTHETIC_BOOLEAN_FAMILY_ID)
    for row in existing:
        if row.challenge_id != record.challenge_id:
            continue
        if row.status == CHALLENGE_STATUS_ACTIVE:
            return row
        if row.status == CHALLENGE_STATUS_LOCKED:
            raise ChallengeSourceError("challenge is already locked")
    return await source.activate(
        family_id=SYNTHETIC_BOOLEAN_FAMILY_ID,
        challenge_id=record.challenge_id,
        now_iso=now_iso,
        retire_current=retire_current,
    )


__all__ = [
    "build_synthetic_boolean_challenge_record",
    "challenge_id_for_cnf",
    "challenge_id_for_cnf_sha256",
    "seed_synthetic_boolean_challenge",
]
