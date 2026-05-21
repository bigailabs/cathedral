from __future__ import annotations

import asyncio
import json

from typer.testing import CliRunner

from cathedral.cli.ops import app
from cathedral.lanes.challenge_source import SqliteChallengeSource, init_sqlite_challenge_source


def test_sat_seed_challenge_cli_activates_private_cnf_path(tmp_path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "publisher.db"
    cnf_path = tmp_path / "operator-input.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "sat-seed-challenge",
            "--db",
            str(db_path),
            "--cnf-path",
            str(cnf_path),
            "--challenge-id",
            "cli-toy-001",
            "--tier",
            "2",
            "--activate",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["challenge_id"] == "cli-toy-001"
    assert payload["status"] == "active"
    assert payload["num_vars"] == 2
    assert str(cnf_path) not in result.output


def test_sat_seed_challenge_cli_can_store_private_cnf_path(tmp_path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "publisher.db"
    cnf_path = tmp_path / "operator-input.cnf"
    cnf_path.write_text("p cnf 2 1\n1 -2 0\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "sat-seed-challenge",
            "--db",
            str(db_path),
            "--cnf-path",
            str(cnf_path),
            "--storage-mode",
            "file",
            "--challenge-id",
            "cli-file-001",
            "--activate",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["challenge_id"] == "cli-file-001"
    assert payload["storage"] == "file"
    assert payload["status"] == "active"
    assert str(cnf_path) not in result.output

    asyncio.run(_assert_file_backed_challenge(str(db_path), "cli-file-001", str(cnf_path)))


def test_sat_seed_challenge_cli_accepts_stdin_without_path_metadata(tmp_path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "publisher.db"

    result = runner.invoke(
        app,
        [
            "sat-seed-challenge",
            "--db",
            str(db_path),
            "--cnf-path",
            "-",
            "--challenge-id",
            "stdin-toy-001",
        ],
        input="p cnf 1 1\n1 0\n",
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["challenge_id"] == "stdin-toy-001"
    assert payload["status"] == "pending"


def test_sat_activate_challenge_cli_promotes_seeded_row(tmp_path) -> None:
    runner = CliRunner()
    db_path = tmp_path / "publisher.db"
    seed = runner.invoke(
        app,
        [
            "sat-seed-challenge",
            "--db",
            str(db_path),
            "--cnf-path",
            "-",
            "--challenge-id",
            "activate-toy-001",
        ],
        input="p cnf 1 1\n1 0\n",
    )
    assert seed.exit_code == 0, seed.output

    activated = runner.invoke(
        app,
        [
            "sat-activate-challenge",
            "--db",
            str(db_path),
            "--challenge-id",
            "activate-toy-001",
        ],
    )
    assert activated.exit_code == 0, activated.output
    payload = json.loads(activated.output)
    assert payload["status"] == "active"

    asyncio.run(_assert_active_challenge(str(db_path), "activate-toy-001"))


async def _assert_active_challenge(db_path: str, challenge_id: str) -> None:
    conn = await init_sqlite_challenge_source(db_path)
    try:
        source = SqliteChallengeSource(conn)
        active = await source.get_active("synthetic_boolean_v1")
        assert active is not None
        assert active.challenge_id == challenge_id
    finally:
        await conn.close()


async def _assert_file_backed_challenge(
    db_path: str,
    challenge_id: str,
    cnf_path: str,
) -> None:
    conn = await init_sqlite_challenge_source(db_path)
    try:
        source = SqliteChallengeSource(conn)
        active = await source.get_active("synthetic_boolean_v1")
        assert active is not None
        assert active.challenge_id == challenge_id
        assert active.cnf_text == ""
        assert active.cnf_path == str(cnf_path)
    finally:
        await conn.close()
