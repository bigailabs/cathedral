"""Publisher app startup wiring.

Pins behaviour of ``_materialize_ssh_probe_key`` — the startup hook that
writes ``CATHEDRAL_PROBE_SSH_PRIVATE_KEY`` from env to disk so the
``attestation_mode=ssh-probe`` runners can construct.

Background: ``SshHermesRunner`` / ``SshProbeRunner`` raise at __init__
if their ``ssh_private_key_path`` doesn't exist on disk. The Railway
publisher container has no key baked in. Without this hook every
ssh-probe submission silently fails at runner construction and hangs
in ``evaluating`` forever.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import cathedral.publisher.app as publisher_app
from cathedral.publisher.app import _materialize_ssh_probe_key

# A valid-looking OpenSSH private key block. Content doesn't have to be a
# real Ed25519 key — we never call ssh with it here, only assert the
# bytes round-trip to disk.
_FAKE_PEM = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZWQy\n"
    "NTUxOQAAACBfakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeQ\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts from a clean slate — no inherited key env vars."""
    monkeypatch.delenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("CATHEDRAL_SSH_KEY_PATH", raising=False)


def test_unset_env_and_no_file_logs_warning_and_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var + no file on disk: warn, return cleanly, no crash."""
    target = tmp_path / "absent" / "cathedral_probe_ed25519"
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))

    # Must not raise.
    _materialize_ssh_probe_key()

    assert not target.exists(), (
        "with env unset we must not create an empty/placeholder file — "
        "ssh-probe stays disabled, other modes keep working"
    )


def test_unset_env_and_no_path_preserves_runner_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No raw key means startup must not override the runner's ~/.ssh fallback."""
    target = tmp_path / "would_be_materialized"
    monkeypatch.setattr(publisher_app, "_DEFAULT_SSH_PROBE_KEY_PATH", str(target))

    _materialize_ssh_probe_key()

    assert "CATHEDRAL_SSH_KEY_PATH" not in os.environ
    assert not target.exists()


def test_env_set_and_file_absent_writes_file_with_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var present, target file absent: write with 0600 perms."""
    target = tmp_path / "subdir" / "cathedral_probe_ed25519"
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM)

    _materialize_ssh_probe_key()

    assert target.is_file(), "key file must be created"
    assert target.read_text() == _FAKE_PEM, "content must match env exactly"

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"perms must be 0600 for OpenSSH, got {oct(mode)}"


def test_env_set_creates_key_inode_with_0600_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first open must create the key with 0600, not chmod after writing."""
    target = tmp_path / "shared-dir" / "cathedral_probe_ed25519"
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM)

    real_open = publisher_app.os.open
    created_modes: list[int] = []

    def recording_open(path: os.PathLike[str] | str, flags: int, mode: int = 0o777) -> int:
        if flags & os.O_CREAT:
            created_modes.append(stat.S_IMODE(mode))
        return real_open(path, flags, mode)

    monkeypatch.setattr(publisher_app.os, "open", recording_open)

    _materialize_ssh_probe_key()

    assert created_modes
    assert created_modes[0] == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_env_set_without_path_exports_materialized_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The later ssh-probe runner reads CATHEDRAL_SSH_KEY_PATH."""
    target = tmp_path / "default_probe_key"
    monkeypatch.setattr(publisher_app, "_DEFAULT_SSH_PROBE_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM)

    _materialize_ssh_probe_key()

    assert target.is_file()
    assert target.read_text() == _FAKE_PEM
    assert os.environ["CATHEDRAL_SSH_KEY_PATH"] == str(target)


def test_env_set_and_file_matches_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File already on disk with matching content: short-circuit, no rewrite."""
    target = tmp_path / "cathedral_probe_ed25519"
    target.write_text(_FAKE_PEM)
    target.chmod(0o600)
    original_mtime_ns = target.stat().st_mtime_ns

    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM)

    _materialize_ssh_probe_key()

    assert target.read_text() == _FAKE_PEM
    assert target.stat().st_mtime_ns == original_mtime_ns, (
        "idempotent path must not rewrite (mtime would change)"
    )


def test_env_set_and_file_matches_tightens_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching content still needs 0600; otherwise the key remains exposed."""
    target = tmp_path / "cathedral_probe_ed25519"
    target.write_text(_FAKE_PEM)
    target.chmod(0o644)

    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM)

    _materialize_ssh_probe_key()

    assert target.read_text() == _FAKE_PEM
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_env_missing_trailing_newline_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""Some env transports strip trailing \n; OpenSSH refuses keys without one."""
    target = tmp_path / "cathedral_probe_ed25519"
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv(
        "CATHEDRAL_PROBE_SSH_PRIVATE_KEY", _FAKE_PEM.rstrip("\n")
    )

    _materialize_ssh_probe_key()

    written = target.read_text()
    assert written.endswith("\n"), "trailing newline must be added back"
    assert written == _FAKE_PEM


def test_env_set_and_file_differs_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator rotated the key: env wins, file gets overwritten."""
    target = tmp_path / "cathedral_probe_ed25519"
    target.write_text("old-stale-key-from-previous-deploy\n")
    target.chmod(0o644)  # also wrong perms — fix should reset to 0600

    rotated = _FAKE_PEM.replace("Fake", "Real")
    monkeypatch.setenv("CATHEDRAL_SSH_KEY_PATH", str(target))
    monkeypatch.setenv("CATHEDRAL_PROBE_SSH_PRIVATE_KEY", rotated)

    _materialize_ssh_probe_key()

    assert target.read_text() == rotated, "rotated key must replace stale one"
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, f"perms must be tightened to 0600, got {oct(mode)}"
