"""Upgrade migration keeps both legacy raw journals private."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import migrate_sn39_status_stream as migration


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_both_known_legacy_journals_migrate_from_0640_to_0600(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    continuous = tmp_path / "continuous.jsonl"
    launch = tmp_path / "launch.jsonl"
    for path in (continuous, launch):
        path.write_text("private\n", encoding="utf-8")
        path.chmod(0o640)

    monkeypatch.setattr(migration, "ROOT_UID", os.geteuid())
    monkeypatch.setattr(migration, "RAW_JOURNALS", (continuous, launch))
    monkeypatch.setattr(
        migration.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=os.geteuid()),
    )
    monkeypatch.setattr(migration, "require_writers_stopped", lambda: None)

    assert migration.main() == 0
    assert _mode(continuous) == 0o600
    assert _mode(launch) == 0o600
    assert continuous.read_text(encoding="utf-8") == "private\n"
    assert launch.read_text(encoding="utf-8") == "private\n"


def test_migration_refuses_symlink_hardlink_and_unreviewed_mode(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("private", encoding="utf-8")
    target.chmod(0o640)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(migration.MigrationError, match="opened safely"):
        migration.privatize_journal(link, expected_uid=os.geteuid())

    alias_directory = tmp_path / "alias-directory"
    alias_directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(migration.MigrationError, match="directory cannot be opened"):
        migration.privatize_journal(
            alias_directory / target.name,
            expected_uid=os.geteuid(),
        )

    hardlink = tmp_path / "hardlink.jsonl"
    os.link(target, hardlink)
    with pytest.raises(migration.MigrationError, match="single-linked"):
        migration.privatize_journal(target, expected_uid=os.geteuid())
    hardlink.unlink()

    target.chmod(0o644)
    with pytest.raises(migration.MigrationError, match="0600/0640"):
        migration.privatize_journal(target, expected_uid=os.geteuid())


def test_migration_refuses_while_any_writer_is_not_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    states = iter(("inactive\n", "active\n"))
    monkeypatch.setattr(
        migration.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=next(states), returncode=0),
    )
    with pytest.raises(migration.MigrationError, match="not stopped"):
        migration.require_writers_stopped()
