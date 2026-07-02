"""Real-instance (CATHEDRAL_V2_CHALLENGE_SOURCE=combinatorial) V2 bitset e2e.

Regression coverage for the shape-mismatch bug: the V2 per-miner bitset path
used to bind the submit token's `nvars` (and the reported `n_vars`) to the
NOMINAL tier shape (`per_miner.shape_for(tier)`, e.g. 400 vars) instead of the
actual generated CNF's var count. That's fine for the default "planted"
source (its CNFs are always exactly `shape_for(tier)` vars) but breaks for
the real-instance sources in real_corpus.py, whose CNFs have their own size
(e.g. 24 vars tier1 / 33 vars tier2 for the graph-coloring generator) — every
bitset submission was rejected with `submit_token_shape_mismatch` or failed
the witness check.

This drives the REAL end-to-end path in-process via TestClient: fetch a real
per-miner challenge, solve it with the scaffold's own DPLL (`dimacs.solve_cnf`
— there is no planted witness for real instances), encode the bitset at the
REAL size, and submit. Also asserts the planted default is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from starlette.testclient import TestClient

from scaffold.dimacs import parse_cnf, solve_cnf
from scaffold.publisher import v2_pipeline
from scaffold.publisher import v2_bitset_submit
from scaffold.publisher import per_miner as pm
from scaffold.publisher import real_corpus
from scaffold.publisher.app import build_app
from scaffold.publisher.auth import canonical_claim_bytes
from scaffold.publisher.store import Store


_FAMILY = "synthetic_boolean_v1"
SIGNING_KEY_HEX = "11" * 32


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


def _read_headers(kp, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    empty_bundle = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"
    msg = canonical_claim_bytes(
        bundle_hash=empty_bundle,
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


def _bitset_headers(kp, body: dict, *, submitted_at: str | None = None) -> dict[str, str]:
    ts = submitted_at or _now_iso()
    submit = v2_bitset_submit.normalize_submit_body(
        body, miner_hotkey=kp.ss58_address, submitted_at=ts, card_id=_FAMILY,
    )
    sig = base64.b64encode(kp.sign(v2_bitset_submit.canonical_submit_bytes(submit))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def _build(tmp_path, monkeypatch, *, source: str | None = None, kind: str | None = None):
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
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "real-instance-e2e-test-seed")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T1", "2")
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_ALLOTMENT_T2", "2")
    monkeypatch.setenv("CATHEDRAL_V2_DB_PATH", str(tmp_path / "v2.sqlite"))
    if source is not None:
        monkeypatch.setenv("CATHEDRAL_V2_CHALLENGE_SOURCE", source)
    else:
        monkeypatch.delenv("CATHEDRAL_V2_CHALLENGE_SOURCE", raising=False)
    if kind is not None:
        monkeypatch.setenv("CATHEDRAL_V2_COMBINATORIAL_KIND", kind)
    else:
        monkeypatch.delenv("CATHEDRAL_V2_COMBINATORIAL_KIND", raising=False)
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    return app, Store(db)


def _fetch_item(client, kp, *, tier: int) -> dict:
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=10",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    items = board.json()["items"]
    match = next(i for i in items if int(i["tier"]) == tier)
    return match


def _submit_and_assert_verified(client, kp, item, *, expected_nvars: int):
    """Solve the CNF the miner actually fetches, submit via the bitset path,
    and assert the receipt is accepted and verified."""
    assert item["n_vars"] == expected_nvars

    cnf_resp = client.get(
        f"/v2/synthetic-boolean/per-miner/cnf?challenge_id={item['challenge_id']}"
        f"&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp),
    )
    assert cnf_resp.status_code == 200
    cnf_text = cnf_resp.text
    actual_nvars, _clauses = parse_cnf(cnf_text)
    assert actual_nvars == expected_nvars
    # The cnf-endpoint-minted token (X-Cathedral-Submit-Token header) must carry
    # the SAME real nvars as the challenges-list-minted token (item["submit_token"]).
    assert cnf_resp.headers["x-cathedral-submit-token"]

    assignment = solve_cnf(cnf_text)
    assert assignment is not None
    assert len(assignment) == expected_nvars

    assignment_b64 = base64.b64encode(
        v2_pipeline.encode_bitset_assignment(assignment)
    ).decode("ascii")
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
    assert receipt["schema"] == "cathedral.v2.submit_bitset_receipt.v1"
    assert receipt["status"] == "verified"
    return receipt


def test_real_instance_combinatorial_bitset_e2e_verifies_at_real_shape(tmp_path, monkeypatch):
    """CATHEDRAL_V2_CHALLENGE_SOURCE=combinatorial + coloring kind: tier1 CNFs
    are 8 nodes * 3 colors = 24 vars, tier2 are 11 * 3 = 33 vars — NOT the
    nominal shape_for(tier) (400 for both, by default). Before the fix, the
    submit token/reported n_vars was pinned to shape_for(tier)=400, so every
    submission on this path was rejected with submit_token_shape_mismatch."""
    app, _store = _build(tmp_path, monkeypatch, source="combinatorial", kind="coloring")
    client = TestClient(app)

    # Sanity: the nominal tier shape really does differ from the real CNF size,
    # so this test is actually exercising the mismatch the bug produced.
    nominal_t1 = pm.shape_for(1)[0]
    nominal_t2 = pm.shape_for(2)[0]
    n_nodes_1, k_1 = real_corpus.coloring_shape_for(1)
    n_nodes_2, k_2 = real_corpus.coloring_shape_for(2)
    real_t1 = n_nodes_1 * k_1
    real_t2 = n_nodes_2 * k_2
    assert real_t1 != nominal_t1
    assert real_t2 != nominal_t2

    kp1 = _keypair("//RealInstanceBitsetT1")
    item1 = _fetch_item(client, kp1, tier=1)
    _submit_and_assert_verified(client, kp1, item1, expected_nvars=real_t1)

    kp2 = _keypair("//RealInstanceBitsetT2")
    item2 = _fetch_item(client, kp2, tier=2)
    _submit_and_assert_verified(client, kp2, item2, expected_nvars=real_t2)


def test_planted_default_source_still_mints_nominal_tier_shape(tmp_path, monkeypatch):
    """Guardrail: for the default 'planted' source, generated CNFs already have
    exactly shape_for(tier) vars, so the fix (binding to the parsed CNF instead
    of shape_for) must be a complete no-op — token/reported nvars == shape_for
    still, and the bitset path still verifies end to end."""
    app, _store = _build(tmp_path, monkeypatch, source=None)
    client = TestClient(app)
    assert real_corpus.challenge_source() == "planted"

    kp = _keypair("//PlantedDefaultStillWorks")
    item = _fetch_item(client, kp, tier=1)
    assert item["n_vars"] == pm.shape_for(1)[0]
    assert item["submit_token"]

    receipt = _submit_and_assert_verified(client, kp, item, expected_nvars=pm.shape_for(1)[0])
    assert receipt["weighted_score"] == item["difficulty_weight"]
