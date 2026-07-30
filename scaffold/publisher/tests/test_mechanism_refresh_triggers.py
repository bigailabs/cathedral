"""Tests for the three refresh cadence triggers: the admin endpoint, the opt-in
periodic loop, and the CLI. Each just drives ``refresh_artifact_scores``; none
composes or writes weights.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from scaffold.publisher import mechanism_artifact_refresh as arf
from scaffold.publisher import mechanism_router as R
from scaffold.publisher import refresh_cli


def _client(store):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(R.create_router(store))
    return TestClient(app)


def _artifact_spec(mid="cybergym_v0"):
    return R.MechanismSpec(mid, "5owner", 0.5, "artifact", owner_uid=None)


def _fake_adapter(vec):
    return lambda store, **kw: (dict(vec), R.ScoreVectorMeta(
        mechanism_id="m", signed_at_ms=1, sig_ok=True, source="test"))


# --------------------------------------------------------------------------- #
# 1. admin endpoint  POST /mechanisms/refresh
# --------------------------------------------------------------------------- #
def test_refresh_endpoint_requires_admin_token(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", raising=False)
    assert _client(R.SqliteMechanismStore(":memory:")).post("/mechanisms/refresh").status_code == 503


def test_refresh_endpoint_rejects_a_bad_token(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "secret")
    resp = _client(R.SqliteMechanismStore(":memory:")).post(
        "/mechanisms/refresh", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_refresh_endpoint_runs_the_adapters_with_a_valid_token(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", "secret")
    monkeypatch.setitem(arf.ARTIFACT_ADAPTERS, "cybergym_v0", _fake_adapter({7: 3.0, 9: 1.0}))
    store = R.SqliteMechanismStore(":memory:")
    store.upsert_spec(_artifact_spec())
    resp = _client(store).post("/mechanisms/refresh", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "refreshed": {"cybergym_v0": 2}}
    assert store.get_scores("cybergym_v0")[0] == {7: 3.0, 9: 1.0}   # actually persisted


# --------------------------------------------------------------------------- #
# 2. opt-in periodic loop
# --------------------------------------------------------------------------- #
def test_refresh_enabled_is_off_by_default_on_by_env(monkeypatch):
    monkeypatch.delenv(arf.REFRESH_INTERVAL_ENV, raising=False)
    assert arf.refresh_enabled() is False
    monkeypatch.setenv(arf.REFRESH_INTERVAL_ENV, "30")
    assert arf.refresh_enabled() is True
    monkeypatch.setenv(arf.REFRESH_INTERVAL_ENV, "0")   # zero = off
    assert arf.refresh_enabled() is False


def test_refresh_loop_ticks_then_stops_cleanly(monkeypatch):
    calls = []
    monkeypatch.setattr(arf, "refresh_artifact_scores",
                        lambda store, **kw: (calls.append(1), {"m": 1})[1])

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(
            arf.refresh_loop(object(), interval_seconds=0.01, stop_event=stop))
        await asyncio.sleep(0.05)          # let it tick a few times
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run())
    assert len(calls) >= 1                 # ran, and exited when signalled


# --------------------------------------------------------------------------- #
# 3. CLI
# --------------------------------------------------------------------------- #
def test_cli_refresh_prints_result(capsys):
    rc = refresh_cli.main(["--db", ":memory:"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out == {"refreshed": {}}   # empty store -> nothing to refresh


def test_cli_publish_requires_target_flags():
    with pytest.raises(SystemExit):               # --publish without netuid/network/key
        refresh_cli.main(["--db", ":memory:", "--publish"])
