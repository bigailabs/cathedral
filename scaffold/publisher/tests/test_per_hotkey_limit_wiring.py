"""Integration tests for the per-hotkey limiter wired into the submit ASGI path.

These exercise the real ``_HotPathBackpressureMiddleware`` in app.py to prove:
  * when the limiter is enabled, a single hotkey flooding /v1/agents/submit gets a
    429 carrying the DISTINCT ``abuse_rate_limited`` reason (not submit_busy_retry),
    and a different hotkey is unaffected;
  * when the flag is unset (default), the limiter is a no-op: no submit ever gets
    the abuse reason, so the live saturation-gate behaviour is preserved exactly.

The limiter runs in the middleware BEFORE signature verification, so we can drive
it with unsigned requests — we only assert on the rejection reason header, never
on accept semantics.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from scaffold.publisher.app import build_app


def _build_client(tmp_path):
    db = str(tmp_path / "phk.db")
    app = build_app(database_path=db, signing_key_hex="11" * 32)
    return TestClient(app)


def _post_submit(client, hotkey):
    return client.post(
        "/v1/agents/submit",
        json={"challenge_id": "pm-x", "dimacs_solution": "s SATISFIABLE\nv 1 0\n"},
        headers={"X-Cathedral-Hotkey": hotkey},
    )


def test_enabled_throttles_one_hotkey_with_distinct_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_BURST", "1")
    # Refill ~never within the test window so the 2nd request is reliably starved.
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC", "0.0001")
    monkeypatch.setenv("CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS", "3")

    client = _build_client(tmp_path)

    # First submit from alice consumes her single token; it is NOT abuse-rejected
    # by the per-hotkey limiter (it proceeds to normal handling downstream).
    r1 = _post_submit(client, "alice")
    assert r1.headers.get("X-Cathedral-Rejection-Reason") != "abuse_rate_limited"

    # Second submit from alice within the window: abuse-throttled with 429 + the
    # distinct reason + a Retry-After hint.
    r2 = _post_submit(client, "alice")
    assert r2.status_code == 429
    assert r2.headers["X-Cathedral-Rejection-Reason"] == "abuse_rate_limited"
    assert r2.headers["X-Cathedral-Rejection-Reason"] != "submit_busy_retry"
    assert r2.headers["Retry-After"] == "3"
    assert r2.text == "abuse_rate_limited"

    # A different hotkey is unaffected by alice's spend (fairness, not throughput).
    rb = _post_submit(client, "bob")
    assert rb.headers.get("X-Cathedral-Rejection-Reason") != "abuse_rate_limited"


def test_flag_off_never_abuse_rejects(tmp_path, monkeypatch):
    # Default: flag unset => limiter inactive => no submit can carry the abuse
    # reason no matter how hard one hotkey hammers the endpoint.
    for k in (
        "CATHEDRAL_PER_HOTKEY_LIMIT_ENABLED",
        "CATHEDRAL_PER_HOTKEY_BURST",
        "CATHEDRAL_PER_HOTKEY_REFILL_PER_SEC",
        "CATHEDRAL_PER_HOTKEY_RETRY_AFTER_SECS",
    ):
        monkeypatch.delenv(k, raising=False)

    client = _build_client(tmp_path)
    for _ in range(50):
        r = _post_submit(client, "alice")
        assert r.headers.get("X-Cathedral-Rejection-Reason") != "abuse_rate_limited"
