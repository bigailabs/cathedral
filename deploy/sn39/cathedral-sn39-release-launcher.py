#!/usr/bin/python3.12 -I
"""Verify and exec one immutable SN39 validator release.

Install this file root-owned, mode 0755, at
``/usr/local/libexec/cathedral-sn39-release``. The release manifest is
generated only after the reviewed commit exists; the service account cannot
modify the manifest, release checkout, versioned venv, configs, pins, or
verifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

INSTALL_ROOT = Path("/etc/cathedral-validator")
MANIFEST = INSTALL_ROOT / "sn39-release-manifest.json"
RELEASES = Path("/opt/cathedral-sn39/releases")
VENVS = Path("/opt/cathedral-sn39/venvs")
RUNTIME_ROOT = Path("/var/lib/cathedral-validator")
JOURNAL_RE = re.compile(r"journal-[0-9a-f]{64}\.json")
FINALIZER_CONTEXT_ENV = "CATHEDRAL_SN39_FINALIZER_CONTEXT"
RECURRING_AUTHORIZER_CONTEXT_ENV = "CATHEDRAL_SN39_RECURRING_AUTHORIZER_CONTEXT"
CONFIGS = {
    "preflight": INSTALL_ROOT / "validator-mainnet-sn39-launch.toml",
    "launch": INSTALL_ROOT / "validator-mainnet-sn39-launch.toml",
    "continuous": INSTALL_ROOT / "validator-mainnet-sn39.toml",
    "reconcile": INSTALL_ROOT / "validator-mainnet-sn39.toml",
}
MODES = frozenset({*CONFIGS, "status", "finalize", "authorize-recurring"})
LEGACY_SERVICE_MASK = Path("/etc/systemd/system/cathedral-thin-validator.service")
SYSTEMCTL = Path("/usr/bin/systemctl")
LEGACY_SERVICE_UNIT = "cathedral-thin-validator.service"
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
ROOT_UID = 0
BOOTSTRAP_PYTHON = Path("/usr/bin/python3.12")


class InstallError(RuntimeError):
    """The installed release differs from its root-owned manifest."""


def _strict_json(path: Path) -> dict[str, Any]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InstallError(f"duplicate manifest key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(path.read_text("utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("release manifest is not strict UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise InstallError("release manifest must be an object")
    return document


def _root_controlled(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallError(f"required path is unavailable: {path}") from exc
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        path.is_symlink()
        or not expected(info.st_mode)
        or info.st_uid != ROOT_UID
        or (not directory and info.st_nlink != 1)
        or stat.S_IMODE(info.st_mode) & 0o022
        or (directory and stat.S_IMODE(info.st_mode) & 0o005 != 0o005)
        or (not directory and stat.S_IMODE(info.st_mode) & 0o004 != 0o004)
    ):
        raise InstallError(
            f"required path is not immutable and root-controlled: {path}"
        )


def _record_digest(state: Any, *parts: str) -> None:
    for part in parts:
        encoded = part.encode("utf-8")
        state.update(len(encoded).to_bytes(8, "big"))
        state.update(encoded)


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
                raise InstallError(
                    f"cannot inspect immutable tree entry: {path}"
                ) from exc
            if info.st_uid != ROOT_UID:
                raise InstallError(
                    f"immutable tree has a mutable or unsupported entry: {path}"
                )
            mode = f"{stat.S_IMODE(info.st_mode):04o}"
            if stat.S_ISREG(info.st_mode):
                if (
                    info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or stat.S_IMODE(info.st_mode) & 0o004 != 0o004
                ):
                    raise InstallError(
                        f"immutable tree has a mutable or unsupported entry: {path}"
                    )
                _record_digest(tree, "file", relative, mode, _digest(path))
            elif stat.S_ISDIR(info.st_mode):
                if (
                    stat.S_IMODE(info.st_mode) & 0o022
                    or stat.S_IMODE(info.st_mode) & 0o005 != 0o005
                ):
                    raise InstallError(
                        f"immutable tree has a mutable or unsupported entry: {path}"
                    )
                _record_digest(tree, "directory", relative, mode)
            elif stat.S_ISLNK(info.st_mode):
                try:
                    target = path.resolve(strict=True)
                    target_info = target.stat()
                except (OSError, RuntimeError) as exc:
                    raise InstallError(
                        f"immutable tree symlink cannot be resolved: {path}"
                    ) from exc
                if stat.S_ISDIR(target_info.st_mode) and target.is_relative_to(root):
                    if (
                        target_info.st_uid != ROOT_UID
                        or stat.S_IMODE(target_info.st_mode) & 0o022
                        or stat.S_IMODE(target_info.st_mode) & 0o005 != 0o005
                    ):
                        raise InstallError(
                            f"immutable tree symlink has an unsafe target: {path}"
                        )
                    _record_digest(
                        tree,
                        "directory-symlink",
                        relative,
                        os.readlink(path),
                        target.relative_to(root).as_posix(),
                        f"{stat.S_IMODE(target_info.st_mode):04o}",
                    )
                elif (
                    target_info.st_uid != ROOT_UID
                    or target_info.st_nlink != 1
                    or stat.S_IMODE(target_info.st_mode) & 0o022
                    or stat.S_IMODE(target_info.st_mode) & 0o004 != 0o004
                    or not stat.S_ISREG(target_info.st_mode)
                ):
                    raise InstallError(
                        f"immutable tree symlink has an unsafe target: {path}"
                    )
                else:
                    _record_digest(
                        tree,
                        "symlink",
                        relative,
                        os.readlink(path),
                        str(target),
                        f"{stat.S_IMODE(target_info.st_mode):04o}",
                        _digest(target),
                    )
            else:
                raise InstallError(
                    f"immutable tree has a mutable or unsupported entry: {path}"
                )
    return "sha256:" + tree.hexdigest()


def _require_service_interpreter(path: Path) -> None:
    """Require an interpreter that every unprivileged service can execute."""
    try:
        info = path.stat()
    except OSError as exc:
        raise InstallError(
            "versioned release interpreter is missing or inaccessible"
        ) from exc
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISREG(info.st_mode) or mode & 0o005 != 0o005:
        raise InstallError(
            "versioned release interpreter is not readable and executable "
            "by service accounts"
        )


def _require_legacy_service_masked() -> None:
    try:
        info = LEGACY_SERVICE_MASK.lstat()
        target = os.readlink(LEGACY_SERVICE_MASK)
    except OSError as exc:
        raise InstallError(
            "legacy cathedral-thin-validator.service is not durably masked"
        ) from exc
    if (
        not stat.S_ISLNK(info.st_mode)
        or info.st_uid != ROOT_UID
        or target != "/dev/null"
    ):
        raise InstallError(
            "legacy cathedral-thin-validator.service is not durably masked"
        )


def _require_legacy_service_stopped() -> None:
    try:
        result = subprocess.run(
            [str(SYSTEMCTL), "is-active", LEGACY_SERVICE_UNIT],
            cwd="/",
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            "cannot verify that the legacy validator is stopped"
        ) from exc
    if result.stdout.strip() not in {"inactive", "failed"}:
        raise InstallError("legacy cathedral-thin-validator.service is not stopped")


def _digest(path: Path) -> str:
    _root_controlled(path)
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _verify_bootstrap(document: dict[str, Any]) -> None:
    """Bind the isolated, absolute interpreter selected by the systemd unit."""
    if not (
        sys.flags.isolated and sys.flags.ignore_environment and sys.flags.no_user_site
    ):
        raise InstallError("release launcher was not started in isolated mode")
    bootstrap = document.get("bootstrap_python")
    if not isinstance(bootstrap, dict) or set(bootstrap) != {
        "invoked_path",
        "resolved_path",
        "digest",
    }:
        raise InstallError("release bootstrap interpreter binding is malformed")
    try:
        invoked_info = BOOTSTRAP_PYTHON.lstat()
        invoked_target = BOOTSTRAP_PYTHON.resolve(strict=True)
        running_target = Path(sys.executable).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InstallError("release bootstrap interpreter is unavailable") from exc
    if (
        invoked_info.st_uid != ROOT_UID
        or stat.S_IMODE(invoked_info.st_mode) & 0o022
        or bootstrap["invoked_path"] != str(BOOTSTRAP_PYTHON)
        or bootstrap["resolved_path"] != str(invoked_target)
        or running_target != invoked_target
        or _digest(invoked_target) != bootstrap["digest"]
    ):
        raise InstallError("release bootstrap interpreter differs from its manifest")


def _child_environment(
    mode: str = "continuous",
    *,
    release_sha: str | None = None,
    launch_config_sha256: str | None = None,
) -> dict[str, str]:
    """Return the complete allowlisted environment for the validator child."""
    environment = {
        "HOME": (
            "/var/lib/cathedral-public-evidence"
            if mode == "status"
            else "/var/lib/cathedral-validator"
        ),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if mode in {"preflight", "launch", "continuous", "reconcile"}:
        environment["CATHEDRAL_VALIDATOR_JSONL_GROUP"] = "cathedral-validator-log"
    if release_sha is not None:
        environment["CATHEDRAL_SN39_RELEASE_SHA"] = release_sha
    if launch_config_sha256 is not None:
        environment["CATHEDRAL_SN39_LAUNCH_CONFIG_SHA256"] = launch_config_sha256
    return environment


def _check_digest_map(
    values: Any,
    *,
    base: Path | None = None,
) -> None:
    if not isinstance(values, dict) or not values:
        raise InstallError("release manifest digest map is empty or malformed")
    for raw_path, expected in values.items():
        if (
            not isinstance(raw_path, str)
            or not isinstance(expected, str)
            or DIGEST_RE.fullmatch(expected) is None
        ):
            raise InstallError("release manifest digest entry is malformed")
        relative = Path(raw_path)
        path = (base / relative) if base is not None else relative
        if base is not None and (relative.is_absolute() or ".." in relative.parts):
            raise InstallError("release manifest relative path escapes its release")
        if _digest(path) != expected:
            raise InstallError(f"installed file differs from release manifest: {path}")


def _git_output(release: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["/usr/bin/git", "-c", f"safe.directory={release}", *args],
            cwd=release,
            text=True,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise InstallError("cannot verify immutable release checkout") from exc


def _finalizer_context_digest(
    *,
    release_sha: str,
    journal: Path,
    manifest_digest: str,
) -> str:
    payload = (
        "cathedral-sn39-finalizer-context-v1\n"
        f"{release_sha}\n"
        f"{manifest_digest}\n"
        f"{journal}\n"
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _recurring_authorizer_context_digest(
    *,
    release_sha: str,
    manifest_digest: str,
    arguments: list[str],
) -> str:
    payload = json.dumps(
        {
            "schema": "cathedral_sn39_recurring_authorizer_context_v1",
            "release_sha": release_sha,
            "manifest_digest": manifest_digest,
            "arguments": arguments,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _finalizer_journal(value: str) -> Path:
    journal = Path(value)
    if (
        not journal.is_absolute()
        or journal.parent != RUNTIME_ROOT
        or JOURNAL_RE.fullmatch(journal.name) is None
    ):
        raise InstallError("finalizer journal is outside the canonical runtime root")
    return journal


def _verify(mode: str) -> tuple[Path, Path, str]:
    _root_controlled(MANIFEST)
    manifest_digest = _digest(MANIFEST)
    document = _strict_json(MANIFEST)
    if (
        set(document)
        != {
            "schema",
            "release_sha",
            "release_files",
            "external_files",
            "venv_tree_digest",
            "bootstrap_python",
        }
        or document.get("schema") != "cathedral_sn39_release_install_v3"
    ):
        raise InstallError("release manifest schema or fields differ")
    _verify_bootstrap(document)
    release_sha = document.get("release_sha")
    if not isinstance(release_sha, str) or SHA_RE.fullmatch(release_sha) is None:
        raise InstallError("release manifest carries an invalid release SHA")

    release = RELEASES / release_sha
    venv = VENVS / release_sha
    _root_controlled(RELEASES, directory=True)
    _root_controlled(VENVS, directory=True)
    _immutable_tree_digest(release)
    installed_venv_digest = _immutable_tree_digest(venv)
    if (
        DIGEST_RE.fullmatch(str(document.get("venv_tree_digest"))) is None
        or installed_venv_digest != document["venv_tree_digest"]
    ):
        raise InstallError(
            "versioned venv bytes differ from the hash-locked installed manifest"
        )
    _require_legacy_service_masked()
    _require_legacy_service_stopped()
    if _git_output(release, "rev-parse", "HEAD") != release_sha:
        raise InstallError("release checkout HEAD differs from its manifest")
    if _git_output(
        release,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=matching",
    ):
        raise InstallError("release checkout is not pristine")

    _check_digest_map(document["release_files"], base=release)
    _check_digest_map(document["external_files"])
    config = CONFIGS.get(mode)
    if config is not None and str(config) not in document["external_files"]:
        raise InstallError("selected service config is not bound by the manifest")
    python = venv / "bin/python"
    _require_service_interpreter(python)
    if _digest(MANIFEST) != manifest_digest:
        raise InstallError("release manifest changed during immutable-install check")
    return release, python, manifest_digest


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    finalize = mode == "finalize"
    authorize_recurring = mode == "authorize-recurring"
    if (
        mode not in MODES
        or (finalize and len(argv) != 2)
        or (authorize_recurring and len(argv) < 2)
        or (not finalize and not authorize_recurring and len(argv) != 1)
    ):
        print(
            "usage: cathedral-sn39-release "
            "{preflight|launch|reconcile|continuous|status|finalize JOURNAL|"
            "authorize-recurring OPTIONS...}",
            file=sys.stderr,
        )
        return 2
    if (
        finalize or authorize_recurring or mode == "preflight"
    ) and os.geteuid() != ROOT_UID:
        print(
            f"SN39 {mode} launcher must run as root",
            file=sys.stderr,
        )
        return 1
    try:
        journal = _finalizer_journal(argv[1]) if finalize else None
        release, python, manifest_digest = _verify(mode)
    except InstallError as exc:
        print(f"SN39 immutable-install check failed: {exc}", file=sys.stderr)
        return 1
    if finalize:
        assert journal is not None
        command = [
            str(python),
            "-I",
            "-B",
            str(release / "scripts/finalize_sn39_public_release.py"),
            "--release",
            str(release),
            "--release-sha",
            release.name,
            "--journal",
            str(journal),
        ]
    elif authorize_recurring:
        # Isolated mode deliberately omits cwd and environment paths. This
        # fixed bootstrap inserts only the already verified immutable release,
        # then executes the reviewed module with the operator's data arguments.
        command = [
            str(python),
            "-I",
            "-E",
            "-s",
            "-B",
            "-c",
            (
                "import runpy,sys;"
                "sys.path.insert(0,sys.argv.pop(1));"
                "runpy.run_module(sys.argv.pop(1),run_name='__main__',alter_sys=True)"
            ),
            str(release),
            "scaffold.sn39_continuous_authorization",
            *argv[1:],
        ]
    elif mode == "status":
        command = [str(python), "scripts/publish_sn39_validator_status.py"]
    else:
        command = [
            str(python),
            "-m",
            "scaffold.cli",
            (
                "reconcile-launch"
                if mode == "reconcile"
                else "preflight-launch"
                if mode == "preflight"
                else "serve"
            ),
            "--config",
            str(CONFIGS[mode]),
        ]
        if mode in {"launch", "continuous"}:
            command.append("--broadcast")
        if mode == "launch":
            command.append("--once")
    config = CONFIGS.get(mode)
    environment = _child_environment(
        mode,
        release_sha=release.name,
        launch_config_sha256=_digest(config) if config is not None else None,
    )
    if finalize:
        assert journal is not None
        environment[FINALIZER_CONTEXT_ENV] = _finalizer_context_digest(
            release_sha=release.name,
            journal=journal,
            manifest_digest=manifest_digest,
        )
    if authorize_recurring:
        environment[RECURRING_AUTHORIZER_CONTEXT_ENV] = (
            _recurring_authorizer_context_digest(
                release_sha=release.name,
                manifest_digest=manifest_digest,
                arguments=argv[1:],
            )
        )
    os.chdir(release)
    os.execve(python, command, environment)
    return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
