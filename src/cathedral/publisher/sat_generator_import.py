"""Import a SAT challenge from the private generator into Cathedral.

The orchestration on top of ``sat_generator_client.SatGeneratorClient``:

    lease  →  fetch CNF  →  verify sha256  →  write to volume
           →  parse DIMACS header  →  upsert lane_challenges row
           →  activate (if requested)  →  confirm lease

Any failure between lease and confirm releases the lease so the CNF
returns to the generator pool for another consumer. The release is
best-effort — if it also fails we log and move on; the lease will
auto-expire at the generator's TTL.

This module knows nothing about HTTP — it accepts a constructed
``SatGeneratorClient`` (so tests can inject a mock-transport client)
and an open ``SqliteChallengeSource`` (so the import lands in the
same DB the publisher reads from).
"""

from __future__ import annotations

import contextlib
import os
import secrets
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog

from cathedral.lanes.challenge_source import (
    ActiveChallengeScope,
    ChallengeRecord,
    SqliteChallengeSource,
)
from cathedral.publisher.sat_generator_client import (
    LeaseResult,
    SatGeneratorClient,
    SatGeneratorError,
)

logger = structlog.get_logger(__name__)

SAT_FAMILY_ID = "synthetic_boolean_v1"
_DEFAULT_TIME_LIMIT_SECONDS = 7 * 24 * 60 * 60  # 1 week


@dataclass(frozen=True)
class ImportResult:
    """Returned to the caller. Contains everything operator/log needs.

    Note: deliberately does NOT include the generator_run_id or
    lease_id in any field that might leak via __repr__ to a public
    surface — those live in audit_metadata only, which is private.
    """

    cathedral_challenge_id: str
    tier: int
    kind: str
    cnf_sha256: str
    byte_size: int
    activated: bool


async def import_challenge_from_generator(
    *,
    client: SatGeneratorClient,
    source: SqliteChallengeSource,
    storage_root: Path,
    tier: int,
    kind: str,
    family: str = SAT_FAMILY_ID,
    activate: bool = True,
    active_scope: ActiveChallengeScope = "tier",
    retire_current: bool = False,
    time_limit_seconds: int = _DEFAULT_TIME_LIMIT_SECONDS,
    idempotency_key: str | None = None,
    db_write_lock: AbstractAsyncContextManager | None = None,
) -> ImportResult:
    """Lease one CNF and durably import it as a Cathedral challenge.

    On the happy path: returns ImportResult with the assigned
    Cathedral-owned challenge_id. The CNF body lives at
    ``storage_root/<challenge_id>.cnf`` (0600 perms).

    On any failure between lease and confirm: releases the lease
    (best-effort) and re-raises. The DB is rolled back; no orphan
    files are left under ``storage_root`` (the partial write, if any,
    is removed in the finally block).
    """
    storage_root.mkdir(parents=True, exist_ok=True)

    lease: LeaseResult | None = None
    challenge_id: str | None = None
    cnf_path: Path | None = None
    db_committed = False  # True once source.upsert returned without raising
    confirmed = False
    try:
        # --- 1. Lease ---
        lease = await client.lease(
            tier=tier, kind=kind, family=family, idempotency_key=idempotency_key
        )
        logger.info(
            "sat_generator_import_leased",
            lease_id=lease.lease_id,
            tier=lease.tier,
            kind=lease.kind,
            cnf_sha256_prefix=lease.cnf_sha256[:12],
            byte_size=lease.byte_size,
        )

        # --- 2. Fetch + verify (verify is inside client.fetch_cnf) ---
        cnf_bytes = await client.fetch_cnf(lease)

        # --- 3. Validate DIMACS header against leased metadata ---
        header_vars, header_clauses = _parse_dimacs_header(cnf_bytes)
        if header_vars != lease.num_vars or header_clauses != lease.num_clauses:
            raise SatGeneratorError(
                f"dimacs header mismatch: header={header_vars}v/{header_clauses}c "
                f"vs lease={lease.num_vars}v/{lease.num_clauses}c"
            )

        # --- 4. Assign Cathedral-owned public challenge_id ---
        challenge_id = _mint_challenge_id(tier=lease.tier, kind=lease.kind)

        # --- 5. Atomic write to volume ---
        cnf_path = storage_root / f"{challenge_id}.cnf"
        _atomic_write_bytes(cnf_path, cnf_bytes, mode=0o600)

        # --- 6. Build + upsert ChallengeRecord ---
        audit = {
            "source": "generator_lease",
            "generator_run_id": lease.generator_run_id,
            "cnf_bytes": lease.byte_size,
            "cnf_sha256": lease.cnf_sha256,
            "num_vars": lease.num_vars,
            "num_clauses": lease.num_clauses,
            "storage": "file",
            "kind": lease.kind,
            "cnf_class": lease.cnf_class,
            "time_limit_seconds": time_limit_seconds,
        }
        # File-backed storage: the ~0.5-1.8 MB DIMACS body lives in the file
        # written above; the row carries only ``cnf_path`` + metadata. This
        # keeps the ``db_write_lock`` insert tiny (it previously held the lock
        # while writing the whole CNF as inline ``cnf_text``, which serialized
        # against the open-window solve flood and capped import throughput).
        # The serve path (challenge_cnf.py) now reads file-backed CNFs lock-free
        # per request, and the solve verifier (submit.py) already reads
        # ``cnf_path``. Inline ``cnf_text`` rows still serve for backward compat.
        record = ChallengeRecord(
            challenge_id=challenge_id,
            family_id=family,
            tier=lease.tier,
            cnf_text="",
            cnf_path=str(cnf_path),
            status="pending",
            audit_metadata=audit,
        )
        now_iso = _now_iso()
        # Serialize ONLY the shared-connection writes (upsert + activate) under
        # the publisher write gate — NOT the lease/fetch/confirm network I/O
        # above and below. The gate is a process-wide non-reentrant lock; if it
        # wrapped the whole import, a slow generator request would block winner
        # writes for the duration of the HTTP round-trips. Default nullcontext
        # keeps direct/test callers lock-free.
        activated = False
        async with (db_write_lock or contextlib.nullcontext()):
            await source.upsert(record, now_iso=now_iso)
            db_committed = True

            # --- 7. Activate if requested ---
            if activate:
                await source.activate(
                    family_id=family,
                    challenge_id=challenge_id,
                    now_iso=now_iso,
                    retire_current=retire_current,
                    active_scope=active_scope,
                )
                activated = True

        # --- 8. Confirm lease (only after durable commit succeeds) ---
        await client.confirm(
            lease.lease_id,
            cathedral_challenge_id=challenge_id,
            cnf_sha256_witnessed=lease.cnf_sha256,
        )
        confirmed = True

        logger.info(
            "sat_generator_import_committed",
            cathedral_challenge_id=challenge_id,
            tier=lease.tier,
            kind=lease.kind,
            byte_size=lease.byte_size,
            activated=activated,
        )

        return ImportResult(
            cathedral_challenge_id=challenge_id,
            tier=lease.tier,
            kind=lease.kind,
            cnf_sha256=lease.cnf_sha256,
            byte_size=lease.byte_size,
            activated=activated,
        )

    finally:
        # Release-on-failure: if we leased but never confirmed (any
        # exception path), release the lease back to the pool so
        # another consumer can take it. The release is best-effort —
        # the generator auto-expires the lease at its configured TTL
        # so we never block on it succeeding.
        if lease is not None and not confirmed:
            try:
                await client.release(lease.lease_id)
            except (SatGeneratorError, OSError) as exc:
                logger.exception(
                    "sat_generator_release_after_failure_errored",
                    lease_id=lease.lease_id,
                    error=str(exc),
                )
            # File cleanup is GATED on db_committed: once the upsert
            # succeeded, the DB has a row pointing at this file and we
            # MUST NOT remove it — that would leave a public active
            # row referencing a missing path. Only clean up when no DB
            # row claims the file (pre-upsert failure path).
            if cnf_path is not None and cnf_path.exists() and not db_committed:
                try:
                    cnf_path.unlink()
                except OSError as exc:
                    logger.warning(
                        "sat_generator_partial_file_cleanup_failed",
                        path=str(cnf_path),
                        error=str(exc),
                    )
            elif db_committed and not confirmed:
                # We have a durable Cathedral challenge that the
                # generator hasn't recorded as consumed. Lease will
                # auto-expire generator-side; the Cathedral row is
                # valid and should stand.
                logger.warning(
                    "sat_generator_import_committed_but_not_confirmed",
                    cathedral_challenge_id=challenge_id,
                    lease_id=lease.lease_id,
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _mint_challenge_id(*, tier: int, kind: str) -> str:
    """Mint a Cathedral-owned public ``challenge_id``.

    Format: ``sat-t{tier}-{kind}-{YYYYMMDD}-{16hex}``.

    Deliberately does NOT include the generator_run_id or any token —
    a public ID derived from a generator-private value would leak the
    mapping. We use ``secrets.token_hex(8)`` (64 bits) for the suffix: at
    8k challenges/day the old 32-bit suffix had a non-negligible per-(tier,
    kind,day) birthday-collision chance, and an id collision can silently
    overwrite a still-``pending`` row (immutability is only enforced after a
    row leaves ``pending``). 64 bits makes a same-day collision astronomically
    unlikely, and the CNF file path is tied to this unique id.
    """
    today = datetime.now(UTC).strftime("%Y%m%d")
    short_kind = kind.replace("_", "-")
    rand = secrets.token_hex(8)
    return f"sat-t{int(tier)}-{short_kind}-{today}-{rand}"


def _parse_dimacs_header(body: bytes) -> tuple[int, int]:
    """Read the ``p cnf <num_vars> <num_clauses>`` header from the body.

    Doesn't load the whole CNF into a string — scans the first ~4 KiB,
    which is more than enough for the header and any leading ``c``
    comment lines.
    """
    head = body[: 8 * 1024].decode("utf-8", errors="replace")
    for raw in head.splitlines():
        line = raw.strip()
        if not line or line.startswith("c"):
            continue
        if line.startswith("p "):
            parts = line.split()
            if len(parts) >= 4 and parts[1] == "cnf":
                return int(parts[2]), int(parts[3])
            raise SatGeneratorError(f"malformed dimacs header: {line!r}")
        # Hit a non-comment, non-header line first → no header at all.
        raise SatGeneratorError("no `p cnf` header in first lines of body")
    raise SatGeneratorError("no `p cnf` header found within scan window")


def _atomic_write_bytes(path: Path, body: bytes, *, mode: int) -> None:
    """Write body atomically: write to a sibling temp file then rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(body)
        os.chmod(tmp, mode)
        tmp.replace(path)
    finally:
        if tmp.exists():
            # If rename didn't happen for some reason, drop the temp.
            try:
                tmp.unlink()
            except OSError:
                pass
