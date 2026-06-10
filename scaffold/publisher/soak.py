"""G4 — Soak harness for the thin publisher.

Runs three checks and emits ONE PASS/FAIL report:

  (a) Validator-style pull loop: walk the thin publisher's feed with the tuple
      cursor, verifying EVERY row signature against a configured SET of public
      keys (production key from fixtures jwks + the local staging test key).
      Reports per-key verified counts; FAILS if any row verifies under none.

  (b) 7-day score divergence: compute per-hotkey 7-day mean weighted_score from
      the thin feed vs the live feed and report the max absolute divergence
      against the shadow thresholds (warn 0.01 / fail 0.10 from V4-DESIGN /
      rc_verify). Because live scoring is flat 1.0 (COMPAT.md), a faithful
      re-served feed diverges by ~0.

  (c) Miner-path smoke: token fetch + submit on a refilled (locally-minted)
      challenge using //SoakMiner — exercises the live miner surface end to end.

Usage:
    # against a locally-running seeded thin publisher:
    python -m scaffold.publisher.soak --thin http://127.0.0.1:8000
    # live-feed read-only divergence/verify portion only (no local publisher):
    python -m scaffold.publisher.soak --live-only
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import wire

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "live-20260609"
LIVE_BASE = "https://api.cathedral.computer"
WARN_THRESHOLD = 0.01
FAIL_THRESHOLD = 0.10


def _http_json(url: str, timeout: int = 90, retries: int = 3,
               headers: dict | None = None, method: str = "GET",
               data: bytes | None = None) -> tuple[int, Any]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "cathedral-soak/1.0", **(headers or {})},
                method=method, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                try:
                    return r.status, json.loads(body)
                except Exception:
                    return r.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            import time
            time.sleep(2 * (attempt + 1))
    raise last  # type: ignore[misc]


def _pull_feed(base: str, since: str, limit: int = 200, max_pages: int = 100,
               timeout: int = 90, pace: float = 1.0, log=print) -> list[dict]:
    """Tuple-cursor walk of /v1/leaderboard/recent from `since`."""
    import time
    out: list[dict] = []
    cur_ran, cur_id = since, None
    url = f"{base}/v1/leaderboard/recent"
    for p in range(max_pages):
        params = f"?since={cur_ran}&limit={limit}"
        if cur_id:
            params += f"&since_ran_at={cur_ran}&since_id={cur_id}"
        st, page = _http_json(url + params, timeout=timeout)
        if st != 200 or not isinstance(page, dict):
            log(f"    feed pull stopped: status={st}")
            break
        items = page.get("items", [])
        if not items:
            break
        out += items
        cur_ran = page.get("next_since_ran_at") or page.get("next_since") or cur_ran
        cur_id = page.get("next_since_id")
        if len(items) < limit:
            break
        if pace:
            time.sleep(pace)
    return out


def _load_keyset() -> dict[str, str]:
    """Configured public-key SET: production key (fixtures jwks) + any
    CATHEDRAL_SOAK_EXTRA_PUBKEYS (comma-sep name=hex)."""
    import os
    jwks = json.loads((FIX / "jwks.json").read_text())
    prod = next(k for k in jwks["keys"] if k["kid"] == "cathedral-eval-signing")
    keys = {"production": prod["public_key_hex"]}
    extra = os.environ.get("CATHEDRAL_SOAK_EXTRA_PUBKEYS", "").strip()
    for tok in (t for t in extra.split(",") if t.strip()):
        name, _, hx = tok.partition("=")
        if hx:
            keys[name.strip()] = hx.strip()
    return keys


def verify_with_keyset(rows: list[dict], keyset: dict[str, str]) -> dict:
    """Verify each row against the key SET; report per-key counts + unverified."""
    per_key: dict[str, int] = defaultdict(int)
    unverified: list[str] = []
    for r in rows:
        matched = None
        for name, hx in keyset.items():
            try:
                if wire.verify_row(r, hx):
                    matched = name
                    break
            except Exception:
                continue
        if matched:
            per_key[matched] += 1
        else:
            unverified.append(r.get("id", "?"))
    return {"total": len(rows), "per_key": dict(per_key),
            "unverified": unverified, "unverified_count": len(unverified)}


def per_hotkey_7d_scores(rows: list[dict], now: datetime | None = None) -> dict[str, float]:
    """Per-hotkey 7-day mean weighted_score over v6 rows (the live aggregation
    shape: schema_version IN (5,6), synthetic_boolean_v1). v5 rows mirror v6, so
    counting v6 only avoids double-weighting one solve."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    acc: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if int(r.get("eval_output_schema_version", 0)) != 6:
            continue
        if r.get("task_type") != "synthetic_boolean_v1":
            continue
        ran = r.get("ran_at")
        try:
            t = datetime.fromisoformat(ran.replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff:
            continue
        acc[r["miner_hotkey"]].append(float(r.get("weighted_score", 0.0)))
    return {hk: sum(v) / len(v) for hk, v in acc.items() if v}


def score_divergence(thin_rows: list[dict], live_rows: list[dict]) -> dict:
    thin = per_hotkey_7d_scores(thin_rows)
    live = per_hotkey_7d_scores(live_rows)
    common = set(thin) & set(live)
    max_abs = 0.0
    worst = None
    for hk in common:
        d = abs(thin[hk] - live[hk])
        if d > max_abs:
            max_abs, worst = d, hk
    return {"thin_hotkeys": len(thin), "live_hotkeys": len(live),
            "common_hotkeys": len(common), "thin_only": len(set(thin) - set(live)),
            "live_only": len(set(live) - set(thin)), "max_abs_divergence": max_abs,
            "worst_hotkey": worst}


def miner_smoke(thin_base: str, log=print) -> dict:
    """token fetch + submit on a locally-minted active challenge using //SoakMiner."""
    try:
        from bittensor_wallet import Keypair
    except Exception as e:
        return {"ok": False, "reason": f"bittensor_wallet unavailable: {e}"}
    from .auth import canonical_claim_bytes
    from ..dimacs import solve_cnf
    import blake3 as _b3
    import hashlib

    def now_iso():
        d = datetime.now(timezone.utc)
        return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"

    miner = Keypair.create_from_uri("//SoakMiner")
    empty = _b3.blake3(b"").hexdigest()

    # find an active challenge whose CNF this publisher actually serves (locally
    # minted). Externally-mirrored seed challenges have no CNF body and 404 on
    # fetch — skip them and try the next.
    st, board = _http_json(f"{thin_base}/v1/synthetic-boolean/active-challenges")
    if st != 200 or not board.get("items"):
        return {"ok": False, "reason": f"no active challenges (status {st})"}

    # prefer the dedicated small smoke challenge (fast DPLL); then any other.
    items = sorted(board["items"],
                   key=lambda it: (0 if it["challenge_id"] == "sat-soak-smoke-001" else 1,
                                   int(it.get("num_vars", 10**9))))
    cid = cnf_text = None
    tried = 0
    for item in items:
        cand = item["challenge_id"]
        tried += 1
        sa = now_iso()
        cnf_claim = canonical_claim_bytes(
            bundle_hash=empty, card_id="synthetic_boolean_v1",
            miner_hotkey=miner.ss58_address, submitted_at=sa, challenge_id="",
            dimacs_solution_sha256="")
        cnf_sig = base64.b64encode(miner.sign(cnf_claim)).decode()
        st, j = _http_json(
            f"{thin_base}/v1/synthetic-boolean/active-cnf?challenge_id={cand}",
            headers={"X-Cathedral-Hotkey": miner.ss58_address,
                     "X-Cathedral-Signature": cnf_sig,
                     "X-Cathedral-Submitted-At": sa})
        if st != 200 or "cnf_url" not in j:
            continue
        st, body = _http_json(f"{thin_base}{j['cnf_url']}")
        if st == 200 and str(body).startswith("p cnf"):
            cid, cnf_text = cand, body
            break
        if tried >= 60:
            break
    if cid is None:
        return {"ok": False, "reason": "no locally-served CNF found "
                "(board may be all external mirrors; enable refill to mint local)"}

    log("    solving CNF (DPLL)...")
    sol = solve_cnf(cnf_text)
    if sol is None:
        return {"ok": False, "reason": "DPLL returned UNSAT (unexpected for planted)"}
    blob = "s SATISFIABLE\nv " + " ".join(str(x) for x in sol) + " 0\n"
    sa2 = now_iso()
    sol_sha = hashlib.sha256(blob.encode()).hexdigest()
    claim = canonical_claim_bytes(
        bundle_hash=empty, card_id="synthetic_boolean_v1",
        miner_hotkey=miner.ss58_address, submitted_at=sa2, challenge_id=cid,
        dimacs_solution_sha256=sol_sha)
    sig = base64.b64encode(miner.sign(claim)).decode()
    body = urllib.parse.urlencode({
        "card_id": "synthetic_boolean_v1", "submitted_at": sa2,
        "challenge_id": cid, "dimacs_solution": blob}).encode()
    st, resp = _http_json(
        f"{thin_base}/v1/agents/submit", method="POST", data=body,
        headers={"X-Cathedral-Hotkey": miner.ss58_address,
                 "X-Cathedral-Signature": sig,
                 "Content-Type": "application/x-www-form-urlencoded"})
    ok = st == 200 and isinstance(resp, dict) and resp.get("status") == "ranked"
    return {"ok": ok, "challenge_id": cid, "submit_status": st, "resp": resp}


import urllib.parse  # noqa: E402


def _provision_smoke_challenge(db_path: str, *, tier: int = 1, log=print) -> str:
    """Insert a small DPLL-tractable local challenge directly into the store so
    the miner smoke has something it can actually solve. Idempotent."""
    from .store import Store
    from .app import seed_challenge
    from ..dimacs import gen_planted_3sat
    cid = "sat-soak-smoke-001"
    store = Store(db_path)
    existing = store.query(
        "SELECT status FROM lane_challenges WHERE challenge_id=?", (cid,))
    if not existing or existing[0]["status"] != "active":
        cnf, _ = gen_planted_3sat(2024, 30, 90)  # small, solvable in ms
        seed_challenge(store, challenge_id=cid, tier=tier, cnf_text=cnf, status="active")
        def _stamp(conn):
            conn.execute(
                "UPDATE lane_challenges SET updated_at_iso=created_at_iso WHERE challenge_id=?",
                (cid,))
        store.write(_stamp)
        log(f"    provisioned smoke challenge {cid} (n=30,m=90)")
    store.close()
    return cid


def run(args: argparse.Namespace, log=print) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    keyset = _load_keyset()
    results: dict[str, Any] = {}
    checks: list[tuple[str, bool]] = []

    def ck(name, cond):
        checks.append((name, bool(cond)))
        log(f"  {'PASS' if cond else 'FAIL'} {name}")

    log(f"SOAK — since={since} keyset={list(keyset)}")

    # (b live half always runs — it's public, read-only)
    log("LIVE FEED — pulling for divergence baseline + signature verify")
    live_rows = _pull_feed(LIVE_BASE, since, limit=args.page_limit,
                           max_pages=args.max_pages, pace=args.pace, log=log)
    log(f"  live rows pulled: {len(live_rows)}")
    live_verify = verify_with_keyset(live_rows, keyset)
    ck(f"live rows all verify under keyset (unverified={live_verify['unverified_count']})",
       live_verify["unverified_count"] == 0)
    results["live_verify"] = live_verify

    if args.live_only or not args.thin:
        log("LIVE-ONLY mode: thin-feed + miner-smoke skipped")
        thin_rows = live_rows  # self-divergence == 0 sanity baseline
        results["mode"] = "live-only"
    else:
        log(f"THIN FEED — pulling from {args.thin}")
        thin_rows = _pull_feed(args.thin, since, limit=args.page_limit,
                               max_pages=args.max_pages, pace=0, log=log)
        log(f"  thin rows pulled: {len(thin_rows)}")
        results["mode"] = "thin-vs-live"

    # (a) signature verify on thin feed
    thin_verify = verify_with_keyset(thin_rows, keyset)
    ck(f"thin rows all verify under keyset (unverified={thin_verify['unverified_count']}, "
       f"per_key={thin_verify['per_key']})", thin_verify["unverified_count"] == 0)
    results["thin_verify"] = thin_verify

    # (b) divergence
    div = score_divergence(thin_rows, live_rows)
    results["divergence"] = div
    md = div["max_abs_divergence"]
    band = "OK" if md < WARN_THRESHOLD else ("WARN" if md < FAIL_THRESHOLD else "FAIL")
    log(f"  7d score divergence: max_abs={md:.4f} ({band}) "
        f"common_hotkeys={div['common_hotkeys']} worst={div['worst_hotkey']}")
    # an empty intersection makes the divergence check vacuously true — require
    # real overlap before low divergence counts as a pass.
    ck("divergence computed over a non-empty hotkey intersection",
       div["common_hotkeys"] > 0)
    ck(f"7d score divergence < fail threshold {FAIL_THRESHOLD}", md < FAIL_THRESHOLD)

    # (c) miner smoke (only with a thin publisher)
    if not args.live_only and args.thin:
        if args.smoke_db:
            # Provision a SMALL, DPLL-tractable local challenge so the smoke
            # exercises the token+submit API path quickly. Live-shape n=6000
            # instances sit near the SAT phase transition; the scaffold's toy
            # DPLL cannot close them in soak time (a real miner brings
            # kissat/cadical) — so the smoke uses a small instance on purpose.
            _provision_smoke_challenge(args.smoke_db, log=log)
        log("MINER SMOKE — token fetch + solve + submit")
        smoke = miner_smoke(args.thin, log=log)
        results["miner_smoke"] = smoke
        ck(f"miner path: token fetch + submit ranked ({smoke.get('reason','ok')})",
           smoke["ok"])

    fails = [n for n, c in checks if not c]
    verdict = "PASS" if not fails else "FAIL"
    log("\n" + "=" * 60)
    log(f"SOAK REPORT: {verdict}  ({len(checks) - len(fails)}/{len(checks)} checks)")
    log(f"  mode               : {results.get('mode')}")
    log(f"  live rows          : {len(live_rows)}")
    log(f"  thin rows          : {len(thin_rows)}")
    log(f"  keyset             : {list(keyset)}")
    log(f"  thin per-key verify: {results.get('thin_verify', {}).get('per_key')}")
    log(f"  unverified (thin)  : {results.get('thin_verify', {}).get('unverified_count')}")
    log(f"  max 7d divergence  : {md:.4f}  (warn {WARN_THRESHOLD} / fail {FAIL_THRESHOLD}) -> {band}")
    if "miner_smoke" in results:
        log(f"  miner smoke        : {'ok' if results['miner_smoke']['ok'] else results['miner_smoke'].get('reason')}")
    if fails:
        log(f"  FAILED CHECKS      : {fails}")
    log("=" * 60)
    return 1 if fails else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Soak the thin publisher feed + miner path.")
    p.add_argument("--thin", default=None, help="thin publisher base URL (e.g. http://127.0.0.1:8000)")
    p.add_argument("--live-only", action="store_true",
                   help="run only the public live-feed verify + divergence baseline")
    p.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    p.add_argument("--page-limit", type=int, default=200, help="feed page size")
    p.add_argument("--max-pages", type=int, default=20, help="cap pages pulled per feed")
    p.add_argument("--pace", type=float, default=1.0, help="sleep between live feed pages")
    p.add_argument("--smoke-db", default=None,
                   help="path to the running publisher's SQLite DB; if given, a small "
                        "DPLL-tractable local challenge is provisioned for the miner smoke")
    return run(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
