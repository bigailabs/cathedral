"""Packaging guard: every console script declared in pyproject.toml must resolve to
an importable, callable `main`. ARENA.md documents these commands ("cathedral-arena…
after editable install"); this test ensures they actually work after install — a
rename or a removed main() breaks the command for users, so it must break a test.
"""
from __future__ import annotations

import importlib
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _scripts() -> dict[str, str]:
    if tomllib is None or not _PYPROJECT.exists():
        return {}
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data.get("project", {}).get("scripts", {})


def test_every_console_script_resolves_to_a_callable():
    scripts = _scripts()
    if not scripts:
        return                                          # no pyproject / tomllib — skip
    failures = []
    for name, target in scripts.items():
        module_name, sep, attr = target.partition(":")
        if not sep:
            failures.append(f"{name}: '{target}' is not 'module:attr'")
            continue
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, attr)
        except (ImportError, AttributeError) as e:
            failures.append(f"{name} -> {target}: {type(e).__name__}: {e}")
            continue
        if not callable(fn):
            failures.append(f"{name} -> {target}: not callable")
    assert not failures, "broken console scripts:\n" + "\n".join(failures)


def test_the_documented_arena_commands_are_declared():
    """The arena commands ARENA.md tells users to run are real entry points."""
    scripts = _scripts()
    if not scripts:
        return
    for expected in ("cathedral-arena", "cathedral-arena-round-verify",
                     "cathedral-arena-audit"):
        assert expected in scripts, f"documented command not packaged: {expected}"
    # the full-round verifier maps to the unified verify module (not the bundle-only one)
    assert scripts["cathedral-arena-round-verify"] == "game.arena.verify:main"
