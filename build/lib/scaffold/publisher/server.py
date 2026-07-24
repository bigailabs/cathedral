"""ASGI entrypoint for the thin publisher (used by the deploy image).

`build_app()` reads CATHEDRAL_EVAL_SIGNING_KEY (production key) and the rest of
the config from the environment. Run with:

    uvicorn scaffold.publisher.server:app --host 0.0.0.0 --port $PORT

The DB path comes from CATHEDRAL_DB_PATH (default ./publisher.db). The refill
loop starts on app startup iff CATHEDRAL_REFILL_ENABLED is truthy (see refill.py).
"""
from __future__ import annotations

import os

from .app import build_app

app = build_app(database_path=os.environ.get("CATHEDRAL_DB_PATH", "publisher.db"))
