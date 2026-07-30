"""CyberGym migrations must mean the same thing on SQLite and on Postgres.

Postgres cannot be executed in this suite, so the two dialect definitions are
compared statically, column by column, and the SQLite side is additionally
exercised against a real database. Nullability is checked explicitly: SQLite
permits NULL in a plain ``TEXT PRIMARY KEY`` while Postgres makes a primary key
implicitly NOT NULL, so a bare declaration would diverge exactly where it hurts,
on deploy.
"""
from __future__ import annotations

import re
import sqlite3

import pytest

from scaffold.publisher.store import _MIGRATIONS, _MIGRATIONS_PG, Store, _translate_sql

MIGRATION_ID = "0048_cybergym_scores"
AUTHENTICATED_BODY_MIGRATION_ID = "0049_cybergym_authenticated_body"
TABLES = ("cybergym_score_reports", "cybergym_scores")


def _ddl_for(migrations: list[tuple[str, str]], migration_id: str) -> str:
    for mid, sql in migrations:
        if mid == migration_id:
            return sql
    raise AssertionError(f"{migration_id} missing")


def _ddl(migrations: list[tuple[str, str]]) -> str:
    return _ddl_for(migrations, MIGRATION_ID)


def _columns(sql: str, table: str) -> list[str]:
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS " + table + r" \((.*?)\n        \);", sql, re.S
    )
    assert match, f"{table} not found"
    out = []
    for line in match.group(1).strip().splitlines():
        line = line.strip().rstrip(",")
        if not line or line.startswith("PRIMARY KEY"):
            continue
        out.append(line)
    return out


def test_migration_is_the_last_one_in_both_dialects():
    assert _MIGRATIONS[-1][0] == AUTHENTICATED_BODY_MIGRATION_ID
    assert _MIGRATIONS_PG[-1][0] == AUTHENTICATED_BODY_MIGRATION_ID


def test_authenticated_body_upgrade_is_present_in_both_dialects():
    sqlite = _ddl_for(_MIGRATIONS, AUTHENTICATED_BODY_MIGRATION_ID)
    postgres = _ddl_for(_MIGRATIONS_PG, AUTHENTICATED_BODY_MIGRATION_ID)
    for sql in (sqlite, postgres):
        assert "authenticated_body TEXT NOT NULL DEFAULT ''" in sql


@pytest.mark.parametrize("table", TABLES)
def test_column_names_and_order_match(table):
    sqlite_cols = [c.split()[0] for c in _columns(_ddl(_MIGRATIONS), table)]
    pg_cols = [c.split()[0] for c in _columns(_ddl(_MIGRATIONS_PG), table)]
    assert sqlite_cols == pg_cols


@pytest.mark.parametrize("table", TABLES)
def test_nullability_and_defaults_match(table):
    def _shape(sql: str) -> dict[str, tuple[bool, str | None]]:
        shape = {}
        for col in _columns(sql, table):
            name = col.split()[0]
            not_null = "NOT NULL" in col.upper()
            default = None
            m = re.search(r"DEFAULT\s+(\S+)", col, re.I)
            if m:
                default = m.group(1)
            shape[name] = (not_null, default)
        return shape

    assert _shape(_ddl(_MIGRATIONS)) == _shape(_ddl(_MIGRATIONS_PG))


def test_report_id_is_not_null_in_both_dialects():
    """The exact divergence this file exists for: a bare TEXT PRIMARY KEY accepts
    NULL on SQLite and would have rejected it on Postgres."""
    for migrations in (_MIGRATIONS, _MIGRATIONS_PG):
        cols = _columns(_ddl(migrations), "cybergym_score_reports")
        id_col = next(c for c in cols if c.split()[0] == "id")
        assert "NOT NULL" in id_col.upper()
        assert "PRIMARY KEY" in id_col.upper()


def test_sqlite_rejects_a_null_report_id(tmp_path):
    store = Store(str(tmp_path / "publisher.sqlite"), prefer_env_database_url=False)
    with pytest.raises(sqlite3.IntegrityError):
        store.write(lambda c: c.execute(
            "INSERT INTO cybergym_score_reports"
            "(id, network, netuid, source_epoch, producer_hotkey, complete, "
            "score_units, score_count, generated_at_iso, received_at_iso, "
            "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (None, "test", 1, 1, "5Producer", 1, "units", 0, "t", "t",
             "a" * 64, "b" * 64, "c" * 64, "", "{}")))
    assert store.query("SELECT * FROM cybergym_score_reports") == []


def test_postgres_conflict_targets_are_declared():
    """Both upsert forms the ingest path emits must translate to a real ON
    CONFLICT target, otherwise a Postgres retry silently does nothing."""
    reports = _translate_sql(
        "INSERT OR REPLACE INTO cybergym_score_reports(id, network) VALUES (?, ?)"
    )
    assert "ON CONFLICT (id) DO UPDATE SET network=excluded.network" in reports
    scores = _translate_sql(
        "INSERT OR REPLACE INTO cybergym_scores"
        "(report_id, miner_hotkey, score) VALUES (?, ?, ?)"
    )
    assert "ON CONFLICT (report_id, miner_hotkey) DO UPDATE SET score=excluded.score" in scores
