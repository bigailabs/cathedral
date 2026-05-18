"""Shared fixtures for v4 tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cathedral.v4.cathedral_engine import CathedralEngine

VAULT_PATH = Path(__file__).resolve().parents[2] / "src" / "cathedral" / "v4" / "vault"


@pytest.fixture
def vault_path() -> Path:
    return VAULT_PATH


@pytest.fixture
def engine(vault_path: Path) -> CathedralEngine:
    return CathedralEngine(vault_path=str(vault_path))


@pytest.fixture
def python_manifest() -> dict:
    text = (VAULT_PATH / "python_fastapi_base" / "scramble.json").read_text()
    return json.loads(text)
