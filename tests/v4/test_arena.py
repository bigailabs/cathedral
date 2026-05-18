"""Arena tests: scrambler determinism + three-op tool surface."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from cathedral.v4.arena import ArenaError, IsomorphicScrambler, MinerArena


def test_tool_surface_is_exactly_three(tmp_path: Path, vault_path: Path) -> None:
    """The Arena exposes only read_file, write_patch, run_local_compile."""
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=42, workspace_root=tmp_path)
    arena = MinerArena(repo)

    # Walk every public name. The contract is: any public method on
    # MinerArena that isn't in TOOL_NAMES is either a helper that
    # shouldn't be public, or a regression.
    public_methods = {
        name
        for name, member in inspect.getmembers(arena, predicate=inspect.ismethod)
        if not name.startswith("_")
    }
    # snapshot is allowed (validator-side introspection, doesn't appear
    # to the miner) but is documented as internal.
    public_methods -= {"snapshot"}
    assert public_methods == set(MinerArena.TOOL_NAMES)


def test_read_file_returns_text(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=1, workspace_root=tmp_path)
    arena = MinerArena(repo)

    content = arena.read_file("app/calculator.py")
    assert "def " in content
    assert "factor" in content


def test_read_file_rejects_traversal(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=2, workspace_root=tmp_path)
    arena = MinerArena(repo)

    with pytest.raises(ArenaError):
        arena.read_file("../../../etc/passwd")
    with pytest.raises(ArenaError):
        arena.read_file("/etc/passwd")
    with pytest.raises(ArenaError):
        arena.read_file("no_such_file.py")


def test_write_patch_round_trip(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=3, workspace_root=tmp_path)
    arena = MinerArena(repo)

    original = arena.read_file("app/calculator.py")
    assert "return price + price * factor" in original

    # Build the diff against the actual line numbers post-scramble.
    lines = original.splitlines()
    fault_idx = next(i for i, ln in enumerate(lines) if ln == "    return price + price * factor")
    # one-line hunk
    diff = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        f"@@ -{fault_idx + 1},1 +{fault_idx + 1},1 @@\n"
        "-    return price + price * factor\n"
        "+    return price - price * factor\n"
    )
    ok = arena.write_patch(diff)
    assert ok is True
    patched = arena.read_file("app/calculator.py")
    assert "return price - price * factor" in patched
    assert "return price + price * factor" not in patched


def test_write_patch_bad_context_returns_false(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=4, workspace_root=tmp_path)
    arena = MinerArena(repo)

    diff = (
        "--- a/app/calculator.py\n"
        "+++ b/app/calculator.py\n"
        "@@ -1,3 +1,3 @@\n"
        " this line is not in the file at all\n"
        "-bogus\n"
        "+newer bogus\n"
    )
    ok = arena.write_patch(diff)
    assert ok is False
    # state preserved
    text = arena.read_file("app/calculator.py")
    assert "this line is not in the file" not in text


def test_run_local_compile_returns_dict(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=5, workspace_root=tmp_path)
    arena = MinerArena(repo)

    result = arena.run_local_compile()
    assert set(result.keys()) >= {"returncode", "stdout", "stderr", "duration_ms"}
    assert result["returncode"] == 0
    assert "ok" in result["stdout"]


def test_scrambler_is_deterministic(tmp_path: Path, vault_path: Path) -> None:
    """Same (base_repo, seed) -> byte-identical scrambled output."""
    scrambler = IsomorphicScrambler(vault_path)
    a = scrambler.scramble("python_fastapi_base", seed=12345, workspace_root=tmp_path / "a")
    b = scrambler.scramble("python_fastapi_base", seed=12345, workspace_root=tmp_path / "b")

    assert a.rename_map == b.rename_map
    assert a.file_rename_map == b.file_rename_map
    assert set(a.files.keys()) == set(b.files.keys())
    for k in a.files:
        assert a.files[k] == b.files[k], f"mismatch on {k}"


def test_scrambler_actually_scrambles(tmp_path: Path, vault_path: Path) -> None:
    """Different seeds produce different identifier mappings (high prob)."""
    scrambler = IsomorphicScrambler(vault_path)
    a = scrambler.scramble("python_fastapi_base", seed=1, workspace_root=tmp_path / "a")
    b = scrambler.scramble("python_fastapi_base", seed=2, workspace_root=tmp_path / "b")

    # At least one identifier should rename differently across seeds
    differences = [k for k in a.rename_map if a.rename_map[k] != b.rename_map.get(k)]
    assert differences, "two different seeds produced identical rename maps"

    # The scrambled file content should reflect the new identifier
    for ident, scrambled_name in a.rename_map.items():
        if scrambled_name != ident:
            assert any(scrambled_name in content for content in a.files.values()), (
                f"renamed identifier {ident} -> {scrambled_name} never appears in scrambled output"
            )
            break


def test_scrambler_preserves_reserved_filenames(tmp_path: Path, vault_path: Path) -> None:
    scrambler = IsomorphicScrambler(vault_path)
    repo = scrambler.scramble("python_fastapi_base", seed=999, workspace_root=tmp_path)
    # __init__.py and pyproject.toml must still exist at their canonical paths
    assert "app/__init__.py" in repo.files or (repo.workspace_path / "app" / "__init__.py").exists()
    assert (repo.workspace_path / "pyproject.toml").exists()
