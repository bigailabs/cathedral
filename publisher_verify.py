"""publisher_verify.py — the thin publisher's self-compat gate.

Proves the publisher emits rows that (a) verify under its own loaded key via
wire.verify_row, and (b) have EXACTLY the signed-key shape of same-version live
rows in fixtures (key-set equality per schema version 5/6). Also pins:

  * the sr25519 golden vectors (//Alice round-trip + tamper/wrong-key reject),
  * the 9 adversarial + 3 golden DIMACS-solution fixtures (Lane A referee),
  * the active-cnf token flow + constant-time compare + opaque 404,
  * the full M4 end-to-end loop (miner signs, fetches CNF, solves with DPLL,
    submits; validator-style tuple-cursor pull verifies every signature).

Run with a python that has cryptography + bittensor_wallet + fastapi
(the cathedral venv): ~/code/cathedral/.venv/bin/python publisher_verify.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

from scaffold import wire
from scaffold.publisher import build_app, seed_challenge
from scaffold.publisher import rows as rowmod
from scaffold.publisher.auth import (
    ALICE_SS58, canonical_claim_bytes, default_verifier,
)
from scaffold.publisher.keys import generate_test_key
from scaffold.publisher.sat_solution import verify_dimacs_solution
from scaffold.dimacs import gen_planted_3sat, solve_cnf

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
    j = client.get("/.well-known/cathedral-jwks.json").json()
    ck("/.well-known jwks served with eval-signing key",
       any(k["kid"] == "cathedral-eval-signing" for k in j["keys"]))

    # active-challenges field-compat (board.json shape)
    ac = client.get("/v1/synthetic-boolean/active-challenges").json()
    board_keys = set(json.loads((FIX / "board.json").read_text())["items"][0])
    pub_keys = set(ac["items"][0])
    ck("active-challenges item carries the live board fields",
       board_keys - {"difficulty_label", "storage"} <= pub_keys)

    from bittensor_wallet import Keypair  # noqa
    miner = Keypair.create_from_uri("//E2EMiner")
    from datetime import datetime, timezone
    def now_iso():
        d = datetime.now(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    # active-cnf: hotkey-signed token fetch
    sa = now_iso()
    import blake3 as _blake3
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
    # token gates: bad token -> opaque 404
    bad = client.get(cnf_url[:cnf_url.index("?t=")] + "?t=deadbeef.bad")
    ck("bad CNF token -> opaque 404", bad.status_code == 404)
    cnf_text = client.get(cnf_url).text
    ck("valid token fetches the CNF", cnf_text.startswith("p cnf"))

    # solve with DPLL + submit (6-field signed)
    sol = solve_cnf(cnf_text)
    blob = "s SATISFIABLE\nv " + " ".join(str(x) for x in sol) + " 0\n"
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
    ck("flat policy emits weighted_score 1.0", resp.json().get("weighted_score") == 1.0)

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

    # COVERAGE POLICY (flag-gated; flat stays the default for the cutover):
    # weighted_score = solved/available in the trailing window, clamped.
    from scaffold.publisher import scoring as _scoring
    os.environ[_scoring.SCORING_POLICY_ENV] = "coverage"
    try:
        ws1 = _scoring.weighted_score_for(store, miner.ss58_address)
        # miner solved 1 of the 1 challenge minted in-window -> full score
        ck("coverage: full-coverage miner scores 1.0", ws1 == 1.0)
        ws_idle = _scoring.weighted_score_for(store, "5IdleHotkeyNeverSolved")
        ck("coverage: idle miner floors at the configured minimum",
           ws_idle == _scoring.coverage_floor())
        # seeded EXTERNAL board mirrors (unsolvable through this publisher)
        # must NOT inflate the coverage denominator and crater every score.
        def _ext(conn):
            conn.execute(
                "INSERT OR REPLACE INTO lane_challenges(challenge_id, family_id, "
                "tier, cnf_text, cnf_sha256, cnf_bytes, num_vars, num_clauses, "
                "status, cnf_source, created_at_iso) "
                "VALUES ('sat-external-mirror', 'synthetic_boolean_v1', 1, '', "
                "'external-mirror-no-cnf', 0, 0, 0, 'active', 'external', ?)", (now_iso(),))
        store.write(_ext)
        ws_after = _scoring.weighted_score_for(store, miner.ss58_address)
        ck("coverage: external mirror challenges do not dilute the denominator",
           ws_after == 1.0)
    finally:
        os.environ.pop(_scoring.SCORING_POLICY_ENV, None)
    ck("flat policy restored as default", _scoring.scoring_policy() == "flat")

    # COVERAGE DENOMINATOR = relative-to-top (TASK 0). In prod the publisher
    # mints far more challenges than any miner solves (92% expire), so the old
    # solved/minted denominator floored EVERYONE. The denominator is now the
    # MAX distinct-challenges solved by any single miner in the window, so the
    # top solver ≈1.0 and a half-as-active one ≈0.5 — independent of mint rate.
    os.environ[_scoring.SCORING_POLICY_ENV] = "coverage"
    try:
        cov_store = build_app(database_path=":memory:", signing_key_hex=key_hex).state.store
        # Seed 3 hotkeys with different distinct-solve counts: top=10, mid=5,
        # low=2 — into a SINGLE challenge-solve ledger. Mint MANY more
        # challenges than anyone solved so the old denominator would floor all.
        def _seed_cov(conn):
            cn = now_iso()
            for i in range(40):  # 40 minted challenges; nobody solves them all
                conn.execute(
                    "INSERT OR REPLACE INTO lane_challenges(challenge_id, family_id, "
                    "tier, cnf_text, cnf_sha256, cnf_bytes, num_vars, num_clauses, "
                    "status, cnf_source, created_at_iso) VALUES "
                    f"('cov-{i}', 'synthetic_boolean_v1', 1, '', 'h{i}', 0, 0, 0, "
                    "'active', 'local', ?)", (cn,))
            for hk, k in (("5TopSolver", 10), ("5MidSolver", 5), ("5LowSolver", 2)):
                for i in range(k):
                    conn.execute(
                        "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, "
                        "miner_hotkey, solved_at_iso) VALUES (?, ?, ?)",
                        (f"cov-{i}", hk, cn))
        cov_store.write(_seed_cov)
        _scoring._reset_coverage_denom_cache()
        top = _scoring.weighted_score_for(cov_store, "5TopSolver")
        mid = _scoring.weighted_score_for(cov_store, "5MidSolver")
        low = _scoring.weighted_score_for(cov_store, "5LowSolver")
        ck("coverage denom: top solver scores ~1.0 (not floored by mint rate)",
           abs(top - 1.0) < 1e-9)
        ck("coverage denom: half-as-active solver scores ~0.5 (relative-to-top)",
           abs(mid - 0.5) < 1e-9)
        ck("coverage denom: low solver scales proportionally (2/10=0.2)",
           abs(low - 0.2) < 1e-9)
        # denominator is cached (one GROUP-BY-MAX, not per-submit table scan).
        # reset first: the weighted_score_for calls above populated the cache at
        # real wall-clock time, which would mask the synthetic-now TTL test.
        _scoring._reset_coverage_denom_cache()
        d1 = _scoring.coverage_denominator(cov_store, now=1000.0)
        # a new top-beating solver appears, but the cache must hold the old denom
        # until the TTL elapses.
        def _seed_more(conn):
            cn = now_iso()
            for i in range(20):
                conn.execute(
                    "INSERT OR IGNORE INTO lane_challenge_solves(challenge_id, "
                    "miner_hotkey, solved_at_iso) VALUES (?, ?, ?)",
                    (f"cov-{i}", "5MegaSolver", cn))
        cov_store.write(_seed_more)
        d_cached = _scoring.coverage_denominator(cov_store, now=1000.0 + 30.0)
        ck("coverage denom: cached within TTL (no per-submit GROUP-BY-MAX)",
           d_cached == d1 == 10)
        d_fresh = _scoring.coverage_denominator(
            cov_store, now=1000.0 + _scoring._COVERAGE_DENOM_TTL_SECS + 1.0)
        ck("coverage denom: recomputes after TTL elapses (now sees 20)",
           d_fresh == 20)
        cov_store.close()
    finally:
        os.environ.pop(_scoring.SCORING_POLICY_ENV, None)
        _scoring._reset_coverage_denom_cache()

    # validator-style pull loop: tuple cursor, verify EVERY signature.
    pulled = []
    cur_ra = cur_id = None
    for _ in range(12):
        params = {"limit": 1}
        if cur_ra:
            params["since_ran_at"] = cur_ra
            params["since_id"] = cur_id
        page = client.get("/v1/leaderboard/recent", params=params).json()
        if not page["items"]:
            break
        pulled += page["items"]
        cur_ra, cur_id = page["next_since_ran_at"], page["next_since_id"]
    ck("validator pull retrieved v6 + v5compat rows for BOTH solves", len(pulled) == 4)
    all_verify = all(wire.verify_row(r, pub_hex) for r in pulled)
    ck("every pulled row signature verifies (validator would score it)", all_verify)
    ck("cursor fields present on feed response",
       all(k in client.get("/v1/leaderboard/recent").json()
           for k in ("next_since", "next_since_ran_at", "next_since_id", "merkle_epoch_latest")))

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
fails = [n for n, c in checks if not c]
print(f"\nPUBLISHER VERIFY: "
      f"{'PASS all ' + str(len(checks)) + ' checks' if not fails else 'FAIL ' + str(fails)}")
sys.exit(1 if fails else 0)
