"""Regression for the recurring publisher wedge: a pooled psycopg2 connection
left in an aborted transaction poisons every subsequent borrower with
InFailedSqlTransaction ("current transaction is aborted, commands ignored until
end of transaction block") across every subsystem until it is rolled back.

These tests use an in-process fake pool that faithfully models psycopg2's
aborted-transaction state machine (a statement error inside a transaction leaves
the connection in a failed state that only rollback()/commit() clears) so no
live Postgres is required. They assert that the Store's acquire/release path
always hands out — and returns — a connection with a clean transaction state.
"""

from __future__ import annotations

import pytest

from scaffold.publisher.store import Store


class InFailedSqlTransaction(Exception):
    """Stand-in for psycopg2.errors.InFailedSqlTransaction."""


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list = []

    def execute(self, sql, params=()):
        # Any statement issued while the transaction is aborted fails the same
        # way, exactly like a real psycopg2 connection.
        if self._conn.aborted:
            raise InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until end of "
                "transaction block"
            )
        # A statement flagged to blow up poisons the transaction, just as a real
        # server-side error would.
        if "BOOM" in sql:
            self._conn.aborted = True
            raise RuntimeError("boom: statement failed mid-transaction")
        self._rows = [(1,)]
        return self

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self) -> None:
        self.aborted = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def rollback(self):
        self.aborted = False

    def commit(self):
        self.aborted = False

    def close(self):
        self.closed = True


class _FakePool:
    """Single-connection pool — the production wedge is one poisoned connection
    being handed back out over and over."""

    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    def getconn(self):
        return self.conn

    def putconn(self, conn, close=False):
        if close:
            conn.close()
            self.conn = _FakeConn()


def _pg_store_with(conn: _FakeConn) -> Store:
    """Build a Postgres-backed Store without touching a real database."""
    store = Store.__new__(Store)
    store.backend = "postgres"
    store._lock = None
    store._pool = _FakePool(conn)
    return store


def test_acquire_heals_a_leaked_aborted_connection() -> None:
    # Simulate a prior borrower that left the pooled connection in an aborted
    # transaction (e.g. an error path whose own rollback() failed). Without the
    # defensive rollback on acquire, the very next query — and every one after —
    # would raise InFailedSqlTransaction across all subsystems.
    conn = _FakeConn()
    conn.aborted = True
    store = _pg_store_with(conn)

    rows = store.query("SELECT 1")

    assert rows == [(1,)]
    assert conn.aborted is False


def test_release_clears_transaction_before_returning_to_pool() -> None:
    conn = _FakeConn()
    store = _pg_store_with(conn)

    # A query that errors mid-transaction poisons the connection.
    with pytest.raises(RuntimeError):
        store.query("SELECT BOOM")

    # The connection was returned to the pool clean, so the next borrower is fine.
    assert conn.aborted is False
    assert store.query("SELECT 1") == [(1,)]


def test_reused_connection_survives_failure_then_normal_query() -> None:
    # The full production scenario: fail a query, then run a normal query on the
    # REUSED connection from the pool and assert it succeeds rather than raising
    # InFailedSqlTransaction.
    conn = _FakeConn()
    store = _pg_store_with(conn)

    with pytest.raises(RuntimeError):
        store.write(lambda c: c.execute("UPDATE t SET x=1 WHERE BOOM"))

    # Same pooled connection, now a plain read — must not inherit the aborted state.
    assert store.query("SELECT 1") == [(1,)]
