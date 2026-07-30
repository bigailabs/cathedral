"""Tests for scaffold/publisher/mechanism_cybergym_adapter.py.

Covers the CyberGym-as-mechanism adapter: verified per-miner CyberGym scores
(the level-weighted sum of verified PoC solves) remapped from miner_hotkey to
miner uid via the metagraph_hotkeys snapshot table, per
deploy/MECHANISM_ROUTER_CONTRACT.md. Mirrors test_mechanism_sat_adapter.py.

The reading contract under test: exactly ONE newest complete report per
audience, freshness taken from the authenticated generated_at rather than read
time, and every failure mode collapsing to the documented empty vector so the
mechanism forfeits its share (which the composition layer burns).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from scaffold.publisher import (
    cybergym_contract as contract,
    mechanism_cybergym_adapter as adapter,
    weights,
)
from scaffold.publisher.store import Store, _MIGRATIONS, _sqlite_exec_migration

NETWORK = "finney"
NETUID = 39
SECRET = "adapter-test-hmac-secret"
PRODUCER = "5Producer"
UNITS = "level_weighted_verified_solves"
NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _store(tmp_path) -> Store:
    # Migration 0048_cybergym_scores creates both CyberGym tables; the metagraph
    # snapshot table has existed since 0028. No manual DDL needed.
    return Store(str(tmp_path / "publisher.sqlite"), prefer_env_database_url=False)


def _document(
    *, epoch: int, scores: dict[str, float], generated_at: datetime,
    complete: bool = True, network: str = NETWORK, netuid: int = NETUID,
    producer: str = PRODUCER,
) -> dict:
    return contract.semantic_view({
        "producer_hotkey": producer,
        "network": network,
        "netuid": netuid,
        "source_epoch": epoch,
        "generated_at": _iso(generated_at),
        "complete": complete,
        "score_units": UNITS,
        "scores": scores,
        "evidence_sha256": "c" * 64,
    })


def _report(
    store: Store,
    *,
    epoch: int,
    scores: dict[str, float],
    generated_at: datetime | None = None,
    complete: int = 1,
    body_sha256: str | None = None,
    signature: str | None = None,
    report_sha256: str | None = None,
    body: str | None = None,
    report_id: str | None = None,
    network: str = NETWORK,
    netuid: int = NETUID,
    score_count: int | None = None,
    rows: dict[str, float] | None = None,
    close_epoch: bool = True,
) -> str:
    """Persist one report the way the authenticated ingest route would.

    The body is the exact canonical document, ``body_sha256`` is its digest, and
    ``signature`` is a real HMAC under SECRET, so the adapter's verification
    passes. Every override exists so a test can corrupt exactly one thing.
    """
    generated = generated_at or NOW
    document = _document(
        epoch=epoch, scores=scores, generated_at=generated,
        complete=bool(complete), network=network, netuid=netuid,
    )
    body_text = body if body is not None else contract.canonical_report_bytes(
        document
    ).decode("utf-8")
    body_bytes = body_text.encode("utf-8")
    digest = report_sha256 or contract.report_digest(document)
    rid = report_id or contract.receipt_id(digest)
    stored_body_digest = (
        body_sha256 if body_sha256 is not None
        else hashlib.sha256(body_bytes).hexdigest()
    )
    stored_signature = (
        signature if signature is not None
        else "sha256=" + contract.body_hmac_hex(body_bytes, SECRET)
    )
    generated_iso = _iso(generated)
    count = score_count if score_count is not None else len(scores)
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_score_reports"
        "(id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, network, netuid, epoch, PRODUCER, complete, UNITS, count,
         generated_iso, generated_iso, digest, stored_body_digest, "c" * 64,
         stored_signature, body_text)))
    for hotkey, score in (rows if rows is not None else scores).items():
        store.write(lambda c, hk=hotkey, sc=score: c.execute(
            "INSERT OR REPLACE INTO cybergym_scores"
            "(report_id, miner_hotkey, epoch, score, network, netuid, "
            "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, hk, epoch, sc, network, netuid, PRODUCER, digest,
             generated_iso, generated_iso)))
    # The closed-epoch gate (#416) refuses to publish an epoch the CyberGym
    # writer has not marked closed. A report the ingest route accepted is a
    # finished epoch in almost every test, so close it by default and let the
    # gate tests below pass close_epoch=False to exercise the refusal.
    if close_epoch:
        _mark_epoch(store, epoch, adapter.EPOCH_CLOSED)
    return rid


def _mark_epoch(store: Store, epoch: int, state: str) -> None:
    """Write the cybergym_epoch_status marker the CyberGym validator owns."""
    store.write(lambda c: c.execute(
        "CREATE TABLE IF NOT EXISTS cybergym_epoch_status ("
        "epoch INTEGER PRIMARY KEY, state TEXT NOT NULL)"))
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_epoch_status(epoch, state) VALUES (?, ?)",
        (int(epoch), state)))


def _uid(store: Store, hotkey: str, uid, *, network=NETWORK, netuid=NETUID) -> None:
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO metagraph_hotkeys("
        "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (network, netuid, hotkey, uid, "", 123, "2026-07-01T00:00:00.000Z")))


def _env(monkeypatch) -> None:
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, SECRET)
    monkeypatch.setenv(weights.NETWORK_ENV, NETWORK)
    monkeypatch.setenv(weights.NETUID_ENV, str(NETUID))
    monkeypatch.delenv(adapter.MAX_SCORE_AGE_SECS_ENV, raising=False)
    monkeypatch.delenv(adapter.MAX_FUTURE_SKEW_SECS_ENV, raising=False)


def test_malformed_netuid_env_returns_empty_not_raises(tmp_path, monkeypatch):
    # The adapter's contract is to never raise; a set-but-malformed NETUID env is a
    # misconfiguration, so it must burn (empty vector + reason), not propagate a
    # ValueError into the refresh cycle.
    _env(monkeypatch)
    monkeypatch.setenv(weights.NETUID_ENV, "notanint")
    store = _store(tmp_path)
    vec, meta, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {} and meta.sig_ok is False
    assert info["reason"] == "bad_netuid_config" and info["contributing"] is False
    # the router-shaped wrapper is equally exception-free
    vec2, meta2 = adapter.cybergym_mechanism_scores(store, epoch=1, now=NOW)
    assert vec2 == {}


def test_verified_scores_map_to_uid(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 12.0, "5Bob": 4.0})
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)

    vec, meta = adapter.cybergym_mechanism_scores(store, epoch=1, now=NOW)
    assert vec == {10: 12.0, 20: 4.0}
    assert meta.mechanism_id == "cybergym_v0"
    assert meta.source == "cybergym_adapter"
    assert meta.sig_ok is True


def test_0048_noncanonical_row_survives_real_0049_upgrade(tmp_path, monkeypatch):
    """Run the actual SQLite migration over a legacy HMAC-covered wire body."""
    _env(monkeypatch)
    db_path = tmp_path / "publisher.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for migration_id, sql in _MIGRATIONS:
        if migration_id == "0049_cybergym_authenticated_body":
            break
        _sqlite_exec_migration(conn, sql)
        conn.execute(
            "INSERT INTO schema_migrations(id, applied_at) VALUES (?, ?)",
            (migration_id, _iso(NOW)),
        )

    legacy_document = {
        "producer_hotkey": PRODUCER,
        "network": NETWORK,
        "netuid": NETUID,
        "source_epoch": 1,
        "generated_at": "2026-07-29T12:00:00+00:00",
        "complete": True,
        "score_units": UNITS,
        "scores": {"5Alice": 12, "5Bob": 4},
        "evidence_sha256": "c" * 64,
    }
    raw_body = json.dumps(
        legacy_document, indent=2, sort_keys=False
    ).encode("utf-8")
    normalized = contract.normalize_semantic_document(legacy_document)
    digest = contract.report_digest(normalized)
    report_id = contract.receipt_id(digest)
    signature = "sha256=" + contract.body_hmac_hex(raw_body, SECRET)
    conn.execute(
        "INSERT INTO cybergym_score_reports"
        "(id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_id,
            NETWORK,
            NETUID,
            1,
            PRODUCER,
            1,
            UNITS,
            2,
            normalized["generated_at"],
            normalized["generated_at"],
            digest,
            hashlib.sha256(raw_body).hexdigest(),
            "c" * 64,
            signature,
            raw_body.decode("utf-8"),
        ),
    )
    for hotkey, score in normalized["scores"].items():
        conn.execute(
            "INSERT INTO cybergym_scores"
            "(report_id, miner_hotkey, epoch, score, network, netuid, "
            "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_id,
                hotkey,
                1,
                score,
                NETWORK,
                NETUID,
                PRODUCER,
                digest,
                normalized["generated_at"],
                normalized["generated_at"],
            ),
        )
    conn.execute(
        "INSERT INTO metagraph_hotkeys("
        "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (NETWORK, NETUID, "5Alice", 10, "", 123, normalized["generated_at"]),
    )
    conn.execute(
        "INSERT INTO metagraph_hotkeys("
        "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (NETWORK, NETUID, "5Bob", 20, "", 123, normalized["generated_at"]),
    )
    conn.commit()
    conn.close()

    store = Store(str(db_path), prefer_env_database_url=False)
    migrated = store.query(
        "SELECT authenticated_body, report_json FROM cybergym_score_reports"
    )[0]
    assert migrated["authenticated_body"] == ""
    assert migrated["report_json"] == raw_body.decode("utf-8")
    assert store.query(
        "SELECT id FROM schema_migrations "
        "WHERE id='0049_cybergym_authenticated_body'"
    )

    vector, meta, info = adapter.cybergym_score_snapshot(
        store, epoch=1, now=NOW
    )
    assert vector == {10: 12.0, 20: 4.0}
    assert meta.sig_ok is True
    assert info["reason"] == "ok"


def test_unmapped_hotkey_is_dropped_not_zeroed(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 8.0, "5NoUid": 99.0})
    _uid(store, "5Alice", 10)

    vec, _ = adapter.cybergym_mechanism_scores(store, epoch=1, now=NOW)
    assert vec == {10: 8.0}  # the unmapped miner's score never lands anywhere


def test_null_uid_is_dropped(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 8.0})
    _uid(store, "5Alice", None)  # registered but no UID yet
    vec, _, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {}
    assert info["reason"] == "no_uid_mapping"


def test_non_positive_scores_ignored(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 0.0, "5Bob": -1.0})
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)
    vec, _ = adapter.cybergym_mechanism_scores(store, epoch=1, now=NOW)
    assert vec == {}


def test_no_scores_returns_empty_not_exception(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert meta.mechanism_id == "cybergym_v0"
    assert meta.sig_ok is False
    assert info["reason"] == "no_report"


def test_empty_complete_report_contributes_nothing(tmp_path, monkeypatch):
    """"Nobody scored this epoch" is a valid complete statement and must yield
    an empty vector, not a carried-forward older report."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _report(store, epoch=2, scores={})
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["source_epoch"] == 2
    assert info["reason"] == "empty_report"


def test_only_the_newest_report_is_read(tmp_path, monkeypatch):
    """A miner present at an older epoch but omitted from the newest complete
    report is revoked, not carried forward. The original per-miner maximum-epoch
    query would have paid it."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0, "5Bob": 7.0})
    _report(store, epoch=2, scores={"5Alice": 3.0})
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 3.0}
    assert info["source_epoch"] == 2


def test_freshness_comes_from_generated_at_not_read_time(tmp_path, monkeypatch):
    """signed_at_ms must equal the authenticated generated_at, so the router's
    own staleness gate can see the real age of the producer document."""
    _env(monkeypatch)
    store = _store(tmp_path)
    generated = NOW - timedelta(seconds=600)
    _report(store, epoch=1, scores={"5Alice": 5.0}, generated_at=generated)
    _uid(store, "5Alice", 10)
    _, meta = adapter.cybergym_mechanism_scores(store, now=NOW)
    assert meta.signed_at_ms == int(generated.timestamp() * 1000)
    assert meta.signed_at_ms < int(NOW.timestamp() * 1000)


def test_stale_report_is_not_resurrected(tmp_path, monkeypatch):
    """A dead producer's last report ages out instead of being restamped fresh
    on every read."""
    _env(monkeypatch)
    store = _store(tmp_path)
    generated = NOW - timedelta(days=30)
    _report(store, epoch=1, scores={"5Alice": 5.0}, generated_at=generated)
    _uid(store, "5Alice", 10)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "stale"
    assert meta.signed_at_ms == int(generated.timestamp() * 1000)


def test_max_age_is_configurable(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv(adapter.MAX_SCORE_AGE_SECS_ENV, "86400")
    store = _store(tmp_path)
    _report(
        store, epoch=1, scores={"5Alice": 5.0},
        generated_at=NOW - timedelta(hours=5),
    )
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 5.0}
    assert info["reason"] == "ok"


def test_report_with_no_body_digest_contributes_nothing(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0}, body_sha256="")
    _uid(store, "5Alice", 10)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert meta.sig_ok is False
    assert info["reason"] == "body_digest_mismatch"


def test_incomplete_report_is_ignored(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0}, complete=0)
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "no_report"


def test_other_audience_report_is_ignored(tmp_path, monkeypatch):
    """A report generated for a different network/netuid must never be composed
    into this publisher's vector."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0}, network="test", netuid=1234)
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "no_report"


def test_pinned_epoch_mismatch_yields_nothing(tmp_path, monkeypatch):
    """A caller that pins an epoch gets that epoch or nothing: never a
    different report's scores under the requested epoch's name."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, epoch=2, now=NOW)
    assert vec == {}
    assert info["reason"] == "epoch_not_available"


def test_missing_table_returns_empty_fallback(tmp_path, monkeypatch):
    """A database that predates migration 0048 must produce the documented
    empty fallback, not an OperationalError."""
    _env(monkeypatch)
    store = _store(tmp_path)
    store.write(lambda c: c.execute("DROP TABLE cybergym_score_reports"))
    vec, meta, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {}
    assert meta.mechanism_id == "cybergym_v0"
    assert info["reason"] == "table_missing"


def test_score_rows_table_missing_returns_empty_fallback(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    store.write(lambda c: c.execute("DROP TABLE cybergym_scores"))
    vec, _, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {}
    assert info["reason"] == "table_missing"


def test_adapter_never_writes(tmp_path, monkeypatch):
    """Read-only guardrail: composing does not mutate any CyberGym table."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 5.0})
    _uid(store, "5Alice", 10)

    def _snapshot():
        return (
            [tuple(r) for r in store.query(
                "SELECT * FROM cybergym_score_reports ORDER BY id")],
            [tuple(r) for r in store.query(
                "SELECT * FROM cybergym_scores ORDER BY report_id, miner_hotkey")],
        )

    before = _snapshot()
    adapter.cybergym_mechanism_scores(store, epoch=1, now=NOW)
    assert _snapshot() == before


def test_future_dated_report_is_not_treated_as_fresh(tmp_path, monkeypatch):
    """A negative age can never exceed max_age, so without an explicit bound a
    future-dated report reads as fresh for as long as it sits in the table.
    Defence in depth: the intake route refuses these on the way in."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(
        store, epoch=1, scores={"5Alice": 5.0},
        generated_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    _uid(store, "5Alice", 10)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "future_dated"
    assert info["age_secs"] < 0
    assert meta.sig_ok is False


def test_small_clock_skew_stays_acceptable(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(
        store, epoch=1, scores={"5Alice": 5.0},
        generated_at=NOW + timedelta(seconds=30),
    )
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 5.0}
    assert info["reason"] == "ok"


def test_future_skew_allowance_is_configurable(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv(adapter.MAX_FUTURE_SKEW_SECS_ENV, "5")
    store = _store(tmp_path)
    _report(
        store, epoch=1, scores={"5Alice": 5.0},
        generated_at=NOW + timedelta(seconds=30),
    )
    _uid(store, "5Alice", 10)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "future_dated"


# --- sig_ok must be a verification, not a column presence check ----------

def test_hand_inserted_row_is_refused(tmp_path, monkeypatch):
    """The probe that falsified the previous docstring: a row written straight
    into the database, with an arbitrary 64-hex body digest and a body that has
    nothing to do with the digests, used to read as reason=ok sig_ok=True and
    take the whole lane share."""
    _env(monkeypatch)
    store = _store(tmp_path)
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_score_reports"
        "(id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forged", NETWORK, NETUID, 1, "5Attacker", 1, UNITS, 1,
         _iso(NOW), _iso(NOW), "0" * 64, "b" * 64, "c" * 64, "",
         '{"totally":"unrelated"}')))
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_scores"
        "(report_id, miner_hotkey, epoch, score, network, netuid, "
        "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forged", "5Attacker", 1, 1000.0, NETWORK, NETUID, "5Attacker",
         "0" * 64, _iso(NOW), _iso(NOW))))
    _uid(store, "5Attacker", 7)

    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert meta.sig_ok is False
    assert info["reason"] == "body_digest_mismatch"


@pytest.mark.parametrize("tamper,reason", [
    ("inflate_row", "rows_tampered"),
    ("delete_row", "rows_tampered"),
    ("add_row", "rows_tampered"),
    ("edit_body", "body_digest_mismatch"),
    ("edit_report_digest", "report_digest_mismatch"),
    ("edit_epoch_column", "header_mismatch"),
    ("edit_producer_column", "header_mismatch"),
    ("edit_score_count", "header_mismatch"),
    ("strip_signature", "signature_invalid"),
    ("wrong_signature", "signature_invalid"),
])
def test_tampering_with_a_genuine_report_is_detected(tmp_path, monkeypatch, tamper, reason):
    """Every column and every row is cross-checked against the authenticated
    body, so editing any one of them makes the mechanism non-contributing."""
    _env(monkeypatch)
    store = _store(tmp_path)
    kwargs: dict = {}
    if tamper == "inflate_row":
        kwargs["rows"] = {"5Alice": 999999.0, "5Bob": 1.0}
    elif tamper == "delete_row":
        kwargs["rows"] = {"5Alice": 3.0}
    elif tamper == "add_row":
        kwargs["rows"] = {"5Alice": 3.0, "5Bob": 1.0, "5Ghost": 5.0}
    elif tamper == "edit_report_digest":
        kwargs["report_sha256"] = "1" * 64
    elif tamper == "edit_score_count":
        kwargs["score_count"] = 7
    elif tamper == "strip_signature":
        kwargs["signature"] = ""
    elif tamper == "wrong_signature":
        kwargs["signature"] = "sha256=" + "f" * 64
    rid = _report(store, epoch=1, scores={"5Alice": 3.0, "5Bob": 1.0}, **kwargs)
    if tamper == "edit_body":
        # Edited in place AFTER storage, so the stored digest still commits to
        # the original bytes. This is the realistic tampering shape.
        store.write(lambda c: c.execute(
            "UPDATE cybergym_score_reports SET report_json=? WHERE id=?",
            ('{"scores":{"5Alice":9999}}', rid)))
    elif tamper == "edit_epoch_column":
        store.write(lambda c: c.execute(
            "UPDATE cybergym_score_reports SET source_epoch=99 WHERE id=?", (rid,)))
    elif tamper == "edit_producer_column":
        store.write(lambda c: c.execute(
            "UPDATE cybergym_score_reports SET producer_hotkey=? WHERE id=?",
            ("5Attacker", rid)))
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)

    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert meta.sig_ok is False
    assert info["reason"] == reason


def test_scores_come_from_the_verified_document(tmp_path, monkeypatch):
    """The document is the authenticated truth; the rows are a cross-checked
    projection. An untampered pair yields the document's values."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 3.0, "5Bob": 1.0})
    _uid(store, "5Alice", 10)
    _uid(store, "5Bob", 20)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 3.0, 20: 1.0}
    assert meta.sig_ok is True
    assert info["verified"] is True


def test_no_hmac_secret_configured_fails_closed(tmp_path, monkeypatch):
    """With no secret nothing can be verified, so the lane contributes nothing
    rather than downgrading to a digest-only check."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 3.0})
    _uid(store, "5Alice", 10)
    monkeypatch.delenv(contract.HMAC_SECRET_ENV, raising=False)
    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert meta.sig_ok is False
    assert info["reason"] == "signature_unverifiable"


def test_rotated_secret_makes_old_reports_unverifiable(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 3.0})
    _uid(store, "5Alice", 10)
    monkeypatch.setenv(contract.HMAC_SECRET_ENV, "a-new-secret")
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {}
    assert info["reason"] == "signature_invalid"


def test_zero_scored_miner_in_the_document_is_not_tampering(tmp_path, monkeypatch):
    """score_count counts the document's entries, including explicit zeros, so a
    zero-scored miner is legitimate rather than a missing row."""
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 3.0, "5Zero": 0.0})
    _uid(store, "5Alice", 10)
    _uid(store, "5Zero", 30)
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 3.0}
    assert info["reason"] == "ok"
    assert info["score_count"] == 2


# --------------------------------------------------------------------------- #
# closed-epoch gate (#416 contract, exercised against the authenticated path)
# --------------------------------------------------------------------------- #
def test_open_epoch_raises_rather_than_publishing(tmp_path, monkeypatch):
    # An open epoch must RAISE, not return empty: refresh persists an empty
    # vector, which would overwrite a prior good one and read as "nobody solved".
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 12.0}, close_epoch=False)
    _mark_epoch(store, 1, "open")
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed):
        adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)


def test_absent_marker_relies_on_the_authenticated_complete_flag(tmp_path, monkeypatch):
    # This lane's completeness proof is `complete: true` inside the HMAC-signed
    # body, not the marker table (which distill writes to its OWN database, not the
    # publisher's). With no marker at all the authenticated report still publishes;
    # refusing here would burn the lane permanently instead of fencing it.
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 12.0}, close_epoch=False)
    _uid(store, "5Alice", 10)
    vec, meta, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {10: 12.0} and info["reason"] == "ok" and meta.sig_ok is True


def test_marker_that_says_incomplete_also_refuses(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 12.0}, close_epoch=False)
    _mark_epoch(store, 1, "incomplete")
    _uid(store, "5Alice", 10)
    with pytest.raises(adapter.CyberGymEpochNotClosed):
        adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)


def test_closed_epoch_publishes_normally(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    _report(store, epoch=1, scores={"5Alice": 12.0})   # closed by default
    _uid(store, "5Alice", 10)
    vec, meta, info = adapter.cybergym_score_snapshot(store, epoch=1, now=NOW)
    assert vec == {10: 12.0} and info["reason"] == "ok" and meta.sig_ok is True


def test_epoch_closed_literal_matches_the_writer():
    # Cross-repo literal: cathedral-distill's writer populates this column and no
    # import spans the boundary, so drift must fail here rather than silently
    # matching no row in production.
    assert adapter.EPOCH_CLOSED == "closed"
