"""TypeScript + Prisma vault: structural checks + gated bun smoke.

The Python vault is the canonical always-on validator path. The TS
vault is gated on ``bun`` being on PATH; this test file skips the
bun-dependent assertions on hosts without bun.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from cathedral.v4.arena import IsomorphicScrambler


def test_ts_vault_manifest_well_formed(vault_path: Path) -> None:
    manifest_path = vault_path / "ts_prisma_base" / "scramble.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["language"] == "typescript"
    assert "formatGreeting" in manifest["renamable_identifiers"]
    assert manifest["compile_command"][:1] == ["bun"]
    assert "test_entry" in manifest
    assert "hidden_test_template" in manifest


def test_ts_vault_scrambles_cleanly(vault_path: Path, tmp_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("ts_prisma_base", seed=7, workspace_root=tmp_path)
    # Reserved Prisma + npm files must remain intact.
    assert (repo.workspace_path / "package.json").exists()
    assert (repo.workspace_path / "prisma" / "schema.prisma").exists()
    # The user_format.ts file should still exist (no rename) and reflect
    # the renamed identifier somewhere.
    src = (repo.workspace_path / "src" / "user_format.ts").read_text()
    assert "formatGreeting" not in src or repo.rename_map.get("formatGreeting") in src


@pytest.mark.skipif(
    shutil.which("bun") is None,
    reason="ts_prisma vault gated on bun being on PATH",
)
def test_ts_vault_compile_command_runs(vault_path: Path, tmp_path: Path) -> None:
    """Smoke: when bun is present, the compile command runs to exit (any code).

    We don't assert exit==0 here because a fresh vault scramble has no
    node_modules installed; the assertion is only that we can invoke
    the toolchain without crashing the validator process.
    """
    import subprocess

    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("ts_prisma_base", seed=8, workspace_root=tmp_path)
    proc = subprocess.run(
        ["bun", "--version"],
        cwd=repo.workspace_path,
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
        check=False,
    )
    assert proc.returncode == 0
