from __future__ import annotations

import asyncio
import hashlib
import json

from typer.testing import CliRunner

from cathedral.cli.ops import app as ops_app
from cathedral.lanes.challenge_source import (
    CHALLENGE_STATUS_ACTIVE,
    ChallengeRecord,
    SqliteChallengeSource,
    init_sqlite_challenge_source,
)
from cathedral.lanes.synthetic_boolean_v1 import FAMILY_ID
from cathedral.publisher.cli import app as publisher_app
from cathedral.publisher.sat_file_challenges import build_synthetic_boolean_file_challenge_record
from cathedral.publisher.sat_status import active_sat_challenge_status


async def _upsert_record(db_path, record: ChallengeRecord) -> None:
    conn = await init_sqlite_challenge_source(str(db_path))
    try:
        source = SqliteChallengeSource(conn)
        await source.upsert(record, overwrite_status=True)
    finally:
        await conn.close()


async def _read_status(db_path, *, verify_file_hash: bool = False) -> dict[str, object]:
    conn = await init_sqlite_challenge_source(str(db_path))
    try:
        source = SqliteChallengeSource(conn)
        return await active_sat_challenge_status(
            source,
            verify_file_hash=verify_file_hash,
        )
    finally:
        await conn.close()


def test_active_sat_status_reports_sqlite_text_without_raw_cnf(tmp_path) -> None:
    cnf_text = "p cnf 2 1\n1 -2 0\n"
    digest = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    record = ChallengeRecord(
        challenge_id="status-text-001",
        family_id=FAMILY_ID,
        tier=2,
        cnf_text=cnf_text,
        status=CHALLENGE_STATUS_ACTIVE,
        audit_metadata={
            "storage": "sqlite_text",
            "cnf_sha256": digest,
            "num_vars": 2,
            "num_clauses": 1,
        },
    )
    db_path = tmp_path / "publisher.db"
    asyncio.run(_upsert_record(db_path, record))

    status = asyncio.run(_read_status(db_path))
    payload = json.dumps(status, sort_keys=True)

    assert status["ok"] is True
    assert status["active"] is True
    assert status["challenge_id"] == "status-text-001"
    assert status["storage"] == "sqlite_text"
    assert status["sqlite_text_bytes"] == len(cnf_text.encode("utf-8"))
    assert cnf_text not in payload
    assert "cnf_path" not in payload


def test_active_sat_status_reports_file_metadata_without_private_path(tmp_path) -> None:
    cnf_path = tmp_path / "operator-input.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")
    record = build_synthetic_boolean_file_challenge_record(
        cnf_path=cnf_path,
        tier=3,
        challenge_id="status-file-001",
        status=CHALLENGE_STATUS_ACTIVE,
    )
    db_path = tmp_path / "publisher.db"
    asyncio.run(_upsert_record(db_path, record))

    status = asyncio.run(_read_status(db_path))
    payload = json.dumps(status, sort_keys=True)

    assert status["ok"] is True
    assert status["active"] is True
    assert status["challenge_id"] == "status-file-001"
    assert status["storage"] == "file"
    assert status["cnf_file_configured"] is True
    assert status["cnf_file_readable"] is True
    assert status["cnf_file_bytes"] == cnf_path.stat().st_size
    assert str(cnf_path) not in payload
    assert "operator-input.cnf" not in payload


def test_active_sat_status_can_verify_file_hash_mismatch(tmp_path) -> None:
    cnf_path = tmp_path / "operator-input.cnf"
    cnf_path.write_text("p cnf 1 1\n1 0\n", encoding="utf-8")
    record = build_synthetic_boolean_file_challenge_record(
        cnf_path=cnf_path,
        tier=1,
        challenge_id="status-hash-001",
        status=CHALLENGE_STATUS_ACTIVE,
    )
    db_path = tmp_path / "publisher.db"
    asyncio.run(_upsert_record(db_path, record))
    cnf_path.write_text("p cnf 1 1\n-1 0\n", encoding="utf-8")

    status = asyncio.run(_read_status(db_path, verify_file_hash=True))

    assert status["ok"] is False
    assert status["cnf_hash_matches_metadata"] is False
    assert "active challenge CNF file hash does not match audit metadata" in status["errors"]


def test_publisher_cli_reports_active_status_without_private_path(tmp_path) -> None:
    cnf_path = tmp_path / "operator-input.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")
    record = build_synthetic_boolean_file_challenge_record(
        cnf_path=cnf_path,
        tier=4,
        challenge_id="publisher-cli-status-001",
        status=CHALLENGE_STATUS_ACTIVE,
    )
    db_path = tmp_path / "publisher.db"
    asyncio.run(_upsert_record(db_path, record))

    result = CliRunner().invoke(
        publisher_app,
        ["sat-active-challenge-status", "--db", str(db_path), "--verify-cnf-hash"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["cnf_hash_matches_metadata"] is True
    assert payload["storage"] == "file"
    assert str(cnf_path) not in result.output


def test_ops_cli_reports_active_status_and_fails_without_active_challenge(tmp_path) -> None:
    db_path = tmp_path / "publisher.db"
    asyncio.run(_upsert_empty_db(db_path))

    result = CliRunner().invoke(
        ops_app,
        ["sat-active-challenge-status", "--db", str(db_path)],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["active"] is False


async def _upsert_empty_db(db_path) -> None:
    conn = await init_sqlite_challenge_source(str(db_path))
    await conn.close()
