"""Cathedral v4 thin publisher — the ~1.5k-line service that replaces the
46.5k-line monolith publisher with zero validator updates.

The frozen wire surface (COMPAT.md) is implemented via scaffold.wire; the
miner-facing Lane A surface and additive Lane S/I endpoints live in app.py.
"""
from .app import build_app, seed_challenge  # noqa: F401
