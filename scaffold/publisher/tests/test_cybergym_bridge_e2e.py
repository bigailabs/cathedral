"""End-to-end CyberGym lane: producer document to composed allocation.

Spans the whole bridge in one pass, using the real HTTP ingest route and the
real publisher Store rather than hand-written table rows:

    signed producer document
      -> POST /v1/cybergym/scores (bearer + raw-body HMAC + audience)
      -> cybergym_score_reports / cybergym_scores
      -> mechanism_cybergym_adapter (one newest fresh complete report)
      -> cybergym_bridge (compose + forfeited share to burn)

Also covers restart (persisted state survives a fresh Store on the same file and
no frozen score is resurrected), retries (idempotent), empty state (documented
empty fallback, share burns), and malformed input (rejected, share burns).

Non-writing with respect to any chain: no weight submission, no signing, no
network. The only writes are to a temporary SQLite file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from scaffold.publisher import (
    cybergym_bridge as bridge,
    cybergym_ingest as ingest,
    mechanism_cybergym_adapter as adapter,
    weights,
)
from scaffold.publisher.store import Store

TOKEN = "e2e-cybergym-token"
SECRET = "e2e-cybergym-secret"
PRODUCER = "5CyberGymProducer"
NETWORK = "test"
NETUID = 1234
BURN_HOTKEY = "5BurnDestination"
BURN_UID = 204
FRACTION = 0.25
NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "publisher.sqlite")


@pytest.fixture
def store(db_path):
    return Store(db_path, prefer_env_database_url=False)


@pytest.fixture(autouse=True)
def lane_env(monkeypatch):
    monkeypatch.setenv(ingest.INGEST_ENABLED_ENV, "1")
    monkeypatch.setenv(ingest.AUTH_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ingest.HMAC_SECRET_ENV, SECRET)
    monkeypatch.setenv(ingest.PRODUCER_HOTKEY_ENV, PRODUCER)
    monkeypatch.setenv(weights.NETWORK_ENV, NETWORK)
    monkeypatch.setenv(weights.NETUID_ENV, str(NETUID))
    # The burn destination is resolved by hotkey through the fresh metagraph
    # snapshot; the numeric var stays empty so nothing leans on the UID default.
    monkeypatch.setenv(weights.BURN_HOTKEY_ENV, BURN_HOTKEY)
    monkeypatch.setenv(weights.BURN_UID_ENV, "")
    monkeypatch.delenv(ingest.MAX_FUTURE_SKEW_SECS_ENV, raising=False)
    monkeypatch.setenv(bridge.MECHANISM_ENABLED_ENV, "1")
    monkeypatch.setenv(bridge.WEIGHT_FRACTION_ENV, str(FRACTION))
    monkeypatch.delenv(adapter.MAX_SCORE_AGE_SECS_ENV, raising=False)


def _client(store: Store) -> TestClient:
    app = FastAPI()
    app.include_router(ingest.router)
    app.dependency_overrides[ingest.get_publisher_store] = lambda: store
    return TestClient(app)


def _doc(*, epoch=1, scores=None, generated_at=None) -> dict:
    return {
        "producer_hotkey": PRODUCER,
        "network": NETWORK,
        "netuid": NETUID,
        "source_epoch": epoch,
        "generated_at": _iso(generated_at or NOW),
        "complete": True,
        "score_units": "level_weighted_verified_solves",
        "scores": {"5Alice": 3.0, "5Bob": 1.0} if scores is None else scores,
        "evidence_sha256": "d" * 64,
    }


def _post(client: TestClient, doc: dict, *, body: bytes | None = None):
    raw = body if body is not None else json.dumps(
        doc, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return client.post(
        "/v1/cybergym/scores",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "X-Cathedral-Cybergym-Signature": "sha256=" + hmac.new(
                SECRET.encode(), raw, hashlib.sha256
            ).hexdigest(),
        },
    )


def _registered(store: Store, mapping: dict[str, int], *, now: datetime = NOW) -> None:
    mapping = dict(mapping)
    mapping.setdefault(BURN_HOTKEY, BURN_UID)
    fresh = _iso(now - timedelta(seconds=60))
    for hotkey, uid in mapping.items():
        store.write(lambda c, hk=hotkey, u=uid: c.execute(
            "INSERT OR REPLACE INTO metagraph_hotkeys("
            "network, netuid, hotkey, uid, coldkey, block, updated_at_iso"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (NETWORK, NETUID, hk, u, "", 123, fresh)))


# --- the full path -------------------------------------------------------

def test_document_to_allocation(store):
    _registered(store, {"5Alice": 10, "5Bob": 20})
    assert _post(_client(store), _doc()).status_code == 200

    vec, meta, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {10: 3.0, 20: 1.0}
    assert meta.sig_ok is True
    assert meta.signed_at_ms == int(NOW.timestamp() * 1000)
    assert info["reason"] == "ok"
    assert info["producer_hotkey"] == PRODUCER

    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "ok"
    assert math.isclose(out["weights"][10], FRACTION * 0.75, abs_tol=1e-12)
    assert math.isclose(out["weights"][20], FRACTION * 0.25, abs_tol=1e-12)
    assert out["forfeited_fraction"] == 0.0
    assert BURN_UID not in out["weights"]


def test_retry_is_idempotent_and_allocation_is_unchanged(store):
    _registered(store, {"5Alice": 10, "5Bob": 20})
    client = _client(store)
    assert _post(client, _doc()).json()["idempotent"] is False
    first = bridge.cybergym_allocation(store, now=NOW)["weights"]
    for _ in range(3):
        assert _post(client, _doc()).json()["idempotent"] is True
    assert bridge.cybergym_allocation(store, now=NOW)["weights"] == first
    assert len(store.query("SELECT * FROM cybergym_score_reports")) == 1
    assert len(store.query("SELECT * FROM cybergym_scores")) == 2


def test_restart_preserves_state_without_resurrecting_frozen_scores(db_path):
    """A fresh Store on the same file sees the same allocation. Once the report
    ages past the freshness window, the same persisted rows burn instead of
    being restamped as current."""
    first = Store(db_path, prefer_env_database_url=False)
    _registered(first, {"5Alice": 10, "5Bob": 20})
    assert _post(_client(first), _doc()).status_code == 200
    before = bridge.cybergym_allocation(first, now=NOW)

    restarted = Store(db_path, prefer_env_database_url=False)
    after = bridge.cybergym_allocation(restarted, now=NOW)
    assert after["weights"] == before["weights"]
    assert after["cybergym"]["report_sha256"] == before["cybergym"]["report_sha256"]

    much_later = NOW + timedelta(days=7)
    _registered(restarted, {"5Alice": 10, "5Bob": 20}, now=much_later)
    stale = bridge.cybergym_allocation(restarted, now=much_later)
    assert stale["cybergym"]["reason"] == "stale"
    assert stale["weights"] == {BURN_UID: pytest.approx(FRACTION)}


def test_empty_state_burns_the_share(store):
    _registered(store, {"5Alice": 10})
    vec, _, info = adapter.cybergym_score_snapshot(store, now=NOW)
    assert vec == {} and info["reason"] == "no_report"
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["weights"] == {BURN_UID: pytest.approx(FRACTION)}
    assert math.isclose(out["forfeited_fraction"], FRACTION, abs_tol=1e-12)


def test_malformed_document_is_rejected_and_the_share_burns(store):
    _registered(store, {"5Alice": 10})
    client = _client(store)
    bad = _doc()
    bad["scores"] = {"5Alice": -5.0}
    assert _post(client, bad).status_code == 400
    assert _post(client, bad, body=b"{not json").status_code == 400
    assert store.query("SELECT * FROM cybergym_score_reports") == []

    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["weights"] == {BURN_UID: pytest.approx(FRACTION)}


def test_replayed_old_document_cannot_displace_the_current_epoch(store):
    """The classic resurrection attack: post epoch 2, then replay epoch 1. The
    fence refuses it and the composed allocation still reflects epoch 2."""
    _registered(store, {"5Alice": 10, "5Bob": 20})
    client = _client(store)
    assert _post(client, _doc(epoch=1, scores={"5Alice": 9.0})).status_code == 200
    assert _post(client, _doc(epoch=2, scores={"5Bob": 1.0})).status_code == 200

    replay = _post(client, _doc(epoch=1, scores={"5Alice": 9.0}))
    assert replay.status_code == 409
    assert replay.json()["detail"] == "epoch_too_old"

    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["cybergym"]["source_epoch"] == 2
    # 5Alice was omitted from the newest complete report, so it is revoked.
    assert out["weights"] == {20: pytest.approx(FRACTION)}


def test_newest_report_revokes_an_omitted_miner(store):
    _registered(store, {"5Alice": 10, "5Bob": 20})
    client = _client(store)
    assert _post(client, _doc(epoch=1)).status_code == 200
    out_before = bridge.cybergym_allocation(store, now=NOW)
    assert set(out_before["weights"]) == {10, 20}

    assert _post(client, _doc(epoch=2, scores={"5Alice": 2.0})).status_code == 200
    out_after = bridge.cybergym_allocation(store, now=NOW)
    assert out_after["weights"] == {10: pytest.approx(FRACTION)}


def test_empty_complete_report_revokes_everyone_and_burns(store):
    """A complete report with no scores is the producer saying "nobody earned
    this epoch". The whole share must burn, not linger on last epoch's miners."""
    _registered(store, {"5Alice": 10, "5Bob": 20})
    client = _client(store)
    assert _post(client, _doc(epoch=1)).status_code == 200
    assert _post(client, _doc(epoch=2, scores={})).status_code == 200
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["cybergym"]["reason"] == "empty_report"
    assert out["weights"] == {BURN_UID: pytest.approx(FRACTION)}


def test_lane_is_inert_when_the_mechanism_is_off(store, monkeypatch):
    """The merge-safety property: ingested data changes nothing while the
    mechanism is at its default-off setting."""
    monkeypatch.delenv(bridge.MECHANISM_ENABLED_ENV, raising=False)
    monkeypatch.delenv(bridge.WEIGHT_FRACTION_ENV, raising=False)
    _registered(store, {"5Alice": 10, "5Bob": 20})
    assert _post(_client(store), _doc()).status_code == 200
    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["status"] == "disabled"
    assert out["weights"] == {}


def test_tampering_with_ingested_rows_burns_the_share(store):
    """The full-path version of the read-side verification: a genuinely ingested
    report whose score rows are later edited in the database contributes nothing,
    and its share burns rather than paying the inflated score."""
    _registered(store, {"5Alice": 10, "5Bob": 20})
    assert _post(_client(store), _doc()).status_code == 200
    healthy = bridge.cybergym_allocation(store, now=NOW)
    assert healthy["status"] == "ok"
    assert set(healthy["weights"]) == {10, 20}

    store.write(lambda c: c.execute(
        "UPDATE cybergym_scores SET score=999999 WHERE miner_hotkey=?", ("5Alice",)))
    tampered = bridge.cybergym_allocation(store, now=NOW)
    assert tampered["cybergym"]["reason"] == "rows_tampered"
    assert tampered["weights"] == {BURN_UID: pytest.approx(FRACTION)}


def test_forged_report_row_burns_the_share(store):
    """A report hand-written into the database, with no credentials involved,
    takes nothing: the adapter re-verifies the stored HMAC."""
    _registered(store, {"5Attacker": 7})
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_score_reports"
        "(id, network, netuid, source_epoch, producer_hotkey, complete, "
        "score_units, score_count, generated_at_iso, received_at_iso, "
        "report_sha256, body_sha256, evidence_sha256, signature, report_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forged", NETWORK, NETUID, 99, "5Attacker", 1, "units", 1,
         _iso(NOW), _iso(NOW), "0" * 64, "b" * 64, "c" * 64, "",
         '{"totally":"unrelated"}')))
    store.write(lambda c: c.execute(
        "INSERT OR REPLACE INTO cybergym_scores"
        "(report_id, miner_hotkey, epoch, score, network, netuid, "
        "producer_hotkey, report_sha256, generated_at_iso, received_at_iso) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("forged", "5Attacker", 99, 1000.0, NETWORK, NETUID, "5Attacker",
         "0" * 64, _iso(NOW), _iso(NOW))))

    out = bridge.cybergym_allocation(store, now=NOW)
    assert out["cybergym"]["reason"] == "body_digest_mismatch"
    assert out["weights"] == {BURN_UID: pytest.approx(FRACTION)}
    assert 7 not in out["weights"]
