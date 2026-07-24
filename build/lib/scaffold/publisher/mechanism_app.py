"""Standalone Mechanism Router service.

Cathedral's `scored → weights` stage as its own FastAPI app, so it can run on
an owned/attestable box (off Railway) alongside the weight-setter — the same
standalone pattern as `v2_lean_ingress.py`.

It mounts three routers over one SQLite-backed MechanismStore:
  - signed-score intake   POST /mechanisms/{id}/scores        (mechanism_intake)
  - allocation admin      PUT  /mechanisms/{id}               (mechanism_router)
  - composed weights read GET  /mechanisms/weights/next       (mechanism_weightset)

Everything is default-OFF and testnet-only:
  - intake 404s unless CATHEDRAL_MECHANISM_INTAKE_ENABLED is set
  - admin upsert requires CATHEDRAL_PUBLISHER_ADMIN_TOKEN
  - the weight-set stage refuses finney/mainnet and dry-runs by default
Nothing here can affect real V1 rewards or on-chain mainnet weights.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from .auth import default_verifier
from . import mechanism_intake
from .mechanism_router import SqliteMechanismStore, create_router
from .mechanism_weightset import build_router as build_weightset_router


def build_mechanism_app(*, store: SqliteMechanismStore | None = None) -> FastAPI:
    app = FastAPI(title="Cathedral Mechanism Router", version="0.1.0")
    store = store or SqliteMechanismStore(os.environ.get("CATHEDRAL_MECH_DB_PATH") or None)
    app.state.store = store

    # wire the injected dependencies the intake router expects
    mechanism_intake.set_mechanism_store(store)
    mechanism_intake.set_hotkey_verifier(default_verifier())

    app.include_router(mechanism_intake.router)      # POST /mechanisms/{id}/scores
    app.include_router(create_router(store))         # PUT  /mechanisms/{id}  (admin)
    app.include_router(build_weightset_router())     # GET  /mechanisms/weights/next

    @app.get("/health/live")
    def health_live():
        return {
            "status": "ok",
            "kind": "live",
            "service_role": "mechanism-router",
            "db": "sqlite",
            "intake_enabled": bool(os.environ.get(mechanism_intake.INTAKE_ENABLED_ENV, "")),
            "admin_token_set": bool(os.environ.get("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "")),
        }

    return app


app = build_mechanism_app()
