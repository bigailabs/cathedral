"""V2 verify worker: lock robustness.

Prod incident this guards against (hit twice, fixed only by a human running
pg_terminate_backend by hand): the V2 verify worker acquires a Postgres
session-level advisory lock; when the worker thread crashes (e.g. a
statement timeout) or its container is replaced on deploy, the lock's PG
session lingers idle-holding the lock, and NO worker anywhere can acquire it
-- verification stops fleet-wide.

Covers:
  * migration 0044 creates v2_worker_heartbeat; Store.write_v2_worker_heartbeat
    upserts it and never raises.
  * Store.advisory_lock discards (rather than reuses) a connection that hit
    an error while holding the lock, so a poisoned session can never be
    silently handed back into the pool still holding the lock.
  * Store.steal_stale_advisory_lock: SQLite no-op; Postgres-path SQL targets
    the exact advisory key (classid/objid split + objsubid=1), the idle
    condition, and the stale/absent-heartbeat guard (via a fake pool/cursor
    -- no real Postgres needed); always best-effort (never raises).
  * The V2 verify singleton background loop: an exception acquiring the lock
    increments worker_restarts / sets last_worker_error and the loop
    continues (no unhandled exception, no dead task) and recovers; the
    heartbeat row is written and updated on tick.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scaffold.publisher.app import build_app
from scaffold.publisher.store import Store

SIGNING_KEY_HEX = "22" * 32


# ---------------------------------------------------------------------------
# Migration + heartbeat write path (SQLite -- real migration, real dialect-
# translated INSERT OR REPLACE path other code in this codebase already uses)
# ---------------------------------------------------------------------------

def _sqlite_store(tmp_path: Path) -> Store:
    return Store(str(tmp_path / "test.db"), prefer_env_database_url=False)


def test_migration_0044_creates_heartbeat_table(tmp_path):
    store = _sqlite_store(tmp_path)
    rows = store.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='v2_worker_heartbeat'"
    )
    assert len(rows) == 1


def test_write_v2_worker_heartbeat_upserts(tmp_path):
    store = _sqlite_store(tmp_path)
    store.write_v2_worker_heartbeat("cathedral:v2:verify", "worker-a", "2026-07-05T00:00:00.000Z")
    rows = store.query("SELECT key, worker_id, beat_at_iso FROM v2_worker_heartbeat")
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "worker-a"

    # Second beat replaces (upsert) -- does not accumulate rows per worker.
    store.write_v2_worker_heartbeat("cathedral:v2:verify", "worker-b", "2026-07-05T00:01:00.000Z")
    rows = store.query("SELECT key, worker_id, beat_at_iso FROM v2_worker_heartbeat")
    assert len(rows) == 1
    assert rows[0]["worker_id"] == "worker-b"
    assert rows[0]["beat_at_iso"] == "2026-07-05T00:01:00.000Z"


def test_write_v2_worker_heartbeat_never_raises(tmp_path, monkeypatch):
    store = _sqlite_store(tmp_path)

    def _boom(fn):
        raise RuntimeError("db is on fire")

    monkeypatch.setattr(store, "write", _boom)
    # Must not raise -- a heartbeat write failure can never stall verification.
    store.write_v2_worker_heartbeat("cathedral:v2:verify", "worker-a", "2026-07-05T00:00:00.000Z")


# ---------------------------------------------------------------------------
# steal_stale_advisory_lock: always a no-op on SQLite (single process -- a
# lock can never go stale across processes there).
# ---------------------------------------------------------------------------

def test_steal_stale_advisory_lock_is_noop_on_sqlite(tmp_path):
    store = _sqlite_store(tmp_path)
    assert store.steal_stale_advisory_lock("cathedral:v2:verify", idle_secs=1) == 0


# ---------------------------------------------------------------------------
# Postgres-path SQL shape, via a fake pool/cursor (no real Postgres needed).
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fetch_result):
        self.sql: str | None = None
        self.params: tuple | None = None
        self._fetch_result = fetch_result

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self._fetch_result

    def fetchone(self):
        return self._fetch_result[0] if self._fetch_result else None


class _FakeConn:
    def __init__(self, fetch_result=()):
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._fetch_result = fetch_result
        self.last_cursor: _FakeCursor | None = None

    def cursor(self):
        self.last_cursor = _FakeCursor(self._fetch_result)
        return self.last_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class _FakePool:
    def __init__(self, conn):
        self.conn = conn
        self.putconn_calls: list[dict] = []

    def getconn(self):
        return self.conn

    def putconn(self, conn, close=False):
        self.putconn_calls.append({"conn": conn, "close": close})


def _pg_store_with_pool(pool: _FakePool) -> Store:
    store = Store.__new__(Store)
    store.backend = "postgres"
    store._pool = pool
    return store


def test_steal_stale_advisory_lock_sql_targets_exact_key_and_conditions():
    conn = _FakeConn(fetch_result=[(True,)])
    pool = _FakePool(conn)
    store = _pg_store_with_pool(pool)

    name = "cathedral:v2:verify"
    terminated = store.steal_stale_advisory_lock(name, idle_secs=180)

    assert terminated == 1
    cur = conn.last_cursor
    sql = cur.sql
    classid, objid, idle_a, key_param, idle_b = cur.params

    expected_key = Store._advisory_lock_key(name)
    assert classid == expected_key >> 32
    assert objid == expected_key & 0xFFFFFFFF
    assert idle_a == 180
    assert idle_b == 180
    assert key_param == name

    # Exact-key + idle + stale-heartbeat guard conditions must all be present.
    assert "l.locktype = 'advisory'" in sql
    assert "l.classid = %s" in sql
    assert "l.objid = %s" in sql
    assert "l.objsubid = 1" in sql
    assert "a.pid <> pg_backend_pid()" in sql
    assert "a.state = 'idle'" in sql
    assert "a.state_change < now()" in sql
    assert "v2_worker_heartbeat" in sql
    assert "NOT EXISTS" in sql
    assert conn.committed is True
    assert pool.putconn_calls == [{"conn": conn, "close": False}]


def test_steal_stale_advisory_lock_returns_zero_when_no_backend_matches():
    conn = _FakeConn(fetch_result=[])
    pool = _FakePool(conn)
    store = _pg_store_with_pool(pool)
    assert store.steal_stale_advisory_lock("cathedral:v2:verify") == 0


def test_steal_stale_advisory_lock_never_raises_on_db_error():
    class _ExplodingConn(_FakeConn):
        def cursor(self):
            raise RuntimeError("connection is dead")

    pool = _FakePool(_ExplodingConn())
    store = _pg_store_with_pool(pool)
    # Best-effort: must swallow the error and report zero, never raise.
    assert store.steal_stale_advisory_lock("cathedral:v2:verify") == 0


# ---------------------------------------------------------------------------
# advisory_lock: discard (not reuse) a connection that errored while it may
# hold the lock -- the fix for the "poisoned session silently perpetuates a
# stuck lock" failure mode.
# ---------------------------------------------------------------------------

class _ConfigurableConn:
    """Like _FakeConn, but each cursor().execute() call can be told (by
    position) to raise instead of succeeding, so unlock-specifically can be
    made to fail without touching the earlier pg_try_advisory_lock call."""

    def __init__(self, execute_side_effects=None):
        self._side_effects = list(execute_side_effects or [])
        self._call_index = 0
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def cursor(self):
        outer = self

        class _Cur:
            def execute(_self, sql, params=()):
                idx = outer._call_index
                outer._call_index += 1
                if idx < len(outer._side_effects) and outer._side_effects[idx] is not None:
                    raise outer._side_effects[idx]
                _self.sql = sql
                _self.params = params

            def fetchone(_self):
                return (True,)

        return _Cur()

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


def test_advisory_lock_healthy_path_reuses_connection():
    conn = _ConfigurableConn()
    pool = _FakePool(conn)
    store = _pg_store_with_pool(pool)
    with store.advisory_lock("cathedral:v2:verify") as acquired:
        assert acquired is True
    assert pool.putconn_calls == [{"conn": conn, "close": False}]


def test_advisory_lock_discards_connection_when_body_raises():
    conn = _ConfigurableConn()
    pool = _FakePool(conn)
    store = _pg_store_with_pool(pool)
    with pytest.raises(RuntimeError):
        with store.advisory_lock("cathedral:v2:verify") as acquired:
            assert acquired is True
            raise RuntimeError("worker crashed mid-batch")
    assert pool.putconn_calls == [{"conn": conn, "close": True}]


def test_advisory_lock_discards_connection_when_unlock_fails():
    # side_effects[0] -> pg_try_advisory_lock (succeeds); side_effects[1] ->
    # pg_advisory_unlock (fails, e.g. broken/aborted session).
    conn = _ConfigurableConn(execute_side_effects=[None, RuntimeError("unlock failed")])
    pool = _FakePool(conn)
    store = _pg_store_with_pool(pool)
    with store.advisory_lock("cathedral:v2:verify") as acquired:
        assert acquired is True
    # The connection must be discarded, not returned to the pool -- reusing
    # it could silently carry the still-held lock back in for someone else.
    assert pool.putconn_calls == [{"conn": conn, "close": True}]


# ---------------------------------------------------------------------------
# End-to-end: the real V2 verify singleton background loop, driven through a
# live app. An exception acquiring the advisory lock must not kill the task;
# it must log to metrics and the loop must recover (re-acquire, keep ticking,
# heartbeat kept fresh).
# ---------------------------------------------------------------------------

def _build_worker_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_VERIFY_WORKER_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_VERIFY_INTERVAL_SECS", "0.05")
    monkeypatch.setenv("CATHEDRAL_V2_SINGLETON_RETRY_SECS", "1")
    monkeypatch.setenv("CATHEDRAL_V2_ERROR_BACKOFF_FLOOR_SECS", "0.05")
    monkeypatch.setenv("CATHEDRAL_V2_ERROR_BACKOFF_CAP_SECS", "0.2")
    monkeypatch.setenv("CATHEDRAL_V2_LOCK_STEAL_IDLE_SECS", "30")
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    # A separate V2 DB/store (distinct from the main publisher store) so
    # monkeypatching v2_store.advisory_lock below only affects the V2 verify
    # singleton loop -- not the main store's weights-refresh / refill / arena
    # singleton loops, which share Store.advisory_lock on the main store when
    # no V2 DB is configured.
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    db = str(tmp_path / "pub.sqlite")
    return build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)


def _poll_metrics(client, predicate, timeout=6.0, interval=0.05):
    deadline = time.time() + timeout
    metrics = {}
    while time.time() < deadline:
        metrics = client.get("/v2/verify/metrics").json()
        if predicate(metrics):
            return metrics
        time.sleep(interval)
    return metrics


def test_v2_verify_worker_crash_containment_restarts_and_recovers(tmp_path, monkeypatch):
    app = _build_worker_app(tmp_path, monkeypatch)
    v2_store = app.state.v2_store
    real_advisory_lock = v2_store.advisory_lock
    calls = {"n": 0}

    @contextlib.contextmanager
    def _flaky_advisory_lock(name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash acquiring the lock")
        with real_advisory_lock(name) as acquired:
            yield acquired

    monkeypatch.setattr(v2_store, "advisory_lock", _flaky_advisory_lock)

    with TestClient(app) as client:
        metrics = _poll_metrics(
            client,
            lambda m: int(m.get("worker_restarts") or 0) >= 1 and m.get("lock_held_by_self"),
        )

    assert calls["n"] >= 2, "advisory_lock should have been retried after the simulated crash"
    assert int(metrics.get("worker_restarts") or 0) >= 1
    assert metrics.get("last_worker_error")
    assert "simulated crash" in metrics["last_worker_error"]
    # The task itself is still alive and ticking -- no unhandled exception
    # killed it (that IS the bug this whole fix targets).
    assert metrics.get("lock_held_by_self") is True


def test_v2_verify_worker_heartbeat_updates_on_tick(tmp_path, monkeypatch):
    app = _build_worker_app(tmp_path, monkeypatch)
    v2_store = app.state.v2_store
    lock_name = "cathedral:v2:verify"

    with TestClient(app):
        deadline = time.time() + 6.0
        first_beat = None
        while time.time() < deadline and first_beat is None:
            rows = v2_store.query(
                "SELECT beat_at_iso FROM v2_worker_heartbeat WHERE key=?", (lock_name,))
            if rows:
                first_beat = rows[0]["beat_at_iso"]
            else:
                time.sleep(0.05)
        assert first_beat is not None, "heartbeat row was never written"

        # Wait for at least one more tick and confirm the beat moved forward.
        time.sleep(0.3)
        rows = v2_store.query(
            "SELECT beat_at_iso FROM v2_worker_heartbeat WHERE key=?", (lock_name,))
        assert rows
        assert rows[0]["beat_at_iso"] >= first_beat
