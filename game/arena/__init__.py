"""Cathedral Arena package.

Importing scanner/replay helpers must stay side-effect free for publisher use.
Arena entrypoints import :mod:`game.config` explicitly when they need defaults.
"""
from __future__ import annotations

__all__: list[str] = []
