"""ARENA.md's documented `python -m game.arena.X` commands must be real.

Doc-accuracy guard: every module the handoff tells an operator to run must be
importable (and the ones invoked as a program expose a callable `main`). This
keeps the "Run it" section honest as modules are added/renamed.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

ARENA_MD = Path(__file__).resolve().parents[1] / "ARENA.md"


def _documented_modules() -> set[str]:
    text = ARENA_MD.read_text(encoding="utf-8")
    # match `python -m game.arena.<module>` (module is a dotted path, no args)
    return set(re.findall(r"python -m (game\.arena(?:\.[a-z_]+)?)", text))


def test_documented_run_commands_are_importable():
    mods = _documented_modules()
    assert "game.arena.selfcheck" in mods                 # the new operator health check
    assert "game.arena.verify" in mods
    bad = []
    for m in sorted(mods):
        try:
            importlib.import_module(m)
        except Exception as exc:                          # noqa: BLE001
            bad.append(f"{m}: {type(exc).__name__}")
    assert not bad, f"ARENA.md documents un-importable modules: {bad}"


def test_runnable_modules_expose_main():
    # modules ARENA.md invokes as programs should be callable as `python -m`.
    for m in ("game.arena.selfcheck", "game.arena.proofboard", "game.arena.frontpage",
              "game.arena.verify"):
        mod = importlib.import_module(m)
        assert callable(getattr(mod, "main", None)), f"{m} has no callable main()"
