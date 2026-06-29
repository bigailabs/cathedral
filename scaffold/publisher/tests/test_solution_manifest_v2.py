from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher import solution_manifest
from scaffold.publisher import v2_pipeline
from scaffold.publisher import per_miner as pm
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store


SIGNING_KEY_HEX = "11" * 32
_FAMILY = "synthetic_boolean_v1"
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str = "//ManifestMiner"):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _body(*, cid: str = "hippius://bafy-solution", encoding: str = "bitset/v1") -> dict:
    return {
        "schema": solution_manifest.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "challenge_id": "pm-t1-e1-test",
        "assignment_encoding": encoding,
        "solution_cid": cid,
        "solution_sha256": hashlib.sha256(b"packed-bitset-solution").hexdigest(),
        "solution_bytes": 2048,
        "cnf_sha256": hashlib.sha256(b"cnf").hexdigest(),
    }


def _upload_headers(kp, blob: bytes, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    blob_sha = hashlib.sha256(blob).hexdigest()
    sig = base64.b64encode(kp.sign(solution_manifest.canonical_blob_upload_bytes(
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        blob_sha256=blob_sha,
        blob_bytes=len(blob),
        kind="solution",
    ))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
        "X-Cathedral-Blob-Sha256": blob_sha,
        "Content-Type": "application/octet-stream",
    }


def _read_headers(kp, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE,
        card_id=_FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        challenge_id="",
        dimacs_solution_sha256="",
    )
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _headers(kp, body: dict, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    manifest = solution_manifest.normalize_manifest(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        card_id="synthetic_boolean_v1",
    )
    sig = base64.b64encode(kp.sign(solution_manifest.canonical_manifest_bytes(manifest))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _build(tmp_path, monkeypatch, *, enabled: bool = True, role: str = "submit",
           separate_v2: bool = False):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", role)
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_UPLOAD_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "manifest-v2-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "8")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_NVARS_T1", "80")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_NCLAUSES_T1", "240")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_METHOD_T1", "biased")
    db = str(tmp_path / "pub.sqlite")
    if separate_v2:
        monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    return app, Store(db)


def test_solution_manifest_v2_default_off(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, enabled=False)
    kp = _keypair()
    body = _body()
    r = TestClient(app).post(
        "/v2/agents/submit-manifest",
        json=body,
        headers=_headers(kp, body),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "solution_manifest_v2_not_enabled"


def test_solution_manifest_v2_serves_prefixed_pm_challenges_and_cnf(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, enabled=True, role="all")
    client = TestClient(app)
    kp = _keypair("//ManifestPMFetch")
    headers = _read_headers(kp)

    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=2",
        headers=headers,
    )
    assert board.status_code == 200
    payload = board.json()
    assert payload["kind"] == "per_miner_v2"
    assert payload["count"] == 3  # T1 limit=2 plus one configured T2 instance
    item = payload["items"][0]
    assert payload["submit_path"] == "/v2/agents/submit-manifest"
    assert payload["blob_upload_path"] == "/v2/blobs/solutions"

    cnf = client.get(
        f"/v2/synthetic-boolean/per-miner/cnf?challenge_id={item['challenge_id']}&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp),
    )
    assert cnf.status_code == 200
    assert "p cnf" in cnf.text
    assert cnf.headers["x-cathedral-v2"] == "true"


def test_solution_manifest_v2_accepts_signed_manifest_and_receipt(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, enabled=True)
    client = TestClient(app)
    kp = _keypair()
    body = _body()
    headers = _headers(kp, body)

    r = client.post("/v2/agents/submit-manifest", json=body, headers=headers)
    assert r.status_code == 202
    payload = r.json()
    assert payload["schema"] == "cathedral.solution_manifest_receipt.v1"
    assert payload["status"] == "received"
    assert payload["open"] is True
    assert payload["terminal"] is False
    assert payload["idempotent_replay"] is False
    assert payload["miner_hotkey"] == kp.ss58_address
    assert payload["assignment_encoding"] == "bitset/v1"
    assert payload["solution_cid"] == body["solution_cid"]

    rows = store.query("SELECT * FROM solution_manifests")
    assert len(rows) == 1
    assert rows[0]["solution_sha256"] == body["solution_sha256"]
    assert "packed-bitset-solution" not in rows[0]["manifest_json"]

    receipt = client.get(payload["receipt_url"])
    assert receipt.status_code == 200
    assert receipt.json()["receipt_id"] == payload["receipt_id"]


def test_solution_manifest_v2_replay_is_idempotent(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, enabled=True)
    client = TestClient(app)
    kp = _keypair()
    body = _body()
    headers = _headers(kp, body)

    first = client.post("/v2/agents/submit-manifest", json=body, headers=headers)
    second = client.post("/v2/agents/submit-manifest", json=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["receipt_id"] == first.json()["receipt_id"]
    assert store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 1


def test_solution_manifest_v2_rejects_tampered_manifest(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, enabled=True)
    client = TestClient(app)
    kp = _keypair()
    body = _body()
    headers = _headers(kp, body)
    tampered = {**body, "solution_cid": "hippius://different-cid"}

    r = client.post("/v2/agents/submit-manifest", json=tampered, headers=headers)

    assert r.status_code == 401
    assert r.json()["detail"] == "invalid hotkey signature"
    assert store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 0


def test_solution_manifest_v2_rejects_unknown_encoding(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, enabled=True)
    client = TestClient(app)
    kp = _keypair()
    body = _body(encoding="literal-list/v0")

    r = client.post("/v2/agents/submit-manifest", json=body, headers=_headers(kp, _body()))

    assert r.status_code == 400
    assert r.json()["detail"] == "unsupported_assignment_encoding"
    assert store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 0


def test_solution_manifest_v2_blob_to_verified_receipt_weights_and_audit(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, enabled=True, role="all")
    client = TestClient(app)
    kp = _keypair("//ManifestE2E")
    with v2_pipeline.v2_pm_env():
        epoch = pm.current_epoch()
        cid, _cnf, assignment = pm.generate_instance(kp.ss58_address, epoch, 1, 0)
    blob = v2_pipeline.encode_bitset_assignment(assignment)

    uploaded = client.post("/v2/blobs/solutions", content=blob, headers=_upload_headers(kp, blob))
    assert uploaded.status_code == 200
    up = uploaded.json()
    assert up["sha256"] == hashlib.sha256(blob).hexdigest()
    assert up["bytes"] == len(blob)
    assert up["cid"].startswith("local://solution/")

    body = {
        "schema": solution_manifest.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "challenge_id": cid,
        "assignment_encoding": "bitset/v1",
        "solution_cid": up["cid"],
        "solution_sha256": up["sha256"],
        "solution_bytes": up["bytes"],
    }
    admitted = client.post("/v2/agents/submit-manifest", json=body, headers=_headers(kp, body))
    assert admitted.status_code == 202
    receipt_id = admitted.json()["receipt_id"]

    tick = client.post(
        "/v2/admin/verify/tick",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert tick.status_code == 200
    assert tick.json()["count"] == 1
    assert tick.json()["results"][0]["status"] == "verified"

    receipt = client.get(f"/v2/agents/submit-manifest/receipts/{receipt_id}")
    assert receipt.status_code == 200
    payload = receipt.json()
    assert payload["status"] == "verified"
    assert payload["terminal"] is True
    assert payload["weighted_score"] == 1.0
    assert payload["challenge_id"] == cid

    weights = client.get("/v2/validator/weights/next")
    assert weights.status_code == 200
    vector = weights.json()
    assert vector["schema"] == "cathedral.v2.shadow_weights.v1"
    assert vector["policy_metadata"]["shadow"] is True
    assert vector["weights"] == [{"miner_hotkey": kp.ss58_address, "weight": 1.0, "raw_score": 1.0}]
    assert vector.get("signature")

    audit = client.get(f"/v2/audit/epochs/{epoch}")
    assert audit.status_code == 200
    bundle = audit.json()
    assert bundle["schema"] == "cathedral.v2.audit_bundle.v1"
    assert bundle["count"] == 1
    assert bundle["status_counts"] == {"verified": 1}
    assert bundle["receipts"][0]["id"] == receipt_id
    assert bundle.get("signature")


def test_solution_manifest_v2_worker_rejects_blob_hash_mismatch(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, enabled=True, role="all")
    client = TestClient(app)
    kp = _keypair("//ManifestBadHash")
    with v2_pipeline.v2_pm_env():
        epoch = pm.current_epoch()
        cid, _cnf, assignment = pm.generate_instance(kp.ss58_address, epoch, 1, 0)
    blob = v2_pipeline.encode_bitset_assignment(assignment)
    uploaded = client.post("/v2/blobs/solutions", content=blob, headers=_upload_headers(kp, blob))
    assert uploaded.status_code == 200
    up = uploaded.json()

    body = {
        "schema": solution_manifest.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "challenge_id": cid,
        "assignment_encoding": "bitset/v1",
        "solution_cid": up["cid"],
        "solution_sha256": hashlib.sha256(b"not-the-blob").hexdigest(),
        "solution_bytes": up["bytes"],
    }
    admitted = client.post("/v2/agents/submit-manifest", json=body, headers=_headers(kp, body))
    assert admitted.status_code == 202
    receipt_id = admitted.json()["receipt_id"]

    tick = client.post("/v2/admin/verify/tick", headers={"Authorization": "Bearer test-admin-token"})
    assert tick.status_code == 200
    assert tick.json()["results"][0]["reason"] == "solution_sha256_mismatch"
    receipt = client.get(f"/v2/agents/submit-manifest/receipts/{receipt_id}").json()
    assert receipt["status"] == "rejected"
    assert receipt["rejection_reason"] == "solution_sha256_mismatch"


def test_solution_manifest_v2_worker_rejects_malformed_bitset(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, enabled=True, role="all")
    client = TestClient(app)
    kp = _keypair("//ManifestBadBitset")
    with v2_pipeline.v2_pm_env():
        epoch = pm.current_epoch()
        cid, _cnf, _assignment = pm.generate_instance(kp.ss58_address, epoch, 1, 0)
    blob = b"too-short"
    uploaded = client.post("/v2/blobs/solutions", content=blob, headers=_upload_headers(kp, blob))
    assert uploaded.status_code == 200
    up = uploaded.json()

    body = {
        "schema": solution_manifest.SCHEMA,
        "card_id": "synthetic_boolean_v1",
        "challenge_id": cid,
        "assignment_encoding": "bitset/v1",
        "solution_cid": up["cid"],
        "solution_sha256": up["sha256"],
        "solution_bytes": up["bytes"],
    }
    admitted = client.post("/v2/agents/submit-manifest", json=body, headers=_headers(kp, body))
    assert admitted.status_code == 202
    receipt_id = admitted.json()["receipt_id"]

    tick = client.post("/v2/admin/verify/tick", headers={"Authorization": "Bearer test-admin-token"})
    assert tick.status_code == 200
    assert tick.json()["results"][0]["reason"] == "bitset_size_mismatch"
    receipt = client.get(f"/v2/agents/submit-manifest/receipts/{receipt_id}").json()
    assert receipt["status"] == "rejected"
    assert receipt["rejection_reason"] == "bitset_size_mismatch"


def test_solution_manifest_v2_can_use_separate_db(tmp_path, monkeypatch):
    app, main_store = _build(tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True)
    client = TestClient(app)
    kp = _keypair("//ManifestSeparateDb")
    body = _body()

    r = client.post("/v2/agents/submit-manifest", json=body, headers=_headers(kp, body))
    assert r.status_code == 202
    assert main_store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 0
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    assert v2_store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 1
