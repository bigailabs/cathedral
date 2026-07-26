#!/usr/bin/env python3
"""Build the root-owned immutable-install manifest for one reviewed SN39 SHA."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"[0-9a-f]{40}")
NAME_RE = re.compile(r"[-_.]+")
EXPECTED_VERIFIER_BINARY = (
    "sha256:35bb55f89f411d5dcf5f72be90488e999ee68c41dfc0429a0dcb8cc2b448b6bb"
)
EXPECTED_CATHEDRAL_URL = (
    "https://github.com/cathedralai/cathedralconfidential/archive/"
    "655c264421a1f5f2e625a372a40f595aa1e114ab.tar.gz"
)
EXPECTED_CATHEDRAL_ARCHIVE_SHA256 = (
    "befc572f459c2d80af7ce18013cb4d3649716f143da0a6a86a4a8b96f84b88fb"
)
RELEASE_FILES = (
    "config/validator-mainnet-sn39.toml",
    "config/validator-mainnet-sn39-launch.toml",
    "config/provenance/registry-keys.json",
    "config/provenance/report-keys.json",
    "config/provenance/index-keys.json",
    "config/provenance/release-attestation-keys.json",
    "requirements/sn39-reproduction.lock",
    "deploy/sn39/cathedral-sn39-release-launcher.py",
    "deploy/sn39/cathedral-sn39-rotation-launcher.py",
    "deploy/sn39/cathedral-validator-sn39.service",
    "deploy/sn39/cathedral-validator-sn39-launch.service",
    "deploy/sn39/cathedral-validator-sn39-reconcile.service",
    "requirements/sn39-build.in",
    "requirements/sn39-build.lock",
    "scaffold/sn39_continuous_authorization.py",
    "scripts/finalize_sn39_public_release.py",
    "scripts/build_sn39_rotation_manifest.py",
    "scripts/publish_sn39_validator_status.py",
    "scripts/sn39_hotkey_rotation_operator.py",
    "deploy/sn39/cathedral-sn39-public-status.service",
    "deploy/sn39/cathedral-sn39-public-status.timer",
    "deploy/sn39/cathedral-sn39-validator.sysusers",
    "deploy/sn39/cathedral-sn39-validator.tmpfiles",
)


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def record_digest(state: Any, *parts: str) -> None:
    for part in parts:
        encoded = part.encode("utf-8")
        state.update(len(encoded).to_bytes(8, "big"))
        state.update(encoded)


def immutable_tree_digest(root: Path) -> str:
    """Generate the byte-for-byte environment commitment checked at exec."""
    if root.is_symlink() or not root.is_dir():
        raise SystemExit("versioned venv root must be a real directory")
    tree = hashlib.sha256()
    record_digest(tree, "root", f"{stat.S_IMODE(root.stat().st_mode):04o}")
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        files.sort()
        base = Path(directory)
        for name in sorted([*names, *files]):
            path = base / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            mode = f"{stat.S_IMODE(info.st_mode):04o}"
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise SystemExit(f"versioned venv has a hard-linked file: {path}")
                record_digest(tree, "file", relative, mode, digest(path))
            elif stat.S_ISDIR(info.st_mode):
                record_digest(tree, "directory", relative, mode)
            elif stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                target_info = target.stat()
                if not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink != 1:
                    raise SystemExit(
                        f"versioned venv symlink target is unsupported: {path}"
                    )
                record_digest(
                    tree,
                    "symlink",
                    relative,
                    os.readlink(path),
                    str(target),
                    f"{stat.S_IMODE(target_info.st_mode):04o}",
                    digest(target),
                )
            else:
                raise SystemExit(f"versioned venv has unsupported entry type: {path}")
    return "sha256:" + tree.hexdigest()


def canonical_name(value: str) -> str:
    return NAME_RE.sub("-", value).lower()


def locked_distributions(
    lock: Path,
    *,
    require_cathedral: bool,
) -> dict[str, str]:
    expected: dict[str, str] = {}
    for line in lock.read_text("utf-8").splitlines():
        if not line or line[0].isspace() or line.startswith("#"):
            continue
        direct = re.match(r"^([A-Za-z0-9_.-]+)\s+@\s+(\S+)\s+\\$", line)
        pinned = re.match(r"^([A-Za-z0-9_.-]+)==(\S+)\s+\\$", line)
        if direct:
            name, url = direct.groups()
            if canonical_name(name) != "cathedral" or url != EXPECTED_CATHEDRAL_URL:
                raise SystemExit(
                    "reproduction lock has an unexpected direct dependency"
                )
            expected["cathedral"] = "0.0.0"
        elif pinned:
            name, version = pinned.groups()
            normalized = canonical_name(name)
            if normalized in expected:
                raise SystemExit(f"reproduction lock duplicates {normalized}")
            expected[normalized] = version
        else:
            raise SystemExit(
                f"reproduction lock has an unsupported requirement: {line}"
            )
    if require_cathedral and (len(expected) != 55 or "cathedral" not in expected):
        raise SystemExit("reproduction lock distribution set is incomplete")
    if not require_cathedral and (
        set(expected)
        != {"hatchling", "packaging", "pathspec", "pluggy", "trove-classifiers"}
        or "cathedral" in expected
    ):
        raise SystemExit("build lock distribution set is incomplete")
    lock_text = lock.read_text("utf-8")
    if require_cathedral and (
        f"--hash=sha256:{EXPECTED_CATHEDRAL_ARCHIVE_SHA256}" not in lock_text
        or EXPECTED_CATHEDRAL_URL not in lock_text
    ):
        raise SystemExit("Cathedral archive bytes are not pinned in the lock")
    if not require_cathedral and (
        EXPECTED_CATHEDRAL_URL in lock_text or "hatchling==" not in lock_text
    ):
        raise SystemExit("build lock contains an unexpected source package")
    return expected


def expected_locked_distributions(
    reproduction_lock: Path,
    build_lock: Path,
) -> dict[str, str]:
    expected = locked_distributions(
        reproduction_lock,
        require_cathedral=True,
    )
    for name, version in locked_distributions(
        build_lock,
        require_cathedral=False,
    ).items():
        if name in expected and expected[name] != version:
            raise SystemExit(f"build and reproduction locks disagree on {name}")
        expected[name] = version
    return expected


def validate_installed_distributions(
    document: dict[str, Any],
    expected: dict[str, str],
) -> None:
    rows = document.get("installed")
    if not isinstance(rows, list):
        raise SystemExit("pip inspect returned no installed distribution list")
    installed: dict[str, tuple[str, dict[str, Any] | None]] = {}
    for row in rows:
        metadata = row.get("metadata") if isinstance(row, dict) else None
        if not isinstance(metadata, dict):
            raise SystemExit("pip inspect returned malformed package metadata")
        name = canonical_name(str(metadata.get("name") or ""))
        version = str(metadata.get("version") or "")
        if not name or not version or name in installed:
            raise SystemExit("pip inspect returned a duplicate or nameless package")
        direct_url = row.get("direct_url")
        installed[name] = (
            version,
            direct_url if isinstance(direct_url, dict) else None,
        )
    unexpected = set(installed) - set(expected) - {"pip"}
    missing = set(expected) - set(installed)
    mismatched = {
        name
        for name, version in expected.items()
        if name in installed and installed[name][0] != version
    }
    if unexpected or missing or mismatched:
        raise SystemExit(
            "installed venv differs from the hash lock "
            f"(unexpected={sorted(unexpected)}, missing={sorted(missing)}, "
            f"version_mismatch={sorted(mismatched)})"
        )
    cathedral_url = installed["cathedral"][1] or {}
    archive_info = cathedral_url.get("archive_info") or {}
    hashes = archive_info.get("hashes") or {}
    if (
        cathedral_url.get("url") != EXPECTED_CATHEDRAL_URL
        or hashes.get("sha256") != EXPECTED_CATHEDRAL_ARCHIVE_SHA256
    ):
        raise SystemExit(
            "installed Cathedral dependency is not the hash-locked commit archive"
        )


def verify_locked_environment(
    venv: Path,
    reproduction_lock: Path,
    build_lock: Path,
) -> None:
    python = venv / "bin/python"
    try:
        raw = subprocess.check_output(
            [str(python), "-m", "pip", "inspect", "--local"],
            text=True,
            timeout=120,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        document = json.loads(raw)
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as exc:
        raise SystemExit("cannot inspect the versioned venv") from exc
    validate_installed_distributions(
        document,
        expected_locked_distributions(reproduction_lock, build_lock),
    )


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["/usr/bin/git", "-c", f"safe.directory={root}", *args],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("cannot verify reviewed release source") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--venv", type=Path)
    parser.add_argument(
        "--continuous-config",
        type=Path,
        default=Path("/etc/cathedral/validator-mainnet-sn39.toml"),
    )
    parser.add_argument(
        "--launch-config",
        type=Path,
        default=Path("/etc/cathedral/validator-mainnet-sn39-launch.toml"),
    )
    parser.add_argument(
        "--verifier",
        type=Path,
        default=Path("/opt/cathedral-sn39/bin/cathedral-tdx-verifier"),
    )
    parser.add_argument(
        "--launcher",
        type=Path,
        default=Path("/usr/local/libexec/cathedral-sn39-release"),
    )
    parser.add_argument(
        "--continuous-unit",
        type=Path,
        default=Path("/etc/systemd/system/cathedral-validator-sn39.service"),
    )
    parser.add_argument(
        "--launch-unit",
        type=Path,
        default=Path("/etc/systemd/system/cathedral-validator-sn39-launch.service"),
    )
    parser.add_argument(
        "--reconcile-unit",
        type=Path,
        default=Path("/etc/systemd/system/cathedral-validator-sn39-reconcile.service"),
    )
    parser.add_argument(
        "--status-unit",
        type=Path,
        default=Path("/etc/systemd/system/cathedral-sn39-public-status.service"),
    )
    parser.add_argument(
        "--status-timer",
        type=Path,
        default=Path("/etc/systemd/system/cathedral-sn39-public-status.timer"),
    )
    parser.add_argument(
        "--sysusers",
        type=Path,
        default=Path("/etc/sysusers.d/cathedral-sn39-validator.conf"),
    )
    parser.add_argument(
        "--tmpfiles",
        type=Path,
        default=Path("/etc/tmpfiles.d/cathedral-sn39-validator.conf"),
    )
    parser.add_argument(
        "--bootstrap-python",
        type=Path,
        default=Path("/usr/bin/python3"),
    )
    args = parser.parse_args()
    root = args.release.resolve()
    venv = (
        args.venv.resolve()
        if args.venv is not None
        else Path("/opt/cathedral-sn39/venvs") / args.release_sha
    )
    if (
        SHA_RE.fullmatch(args.release_sha) is None
        or git(root, "rev-parse", "HEAD") != args.release_sha
        or git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
    ):
        raise SystemExit("release checkout is not the exact pristine requested SHA")
    config_pairs = (
        (args.continuous_config, root / "config/validator-mainnet-sn39.toml"),
        (args.launch_config, root / "config/validator-mainnet-sn39-launch.toml"),
        (
            args.launcher,
            root / "deploy/sn39/cathedral-sn39-release-launcher.py",
        ),
        (
            args.continuous_unit,
            root / "deploy/sn39/cathedral-validator-sn39.service",
        ),
        (
            args.launch_unit,
            root / "deploy/sn39/cathedral-validator-sn39-launch.service",
        ),
        (
            args.reconcile_unit,
            root / "deploy/sn39/cathedral-validator-sn39-reconcile.service",
        ),
        (
            args.status_unit,
            root / "deploy/sn39/cathedral-sn39-public-status.service",
        ),
        (
            args.status_timer,
            root / "deploy/sn39/cathedral-sn39-public-status.timer",
        ),
        (args.sysusers, root / "deploy/sn39/cathedral-sn39-validator.sysusers"),
        (args.tmpfiles, root / "deploy/sn39/cathedral-sn39-validator.tmpfiles"),
    )
    for installed, reviewed in config_pairs:
        if installed.read_bytes() != reviewed.read_bytes():
            raise SystemExit(
                f"installed file differs from reviewed release: {installed}"
            )
    if digest(args.verifier) != EXPECTED_VERIFIER_BINARY:
        raise SystemExit("installed verifier binary differs from the launch pin")
    try:
        bootstrap_info = args.bootstrap_python.lstat()
        bootstrap_resolved = args.bootstrap_python.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemExit("bootstrap interpreter is unavailable") from exc
    if (
        bootstrap_info.st_uid != 0
        or stat.S_IMODE(bootstrap_info.st_mode) & 0o022
        or not bootstrap_resolved.is_file()
        or bootstrap_resolved.stat().st_uid != 0
        or stat.S_IMODE(bootstrap_resolved.stat().st_mode) & 0o022
    ):
        raise SystemExit("bootstrap interpreter is not root-controlled")
    verify_locked_environment(
        venv,
        root / "requirements/sn39-reproduction.lock",
        root / "requirements/sn39-build.lock",
    )
    release_files = {name: digest(root / name) for name in RELEASE_FILES}
    external_files = {
        str(args.continuous_config): digest(args.continuous_config),
        str(args.launch_config): digest(args.launch_config),
        str(args.verifier): digest(args.verifier),
        str(args.launcher): digest(args.launcher),
        str(args.continuous_unit): digest(args.continuous_unit),
        str(args.launch_unit): digest(args.launch_unit),
        str(args.reconcile_unit): digest(args.reconcile_unit),
        str(args.status_unit): digest(args.status_unit),
        str(args.status_timer): digest(args.status_timer),
        str(args.sysusers): digest(args.sysusers),
        str(args.tmpfiles): digest(args.tmpfiles),
    }
    print(
        json.dumps(
            {
                "schema": "cathedral_sn39_release_install_v3",
                "release_sha": args.release_sha,
                "release_files": release_files,
                "external_files": external_files,
                "venv_tree_digest": immutable_tree_digest(venv),
                "bootstrap_python": {
                    "invoked_path": str(args.bootstrap_python),
                    "resolved_path": str(bootstrap_resolved),
                    "digest": digest(bootstrap_resolved),
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
