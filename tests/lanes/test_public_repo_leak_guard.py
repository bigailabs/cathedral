from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-byo",
    ".venv-fix",
    ".venv-int",
    ".venv-local",
    ".venv-prefix",
    "__pycache__",
    "build",
    "data",
    "dist",
    "htmlcov",
    "node_modules",
    "secrets",
    "venv",
}

_TEXT_SUFFIXES = {
    ".c",
    ".cfg",
    ".cnf",
    ".cpp",
    ".css",
    ".csv",
    ".dimacs",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sol",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

_MAX_PUBLIC_CNF_BYTES = 10_000


def _private_markers() -> tuple[str, ...]:
    # Build strings from pieces so this guard does not trip on itself.
    return (
        "sro" + "gatch",
        "fred-" + "bsat",
        "rse" + "rge",
        "uf20-" + "01",
        "uf50-" + "01000",
        "uf250-" + "0100",
        "sha" + "1.cnf",
    )


def _private_formula_names() -> tuple[str, ...]:
    return (
        "1_" + "uf20-" + "01.cnf",
        "2_" + "uf50-" + "01000.cnf",
        "3_" + "uf250-" + "0100.cnf",
        "4_" + "f" + "600.cnf",
        "5_" + "f" + "1000.cnf",
        "6_" + "f" + "2000.cnf",
        "7_" + "sha" + "1.cnf",
    )


def _walk_repo_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def test_private_serge_artifact_names_are_not_committed() -> None:
    offenders: list[str] = []
    private_names = set(_private_formula_names())
    for path in _walk_repo_files():
        if path.name in private_names:
            offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, f"private SAT artifact filenames committed: {offenders}"


def test_no_large_public_sat_formula_or_solution_artifacts() -> None:
    offenders: list[str] = []
    for path in _walk_repo_files():
        if path.suffix.lower() not in {".cnf", ".dimacs", ".sol"}:
            continue
        if path.stat().st_size > _MAX_PUBLIC_CNF_BYTES:
            offenders.append(f"{path.relative_to(ROOT)} ({path.stat().st_size} bytes)")

    assert not offenders, (
        "large SAT formula or solution artifacts must stay private, not in "
        f"cathedralai/cathedral: {offenders}"
    )


def test_private_serge_markers_are_not_committed_in_text_files() -> None:
    offenders: list[str] = []
    markers = _private_markers()
    for path in _walk_repo_files():
        if path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.lower()
        hits = [marker for marker in markers if marker.lower() in lowered]
        if hits:
            offenders.append(f"{path.relative_to(ROOT)}: {hits}")

    assert not offenders, f"private SAT corpus or generator markers committed: {offenders}"
