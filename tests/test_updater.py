from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


def _run_unchecked(
    cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _make_signer(tmp_path: Path, name: str, email: str) -> tuple[Path, str]:
    key_path = tmp_path / f"{name}_signing_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        text=True,
        capture_output=True,
    )
    return key_path, f"{email} {key_path.with_suffix('.pub').read_text()}"


def _write_executable(path: Path, log_path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"$0 $*\" >> {shlex.quote(str(log_path))}\n"
    )
    path.chmod(0o755)


def _write_validator_executable_with_state_log(path: Path, log_path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"printf '%s\\n' \"STATE=${{CATHEDRAL_VALIDATOR_STATE_DIR:-}}\""
        f" >> {shlex.quote(str(log_path))}\n"
        f"printf '%s\\n' \"$0 $*\" >> {shlex.quote(str(log_path))}\n"
    )
    path.chmod(0o755)


def _create_signed_repo(
    tmp_path: Path,
    *,
    latest_signer: Path | None = None,
    latest_email: str = "release@example.com",
) -> tuple[Path, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _run(["git", "init", "--initial-branch=main", "--quiet"], cwd=source)
    _run(["git", "config", "user.name", "Release Signer"], cwd=source)
    _run(["git", "config", "user.email", "release@example.com"], cwd=source)
    _run(["git", "config", "gpg.format", "ssh"], cwd=source)

    allowed_key, allowed_signer = _make_signer(tmp_path, "allowed", "release@example.com")
    _run(["git", "config", "user.signingkey", str(allowed_key)], cwd=source)

    (source / "bin").mkdir()
    (source / "scripts").mkdir()
    updater = source / "bin" / "updater.sh"
    updater.write_text((REPO_ROOT / "bin" / "updater.sh").read_text())
    updater.chmod(0o755)
    (source / "scripts" / "ecosystem.config.cjs").write_text("module.exports = { apps: [] };\n")
    _run(["git", "add", "."], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "initial updater"], cwd=source)
    _run(["git", "tag", "-s", "v0.0.1", "-m", "v0.0.1"], cwd=source)

    (source / "scripts" / "ecosystem.config.cjs").write_text(
        "module.exports = { apps: [{ name: 'cathedral-validator' }] };\n"
    )
    _run(["git", "add", "scripts/ecosystem.config.cjs"], cwd=source)
    _run(["git", "commit", "--quiet", "-m", "update ecosystem"], cwd=source)
    if latest_signer is not None:
        _run(["git", "config", "user.email", latest_email], cwd=source)
        _run(["git", "config", "user.signingkey", str(latest_signer)], cwd=source)
    _run(["git", "tag", "-s", "v0.0.2", "-m", "v0.0.2"], cwd=source)

    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "--initial-branch=main", "--quiet", str(origin)], cwd=tmp_path)
    _run(["git", "remote", "add", "origin", str(origin)], cwd=source)
    _run(["git", "push", "--quiet", "origin", "main", "--tags"], cwd=source)

    work = tmp_path / "work"
    _run(["git", "clone", "--quiet", str(origin), str(work)], cwd=tmp_path)
    _run(["git", "checkout", "--quiet", "v0.0.1"], cwd=work)
    return work, allowed_key, allowed_signer


def test_updater_applies_latest_signed_tag_and_restarts_validator(tmp_path: Path) -> None:
    work, _allowed_key, allowed_signer = _create_signed_repo(tmp_path)
    install_prefix = tmp_path / "install"
    etc_dir = tmp_path / "etc" / "cathedral"
    bin_dir = tmp_path / "bin"
    install_prefix.mkdir()
    etc_dir.mkdir(parents=True)
    bin_dir.mkdir()

    allowed_signers = install_prefix / "allowed_signers"
    allowed_signers.write_text(allowed_signer)
    config_path = etc_dir / "custom-mainnet.toml"
    config_path.write_text("[network]\nname = \"finney\"\n")
    (etc_dir / "validator.env").write_text(f"CATHEDRAL_CONFIG_PATH={config_path}\n")

    pip_log = tmp_path / "pip.log"
    validator_log = tmp_path / "validator.log"
    pm2_log = tmp_path / "pm2.log"
    fake_pip = bin_dir / "pip"
    fake_validator = bin_dir / "cathedral-validator"
    fake_pm2 = bin_dir / "pm2"
    _write_executable(fake_pip, pip_log)
    _write_executable(fake_validator, validator_log)
    _write_executable(fake_pm2, pm2_log)

    env = {
        **os.environ,
        "CATHEDRAL_UPDATER_REPO_DIR": str(work),
        "CATHEDRAL_INSTALL_PREFIX": str(install_prefix),
        "CATHEDRAL_VALIDATOR_ENV": str(etc_dir / "validator.env"),
        "CATHEDRAL_ETC_DIR": str(etc_dir),
        "CATHEDRAL_UPDATER_RUN_ONCE": "1",
        "CATHEDRAL_UPDATER_PIP_BIN": str(fake_pip),
        "CATHEDRAL_UPDATER_VALIDATOR_BIN": str(fake_validator),
        "CATHEDRAL_UPDATER_PM2_BIN": str(fake_pm2),
    }

    result = _run_unchecked([str(work / "bin" / "updater.sh")], cwd=work, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    current_tag = _run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=work,
    ).stdout.strip()
    assert current_tag == "v0.0.2"
    assert " install --quiet -e ." in pip_log.read_text()
    assert f" migrate --config {config_path}" in validator_log.read_text()
    expected_pm2 = (
        f" startOrReload {install_prefix / 'ecosystem.config.cjs'} "
        "--only cathedral-validator --update-env"
    )
    assert expected_pm2 in pm2_log.read_text()
    assert "cathedral-validator" in (install_prefix / "ecosystem.config.cjs").read_text()


def test_updater_exports_writable_state_dir_before_legacy_migration(tmp_path: Path) -> None:
    work, _allowed_key, allowed_signer = _create_signed_repo(tmp_path)
    install_prefix = tmp_path / "install"
    etc_dir = tmp_path / "etc" / "cathedral"
    bin_dir = tmp_path / "bin"
    install_prefix.mkdir()
    etc_dir.mkdir(parents=True)
    bin_dir.mkdir()

    allowed_signers = install_prefix / "allowed_signers"
    allowed_signers.write_text(allowed_signer)
    (etc_dir / "testnet.toml").write_text("[network]\nname = \"test\"\n")

    blocked_parent = tmp_path / "blocked-state-parent"
    blocked_parent.write_text("not a directory")
    requested_state_dir = blocked_parent / "cathedral"
    fallback_state_dir = install_prefix / "state"

    pip_log = tmp_path / "pip.log"
    validator_log = tmp_path / "validator.log"
    pm2_log = tmp_path / "pm2.log"
    fake_pip = bin_dir / "pip"
    fake_validator = bin_dir / "cathedral-validator"
    fake_pm2 = bin_dir / "pm2"
    _write_executable(fake_pip, pip_log)
    _write_validator_executable_with_state_log(fake_validator, validator_log)
    _write_executable(fake_pm2, pm2_log)

    env = {
        **os.environ,
        "CATHEDRAL_UPDATER_REPO_DIR": str(work),
        "CATHEDRAL_INSTALL_PREFIX": str(install_prefix),
        "CATHEDRAL_VALIDATOR_ENV": str(etc_dir / "validator.env"),
        "CATHEDRAL_ETC_DIR": str(etc_dir),
        "CATHEDRAL_VALIDATOR_STATE_DIR": str(requested_state_dir),
        "CATHEDRAL_UPDATER_RUN_ONCE": "1",
        "CATHEDRAL_UPDATER_PIP_BIN": str(fake_pip),
        "CATHEDRAL_UPDATER_VALIDATOR_BIN": str(fake_validator),
        "CATHEDRAL_UPDATER_PM2_BIN": str(fake_pm2),
    }

    result = _run_unchecked([str(work / "bin" / "updater.sh")], cwd=work, env=env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert fallback_state_dir.is_dir()
    validator_text = validator_log.read_text()
    assert f"STATE={fallback_state_dir}" in validator_text
    assert f" migrate --config {etc_dir / 'testnet.toml'}" in validator_text
    assert "cannot use validator state dir" in result.stderr


def test_updater_refuses_untrusted_latest_tag_before_checkout(tmp_path: Path) -> None:
    untrusted_key, _untrusted_signer = _make_signer(
        tmp_path,
        "untrusted",
        "untrusted@example.com",
    )
    work, _allowed_key, allowed_signer = _create_signed_repo(
        tmp_path,
        latest_signer=untrusted_key,
        latest_email="untrusted@example.com",
    )
    install_prefix = tmp_path / "install"
    etc_dir = tmp_path / "etc" / "cathedral"
    bin_dir = tmp_path / "bin"
    install_prefix.mkdir()
    etc_dir.mkdir(parents=True)
    bin_dir.mkdir()
    (install_prefix / "allowed_signers").write_text(allowed_signer)

    pip_log = tmp_path / "pip.log"
    validator_log = tmp_path / "validator.log"
    pm2_log = tmp_path / "pm2.log"
    fake_pip = bin_dir / "pip"
    fake_validator = bin_dir / "cathedral-validator"
    fake_pm2 = bin_dir / "pm2"
    _write_executable(fake_pip, pip_log)
    _write_executable(fake_validator, validator_log)
    _write_executable(fake_pm2, pm2_log)

    env = {
        **os.environ,
        "CATHEDRAL_UPDATER_REPO_DIR": str(work),
        "CATHEDRAL_INSTALL_PREFIX": str(install_prefix),
        "CATHEDRAL_VALIDATOR_ENV": str(etc_dir / "validator.env"),
        "CATHEDRAL_ETC_DIR": str(etc_dir),
        "CATHEDRAL_UPDATER_RUN_ONCE": "1",
        "CATHEDRAL_UPDATER_PIP_BIN": str(fake_pip),
        "CATHEDRAL_UPDATER_VALIDATOR_BIN": str(fake_validator),
        "CATHEDRAL_UPDATER_PM2_BIN": str(fake_pm2),
    }

    result = _run_unchecked([str(work / "bin" / "updater.sh")], cwd=work, env=env)

    assert result.returncode != 0
    assert "bad signature on v0.0.2" in result.stdout
    current_tag = _run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=work,
    ).stdout.strip()
    assert current_tag == "v0.0.1"
    assert not pip_log.exists()
    assert not validator_log.exists()
    assert not pm2_log.exists()


def test_updater_exits_after_successful_update_so_pm2_respawns_from_new_script(
    tmp_path: Path,
) -> None:
    """A live updater bug fix only takes effect if the running bash process
    exits after writing the new updater.sh to disk. Otherwise PM2 keeps
    executing the old loop and updater changes never reach production.

    This test runs the updater in daemon mode (no RUN_ONCE) with a long
    POLL_SECS, so the only way the script can exit is via the explicit
    post-restart `exit 0`. A bounded subprocess timeout ensures the test
    fails loudly if the self-exit ever regresses.
    """
    work, _allowed_key, allowed_signer = _create_signed_repo(tmp_path)
    install_prefix = tmp_path / "install"
    etc_dir = tmp_path / "etc" / "cathedral"
    bin_dir = tmp_path / "bin"
    install_prefix.mkdir()
    etc_dir.mkdir(parents=True)
    bin_dir.mkdir()
    (install_prefix / "allowed_signers").write_text(allowed_signer)

    pip_log = tmp_path / "pip.log"
    validator_log = tmp_path / "validator.log"
    pm2_log = tmp_path / "pm2.log"
    _write_executable(bin_dir / "pip", pip_log)
    _write_executable(bin_dir / "cathedral-validator", validator_log)
    _write_executable(bin_dir / "pm2", pm2_log)

    env = {
        **os.environ,
        "CATHEDRAL_UPDATER_REPO_DIR": str(work),
        "CATHEDRAL_INSTALL_PREFIX": str(install_prefix),
        "CATHEDRAL_VALIDATOR_ENV": str(etc_dir / "validator.env"),
        "CATHEDRAL_ETC_DIR": str(etc_dir),
        # No CATHEDRAL_UPDATER_RUN_ONCE: this is daemon mode.
        "CATHEDRAL_UPDATER_POLL_SECS": "3600",
        "CATHEDRAL_UPDATER_PIP_BIN": str(bin_dir / "pip"),
        "CATHEDRAL_UPDATER_VALIDATOR_BIN": str(bin_dir / "cathedral-validator"),
        "CATHEDRAL_UPDATER_PM2_BIN": str(bin_dir / "pm2"),
    }

    result = subprocess.run(
        [str(work / "bin" / "updater.sh")],
        cwd=work,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "exiting to let PM2 respawn from new on-disk script" in result.stdout
    current_tag = _run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        cwd=work,
    ).stdout.strip()
    assert current_tag == "v0.0.2"
    expected_pm2 = (
        f" startOrReload {install_prefix / 'ecosystem.config.cjs'} "
        "--only cathedral-validator --update-env"
    )
    assert expected_pm2 in pm2_log.read_text()
