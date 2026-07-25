#!/usr/bin/env python3
"""Build the immutable preparatory SN39 hotkey-rotation manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

_RELEASE_MANIFEST_PATH = Path(__file__).with_name("build_sn39_release_manifest.py")
_RELEASE_MANIFEST_SPEC = importlib.util.spec_from_file_location(
    "_cathedral_sn39_release_manifest",
    _RELEASE_MANIFEST_PATH,
)
if _RELEASE_MANIFEST_SPEC is None or _RELEASE_MANIFEST_SPEC.loader is None:
    raise SystemExit("cannot load the adjacent release-manifest verifier")
release_manifest = importlib.util.module_from_spec(_RELEASE_MANIFEST_SPEC)
_RELEASE_MANIFEST_SPEC.loader.exec_module(release_manifest)

SCHEMA = "cathedral_sn39_rotation_install_v1"
SHA_RE = re.compile(r"[0-9a-f]{40}")
HOST_RE = re.compile(r"[^\s\x00-\x1f\x7f]{1,255}")
BUNDLES = Path("/opt/cathedral-sn39/rotation-bundles")
VENVS = Path("/opt/cathedral-sn39/rotation-venvs")
AUTHORITY_STATE_ROOT = Path("/var/lib/cathedral-sn39-rotation")
EXECUTING_BUNDLE_ROOT = Path(__file__).resolve(strict=True).parents[1]
BUNDLE_FILES = (
    "scripts/sn39_hotkey_rotation_operator.py",
    "scripts/build_sn39_rotation_manifest.py",
    "scripts/build_sn39_release_manifest.py",
    "deploy/sn39/cathedral-sn39-rotation-launcher.py",
    "requirements/sn39-reproduction.lock",
    "requirements/sn39-build.lock",
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["/usr/bin/git", "-c", f"safe.directory={root}", *args],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit("cannot verify preparatory rotation source") from exc


def _git_blob(root: Path, source_sha: str, name: str) -> bytes:
    try:
        return subprocess.check_output(
            [
                "/usr/bin/git",
                "-c",
                f"safe.directory={root}",
                "show",
                f"{source_sha}:{name}",
            ],
            cwd=root,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            f"cannot read reviewed rotation file from source commit: {name}"
        ) from exc


def _require_root_controlled(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SystemExit(f"required installed path is unavailable: {path}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not expected(info.st_mode)
        or info.st_uid != 0
        or (not directory and info.st_nlink != 1)
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise SystemExit(f"installed path is not immutable and root-controlled: {path}")


def _require_authority_readable_tree(root: Path, *, python: Path | None = None) -> None:
    """Reject an immutable tree the named non-root authority cannot traverse."""
    for path in (root, *sorted(root.rglob("*"))):
        try:
            info = path.lstat()
        except OSError as exc:
            raise SystemExit(f"installed rotation path is unavailable: {path}") from exc
        mode = stat.S_IMODE(info.st_mode)
        if info.st_uid != 0 or mode & 0o022:
            raise SystemExit(
                f"installed rotation tree is not immutable and root-owned: {path}"
            )
        if stat.S_ISDIR(info.st_mode):
            if mode & 0o005 != 0o005:
                raise SystemExit(
                    f"installed rotation directory is not authority-readable: {path}"
                )
        elif stat.S_ISREG(info.st_mode):
            required = 0o005 if python is not None and path == python else 0o004
            if info.st_nlink != 1 or mode & required != required:
                raise SystemExit(
                    f"installed rotation file is not authority-readable: {path}"
                )
        elif stat.S_ISLNK(info.st_mode):
            try:
                target = path.resolve(strict=True)
                target_info = target.stat()
            except (OSError, RuntimeError) as exc:
                raise SystemExit(
                    f"installed rotation symlink is unavailable: {path}"
                ) from exc
            target_mode = stat.S_IMODE(target_info.st_mode)
            required = 0o005 if python is not None and path == python else 0o004
            if (
                target_info.st_uid != 0
                or target_info.st_nlink != 1
                or target_mode & 0o022
                or not stat.S_ISREG(target_info.st_mode)
                or target_mode & required != required
            ):
                raise SystemExit(f"installed rotation symlink target is unsafe: {path}")
        else:
            raise SystemExit(f"installed rotation tree has unsupported entry: {path}")


def _verify_installed_bundle(
    source: Path,
    source_sha: str,
    bundle: Path,
) -> dict[str, str]:
    _require_root_controlled(bundle, directory=True)
    digests: dict[str, str] = {}
    for name in BUNDLE_FILES:
        installed = bundle / name
        _require_root_controlled(installed)
        reviewed = _git_blob(source, source_sha, name)
        if reviewed != installed.read_bytes():
            raise SystemExit(
                f"installed rotation file differs from source commit: {name}"
            )
        digests[name] = "sha256:" + hashlib.sha256(reviewed).hexdigest()
    installed_files = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    installed_symlinks = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_symlink()
    }
    if installed_files != set(BUNDLE_FILES) or installed_symlinks:
        raise SystemExit(
            "installed rotation bundle contains an unexpected file or symlink"
        )
    return digests


def build_manifest(
    *,
    source: Path,
    source_sha: str,
    bundle: Path,
    venv: Path,
    launcher: Path,
    bootstrap_python: Path,
    authority_host: str,
    authority_uid: int,
) -> dict[str, Any]:
    root = source.resolve(strict=True)
    bundle_root = bundle.resolve(strict=True)
    venv_root = venv.resolve(strict=True)
    if (
        SHA_RE.fullmatch(source_sha) is None
        or _git(root, "rev-parse", "HEAD") != source_sha
        or _git(
            root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        )
    ):
        raise SystemExit("rotation source is not the exact pristine requested SHA")
    if (
        os.geteuid() != 0
        or HOST_RE.fullmatch(authority_host) is None
        or isinstance(authority_uid, bool)
        or authority_uid <= 0
    ):
        raise SystemExit("rotation authority host or UID is malformed")
    if os.uname().nodename != authority_host:
        raise SystemExit("rotation manifest must be built on the authority host")
    try:
        account = pwd.getpwuid(authority_uid)
    except KeyError as exc:
        raise SystemExit("rotation authority UID has no local account") from exc
    authority_home = Path(str(account.pw_dir))
    authority_state_dir = AUTHORITY_STATE_ROOT / f"uid-{authority_uid}"
    try:
        home_info = authority_home.lstat()
        state_root_info = AUTHORITY_STATE_ROOT.lstat()
        state_info = authority_state_dir.lstat()
    except OSError as exc:
        raise SystemExit(
            "rotation authority home or durable state directory is unavailable"
        ) from exc
    if (
        not authority_home.is_absolute()
        or authority_home.is_symlink()
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != authority_uid
        or stat.S_IMODE(home_info.st_mode) & 0o022
        or AUTHORITY_STATE_ROOT.is_symlink()
        or not stat.S_ISDIR(state_root_info.st_mode)
        or state_root_info.st_uid != 0
        or stat.S_IMODE(state_root_info.st_mode) & 0o022
        or authority_state_dir.is_symlink()
        or not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != authority_uid
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise SystemExit("rotation authority home or durable state directory is unsafe")
    expected_bundle = BUNDLES / source_sha
    expected_venv = VENVS / source_sha
    if bundle_root != expected_bundle or venv_root != expected_venv:
        raise SystemExit("rotation bundle or venv is not at its content-addressed path")
    if EXECUTING_BUNDLE_ROOT != bundle_root:
        raise SystemExit(
            "rotation manifest builder is not executing from the installed bundle"
        )

    _require_root_controlled(launcher)
    reviewed_launcher = _git_blob(
        root,
        source_sha,
        "deploy/sn39/cathedral-sn39-rotation-launcher.py",
    )
    if launcher.read_bytes() != reviewed_launcher:
        raise SystemExit(
            "installed rotation launcher differs from reviewed source commit"
        )
    bundle_files = _verify_installed_bundle(root, source_sha, bundle_root)

    _require_root_controlled(venv_root, directory=True)
    _require_authority_readable_tree(bundle_root)
    _require_authority_readable_tree(venv_root, python=venv_root / "bin/python")
    release_manifest.verify_locked_environment(
        venv_root,
        bundle_root / "requirements/sn39-reproduction.lock",
        bundle_root / "requirements/sn39-build.lock",
    )
    try:
        bootstrap_info = bootstrap_python.lstat()
        bootstrap_resolved = bootstrap_python.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemExit("bootstrap interpreter is unavailable") from exc
    if (
        bootstrap_info.st_uid != 0
        or (stat.S_ISREG(bootstrap_info.st_mode) and bootstrap_info.st_nlink != 1)
        or stat.S_IMODE(bootstrap_info.st_mode) & 0o022
        or not bootstrap_resolved.is_file()
        or bootstrap_resolved.stat().st_uid != 0
        or bootstrap_resolved.stat().st_nlink != 1
        or stat.S_IMODE(bootstrap_resolved.stat().st_mode) & 0o022
    ):
        raise SystemExit("bootstrap interpreter is not root-controlled")

    return {
        "schema": SCHEMA,
        "source_sha": source_sha,
        "bundle_files": bundle_files,
        "bundle_tree_digest": release_manifest.immutable_tree_digest(bundle_root),
        "venv_tree_digest": release_manifest.immutable_tree_digest(venv_root),
        "launcher_digest": _digest(launcher),
        "bootstrap_python": {
            "invoked_path": str(bootstrap_python),
            "resolved_path": str(bootstrap_resolved),
            "digest": _digest(bootstrap_resolved),
        },
        "authority_host": authority_host,
        "authority_uid": authority_uid,
        "authority_home": str(authority_home),
        "authority_state_dir": str(authority_state_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--venv", type=Path)
    parser.add_argument(
        "--launcher",
        type=Path,
        default=Path("/usr/local/libexec/cathedral-sn39-rotation"),
    )
    parser.add_argument(
        "--bootstrap-python",
        type=Path,
        default=Path("/usr/bin/python3"),
    )
    parser.add_argument("--authority-host", required=True)
    parser.add_argument("--authority-uid", required=True, type=int)
    args = parser.parse_args()
    source_sha = str(args.source_sha)
    bundle = args.bundle or (BUNDLES / source_sha)
    venv = args.venv or (VENVS / source_sha)
    document = build_manifest(
        source=args.source,
        source_sha=source_sha,
        bundle=bundle,
        venv=venv,
        launcher=args.launcher,
        bootstrap_python=args.bootstrap_python,
        authority_host=args.authority_host,
        authority_uid=args.authority_uid,
    )
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
