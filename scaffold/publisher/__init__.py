"""Cathedral v4 thin publisher — the ~1.5k-line service that replaces the
46.5k-line monolith publisher with zero validator updates.

The frozen wire surface (COMPAT.md) is implemented via scaffold.wire; the
miner-facing Lane A surface and additive Lane S/I endpoints live in app.py.
"""
# Lazy re-export (PEP 562): importing a submodule — e.g. scaffold.publisher.weights
# for the shared verify surface — must NOT pull in the FastAPI app, so a
# validator-only install without the server stack still imports cleanly.
# `from scaffold.publisher import build_app` resolves on demand.
def __getattr__(name):
    if name in ("build_app", "seed_challenge", "seed_audit_challenge"):
        from . import app
        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
