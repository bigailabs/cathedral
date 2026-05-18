"""Arena sandbox: scrambler + miner-facing tool API.

This module is the *miner side* of the v4 loop. The validator never
runs this against untrusted miner code; it stages the scrambled
workspace and exposes exactly three atomic operations:

  1. ``read_file(path)`` -> the file's text content
  2. ``write_patch(diff_string)`` -> True if the unified diff applied
  3. ``run_local_compile()`` -> a dict with stdout/stderr/returncode

The ``IsomorphicScrambler`` mutates a base repository from
``vault/<name>/`` into a structurally-equivalent variant with
deterministically-renamed identifiers, file paths, and string
constants. This defeats miner gaming via vault lookup: every
challenge presents fresh symbol names while preserving the same fault
geometry that the hidden test exercises.

Scrambling algorithm (per seed):

  * **Filenames**: each non-canonical module/file basename gets a
    suffix derived from the seed. Canonical entry points (e.g.
    ``main.py``, ``pyproject.toml``, ``package.json``,
    ``schema.prisma``) are preserved so the language toolchain still
    works.
  * **Identifiers**: a fixed allowlist of internal symbol names
    declared in the vault's ``scramble.json`` manifest is renamed to
    ``<original>_s<hex>``. References inside files are updated in
    lockstep using whole-word regex.
  * **String constants**: a manifest-supplied list of safe string
    literals is rotated to a per-seed variant.

Determinism: every transform reads its randomness from
``random.Random(seed)``, so the same (base_repo, seed) pair always
produces byte-identical output. The validator records the seed in the
``ValidationPayload`` so a third party can reproduce the workspace.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class ArenaError(Exception):
    """Raised when the arena cannot satisfy a tool call (bad path,
    failed patch application, missing compile entry).
    """


# ---------------------------------------------------------------------------
# scrambler
# ---------------------------------------------------------------------------


# Reserved entry-point filenames the scrambler must NOT rename -- the
# language toolchain (pytest, bun, tsc, pip) discovers them by exact
# name and breaks if they move.
_RESERVED_BASENAMES: frozenset[str] = frozenset(
    {
        "main.py",
        "app.py",
        "conftest.py",
        "__init__.py",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "requirements.txt",
        "package.json",
        "tsconfig.json",
        "schema.prisma",
        "bun.lockb",
        "scramble.json",
        ".gitignore",
    }
)


@dataclass(frozen=True)
class ScrambledRepo:
    """One materialized scrambled repository on disk.

    ``files`` is the in-memory mirror of every text file under
    ``workspace_path`` keyed by repo-relative POSIX path. ``rename_map``
    is the identifier -> scrambled-identifier map; the validator
    stores it so the hidden test can be rewritten to match.
    """

    workspace_path: Path
    seed: int
    files: dict[str, str] = field(default_factory=dict)
    rename_map: dict[str, str] = field(default_factory=dict)
    file_rename_map: dict[str, str] = field(default_factory=dict)
    string_rotation: dict[str, str] = field(default_factory=dict)
    compile_command: list[str] = field(default_factory=list)
    test_entry_path: str = ""
    language: str = ""


class IsomorphicScrambler:
    """Load a vault base repo and emit a scrambled ``ScrambledRepo``.

    The vault repo must contain a ``scramble.json`` manifest declaring
    which identifiers and string literals are safe to mutate, plus the
    ``compile_command`` and ``test_entry`` the oracle will invoke.
    """

    def __init__(self, vault_path: Path) -> None:
        self.vault_path = Path(vault_path)
        if not self.vault_path.exists():
            raise ArenaError(f"vault path does not exist: {self.vault_path}")

    def load_manifest(self, base_repo: str) -> dict[str, Any]:
        repo_dir = self.vault_path / base_repo
        manifest_path = repo_dir / "scramble.json"
        if not manifest_path.is_file():
            raise ArenaError(f"missing scramble.json in {repo_dir}")
        data: dict[str, Any] = json.loads(manifest_path.read_text())
        return data

    def scramble(
        self,
        base_repo: str,
        seed: int,
        workspace_root: Path,
    ) -> ScrambledRepo:
        """Materialize a scrambled copy of ``base_repo`` under
        ``workspace_root``.

        The workspace is created fresh (any pre-existing directory at
        ``workspace_root / base_repo`` is wiped first) and is fully
        self-contained: no symlinks back into the vault.
        """
        repo_dir = self.vault_path / base_repo
        if not repo_dir.is_dir():
            raise ArenaError(f"unknown base repo: {base_repo}")

        manifest = self.load_manifest(base_repo)
        rng = random.Random(seed)  # noqa: S311 -- deterministic scrambler, not crypto
        suffix = f"_s{rng.randrange(0x10_0000, 0xFF_FFFF):06x}"

        renamable_ids: list[str] = list(manifest.get("renamable_identifiers", []))
        renamable_files: list[str] = list(manifest.get("renamable_files", []))
        string_rotations: dict[str, list[str]] = dict(manifest.get("string_rotations", {}))
        compile_command: list[str] = list(manifest.get("compile_command", []))
        test_entry: str = str(manifest.get("test_entry", ""))
        language: str = str(manifest.get("language", ""))

        rename_map: dict[str, str] = {ident: f"{ident}{suffix}" for ident in renamable_ids}
        file_rename_map: dict[str, str] = {}
        for relpath in renamable_files:
            p = Path(relpath)
            if p.name in _RESERVED_BASENAMES:
                continue
            new_stem = f"{p.stem}{suffix}"
            file_rename_map[relpath] = str(p.with_name(new_stem + p.suffix))

        string_rotation_pick: dict[str, str] = {}
        for original, candidates in string_rotations.items():
            if not candidates:
                continue
            string_rotation_pick[original] = candidates[rng.randrange(len(candidates))]

        target_dir = workspace_root / base_repo
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True)

        files_out: dict[str, str] = {}
        for src_path in sorted(_iter_repo_files(repo_dir)):
            rel = src_path.relative_to(repo_dir).as_posix()
            if rel == "scramble.json":
                # never ship the manifest into the miner workspace
                continue
            try:
                text = src_path.read_text()
                is_text = True
            except UnicodeDecodeError:
                is_text = False
                text = ""

            new_rel = file_rename_map.get(rel, rel)
            dest = target_dir / new_rel
            dest.parent.mkdir(parents=True, exist_ok=True)

            if not is_text:
                shutil.copyfile(src_path, dest)
                continue

            new_text = _apply_text_transforms(
                text=text,
                rename_map=rename_map,
                string_rotation=string_rotation_pick,
            )
            dest.write_text(new_text)
            files_out[new_rel] = new_text

        # Compile command / test entry get the same transforms so the
        # paths in them match the renamed files on disk.
        compile_command_scrambled: list[str] = [
            _apply_path_renames(token, file_rename_map) for token in compile_command
        ]
        test_entry_scrambled = file_rename_map.get(test_entry, test_entry)

        return ScrambledRepo(
            workspace_path=target_dir,
            seed=seed,
            files=files_out,
            rename_map=dict(rename_map),
            file_rename_map=dict(file_rename_map),
            string_rotation=dict(string_rotation_pick),
            compile_command=compile_command_scrambled,
            test_entry_path=test_entry_scrambled,
            language=language,
        )


def _iter_repo_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # skip common junk that may have crept in
        if any(part in {"__pycache__", ".pytest_cache", "node_modules"} for part in p.parts):
            continue
        out.append(p)
    return out


def _apply_text_transforms(
    text: str,
    rename_map: dict[str, str],
    string_rotation: dict[str, str],
) -> str:
    out = text
    for original, replacement in sorted(rename_map.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(original)}\b", replacement, out)
    for original, replacement in sorted(string_rotation.items(), key=lambda kv: -len(kv[0])):
        out = out.replace(original, replacement)
    return out


def _apply_path_renames(token: str, file_rename_map: dict[str, str]) -> str:
    for original, replacement in sorted(file_rename_map.items(), key=lambda kv: -len(kv[0])):
        if original in token:
            token = token.replace(original, replacement)
    return token


# ---------------------------------------------------------------------------
# miner-facing arena: exactly three atomic tool ops
# ---------------------------------------------------------------------------


class MinerArena:
    """Miner-facing tool surface backed by a ``ScrambledRepo``.

    The class exposes exactly three public methods: ``read_file``,
    ``write_patch``, ``run_local_compile``. Any other public method is
    a bug; the test suite asserts the surface stays exactly these
    three.

    The arena does NOT execute any code from the miner. It only
    applies text patches to its in-memory copy of the workspace and,
    on ``run_local_compile``, invokes the validator-chosen compile
    command that came from the vault manifest.
    """

    TOOL_NAMES: tuple[str, ...] = ("read_file", "write_patch", "run_local_compile")

    def __init__(self, repo: ScrambledRepo) -> None:
        self._repo = repo
        # mutable in-memory mirror; reset on demand by the engine
        self._files: dict[str, str] = dict(repo.files)

    # -- Tool #1 -----------------------------------------------------------
    def read_file(self, path: str) -> str:
        """Return the current text content of ``path``.

        Raises ``ArenaError`` if the path is outside the workspace or
        does not exist. Paths are repo-relative POSIX strings.
        """
        normalized = _normalize_relpath(path)
        if normalized not in self._files:
            raise ArenaError(f"no such file in arena: {path!r}")
        return self._files[normalized]

    # -- Tool #2 -----------------------------------------------------------
    def write_patch(self, diff_string: str) -> bool:
        """Apply a unified-diff patch in memory.

        Returns ``True`` if every hunk applied cleanly. Returns
        ``False`` (and leaves the arena state unchanged) if any hunk
        failed to match.

        The patcher accepts the minimal-but-real subset of unified
        diff used by GNU patch: ``--- a/<path>``, ``+++ b/<path>``,
        ``@@ -<old>,<n> +<new>,<m> @@`` hunk headers, and ``+``/``-``/
        `` `` lines. Whole-file additions and deletions are supported
        via ``--- /dev/null`` and ``+++ /dev/null``.
        """
        try:
            new_files = _apply_unified_diff(self._files, diff_string)
        except _DiffError as e:
            logger.info("arena.patch_failed", reason=str(e))
            return False
        self._files = new_files
        return True

    # -- Tool #3 -----------------------------------------------------------
    def run_local_compile(self) -> dict[str, Any]:
        """Flush the in-memory workspace to disk and invoke the
        compile/test command declared in the vault manifest.

        Returns ``{"returncode": int, "stdout": str, "stderr": str,
        "duration_ms": float}``. This is the miner's *training-time*
        signal -- the validator's hermetic oracle is in
        ``cathedral.v4.oracle.patch_runner`` and is a separate path.
        """
        import subprocess
        import time

        if not self._repo.compile_command:
            raise ArenaError("vault manifest has no compile_command")

        self._flush_to_disk()
        start = time.monotonic()
        cmd = list(self._repo.compile_command)
        # Resolve a bare "python" or "python3" to sys.executable so the
        # arena works on hosts that only ship python3.X without the
        # generic shim.
        if cmd and cmd[0] in {"python", "python3"}:
            cmd[0] = sys.executable
        try:
            proc = subprocess.run(  # noqa: S603 -- argv list, no shell
                cmd,
                cwd=self._repo.workspace_path,
                capture_output=True,
                text=True,
                timeout=5.0,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "returncode": -1,
                "stdout": "",
                "stderr": "compile timeout",
                "duration_ms": (time.monotonic() - start) * 1000.0,
            }
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "duration_ms": (time.monotonic() - start) * 1000.0,
        }

    # -- internal ----------------------------------------------------------
    def snapshot(self) -> dict[str, str]:
        """Return a copy of the current in-memory workspace state."""
        return dict(self._files)

    def _flush_to_disk(self) -> None:
        root = self._repo.workspace_path
        for relpath, content in self._files.items():
            dest = root / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)


# ---------------------------------------------------------------------------
# unified diff applier
# ---------------------------------------------------------------------------


class _DiffError(Exception):
    pass


_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@"
)


def _normalize_relpath(path: str) -> str:
    p = Path(path.replace("\\", "/"))
    if p.is_absolute():
        raise ArenaError(f"absolute paths not allowed in arena: {path!r}")
    parts = [part for part in p.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ArenaError(f"path traversal not allowed in arena: {path!r}")
    return "/".join(parts)


def _strip_diff_prefix(path: str) -> str:
    if path in ("/dev/null",):
        return path
    # strip a/ or b/ prefix that unified diff conventionally adds
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _apply_unified_diff(files: dict[str, str], diff_string: str) -> dict[str, str]:
    """Apply a unified diff to a dict of {path: content}.

    Returns a new dict. Raises ``_DiffError`` if any hunk fails to
    apply.
    """
    if not diff_string.strip():
        raise _DiffError("empty diff")

    out = dict(files)
    lines = diff_string.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith("--- "):
            i += 1
            continue
        if i + 1 >= n or not lines[i + 1].startswith("+++ "):
            raise _DiffError(f"expected +++ after --- at line {i}")
        old_path = _strip_diff_prefix(line[4:].split("\t", 1)[0].strip())
        new_path = _strip_diff_prefix(lines[i + 1][4:].split("\t", 1)[0].strip())
        i += 2
        # gather hunks for this file
        hunks: list[tuple[int, int, int, int, list[str]]] = []
        while i < n and lines[i].startswith("@@"):
            m = _HUNK_HEADER.match(lines[i])
            if not m:
                raise _DiffError(f"malformed hunk header: {lines[i]!r}")
            old_start = int(m.group("old_start"))
            old_len = int(m.group("old_len") or "1")
            new_start = int(m.group("new_start"))
            new_len = int(m.group("new_len") or "1")
            i += 1
            body: list[str] = []
            while i < n and not lines[i].startswith(("--- ", "@@")):
                body.append(lines[i])
                i += 1
            hunks.append((old_start, old_len, new_start, new_len, body))

        out = _apply_hunks(out, old_path, new_path, hunks)
    return out


def _apply_hunks(
    files: dict[str, str],
    old_path: str,
    new_path: str,
    hunks: list[tuple[int, int, int, int, list[str]]],
) -> dict[str, str]:
    creating = old_path == "/dev/null"
    deleting = new_path == "/dev/null"
    if creating:
        # whole-file add: one hunk, all "+" lines
        if len(hunks) != 1:
            raise _DiffError("create must have exactly one hunk")
        _old_start, _old_len, _new_start, _new_len, body = hunks[0]
        added: list[str] = []
        for ln in body:
            if not ln:
                continue
            tag = ln[0]
            if tag == "+":
                added.append(ln[1:])
            elif tag in (" ", "-"):
                raise _DiffError("create hunk must be all '+' lines")
        new_files = dict(files)
        rel = _normalize_relpath(new_path)
        body_text = "\n".join(added)
        # Most generated patches end with a trailing newline; normalize.
        if not body_text.endswith("\n"):
            body_text += "\n"
        new_files[rel] = body_text
        return new_files

    rel = _normalize_relpath(old_path)
    if rel not in files:
        raise _DiffError(f"patch target file does not exist: {rel}")

    if deleting:
        new_files = dict(files)
        del new_files[rel]
        return new_files

    # mutate file in memory
    original_lines = files[rel].splitlines(keepends=True)
    # Strip keepends for hunk match; re-attach a trailing newline on join.
    src = files[rel].splitlines()
    cursor = 0
    out_lines: list[str] = []
    for old_start, _old_len, _new_start, _new_len, body in hunks:
        # copy unchanged region before this hunk
        target_index = old_start - 1
        if target_index < cursor:
            raise _DiffError(
                f"hunks out of order: hunk starts at {old_start} but cursor is {cursor + 1}"
            )
        out_lines.extend(src[cursor:target_index])
        cursor = target_index
        # apply hunk body
        for ln in body:
            if not ln:
                # blank in diff body == context blank line
                if cursor >= len(src) or src[cursor] != "":
                    raise _DiffError("context mismatch on blank line")
                out_lines.append("")
                cursor += 1
                continue
            tag = ln[0]
            payload = ln[1:]
            if tag == " ":
                if cursor >= len(src) or src[cursor] != payload:
                    raise _DiffError(
                        f"context mismatch at src line {cursor + 1}: "
                        f"have {src[cursor] if cursor < len(src) else '<EOF>'!r}, "
                        f"expected {payload!r}"
                    )
                out_lines.append(payload)
                cursor += 1
            elif tag == "-":
                if cursor >= len(src) or src[cursor] != payload:
                    raise _DiffError(
                        f"delete mismatch at src line {cursor + 1}: "
                        f"have {src[cursor] if cursor < len(src) else '<EOF>'!r}, "
                        f"expected {payload!r}"
                    )
                cursor += 1
            elif tag == "+":
                out_lines.append(payload)
            elif tag == "\\":
                # "\ No newline at end of file" -- ignore
                continue
            else:
                raise _DiffError(f"unknown diff line tag: {tag!r}")
    # append tail
    out_lines.extend(src[cursor:])
    new_text = "\n".join(out_lines)
    if original_lines and original_lines[-1].endswith("\n"):
        new_text += "\n"
    new_files = dict(files)
    new_files[_normalize_relpath(new_path)] = new_text
    return new_files


__all__ = [
    "ArenaError",
    "IsomorphicScrambler",
    "MinerArena",
    "ScrambledRepo",
]
