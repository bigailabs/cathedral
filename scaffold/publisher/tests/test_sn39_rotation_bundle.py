from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROOT = Path(__file__).resolve().parents[3]
launcher = _load(
    ROOT / "deploy/sn39/cathedral-sn39-rotation-launcher.py",
    "sn39_rotation_launcher_test",
)
builder = _load(
    ROOT / "scripts/build_sn39_rotation_manifest.py",
    "sn39_rotation_manifest_builder_test",
)
SOURCE_SHA = "a" * 40


def test_rotation_manifest_builder_imports_in_isolated_mode(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(ROOT / "scripts/build_sn39_rotation_manifest.py"),
            "--help",
        ],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "authority-host" in result.stdout


def test_rotation_runbook_uses_only_manifest_bound_launcher() -> None:
    guide = (ROOT / "docs/SN39_MAINNET_RELEASE_20260724.md").read_text("utf-8")
    assert "scripts/build_sn39_rotation_manifest.py" in guide
    assert "/usr/local/libexec/cathedral-sn39-rotation" in guide
    assert "/var/lib/cathedral-sn39-rotation/uid-$authority_uid" in guide
    assert "--reconcile" in guide
    assert '"$rotation_bundle/scripts/sn39_hotkey_rotation_operator.py"' not in guide
    assert '"$rotation_venv/bin/python" -I -B' not in guide


def _write_bundle(root: Path, *, source: Path | None = None) -> None:
    source_root = source or ROOT
    for name in builder.BUNDLE_FILES:
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source_root / name).read_bytes())


def _make_immutable_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o444)
        elif path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def test_rotation_launcher_binds_exact_bundle_bytes_and_file_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "bundle"
    _write_bundle(bundle)
    _make_immutable_tree(bundle)
    monkeypatch.setattr(launcher, "ROOT_UID", os.getuid())

    digest = launcher._immutable_tree_digest(bundle)
    expected = {
        name: launcher._file_digest(bundle / name) for name in builder.BUNDLE_FILES
    }
    launcher._check_digest_map(expected, base=bundle)

    operator = bundle / "scripts/sn39_hotkey_rotation_operator.py"
    operator.chmod(0o644)
    operator.write_bytes(operator.read_bytes() + b"\n# substituted\n")
    operator.chmod(0o444)
    with pytest.raises(
        launcher.RotationInstallError,
        match="differs from rotation manifest",
    ):
        launcher._check_digest_map(expected, base=bundle)
    assert launcher._immutable_tree_digest(bundle) != digest

    with pytest.raises(
        launcher.RotationInstallError,
        match="file set is incomplete or changed",
    ):
        launcher._check_digest_map(
            {**expected, "unexpected.py": "sha256:" + "0" * 64},
            base=bundle,
        )


def test_rotation_launcher_rejects_mutable_or_external_runtime_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv = tmp_path / "venv"
    binary = venv / "bin/python-real"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"reviewed-python")
    (venv / "bin/python").symlink_to("python-real")
    _make_immutable_tree(venv)
    monkeypatch.setattr(launcher, "ROOT_UID", os.getuid())
    reviewed = launcher._immutable_tree_digest(venv)

    binary.chmod(0o666)
    with pytest.raises(
        launcher.RotationInstallError,
        match="unsafe target",
    ):
        launcher._immutable_tree_digest(venv)
    binary.chmod(0o444)

    (venv / "bin").chmod(0o755)
    (venv / "bin/python").unlink()
    outside = tmp_path / "user-python"
    outside.write_bytes(b"unreviewed-python")
    outside.chmod(0o666)
    (venv / "bin/python").symlink_to(outside)
    with pytest.raises(
        launcher.RotationInstallError,
        match="unsafe target",
    ):
        launcher._immutable_tree_digest(venv)
    assert reviewed.startswith("sha256:")


def test_rotation_install_rejects_hard_linked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    reviewed = tree / "reviewed.py"
    outside = tmp_path / "outside-link.py"
    tree.mkdir()
    reviewed.write_bytes(b"reviewed")
    os.link(reviewed, outside)
    reviewed.chmod(0o444)
    tree.chmod(0o555)

    monkeypatch.setattr(launcher, "ROOT_UID", os.getuid())
    with pytest.raises(
        launcher.RotationInstallError,
        match="not immutable and root-controlled",
    ):
        launcher._file_digest(reviewed)
    with pytest.raises(
        launcher.RotationInstallError,
        match="mutable or unsupported",
    ):
        launcher._immutable_tree_digest(tree)

    original_lstat = Path.lstat

    def root_owned_lstat(path: Path):
        observed = original_lstat(path)
        return type(
            "Info",
            (),
            {
                "st_uid": 0,
                "st_mode": observed.st_mode,
                "st_nlink": observed.st_nlink,
            },
        )()

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    with pytest.raises(SystemExit, match="not immutable and root-controlled"):
        builder._require_root_controlled(reviewed)
    with pytest.raises(SystemExit, match="not authority-readable"):
        builder._require_authority_readable_tree(tree)


def test_manifest_builder_rejects_non_traversable_or_non_executable_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = tmp_path / "tree"
    python = tree / "bin/python"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    tree.chmod(0o755)
    python.parent.chmod(0o755)
    python.chmod(0o444)
    original_lstat = Path.lstat

    def root_owned_lstat(path: Path):
        observed = original_lstat(path)
        return type(
            "Info",
            (),
            {
                "st_uid": 0,
                "st_mode": observed.st_mode,
                "st_nlink": observed.st_nlink,
            },
        )()

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    with pytest.raises(SystemExit, match="not authority-readable"):
        builder._require_authority_readable_tree(tree, python=python)

    python.chmod(0o555)
    python.parent.chmod(0o700)
    with pytest.raises(SystemExit, match="directory is not authority-readable"):
        builder._require_authority_readable_tree(tree, python=python)


def test_rotation_launcher_binds_authority_and_scrubs_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_uid = os.getuid()
    authority_home = tmp_path / "authority-home"
    state_root = tmp_path / "rotation-state"
    state_dir = state_root / f"uid-{authority_uid}"
    authority_home.mkdir(mode=0o700)
    state_root.mkdir(mode=0o755)
    state_dir.mkdir(mode=0o700)
    monkeypatch.setattr(launcher, "ROOT_UID", authority_uid)
    monkeypatch.setattr(launcher, "AUTHORITY_STATE_ROOT", state_root)
    monkeypatch.setattr(launcher.os, "geteuid", lambda: authority_uid)
    monkeypatch.setattr(
        launcher.os,
        "uname",
        lambda: type("Uname", (), {"nodename": "reviewed-host"})(),
    )
    monkeypatch.setattr(
        launcher.pwd,
        "getpwuid",
        lambda _uid: type("Passwd", (), {"pw_dir": str(authority_home)})(),
    )
    authority = {
        "authority_host": "reviewed-host",
        "authority_uid": authority_uid,
        "authority_home": str(authority_home),
        "authority_state_dir": str(state_dir),
    }
    _, home, durable_state = launcher._verify_authority(authority)
    assert home == str(authority_home)
    assert durable_state == str(state_dir)
    context = {"schema": launcher.CONTEXT_SCHEMA}
    environment = launcher._child_environment(home, context)
    assert environment == {
        "HOME": str(authority_home),
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        launcher.ROTATION_CONTEXT_ENV: json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    assert "PYTHONPATH" not in environment

    with pytest.raises(
        launcher.RotationInstallError,
        match="not running as the reviewed authority",
    ):
        launcher._verify_authority({**authority, "authority_host": "other-host"})
    with pytest.raises(
        launcher.RotationInstallError,
        match="authority binding is malformed",
    ):
        launcher._verify_authority({**authority, "authority_uid": 0})


def test_rotation_manifest_builder_requires_pristine_exact_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    bundle_parent = tmp_path / "bundles"
    venv_parent = tmp_path / "venvs"
    bundle = bundle_parent / SOURCE_SHA
    venv = venv_parent / SOURCE_SHA
    launcher_path = tmp_path / "cathedral-sn39-rotation"
    bootstrap = tmp_path / "python3"
    authority_uid = os.getuid()
    authority_home = tmp_path / "authority-home"
    state_root = tmp_path / "rotation-state"
    state_dir = state_root / f"uid-{authority_uid}"
    for root in (source, bundle, venv):
        root.mkdir(parents=True)
    authority_home.mkdir(mode=0o700)
    state_root.mkdir(mode=0o755)
    state_dir.mkdir(mode=0o700)
    for name in builder.BUNDLE_FILES:
        reviewed = source / name
        reviewed.parent.mkdir(parents=True, exist_ok=True)
        reviewed.write_text(f"reviewed:{name}\n")
    reviewed_launcher = source / "deploy/sn39/cathedral-sn39-rotation-launcher.py"
    reviewed_launcher.parent.mkdir(parents=True, exist_ok=True)
    reviewed_launcher.write_text("reviewed launcher\n")
    _write_bundle(bundle, source=source)
    launcher_path.write_bytes(reviewed_launcher.read_bytes())
    bootstrap.write_text("reviewed bootstrap\n")
    for root in (bundle, venv):
        _make_immutable_tree(root)
    launcher_path.chmod(0o444)
    bootstrap.chmod(0o444)

    monkeypatch.setattr(builder, "BUNDLES", bundle_parent)
    monkeypatch.setattr(builder, "VENVS", venv_parent)
    monkeypatch.setattr(builder, "AUTHORITY_STATE_ROOT", state_root)
    monkeypatch.setattr(builder, "EXECUTING_BUNDLE_ROOT", bundle)
    monkeypatch.setattr(
        builder,
        "_git",
        lambda _root, *args: SOURCE_SHA if args == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        builder,
        "_git_blob",
        lambda _root, _sha, name: (source / name).read_bytes(),
    )
    monkeypatch.setattr(
        builder,
        "_require_root_controlled",
        lambda _path, directory=False: None,
    )
    monkeypatch.setattr(
        builder,
        "_require_authority_readable_tree",
        lambda _path, python=None: None,
    )
    monkeypatch.setattr(
        builder.release_manifest,
        "verify_locked_environment",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        builder.release_manifest,
        "immutable_tree_digest",
        lambda path: "sha256:" + ("1" if path == bundle else "2") * 64,
    )
    monkeypatch.setattr(builder.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        builder.os,
        "uname",
        lambda: type("Uname", (), {"nodename": "reviewed-host"})(),
    )
    monkeypatch.setattr(
        builder.pwd,
        "getpwuid",
        lambda uid: type(
            "Passwd",
            (),
            {"pw_uid": uid, "pw_dir": str(authority_home)},
        )(),
    )
    original_lstat = Path.lstat
    original_stat = Path.stat

    class _Info:
        def __init__(self, uid: int, mode: int, nlink: int = 1) -> None:
            self.st_uid = uid
            self.st_mode = mode
            self.st_nlink = nlink

    def fake_lstat(path: Path):
        if path == bootstrap:
            return _Info(0, 0o100444)
        if path == state_root:
            return _Info(0, 0o040755)
        return original_lstat(path)

    def fake_stat(path: Path, *args, **kwargs):
        if path == bootstrap:
            return _Info(0, 0o100444)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setattr(Path, "stat", fake_stat)
    document = builder.build_manifest(
        source=source,
        source_sha=SOURCE_SHA,
        bundle=bundle,
        venv=venv,
        launcher=launcher_path,
        bootstrap_python=bootstrap,
        authority_host="reviewed-host",
        authority_uid=authority_uid,
    )
    assert document["schema"] == builder.SCHEMA
    assert document["bundle_tree_digest"] == "sha256:" + "1" * 64
    assert document["venv_tree_digest"] == "sha256:" + "2" * 64
    assert set(document["bundle_files"]) == set(builder.BUNDLE_FILES)
    assert document["authority_home"] == str(authority_home)
    assert document["authority_state_dir"] == str(state_dir)
    assert (
        json.loads(json.dumps(document, sort_keys=True, separators=(",", ":")))
        == document
    )

    monkeypatch.setattr(builder, "EXECUTING_BUNDLE_ROOT", source)
    with pytest.raises(SystemExit, match="not executing from the installed bundle"):
        builder.build_manifest(
            source=source,
            source_sha=SOURCE_SHA,
            bundle=bundle,
            venv=venv,
            launcher=launcher_path,
            bootstrap_python=bootstrap,
            authority_host="reviewed-host",
            authority_uid=authority_uid,
        )
    monkeypatch.setattr(builder, "EXECUTING_BUNDLE_ROOT", bundle)

    installed_operator = bundle / builder.BUNDLE_FILES[0]
    installed_operator.chmod(0o644)
    installed_operator.write_text("substituted\n")
    installed_operator.chmod(0o444)
    with pytest.raises(SystemExit, match="differs from source commit"):
        builder.build_manifest(
            source=source,
            source_sha=SOURCE_SHA,
            bundle=bundle,
            venv=venv,
            launcher=launcher_path,
            bootstrap_python=bootstrap,
            authority_host="reviewed-host",
            authority_uid=authority_uid,
        )
