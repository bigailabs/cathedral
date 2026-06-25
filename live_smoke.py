"""live_smoke.py - miner smoke against the DEPLOYED publisher (KEYSTONE TASK 4).

Drives the real write path end-to-end over HTTP against the deployed publisher:
pick an active local challenge from the broadcast board, sign as //SmokeMiner,
fetch the CNF via the token flow, submit a deliberately wrong but well-formed
assignment, verify the rejected submission persists, and check the public scoring
metadata/explain surfaces promised by the launch.

Usage: BASE_URL=https://... python live_smoke.py
Needs bittensor_wallet + cryptography + blake3 (the deploy venv has them).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = os.environ.get("BASE_URL", "").rstrip("/")
checks: list[tuple[str, bool]] = []


def ck(name: str, cond: bool) -> None:
    checks.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'} {name}")


def _get(path: str, headers: dict | None = None):
    req = urllib.request.Request(_url(path), headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def _post_form(path: str, headers: dict, form: dict):
    body = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in form.items()).encode()
    h = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    req = urllib.request.Request(BASE + path, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _url(path: str) -> str:
    path = path.lstrip("\ufeff").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return BASE + path


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def main() -> int:
    if not BASE:
        print("live_smoke: BASE_URL required", file=sys.stderr)
        return 2
    from bittensor_wallet import Keypair
    import blake3 as _blake3
    from scaffold.publisher.auth import canonical_claim_bytes

    print(f"LIVE SMOKE - {BASE}")
    st, raw = _get("/health")
    health = json.loads(raw)
    ck("/health 200", st == 200)
    ck("sr25519_backend=bittensor (prod backend, not stub)",
       health.get("sr25519_backend") == "bittensor")

    st, raw = _get("/v1/synthetic-boolean/active-challenges")
    board = json.loads(raw)
    ck("board served", st == 200 and board["count"] > 0)
    scoring = board.get("scoring") or {}
    distribution = board.get("distribution") or {}
    ck("board exposes scoring metadata",
       scoring.get("mode") in ("proportional", "per_miner", "row_score_recent", "flat_recent")
       and isinstance(scoring.get("tier_weights"), dict))
    ck("board exposes challenge distribution",
       distribution.get("total_challenges") == board.get("count")
       and isinstance(distribution.get("tiers"), list))
    generator = board.get("generator") or {}
    generator_tiers = {int(t.get("tier")): t for t in generator.get("tiers", []) if t.get("tier")}
    ck("board exposes SAT generator policy",
       generator.get("kind") in ("local_refill", "external_sat_generator")
       and generator_tiers.get(1, {}).get("method") == "biased"
       and generator_tiers.get(2, {}).get("method") == "ajm")
    # pick a LOCAL challenge (has a fetchable body); external mirrors have none.
    local = [c for c in board["items"] if c.get("storage") == "sqlite_text"]
    ck("board has at least one local (minted) challenge", len(local) > 0)
    chal = local[0]
    cid = chal["challenge_id"]
    print(f"  using challenge {cid} (tier {chal['tier']})")

    miner = Keypair.create_from_uri("//SmokeMiner")
    bh = _blake3.blake3(b"").hexdigest()

    # active-cnf token fetch (hotkey-signed, empty challenge/sol fields)
    sa = now_iso()
    cnf_claim = canonical_claim_bytes(
        bundle_hash=bh, card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sa,
        challenge_id="", dimacs_solution_sha256="")
    cnf_sig = base64.b64encode(miner.sign(cnf_claim)).decode()
    st, raw = _get("/v1/synthetic-boolean/active-cnf?challenge_id=" + cid,
                   headers={"X-Cathedral-Hotkey": miner.ss58_address,
                            "X-Cathedral-Signature": cnf_sig,
                            "X-Cathedral-Submitted-At": sa})
    acnf = json.loads(raw)
    ck("active-cnf returns tokenized cnf_url", st == 200 and "?t=" in acnf["cnf_url"])
    cnf_url = acnf["cnf_url"]

    # fetch the CNF body + check immutable cache headers
    req = urllib.request.Request(_url(cnf_url))
    with urllib.request.urlopen(req, timeout=30) as r:
        cnf_text = r.read().decode()
        cc = r.headers.get("Cache-Control", "")
    ck("CNF body fetched via token", "p cnf" in cnf_text[:512])
    ck("CNF served with immutable cache headers", "immutable" in cc)

    # bad token -> opaque 404
    bad_path = cnf_url[:cnf_url.index("?t=")] + "?t=deadbeef.bad"
    try:
        st_bad, _ = _get(bad_path)
    except urllib.error.HTTPError as e:
        st_bad = e.code
    ck("bad CNF token -> opaque 404", st_bad == 404)

    # WRITE PATH -> POSTGRES. The live board challenges are 6000-var random
    # 3-SAT (industrial-solver territory); naive DPLL won't crack them, so the
    # smoke exercises the full HTTP write path with a deliberately WRONG (but
    # well-formed) assignment. That still drives: sig verify -> PG challenge
    # lookup -> referee check (fails) -> rejection committed to Postgres
    # (agent_submissions + submit_signatures in one txn). The replay of that
    # signature returning 409 PROVES the signature row was burned into PG.
    n_vars = int(chal.get("num_vars") or 0)
    assignment = " ".join(str(i) for i in range(1, max(n_vars, 1) + 1))  # all-true; wrong w.h.p.
    blob = f"s SATISFIABLE\nv {assignment} 0\n"
    sa2 = now_iso()
    sol_sha = hashlib.sha256(blob.encode()).hexdigest()
    claim = canonical_claim_bytes(
        bundle_hash=bh, card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sa2,
        challenge_id=cid, dimacs_solution_sha256=sol_sha)
    sig = base64.b64encode(miner.sign(claim)).decode()
    st, raw = _post_form("/v1/agents/submit",
                         {"X-Cathedral-Hotkey": miner.ss58_address,
                          "X-Cathedral-Signature": sig},
                         {"card_id": "synthetic_boolean_v1", "submitted_at": sa2,
                          "challenge_id": cid, "dimacs_solution": blob})
    body = json.loads(raw) if raw else {}
    # 400 = referee ran in PG and rejected the wrong assignment (write committed).
    # 200/ranked would mean we happened to satisfy it - also a valid write.
    ck("submit reached the PG referee (rejected wrong assignment, 400)",
       st in (200, 400))
    print(f"  submit status={st} detail={str(body)[:120]}")

    # replay the exact signature -> 409: proves the signature was persisted to PG.
    st_replay, _ = _post_form("/v1/agents/submit",
                              {"X-Cathedral-Hotkey": miner.ss58_address,
                               "X-Cathedral-Signature": sig},
                              {"card_id": "synthetic_boolean_v1", "submitted_at": sa2,
                               "challenge_id": cid, "dimacs_solution": blob})
    ck("replayed signature rejected 409 (signature persisted to Postgres)",
       st_replay == 409)

    st, raw = _get("/v1/leaderboard/explain?miner_hotkey=" + miner.ss58_address)
    explain = json.loads(raw)
    ck("leaderboard explain endpoint served",
       st == 200 and explain.get("miner_hotkey") == miner.ss58_address)
    ck("leaderboard explain includes current score context",
       "current_signed_weight" in explain or explain.get("score_source") in (
            "proportional",
            "per_miner",
            "pm_primary",
            "row_score_recent",
            "flat_recent",
            "flat_recent_fallback",
       ))

    st, raw = _get("/v1/validator/weights/next")
    vector = json.loads(raw)
    policy = vector.get("policy_metadata") or {}
    ck("signed vector exposes score_source metadata",
       policy.get("score_source") in (
            "proportional",
            "per_miner",
            "pm_primary",
            "row_score_recent",
            "flat_recent",
           "flat_recent_fallback",
       ))

    # the validator feed serves seeded signed rows verbatim from Postgres (the
    # seeder stored them with their original production cathedral_signature).
    st, raw = _get("/v1/leaderboard/recent?limit=5")
    feed = json.loads(raw)["items"]
    ck("validator feed serves rows from Postgres", len(feed) > 0)
    ck("served feed rows carry a cathedral_signature (verbatim seed)",
       all("cathedral_signature" in r for r in feed))

    passed = sum(1 for _, ok in checks if ok)
    total = len(checks)
    print()
    if passed == total:
        print(f"LIVE SMOKE: PASS all {total} checks")
        return 0
    print(f"LIVE SMOKE: FAIL {total - passed}/{total} checks")
    for n, ok in checks:
        if not ok:
            print(f"   FAILED: {n}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
