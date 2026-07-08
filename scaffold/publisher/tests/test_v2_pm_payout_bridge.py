"""V2 -> per_miner_solves payout bridge + lazy issuance (V1/V2 convergence).

Covers:
  * CATHEDRAL_V2_PM_PAYOUT_BRIDGE=1: a VERIFIED bitset event records an
    idempotent per_miner_solves row (same difficulty_weight the eval used),
    so the existing pm_primary scoring pays V2 submits unchanged.
  * bridge default OFF: verify stays shadow-only (no payout row).
  * CATHEDRAL_V2_LAZY_ISSUANCE=1: the challenges page returns descriptors only
    (no per-item CNF generation / token minting); the CNF fetch mints the
    token in headers; the full lazy fetch -> solve -> submit -> verify loop
    still lands the payout row when the bridge is on.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher import per_miner as pm
from scaffold.publisher import v2_bitset_submit
from scaffold.publisher import v2_pipeline
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store

SIGNING_KEY_HEX = "22" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _build(tmp_path, monkeypatch, *, bridge: bool, lazy: bool = False):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "payout-bridge-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "4")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    if bridge:
        monkeypatch.setenv("CATHEDRAL_V2_PM_PAYOUT_BRIDGE", "1")
    else:
        monkeypatch.delenv("CATHEDRAL_V2_PM_PAYOUT_BRIDGE", raising=False)
    if lazy:
        monkeypatch.setenv("CATHEDRAL_V2_LAZY_ISSUANCE", "1")
    else:
        monkeypatch.delenv("CATHEDRAL_V2_LAZY_ISSUANCE", raising=False)
    app = build_app(
        database_path=str(tmp_path / "pub.sqlite"), signing_key_hex=SIGNING_KEY_HEX)
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    return app, v2_store


def _read_headers(kp) -> dict[str, str]:
    ts = _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE, card_id=_FAMILY, miner_hotkey=kp.ss58_address,
        submitted_at=ts, challenge_id="", dimacs_solution_sha256="",
    )
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _submit_bitset(client, kp, item, submit_token):
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)).decode("ascii")
    body = {
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": submit_token,
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    submitted_at = _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY)
    sig = base64.b64encode(
        kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    r = client.post(
        "/v2/agents/submit-bitset", json=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
        },
    )
    assert r.status_code == 202, r.text
    return r.json()


def _payout_rows(v2_store, hotkey):
    return v2_store.query(
        "SELECT * FROM per_miner_solves WHERE miner_hotkey=?", (hotkey,))


def test_verified_bitset_event_bridges_to_per_miner_solves(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True)
    client = TestClient(app)
    kp = _keypair("//PayoutBridgeOn")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    assert page.json()["issuance"] == "eager"
    item = page.json()["items"][0]
    _submit_bitset(client, kp, item, item["submit_token"])

    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED
    assert results[0]["pm_payout_bridged"] is True

    rows = _payout_rows(v2_store, kp.ss58_address)
    assert len(rows) == 1
    row = rows[0]
    assert row["challenge_id"] == item["challenge_id"]
    assert int(row["verified"]) == 1
    with v2_pipeline.v2_pm_env():
        assert float(row["difficulty_weight"]) == pm.weight_for(int(item["tier"]))

    # Idempotent: re-verifying the same event cannot double-pay.
    assert pm.record_perminer_solve(
        v2_store, kp.ss58_address, int(item["epoch"]), item["challenge_id"],
        int(item["tier"]), int(item["seq"]), True) is False
    assert len(_payout_rows(v2_store, kp.ss58_address)) == 1


def test_bridge_default_off_stays_shadow_only(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=False)
    client = TestClient(app)
    kp = _keypair("//PayoutBridgeOff")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    item = page.json()["items"][0]
    _submit_bitset(client, kp, item, item["submit_token"])

    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED
    assert results[0]["pm_payout_bridged"] is False
    assert _payout_rows(v2_store, kp.ss58_address) == []


def test_lazy_issuance_mints_at_cnf_fetch_and_bridges(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True, lazy=True)
    client = TestClient(app)
    kp = _keypair("//LazyIssuance")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=2",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    payload = page.json()
    assert payload["issuance"] == "lazy"
    assert payload["items"], "lazy page must still list descriptors"
    for it in payload["items"]:
        assert "submit_token" not in it
        assert it["token_source"] == "cnf_fetch"
    item = payload["items"][0]

    cnf = client.get(
        "/v2/synthetic-boolean/per-miner/cnf"
        f"?challenge_id={item['challenge_id']}&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp))
    assert cnf.status_code == 200, cnf.text
    header_token = cnf.headers.get("x-cathedral-submit-token")
    assert header_token, "lazy issuance must mint the token at CNF fetch"

    _submit_bitset(client, kp, item, header_token)
    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED

    rows = _payout_rows(v2_store, kp.ss58_address)
    assert len(rows) == 1
    assert int(rows[0]["verified"]) == 1
