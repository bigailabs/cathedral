from __future__ import annotations

import tomllib
from pathlib import Path

import cathedral

ROOT = Path(__file__).parents[1]


def test_pyproject_version_matches_runtime_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == cathedral.__version__


def test_package_version_matches_latest_release_notes() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    release_heading = next(
        line for line in (ROOT / "RELEASES.md").read_text().splitlines() if line.startswith("## v")
    )
    latest_release = release_heading.split()[1].removeprefix("v")
    assert pyproject["project"]["version"] == latest_release
