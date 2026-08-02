#!/usr/bin/env python3
"""Privatize legacy SN39 raw journals before status-stream cutover.

Run only inside the documented maintenance window, after every validator
writer is stopped. The migration opens each known raw journal without
following symlinks, verifies the opened descriptor, changes 0640 to 0600, and
verifies the same inode again before returning success.
"""

from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

ROOT_UID = 0
SYSTEMCTL = Path("/usr/bin/systemctl")
VALIDATOR_USER = "cathedral-validator"
RAW_JOURNALS = (
    Path("/var/log/cathedral-validator/validator-events.jsonl"),
    Path("/var/log/cathedral-validator-launch/validator-events.jsonl"),
)
WRITER_UNITS = (
    "cathedral-validator-sn39.service",
    "cathedral-validator-sn39-launch.service",
    "cathedral-validator-sn39-reconcile.service",
    "cathedral-thin-validator.service",
    "cathedral-confidential-validator-sn39.service",
    "cathedral-confidential-validator.service",
    "cathedral-validator.service",
)


class MigrationError(RuntimeError):
    """The journal migration cannot be completed safely."""


def require_writers_stopped() -> None:
    """Refuse mode changes while any known validator writer is running."""
    for unit in WRITER_UNITS:
        try:
            result = subprocess.run(
                [str(SYSTEMCTL), "is-active", unit],
                cwd="/",
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MigrationError(
                "cannot prove every validator writer is stopped"
            ) from exc
        if result.stdout.strip() not in {"inactive", "failed"}:
            raise MigrationError(f"validator writer is not stopped: {unit}")


def privatize_journal(path: Path, *, expected_uid: int) -> str:
    """Safely convert one existing 0640 raw journal to 0600."""
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise MigrationError("raw journal path is not canonical")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = (
        os.O_RDONLY | nofollow | close_on_exec | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        parent_descriptor = os.open(path.parent, directory_flags)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        raise MigrationError(
            f"raw journal directory cannot be opened safely: {path}"
        ) from exc
    try:
        parent = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {ROOT_UID, expected_uid}
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise MigrationError(
                f"raw journal directory is not controlled: {path.parent}"
            )
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | nofollow | close_on_exec,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return "absent"
        except OSError as exc:
            raise MigrationError(
                f"raw journal cannot be opened safely: {path}"
            ) from exc
        try:
            before = os.fstat(descriptor)
            before_mode = stat.S_IMODE(before.st_mode)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid != expected_uid
                or before.st_nlink != 1
                or before_mode not in {0o600, 0o640}
            ):
                raise MigrationError(
                    "raw journal is not a single-linked validator-owned "
                    f"0600/0640 file: {path}"
                )
            if before_mode == 0o640:
                os.fchmod(descriptor, 0o600)
                os.fsync(descriptor)
            after = os.fstat(descriptor)
            if (
                (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != expected_uid
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
            ):
                raise MigrationError(f"raw journal privacy verification failed: {path}")
            return "changed" if before_mode == 0o640 else "already-private"
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def main() -> int:
    if os.geteuid() != ROOT_UID:
        print("status-stream migration must run as root", file=sys.stderr)
        return 1
    try:
        expected_uid = pwd.getpwnam(VALIDATOR_USER).pw_uid
        require_writers_stopped()
        results = [
            (path, privatize_journal(path, expected_uid=expected_uid))
            for path in RAW_JOURNALS
        ]
    except (KeyError, MigrationError) as exc:
        print(f"status-stream migration failed closed: {exc}", file=sys.stderr)
        return 1
    for path, result in results:
        print(f"{path}: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
