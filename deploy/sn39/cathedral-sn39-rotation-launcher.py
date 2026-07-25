#!/usr/bin/python3 -I
"""Verify and execute the immutable preparatory SN39 rotation bundle.

Install this file root-owned, mode 0755, at
``/usr/local/libexec/cathedral-sn39-rotation``. The launcher accepts the
rotation operator's arguments only after it has verified the exact reviewed
source bytes, hash-locked Python environment, bootstrap interpreter, authority
host, and authority UID recorded in the root-owned manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import sys
from pathlib import Path
from typing import Any

MANIFEST = Path("/etc/cathedral/sn39-rotation-manifest.json")
BUNDLES = Path("/opt/cathedral-sn39/rotation-bundles")
VENVS = Path("/opt/cathedral-sn39/rotation-venvs")
AUTHORITY_STATE_ROOT = Path("/var/lib/cathedral-sn39-rotation")
INSTALLED_LAUNCHER = Path("/usr/local/libexec/cathedral-sn39-rotation")
BOOTSTRAP_PYTHON = Path("/usr/bin/python3")
OPERATOR = Path("scripts/sn39_hotkey_rotation_operator.py")
MANIFEST_BUILDER = Path("scripts/build_sn39_rotation_manifest.py")
RELEASE_MANIFEST_VERIFIER = Path("scripts/build_sn39_release_manifest.py")
REVIEWED_LAUNCHER = Path("deploy/sn39/cathedral-sn39-rotation-launcher.py")
REPRODUCTION_LOCK = Path("requirements/sn39-reproduction.lock")
BUILD_LOCK = Path("requirements/sn39-build.lock")
REQUIRED_BUNDLE_FILES = frozenset(
    {
        OPERATOR.as_posix(),
        MANIFEST_BUILDER.as_posix(),
        RELEASE_MANIFEST_VERIFIER.as_posix(),
        REVIEWED_LAUNCHER.as_posix(),
        REPRODUCTION_LOCK.as_posix(),
        BUILD_LOCK.as_posix(),
    }
)
SCHEMA = "cathedral_sn39_rotation_install_v1"
CONTEXT_SCHEMA = "cathedral_sn39_rotation_execution_v1"
ROTATION_CONTEXT_ENV = "CATHEDRAL_SN39_ROTATION_CONTEXT"
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
HOST_RE = re.compile(r"[^\s\x00-\x1f\x7f]{1,255}")
ROOT_UID = 0


class RotationInstallError(RuntimeError):
    """The installed rotation runtime differs from its reviewed manifest."""


def _strict_json(path: Path) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RotationInstallError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(path.read_text("utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RotationInstallError(
            "rotation manifest is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(document, dict):
        raise RotationInstallError("rotation manifest must be an object")
    return document


def _root_controlled(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RotationInstallError(f"required path is unavailable: {path}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not expected(info.st_mode)
        or info.st_uid != ROOT_UID
        or (not directory and info.st_nlink != 1)
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise RotationInstallError(
            f"required path is not immutable and root-controlled: {path}"
        )


def _record_digest(state: Any, *parts: str) -> None:
    for part in parts:
        encoded = part.encode("utf-8")
        state.update(len(encoded).to_bytes(8, "big"))
        state.update(encoded)


def _file_digest(path: Path) -> str:
    _root_controlled(path)
    value = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                value.update(chunk)
    except OSError as exc:
        raise RotationInstallError(f"cannot hash required file: {path}") from exc
    return "sha256:" + value.hexdigest()


def _immutable_tree_digest(root: Path) -> str:
    """Bind every installed byte, mode, path, and external symlink target."""
    _root_controlled(root, directory=True)
    tree = hashlib.sha256()
    _record_digest(tree, "root", f"{stat.S_IMODE(root.stat().st_mode):04o}")
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        files.sort()
        base = Path(directory)
        for name in sorted([*names, *files]):
            path = base / name
            relative = path.relative_to(root).as_posix()
            try:
                info = path.lstat()
            except OSError as exc:
                raise RotationInstallError(
                    f"cannot inspect immutable tree entry: {path}"
                ) from exc
            if info.st_uid != ROOT_UID:
                raise RotationInstallError(
                    f"immutable tree has a mutable or unsupported entry: {path}"
                )
            mode = f"{stat.S_IMODE(info.st_mode):04o}"
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022:
                    raise RotationInstallError(
                        f"immutable tree has a mutable or unsupported entry: {path}"
                    )
                _record_digest(tree, "file", relative, mode, _file_digest(path))
            elif stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) & 0o022:
                    raise RotationInstallError(
                        f"immutable tree has a mutable or unsupported entry: {path}"
                    )
                _record_digest(tree, "directory", relative, mode)
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve(strict=True)
                    target_info = target.stat()
                except (OSError, RuntimeError) as exc:
                    raise RotationInstallError(
                        f"immutable tree symlink cannot be resolved: {path}"
                    ) from exc
                if (
                    target_info.st_uid != ROOT_UID
                    or target_info.st_nlink != 1
                    or stat.S_IMODE(target_info.st_mode) & 0o022
                    or not stat.S_ISREG(target_info.st_mode)
                ):
                    raise RotationInstallError(
                        f"immutable tree symlink has an unsafe target: {path}"
                    )
                _record_digest(
                    tree,
                    "symlink",
                    relative,
                    os.readlink(path),
                    str(target),
                    f"{stat.S_IMODE(target_info.st_mode):04o}",
                    _file_digest(target),
                )
            else:
                raise RotationInstallError(
                    f"immutable tree has a mutable or unsupported entry: {path}"
                )
    return "sha256:" + tree.hexdigest()


def _check_digest_map(values: Any, *, base: Path) -> None:
    if not isinstance(values, dict) or set(values) != REQUIRED_BUNDLE_FILES:
        raise RotationInstallError("rotation bundle file set is incomplete or changed")
    for raw_path, expected in values.items():
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected, str)
            or DIGEST_RE.fullmatch(expected) is None
        ):
            raise RotationInstallError("rotation bundle digest entry is malformed")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RotationInstallError("rotation bundle path escapes its root")
        if _file_digest(base / relative) != expected:
            raise RotationInstallError(
                f"installed file differs from rotation manifest: {base / relative}"
            )


def _verify_bootstrap(document: dict[str, Any]) -> None:
    if not (
        sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_user_site
    ):
        raise RotationInstallError("rotation launcher was not started in isolated mode")
    bootstrap = document.get("bootstrap_python")
    if not isinstance(bootstrap, dict) or set(bootstrap) != {
        "invoked_path",
        "resolved_path",
        "digest",
    }:
        raise RotationInstallError("rotation bootstrap binding is malformed")
    try:
        invoked_info = BOOTSTRAP_PYTHON.lstat()
        invoked_target = BOOTSTRAP_PYTHON.resolve(strict=True)
        running_target = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RotationInstallError(
            "rotation bootstrap interpreter is unavailable"
        ) from exc
    if (
        invoked_info.st_uid != ROOT_UID
        or stat.S_IMODE(invoked_info.st_mode) & 0o022
        or bootstrap["invoked_path"] != str(BOOTSTRAP_PYTHON)
        or bootstrap["resolved_path"] != str(invoked_target)
        or running_target != invoked_target
        or _file_digest(invoked_target) != bootstrap["digest"]
    ):
        raise RotationInstallError(
            "rotation bootstrap interpreter differs from its manifest"
        )


def _verify_authority(document: dict[str, Any]) -> tuple[int, str, str]:
    authority_host = document.get("authority_host")
    authority_uid = document.get("authority_uid")
    authority_home = document.get("authority_home")
    authority_state_dir = document.get("authority_state_dir")
    if (
        not isinstance(authority_host, str)
        or HOST_RE.fullmatch(authority_host) is None
        or isinstance(authority_uid, bool)
        or not isinstance(authority_uid, int)
        or authority_uid <= 0
        or not isinstance(authority_home, str)
        or not authority_home.startswith("/")
        or not isinstance(authority_state_dir, str)
        or authority_state_dir != str(AUTHORITY_STATE_ROOT / f"uid-{authority_uid}")
    ):
        raise RotationInstallError("rotation authority binding is malformed")
    if os.uname().nodename != authority_host or os.geteuid() != authority_uid:
        raise RotationInstallError(
            "rotation launcher is not running as the reviewed authority"
        )
    try:
        login_home = pwd.getpwuid(authority_uid).pw_dir
        home_info = Path(login_home).lstat()
        state_root_info = AUTHORITY_STATE_ROOT.lstat()
        state_info = Path(authority_state_dir).lstat()
    except KeyError as exc:
        raise RotationInstallError("rotation authority has no local account") from exc
    except OSError as exc:
        raise RotationInstallError(
            "rotation authority home or durable state directory is unavailable"
        ) from exc
    if (
        not isinstance(login_home, str)
        or login_home != authority_home
        or Path(login_home).is_symlink()
        or not stat.S_ISDIR(home_info.st_mode)
        or home_info.st_uid != authority_uid
        or stat.S_IMODE(home_info.st_mode) & 0o022
        or AUTHORITY_STATE_ROOT.is_symlink()
        or not stat.S_ISDIR(state_root_info.st_mode)
        or state_root_info.st_uid != ROOT_UID
        or stat.S_IMODE(state_root_info.st_mode) & 0o022
        or Path(authority_state_dir).is_symlink()
        or not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != authority_uid
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise RotationInstallError(
            "rotation authority home or durable state directory is unsafe"
        )
    return authority_uid, login_home, authority_state_dir


def _verify() -> tuple[Path, Path, str, dict[str, Any]]:
    _root_controlled(MANIFEST)
    manifest_digest = _file_digest(MANIFEST)
    document = _strict_json(MANIFEST)
    if (
        set(document)
        != {
            "schema",
            "source_sha",
            "bundle_files",
            "bundle_tree_digest",
            "venv_tree_digest",
            "launcher_digest",
            "bootstrap_python",
            "authority_host",
            "authority_uid",
            "authority_home",
            "authority_state_dir",
        }
        or document.get("schema") != SCHEMA
    ):
        raise RotationInstallError("rotation manifest schema or fields differ")
    _verify_bootstrap(document)
    _, login_home, authority_state_dir = _verify_authority(document)
    source_sha = document.get("source_sha")
    if not isinstance(source_sha, str) or SHA_RE.fullmatch(source_sha) is None:
        raise RotationInstallError("rotation manifest carries an invalid source SHA")

    bundle = BUNDLES / source_sha
    venv = VENVS / source_sha
    _root_controlled(BUNDLES, directory=True)
    _root_controlled(VENVS, directory=True)
    installed_bundle_digest = _immutable_tree_digest(bundle)
    if (
        DIGEST_RE.fullmatch(str(document.get("bundle_tree_digest"))) is None
        or installed_bundle_digest != document["bundle_tree_digest"]
    ):
        raise RotationInstallError(
            "rotation bundle bytes differ from the content-addressed manifest"
        )
    installed_venv_digest = _immutable_tree_digest(venv)
    if (
        DIGEST_RE.fullmatch(str(document.get("venv_tree_digest"))) is None
        or installed_venv_digest != document["venv_tree_digest"]
    ):
        raise RotationInstallError(
            "rotation venv bytes differ from the hash-locked manifest"
        )
    _check_digest_map(document["bundle_files"], base=bundle)
    if (
        DIGEST_RE.fullmatch(str(document.get("launcher_digest"))) is None
        or _file_digest(INSTALLED_LAUNCHER) != document["launcher_digest"]
    ):
        raise RotationInstallError(
            "installed rotation launcher differs from its manifest"
        )
    python = venv / "bin/python"
    if (
        not python.exists()
        or not os.access(python, os.R_OK | os.X_OK)
        or os.access(python, os.W_OK)
    ):
        raise RotationInstallError(
            "rotation environment interpreter is missing, inaccessible, or writable"
        )
    if _file_digest(MANIFEST) != manifest_digest:
        raise RotationInstallError("rotation manifest changed during verification")
    context = {
        "schema": CONTEXT_SCHEMA,
        "source_sha": source_sha,
        "manifest_sha256": manifest_digest,
        "bundle_tree_sha256": document["bundle_tree_digest"],
        "venv_tree_sha256": document["venv_tree_digest"],
        "launcher_sha256": document["launcher_digest"],
        "authority_host": document["authority_host"],
        "authority_uid": document["authority_uid"],
        "authority_home": login_home,
        "authority_state_dir": authority_state_dir,
    }
    return bundle, python, login_home, context


def _child_environment(home: str, context: dict[str, Any]) -> dict[str, str]:
    return {
        "HOME": home,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        ROTATION_CONTEXT_ENV: json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    }


def main(argv: list[str]) -> int:
    if not argv or any("\x00" in value for value in argv):
        print(
            "usage: cathedral-sn39-rotation <rotation-operator arguments>",
            file=sys.stderr,
        )
        return 2
    try:
        bundle, python, login_home, context = _verify()
    except RotationInstallError as exc:
        print(f"SN39 rotation-install check failed: {exc}", file=sys.stderr)
        return 1
    command = [
        str(python),
        "-I",
        "-B",
        str(bundle / OPERATOR),
        *argv,
    ]
    os.chdir(bundle)
    os.execve(python, command, _child_environment(login_home, context))
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
