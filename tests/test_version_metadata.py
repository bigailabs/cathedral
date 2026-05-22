from __future__ import annotations

import tomllib
from pathlib import Path

import cathedral


def test_pyproject_version_matches_runtime_version() -> None:
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == cathedral.__version__
