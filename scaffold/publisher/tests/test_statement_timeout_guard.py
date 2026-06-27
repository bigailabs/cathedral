from __future__ import annotations

import pytest

from scaffold.publisher.app import (
    _check_read_statement_timeout,
    _statement_timeout_guard_warning,
)


def test_warns_when_read_role_has_no_statement_timeout() -> None:
    warning = _statement_timeout_guard_warning("read", None)
    assert warning is not None
    assert "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS" in warning
    assert "WARNING" in warning


def test_warns_when_value_is_zero() -> None:
    # 0 is Postgres' "no timeout" sentinel, the exact missing value.
    assert _statement_timeout_guard_warning("read", "0") is not None


def test_warns_when_value_is_blank() -> None:
    assert _statement_timeout_guard_warning("all", "   ") is not None


def test_warns_when_value_is_non_numeric() -> None:
    assert _statement_timeout_guard_warning("read", "abc") is not None


def test_all_role_also_serves_reads_and_is_guarded() -> None:
    assert _statement_timeout_guard_warning("all", None) is not None


def test_silent_when_timeout_is_positive() -> None:
    assert _statement_timeout_guard_warning("read", "4000") is None
    assert _statement_timeout_guard_warning("all", "4000") is None


@pytest.mark.parametrize("role", ["submit", "worker"])
def test_non_read_roles_are_not_guarded(role: str) -> None:
    # Submit/worker do not serve the slow board queries; no warning expected
    # even with the timeout unset.
    assert _statement_timeout_guard_warning(role, None) is None


def test_check_logs_warning(capsys, monkeypatch) -> None:
    monkeypatch.delenv("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS", raising=False)
    _check_read_statement_timeout("read")
    out = capsys.readouterr().out
    assert "CATHEDRAL_PG_STATEMENT_TIMEOUT_MS" in out
    assert "WARNING" in out


def test_check_silent_when_configured(capsys, monkeypatch) -> None:
    monkeypatch.setenv("CATHEDRAL_PG_STATEMENT_TIMEOUT_MS", "4000")
    _check_read_statement_timeout("read")
    assert capsys.readouterr().out == ""
