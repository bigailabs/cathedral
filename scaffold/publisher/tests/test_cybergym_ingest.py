"""Tests for scaffold/publisher/cybergym_ingest.py.

The authenticated, idempotent producer-to-publisher transport for CyberGym
verified scores. Mirrors the gate structure of test_external_scores_gates.py /
test_external_scores_route_auth.py: default off, fail closed on every missing
credential, audience bound, epoch fenced, byte-identical retries idempotent.

All tests are non-writing with respect to any chain or live database: they build
a temporary SQLite Store and call the router through Starlette's TestClient.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from scaffold.publisher import cybergym_contract as contract
from scaffold.publisher import cybergym_ingest as ingest
from scaffold.publisher.store import Store

TOKEN = "cybergym-test-token"
SECRET = "cybergym-test-hmac-secret"
PRODUCER = "5CyberGymProducer"
NETWORK = "test"
NETUID = 1234
UNITS = "level_weighted_verified_solves"
EVIDENCE = "a" * 64


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{dt.microsecond // 1000:03d}Z"
    )


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "publisher.sqlite"), prefer_env_database_url=False)


def _env(monkeypatch, *, enabled=True, token=TOKEN, secret=SECRET,
         network=NETWORK, netuid=NETUID, producer_pin=PRODUCER, skew=None) -> None:
    if enabled:
        monkeypatch.setenv(ingest.INGEST_ENABLED_ENV, "1")
    else:
        monkeypatch.delenv(ingest.INGEST_ENABLED_ENV, raising=False)
    for name, value in (
        (ingest.AUTH_TOKEN_ENV, token),
        (ingest.HMAC_SECRET_ENV, secret),
        (ingest.NETWORK_ENV, network),
        (ingest.NETUID_ENV, None if netuid is None else str(netuid)),
        (ingest.PRODUCER_HOTKEY_ENV, producer_pin),
        (ingest.MAX_FUTURE_SKEW_SECS_ENV, skew),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _doc(*, source_epoch=1, scores=None, generated_at="2026-07-29T00:00:00.000Z",
         complete=True, network=NETWORK, netuid=NETUID, producer=PRODUCER,
         units=UNITS, evidence=EVIDENCE) -> dict:
    return {
        "producer_hotkey": producer,
        "network": network,
        "netuid": netuid,
        "source_epoch": source_epoch,
        "generated_at": generated_at,
        "complete": complete,
        "score_units": units,
        "scores": {"5Alice": 12.0, "5Bob": 4.0} if scores is None else scores,
        "evidence_sha256": evidence,
    }


def _body(doc: dict) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _client(store: Store) -> TestClient:
    app = FastAPI()
    app.include_router(ingest.router)
    app.dependency_overrides[ingest.get_publisher_store] = lambda: store
    return TestClient(app)


def _post(client: TestClient, doc: dict, *, token=TOKEN, secret=SECRET,
          signature=None, body=None):
    raw = body if body is not None else _body(doc)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    sig = signature if signature is not None else _sig(raw, secret)
    if sig:
        headers["X-Cathedral-Cybergym-Signature"] = sig
    return client.post("/v1/cybergym/scores", content=raw, headers=headers)


# --- default off / fail closed ------------------------------------------

def test_disabled_by_default(tmp_path, monkeypatch):
    _env(monkeypatch, enabled=False)
    r = _post(_client(_store(tmp_path)), _doc())
    assert r.status_code == 404
    assert r.json()["detail"] == "cybergym_ingest_not_enabled"


def test_missing_token_config_fails_closed(tmp_path, monkeypatch):
    _env(monkeypatch, token=None)
    r = _post(_client(_store(tmp_path)), _doc(), token=None)
    assert r.status_code == 503
    assert r.json()["detail"] == "cybergym_token_required"


def test_missing_hmac_secret_fails_closed(tmp_path, monkeypatch):
    _env(monkeypatch, secret=None)
    r = _post(_client(_store(tmp_path)), _doc())
    assert r.status_code == 503
    assert r.json()["detail"] == "cybergym_hmac_secret_required"


def test_unconfigured_audience_fails_closed(tmp_path, monkeypatch):
    _env(monkeypatch, netuid=None)
    r = _post(_client(_store(tmp_path)), _doc())
    assert r.status_code == 503
    assert r.json()["detail"] == "cybergym_audience_not_configured"


def test_no_store_wired_is_503(tmp_path, monkeypatch):
    _env(monkeypatch)
    app = FastAPI()
    app.include_router(ingest.router)
    app.dependency_overrides[ingest.get_publisher_store] = lambda: None
    r = _post(TestClient(app), _doc())
    assert r.status_code == 503
    assert r.json()["detail"] == "cybergym_store_not_configured"


# --- authentication -----------------------------------------------------

def test_bad_bearer_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    r = _post(_client(_store(tmp_path)), _doc(), token="wrong-token")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_cybergym_token"


def test_bad_hmac_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    r = _post(_client(_store(tmp_path)), _doc(), secret="wrong-secret")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_cybergym_signature"


def test_missing_hmac_header_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    r = _post(_client(_store(tmp_path)), _doc(), signature="")
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_cybergym_signature"


def test_hmac_is_over_the_exact_bytes(tmp_path, monkeypatch):
    """A signature computed over a different serialization does not pass, so a
    body cannot be mutated in flight while keeping the signature valid."""
    _env(monkeypatch)
    doc = _doc()
    other = _body(_doc(source_epoch=2))
    r = _post(_client(_store(tmp_path)), doc, signature=_sig(other))
    assert r.status_code == 401


def test_unconfigured_producer_fails_closed(tmp_path, monkeypatch):
    """The producer identity is required, not optional: the epoch fence is
    audience-scoped, so a second signer could outbid the real producer."""
    _env(monkeypatch, producer_pin=None)
    r = _post(_client(_store(tmp_path)), _doc())
    assert r.status_code == 503
    assert r.json()["detail"] == "cybergym_producer_not_configured"


def test_document_from_another_producer_is_forbidden(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    r = _post(_client(store), _doc(producer="5SomeoneElse"))
    assert r.status_code == 403
    assert r.json()["detail"] == "producer_hotkey_mismatch"
    assert store.query("SELECT * FROM cybergym_score_reports") == []


# --- audience binding ---------------------------------------------------

def test_wrong_audience_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    assert _post(client, _doc(network="finney")).status_code == 400
    assert _post(client, _doc(netuid=39)).status_code == 400
    assert store.query("SELECT * FROM cybergym_score_reports") == []


# --- malformed input ----------------------------------------------------

@pytest.mark.parametrize("doc,detail", [
    (_doc(complete=False), "complete_required"),
    (_doc(source_epoch=-1), "invalid_source_epoch"),
    (_doc(source_epoch=2**63 - 1), "invalid_source_epoch"),   # fence-poison guard
    (_doc(source_epoch=2**31), "invalid_source_epoch"),       # just over the cap
    (_doc(generated_at="not-a-time"), "invalid_generated_at"),
    (_doc(units="not valid units!"), "invalid_score_units"),
    (_doc(evidence="short"), "invalid_evidence_sha256"),
    (_doc(scores={"5Alice": -1.0}), "invalid_score"),
    (_doc(scores={"5Alice": "twelve"}), "invalid_score"),
    (_doc(scores=[]), "invalid_scores"),
    (_doc(producer=""), "invalid_producer_hotkey"),
])
def test_malformed_documents_rejected_and_nothing_persisted(
    tmp_path, monkeypatch, doc, detail
):
    _env(monkeypatch)
    store = _store(tmp_path)
    r = _post(_client(store), doc)
    assert r.status_code == 400
    assert r.json()["detail"] == detail
    assert store.query("SELECT * FROM cybergym_score_reports") == []
    assert store.query("SELECT * FROM cybergym_scores") == []


def test_unexpected_and_missing_fields_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    client = _client(_store(tmp_path))
    extra = _doc()
    extra["surprise"] = 1
    assert _post(client, extra).json()["detail"] == "unexpected_fields"
    short = _doc()
    del short["score_units"]
    assert _post(client, short).json()["detail"] == "missing_fields"


def test_invalid_json_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    r = _post(_client(_store(tmp_path)), _doc(), body=b"{not json")
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_json"


def test_body_cap_enforced_before_parsing(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv(ingest.MAX_BODY_BYTES_ENV, "1024")
    doc = _doc(scores={f"5Miner{i}": 1.0 for i in range(400)})
    r = _post(_client(_store(tmp_path)), doc)
    assert r.status_code == 413
    assert r.json()["detail"] == "cybergym_report_too_large"


def test_too_many_scores_rejected(tmp_path, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv(ingest.MAX_SCORES_ENV, "2")
    doc = _doc(scores={"5A": 1.0, "5B": 1.0, "5C": 1.0})
    r = _post(_client(_store(tmp_path)), doc)
    assert r.status_code == 400
    assert r.json()["detail"] == "too_many_scores"


# --- happy path + persistence contract ----------------------------------

def test_accepted_report_persists_header_and_rows(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    body = _body(_doc())
    r = _post(_client(store), _doc())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["accepted"] is True
    assert out["idempotent"] is False
    assert out["source_epoch"] == 1
    assert out["score_count"] == 2
    assert out["body_sha256"] == hashlib.sha256(body).hexdigest()

    headers = store.query("SELECT * FROM cybergym_score_reports")
    assert len(headers) == 1
    header = headers[0]
    assert header["network"] == NETWORK
    assert header["netuid"] == NETUID
    assert header["source_epoch"] == 1
    assert header["producer_hotkey"] == PRODUCER
    assert header["complete"] == 1
    assert header["score_units"] == UNITS
    assert header["score_count"] == 2
    assert header["generated_at_iso"] == "2026-07-29T00:00:00.000Z"
    assert header["received_at_iso"]
    assert header["report_sha256"] == out["report_sha256"]
    assert header["body_sha256"] == out["body_sha256"]
    assert header["evidence_sha256"] == EVIDENCE
    assert header["signature"].startswith("sha256=")
    assert header["authenticated_body"] == body.decode("utf-8")
    assert header["report_json"] == ingest.canonical_report_bytes(
        ingest.semantic_view(ingest.validate_report(
            _doc(), audience=(NETWORK, NETUID), producer=PRODUCER
        ))
    ).decode("utf-8")

    rows = store.query(
        "SELECT miner_hotkey, score, epoch, report_id, network, netuid "
        "FROM cybergym_scores ORDER BY miner_hotkey"
    )
    assert [(r["miner_hotkey"], r["score"], r["epoch"]) for r in rows] == [
        ("5Alice", 12.0, 1), ("5Bob", 4.0, 1),
    ]
    assert {r["report_id"] for r in rows} == {out["report_id"]}


def test_valid_wire_variants_are_normalized_and_read_verifiable(
    tmp_path, monkeypatch
):
    """A valid wire representation must not be accepted and later burned.

    ``+00:00`` and integer JSON scores are both accepted by the public
    contract. The exact raw bytes remain HMAC-verifiable while every semantic
    projection uses the normalized ``.000Z`` and float representation.
    """
    _env(monkeypatch)
    store = _store(tmp_path)
    doc = _doc(
        generated_at="2026-07-29T00:00:00+00:00",
        scores={"5Alice": 12, "5Bob": 4},
    )
    raw_body = _body(doc)

    response = _post(_client(store), doc, body=raw_body)
    assert response.status_code == 200, response.text

    header = store.query("SELECT * FROM cybergym_score_reports")[0]
    rows = {
        row["miner_hotkey"]: row["score"]
        for row in store.query(
            "SELECT miner_hotkey, score FROM cybergym_scores WHERE report_id=?",
            (header["id"],),
        )
    }
    verified = contract.verify_stored_report(
        dict(header),
        body=header["report_json"],
        authenticated_body=header["authenticated_body"],
        signature=header["signature"],
        rows=rows,
    )

    assert header["authenticated_body"] == raw_body.decode("utf-8")
    assert header["body_sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert verified["document"]["generated_at"] == "2026-07-29T00:00:00.000Z"
    assert verified["scores"] == {"5Alice": 12.0, "5Bob": 4.0}
    assert header["report_sha256"] == contract.report_digest(
        verified["document"]
    )


def test_empty_complete_report_is_accepted(tmp_path, monkeypatch):
    """"Nobody scored this epoch" is a legal complete statement: the header
    persists with zero rows so the mechanism contributes nothing."""
    _env(monkeypatch)
    store = _store(tmp_path)
    r = _post(_client(store), _doc(scores={}))
    assert r.status_code == 200
    assert r.json()["score_count"] == 0
    assert len(store.query("SELECT * FROM cybergym_score_reports")) == 1
    assert store.query("SELECT * FROM cybergym_scores") == []


# --- idempotency + epoch fence ------------------------------------------

def test_identical_retry_is_idempotent(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    first = _post(client, _doc())
    second = _post(client, _doc())
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["idempotent"] is False
    assert second.json()["idempotent"] is True
    assert first.json()["report_id"] == second.json()["report_id"]
    assert len(store.query("SELECT * FROM cybergym_score_reports")) == 1
    assert len(store.query("SELECT * FROM cybergym_scores")) == 2


def test_exact_retry_backfills_pre_split_noncanonical_body(tmp_path, monkeypatch):
    """A valid 0048 row remains readable and an exact retry upgrades its split.

    Before 0049, report_json held the exact authenticated serialization. That
    can contain accepted-but-noncanonical values such as a ``+00:00`` timestamp
    and integer scores. The migration marker is an empty authenticated_body.
    """
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    doc = _doc(
        generated_at="2026-07-29T00:00:00+00:00",
        scores={"5Alice": 12, "5Bob": 4},
    )
    raw_body = json.dumps(doc, indent=2, sort_keys=False).encode("utf-8")
    first = _post(client, doc, body=raw_body)
    assert first.status_code == 200, first.text

    # Recreate the exact persisted shape migration 0049 sees.
    store.write(
        lambda conn: conn.execute(
            "UPDATE cybergym_score_reports "
            "SET report_json=authenticated_body, authenticated_body=''"
        )
    )
    legacy = store.query("SELECT * FROM cybergym_score_reports")[0]
    rows = {
        row["miner_hotkey"]: row["score"]
        for row in store.query(
            "SELECT miner_hotkey, score FROM cybergym_scores WHERE report_id=?",
            (legacy["id"],),
        )
    }
    verified_legacy = contract.verify_stored_report(
        dict(legacy),
        body=legacy["report_json"],
        authenticated_body=legacy["authenticated_body"],
        signature=legacy["signature"],
        rows=rows,
    )
    assert verified_legacy["document"]["generated_at"] == (
        "2026-07-29T00:00:00.000Z"
    )
    assert verified_legacy["scores"] == {"5Alice": 12.0, "5Bob": 4.0}

    retry = _post(client, doc, body=raw_body)
    assert retry.status_code == 200, retry.text
    assert retry.json()["idempotent"] is True

    upgraded = store.query("SELECT * FROM cybergym_score_reports")[0]
    assert upgraded["authenticated_body"] == raw_body.decode("utf-8")
    assert upgraded["report_json"] == contract.canonical_report_bytes(
        verified_legacy["document"]
    ).decode("utf-8")
    assert upgraded["report_json"] != upgraded["authenticated_body"]
    verified_upgraded = contract.verify_stored_report(
        dict(upgraded),
        body=upgraded["report_json"],
        authenticated_body=upgraded["authenticated_body"],
        signature=upgraded["signature"],
        rows=rows,
    )
    assert verified_upgraded == verified_legacy


def test_conflicting_document_at_stored_epoch_is_409(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    assert _post(client, _doc()).status_code == 200
    r = _post(client, _doc(scores={"5Attacker": 999.0}))
    assert r.status_code == 409
    assert r.json()["detail"] == "epoch_conflict"
    rows = store.query("SELECT miner_hotkey FROM cybergym_scores ORDER BY miner_hotkey")
    assert [r["miner_hotkey"] for r in rows] == ["5Alice", "5Bob"]


def test_epoch_rollback_is_409(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    assert _post(client, _doc(source_epoch=5)).status_code == 200
    r = _post(client, _doc(source_epoch=4))
    assert r.status_code == 409
    assert r.json()["detail"] == "epoch_too_old"
    assert ingest.latest_complete_report(
        store, audience=(NETWORK, NETUID)
    )["source_epoch"] == 5


def test_newer_epoch_supersedes(tmp_path, monkeypatch):
    _env(monkeypatch)
    store = _store(tmp_path)
    client = _client(store)
    assert _post(client, _doc(source_epoch=1)).status_code == 200
    assert _post(
        client, _doc(source_epoch=2, scores={"5Alice": 1.0})
    ).status_code == 200
    latest = ingest.latest_complete_report(store, audience=(NETWORK, NETUID))
    assert latest["source_epoch"] == 2
    assert latest["score_count"] == 1
    rows = store.query(
        "SELECT miner_hotkey FROM cybergym_scores WHERE report_id=? ",
        (latest["report_id"],),
    )
    assert [r["miner_hotkey"] for r in rows] == ["5Alice"]


def test_latest_complete_report_is_none_on_empty_state(tmp_path):
    assert ingest.latest_complete_report(
        _store(tmp_path), audience=(NETWORK, NETUID)
    ) is None


def test_store_report_requires_authenticated_body_digest(tmp_path, monkeypatch):
    """A report that never passed the authenticated route cannot be persisted:
    the body digest is a hard precondition, not an optional annotation."""
    _env(monkeypatch)
    store = _store(tmp_path)
    report = ingest.validate_report(
        _doc(), audience=(NETWORK, NETUID), producer=PRODUCER,
    )
    with pytest.raises(ingest.CybergymIngestError) as exc:
        ingest.store_report(store, report)
    assert exc.value.reason == "authenticated_body_digest_required"
    assert store.query("SELECT * FROM cybergym_score_reports") == []


# --- future-skew bound ---------------------------------------------------

def test_future_dated_report_is_rejected(tmp_path, monkeypatch):
    """A future-dated report would compute a negative age in the adapter and so
    stay inside every freshness window for as long as it sat in the table."""
    _env(monkeypatch)
    store = _store(tmp_path)
    r = _post(_client(store), _doc(generated_at="2099-01-01T00:00:00.000Z"))
    assert r.status_code == 400
    assert r.json()["detail"] == "report_in_future"
    assert store.query("SELECT * FROM cybergym_score_reports") == []


def test_small_clock_skew_is_tolerated(tmp_path, monkeypatch):
    _env(monkeypatch, skew="300")
    ahead = datetime.now(timezone.utc) + timedelta(seconds=60)
    r = _post(_client(_store(tmp_path)), _doc(generated_at=_iso(ahead)))
    assert r.status_code == 200


def test_skew_bound_is_configurable(tmp_path, monkeypatch):
    _env(monkeypatch, skew="5")
    ahead = datetime.now(timezone.utc) + timedelta(seconds=60)
    r = _post(_client(_store(tmp_path)), _doc(generated_at=_iso(ahead)))
    assert r.status_code == 400
    assert r.json()["detail"] == "report_in_future"


def test_default_skew_matches_the_external_scores_convention(monkeypatch):
    monkeypatch.delenv(ingest.MAX_FUTURE_SKEW_SECS_ENV, raising=False)
    assert ingest.max_future_skew_secs() == 120.0
    monkeypatch.setenv(ingest.MAX_FUTURE_SKEW_SECS_ENV, "nonsense")
    assert ingest.max_future_skew_secs() == 120.0


# --- streaming body bound ------------------------------------------------

class _StubStream:
    """Minimal Request stand-in: a body delivered in chunks, no Content-Length.

    ``read_bounded_body`` must abort mid-stream, so the test also records how
    many chunks were pulled.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.consumed = 0

    async def stream(self):
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


def test_bounded_read_aborts_before_buffering_everything():
    stub = _StubStream([b"x" * 1024] * 64)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(ingest.read_bounded_body(stub, 4096))
    assert exc.value.status_code == 413
    assert exc.value.detail == "cybergym_report_too_large"
    # 4 chunks fit, the 5th trips the bound: the other 59 were never buffered.
    assert stub.consumed == 5


def test_bounded_read_returns_a_body_under_the_limit():
    stub = _StubStream([b"ab", b"cd", b""])
    body = asyncio.run(ingest.read_bounded_body(stub, 4096))
    assert body == b"abcd"


def test_oversized_body_with_no_content_length_is_rejected(tmp_path, monkeypatch):
    """The route path: a client that omits Content-Length still cannot exceed
    the bound, because the stream itself is bounded."""
    _env(monkeypatch)
    monkeypatch.setenv(ingest.MAX_BODY_BYTES_ENV, "1024")
    store = _store(tmp_path)
    doc = _doc(scores={f"5Miner{i}": 1.0 for i in range(400)})
    raw = _body(doc)
    assert len(raw) > 1024
    client = _client(store)
    r = client.request(
        "POST",
        "/v1/cybergym/scores",
        content=iter([raw[:512], raw[512:]]),  # chunked: no Content-Length
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "X-Cathedral-Cybergym-Signature": _sig(raw),
        },
    )
    assert r.status_code == 413
    assert r.json()["detail"] == "cybergym_report_too_large"
    assert store.query("SELECT * FROM cybergym_score_reports") == []


# --- the audience-scoped fence contract ---------------------------------

def test_producer_rotation_must_continue_above_the_stored_epoch(tmp_path, monkeypatch):
    """Documents the deliberate cost of an audience-scoped fence: a replacement
    producer key does not reset the epoch counter."""
    _env(monkeypatch)
    store = _store(tmp_path)
    assert _post(_client(store), _doc(source_epoch=7)).status_code == 200

    rotated = "5RotatedProducer"
    monkeypatch.setenv(ingest.PRODUCER_HOTKEY_ENV, rotated)
    client = _client(store)
    restart = _post(client, _doc(source_epoch=1, producer=rotated))
    assert restart.status_code == 409
    assert restart.json()["detail"] == "epoch_too_old"
    assert _post(client, _doc(source_epoch=8, producer=rotated)).status_code == 200
    latest = ingest.latest_complete_report(store, audience=(NETWORK, NETUID))
    assert latest["source_epoch"] == 8
    assert latest["producer_hotkey"] == rotated


# --- non-ASCII credential bytes ------------------------------------------

def _raw_asgi_post(app, headers: list[tuple[bytes, bytes]], body: bytes) -> int:
    """Drive the ASGI app directly, bypassing httpx's ASCII header validation."""
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/cybergym/scores",
        "raw_path": b"/v1/cybergym/scores",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    starts = [m for m in messages if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else 0


@pytest.mark.parametrize("header", [b"authorization", b"x-cathedral-cybergym-signature"])
def test_non_ascii_credential_bytes_are_401_not_500(tmp_path, monkeypatch, header):
    """hmac.compare_digest raises TypeError on non-ASCII str, and Starlette
    decodes headers as latin-1, so an unauthenticated caller used to get a 500."""
    _env(monkeypatch)
    store = _store(tmp_path)
    app = FastAPI()
    app.include_router(ingest.router)
    app.dependency_overrides[ingest.get_publisher_store] = lambda: store

    raw = _body(_doc())
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(raw)).encode()),
        (b"authorization", f"Bearer {TOKEN}".encode()),
        (b"x-cathedral-cybergym-signature", _sig(raw).encode()),
    ]
    headers = [(k, b"Bearer tok\xc3\xa9" if k == header else v) for k, v in headers]
    status = _raw_asgi_post(app, headers, raw)
    assert status == 401
    assert store.query("SELECT * FROM cybergym_score_reports") == []


def test_constant_time_equal_handles_non_ascii():
    assert ingest._constant_time_equal("tok", "tok") is True
    assert ingest._constant_time_equal("toké", "tok") is False
    assert ingest._constant_time_equal("tok", "toké") is False
