from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher import solution_manifest
from scaffold.publisher import v2_pipeline
from scaffold.publisher import v2_bitset_submit
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


def _bitset_headers(kp, body: dict, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        card_id="synthetic_boolean_v1",
    )
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _v1_submit_headers(kp, *, challenge_id: str, solution: str,
                       submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    sol_sha = hashlib.sha256(solution.encode("utf-8")).hexdigest()
    msg = canonical_claim_bytes(
        bundle_hash=_EMPTY_BUNDLE,
        card_id=_FAMILY,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        challenge_id=challenge_id,
        dimacs_solution_sha256=sol_sha,
    )
    sig = base64.b64encode(kp.sign(msg)).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
        "X-Cathedral-Shadow-Source": "test-mirror",
    }


def _build(tmp_path, monkeypatch, *, enabled: bool = True, role: str = "submit",
           separate_v2: bool = False, shadow_v1: bool = False,
           bitset_submit: bool = False):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", role)
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_UPLOAD_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("CATHEDRAL_V2_SHADOW_V1_ENABLED", "true" if shadow_v1 else "false")
    monkeypatch.setenv("CATHEDRAL_V2_SHADOW_V1_MAX_SOLUTION_BYTES", "1000000")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true" if bitset_submit else "false")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
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


def test_solution_manifest_v2_shadow_v1_submit_default_off(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, enabled=True, role="all", shadow_v1=False)
    client = TestClient(app)
    kp = _keypair("//ShadowV1DefaultOff")
    solution = "s SATISFIABLE\nv 1 -2 3 0\n"
    body = {
        "card_id": _FAMILY,
        "challenge_id": "pm-t1-e1-shadow",
        "dimacs_solution": solution,
        "submitted_at": _now_iso(),
    }
    r = client.post(
        "/v2/shadow/v1/agents/submit",
        data=body,
        headers=_v1_submit_headers(kp, challenge_id=body["challenge_id"], solution=solution,
                                   submitted_at=body["submitted_at"]),
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "v2_shadow_v1_not_enabled"


def test_solution_manifest_v2_shadow_v1_submit_admits_storage_only(tmp_path, monkeypatch):
    app, main_store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True, shadow_v1=True)
    client = TestClient(app)
    kp = _keypair("//ShadowV1Mirror")
    challenge_id = "pm-t1-e1-shadow"
    submitted_at = _now_iso()
    solution = "s SATISFIABLE\nv 1 -2 3 0\n"
    body = {
        "card_id": _FAMILY,
        "challenge_id": challenge_id,
        "dimacs_solution": solution,
        "display_name": "mirror-test",
        "submitted_at": submitted_at,
    }
    headers = _v1_submit_headers(
        kp, challenge_id=challenge_id, solution=solution, submitted_at=submitted_at)

    first = client.post("/v2/shadow/v1/agents/submit", data=body, headers=headers)
    second = client.post("/v2/shadow/v1/agents/submit", data=body, headers=headers)

    assert first.status_code == 202
    assert second.status_code == 200
    payload = first.json()
    assert payload["schema"] == "cathedral.v2.shadow_v1_submit_receipt.v1"
    assert payload["shadow"] is True
    assert payload["status"] == "received"
    assert payload["miner_hotkey"] == kp.ss58_address
    assert payload["challenge_id"] == challenge_id
    assert payload["solution_sha256"] == hashlib.sha256(solution.encode("utf-8")).hexdigest()
    assert payload["solution_cid"].startswith("local://v1_submit_solution/")
    assert second.json()["receipt_id"] == payload["receipt_id"]
    assert second.json()["idempotent_replay"] is True

    receipt = client.get(f"/v2/shadow/v1/agents/submit/receipts/{payload['receipt_id']}")
    assert receipt.status_code == 200
    assert receipt.json()["receipt_id"] == payload["receipt_id"]

    metrics = client.get("/v2/shadow/v1/agents/submit/metrics")
    assert metrics.status_code == 200
    assert metrics.json()["total"]["count"] == 1
    assert metrics.json()["total"]["bytes"] == len(solution.encode("utf-8"))
    assert metrics.json()["windows"]["1h"]["count"] == 1


def test_solution_manifest_v2_shadow_v1_submit_meta_admits_without_body(tmp_path, monkeypatch):
    app, _main_store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True, shadow_v1=True)
    client = TestClient(app)
    kp = _keypair("//ShadowV1Meta")
    payload = {
        "schema": "cathedral.v2.shadow_v1_submit_meta.v1",
        "request_id": "req-meta-1",
        "source": "test-edge-meta",
        "edge_received_at_iso": _now_iso(),
        "miner_hotkey": kp.ss58_address,
        "card_id": _FAMILY,
        "challenge_id": "pm-t1-e1-meta",
        "submitted_at": _now_iso(),
        "signature_present": True,
        "content_type": "application/x-www-form-urlencoded",
        "original_content_length": 4096,
        "original_body_bytes": 4096,
        "dimacs_solution_bytes": 3500,
        "field_count": 4,
    }
    r = client.post(
        "/v2/shadow/v1/agents/submit/meta",
        json=payload,
        headers={"X-Cathedral-Hotkey": kp.ss58_address, "X-Cathedral-Shadow-Source": "test-edge-meta"},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["schema"] == "cathedral.v2.shadow_v1_submit_meta_receipt.v1"
    assert body["metadata_only"] is True
    assert body["solution_body_stored"] is False
    assert body["miner_hotkey"] == kp.ss58_address
    assert body["challenge_id"] == "pm-t1-e1-meta"
    assert body["dimacs_solution_bytes"] == 3500

    metrics = client.get("/v2/shadow/v1/agents/submit/meta/metrics")
    assert metrics.status_code == 200
    data = metrics.json()
    assert data["schema"] == "cathedral.v2.shadow_v1_submit_meta_metrics.v1"
    assert data["metadata_only"] is True
    assert data["solution_body_stored"] is False
    assert data["total"]["count"] == 1
    assert data["total"]["body_bytes"] == 4096
    assert data["total"]["solution_bytes"] == 3500
    assert data["windows"]["1h"]["count"] == 1

    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    meta_rows = v2_store.query("SELECT * FROM v2_shadow_v1_submit_meta")
    assert len(meta_rows) == 1
    assert meta_rows[0]["dimacs_solution_bytes"] == 3500
    assert v2_store.query("SELECT COUNT(*) AS n FROM v2_shadow_v1_submits")[0]["n"] == 0


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


def test_v2_verify_metrics_endpoint_reports_pending(tmp_path, monkeypatch):
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)
    app.state.v2_store.write(lambda conn: conn.execute(
        "INSERT INTO solution_manifests("
        "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
        "assignment_encoding, solution_cid, solution_sha256, solution_bytes, "
        "status, received_at_iso, submitted_at, signature, manifest_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "pending-metrics-1",
            "idem-pending-metrics-1",
            "5PendingMetricsHotkey",
            "pm-t1-e1-metrics",
            _FAMILY,
            "dimacs/v1",
            "local://missing",
            hashlib.sha256(b"x").hexdigest(),
            1,
            "received",
            "2026-07-01T00:00:00.000Z",
            "2026-07-01T00:00:00.000Z",
            "sig",
            "{}",
        ),
    ))

    r = client.get("/v2/verify/metrics")
    assert r.status_code == 200
    payload = r.json()
    assert payload["schema"] == "cathedral.v2.verify_metrics.v1"
    assert payload["pending_count"] >= 1
    assert payload["by_source"]["manifest"]["pending_count"] >= 1
    assert "lock_held_by_self" in payload
    assert "verify_rate_per_sec" in payload


def test_v2_submit_token_allowlist_blocks_unlisted_hotkeys(tmp_path, monkeypatch):
    allowed = _keypair("//V2TokenAllowed")
    blocked = _keypair("//V2TokenBlocked")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_ALLOWLIST", allowed.ss58_address)
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)

    blocked_r = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(blocked),
    )
    assert blocked_r.status_code == 403
    assert blocked_r.json()["detail"] == "v2_submit_token_hotkey_not_allowlisted"

    allowed_r = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(allowed),
    )
    assert allowed_r.status_code == 200
    assert allowed_r.json()["items"][0]["submit_token"]


def test_solution_manifest_v2_submit_bitset_e2e_scores_shadow_weights(tmp_path, monkeypatch):
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)
    kp = _keypair("//BitsetSubmitE2E")

    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    payload = board.json()
    assert payload["submit_path"] == "/v2/agents/submit-bitset"
    item = payload["items"][0]
    assert item["assignment_encoding"] == "bitset/v1"
    assert item["submit_token"]
    assert item["cnf_sha256"]

    cnf = client.get(
        f"/v2/synthetic-boolean/per-miner/cnf?challenge_id={item['challenge_id']}&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp),
    )
    assert cnf.status_code == 200
    assert cnf.headers["x-cathedral-submit-path"] == "/v2/agents/submit-bitset"
    assert cnf.headers["x-cathedral-assignment-encoding"] == "bitset/v1"

    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assignment_b64 = base64.b64encode(v2_pipeline.encode_bitset_assignment(assignment)).decode("ascii")
    submitted_at = _now_iso()
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    first = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body, submitted_at=submitted_at),
    )
    second = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body, submitted_at=submitted_at),
    )
    assert first.status_code == 202
    assert second.status_code == 200
    received = first.json()
    assert received["schema"] == "cathedral.v2.submit_bitset_receipt.v1"
    assert received["status"] == "received"
    assert received["open"] is True
    assert received["terminal"] is False
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    results = v2_pipeline.process_bitset_batch(v2_store, batch_size=1)
    assert results[0]["status"] == "verified"

    fetched = client.get(received["receipt_url"])
    assert fetched.status_code == 200
    receipt = fetched.json()
    assert receipt["receipt_id"] == received["receipt_id"]
    assert receipt["schema"] == "cathedral.v2.submit_bitset_receipt.v1"
    assert receipt["status"] == "verified"
    assert receipt["open"] is False
    assert receipt["terminal"] is True
    assert receipt["weighted_score"] == item["difficulty_weight"]
    assert second.json()["receipt_id"] == received["receipt_id"]
    assert second.json()["idempotent_replay"] is True

    weights = client.get("/v2/validator/weights/next")
    assert weights.status_code == 200
    vector = weights.json()
    row = next((w for w in vector["weights"] if w["miner_hotkey"] == kp.ss58_address), None)
    assert row is not None
    assert row["weight"] == 1.0
    assert row["raw_score"] == item["difficulty_weight"]
    assert vector["policy_metadata"]["receipt_counts"]["bitset:verified"] == 1

    audit = client.get(f"/v2/audit/epochs/{int(item['epoch'])}")
    assert audit.status_code == 200
    bundle = audit.json()
    assert bundle["status_counts"]["bitset:verified"] == 1
    assert any(r["id"] == receipt["receipt_id"] and r["source"] == "bitset" for r in bundle["receipts"])

    assert v2_store.query("SELECT COUNT(*) AS n FROM v2_submit_events")[0]["n"] == 1
    assert v2_store.query("SELECT COUNT(*) AS n FROM solution_manifests")[0]["n"] == 0


def test_solution_manifest_v2_submit_bitset_backpressure_sheds_new_work_not_replays(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_MAX_PENDING", "1")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BACKPRESSURE_RETRY_AFTER_SECS", "7")
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)

    def build_submit(uri: str):
        kp = _keypair(uri)
        board = client.get(
            "/v2/synthetic-boolean/per-miner/challenges?limit=1",
            headers=_read_headers(kp),
        )
        assert board.status_code == 200
        item = board.json()["items"][0]
        with v2_pipeline.v2_pm_env():
            _cid, _cnf, assignment = pm.generate_instance(
                kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
        body = {
            "schema": v2_bitset_submit.SCHEMA,
            "card_id": _FAMILY,
            "challenge_id": item["challenge_id"],
            "submit_token": item["submit_token"],
            "assignment_encoding": "bitset/v1",
            "assignment_b64": base64.b64encode(
                v2_pipeline.encode_bitset_assignment(assignment)
            ).decode("ascii"),
        }
        submitted_at = _now_iso()
        return kp, body, _bitset_headers(kp, body, submitted_at=submitted_at)

    _kp1, body1, headers1 = build_submit("//BitsetBackpressureOne")
    first = client.post("/v2/agents/submit-bitset", json=body1, headers=headers1)
    assert first.status_code == 202, first.text

    replay = client.post("/v2/agents/submit-bitset", json=body1, headers=headers1)
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]
    assert replay.json()["idempotent_replay"] is True

    _kp2, body2, headers2 = build_submit("//BitsetBackpressureTwo")
    shed = client.post("/v2/agents/submit-bitset", json=body2, headers=headers2)
    assert shed.status_code == 503
    assert shed.headers["retry-after"] == "7"
    assert shed.headers["x-cathedral-rejection-reason"] == "v2_submit_backpressure"
    assert shed.json()["reason"] == "v2_submit_backpressure"
    assert shed.json()["pending_count"] == 1


def test_solution_manifest_v2_manifest_and_bitset_same_challenge_count_once(tmp_path, monkeypatch):
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)
    kp = _keypair("//BitsetManifestDedupe")
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    item = board.json()["items"][0]
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    blob = v2_pipeline.encode_bitset_assignment(assignment)
    assignment_b64 = base64.b64encode(blob).decode("ascii")

    uploaded = client.post("/v2/blobs/solutions", content=blob, headers=_upload_headers(kp, blob))
    assert uploaded.status_code == 200
    up = uploaded.json()
    manifest_body = {
        "schema": solution_manifest.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "assignment_encoding": "bitset/v1",
        "solution_cid": up["cid"],
        "solution_sha256": up["sha256"],
        "solution_bytes": int(up["bytes"]),
        "cnf_sha256": item["cnf_sha256"],
    }
    manifest = client.post(
        "/v2/agents/submit-manifest",
        json=manifest_body,
        headers=_headers(kp, manifest_body),
    )
    assert manifest.status_code == 202
    tick = client.post("/v2/admin/verify/tick", headers={"Authorization": "Bearer test-admin-token"})
    assert tick.status_code == 200
    assert client.get(manifest.json()["receipt_url"]).json()["status"] == "verified"

    bitset_body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    bitset = client.post(
        "/v2/agents/submit-bitset",
        json=bitset_body,
        headers=_bitset_headers(kp, bitset_body),
    )
    assert bitset.status_code == 202
    # Thin submit: drain the async verify worker before asserting scored state.
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    results = v2_pipeline.process_bitset_batch(v2_store, batch_size=1)
    assert results and results[0]["status"] == "verified"

    vector = client.get("/v2/validator/weights/next").json()
    row = next(w for w in vector["weights"] if w["miner_hotkey"] == kp.ss58_address)
    assert row["raw_score"] == item["difficulty_weight"]
    assert vector["policy_metadata"]["receipt_counts"]["manifest:verified"] == 1
    assert vector["policy_metadata"]["receipt_counts"]["bitset:verified"] == 1

    audit = client.get(f"/v2/audit/epochs/{int(item['epoch'])}").json()
    assert audit["status_counts"]["manifest:verified"] == 1
    assert audit["status_counts"]["bitset:verified"] == 1


def test_solution_manifest_v2_submit_bitset_rejects_bad_shape_without_row(tmp_path, monkeypatch):
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)
    kp = _keypair("//BitsetSubmitBadShape")
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    item = board.json()["items"][0]
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": base64.b64encode(b"short").decode("ascii"),
    }
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "bitset_size_mismatch"
    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    assert v2_store.query("SELECT COUNT(*) AS n FROM v2_submit_events")[0]["n"] == 0


def test_solution_manifest_v2_submit_bitset_rejects_auth_failures_without_rows(tmp_path, monkeypatch):
    app, _store = _build(
        tmp_path, monkeypatch, enabled=True, role="all", separate_v2=True,
        bitset_submit=True)
    client = TestClient(app)
    kp = _keypair("//BitsetSubmitAuthFailures")
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    item = board.json()["items"][0]
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assignment_b64 = base64.b64encode(v2_pipeline.encode_bitset_assignment(assignment)).decode("ascii")
    valid_body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }

    bad_sig_headers = _bitset_headers(kp, valid_body)
    bad_sig_headers["X-Cathedral-Signature"] = base64.b64encode(b"0" * 64).decode("ascii")
    bad_sig = client.post("/v2/agents/submit-bitset", json=valid_body, headers=bad_sig_headers)
    assert bad_sig.status_code == 401
    assert bad_sig.json()["detail"] == "invalid hotkey signature"

    forged = {**valid_body, "submit_token": item["submit_token"][:-1] + ("A" if item["submit_token"][-1] != "A" else "B")}
    forged_r = client.post("/v2/agents/submit-bitset", json=forged, headers=_bitset_headers(kp, forged))
    assert forged_r.status_code == 400
    assert forged_r.json()["detail"] == "invalid_submit_token"

    expired_token = v2_bitset_submit.mint_submit_token(
        secret="test-v2-submit-token-secret",
        miner_hotkey=kp.ss58_address,
        challenge_id=item["challenge_id"],
        epoch=int(item["epoch"]),
        tier=int(item["tier"]),
        seq=int(item["seq"]),
        nvars=int(item["n_vars"]),
        cnf_sha256=item["cnf_sha256"],
        expires_at="2000-01-01T00:00:00.000Z",
    )
    expired = {**valid_body, "submit_token": expired_token}
    expired_r = client.post("/v2/agents/submit-bitset", json=expired, headers=_bitset_headers(kp, expired))
    assert expired_r.status_code == 400
    assert expired_r.json()["detail"] == "submit_token_expired"

    v2_store = Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)
    assert v2_store.query("SELECT COUNT(*) AS n FROM v2_submit_events")[0]["n"] == 0


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
    assert bundle["status_counts"] == {"manifest:verified": 1}
    assert bundle["receipts"][0]["id"] == receipt_id
    assert bundle["receipts"][0]["source"] == "manifest"
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
