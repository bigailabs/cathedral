"""publisher_verify.py — the thin publisher's self-compat gate.

Proves the publisher emits rows that (a) verify under its own loaded key via
wire.verify_row, and (b) have EXACTLY the signed-key shape of same-version live
rows in fixtures (key-set equality per schema version 5/6). Also pins:

  * the sr25519 golden vectors (//Alice round-trip + tamper/wrong-key reject),
  * the 9 adversarial + 3 golden DIMACS-solution fixtures (Lane A referee),
  * the active-cnf token flow + constant-time compare + opaque 404,
  * the signed audit-scanner bridge (replay -> submit -> hash-only ledger),
  * the full M4 end-to-end loop (miner signs, fetches CNF, solves with DPLL,
    submits; validator-style tuple-cursor pull verifies every signature).

Run with a python that has cryptography + bittensor_wallet + fastapi
(the cathedral venv): ~/code/cathedral/.venv/bin/python publisher_verify.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path

from scaffold import wire
from scaffold.publisher import build_app, seed_challenge
from scaffold.publisher import rows as rowmod
from scaffold.publisher import tee_gpu as _tee_gpu
from scaffold.publisher import weights as _weights
from scaffold.publisher.auth import (
    ALICE_SS58, canonical_claim_bytes, default_verifier,
)
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher.sat_solution import verify_dimacs_solution
from scaffold.dimacs import gen_planted_3sat, solve_cnf

for _env_key in (_weights.MODE_ENV, _weights.TIER_WEIGHTS_ENV, _weights.TIER2_MULT_ENV):
    os.environ.pop(_env_key, None)

FIX = Path(__file__).parent / "fixtures" / "live-20260609"
checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


# --------------------------------------------------------------------------
# 1. Self-compat: emitted rows verify + match live signed-key shape per version.
# --------------------------------------------------------------------------
print("SELF-COMPAT — emitted rows verify + match live signed-key shape")
key_hex = generate_test_key()
pub_hex = rowmod.public_key_hex(key_hex)

emitted = rowmod.build_solve_rows(
    row_uuid="11111111-1111-1111-1111-111111111111",
    miner_hotkey="5G3r19ZK3Gipvh5M35JYQ43qWS7cLuMfXVAwee8duwXoHzKH",
    agent_id="22222222-2222-2222-2222-222222222222",
    challenge_id="sat-t1-demo", tier=1, weighted_score=1.0,
    answer_hash="ab" * 32, verifier_details_hash="cd" * 32,
    ran_at="2026-06-09T00:00:00.000Z", epoch_salt="epoch_20260609:synthetic_boolean_v1",
    solve_rank=1, solved=True, private_key_hex=key_hex,
)
v6 = next(r for r in emitted if r["eval_output_schema_version"] == 6)
v5 = next(r for r in emitted if r["eval_output_schema_version"] == 5)

ck("emitted v6 row verifies under loaded key", wire.verify_row(v6, pub_hex))
ck("emitted v5 row verifies under loaded key", wire.verify_row(v5, pub_hex))
ck("v5 id is the v6 id + '-v5compat' (live suffix convention)",
   v5["id"] == v6["id"] + "-v5compat")

# key-set equality of the SIGNED subset against same-version live rows.
live = json.loads((FIX / "rows.json").read_text())["items"]
live_v6 = next(r for r in live if int(r.get("eval_output_schema_version", 1)) == 6)
live_v5 = next(r for r in live if int(r.get("eval_output_schema_version", 1)) == 5)
ck("v6 signed-key set == live v6 signed-key set",
   set(wire.signed_payload(v6)) == set(wire.signed_payload(live_v6)))
ck("v5 signed-key set == live v5 signed-key set",
   set(wire.signed_payload(v5)) == set(wire.signed_payload(live_v5)))
# tamper: any signed field breaks; post-signing fields don't.
import copy
t = copy.deepcopy(v6); t["weighted_score"] = 0.5
ck("tampered weighted_score breaks emitted-row signature", not wire.verify_row(t, pub_hex))
t = copy.deepcopy(v6); t["output_card"] = {"x": 1}
ck("output_card is post-signing (mutation ignored)", wire.verify_row(t, pub_hex))

# JWKS shape derived from the key matches live jwks shape.
jwks = rowmod.jwks_from_key(key_hex)
live_jwks = json.loads((FIX / "jwks.json").read_text())
live_key = next(k for k in live_jwks["keys"] if k["kid"] == "cathedral-eval-signing")
ck("derived JWKS key-set matches live jwks key shape",
   set(jwks["keys"][0]) >= (set(live_key) - {"purpose"}))

# --------------------------------------------------------------------------
# 2. sr25519 golden vectors (//Alice) — the real backend, fail-closed otherwise.
# --------------------------------------------------------------------------
print("AUTH — sr25519 golden vectors (//Alice)")
verifier = default_verifier()
backend = getattr(verifier, "backend", "bittensor")
ck(f"sr25519 backend is the production one (got: {backend})", backend != "stub-fail-closed")
try:
    from bittensor_wallet import Keypair
    alice = Keypair.create_from_uri("//Alice")
    bob = Keypair.create_from_uri("//Bob")
    ck("//Alice ss58 matches pinned vector", alice.ss58_address == ALICE_SS58)
    msg = canonical_claim_bytes(
        bundle_hash="00" * 32, card_id="synthetic_boolean_v1",
        miner_hotkey=alice.ss58_address, submitted_at="2026-06-09T00:00:00.000Z",
        challenge_id="c1", dimacs_solution_sha256="ab" * 32)
    sig = base64.b64encode(alice.sign(msg)).decode("ascii")
    ck("Alice 6-field claim verifies", verifier.verify(alice.ss58_address, msg, sig))
    ck("wrong hotkey (Bob) rejects Alice's sig", not verifier.verify(bob.ss58_address, msg, sig))
    ck("tampered message rejects", not verifier.verify(alice.ss58_address, msg + b"x", sig))
    ck("garbage sig rejects",
       not verifier.verify(alice.ss58_address, msg, base64.b64encode(b"\x00" * 64).decode()))
except Exception as e:  # backend unavailable -> these are skipped but recorded
    ck(f"sr25519 golden vectors runnable (import ok) — {e}", False)

# --------------------------------------------------------------------------
# 3. DIMACS-solution fixtures: 3 golden score, 9 adversarial reject by reason.
# --------------------------------------------------------------------------
print("LANE A — 3 golden + 9 adversarial DIMACS-solution fixtures")
golden = [(1, 0, "v 1 0"), (42, 1, "v 1 2 3 0"), (7, 2, "v 1 -2 3 4 0")]
# Generate small planted instances the scaffold DPLL can close.
_TIER_NV = {0: 10, 1: 12, 2: 14}
g_ok = 0
for seed, tier, _vline in golden:
    nv = _TIER_NV[tier]
    cnf, planted = gen_planted_3sat(seed, nv, nv * 3)
    sol = solve_cnf(cnf)
    blob = "s SATISFIABLE\nv " + " ".join(str(x) for x in sol) + " 0\n"
    chk = verify_dimacs_solution(cnf, blob)
    if chk.ok:
        g_ok += 1
ck(f"golden: honest DPLL solves verify ({g_ok}/3)", g_ok == 3)

# adversarial — build a tier-1 instance, then exercise each rejection class.
cnf, planted = gen_planted_3sat(42, 12, 36)
nv = 12
adversarial = [
    ("answer_missing_dimacs_solution", None),
    ("solution_missing_status", "v 1 2 3 0\n"),
    ("solution_unknown_status", "s ROOMBA\nv 1 2 3 0\n"),
    ("solution_status_unsatisfiable", "s UNSATISFIABLE\n"),
    ("solution_non_integer_literal", "s SATISFIABLE\nv 1 two 3 0\n"),
    ("solution_variable_out_of_range", "s SATISFIABLE\nv 1 2 99 0\n"),
    ("solution_contradictory_assignment", "s SATISFIABLE\nv 1 -1 2 3 0\n"),
    ("solution_incomplete_assignment", "s SATISFIABLE\nv 1 2 0\n"),
    ("solution_unsatisfied",
     "s SATISFIABLE\nv " + " ".join(str(-x) for x in planted) + " 0\n"),
]
adv_ok = 0
crashes = 0
for expected, blob in adversarial:
    try:
        chk = verify_dimacs_solution(cnf, blob)
        if (not chk.ok) and chk.rejection_reason == expected:
            adv_ok += 1
        else:
            print(f"    MISMATCH {expected!r} -> {chk.rejection_reason!r}")
    except Exception as e:
        crashes += 1
        print(f"    CRASH on {expected}: {e}")
ck(f"adversarial: each rejected with the right reason ({adv_ok}/9)", adv_ok == 9)
ck(f"verifier totality: 0 crashes on hostile solution input ({crashes})", crashes == 0)

# --------------------------------------------------------------------------
# 4. End-to-end: miner signs -> fetch CNF (token) -> solve -> submit;
#    validator-style tuple-cursor pull verifies every signature.
# --------------------------------------------------------------------------
print("END-TO-END — miner solve + validator pull loop")
from fastapi.testclient import TestClient  # noqa: E402

app = build_app(database_path=":memory:", signing_key_hex=key_hex, submit_min_interval_secs=0)
store = app.state.store
cnf_e2e, _ = gen_planted_3sat(123, 14, 42)
seed_challenge(store, challenge_id="sat-e2e-1", tier=1, cnf_text=cnf_e2e)

with TestClient(app) as client:
    # health + jwks shape
    h = client.get("/health").json()
    ck("/health reports ok", h["status"] == "ok")
    ck("/health is live-fast and points to readiness",
       h["kind"] == "live" and h["db"] == "not_checked"
       and h["ready_path"] == "/health/ready")
    h_live = client.get("/health/live").json()
    ck("/health/live reports live without DB dependency",
       h_live["status"] == "ok" and h_live["kind"] == "live"
       and h_live["db"] == "not_checked")
    h_ready = client.get("/health/ready").json()
    ck("/health/ready checks the backing store",
       h_ready["status"] == "ok" and h_ready["kind"] == "ready"
       and h_ready["db"] == "ok")
    with store.advisory_lock("publisher_verify_sqlite") as _got_lock:
        ck("sqlite advisory lock path allows singleton tasks", _got_lock is True)
    j = client.get("/.well-known/cathedral-jwks.json").json()
    ck("/.well-known jwks served with eval-signing key",
       any(k["kid"] == "cathedral-eval-signing" for k in j["keys"]))

    # active-challenges field-compat (board.json shape)
    ac = client.get("/v1/synthetic-boolean/active-challenges").json()
    board_keys = set(json.loads((FIX / "board.json").read_text())["items"][0])
    pub_keys = set(ac["items"][0])
    ck("active-challenges item carries the live board fields",
       board_keys - {"difficulty_label", "storage"} <= pub_keys)
    ck("active-challenges exposes scoring and tier distribution",
       ac["scoring"]["mode"] == "proportional"
       and ac["distribution"]["total_challenges"] == ac["count"]
       and ac["distribution"]["tiers"][0]["count"] == ac["count"])
    generator = ac.get("generator") or {}
    generator_tiers = {int(t["tier"]): t for t in generator.get("tiers", [])}
    ck("active-challenges exposes SAT generator refill policy",
       generator.get("kind") == "local_refill"
       and generator.get("source") == "scaffold.publisher.refill"
       and generator_tiers.get(1, {}).get("method") == "biased"
       and generator_tiers.get(2, {}).get("method") == "ajm"
       and generator_tiers.get(1, {}).get("target_active") == 25
       and generator_tiers.get(2, {}).get("target_active") == 25)
    bc = client.get("/v1/synthetic-boolean/challenge-broadcast")
    ck("challenge-broadcast serves the same cacheable board snapshot",
       bc.status_code == 200
       and bc.json()["items"][0]["challenge_id"] == ac["items"][0]["challenge_id"]
       and bool(bc.headers.get("etag"))
       and bc.headers.get("x-cathedral-board-rebuilds") is not None)
    bc_304 = client.get(
        "/v1/synthetic-boolean/challenge-broadcast",
        headers={"If-None-Match": bc.headers.get("etag", "")},
    )
    ck("challenge-broadcast supports ETag 304", bc_304.status_code == 304)

    old_role_env = {
        "CATHEDRAL_SERVICE_ROLE": os.environ.get("CATHEDRAL_SERVICE_ROLE"),
        "CATHEDRAL_REFILL_ENABLED": os.environ.get("CATHEDRAL_REFILL_ENABLED"),
    }
    try:
        os.environ["CATHEDRAL_SERVICE_ROLE"] = "read"
        os.environ["CATHEDRAL_REFILL_ENABLED"] = "true"
        role_app = build_app(database_path=":memory:", signing_key_hex=key_hex,
                             submit_min_interval_secs=0)
        with TestClient(role_app) as role_client:
            role_health = role_client.get("/health/live").json()
            ck("service role appears in health", role_health["service_role"] == "read")
            ck("read role does not start the refill worker",
               role_app.state.service_role == "read" and role_app.state.refill_task is None)
            read_submit = role_client.post("/v1/agents/submit", data={})
            ck("read role rejects submit traffic before route work",
               read_submit.status_code == 404
               and read_submit.text == "route_not_served_by_read_role"
               and read_submit.headers.get("x-cathedral-service-role") == "read")

        os.environ["CATHEDRAL_SERVICE_ROLE"] = "submit"
        os.environ["CATHEDRAL_REFILL_ENABLED"] = "false"
        submit_role_app = build_app(database_path=":memory:", signing_key_hex=key_hex,
                                    submit_min_interval_secs=0)
        with TestClient(submit_role_app) as submit_role_client:
            submit_role_health = submit_role_client.get("/health/live").json()
            ck("submit role appears in health",
               submit_role_health["service_role"] == "submit")
            submit_read = submit_role_client.get("/v1/leaderboard/top")
            ck("submit role rejects leaderboard traffic before route work",
               submit_read.status_code == 404
               and submit_read.text == "route_not_served_by_submit_role"
               and submit_read.headers.get("x-cathedral-service-role") == "submit")
            submit_cnf = submit_role_client.get("/v1/synthetic-boolean/active-cnf")
            ck("submit role allows miner CNF route to reach auth validation",
               submit_cnf.status_code == 422)

        os.environ["CATHEDRAL_SERVICE_ROLE"] = "worker"
        worker_role_app = build_app(database_path=":memory:", signing_key_hex=key_hex,
                                    submit_min_interval_secs=0)
        with TestClient(worker_role_app) as worker_role_client:
            worker_role_health = worker_role_client.get("/health/live").json()
            ck("worker role appears in health",
               worker_role_health["service_role"] == "worker")
            worker_public = worker_role_client.get("/v1/synthetic-boolean/active-challenges")
            ck("worker role rejects public miner reads",
               worker_public.status_code == 404
               and worker_public.text == "route_not_served_by_worker_role"
               and worker_public.headers.get("x-cathedral-service-role") == "worker")

        from scaffold.publisher import retention as _retention
        from datetime import datetime, timezone, timedelta

        retention_app = build_app(database_path=":memory:", signing_key_hex=key_hex,
                                  submit_min_interval_secs=0)
        retention_store = retention_app.state.store
        now_dt = datetime.now(timezone.utc)
        old_iso = (now_dt - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        recent_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        current_epoch = int(now_dt.timestamp()) // 3600

        def _seed_retention_rows(conn):
            conn.execute(
                "INSERT INTO eval_runs(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                "VALUES ('old-eval', ?, 6, 'hk-old', ?, '{}')",
                (old_iso, "synthetic_boolean_v1"))
            conn.execute(
                "INSERT INTO eval_runs(id, ran_at, eval_output_schema_version, miner_hotkey, task_type, row_json) "
                "VALUES ('recent-eval', ?, 6, 'hk-new', ?, '{}')",
                (recent_iso, "synthetic_boolean_v1"))
            conn.execute(
                "INSERT INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
                "VALUES ('old-shared', 'hk-old', ?)",
                (old_iso,))
            conn.execute(
                "INSERT INTO lane_challenge_solves(challenge_id, miner_hotkey, solved_at_iso) "
                "VALUES ('recent-shared', 'hk-new', ?)",
                (recent_iso,))
            conn.execute(
                "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, status, "
                "rejection_reason, dimacs_solution_sha256, submitted_at, recorded_at_iso, signature) "
                "VALUES ('old-attempt', 'old-pm', 'hk-old', 1, 'rejected', 'x', '00', ?, ?, 'sig-old')",
                (old_iso, old_iso))
            conn.execute(
                "INSERT INTO per_miner_attempts(id, challenge_id, miner_hotkey, epoch, status, "
                "rejection_reason, dimacs_solution_sha256, submitted_at, recorded_at_iso, signature) "
                "VALUES ('recent-attempt', 'recent-pm', 'hk-new', ?, 'ranked', NULL, '00', ?, ?, 'sig-new')",
                (current_epoch, recent_iso, recent_iso))
            conn.execute(
                "INSERT INTO per_miner_solves(challenge_id, miner_hotkey, epoch, tier, seq, "
                "difficulty_weight, verified, solved_at_iso) VALUES ('old-pm', 'hk-old', 1, 1, 0, 1, 1, ?)",
                (old_iso,))
            conn.execute(
                "INSERT INTO per_miner_solves(challenge_id, miner_hotkey, epoch, tier, seq, "
                "difficulty_weight, verified, solved_at_iso) VALUES ('recent-pm', 'hk-new', ?, 1, 0, 1, 1, ?)",
                (current_epoch, recent_iso))
            conn.execute(
                "INSERT INTO per_miner_assignments(challenge_id, miner_hotkey, epoch, tier, seq, "
                "difficulty_weight, assigned_at_iso) VALUES ('old-assignment', 'hk-old', 1, 1, 0, 1, ?)",
                (old_iso,))
            conn.execute(
                "INSERT INTO per_miner_assignments(challenge_id, miner_hotkey, epoch, tier, seq, "
                "difficulty_weight, assigned_at_iso) VALUES ('recent-assignment', 'hk-new', ?, 1, 0, 1, ?)",
                (current_epoch, recent_iso))

        retention_store.write(_seed_retention_rows)
        retention_summary = _retention.retention_tick(retention_store, now=now_dt)
        deleted = retention_summary["deleted"]
        ck("retention prunes old high-volume rows in bounded batches",
           deleted["eval_runs"] == 1
           and deleted["lane_challenge_solves"] == 1
           and deleted["per_miner_attempts"] == 1
           and deleted["per_miner_solves"] == 1
           and deleted["per_miner_assignments"] == 1)
        ck("retention keeps recent scoring rows",
           retention_store.query("SELECT COUNT(*) AS n FROM eval_runs")[0]["n"] == 1
           and retention_store.query("SELECT COUNT(*) AS n FROM lane_challenge_solves")[0]["n"] == 1
           and retention_store.query("SELECT COUNT(*) AS n FROM per_miner_attempts")[0]["n"] == 1
           and retention_store.query("SELECT COUNT(*) AS n FROM per_miner_solves")[0]["n"] == 1
           and retention_store.query("SELECT COUNT(*) AS n FROM per_miner_assignments")[0]["n"] == 1)
    finally:
        for _key, _value in old_role_env.items():
            if _value is None:
                os.environ.pop(_key, None)
            else:
                os.environ[_key] = _value

    from bittensor_wallet import Keypair  # noqa
    miner = Keypair.create_from_uri("//E2EMiner")
    from datetime import datetime, timezone
    def now_iso():
        d = datetime.now(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    import blake3 as _blake3
    old_gate_env = {
        k: os.environ.get(k)
        for k in (
            "CATHEDRAL_TEE_GPU_ENABLED",
            "CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE",
            "CATHEDRAL_TEE_GPU_INTAKE_CODE",
            "CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST",
        )
    }
    try:
        os.environ["CATHEDRAL_TEE_GPU_ENABLED"] = "1"
        os.environ.pop("CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE", None)
        os.environ.pop("CATHEDRAL_TEE_GPU_INTAKE_CODE", None)
        os.environ.pop("CATHEDRAL_TEE_GPU_INTAKE_ALLOWLIST", None)

        def _offer_sig(body: dict, *, when: str):
            digest = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            claim = canonical_claim_bytes(
                bundle_hash=_blake3.blake3(b"").hexdigest(),
                card_id=_tee_gpu.TEE_CARD_ID,
                miner_hotkey=miner.ss58_address,
                submitted_at=when,
                challenge_id=body["node_id"],
                dimacs_solution_sha256=digest,
            )
            return base64.b64encode(miner.sign(claim)).decode()

        offer_body = {
            "node_id": "publisher-gated-h200-0",
            "gpu_short_ref": "h200",
            "gpu_count": 8,
            "hourly_cost": 2.75,
            "agent_api": "http://203.0.113.50:32000",
            "tee_kind": "intel_tdx",
            "tdx_claimed": True,
            "gpu_cc_claimed": True,
            "operator_use_authorized": True,
        }
        closed_at = now_iso()
        closed_offer = client.post(
            "/v1/tee-gpu/offers",
            headers={
                "X-Cathedral-Hotkey": miner.ss58_address,
                "X-Cathedral-Signature": _offer_sig(offer_body, when=closed_at),
                "X-Cathedral-Submitted-At": closed_at,
            },
            json=offer_body,
        )
        ck("TEE GPU live intake fails closed when gate is unconfigured",
           closed_offer.status_code == 503
           and closed_offer.json().get("detail") == "tee_gpu_intake_gate_not_configured")

        os.environ["CATHEDRAL_TEE_GPU_REQUIRE_INTAKE_CODE"] = "1"
        os.environ["CATHEDRAL_TEE_GPU_INTAKE_CODE"] = "publisher-gate-secret"
        offer_at = now_iso()
        blocked_offer = client.post(
            "/v1/tee-gpu/offers",
            headers={
                "X-Cathedral-Hotkey": miner.ss58_address,
                "X-Cathedral-Signature": _offer_sig(offer_body, when=offer_at),
                "X-Cathedral-Submitted-At": offer_at,
            },
            json=offer_body,
        )
        ck("TEE GPU live intake rejects missing invite code",
           blocked_offer.status_code == 403
           and blocked_offer.json().get("detail") == "invalid_tee_gpu_intake_code")

        allowed_body = {**offer_body, "intake_code": "publisher-gate-secret"}
        allowed_at = now_iso()
        allowed_offer = client.post(
            "/v1/tee-gpu/offers",
            headers={
                "X-Cathedral-Hotkey": miner.ss58_address,
                "X-Cathedral-Signature": _offer_sig(allowed_body, when=allowed_at),
                "X-Cathedral-Submitted-At": allowed_at,
            },
            json=allowed_body,
        )
        allowed_json = allowed_offer.json() if allowed_offer.status_code == 200 else {}
        ck("TEE GPU live intake accepts signed invite-code offer",
           allowed_offer.status_code == 200
           and allowed_json.get("capacity", {}).get("node_id") == "publisher-gated-h200-0")
        request_body = {"node_id": "publisher-gated-evidence-h200-0", "ttl_secs": 60}
        request_at = now_iso()
        blocked_request = client.post(
            "/v1/tee-gpu/evidence-request",
            headers={
                "X-Cathedral-Hotkey": miner.ss58_address,
                "X-Cathedral-Signature": _offer_sig(request_body, when=request_at),
                "X-Cathedral-Submitted-At": request_at,
            },
            json=request_body,
        )
        ck("TEE GPU evidence request rejects missing invite code",
           blocked_request.status_code == 403
           and blocked_request.json().get("detail") == "invalid_tee_gpu_intake_code")
        allowed_request_body = {**request_body, "invite_code": "publisher-gate-secret"}
        allowed_request_at = now_iso()
        allowed_request = client.post(
            "/v1/tee-gpu/evidence-request",
            headers={
                "X-Cathedral-Hotkey": miner.ss58_address,
                "X-Cathedral-Signature": _offer_sig(allowed_request_body, when=allowed_request_at),
                "X-Cathedral-Submitted-At": allowed_request_at,
            },
            json=allowed_request_body,
        )
        ck("TEE GPU evidence request accepts signed invite code",
           allowed_request.status_code == 200
           and allowed_request.json().get("status") == "issued")
        metrics = client.get("/v1/admin/tee-gpu/metrics", headers={"Authorization": "Bearer no-token"})
        ck("TEE GPU metrics remain admin-gated", metrics.status_code in (401, 503))
    finally:
        for key, value in old_gate_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # AUDIT SCANNER BRIDGE: default-off in production, but release verification
    # proves the signed replay/submit/ledger path whenever deliberately enabled.
    old_audit_env = {
        k: os.environ.get(k)
        for k in (
            "CATHEDRAL_AUDIT_SCANNER_ENABLED",
            "CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH",
        )
    }
    old_audit_pm_env = {
        k: os.environ.get(k)
        for k in (
            "CATHEDRAL_PERMINER_ENABLED",
            "CATHEDRAL_PERMINER_SEED_SECRET",
        )
    }
    try:
        import tempfile as _tempfile
        from game.arena import audit_scanner_smoke as _audit_smoke

        with _tempfile.TemporaryDirectory() as _audit_tmp:
            os.environ["CATHEDRAL_AUDIT_SCANNER_ENABLED"] = "1"
            os.environ["CATHEDRAL_AUDIT_SCANNER_LEDGER_PATH"] = str(
                Path(_audit_tmp) / "audit_scanner_verify.jsonl"
            )
            audit_app = build_app(
                database_path=":memory:",
                signing_key_hex=key_hex,
                submit_min_interval_secs=0,
            )
            audit_miner = Keypair.create_from_uri("//AuditScannerVerify")
            with TestClient(audit_app) as audit_client:
                audit_result = _audit_smoke.run_smoke(
                    _audit_smoke.TestClientTransport(audit_client),
                    audit_miner,
                )
        ck("audit scanner bridge smoke runs signed replay->submit",
           audit_result["replay"]["accepted"] is True
           and audit_result["replay"]["ledger_written"] is False
           and audit_result["submit"]["accepted"] is True)
        ck("audit scanner bridge writes hash-only submissions evidence",
           audit_result["submissions"]["count"] == 1
           and audit_result["submissions"]["total"] == 1
           and audit_result["submissions"]["contains_witnesses"] is False
           and audit_result["submissions"]["contains_reports"] is False
           and audit_result["submissions"]["contains_trace_bodies"] is False
           and "artifact" not in audit_result["submissions"]["entries"][0])
        ck("audit scanner contract endpoint documents replay-only scoring",
           audit_result["contract"]["card_id"] == audit_result["status"]["card_id"]
           and audit_result["contract"]["payment_weights"] is False
           and audit_result["contract"]["scoring"]["reports_score"] is False
           and audit_result["contract"]["scoring"]["claims_score"] is False
           and "witness" in audit_result["contract"]["submission_schema"]["required_fields"])
        ck("audit scanner bridge exposes replay-gated taxonomy",
           audit_result["families"]["reward_gate"] == "deterministic_replay"
           and audit_result["families"]["category_scoring"] == "claim_category_is_metadata_only"
           and audit_result["families"]["claim_categories"]
           and audit_result["families"]["payment_weights"] is False)
        ck("audit scanner example endpoint is redacted by default",
           audit_result["example"]["solution_exported"] is False
           and audit_result["example"]["submission"]["witness"] is None
           and audit_result["example"]["redaction"]["witness_exported"] is False)
        ck("audit scanner bridge exposes hash-only replay trace labels",
           audit_result["traces"]["count"] == 1
           and audit_result["traces"]["accepted"] == 1
           and audit_result["traces"]["contains_witnesses"] is False
           and audit_result["traces"]["contains_reports"] is False
           and audit_result["traces"]["contains_trace_bodies"] is False
           and "artifact" not in audit_result["traces"]["traces"][0]
           and audit_result["traces"]["traces"][0]["label"] == "accepted")
        ck("audit scanner bridge remains outside payment weights",
           audit_result["status"]["payment_weights"] is False
           and audit_result["submit"]["payment_weights"] is False)
        ck("audit scanner benchmark exposes replay kill rate",
           audit_result["benchmark"]["metric"] == "replay_kill_rate"
           and audit_result["state"]["accepted"] == 1)
        ck("audit scanner import leaves per-miner scoring env unchanged",
           {k: os.environ.get(k) for k in old_audit_pm_env} == old_audit_pm_env)
    except Exception as exc:
        print(f"    audit scanner bridge smoke failed: {exc!r}")
        ck("audit scanner bridge smoke runs signed replay->submit", False)
        ck("audit scanner bridge writes hash-only submissions evidence", False)
        ck("audit scanner contract endpoint documents replay-only scoring", False)
        ck("audit scanner bridge exposes replay-gated taxonomy", False)
        ck("audit scanner example endpoint is redacted by default", False)
        ck("audit scanner bridge exposes hash-only replay trace labels", False)
        ck("audit scanner bridge remains outside payment weights", False)
        ck("audit scanner benchmark exposes replay kill rate", False)
        ck("audit scanner import leaves per-miner scoring env unchanged", False)
    finally:
        for key, value in old_audit_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _weights._reset_vector_cache()

    # active-cnf: hotkey-signed token fetch
    sa = now_iso()
    cnf_claim = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sa,
        challenge_id="", dimacs_solution_sha256="")
    cnf_sig = base64.b64encode(miner.sign(cnf_claim)).decode()
    r = client.get("/v1/synthetic-boolean/active-cnf",
                   headers={"X-Cathedral-Hotkey": miner.ss58_address,
                            "X-Cathedral-Signature": cnf_sig,
                            "X-Cathedral-Submitted-At": sa})
    ck("active-cnf returns a tokenized cnf_url", r.status_code == 200 and "?t=" in r.json()["cnf_url"])
    cnf_url = r.json()["cnf_url"]
    old_cnf_public_base = os.environ.get("CATHEDRAL_CNF_PUBLIC_BASE_URL")
    try:
        os.environ["CATHEDRAL_CNF_PUBLIC_BASE_URL"] = "https://submit.cathedral.computer"
        abs_r = client.get(
            "/v1/synthetic-boolean/active-cnf",
            headers={"X-Cathedral-Hotkey": miner.ss58_address,
                     "X-Cathedral-Signature": cnf_sig,
                     "X-Cathedral-Submitted-At": sa},
        )
        ck("active-cnf can advertise submit-host cnf_url",
           abs_r.status_code == 200
           and abs_r.json()["cnf_url"].startswith("https://submit.cathedral.computer/v1/challenges/"))
    finally:
        if old_cnf_public_base is None:
            os.environ.pop("CATHEDRAL_CNF_PUBLIC_BASE_URL", None)
        else:
            os.environ["CATHEDRAL_CNF_PUBLIC_BASE_URL"] = old_cnf_public_base
    # token gates: bad token -> opaque 404
    bad = client.get(cnf_url[:cnf_url.index("?t=")] + "?t=deadbeef.bad")
    ck("bad CNF token -> opaque 404", bad.status_code == 404)
    cnf_text = client.get(cnf_url).text
    ck("valid token fetches the CNF", cnf_text.startswith("p cnf"))

    # Split-role production can mint active-cnf tokens on one replica and serve
    # the CNF on another. A stable secret must make that round trip valid.
    import tempfile as _cnf_tempfile
    old_cnf_token_secret = os.environ.get("CATHEDRAL_CNF_TOKEN_SECRET")
    cnf_db_path = ""
    try:
        os.environ["CATHEDRAL_CNF_TOKEN_SECRET"] = "publisher-verify-shared-cnf-token-secret"
        with _cnf_tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as _cnf_db:
            cnf_db_path = _cnf_db.name
        token_app_a = build_app(
            database_path=cnf_db_path,
            signing_key_hex=key_hex,
            submit_min_interval_secs=0,
        )
        token_store = token_app_a.state.store
        seed_challenge(token_store, challenge_id="sat-token-cross-app", tier=1, cnf_text=cnf_e2e)
        token_app_b = build_app(
            database_path=cnf_db_path,
            signing_key_hex=key_hex,
            submit_min_interval_secs=0,
        )
        with TestClient(token_app_a) as token_client_a, TestClient(token_app_b) as token_client_b:
            token_resp = token_client_a.get(
                "/v1/synthetic-boolean/active-cnf?challenge_id=sat-token-cross-app",
                headers={"X-Cathedral-Hotkey": miner.ss58_address,
                         "X-Cathedral-Signature": cnf_sig,
                         "X-Cathedral-Submitted-At": sa},
            )
            token_url = token_resp.json()["cnf_url"] if token_resp.status_code == 200 else ""
            cross_resp = token_client_b.get(token_url) if token_url else token_resp
            ck("active-cnf token validates across split app instances",
               token_resp.status_code == 200
               and cross_resp.status_code == 200
               and cross_resp.text.startswith("p cnf"))
    finally:
        if old_cnf_token_secret is None:
            os.environ.pop("CATHEDRAL_CNF_TOKEN_SECRET", None)
        else:
            os.environ["CATHEDRAL_CNF_TOKEN_SECRET"] = old_cnf_token_secret
        try:
            if cnf_db_path:
                os.unlink(cnf_db_path)
        except Exception:
            pass

    # solve with DPLL + submit (6-field signed)
    sol = solve_cnf(cnf_text)
    blob = "s SATISFIABLE\nv " + " ".join(str(x) for x in sol) + " 0\n"
    blank_reuse = client.post("/v1/agents/submit",
                              headers={"X-Cathedral-Hotkey": miner.ss58_address,
                                       "X-Cathedral-Signature": cnf_sig},
                              data={"card_id": "synthetic_boolean_v1", "submitted_at": sa,
                                    "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("submit rejects reused active-cnf blank signature",
       blank_reuse.status_code == 401)
    sa2 = now_iso()
    sol_sha = __import__("hashlib").sha256(blob.encode()).hexdigest()
    claim = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sa2,
        challenge_id="sat-e2e-1", dimacs_solution_sha256=sol_sha)
    sig = base64.b64encode(miner.sign(claim)).decode()
    resp = client.post("/v1/agents/submit",
                       headers={"X-Cathedral-Hotkey": miner.ss58_address,
                                "X-Cathedral-Signature": sig},
                       data={"card_id": "synthetic_boolean_v1", "submitted_at": sa2,
                             "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("submit accepted as ranked", resp.status_code == 200 and resp.json()["status"] == "ranked")

    ck("first solve gets open-window rank 1", resp.json().get("solve_rank") == 1)
    ck("submit row emits base weighted_score 1.0", resp.json().get("weighted_score") == 1.0)
    explain = client.get(
        "/v1/leaderboard/explain",
        params={"miner_hotkey": miner.ss58_address},
    )
    explain_json = explain.json() if explain.status_code == 200 else {}
    ck(
        "leaderboard explain shows the miner's proportional score inputs",
        explain.status_code == 200
        and explain_json.get("score_source") == "proportional"
        and explain_json.get("normalized_weight") == 1.0
        and explain_json.get("distinct_challenges") == 1,
    )

    # replay the exact same signature -> rejected
    replay = client.post("/v1/agents/submit",
                        headers={"X-Cathedral-Hotkey": miner.ss58_address,
                                 "X-Cathedral-Signature": sig},
                        data={"card_id": "synthetic_boolean_v1", "submitted_at": sa2,
                              "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("replayed signature rejected", replay.status_code == 409)

    # OPEN WINDOW (live since 2026-06-04): a SECOND distinct miner solves the
    # same challenge and is ranked 2 — the challenge does NOT lock.
    miner2 = Keypair.create_from_uri("//E2EMiner2")
    sa3 = now_iso()
    claim2 = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner2.ss58_address, submitted_at=sa3,
        challenge_id="sat-e2e-1", dimacs_solution_sha256=sol_sha)
    sig2 = base64.b64encode(miner2.sign(claim2)).decode()
    resp2 = client.post("/v1/agents/submit",
                        headers={"X-Cathedral-Hotkey": miner2.ss58_address,
                                 "X-Cathedral-Signature": sig2},
                        data={"card_id": "synthetic_boolean_v1", "submitted_at": sa3,
                              "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("open window: second distinct miner also ranked (no lock)",
       resp2.status_code == 200 and resp2.json()["status"] == "ranked")
    ck("open window: second miner gets solve_rank 2", resp2.json().get("solve_rank") == 2)

    # the same miner re-solving the same challenge (fresh signature) -> 409
    sa4 = now_iso()
    claim3 = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner2.ss58_address, submitted_at=sa4,
        challenge_id="sat-e2e-1", dimacs_solution_sha256=sol_sha)
    sig3 = base64.b64encode(miner2.sign(claim3)).decode()
    resp3 = client.post("/v1/agents/submit",
                        headers={"X-Cathedral-Hotkey": miner2.ss58_address,
                                 "X-Cathedral-Signature": sig3},
                        data={"card_id": "synthetic_boolean_v1", "submitted_at": sa4,
                              "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("open window: same miner re-solve rejected (already_solved)",
       resp3.status_code == 409)

    # ACTIVE-GUARD: once a challenge is retired, a fresh solve on it must NOT pay
    # — the claim is guarded by an in-transaction active check (defends the race
    # where the refill loop retires a challenge between the pre-tx read and write).
    def _retire(conn):
        conn.execute("UPDATE lane_challenges SET status='retired' WHERE challenge_id=?",
                     ("sat-e2e-1",))
    store.write(_retire)
    miner3 = Keypair.create_from_uri("//E2EMiner3")
    sa5 = now_iso()
    claim4 = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner3.ss58_address, submitted_at=sa5,
        challenge_id="sat-e2e-1", dimacs_solution_sha256=sol_sha)
    sig4 = base64.b64encode(miner3.sign(claim4)).decode()
    resp4 = client.post("/v1/agents/submit",
                        headers={"X-Cathedral-Hotkey": miner3.ss58_address,
                                 "X-Cathedral-Signature": sig4},
                        data={"card_id": "synthetic_boolean_v1", "submitted_at": sa5,
                              "challenge_id": "sat-e2e-1", "dimacs_solution": blob})
    ck("open window: solve on a RETIRED challenge is rejected (no pay on dead challenge)",
       resp4.status_code == 409)
    # restore active so the downstream validator-pull check still sees the feed.
    def _reactivate(conn):
        conn.execute("UPDATE lane_challenges SET status='active' WHERE challenge_id=?",
                     ("sat-e2e-1",))
    store.write(_reactivate)

    # PER-MINER ASSIGNMENTS: standard DIMACS solver output is accepted, solve
    # ledger write is atomic inside submit txn, and repeat solve is rejected.
    from scaffold.publisher import per_miner as _pm
    old_pm_env = {
        k: os.environ.get(k)
        for k in (
            "CATHEDRAL_PERMINER_ENABLED",
            "CATHEDRAL_PERMINER_SHADOW",
            "CATHEDRAL_PERMINER_ALLOTMENT_T1",
            "CATHEDRAL_PERMINER_ALLOTMENT_T2",
            "CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE",
            "CATHEDRAL_PERMINER_SCORING_MODE",
            "CATHEDRAL_PERMINER_REQUIRE_COLDKEY",
            "CATHEDRAL_PERMINER_PUBLIC_BASELINE",
            "CATHEDRAL_PERMINER_SEED_SECRET",
            "CATHEDRAL_PUBLISHER_ADMIN_TOKEN",
            "CATHEDRAL_TEE_GPU_ADMIN_TOKEN",
        )
    }
    try:
        os.environ["CATHEDRAL_PERMINER_ENABLED"] = "1"
        os.environ.pop("CATHEDRAL_PERMINER_SHADOW", None)
        os.environ["CATHEDRAL_PERMINER_ALLOTMENT_T1"] = "1"
        os.environ["CATHEDRAL_PERMINER_ALLOTMENT_T2"] = "1"
        os.environ["CATHEDRAL_WEIGHTS_COLDKEY_COLLAPSE"] = "1"
        os.environ.pop("CATHEDRAL_PERMINER_SEED_SECRET", None)
        pm_miner = Keypair.create_from_uri("//PerMinerE2E")
        pm_miner2 = Keypair.create_from_uri("//PerMinerE2EStacked")
        pm_closed_at = now_iso()
        pm_closed_claim = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=pm_closed_at,
            challenge_id="", dimacs_solution_sha256="")
        pm_closed_sig = base64.b64encode(pm_miner.sign(pm_closed_claim)).decode()
        pm_closed = client.get(
            "/v1/synthetic-boolean/per-miner/challenges",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_closed_sig,
                     "X-Cathedral-Submitted-At": pm_closed_at},
        )
        ck("per-miner assignments fail closed without stable seed secret",
           pm_closed.status_code == 503
           and pm_closed.json().get("detail") == "per_miner_seed_secret_missing")
        os.environ["CATHEDRAL_PERMINER_SEED_SECRET"] = "publisher-verify-stable-seed"
        def _map_pm_coldkey(conn):
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                (pm_miner.ss58_address, "coldkey-shared", now_iso()),
            )
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                (pm_miner2.ss58_address, "coldkey-shared", now_iso()),
            )
        store.write(_map_pm_coldkey)
        pm_epoch = _pm.current_epoch()
        seed_suffix = f"{_pm.instance_seed('coldkey-shared', pm_epoch, 1, 0):016x}"
        pm_list_at = now_iso()
        pm_list_claim = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=pm_list_at,
            challenge_id="", dimacs_solution_sha256="")
        pm_list_sig = base64.b64encode(pm_miner.sign(pm_list_claim)).decode()
        pm_list = client.get(
            "/v1/synthetic-boolean/per-miner/challenges",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_list_sig,
                     "X-Cathedral-Submitted-At": pm_list_at},
        )
        pm_list2_at = now_iso()
        pm_list2_claim = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner2.ss58_address, submitted_at=pm_list2_at,
            challenge_id="", dimacs_solution_sha256="")
        pm_list2_sig = base64.b64encode(pm_miner2.sign(pm_list2_claim)).decode()
        pm_list2 = client.get(
            "/v1/synthetic-boolean/per-miner/challenges",
            headers={"X-Cathedral-Hotkey": pm_miner2.ss58_address,
                     "X-Cathedral-Signature": pm_list2_sig,
                     "X-Cathedral-Submitted-At": pm_list2_at},
        )
        ck("per-miner coldkey collapse shares one assignment stream across stacked hotkeys",
           pm_list.status_code == 200
           and pm_list2.status_code == 200
           and [i["challenge_id"] for i in pm_list.json()["items"]]
           == [i["challenge_id"] for i in pm_list2.json()["items"]])
        pm_cid = pm_list.json()["items"][0]["challenge_id"]
        _pm_cid, _pm_cnf, pm_assignment = _pm.generate_instance("coldkey-shared", pm_epoch, 1, 0)
        ck("per-miner assignment endpoint uses coldkey-derived challenge id",
           pm_cid == _pm_cid)
        ck("per-miner public challenge id does not leak planted seed prefix",
           seed_suffix not in pm_cid)
        pm_assignments_after_list = store.query(
            "SELECT COUNT(*) AS n FROM per_miner_assignments WHERE miner_hotkey=?",
            ("coldkey-shared",),
        )
        ck("per-miner listing does not persist full assignment pages by default",
           pm_list.json().get("assignment_persistence") == "cnf_fetch"
           and pm_list.json().get("cnf_params") == ["challenge_id", "tier", "seq"]
           and pm_assignments_after_list[0]["n"] == 0)
        pm_cnf_resp = client.get(
            "/v1/synthetic-boolean/per-miner/cnf",
            params={"challenge_id": pm_cid, "tier": 1, "seq": 0},
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_list_sig,
                     "X-Cathedral-Submitted-At": pm_list_at},
        )
        pm_assignments_after_cnf = store.query(
            "SELECT COUNT(*) AS n FROM per_miner_assignments WHERE miner_hotkey=?",
            ("coldkey-shared",),
        )
        ck("per-miner cnf fetch persists only requested assignment",
           pm_cnf_resp.status_code == 200
           and pm_cnf_resp.headers.get("X-Perminer-Seq") == "0"
           and pm_assignments_after_cnf[0]["n"] == 1)
        pm_legacy_cid = pm_list.json()["items"][1]["challenge_id"]
        pm_legacy_cnf = client.get(
            "/v1/synthetic-boolean/per-miner/cnf",
            params={"challenge_id": pm_legacy_cid},
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_list_sig,
                     "X-Cathedral-Submitted-At": pm_list_at},
        )
        pm_assignments_after_legacy = store.query(
            "SELECT COUNT(*) AS n FROM per_miner_assignments WHERE miner_hotkey=?",
            ("coldkey-shared",),
        )
        ck("per-miner legacy challenge-id cnf fetch still works",
           pm_legacy_cnf.status_code == 200
           and pm_legacy_cnf.headers.get("X-Perminer-Tier") == "2"
           and pm_legacy_cnf.headers.get("X-Perminer-Seq") == "0"
           and pm_assignments_after_legacy[0]["n"] == 2)
        pm_blob = "s SATISFIABLE\nv " + " ".join(str(x) for x in pm_assignment) + " 0\n"
        pm_sha = hashlib.sha256(pm_blob.encode()).hexdigest()
        pm_at = now_iso()
        pm_claim = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=pm_at,
            challenge_id=pm_cid, dimacs_solution_sha256=pm_sha)
        pm_sig = base64.b64encode(pm_miner.sign(pm_claim)).decode()
        pm_resp = client.post(
            "/v1/agents/submit",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_sig},
            data={"card_id": "synthetic_boolean_v1", "submitted_at": pm_at,
                  "challenge_id": pm_cid, "dimacs_solution": pm_blob},
        )
        ck("per-miner submit accepts standard DIMACS solver output",
           pm_resp.status_code == 200
           and pm_resp.json().get("status") == "ranked"
           and pm_resp.json().get("solve_rank") == 1)
        pm_rows = store.query(
            "SELECT COUNT(*) AS n FROM per_miner_solves WHERE miner_hotkey=? AND challenge_id=?",
            (pm_miner.ss58_address, pm_cid),
        )
        ck("per-miner submit records exactly one solve row atomically",
           pm_rows[0]["n"] == 1)
        pm_bad_cid = pm_list.json()["items"][1]["challenge_id"]
        pm_bad_blob = "v 1 -2 3 0\n"
        pm_bad_sha = hashlib.sha256(pm_bad_blob.encode()).hexdigest()
        pm_bad_at = now_iso()
        pm_bad_claim = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=pm_bad_at,
            challenge_id=pm_bad_cid, dimacs_solution_sha256=pm_bad_sha)
        pm_bad_sig = base64.b64encode(pm_miner.sign(pm_bad_claim)).decode()
        pm_bad = client.post(
            "/v1/agents/submit",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_bad_sig},
            data={"card_id": "synthetic_boolean_v1", "submitted_at": pm_bad_at,
                  "challenge_id": pm_bad_cid, "dimacs_solution": pm_bad_blob},
        )
        ck("per-miner bad DIMACS submit reports reason header",
           pm_bad.status_code == 400
           and pm_bad.headers.get("X-Cathedral-Rejection-Reason") == "solution_missing_status")
        pm_status = client.get(
            "/v1/synthetic-boolean/per-miner/status",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_list_sig,
                     "X-Cathedral-Submitted-At": pm_list_at},
        )
        pm_status_json = pm_status.json()
        pm_current = pm_status_json.get("current_epoch_totals", {})
        ck("per-miner status surfaces accepted and rejected attempts",
           pm_status.status_code == 200
           and pm_status_json.get("assignment_identity") == "coldkey-shared"
           and pm_current.get("unique_verified_solves") == 1
           and pm_current.get("rejected_attempts") == 1
           and pm_current.get("rejection_reasons", [{}])[0].get("reason") == "solution_missing_status")
        pm_summary = client.get("/v1/synthetic-boolean/per-miner/summary")
        pm_summary_json = pm_summary.json()
        ck("per-miner summary surfaces dashboard aggregate",
           pm_summary.status_code == 200
           and pm_summary_json.get("assignment_accounting") == "cnf_fetch"
           and pm_summary_json.get("current_epoch_assignment_miners", 0) == 0
           and pm_summary_json.get("current_epoch_assigned_challenges", 0) == 0
           and pm_summary_json.get("active_miners_24h", 0) >= 1
           and "submit_metrics" not in pm_summary_json
           and "rejection_reasons_24h" not in pm_summary_json
           and all("assignment_identity" not in item for item in pm_summary_json.get("miners", []))
           and all("rejection_reasons" not in item for item in pm_summary_json.get("miners", [])))
        os.environ["CATHEDRAL_PUBLISHER_ADMIN_TOKEN"] = "publisher-admin-secret"
        pm_submit_metrics = client.get(
            "/v1/admin/synthetic-boolean/submit-metrics",
            headers={"Authorization": "Bearer publisher-admin-secret"},
        )
        pm_submit_metrics_bad = client.get(
            "/v1/admin/synthetic-boolean/submit-metrics",
            headers={"Authorization": "Bearer wrong-token"},
        )
        os.environ.pop("CATHEDRAL_PUBLISHER_ADMIN_TOKEN", None)
        pm_submit_metrics_missing = client.get(
            "/v1/admin/synthetic-boolean/submit-metrics",
            headers={"Authorization": "Bearer publisher-admin-secret"},
        )
        os.environ["CATHEDRAL_PUBLISHER_ADMIN_TOKEN"] = "publisher-admin-secret"
        ck("submit metrics are admin-gated and include PM rejection reasons",
           pm_submit_metrics.status_code == 200
           and pm_submit_metrics.json().get("by_reason", {}).get("solution_missing_status") == 1
           and pm_submit_metrics_bad.status_code == 401
           and pm_submit_metrics_missing.status_code == 503)
        pm_explain = client.get(
            "/v1/leaderboard/explain",
            params={"miner_hotkey": pm_miner.ss58_address},
        )
        pm_contribution = pm_explain.json().get("perminer", {}).get("contribution", {})
        ck("leaderboard explain includes per-miner contribution",
           pm_explain.status_code == 200
           and "current_signed_weight" in pm_explain.json()
           and "current_signed_weight_rank" in pm_explain.json()
           and pm_explain.json().get("visibility_schema") == "cathedral_miner_truth_v1"
           and pm_explain.json().get("visibility", {}).get("perminer_contribution", {}).get("status") in {"available", "ineligible"}
           and pm_explain.json().get("visibility", {}).get("recent_activity", {}).get("rank_kind") == "activity_only_not_payment"
           and pm_contribution.get("current_epoch_totals", {}).get("unique_verified_solves") == 1
           and "rejection_reasons" not in pm_contribution.get("current_epoch_totals", {})
           and "assignment_identity" not in pm_contribution
           and "assignment_supply" not in pm_contribution)
        os.environ["CATHEDRAL_PERMINER_SCORING_MODE"] = "pm_primary"
        os.environ["CATHEDRAL_PERMINER_REQUIRE_COLDKEY"] = "1"
        os.environ["CATHEDRAL_PERMINER_PUBLIC_BASELINE"] = "0.05"
        _weights._reset_vector_cache()
        pm_vector = _weights.build_signed_vector(store, signing_key_hex=key_hex)
        pm_weight_by_hotkey = {
            str(w["miner_hotkey"]): float(w["weight"])
            for w in pm_vector.get("weights", [])
        }
        pm_meta = pm_vector.get("policy_metadata", {}).get("perminer", {})
        ck("pm-primary vector pays the private-assignment solver",
           pm_meta.get("enabled") is True
           and pm_meta.get("primary_live") is True
           and pm_meta.get("identity_ready") is True
           and pm_meta.get("degraded_reason") is None
           and pm_vector.get("policy_metadata", {}).get("score_source") == "pm_primary"
           and pm_weight_by_hotkey.get(pm_miner.ss58_address) == 1.0
           and pm_weight_by_hotkey.get(miner.ss58_address, 0.0) < 0.1
           and pm_weight_by_hotkey.get(miner2.ss58_address, 0.0) < 0.1)
        degraded_app = build_app(
            database_path=":memory:",
            signing_key_hex=key_hex,
            submit_min_interval_secs=0,
        )
        degraded_store = degraded_app.state.store
        def _seed_unmapped_pm_solve(conn):
            conn.execute(
                "INSERT INTO per_miner_solves(challenge_id, miner_hotkey, epoch, tier, seq, "
                "difficulty_weight, verified, solved_at_iso) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("pm-unmapped", "5UnmappedPmMiner", _pm.current_epoch(), 1, 0, 1.0, 1, now_iso()),
            )
        degraded_store.write(_seed_unmapped_pm_solve)
        degraded_vector = _weights.build_signed_vector(degraded_store, signing_key_hex=key_hex)
        degraded_pm = degraded_vector.get("policy_metadata", {}).get("perminer", {})
        ck("pm-primary degraded state is explicit when coldkey map is missing",
           degraded_pm.get("enabled") is True
           and degraded_pm.get("primary_live") is False
           and degraded_pm.get("identity_ready") is False
           and degraded_pm.get("degraded_reason") == "coldkey_map_required_but_unavailable"
           and degraded_vector.get("weights") == [])
        rate_app = build_app(
            database_path=":memory:",
            signing_key_hex=key_hex,
            submit_min_interval_secs=60,
        )
        def _map_rate_miner(conn):
            conn.execute(
                "INSERT OR REPLACE INTO coldkey_map(hotkey, coldkey, updated_at_iso) "
                "VALUES (?, ?, ?)",
                (pm_miner.ss58_address, "coldkey-shared", now_iso()),
            )
        rate_app.state.store.write(_map_rate_miner)
        bad_pm_id = "pm-not-assigned"
        rate_blob = "s SATISFIABLE\nv 1 -2 3 0\n"
        rate_sha = hashlib.sha256(rate_blob.encode()).hexdigest()
        rate_at_1 = now_iso()
        rate_claim_1 = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=rate_at_1,
            challenge_id=bad_pm_id, dimacs_solution_sha256=rate_sha)
        rate_sig_1 = base64.b64encode(pm_miner.sign(rate_claim_1)).decode()
        rate_at_2 = now_iso()
        rate_claim_2 = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=rate_at_2,
            challenge_id=bad_pm_id, dimacs_solution_sha256=rate_sha)
        rate_sig_2 = base64.b64encode(pm_miner.sign(rate_claim_2)).decode()
        with TestClient(rate_app) as rate_client:
            rate_first = rate_client.post(
                "/v1/agents/submit",
                headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                         "X-Cathedral-Signature": rate_sig_1},
                data={"card_id": "synthetic_boolean_v1", "submitted_at": rate_at_1,
                      "challenge_id": bad_pm_id, "dimacs_solution": rate_blob},
            )
            rate_second = rate_client.post(
                "/v1/agents/submit",
                headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                         "X-Cathedral-Signature": rate_sig_2},
                data={"card_id": "synthetic_boolean_v1", "submitted_at": rate_at_2,
                      "challenge_id": bad_pm_id, "dimacs_solution": rate_blob},
            )
        ck("per-miner invalid assignment attempts are rate-limited after first signed try",
           rate_first.status_code == 400
           and rate_second.status_code == 429
           and rate_second.headers.get("X-Cathedral-Rejection-Reason") == "rate_limited")
        pm_at2 = now_iso()
        pm_claim2 = canonical_claim_bytes(
            bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
            miner_hotkey=pm_miner.ss58_address, submitted_at=pm_at2,
            challenge_id=pm_cid, dimacs_solution_sha256=pm_sha)
        pm_sig2 = base64.b64encode(pm_miner.sign(pm_claim2)).decode()
        pm_dupe = client.post(
            "/v1/agents/submit",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_sig2},
            data={"card_id": "synthetic_boolean_v1", "submitted_at": pm_at2,
                  "challenge_id": pm_cid, "dimacs_solution": pm_blob},
        )
        ck("per-miner duplicate solve is rejected",
           pm_dupe.status_code == 409
           and pm_dupe.headers.get("X-Cathedral-Rejection-Reason") == "already_solved")
        pm_status_after_dupe = client.get(
            "/v1/synthetic-boolean/per-miner/status",
            headers={"X-Cathedral-Hotkey": pm_miner.ss58_address,
                     "X-Cathedral-Signature": pm_list_sig,
                     "X-Cathedral-Submitted-At": pm_list_at},
        )
        reasons_after_dupe = {
            r.get("reason"): r.get("attempts")
            for r in pm_status_after_dupe.json().get("current_epoch_totals", {}).get("rejection_reasons", [])
        }
        ck("per-miner status includes duplicate-solve rejection reasons",
           pm_status_after_dupe.status_code == 200
           and reasons_after_dupe.get("solution_missing_status") == 1
           and reasons_after_dupe.get("already_solved") == 1)
    finally:
        for key, value in old_pm_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _weights._reset_vector_cache()

    # SIGNED FINAL-SCORES VECTOR (the v4 scoring interface). One number per
    # miner + burn, Ed25519-signed — validators verify and apply, no local
    # averaging. Both e2e miners solved sat-e2e-1 above, so both appear.
    vec_resp = client.get("/v1/validator/weights/next")
    ck("weights/next serves the signed vector", vec_resp.status_code == 200)
    vec = vec_resp.json()
    vec_hotkeys = {w["miner_hotkey"] for w in vec["weights"]}
    ck("vector contains exactly the miners who solved in-window",
       vec_hotkeys == {miner.ss58_address, miner2.ss58_address})
    ck("default proportional: equal work earns equal weight",
       vec["policy_metadata"]["requested_mode"] == "proportional"
       and vec["policy_metadata"]["effective_mode"] == "proportional"
       and all(w["weight"] == 1.0 for w in vec["weights"]))
    top_weights = client.get("/v1/leaderboard/top").json()
    ck("leaderboard/top defaults to current payment weights",
       top_weights.get("view") == "weights"
       and top_weights.get("rank_kind") == "current_payment_weight"
       and top_weights.get("earning_weight_source") == "v1/validator/weights/next"
       and top_weights.get("miners")
       and "current_weight_rank" in top_weights["miners"][0])
    top_visibility = top_weights["miners"][0].get("visibility", {}) if top_weights.get("miners") else {}
    ck("leaderboard/top surfaces miner truth fields with source labels",
       top_weights.get("visibility_schema") == "cathedral_miner_truth_v1"
       and bool(top_weights.get("miners"))
       and {"uid", "registered", "payable", "current_signed_weight",
            "chain_incentive", "chain_emission", "perminer_weighted_units",
            "recent_activity_last_seen"} <= set(top_weights["miners"][0])
       and top_visibility.get("recent_activity", {}).get("rank_kind") == "activity_only_not_payment"
       and top_visibility.get("sources", {}).get("payment", {}).get("path") == "v1/validator/weights/next"
       and top_visibility.get("sources", {}).get("chain", {}).get("status") == "unavailable")
    top_receipts = client.get("/v1/leaderboard/top", params={"view": "receipts"}).json()
    ck("leaderboard/top view=receipts is explicitly not the earning order",
       top_receipts.get("view") == "receipts"
       and top_receipts.get("rank_kind") == "receipt_total_score_24h"
       and (
           not top_receipts.get("miners")
           or "current_weight" in top_receipts.get("miners", [{}])[0]
       ))
    ck("burn rides the same signed payload (85.0 -> uid 204)",
       vec["burn_snapshot"] == {"burn_uid": 204, "forced_burn_percentage": 85.0})
    try:
        _weights.verify_signature(vec, public_key_hex=pub_hex,
                                  expected_key_id="cathedral-weight-policy")
        _weights.invariant_check(vec, network="finney", netuid=39, now_iso=now_iso())
        vec_ok = True
    except _weights.VectorError:
        vec_ok = False
    ck("vector signature + invariants verify (scaffold checker)", vec_ok)
    tampered = dict(vec)
    tampered["weights"] = [{"miner_hotkey": "5Attacker", "weight": 1.0}]
    try:
        _weights.verify_signature(tampered, public_key_hex=pub_hex,
                                  expected_key_id="cathedral-weight-policy")
        tamper_caught = False
    except _weights.VectorError:
        tamper_caught = True
    ck("tampered weights rejected", tamper_caught)

    # DROP-IN PROOF: the DEPLOYED validator's own verifier accepts this vector.
    import importlib.util as _ilu
    _sig_path = next((p for p in (
        Path.home() / "code" / "cathedral" / "src" / "cathedral" / "policy" / "signing.py",
        Path.home() / "cathedral" / "src" / "cathedral" / "policy" / "signing.py",
    ) if p.exists()), None)
    if _sig_path is None:
        print("    (monolith checkout not found — drop-in proof SKIPPED)")
    else:
        _spec = _ilu.spec_from_file_location("monolith_signing", _sig_path)
        _mono = _ilu.module_from_spec(_spec)
        sys.modules["monolith_signing"] = _mono  # pydantic forward-ref resolution
        _spec.loader.exec_module(_mono)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey as _PK
        try:
            _model = _mono.SignedWeightVector.model_validate(vec)
            _mono.verify_vector(_model, public_key=_PK.from_public_bytes(bytes.fromhex(pub_hex)),
                                expected_key_id="cathedral-weight-policy")
            _model.invariant_check(network="finney", netuid=39, now_iso=now_iso())
            mono_ok = True
        except Exception as e:
            print(f"    monolith verifier rejected: {e}")
            mono_ok = False
        ck("DEPLOYED validator's verify_vector + invariant_check accept the v4 vector", mono_ok)

    # RECENCY GATE: scores compose only from solves inside the trailing window
    # (default 24h) — an idle miner drops out when the window passes. This
    # replaces the validator-side 7-day mean whose tail paid stopped miners.
    from datetime import timedelta as _td
    sc_now = _weights.compose_scores(store)
    ck("recency: miners who just solved are in the composition", len(sc_now) == 2)
    sc_later = _weights.compose_scores(
        store, now=datetime.now(timezone.utc) + _td(hours=25))
    ck("recency: 25h later with no new solves, every idle miner drops to zero",
       sc_later == {})

    # PROPORTIONAL MODE (env dial, no validator involvement): weight = distinct
    # solves relative to the busiest solver.
    os.environ[_weights.MODE_ENV] = "proportional"
    try:
        prop_store = build_app(database_path=":memory:", signing_key_hex=key_hex).state.store
        def _seed_prop(conn):
            cn = now_iso()
            for hk, k in (("5Busy", 4), ("5Slow", 1)):
                for i in range(k):
                    conn.execute(
                        "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, "
                        "miner_hotkey, solved_at_iso) VALUES (?, ?, ?)",
                        (f"prop-{i}", hk, cn))
        prop_store.write(_seed_prop)
        sc_prop = _weights.compose_scores(prop_store)
        ck("proportional: busiest solver = 1.0, quarter-as-busy = 0.25",
           sc_prop == {"5Busy": 1.0, "5Slow": 0.25})
        prop_store.close()
    finally:
        os.environ.pop(_weights.MODE_ENV, None)
    ck("proportional restored as default", _weights.mode() == "proportional")

    # BURN IS REMOTE: change the env, fresh vector carries the new signed burn.
    os.environ[_weights.BURN_PERCENTAGE_ENV] = "50.0"
    _weights._reset_vector_cache()
    try:
        vec50 = client.get("/v1/validator/weights/next").json()
        ck("burn change is one env flip, signed into the next vector",
           vec50["burn_snapshot"]["forced_burn_percentage"] == 50.0)
        ck("policy_version is monotonic (rollback fence)",
           int(vec50["policy_version"]) > int(vec["policy_version"]))
        # CONTINUITY: deployed validators' fences hold the LIVE emitter's
        # epoch-ms policy_versions (~1.78e12). A successor that restarts its
        # counter at 1 would be rejected as a rollback by every fence.
        ck("policy_version continues past the live emitter's epoch-ms fence",
           int(vec["policy_version"]) > 1_781_232_182_941)
    finally:
        os.environ.pop(_weights.BURN_PERCENTAGE_ENV, None)
        _weights._reset_vector_cache()

    # THIN VALIDATOR (scaffold/validator_thin.py): the v4 validator binary
    # accepts the vector end-to-end — verify, burn from the signed payload,
    # normalized uid vector; rollback fence rejects older policy versions.
    from scaffold import validator_thin as _vthin
    try:
        _vthin.accept_vector(vec, public_key_hex=pub_hex,
                             key_id="cathedral-weight-policy",
                             network="finney", netuid=39, fence_version=-1)
        _hk2uid = {w["miner_hotkey"]: i + 1 for i, w in enumerate(vec["weights"])}
        _uw = _vthin.vector_to_uid_weights(vec, _hk2uid)
        thin_ok = (abs(sum(_uw.values()) - 1.0) < 1e-9
                   and abs(_uw.get(204, 0.0) - 0.85) < 1e-9)
    except Exception as _e:
        print(f"    thin validator rejected: {_e}")
        thin_ok = False
    ck("thin validator: verify -> burn -> normalized uid vector (85% to uid 204)", thin_ok)
    try:
        _vthin.accept_vector(vec, public_key_hex=pub_hex,
                             key_id="cathedral-weight-policy",
                             network="finney", netuid=39,
                             fence_version=int(vec["policy_version"]) + 1)
        fence_ok = False
    except _weights.VectorError:
        fence_ok = True
    ck("thin validator: rollback fence rejects an older policy_version", fence_ok)
    # fence also rejects the SAME version (replay) — pv <= fence, not just <.
    try:
        _vthin.accept_vector(vec, public_key_hex=pub_hex, key_id="cathedral-weight-policy",
                             network="finney", netuid=39,
                             fence_version=int(vec["policy_version"]))
        replay_ok = False
    except _weights.VectorError:
        replay_ok = True
    ck("thin validator: fence rejects re-applying the SAME policy_version (replay)", replay_ok)

    # validator-style pull loop: tuple cursor, verify EVERY signature.
    pulled = []
    cur_ra, cur_id = "1970-01-01T00:00:00+00:00", ""
    for _ in range(12):
        params = {"limit": 1}
        params["since_ran_at"] = cur_ra
        params["since_id"] = cur_id
        page = client.get("/v1/leaderboard/recent", params=params).json()
        if not page["items"]:
            break
        pulled += page["items"]
        cur_ra, cur_id = page["next_since_ran_at"], page["next_since_id"]
    shared_pulled = [
        r for r in pulled
        if r.get("miner_hotkey") in {miner.ss58_address, miner2.ss58_address}
        and r.get("task_type") == "synthetic_boolean_v1"
    ]
    ck("validator pull retrieved v6 + v5compat rows for BOTH shared solves",
       len(shared_pulled) == 4)
    all_verify = all(wire.verify_row(r, pub_hex) for r in pulled)
    ck("every pulled row signature verifies (validator would score it)", all_verify)
    ck("cursor fields present on feed response",
       all(k in client.get("/v1/leaderboard/recent").json()
           for k in ("next_since", "next_since_ran_at", "next_since_id", "merkle_epoch_latest")))
    recent_default = client.get("/v1/leaderboard/recent").json()
    ck("recent exposes current weights without mutating signed receipt rows",
       recent_default.get("view") == "recent_signed_receipts"
       and recent_default.get("rank_kind") == "none"
       and recent_default.get("current_weights_status") == "available"
       and recent_default.get("current_weights")
       and all(wire.verify_row(r, pub_hex) for r in recent_default["items"]))

    # Lane S register + status
    sd = now_iso()
    sclaim = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sd,
        challenge_id="arena", dimacs_solution_sha256="ab" * 32)
    ssig = base64.b64encode(miner.sign(sclaim)).decode()
    rs = client.post("/v1/arena/solvers",
                     headers={"X-Cathedral-Hotkey": miner.ss58_address,
                              "X-Cathedral-Signature": ssig},
                     data={"source_url": "https://x/y.tgz",
                           "container_digest": "sha256:" + "00" * 32,
                           "source_sha256": "ab" * 32, "submitted_at": sd})
    ck("Lane S solver register accepted", rs.status_code == 200 and rs.json()["accepted"])
    st = client.get("/v1/arena/status").json()
    ck("Lane S status lists the pending challenger", st["count_pending"] == 1)

    # Lane I instance intake enforces quarantine + min batch score
    sd2 = now_iso()
    icnf_sha = __import__("hashlib").sha256(b"p cnf 1 1\n1 0\n").hexdigest()
    iclaim = canonical_claim_bytes(
        bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sd2,
        challenge_id="arena-instance", dimacs_solution_sha256=icnf_sha)
    isig = base64.b64encode(miner.sign(iclaim)).decode()
    ri = client.post("/v1/arena/instances",
                     headers={"X-Cathedral-Hotkey": miner.ss58_address,
                              "X-Cathedral-Signature": isig},
                     data={"cnf_text": "p cnf 1 1\n1 0\n", "round_no": "5",
                           "submitted_at": sd2})
    ib = ri.json()
    ck("Lane I instance quarantine = round + 3",
       ri.status_code == 200 and ib["quarantine_until_round"] == 8)
    ck("Lane I instance min_batch_score = 0.5", ib["min_batch_score"] == 0.5)

# --------------------------------------------------------------------------
# 5. LANE S — arena eval loop turns a record-fall into a signed feed row (TASK 1).
# --------------------------------------------------------------------------
print("LANE S — arena eval tick + record-fall -> signed row")
from scaffold.publisher import arena_eval  # noqa: E402
from scaffold.lanes.solver_arena import SolverArenaLane, SolverSpec, run_batch  # noqa: E402
from scaffold.lanes.arena_e2e import stub_adapter, run_arena_round  # noqa: E402

_arena_app = build_app(database_path=":memory:", signing_key_hex=key_hex)
_arena_store = _arena_app.state.store
_arena_lane: SolverArenaLane = _arena_app.state.arena_lane
_arena_lane.batch_size = 6

# seed the launch champion (an honest-but-unhurried solver), registered + marked
# evaluated so a copy of it dedups — exactly arena_e2e's seeding.
_ctx = arena_eval.GenerateCtx(seed=4242, tier=0, issued_at_iso="x")
_prob, _hidden = _arena_lane.mint_challenge(_ctx)
_insts = _arena_lane._batch_from_hidden(_hidden)
_timeouts = {i.task_id: i.timeout_ms for i in _insts}
_champ_spec = SolverSpec("https://x/champ", "sha256:" + "cc" * 32, "sc2025champion",
                         owner_hotkey="5LaunchChampOwner")
_champ_results = run_batch(stub_adapter("honest_slow"), _insts)
_arena_lane.seed_launch_champion(_champ_spec, _champ_results, _timeouts)

# register a fresh challenger solver in the DB (as the /v1/arena/solvers route does).
def _reg(conn):
    conn.execute(
        "INSERT OR IGNORE INTO arena_solvers(source_sha256, source_url, "
        "container_digest, owner_hotkey, registered_round, status, created_at_iso) "
        "VALUES ('fastchallenger', 'https://x/fast', ?, '5FastChallengerOwner', 0, "
        "'pending', ?)", ("sha256:" + "ff" * 32, "2026-06-10T00:00:00.000Z"))
_arena_store.write(_reg)

# adapter resolver: a fast honest solver for the challenger, None otherwise.
def _adapter_for(spec):
    if spec.commitment_id == "fastchallenger":
        return stub_adapter("honest_fast")
    return None

_summary = arena_eval.arena_eval_tick(
    _arena_store, _arena_lane, adapter_for=_adapter_for,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1",
    seed=4242, tier=0)
ck("arena tick evaluated the pending challenger", _summary["evaluated"] == 1)
ck("arena tick recorded a record-fall (fast beat the slow champion)",
   _summary["record_falls"] == 1)
ck("arena tick set the new champion to the challenger commitment",
   _arena_lane.champion.champion.commitment_id == "fastchallenger")

# the new champion is promoted in the DB + the old one retired.
_champ_row = _arena_store.query(
    "SELECT source_sha256, owner_hotkey FROM arena_solvers WHERE status='champion'")
ck("DB champion row is the new challenger",
   len(_champ_row) == 1 and _champ_row[0]["source_sha256"] == "fastchallenger")

# the emitted signed rows are in the feed and VERIFY under the loaded key.
_feed = _arena_store.recent_rows(None, None, 50)
_arena_rows = [r for r in _feed if str(r.get("id", "")).startswith("arena") or
               r.get("operator") == "5FastChallengerOwner" or
               r.get("miner_hotkey") == "5FastChallengerOwner"]
ck("arena record-fall emitted v6 + v5compat rows (2 rows)", len(_arena_rows) == 2)
ck("every emitted arena row verifies under the loaded key (validator would score it)",
   len(_arena_rows) == 2 and all(wire.verify_row(r, pub_hex) for r in _arena_rows))
ck("emitted arena row credits the new champion's owner hotkey",
   all(r["miner_hotkey"] == "5FastChallengerOwner" for r in _arena_rows))
ck("emitted arena row carries a positive weighted_score (a paid record-fall)",
   all(float(r["weighted_score"]) > 0.0 for r in _arena_rows))

# a SECOND tick with no new pending solver emits nothing (one-eval-per-commitment).
_summary2 = arena_eval.arena_eval_tick(
    _arena_store, _arena_lane, adapter_for=_adapter_for,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1",
    seed=4243, tier=0)
ck("arena re-tick with no new pending solver emits no rows",
   _summary2["rows_emitted"] == 0 and _summary2["evaluated"] == 0)

# ADVERSARIAL BATTERY (reuse arena_e2e): liar / forged-DRAT / copied-champion /
# timeout-fraud all score 0 — so none can ever be the source of a paid row.
_adv = run_arena_round(include_real=False)
_adv_by_label = {label: (score, reason) for label, _o, score, reason, _d in _adv.rows}
for _bad in ("liar_solver", "forged_unsat", "timeout_solver", "champion_copy"):
    sc_bad, _ = _adv_by_label[_bad]
    ck(f"adversarial {_bad} scores 0 (never a record-fall, never a paid row)",
       sc_bad == 0.0)
_arena_store.close()

# --------------------------------------------------------------------------
# 6. LANE I — pay-on-disagreement-proven-hardness payout (TASK 2).
# --------------------------------------------------------------------------
print("LANE I — payout on disagreement-proven hardness + anti-gaming gates")
from scaffold.publisher import arena_payout  # noqa: E402
from scaffold.lanes.solver_arena import AdapterOutput, Outcome as _O  # noqa: E402
from scaffold.lanes.arena_e2e import _rr  # noqa: E402

_li_app = build_app(database_path=":memory:", signing_key_hex=key_hex)
_li_store = _li_app.state.store
_li_lane: SolverArenaLane = _li_app.state.arena_lane
_li_lane.batch_size = 6

# A breaker instance: a real planted-3SAT CNF (so honest solvers CAN close it,
# proving the champion's timeout is the hard part, not unsolvability).
_brk_cnf, _ = gen_planted_3sat(99, 12, 36)
_brk_sol = solve_cnf(_brk_cnf)

# champion adapter: TIMES OUT on the breaker CNF, but solves standard batch CNFs
# (it is the champion — broadly competent, just not on this instance).
def _champ_adapter(cnf, timeout_ms):
    if cnf == _brk_cnf:
        return AdapterOutput(_O.TIMEOUT, [], "", _rr(timeout_ms, True))
    return AdapterOutput(_O.SAT, solve_cnf(cnf) or [], "", _rr(120.0))
_champ_spec = SolverSpec("https://x/champ", "sha256:" + "cc" * 32, "li-champion",
                         owner_hotkey="5ChampOwner")

# a broadly-competitive CLOSER: solves the breaker AND the standard batch.
def _closer_adapter(cnf, timeout_ms):
    return AdapterOutput(_O.SAT, solve_cnf(cnf) or [], "", _rr(80.0))
_closer_spec = SolverSpec("https://x/closer", "sha256:" + "dd" * 32, "li-closer",
                          owner_hotkey="5CloserOwner")

# register an instance via the route, submitted at round 5 (quarantine cutoff = 2).
sd_li = now_iso()
_li_sha = __import__("hashlib").sha256(_brk_cnf.encode()).hexdigest()
_iclaim = canonical_claim_bytes(
    bundle_hash=_blake3.blake3(b"").hexdigest(), card_id="synthetic_boolean_v1",
    miner_hotkey="placeholder", submitted_at=sd_li,
    challenge_id="arena-instance", dimacs_solution_sha256=_li_sha)
# insert the instance directly (the route requires a real signature; we test the
# PAYOUT engine, which the route already feeds — quarantine/min-batch are gated here).
def _ins_inst(conn):
    conn.execute(
        "INSERT INTO arena_instances(instance_id, owner_hotkey, cnf_sha256, cnf_text, "
        "submitted_round, quarantine_until_round, min_batch_score, status, created_at_iso) "
        "VALUES ('brk-1', '5InstanceOwner', ?, ?, 5, 8, 0.5, 'pending', ?)",
        (_li_sha, _brk_cnf, now_iso()))
_li_store.write(_ins_inst)

# HAPPY PATH: closer registered at round 1 (≤ 5−3=2, quarantine-clear), broadly
# competitive (solves the whole batch). Champion times out -> instance pays.
_brk1_stale_snapshot = dict(_li_store.query("SELECT * FROM arena_instances WHERE instance_id='brk-1'")[0])
v = arena_payout.settle_instance(
    _li_store, _li_lane, _brk1_stale_snapshot,
    champion_spec=_champ_spec, champion_adapter=_champ_adapter,
    closers=[(_closer_spec, _closer_adapter, 1)], current_round=6,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1")
ck("Lane I happy path: instance pays on disagreement-proven hardness", v["paid"] is True)
ck("Lane I price is positive and <= base (separation factor clamped)",
   0.0 < v["price"] <= 1.0)
# emitted row credits the INSTANCE owner + verifies.
_li_feed = _li_store.recent_rows(None, None, 50)
_li_rows = [r for r in _li_feed if r.get("miner_hotkey") == "5InstanceOwner"]
ck("Lane I emitted v6 + v5compat rows to the instance owner", len(_li_rows) == 2)
ck("every Lane I payout row verifies under the loaded key",
   len(_li_rows) == 2 and all(wire.verify_row(r, pub_hex) for r in _li_rows))
v_again = arena_payout.settle_instance(
    _li_store, _li_lane, _brk1_stale_snapshot,
    champion_spec=_champ_spec, champion_adapter=_champ_adapter,
    closers=[(_closer_spec, _closer_adapter, 1)], current_round=6,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1")
ck("Lane I same-round payout is atomic/idempotent even with stale input",
   v_again["paid"] is False and v_again["reason"] == "already_paid_this_round"
   and len([r for r in _li_store.recent_rows(None, None, 50)
            if r.get("miner_hotkey") == "5InstanceOwner"]) == 2)

# price decays with the round (0.97^r): a later round pays strictly less.
p_r6 = arena_payout.lane_i_price(2 * 5000.0, 80.0, 5000.0, 6)
p_r30 = arena_payout.lane_i_price(2 * 5000.0, 80.0, 5000.0, 30)
ck("Lane I price decays 0.97^round (later round pays less)", p_r30 < p_r6 < 1.0001)

# ANTI-GAMING 1 — QUARANTINE: a closer registered at round 5 (> cutoff 2) cannot
# serve as closing evidence for a round-5 instance (submit-and-farm-same-round).
def _ins_inst2(conn):
    conn.execute(
        "INSERT INTO arena_instances(instance_id, owner_hotkey, cnf_sha256, cnf_text, "
        "submitted_round, quarantine_until_round, min_batch_score, status, created_at_iso) "
        "VALUES ('brk-q', '5InstanceOwner2', ?, ?, 5, 8, 0.5, 'pending', ?)",
        (_li_sha, _brk_cnf, now_iso()))
_li_store.write(_ins_inst2)
vq = arena_payout.settle_instance(
    _li_store, _li_lane, dict(_li_store.query("SELECT * FROM arena_instances WHERE instance_id='brk-q'")[0]),
    champion_spec=_champ_spec, champion_adapter=_champ_adapter,
    closers=[(_closer_spec, _closer_adapter, 5)], current_round=6,   # reg round 5 > cutoff 2
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1")
ck("Lane I quarantine: same-round closer is rejected (no payout)",
   vq["paid"] is False and vq["reason"] == "no_valid_closer")

# ANTI-GAMING 2 — MIN_BATCH_SCORE: a closer that solves the breaker but <50% of
# the standard batch (trivially specialized) cannot close it.
def _specialized_closer(cnf, timeout_ms):
    if cnf == _brk_cnf:
        return AdapterOutput(_O.SAT, _brk_sol or [], "", _rr(70.0))
    return AdapterOutput(_O.TIMEOUT, [], "", _rr(timeout_ms, True))  # fails the batch
_spec_spec = SolverSpec("https://x/spec", "sha256:" + "ee" * 32, "li-specialist",
                        owner_hotkey="5SpecOwner")
def _ins_inst3(conn):
    conn.execute(
        "INSERT INTO arena_instances(instance_id, owner_hotkey, cnf_sha256, cnf_text, "
        "submitted_round, quarantine_until_round, min_batch_score, status, created_at_iso) "
        "VALUES ('brk-s', '5InstanceOwner3', ?, ?, 5, 8, 0.5, 'pending', ?)",
        (_li_sha, _brk_cnf, now_iso()))
_li_store.write(_ins_inst3)
vs = arena_payout.settle_instance(
    _li_store, _li_lane, dict(_li_store.query("SELECT * FROM arena_instances WHERE instance_id='brk-s'")[0]),
    champion_spec=_champ_spec, champion_adapter=_champ_adapter,
    closers=[(_spec_spec, _specialized_closer, 1)], current_round=6,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1")
ck("Lane I min-batch-score: trivially-specialized closer rejected (no payout)",
   vs["paid"] is False and vs["reason"] == "no_valid_closer")

# ANTI-GAMING 3 — CHAMPION DOESN'T TIME OUT: if the champion closes the instance,
# there is no proven hardness, so it never pays (reward dries up — PUPPER fix).
def _champ_solves_all(cnf, timeout_ms):
    return AdapterOutput(_O.SAT, solve_cnf(cnf) or [], "", _rr(50.0))
def _ins_inst4(conn):
    conn.execute(
        "INSERT INTO arena_instances(instance_id, owner_hotkey, cnf_sha256, cnf_text, "
        "submitted_round, quarantine_until_round, min_batch_score, status, created_at_iso) "
        "VALUES ('brk-easy', '5InstanceOwner4', ?, ?, 5, 8, 0.5, 'pending', ?)",
        (_li_sha, _brk_cnf, now_iso()))
_li_store.write(_ins_inst4)
ve = arena_payout.settle_instance(
    _li_store, _li_lane, dict(_li_store.query("SELECT * FROM arena_instances WHERE instance_id='brk-easy'")[0]),
    champion_spec=_champ_spec, champion_adapter=_champ_solves_all,
    closers=[(_closer_spec, _closer_adapter, 1)], current_round=6,
    private_key_hex=key_hex, epoch_salt="epoch_20260610:synthetic_boolean_v1")
ck("Lane I no-hardness: champion closes it -> never pays (self-healing benchmark)",
   ve["paid"] is False and ve["reason"] == "champion_did_not_time_out")
_li_store.close()

# --------------------------------------------------------------------------
# 6. VALIDATOR CLI — `cathedral-validator serve/migrate` surface + lean import.
# --------------------------------------------------------------------------
print("VALIDATOR CLI — serve config resolution, migrate no-op, lean import")
import subprocess  # noqa: E402
import tempfile  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402
from scaffold import cli as _cli  # noqa: E402

# migrate is a no-op that returns 0 (kept only for update-script parity).
ck("cli migrate is a no-op returning 0", _cli._cmd_migrate(_NS()) == 0)

# serve config resolves: built-in defaults < TOML < env < flags.
with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as _tf:
    _tf.write(
        '[network]\nname="finney"\nnetuid=39\nwallet_name="wA"\nvalidator_hotkey="hA"\n'
        '[publisher]\nurl="https://api.cathedral.computer"\n'
        '[weight_policy]\npublic_key_hex="' + pub_hex + '"\nkey_id="cathedral-weight-policy"\n')
    _cfg_path = _tf.name
_ns = _NS(config=_cfg_path, publisher_url=None, public_key_hex=None, network=None,
          netuid=None, wallet_name=None, wallet_hotkey=None, state_file=None,
          interval_secs=None, dry_run=True, once=True, offline=True)
_resolved = _cli._resolve_serve_config(_ns)
ck("cli serve reads wallet/netuid/key from TOML",
   _resolved.wallet_name == "wA" and _resolved.wallet_hotkey == "hA"
   and _resolved.netuid == 39 and _resolved.public_key_hex == pub_hex)
ck("cli serve --dry-run/--offline force broadcast off",
   _resolved.broadcast is False and _resolved.offline is True)
# flags override the TOML.
_ns2 = _NS(config=_cfg_path, publisher_url=None, public_key_hex=None, network=None,
           netuid=99, wallet_name="wB", wallet_hotkey=None, state_file=None,
           interval_secs=None, dry_run=False, once=True, offline=False)
_r2 = _cli._resolve_serve_config(_ns2)
ck("cli serve flag overrides TOML (netuid 99, wallet wB)",
   _r2.netuid == 99 and _r2.wallet_name == "wB")

# LEAN INSTALL: importing the validator must NOT drag in FastAPI/the server
# stack (run in a fresh process — this one already imported the publisher app).
_lean = subprocess.run(
    [sys.executable, "-c",
     "import scaffold.validator_thin, scaffold.cli, sys; "
     "sys.exit(0 if 'fastapi' not in sys.modules else 1)"],
    capture_output=True)
ck("validator imports WITHOUT pulling in FastAPI (lean install)", _lean.returncode == 0)

# offline is authoritative: --offline --broadcast together must NOT read the
# metagraph or broadcast (the two are contradictory; offline wins). Spy on the
# chain-touching calls.
from scaffold import validator_thin as _vt  # noqa: E402
_spy = {"metagraph": 0, "broadcast_arg": "unset"}
_o_mg, _o_set, _o_fetch = (_vt.metagraph_hotkey_to_uid, _vt.set_weights_on_chain,
                           _vt.fetch_vector)
try:
    _vt.fetch_vector = lambda url, timeout=30.0: vec
    def _spy_mg(**k):
        _spy["metagraph"] += 1
        return {}
    def _spy_set(uw, **k):
        _spy["broadcast_arg"] = k.get("broadcast")
        return True
    _vt.metagraph_hotkey_to_uid = _spy_mg
    _vt.set_weights_on_chain = _spy_set
    _off = _NS(publisher_url="x", public_key_hex=pub_hex, key_id="cathedral-weight-policy",
               network="finney", netuid=39, wallet_name="w", wallet_hotkey="h",
               state_file=os.path.join(tempfile.mkdtemp(), "fence.json"),  # absent -> fence -1
               offline=True, broadcast=True, once=True, interval_secs=1.0)
    _vt.tick(_off)
    ck("offline authoritative: --offline --broadcast does NOT read the metagraph",
       _spy["metagraph"] == 0)
    ck("offline authoritative: --offline --broadcast does NOT broadcast",
       _spy["broadcast_arg"] is False)
finally:
    _vt.metagraph_hotkey_to_uid, _vt.set_weights_on_chain, _vt.fetch_vector = (
        _o_mg, _o_set, _o_fetch)

# --------------------------------------------------------------------------
fails = [n for n, c in checks if not c]
print(f"\nPUBLISHER VERIFY: "
      f"{'PASS all ' + str(len(checks)) + ' checks' if not fails else 'FAIL ' + str(fails)}")
sys.exit(1 if fails else 0)
