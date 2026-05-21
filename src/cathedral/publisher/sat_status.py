"""Private-safe publisher SAT challenge status helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cathedral.lanes.challenge_source import SqliteChallengeSource, init_sqlite_challenge_source
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID as SYNTHETIC_BOOLEAN_FAMILY_ID
from cathedral.publisher.sat_file_challenges import sha256_file


async def active_sat_challenge_status(
    source: SqliteChallengeSource,
    *,
    verify_file_hash: bool = False,
) -> dict[str, Any]:
    """Return operator-safe metadata for the active SAT challenge.

    The result deliberately omits raw CNF text, local CNF paths, and
    fetch tokens. It is safe for operator logs and deploy checks, but it
    is still a publisher-private view and should not be exposed as a
    public endpoint.
    """
    record = await source.get_active(SYNTHETIC_BOOLEAN_FAMILY_ID)
    if record is None:
        return {
            "ok": False,
            "active": False,
            "family_id": SYNTHETIC_BOOLEAN_FAMILY_ID,
            "errors": ["no active synthetic_boolean_v1 challenge"],
        }

    errors: list[str] = []
    audit = record.audit_metadata
    storage = _storage_mode(record)
    status: dict[str, Any] = {
        "ok": True,
        "active": True,
        "challenge_id": record.challenge_id,
        "family_id": record.family_id,
        "tier": record.tier,
        "status": record.status,
        "storage": storage,
        "cnf_file_configured": bool(record.cnf_path),
    }
    _copy_audit_field(status, audit, "cnf_sha256")
    _copy_audit_field(status, audit, "num_vars")
    _copy_audit_field(status, audit, "num_clauses")

    if record.cnf_path:
        _add_file_status(
            status,
            errors,
            cnf_path=record.cnf_path,
            expected_sha256=_string_or_none(audit.get("cnf_sha256")),
            verify_file_hash=verify_file_hash,
        )
    else:
        status["sqlite_text_bytes"] = len(record.cnf_text.encode("utf-8"))
        if storage == "file":
            errors.append("active challenge is marked file-backed but has no CNF file")
        if not record.cnf_text:
            errors.append("active challenge has neither SQLite CNF text nor CNF file")

    if errors:
        status["ok"] = False
        status["errors"] = errors
    return status


async def active_sat_challenge_status_from_db(
    database_path: str,
    *,
    verify_file_hash: bool = False,
) -> dict[str, Any]:
    """Open the publisher DB and return active SAT status without mutating rows."""
    if not await asyncio.to_thread(_database_exists, database_path):
        return {
            "ok": False,
            "active": False,
            "family_id": SYNTHETIC_BOOLEAN_FAMILY_ID,
            "errors": ["publisher database does not exist"],
        }

    conn = await init_sqlite_challenge_source(database_path)
    try:
        source = SqliteChallengeSource(conn)
        return await active_sat_challenge_status(source, verify_file_hash=verify_file_hash)
    finally:
        await conn.close()


def _storage_mode(record: Any) -> str:
    audit_storage = _string_or_none(record.audit_metadata.get("storage"))
    if record.cnf_path:
        return "file"
    return audit_storage or "sqlite_text"


def _database_exists(database_path: str) -> bool:
    return Path(database_path).expanduser().exists()


def _copy_audit_field(target: dict[str, Any], audit: dict[str, Any], key: str) -> None:
    value = audit.get(key)
    if value is not None:
        target[key] = value


def _add_file_status(
    status: dict[str, Any],
    errors: list[str],
    *,
    cnf_path: str,
    expected_sha256: str | None,
    verify_file_hash: bool,
) -> None:
    path = Path(cnf_path).expanduser()
    try:
        stat = path.stat()
    except OSError:
        status["cnf_file_readable"] = False
        errors.append("active challenge CNF file is not readable")
        return

    status["cnf_file_readable"] = path.is_file()
    status["cnf_file_bytes"] = stat.st_size
    status["cnf_file_modified_at"] = _timestamp_iso(stat.st_mtime)
    if not path.is_file():
        errors.append("active challenge CNF file is not a regular file")
        return

    if verify_file_hash:
        try:
            actual_sha256 = sha256_file(path)
        except OSError:
            status["cnf_file_readable"] = False
            errors.append("active challenge CNF file could not be hashed")
            return
        status["cnf_sha256_actual"] = actual_sha256
        if expected_sha256 is None:
            errors.append("active challenge has no CNF SHA-256 audit metadata")
            return
        matches = actual_sha256 == expected_sha256
        status["cnf_hash_matches_metadata"] = matches
        if not matches:
            errors.append("active challenge CNF file hash does not match audit metadata")


def _timestamp_iso(seconds: float) -> str:
    dt = datetime.fromtimestamp(seconds, tz=UTC)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "active_sat_challenge_status",
    "active_sat_challenge_status_from_db",
]
