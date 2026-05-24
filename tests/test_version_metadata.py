from __future__ import annotations

import re
import tomllib
from pathlib import Path

import cathedral

ROOT = Path(__file__).parents[1]
_RELEASE_VERSION_HEADING_RE = re.compile(r"^#{2,3}\s+v(?P<version>\d+\.\d+\.\d+)\b")


def _latest_release_version(releases_text: str) -> str:
    for line in releases_text.splitlines():
        match = _RELEASE_VERSION_HEADING_RE.match(line)
        if match is not None:
            return match.group("version")
    raise AssertionError("RELEASES.md does not contain a vX.Y.Z release heading")


def test_pyproject_version_matches_runtime_version() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == cathedral.__version__


def test_package_version_matches_latest_release_notes() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    latest_release = _latest_release_version((ROOT / "RELEASES.md").read_text())
    assert pyproject["project"]["version"] == latest_release


def test_latest_release_parser_accepts_current_release_subheading() -> None:
    releases = """# Releases

## Current Release

### v1.2.3 - Current

## Older Releases

### v1.2.2 - Previous
"""
    assert _latest_release_version(releases) == "1.2.3"
