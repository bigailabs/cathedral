"""Packaging checks for the local game and arena entrypoints."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

from setuptools import find_packages
from game.arena import corpus


ROOT = Path(__file__).resolve().parents[3]


def test_pyproject_packages_game_modules():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_config = pyproject["tool"]["setuptools"]["packages"]["find"]
    packages = set(find_packages(where=str(ROOT), **package_config))

    assert "game" in packages
    assert "game.arena" in packages
    assert "scaffold" in packages
    assert "game.tests" not in packages
    assert "game.arena.tests" not in packages


def test_console_scripts_are_declared_and_importable():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = pyproject["project"]["scripts"]

    assert "cathedral-validator" not in scripts
    assert "cathedral-thin-validator" not in scripts

    expected = {
        "cathedral-game": "game.__main__:main",
        "cathedral-arena": "game.arena.__main__:main",
        "cathedral-arena-audit": "game.arena.audit:main",
        "cathedral-arena-serve": "game.arena.serve:main",
        "cathedral-arena-verify": "game.arena.bundle:main",
        "cathedral-arena-playthrough": "game.arena.playthrough:main",
        "cathedral-arena-round-verify": "game.arena.verify:main",
    }
    for name, target in expected.items():
        assert scripts[name] == target
        module_name, attr = target.split(":")
        assert callable(getattr(importlib.import_module(module_name), attr))


def test_installed_package_has_fallback_targets(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, "_AUDIT_HUNTER", tmp_path / "missing-audit-hunter")

    targets = corpus.load_targets()
    summary = corpus.corpus_summary()

    assert len(targets) >= 10
    assert summary["targets"] == len(targets)
    assert summary["proof_tasks"] >= 1
    assert summary["source"] == "bundled-fallback"
