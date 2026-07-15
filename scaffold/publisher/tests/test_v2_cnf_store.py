"""V2 per-miner CNF store — bake once, verify reads the stored CNF.

Covers:
  * pure put/get/purge_older_than behaviour against a Store (SQLite).
  * sha-gating (a mismatched expected_sha256 is treated as a miss).
  * both env kill-switches (CATHEDRAL_V2_CNF_STORE_READ / _WRITE).
  * put()/get() never raise, even when the underlying store blows up.
  * end-to-end: the V2 verify worker (`v2_pipeline.process_batch` /
    `verify_one`) reads a pre-baked CNF (cache hit, no regeneration) and,
    on a miss, falls back to the EXACT existing regeneration path and
    backfills the store so the next event for the same challenge_id hits.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from scaffold.publisher import per_miner as pm
from scaffold.publisher import solution_manifest
from scaffold.publisher import v2_cnf_store
from scaffold.publisher import v2_pipeline
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store

import base64

SIGNING_KEY_HEX = "22" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _iso_hours_ago(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# Pure store tests — no app, just Store + v2_cnf_store.
# ---------------------------------------------------------------------------

def test_put_get_roundtrip(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    cnf_text = "p cnf 2 1\n1 -2 0\n"
    cid = "pm-t1-e1-deadbeefdeadbeefdeadbeef"
    assert v2_cnf_store.put(store, cid, cnf_text) is True
    assert v2_cnf_store.get(store, cid) == cnf_text


def test_get_sha_mismatch_is_treated_as_miss(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    cnf_text = "p cnf 2 1\n1 -2 0\n"
    cid = "pm-t1-e1-deadbeefdeadbeefdeadbeef"
    v2_cnf_store.put(store, cid, cnf_text)

    assert v2_cnf_store.get(store, cid, expected_sha256="0" * 64) is None

    correct_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()
    assert v2_cnf_store.get(store, cid, expected_sha256=correct_sha) == cnf_text


def test_get_missing_row_is_none(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    assert v2_cnf_store.get(store, "pm-t1-e1-doesnotexist0000000000") is None


def test_read_kill_switch_off_returns_none_even_when_present(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    cid = "pm-t1-e1-deadbeefdeadbeefdeadbeef"
    v2_cnf_store.put(store, cid, "p cnf 1 1\n1 0\n")
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_READ", "0")
    assert v2_cnf_store.get(store, cid) is None


def test_write_kill_switch_off_no_row_written(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_WRITE", "0")
    cid = "pm-t1-e1-deadbeefdeadbeefdeadbeef"
    assert v2_cnf_store.put(store, cid, "p cnf 1 1\n1 0\n") is False
    assert store.query("SELECT * FROM v2_cnf_store WHERE challenge_id=?", (cid,)) == []


def test_put_failure_is_swallowed_never_raises(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)

    def _boom(fn):
        raise RuntimeError("db is down")

    monkeypatch.setattr(store, "write", _boom)
    assert v2_cnf_store.put(store, "pm-t1-e1-x", "p cnf 1 1\n1 0\n") is False


def test_get_failure_is_swallowed_never_raises(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)

    def _boom(sql, params=()):
        raise RuntimeError("db is down")

    monkeypatch.setattr(store, "query", _boom)
    assert v2_cnf_store.get(store, "pm-t1-e1-x") is None


def test_purge_deletes_only_rows_older_than_cutoff(tmp_path):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    old_cid = "pm-t1-e1-old000000000000000000000"
    new_cid = "pm-t1-e1-new000000000000000000000"
    v2_cnf_store.put(store, old_cid, "p cnf 1 1\n1 0\n")
    v2_cnf_store.put(store, new_cid, "p cnf 1 1\n-1 0\n")

    def _backdate(conn):
        conn.execute(
            "UPDATE v2_cnf_store SET created_at_iso=? WHERE challenge_id=?",
            (_iso_hours_ago(48), old_cid),
        )

    store.write(_backdate)

    deleted = v2_cnf_store.purge_older_than(store, hours=24)
    assert deleted == 1
    assert store.query("SELECT * FROM v2_cnf_store WHERE challenge_id=?", (old_cid,)) == []
    assert len(store.query("SELECT * FROM v2_cnf_store WHERE challenge_id=?", (new_cid,))) == 1


def test_maybe_purge_older_than_throttles_to_one_call(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s.sqlite"), prefer_env_database_url=False)
    monkeypatch.setattr(v2_cnf_store, "_last_purge_at", 0.0)
    calls = []
    monkeypatch.setattr(
        v2_cnf_store, "purge_older_than",
        lambda store, hours=24: (calls.append(1), 0)[1],
    )
    v2_cnf_store.maybe_purge_older_than(store, hours=24, min_interval_secs=600.0)
    v2_cnf_store.maybe_purge_older_than(store, hours=24, min_interval_secs=600.0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Verify-path integration — the real V2 verify worker against a built app.
# ---------------------------------------------------------------------------

def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _build(tmp_path, monkeypatch, *, submit_bitset_enabled: bool):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_UPLOAD_ENABLED", "true")
    monkeypatch.setenv(
        "CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true" if submit_bitset_enabled else "false")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "cnf-store-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "8")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    return app, v2_store


def _read_headers(kp, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
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


def _fetch_item(client, kp) -> dict:
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200, board.text
    return board.json()["items"][0]


def _dimacs_solution_text(assignment: list[int]) -> str:
    return "s SATISFIABLE\nv " + " ".join(str(lit) for lit in assignment) + " 0\n"


def _upload_blob(client, kp, body: bytes) -> tuple[str, str]:
    sha = hashlib.sha256(body).hexdigest()
    submitted_at = _now_iso()
    msg = solution_manifest.canonical_blob_upload_bytes(
        miner_hotkey=kp.ss58_address, submitted_at=submitted_at,
        blob_sha256=sha, blob_bytes=len(body), kind="solution")
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    r = client.post(
        "/v2/blobs/solutions",
        content=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
            "X-Cathedral-Blob-Sha256": sha,
            "Content-Type": "application/octet-stream",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["cid"], sha


def _submit_manifest(client, kp, *, challenge_id, solution_cid, solution_sha256, cnf_sha256):
    submitted_at = _now_iso()
    body = {
        "schema": solution_manifest.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": challenge_id,
        "assignment_encoding": "dimacs/v1",
        "solution_cid": solution_cid,
        "solution_sha256": solution_sha256,
        "cnf_sha256": cnf_sha256,
    }
    manifest = solution_manifest.normalize_manifest(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY)
    msg = solution_manifest.canonical_manifest_bytes(manifest)
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    r = client.post(
        "/v2/agents/submit-manifest",
        json=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
        },
    )
    assert r.status_code in (200, 202), r.text
    return r.json()


def test_mint_time_and_bitset_verify_bake_wire_the_store(tmp_path, monkeypatch):
    """Challenge-list mint and async bitset verify land a row keyed by
    challenge_id, matching the CNF that was generated. Thin submit itself stays
    cheap and does not regenerate/bake the CNF."""
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfStoreMintWrite")
    item = _fetch_item(client, kp)

    with v2_pipeline.v2_pm_env():
        _cid, cnf_text, _assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))

    baked = v2_cnf_store.get(v2_store, item["challenge_id"], expected_sha256=item["cnf_sha256"])
    assert baked == cnf_text

    # Delete the row to isolate the bitset verify worker write site, then
    # re-derive an assignment and submit via the thin bitset path.
    def _wipe(conn):
        conn.execute(
            "DELETE FROM v2_cnf_store WHERE challenge_id=?", (item["challenge_id"],))

    v2_store.write(_wipe)
    assert v2_cnf_store.get(v2_store, item["challenge_id"]) is None

    with v2_pipeline.v2_pm_env():
        _cid2, _cnf2, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)
    ).decode("ascii")
    body = {
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    from scaffold.publisher import v2_bitset_submit
    submitted_at = _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY)
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    r = client.post(
        "/v2/agents/submit-bitset", json=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "received"

    assert v2_cnf_store.get(v2_store, item["challenge_id"]) is None

    results = v2_pipeline.process_bitset_batch(
        v2_store, worker_id="test-bitset-bake", batch_size=8, lock_secs=60)
    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]

    rebaked = v2_cnf_store.get(v2_store, item["challenge_id"], expected_sha256=item["cnf_sha256"])
    assert rebaked == cnf_text


def test_verify_worker_hits_cache_when_prebaked(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfStoreHit")
    item = _fetch_item(client, kp)  # mint-time bake happens here

    with v2_pipeline.v2_pm_env():
        _cid, cnf_text, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))

    baked = v2_cnf_store.get(v2_store, item["challenge_id"], expected_sha256=item["cnf_sha256"])
    assert baked == cnf_text  # confirm the store is actually populated

    solution_text = _dimacs_solution_text(assignment)
    cid_blob, sha = _upload_blob(client, kp, solution_text.encode("utf-8"))
    receipt = _submit_manifest(
        client, kp, challenge_id=item["challenge_id"], solution_cid=cid_blob,
        solution_sha256=sha, cnf_sha256=item["cnf_sha256"])
    assert receipt["status"] == "received"

    before = v2_pipeline.cnf_store_metrics()
    results = v2_pipeline.process_batch(
        v2_store, app.state.v2_blob_store, worker_id="test-hit", batch_size=8, lock_secs=60)
    after = v2_pipeline.cnf_store_metrics()

    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]
    assert after["cnf_store_hits"] == before["cnf_store_hits"] + 1
    assert after["cnf_store_misses"] == before["cnf_store_misses"]


def test_bitset_verify_worker_hits_cache_when_prebaked(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfStoreBitsetHit")
    item = _fetch_item(client, kp)  # mint-time bake happens here

    with v2_pipeline.v2_pm_env():
        _cid, cnf_text, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))

    baked = v2_cnf_store.get(
        v2_store, item["challenge_id"], expected_sha256=item["cnf_sha256"])
    assert baked == cnf_text

    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)
    ).decode("ascii")
    body = {
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    from scaffold.publisher import v2_bitset_submit
    submitted_at = _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY)
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    r = client.post(
        "/v2/agents/submit-bitset", json=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "received"

    def _boom(*args, **kwargs):
        raise AssertionError("generate_instance called despite a prebaked bitset row")

    monkeypatch.setattr(pm, "generate_instance", _boom)

    before = v2_pipeline.cnf_store_metrics()
    results = v2_pipeline.process_bitset_batch(
        v2_store, worker_id="test-bitset-hit", batch_size=8, lock_secs=60)
    after = v2_pipeline.cnf_store_metrics()

    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]
    assert after["cnf_store_hits"] == before["cnf_store_hits"] + 1
    assert after["cnf_store_misses"] == before["cnf_store_misses"]


def test_verify_worker_falls_back_and_backfills_on_cache_miss(tmp_path, monkeypatch):
    # submit_bitset disabled so the challenge-list mint-time write never runs —
    # the store starts empty for this challenge_id.
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=False)
    client = TestClient(app)
    kp = _keypair("//CnfStoreMiss")
    item = _fetch_item(client, kp)
    assert v2_cnf_store.get(v2_store, item["challenge_id"]) is None

    with v2_pipeline.v2_pm_env():
        cid, cnf_text, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assert cid == item["challenge_id"]
    cnf_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()

    solution_text = _dimacs_solution_text(assignment)
    cid_blob, sha = _upload_blob(client, kp, solution_text.encode("utf-8"))
    receipt = _submit_manifest(
        client, kp, challenge_id=item["challenge_id"], solution_cid=cid_blob,
        solution_sha256=sha, cnf_sha256=cnf_sha)
    assert receipt["status"] == "received"

    before = v2_pipeline.cnf_store_metrics()
    results = v2_pipeline.process_batch(
        v2_store, app.state.v2_blob_store, worker_id="test-miss", batch_size=8, lock_secs=60)
    after = v2_pipeline.cnf_store_metrics()

    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]
    assert after["cnf_store_misses"] == before["cnf_store_misses"] + 1
    assert after["cnf_store_hits"] == before["cnf_store_hits"]

    # Backfilled: the store now serves this challenge_id.
    backfilled = v2_cnf_store.get(v2_store, item["challenge_id"], expected_sha256=cnf_sha)
    assert backfilled == cnf_text


def test_put_exception_at_verify_time_does_not_break_verification(tmp_path, monkeypatch):
    """A backfill write failure during the verify tick must never affect the
    verification result — cnf_store errors are always swallowed."""
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=False)
    client = TestClient(app)
    kp = _keypair("//CnfStorePutBoom")
    item = _fetch_item(client, kp)

    with v2_pipeline.v2_pm_env():
        cid, cnf_text, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    cnf_sha = hashlib.sha256(cnf_text.encode("utf-8")).hexdigest()

    solution_text = _dimacs_solution_text(assignment)
    cid_blob, sha = _upload_blob(client, kp, solution_text.encode("utf-8"))
    _submit_manifest(
        client, kp, challenge_id=item["challenge_id"], solution_cid=cid_blob,
        solution_sha256=sha, cnf_sha256=cnf_sha)

    def _boom_put(store, challenge_id, cnf_text):
        raise RuntimeError("simulated store outage")

    monkeypatch.setattr(v2_pipeline.v2_cnf_store, "put", _boom_put)

    results = v2_pipeline.process_batch(
        v2_store, app.state.v2_blob_store, worker_id="test-put-boom", batch_size=8, lock_secs=60)
    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]


# ---------------------------------------------------------------------------
# Serving-path read-through: the challenges page and the /cnf endpoint consult
# the store before generating. The invariant under test: a page-minted token's
# cnf_sha256 equals sha256 of the exact bytes /cnf returns, on the cold
# (generate) path AND the warm (cache-hit) path.
# ---------------------------------------------------------------------------

def _get_cnf(client, kp, item):
    r = client.get(
        "/v2/synthetic-boolean/per-miner/cnf",
        params={
            "challenge_id": item["challenge_id"],
            "tier": item["tier"],
            "seq": item["seq"],
        },
        headers=_read_headers(kp),
    )
    assert r.status_code == 200, r.text
    return r


def test_page_token_sha_matches_cnf_body_cold_and_warm(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfReadThroughShaBinding")

    # Cold: the store starts empty, so the first page takes the generate path.
    cold = _fetch_item(client, kp)
    r1 = _get_cnf(client, kp, cold)
    assert hashlib.sha256(r1.text.encode("utf-8")).hexdigest() == cold["cnf_sha256"]
    assert r1.headers["X-Cathedral-CNF-Sha256"] == cold["cnf_sha256"]

    # The token itself binds the same sha the item reports.
    from scaffold.publisher import v2_bitset_submit
    payload = v2_bitset_submit.verify_submit_token(
        cold["submit_token"], secret="test-v2-submit-token-secret",
        miner_hotkey=kp.ss58_address, challenge_id=cold["challenge_id"])
    assert payload["cnf_sha256"] == cold["cnf_sha256"]

    # Warm: the row is baked now. Booby-trap generation: a warm page and a
    # warm /cnf must do ZERO generation and still agree byte-for-byte.
    def _boom(*args, **kwargs):
        raise AssertionError("generate_instance called on a warm read-through path")

    monkeypatch.setattr(pm, "generate_instance", _boom)

    warm = _fetch_item(client, kp)
    assert warm["challenge_id"] == cold["challenge_id"]
    assert warm["cnf_sha256"] == cold["cnf_sha256"]
    assert warm["kind"] == cold["kind"]
    assert warm["n_vars"] == cold["n_vars"]
    r2 = _get_cnf(client, kp, warm)
    assert r2.text == r1.text
    assert hashlib.sha256(r2.text.encode("utf-8")).hexdigest() == warm["cnf_sha256"]
    warm_payload = v2_bitset_submit.verify_submit_token(
        warm["submit_token"], secret="test-v2-submit-token-secret",
        miner_hotkey=kp.ss58_address, challenge_id=warm["challenge_id"])
    assert warm_payload["cnf_sha256"] == warm["cnf_sha256"]


def test_cnf_endpoint_bakes_on_miss_and_warms_the_page(tmp_path, monkeypatch):
    """/cnf now writes the store on its generate path, so a cold challenge_id
    fetched via /cnf first serves the page from cache afterwards."""
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfEndpointBakes")

    item = _fetch_item(client, kp)
    # Wipe the mint-time bake so /cnf sees a genuinely cold store.
    def _wipe(conn):
        conn.execute(
            "DELETE FROM v2_cnf_store WHERE challenge_id=?", (item["challenge_id"],))

    v2_store.write(_wipe)
    assert v2_cnf_store.get(v2_store, item["challenge_id"]) is None

    r = _get_cnf(client, kp, item)
    baked = v2_cnf_store.get(
        v2_store, item["challenge_id"], expected_sha256=item["cnf_sha256"])
    assert baked == r.text

    def _boom(*args, **kwargs):
        raise AssertionError("generate_instance called after /cnf baked the row")

    monkeypatch.setattr(pm, "generate_instance", _boom)
    warm = _fetch_item(client, kp)
    assert warm["cnf_sha256"] == item["cnf_sha256"]


def test_submit_bitset_accepts_token_minted_from_warm_page(tmp_path, monkeypatch):
    """End to end: a token minted purely from the cached CNF (warm page) must
    pass the submit path, which regenerates from seed and sha-compares."""
    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfReadThroughWarmSubmit")

    _cold = _fetch_item(client, kp)  # bakes the row
    warm = _fetch_item(client, kp)   # minted from the store

    with v2_pipeline.v2_pm_env():
        cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(warm["epoch"]), int(warm["tier"]), int(warm["seq"]))
    assert cid == warm["challenge_id"]

    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)
    ).decode("ascii")
    body = {
        "schema": "cathedral.v2.submit_bitset.v1",
        "card_id": _FAMILY,
        "challenge_id": warm["challenge_id"],
        "submit_token": warm["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    from scaffold.publisher import v2_bitset_submit
    submitted_at = _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY)
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    r = client.post(
        "/v2/agents/submit-bitset", json=body,
        headers={
            "X-Cathedral-Hotkey": kp.ss58_address,
            "X-Cathedral-Signature": sig,
            "X-Cathedral-Submitted-At": submitted_at,
        },
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "received"


def test_read_kill_switch_restores_always_generate_on_page(tmp_path, monkeypatch):
    """CATHEDRAL_V2_CNF_STORE_READ=0 must bypass the cache on the serving
    paths. Proven by poisoning the stored row with a different, self-consistent
    body: with reads off the page mints the fresh-generation sha; with reads on
    it serves the cached row."""
    import zlib

    app, v2_store = _build(tmp_path, monkeypatch, submit_bitset_enabled=True)
    client = TestClient(app)
    kp = _keypair("//CnfReadThroughKillSwitch")

    cold = _fetch_item(client, kp)  # bakes the true row

    poison = "p cnf 1 1\n1 0\n"
    poison_sha = hashlib.sha256(poison.encode("utf-8")).hexdigest()
    assert poison_sha != cold["cnf_sha256"]

    def _poison(conn):
        conn.execute(
            "UPDATE v2_cnf_store SET cnf_sha256=?, cnf_zlib=? WHERE challenge_id=?",
            (poison_sha, zlib.compress(poison.encode("utf-8")), cold["challenge_id"]))

    v2_store.write(_poison)

    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_READ", "0")
    off = _fetch_item(client, kp)
    assert off["cnf_sha256"] == cold["cnf_sha256"]

    monkeypatch.delenv("CATHEDRAL_V2_CNF_STORE_READ", raising=False)
    on = _fetch_item(client, kp)
    assert on["cnf_sha256"] == poison_sha


# ---- retention_hours (env knob, 2026-07-09 disk-full incident) -------------

def test_retention_hours_default_is_4(monkeypatch):
    monkeypatch.delenv("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", raising=False)
    assert v2_cnf_store.retention_hours() == 4.0


def test_retention_hours_env_override(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", "8.5")
    assert v2_cnf_store.retention_hours() == 8.5


def test_retention_hours_clamped_to_2h_floor(monkeypatch):
    """A too-small window could purge the current epoch's CNFs out from under
    the verifier; the floor makes that misconfiguration impossible."""
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", "0.25")
    assert v2_cnf_store.retention_hours() == 2.0


def test_retention_hours_garbage_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", "banana")
    assert v2_cnf_store.retention_hours() == 4.0
    monkeypatch.setenv("CATHEDRAL_V2_CNF_STORE_RETENTION_HOURS", "")
    assert v2_cnf_store.retention_hours() == 4.0
