"""Public receipts feed: scrubbed, signed, per-epoch verified-row export.

Covers `v2_receipts.build_receipts_bundle` directly (SQLite `Store`, rows
seeded straight into `solution_manifests` / `v2_submit_events`) and the
`/v2/receipts/epochs/{epoch}` + `/v2/receipts/latest` endpoints wired in
app.py, including that both are TTL-cached (a required guardrail — every
DB-heavy public V2 endpoint must be, per the outages this fixes for).
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.publisher import v2_pipeline
from scaffold.publisher import v2_receipts
from scaffold.publisher.app import build_app
from scaffold.publisher.store import Store

SIGNING_KEY_HEX = "22" * 32
_FORBIDDEN_SUBSTRINGS = ("submit_token", "idempotency", "locked_by")


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _insert_manifest_row(store: Store, *, rid: str, hotkey: str, challenge_id: str,
                          epoch: int, tier: int = 1, seq: int = 0,
                          status: str = "verified", solution_inline: bytes | None = b"abc123",
                          received_at_iso: str | None = None) -> None:
    received = received_at_iso or _now_iso()

    def _tx(conn):
        conn.execute(
            "INSERT INTO solution_manifests("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
            "assignment_encoding, solution_cid, solution_sha256, solution_bytes, "
            "cnf_sha256, status, submitted_at, received_at_iso, verified_at_iso, "
            "epoch, tier, seq, weighted_score, answer_hash, "
            "locked_by, locked_until_iso, next_attempt_at_iso, last_error, "
            "rejection_reason, signature, manifest_json, solution_inline"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid, f"idem-{rid}", hotkey, challenge_id, "synthetic_boolean_v1",
                "bitset/v1", "hippius://fake-cid", hashlib.sha256(b"solution").hexdigest(), 3,
                hashlib.sha256(b"cnf").hexdigest(), status, received, received,
                received if status == "verified" else None,
                epoch, tier, seq, 1.0, hashlib.sha256(b"answer").hexdigest(),
                "some-worker-id-should-never-leak", received, None,
                "internal error detail should never leak" if status != "verified" else None,
                "solution_sha256_mismatch" if status != "verified" else None,
                "0xminer-signature-" + rid, json.dumps({"schema": "cathedral.solution_manifest.v1"}),
                solution_inline,
            ),
        )

    store.write(_tx)


def _insert_bitset_row(store: Store, *, rid: str, hotkey: str, challenge_id: str,
                        epoch: int, tier: int = 1, seq: int = 1,
                        status: str = "verified", challenge_kind: str | None = "coloring",
                        received_at_iso: str | None = None) -> None:
    received = received_at_iso or _now_iso()
    assignment_b64 = base64.b64encode(b"\x01\x02\x03").decode()

    def _tx(conn):
        conn.execute(
            "INSERT INTO v2_submit_events("
            "id, idempotency_key, miner_hotkey, challenge_id, card_id, "
            "epoch, tier, seq, cnf_sha256, assignment_encoding, assignment_sha256, "
            "assignment_b64, status, rejection_reason, eligibility_status, "
            "received_at_iso, submitted_at, verified_at_iso, signature, "
            "submit_token_id, weighted_score, answer_hash, verifier_details_hash, "
            "solver_id, solver_hash, image_url, challenge_kind"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid, f"idem-{rid}", hotkey, challenge_id, "synthetic_boolean_v1",
                epoch, tier, seq, hashlib.sha256(b"cnf").hexdigest(), "bitset/v1",
                hashlib.sha256(b"\x01\x02\x03").hexdigest(), assignment_b64,
                status,
                "witness_check_failed" if status != "verified" else None,
                "unknown_beta",
                received, received, received if status == "verified" else None,
                "0xbitset-signature-" + rid,
                "should-never-leak-submit-token-id", 1.0,
                hashlib.sha256(b"bitset-answer").hexdigest(), hashlib.sha256(b"details").hexdigest(),
                "cadical153", "sha256:" + hashlib.sha256(b"cadical153").hexdigest(),
                "hippius://solver-image", challenge_kind,
            ),
        )

    store.write(_tx)


def _build_store(tmp_path) -> Store:
    return Store(str(tmp_path / "receipts.sqlite"), prefer_env_database_url=False)


def test_bundle_includes_verified_rows_from_both_sources_with_all_public_fields(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)
    _insert_bitset_row(store, rid="b1", hotkey="hkB", challenge_id="pm-t1-e5-def", epoch=5)

    bundle = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)

    assert bundle["schema"] == "cathedral.v2.receipts.v1"
    assert bundle["epoch"] == 5
    assert bundle["count"] == 2
    assert bundle["coldkey_resolution"] == "unavailable"
    assert bundle.get("publisher_signature")
    assert bundle["merkle_root"].startswith("sha256:")

    by_source = {r["source"]: r for r in bundle["receipts"]}
    manifest_receipt = by_source["manifest"]
    bitset_receipt = by_source["bitset"]

    assert manifest_receipt["receipt_id"] == "m1"
    assert manifest_receipt["miner_hotkey"] == "hkA"
    assert manifest_receipt["challenge_kind"] == "unknown"
    assert manifest_receipt["answer_b64"] == base64.b64encode(b"abc123").decode()
    assert manifest_receipt["miner_coldkey"] is None
    assert manifest_receipt["miner_signature"] == "0xminer-signature-m1"

    assert bitset_receipt["receipt_id"] == "b1"
    assert bitset_receipt["miner_hotkey"] == "hkB"
    assert bitset_receipt["challenge_kind"] == "coloring"
    assert bitset_receipt["answer_b64"] == base64.b64encode(b"\x01\x02\x03").decode()
    assert bitset_receipt["solver_id"] == "cadical153"
    assert bitset_receipt["solver_hash"] == "sha256:" + hashlib.sha256(b"cadical153").hexdigest()
    assert bitset_receipt["image_url"] == "hippius://solver-image"


def test_no_scrubbed_fields_anywhere_in_serialized_bundle(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)
    _insert_bitset_row(store, rid="b1", hotkey="hkB", challenge_id="pm-t1-e5-def", epoch=5)

    bundle = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)
    serialized = json.dumps(bundle)

    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in serialized, f"leaked scrubbed field marker: {forbidden}"
    assert "should-never-leak" not in serialized
    assert "internal error detail" not in serialized


def test_leaf_hash_matches_audit_bundle_for_same_row(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)
    _insert_bitset_row(store, rid="b1", hotkey="hkB", challenge_id="pm-t1-e5-def", epoch=5)

    receipts_bundle = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)
    audit_bundle = v2_pipeline.audit_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)

    audit_leaves = set(audit_bundle["leaves"])
    for receipt in receipts_bundle["receipts"]:
        assert receipt["leaf_hash"] in audit_leaves


def test_merkle_root_stable_across_calls(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)

    b1 = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)
    b2 = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)
    assert b1["merkle_root"] == b2["merkle_root"]


def test_unverified_rows_excluded(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc",
                         epoch=5, status="rejected")
    _insert_bitset_row(store, rid="b1", hotkey="hkB", challenge_id="pm-t1-e5-def",
                       epoch=5, status="rejected")

    bundle = v2_receipts.build_receipts_bundle(store, epoch=5, signing_key_hex=SIGNING_KEY_HEX)
    assert bundle["count"] == 0
    assert bundle["receipts"] == []


def test_unknown_epoch_returns_empty_but_valid_signed_envelope(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)

    bundle = v2_receipts.build_receipts_bundle(store, epoch=999, signing_key_hex=SIGNING_KEY_HEX)
    assert bundle["count"] == 0
    assert bundle["epoch"] == 999
    assert bundle.get("publisher_signature")
    assert bundle["merkle_root"] == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_coldkey_resolver_success_populates_miner_coldkey(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)

    def resolver(hotkey: str) -> str | None:
        return {"hkA": "coldkeyA"}.get(hotkey)

    bundle = v2_receipts.build_receipts_bundle(
        store, epoch=5, signing_key_hex=SIGNING_KEY_HEX, coldkey_resolver=resolver)
    assert bundle["coldkey_resolution"] == "live"
    assert bundle["receipts"][0]["miner_coldkey"] == "coldkeyA"


def test_coldkey_resolver_raising_still_emits_receipts_with_null_coldkey(tmp_path):
    store = _build_store(tmp_path)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e5-abc", epoch=5)

    def bad_resolver(hotkey: str) -> str | None:
        raise RuntimeError("metagraph is down")

    bundle = v2_receipts.build_receipts_bundle(
        store, epoch=5, signing_key_hex=SIGNING_KEY_HEX, coldkey_resolver=bad_resolver)
    assert bundle["count"] == 1
    assert bundle["receipts"][0]["miner_coldkey"] is None


def test_make_coldkey_resolver_missing_table_resolves_to_none(tmp_path):
    """No coldkey_map rows present -> resolver must degrade to None, never raise."""
    store = _build_store(tmp_path)
    resolver = v2_receipts.make_coldkey_resolver(store)
    assert resolver("any-hotkey") is None


# ---- Endpoint tests ---------------------------------------------------------


def _build_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "true")
    monkeypatch.setenv("CATHEDRAL_V2_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "receipts-test-seed")
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    return app, Store(db)


def test_receipts_epoch_endpoint_returns_bundle(tmp_path, monkeypatch):
    app, store = _build_app(tmp_path, monkeypatch)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e7-abc", epoch=7)
    client = TestClient(app)

    r = client.get("/v2/receipts/epochs/7")
    assert r.status_code == 200
    body = r.json()
    assert body["schema"] == "cathedral.v2.receipts.v1"
    assert body["epoch"] == 7
    assert body["count"] == 1
    assert r.headers["access-control-allow-origin"] == "*"
    assert "max-age=60" in r.headers["cache-control"]


def test_receipts_latest_endpoint_resolves_most_recent_verified_epoch(tmp_path, monkeypatch):
    app, store = _build_app(tmp_path, monkeypatch)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e3-abc", epoch=3)
    _insert_bitset_row(store, rid="b1", hotkey="hkB", challenge_id="pm-t1-e9-def", epoch=9)
    client = TestClient(app)

    r = client.get("/v2/receipts/latest")
    assert r.status_code == 200
    body = r.json()
    assert body["epoch"] == 9
    assert body["count"] == 1
    assert body["receipts"][0]["receipt_id"] == "b1"


def test_receipts_endpoint_is_ttl_cached(tmp_path, monkeypatch):
    app, store = _build_app(tmp_path, monkeypatch)
    _insert_manifest_row(store, rid="m1", hotkey="hkA", challenge_id="pm-t1-e7-abc", epoch=7)
    client = TestClient(app)

    calls = {"n": 0}
    real_build = v2_receipts.build_receipts_bundle

    def counting_build(*args, **kwargs):
        calls["n"] += 1
        return real_build(*args, **kwargs)

    monkeypatch.setattr(v2_receipts, "build_receipts_bundle", counting_build)

    r1 = client.get("/v2/receipts/epochs/7")
    r2 = client.get("/v2/receipts/epochs/7")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert calls["n"] == 1, "second call within TTL should hit the cache, not recompute"


def test_disabled_flag_returns_404(tmp_path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_SERVICE_ROLE", "all")
    monkeypatch.setenv("CATHEDRAL_RATELIMIT_RPM", "0")
    monkeypatch.setenv("CATHEDRAL_V2_ENABLED", "false")
    monkeypatch.setenv("CATHEDRAL_CNF_TOKEN_SECRET", "test-secret")
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    client = TestClient(app)

    assert client.get("/v2/receipts/epochs/1").status_code == 404
    assert client.get("/v2/receipts/latest").status_code == 404
