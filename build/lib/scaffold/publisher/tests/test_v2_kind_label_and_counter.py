"""V2 per-miner challenge `kind` label + attributed real-vs-planted counter.

Background: on the V2 fast path, ~10% of per-miner challenges are REAL
(graph-coloring / Latin-square) instances (see real_corpus.py), the rest are
planted random 3SAT. Before this fix every challenge was mislabeled
`kind="random_3sat_perminer"` regardless of what `per_miner.generate_instance`
actually returned, and there was no way to count real-vs-planted solves.

Covers:
  1. The per-miner challenges-list handler's `item["kind"]` now reflects the
     ACTUAL generation result (`planted is None` <=> real) instead of the
     hardcoded default.
  2. `/v2/agents/submit-bitset` stores `challenge_kind` on the verified
     v2_submit_events row (derived the same way) and echoes it on the receipt.
  3. `/v2/verify/metrics` exposes an attributed `by_kind` block (totals /
     kinds / by_hotkey) computed from verified v2_submit_events, tolerating
     NULL challenge_kind (pre-existing rows) as "unknown".

This is metadata only — it must never change scoring/verification/eligibility.
"""
from __future__ import annotations

import base64
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
_EMPTY_BUNDLE = "af1349b9f5f9a1a6a0404dea36dcc9499bcb25c9adc112b7cc9a93cae41f3262"


def _now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _keypair(uri: str):
    from bittensor_wallet import Keypair
    return Keypair.create_from_uri(uri)


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
    monkeypatch.setenv("CATHEDRAL_V2_PERMINER_SEED_SECRET", "kind-label-counter-test-seed")
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
    monkeypatch.delenv("CATHEDRAL_V2_REAL_FRACTION", raising=False)
    db = str(tmp_path / "pub.sqlite")
    app = build_app(database_path=db, signing_key_hex=SIGNING_KEY_HEX)
    # v2 rows (v2_submit_events receipts) live in the separate V2 store the app
    # wires from CATHEDRAL_V2_DB_PATH above, not the main publisher DB.
    return app, Store(str(tmp_path / "v2.sqlite"))


def _fetch_item(client, kp, *, tier: int) -> dict:
    board = client.get(
        "/v2/synthetic-boolean/per-miner/challenges?limit=10",
        headers=_read_headers(kp),
    )
    assert board.status_code == 200
    items = board.json()["items"]
    match = next(i for i in items if int(i["tier"]) == tier)
    return match


def _submit_and_assert_verified(client, kp, item):
    """Solve the CNF the miner actually fetches and submit via the bitset path."""
    cnf_resp = client.get(
        f"/v2/synthetic-boolean/per-miner/cnf?challenge_id={item['challenge_id']}"
        f"&tier={item['tier']}&seq={item['seq']}",
        headers=_read_headers(kp),
    )
    assert cnf_resp.status_code == 200
    cnf_text = cnf_resp.text
    actual_nvars, _clauses = parse_cnf(cnf_text)
    assert actual_nvars == item["n_vars"]

    if item.get("kind") == "random_3sat_perminer":
        with v2_pipeline.v2_pm_env():
            _cid, _cnf, planted = pm.generate_instance(
                kp.ss58_address, int(item["epoch"]), int(item["tier"]), int(item["seq"]))
        assert planted is not None
        assignment = planted
    else:
        assignment = solve_cnf(cnf_text)
        assert assignment is not None

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
    assert receipt["status"] == "received"

    results = v2_pipeline.process_bitset_batch(
        client.app.state.v2_store, worker_id="test-kind", batch_size=8, lock_secs=60)
    assert len(results) == 1
    assert results[0]["status"] == v2_pipeline.STATUS_VERIFIED, results[0]

    final = client.get(receipt["receipt_url"])
    assert final.status_code == 200
    final_payload = final.json()
    assert final_payload["status"] == "verified"
    return final_payload


# --------------------------------------------------------------------------
# Task 1: challenges-list `kind` label
# --------------------------------------------------------------------------

def test_real_source_reports_real_kind_matching_actual_generation(tmp_path, monkeypatch):
    """With CATHEDRAL_V2_CHALLENGE_SOURCE=combinatorial (real, unplanted
    instances), the challenges-list handler must report kind in
    {coloring, latin} — NOT the hardcoded random_3sat_perminer default — and
    it must match what generate_instance actually produced (planted is None),
    and n_vars must match the real CNF's actual var count."""
    app, _store = _build(tmp_path, monkeypatch, source="combinatorial", kind="coloring")
    client = TestClient(app)
    kp = _keypair("//KindLabelRealColoring")
    item = _fetch_item(client, kp, tier=1)

    epoch = int(item["epoch"])
    with v2_pipeline.v2_pm_env():
        cid, cnf_text, planted = pm.generate_instance(
            kp.ss58_address, epoch, int(item["tier"]), int(item["seq"]))
    assert cid == item["challenge_id"]
    assert planted is None  # confirms this really is a REAL instance

    expected_kind = real_corpus.kind_for(
        epoch, int(item["tier"]), int(item["seq"]), salt=kp.ss58_address)
    assert expected_kind == "coloring"
    assert item["kind"] == expected_kind
    assert item["kind"] in ("coloring", "latin")

    actual_nvars, _clauses = parse_cnf(cnf_text)
    assert item["n_vars"] == actual_nvars


def test_real_source_latin_kind_matches_actual_generation(tmp_path, monkeypatch):
    app, _store = _build(tmp_path, monkeypatch, source="combinatorial", kind="latin")
    client = TestClient(app)
    kp = _keypair("//KindLabelRealLatin")
    item = _fetch_item(client, kp, tier=1)

    epoch = int(item["epoch"])
    expected_kind = real_corpus.kind_for(
        epoch, int(item["tier"]), int(item["seq"]), salt=kp.ss58_address)
    assert expected_kind == "latin"
    assert item["kind"] == "latin"


def test_default_planted_source_still_reports_random_3sat_perminer(tmp_path, monkeypatch):
    """Guardrail: the default 'planted' source is unchanged — item["kind"]
    stays 'random_3sat_perminer' for every miner-instance."""
    app, _store = _build(tmp_path, monkeypatch, source=None)
    client = TestClient(app)
    assert real_corpus.challenge_source() == "planted"
    kp = _keypair("//KindLabelPlantedDefault")
    item = _fetch_item(client, kp, tier=1)
    assert item["kind"] == "random_3sat_perminer"

    epoch = int(item["epoch"])
    with v2_pipeline.v2_pm_env():
        _cid, _cnf, planted = pm.generate_instance(
            kp.ss58_address, epoch, int(item["tier"]), int(item["seq"]))
    assert planted is not None


# --------------------------------------------------------------------------
# Task 2: attributed real-vs-planted counter
# --------------------------------------------------------------------------

def test_verify_metrics_by_kind_counts_real_and_planted_solves(tmp_path, monkeypatch):
    # Real solve.
    app, store = _build(tmp_path, monkeypatch, source="combinatorial", kind="coloring")
    client = TestClient(app)
    kp_real = _keypair("//CounterRealSolver")
    item_real = _fetch_item(client, kp_real, tier=1)
    receipt_real = _submit_and_assert_verified(client, kp_real, item_real)
    assert receipt_real["challenge_kind"] == "coloring"

    row = v2_bitset_submit.get_receipt(store, receipt_real["receipt_id"])
    assert row["challenge_kind"] == "coloring"

    metrics = client.get("/v2/verify/metrics")
    assert metrics.status_code == 200
    by_kind = metrics.json()["by_kind"]
    assert by_kind["totals"]["real"] == 1
    assert by_kind["totals"]["planted"] == 0
    assert by_kind["kinds"]["coloring"] == 1
    assert by_kind["by_hotkey"][kp_real.ss58_address]["real"] == 1
    assert by_kind["by_hotkey"][kp_real.ss58_address]["planted"] == 0


def test_verify_metrics_by_kind_counts_planted_solve(tmp_path, monkeypatch):
    app, store = _build(tmp_path, monkeypatch, source=None)
    client = TestClient(app)
    kp = _keypair("//CounterPlantedSolver")
    item = _fetch_item(client, kp, tier=1)
    receipt = _submit_and_assert_verified(client, kp, item)
    assert receipt["challenge_kind"] == "random_3sat_perminer"

    metrics = client.get("/v2/verify/metrics")
    by_kind = metrics.json()["by_kind"]
    assert by_kind["totals"]["planted"] == 1
    assert by_kind["totals"]["real"] == 0
    assert by_kind["kinds"]["random_3sat_perminer"] == 1
    assert by_kind["by_hotkey"][kp.ss58_address]["planted"] == 1
    assert by_kind["by_hotkey"][kp.ss58_address]["real"] == 0


def test_verify_metrics_by_kind_tolerates_null_challenge_kind_as_unknown(tmp_path, monkeypatch):
    """Pre-existing rows from before this migration have NULL challenge_kind —
    the counter must bucket them as 'unknown', not crash."""
    app, store = _build(tmp_path, monkeypatch, source=None)
    client = TestClient(app)
    kp = _keypair("//CounterNullKindLegacyRow")
    item = _fetch_item(client, kp, tier=1)
    receipt = _submit_and_assert_verified(client, kp, item)

    # Simulate a pre-migration row by nulling out challenge_kind directly.
    def _null_it(conn):
        conn.execute(
            "UPDATE v2_submit_events SET challenge_kind = NULL WHERE id = ?",
            (receipt["receipt_id"],),
        )
    store.write(_null_it)

    metrics = client.get("/v2/verify/metrics")
    assert metrics.status_code == 200
    by_kind = metrics.json()["by_kind"]
    assert by_kind["totals"]["unknown"] == 1
    assert by_kind["totals"]["real"] == 0
    assert by_kind["totals"]["planted"] == 0
    assert by_kind["kinds"]["unknown"] == 1
    # Unknown rows are not attributed in by_hotkey's real/planted breakdown.
    assert kp.ss58_address not in by_kind["by_hotkey"]


def test_verify_metrics_by_kind_never_touches_scoring(tmp_path, monkeypatch):
    """Guardrail: challenge_kind is metadata only — weighted_score/eligibility
    are unaffected by the real/planted distinction."""
    app, _store = _build(tmp_path, monkeypatch, source="combinatorial", kind="coloring")
    client = TestClient(app)
    kp = _keypair("//KindNeverTouchesScoring")
    item = _fetch_item(client, kp, tier=1)
    receipt = _submit_and_assert_verified(client, kp, item)
    assert receipt["weighted_score"] == item["difficulty_weight"]
    assert receipt["eligibility_status"] == "eligible_beta"
