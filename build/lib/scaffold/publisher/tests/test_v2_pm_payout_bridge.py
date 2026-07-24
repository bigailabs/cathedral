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
from scaffold.publisher import weights
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store

SIGNING_KEY_HEX = "22" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_plus_secs(secs: int) -> str:
    from datetime import timedelta
    dt = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _build(tmp_path, monkeypatch, *, bridge: bool, lazy: bool = False,
           collapse: bool = False, weight_t1: str | None = None):
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
    if collapse:
        monkeypatch.setenv("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", "1")
    else:
        monkeypatch.delenv("CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE", raising=False)
    if weight_t1 is not None:
        monkeypatch.setenv("CATHEDRAL_V2_PERMINER_WEIGHT_T1", weight_t1)
    else:
        monkeypatch.delenv("CATHEDRAL_V2_PERMINER_WEIGHT_T1", raising=False)
    # Bridge requires main store == V2 store (split DB hard-refuses), matching
    # the production sandbox layout.
    if bridge:
        monkeypatch.delenv("CATHEDRAL_V2_DB_PATH", raising=False)
        db = str(tmp_path / "pub.sqlite")
        app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
        v2_store = Store(db, prefer_env_database_url=False)
    else:
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


def test_bridge_with_split_v2_db_refuses_to_boot(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_PM_PAYOUT_BRIDGE", "1")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2-split.sqlite"))
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    with pytest.raises(RuntimeError, match="payout store"):
        build_app(database_path=str(tmp_path / "pub.sqlite"),
                  signing_key_hex=SIGNING_KEY_HEX)


def test_stale_epoch_rejected_at_cnf_mint_and_admit(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True)
    client = TestClient(app)
    kp = _keypair("//StaleEpoch")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    item = page.json()["items"][0]
    epoch = int(item["epoch"])

    # CNF mint: an archived (epoch-2) challenge id must never re-token.
    stale_cid = item["challenge_id"].replace(f"-e{epoch}-", f"-e{epoch - 2}-")
    assert stale_cid != item["challenge_id"]
    r = client.get(
        "/v2/synthetic-boolean/per-miner/cnf"
        f"?challenge_id={stale_cid}&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp))
    assert r.status_code == 410, r.text
    assert "per_miner_challenge_expired" in r.text

    # Admit: a token carrying a stale epoch is refused before any event write.
    stale_token = v2_bitset_submit.mint_submit_token(
        secret="test-v2-submit-token-secret",
        miner_hotkey=kp.ss58_address,
        challenge_id=stale_cid,
        epoch=epoch - 2,
        tier=int(item["tier"]),
        seq=int(item["seq"]),
        nvars=int(item["n_vars"]),
        cnf_sha256=str(item["cnf_sha256"]),
        expires_at=_iso_plus_secs(300),
    )
    body = {
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": _FAMILY,
        "challenge_id": stale_cid,
        "submit_token": stale_token,
        "assignment_encoding": "bitset/v1",
        "assignment_b64": base64.b64encode(
            b"\x00" * ((int(item["n_vars"]) + 7) // 8)).decode("ascii"),
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
    assert r.status_code == 410, r.text
    assert _payout_rows(v2_store, kp.ss58_address) == []


def test_stale_epoch_event_rejected_at_verify_before_payout(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True)
    client = TestClient(app)
    kp = _keypair("//StaleVerify")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    item = page.json()["items"][0]
    epoch = int(item["epoch"])
    stale_epoch = epoch - 2
    stale_cid = item["challenge_id"].replace(f"-e{epoch}-", f"-e{stale_epoch}-")

    # A stale 'received' event that slipped past admit must terminal-reject at
    # verify without ever reaching the payout bridge.
    def _insert(conn):
        conn.execute(
            "INSERT INTO v2_submit_events(id, idempotency_key, miner_hotkey, "
            "challenge_id, card_id, epoch, tier, seq, cnf_sha256, "
            "assignment_encoding, assignment_sha256, assignment_b64, status, "
            "eligibility_status, received_at_iso, submitted_at, signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("stale-event-1", "stale-idem-1", kp.ss58_address, stale_cid,
             _FAMILY, stale_epoch, int(item["tier"]), int(item["seq"]),
             str(item["cnf_sha256"]), "bitset/v1", "0" * 64,
             base64.b64encode(
                 b"\x00" * ((int(item["n_vars"]) + 7) // 8)).decode("ascii"), "received",
             "unknown_beta", _now_iso(), _now_iso(), "sig"))

    v2_store.write(_insert)
    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_REJECTED
    assert results[0]["reason"] == "per_miner_challenge_expired"
    assert _payout_rows(v2_store, kp.ss58_address) == []


def test_bridge_records_exact_v2_weight(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True, weight_t1="3.5")
    client = TestClient(app)
    kp = _keypair("//ExactWeight")

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    item = page.json()["items"][0]
    assert int(item["tier"]) == 1
    _submit_bitset(client, kp, item, item["submit_token"])

    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED
    assert float(results[0]["weighted_score"]) == 3.5

    rows = _payout_rows(v2_store, kp.ss58_address)
    assert len(rows) == 1
    # The payout row must carry the EXACT verifier weight (V2-prefixed env),
    # not a recompute under legacy/default env (which would be 1.0 here).
    assert float(rows[0]["difficulty_weight"]) == 3.5


def test_identity_alignment_under_coldkey_collapse(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, bridge=True, collapse=True)
    client = TestClient(app)
    kp = _keypair("//CollapseIdentity")
    coldkey = "5ColdkeyIdentityForCollapseTest111111111111111"

    def _map(conn):
        conn.execute(
            "INSERT OR IGNORE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
            "VALUES (?, ?, ?)", (kp.ss58_address, coldkey, _now_iso()))

    v2_store.write(_map)  # bridge mode: main store == v2 store

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    payload = page.json()
    # V1 parity: instances derive from the coldkey identity...
    assert payload["assignment_identity"] == coldkey
    item = payload["items"][0]
    with v2_pipeline.v2_pm_env():
        expected_cid = pm.instance_id(
            coldkey, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assert item["challenge_id"] == expected_cid

    # ...the solve verifies against the identity-derived CNF, and the payout
    # row keys the RAW signing hotkey with the identity-derived challenge id
    # (identical PK to what the V1 accept path would write -- no double pay).
    def _solve_and_submit():
        with v2_pipeline.v2_pm_env():
            _cid, _cnf, assignment = pm.generate_instance(
                coldkey, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
        assignment_b64 = base64.b64encode(
            v2_pipeline.encode_bitset_assignment(assignment)).decode("ascii")
        body = {
            "schema": "cathedral.v2.submit_bitset.v1",
            "card_id": _FAMILY,
            "challenge_id": item["challenge_id"],
            "submit_token": item["submit_token"],
            "assignment_encoding": "bitset/v1",
            "assignment_b64": assignment_b64,
        }
        submitted_at = _now_iso()
        submit = v2_bitset_submit.normalize_submit_body(
            body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at,
            card_id=_FAMILY)
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

    _solve_and_submit()
    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED

    rows = _payout_rows(v2_store, kp.ss58_address)
    assert len(rows) == 1
    assert rows[0]["challenge_id"] == expected_cid
    assert rows[0]["miner_hotkey"] == kp.ss58_address


def test_launch_profile_v2_converged_implies_unified_protocol(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    for name in ("CATHEDRAL_V2_ENABLED", "CATHEDRAL_V2_SUBMIT_BITSET_ENABLED",
                 "CATHEDRAL_V2_LAZY_ISSUANCE", "CATHEDRAL_V2_PM_PAYOUT_BRIDGE",
                 "CATHEDRAL_V2_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "profile-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "4")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_PERMINER_SCORING_MODE", "pm_primary")
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    v2_store = Store(db, prefer_env_database_url=False)
    client = TestClient(app)
    kp = _keypair("//ProfileConverged")

    assert v2_pipeline.pm_payout_bridge_enabled() is True

    page = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp))
    assert page.status_code == 200, page.text
    payload = page.json()
    assert payload["issuance"] == "lazy"
    item = payload["items"][0]
    assert "submit_token" not in item

    cnf = client.get(
        "/v2/synthetic-boolean/per-miner/cnf"
        f"?challenge_id={item['challenge_id']}&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp))
    assert cnf.status_code == 200
    token = cnf.headers.get("x-cathedral-submit-token")
    assert token

    _submit_bitset(client, kp, item, token)
    results = v2_pipeline.process_bitset_batch(v2_store)
    assert results and results[0]["status"] == v2_pipeline.STATUS_VERIFIED
    assert len(_payout_rows(v2_store, kp.ss58_address)) == 1
    # Route safety/env pinning keeps the legacy V1 per-miner route flag unset,
    # but converged V2 bridged solves must still feed the signed vector.
    assert pm.perminer_enabled() is False
    scores = weights.compose_scores(v2_store)
    assert scores == {kp.ss58_address: 1.0}
    vector = weights.build_signed_vector(v2_store, signing_key_hex=SIGNING_KEY_HEX)
    assert vector["policy_metadata"]["perminer"]["enabled"] is True
    assert vector["policy_metadata"]["perminer"]["primary_live"] is True
    assert vector["policy_metadata"]["score_source"] == "pm_primary"
    assert vector["weights"] == [{"miner_hotkey": kp.ss58_address, "weight": 1.0}]


def test_launch_profile_contradiction_fails_closed(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-secret")
    monkeypatch.delenv("CATHEDRAL_V2_DB_PATH", raising=False)
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "0")
    with pytest.raises(RuntimeError, match="fail-closed"):
        build_app(database_path=str(tmp_path / "pub.sqlite"),
                  signing_key_hex=SIGNING_KEY_HEX)
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "split.sqlite"))
    with pytest.raises(RuntimeError, match="fail-closed"):
        build_app(database_path=str(tmp_path / "pub.sqlite"),
                  signing_key_hex=SIGNING_KEY_HEX)


def test_launch_profile_requires_pinned_eval_signing_key_for_env_boot(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("CATHEDRAL_LAUNCH_PROFILE", "v2-converged")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "profile-test-seed")
    monkeypatch.delenv("CATHEDRAL_EVAL_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CATHEDRAL_V2_DB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="CATHEDRAL_EVAL_SIGNING_KEY"):
        build_app(database_path=str(tmp_path / "pub.sqlite"))


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
