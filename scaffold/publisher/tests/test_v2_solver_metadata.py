"""V2 bitset submit: miner-declared solver provenance metadata.

Covers `solver_id` / `solver_hash` / `image_url` on the V2 bitset submit path
(`/v2/agents/submit-bitset`). This metadata is forward-looking (later
verification/attestation): it is SIGNED (part of canonical_submit_bytes, so
tampering with it after signing must fail the hotkey signature check), it is
stored on the v2_submit_events row and echoed back on the receipt, but it
never affects scoring/verification/eligibility today.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from starlette.testclient import TestClient

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


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _build(tmp_path, monkeypatch, *, require_solver_meta: bool = False):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_BITSET_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_SECRET", "test-v2-submit-token-secret")
    monkeypatch.setenv("CATHEDRAL_V2_SUBMIT_TOKEN_TTL_SECS", "300")
    monkeypatch.setenv("CATHEDRAL_V2_BLOB_DIR", str(tmp_path / "v2_blobs"))
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "solver-meta-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "8")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "1")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    if require_solver_meta:
        monkeypatch.setenv("CATHEDRAL_V2_REQUIRE_SOLVER_META", "true")
    else:
        monkeypatch.delenv("CATHEDRAL_V2_REQUIRE_SOLVER_META", raising=False)
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    return app, Store(str(tmp_path / "v2.sqlite"), prefer_env_database_url=False)


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


def _bitset_headers(kp, body: dict, *, submitted_at: str) -> dict[str, str]:
    """Sign EXACTLY this body — used both for legitimate requests and to
    produce a stale signature when the caller mutates `body` afterwards
    (the tamper test)."""
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY,
    )
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": submitted_at,
    }


def _stub_headers(kp, *, submitted_at: str) -> dict[str, str]:
    """A syntactically-valid but unverified signature. Safe to use for
    requests we expect to be rejected by normalize_submit_body's field
    validation, which runs (in app.py) before the hotkey signature is ever
    checked — so the signature's validity is irrelevant to these cases."""
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": base64.b64encode(b"placeholder-unverified").decode("ascii"),
        "X-Cathedral-Submitted-At": submitted_at,
    }


def _fetch_and_solve(client, kp) -> tuple[dict, str]:
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=1",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    item = board.json()["items"][0]
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, assignment = pm.generate_instance(
            kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)
    ).decode("ascii")
    return item, assignment_b64


def _solver_hash(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


def test_solver_metadata_present_is_signed_stored_and_echoed(tmp_path, monkeypatch):
    app, v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//SolverMetaPresent")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    solver_id = "cadical153"
    solver_hash = _solver_hash("cadical153")
    image_url = "hippius://bafybeituplenty0f-fake-cid-solver-image"
    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_id": solver_id,
        "solver_hash": solver_hash,
        "image_url": image_url,
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body, submitted_at=submitted_at),
    )
    assert r.status_code == 202, r.text
    receipt = r.json()
    assert receipt["status"] == "verified"
    assert receipt["solver_id"] == solver_id
    assert receipt["solver_hash"] == solver_hash
    assert receipt["image_url"] == image_url

    row = v2_bitset_submit.get_receipt(v2_store, receipt["receipt_id"])
    assert row is not None
    assert row["solver_id"] == solver_id
    assert row["solver_hash"] == solver_hash
    assert row["image_url"] == image_url

    # The metadata is part of the SIGNED bytes: canonical_submit_bytes covers it.
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=submitted_at, card_id=_FAMILY,
    )
    canonical = v2_bitset_submit.canonical_submit_bytes(submit)
    assert b'"solver_id":"cadical153"' in canonical
    assert b'"solver_hash":"' in canonical
    assert b'"image_url":"' in canonical

    # Scoring is unaffected by the metadata: weight matches the plain path.
    assert receipt["weighted_score"] == item["difficulty_weight"]


def test_tampering_solver_metadata_after_signing_fails_signature(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//SolverMetaTamper")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_id": "cadical153",
        "solver_hash": _solver_hash("cadical153"),
    }
    submitted_at = _now_iso()
    # Sign the ORIGINAL (honest) body, then tamper solver_id before sending —
    # proves the metadata is covered by the signature, not just stored blindly.
    headers = _bitset_headers(kp, body, submitted_at=submitted_at)
    tampered = dict(body)
    tampered["solver_id"] = "glucose3"

    r = client.post("/v2/agents/submit-bitset", json=tampered, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid hotkey signature"


def test_tampering_solver_hash_after_signing_fails_signature(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//SolverHashTamper")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_id": "cadical153",
        "solver_hash": _solver_hash("cadical153"),
    }
    submitted_at = _now_iso()
    headers = _bitset_headers(kp, body, submitted_at=submitted_at)
    tampered = dict(body)
    tampered["solver_hash"] = _solver_hash("some-other-solver-binary")

    r = client.post("/v2/agents/submit-bitset", json=tampered, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid hotkey signature"


def test_bad_image_url_scheme_rejected(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//BadImageUrlScheme")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "image_url": "ftp://malicious.example/payload",
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_stub_headers(kp, submitted_at=submitted_at),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_image_url"


def test_invalid_solver_id_charset_rejected(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//BadSolverId")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_id": "bad@solver#id!",
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_stub_headers(kp, submitted_at=submitted_at),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_solver_id"


def test_invalid_solver_hash_format_rejected(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch)
    client = TestClient(app)
    kp = _keypair("//BadSolverHash")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_hash": "not-hex-at-all!!",
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_stub_headers(kp, submitted_at=submitted_at),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_solver_hash"


def test_require_solver_meta_flag_on_rejects_missing_metadata(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch, require_solver_meta=True)
    client = TestClient(app)
    kp = _keypair("//RequireSolverMetaOnMissing")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_stub_headers(kp, submitted_at=submitted_at),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "solver_meta_required"


def test_require_solver_meta_flag_on_accepts_with_metadata(tmp_path, monkeypatch):
    app, _v2_store = _build(tmp_path, monkeypatch, require_solver_meta=True)
    client = TestClient(app)
    kp = _keypair("//RequireSolverMetaOnPresent")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
        "solver_id": "cadical153",
        "solver_hash": _solver_hash("cadical153"),
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body, submitted_at=submitted_at),
    )
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "verified"


def test_require_solver_meta_flag_off_by_default_accepts_missing_metadata(tmp_path, monkeypatch):
    """Default is accept-optional — no breakage for existing miners that don't
    declare solver provenance yet."""
    app, _v2_store = _build(tmp_path, monkeypatch, require_solver_meta=False)
    client = TestClient(app)
    kp = _keypair("//RequireSolverMetaOffMissing")
    item, assignment_b64 = _fetch_and_solve(client, kp)

    body = {
        "schema": v2_bitset_submit.SCHEMA,
        "card_id": _FAMILY,
        "challenge_id": item["challenge_id"],
        "submit_token": item["submit_token"],
        "assignment_encoding": "bitset/v1",
        "assignment_b64": assignment_b64,
    }
    submitted_at = _now_iso()
    r = client.post(
        "/v2/agents/submit-bitset",
        json=body,
        headers=_bitset_headers(kp, body, submitted_at=submitted_at),
    )
    assert r.status_code == 202, r.text
    receipt = r.json()
    assert receipt["status"] == "verified"
    assert "solver_id" not in receipt
    assert "solver_hash" not in receipt
    assert "image_url" not in receipt
