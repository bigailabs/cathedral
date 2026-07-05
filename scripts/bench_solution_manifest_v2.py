#!/usr/bin/env python3
"""Local load comparison: current full submit vs V2 manifest admission.

No live services are touched. The script builds the FastAPI app in-process with a
throwaway SQLite database, then sends signed miner requests through ASGITransport.

It is intentionally a coarse admission benchmark, not a full production capacity
model. It answers: how much work does the request path do before returning?
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import blake3
import httpx
from bittensor_wallet import Keypair

CARD_ID = "synthetic_boolean_v1"
EMPTY_BUNDLE = blake3.blake3(b"").hexdigest()


def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[idx]


def install_env(args: argparse.Namespace) -> None:
    os.environ.setdefault("CATHEDRAL_SERVICE_ROLE", "all")
    os.environ.setdefault("CATHEDRAL_RATELIMIT_RPM", "0")
    os.environ.setdefault("CATHEDRAL_ABUSE_RATELIMIT_RPM", "0")
    os.environ.setdefault("CATHEDRAL_CNF_TOKEN_SECRET", "bench-secret")
    os.environ.setdefault("CATHEDRAL_PERMINER_ENABLED", "1")
    os.environ.setdefault("CATHEDRAL_PERMINER_SEED_SECRET", "bench-perminer-secret")
    os.environ.setdefault("CATHEDRAL_PERMINER_ALLOTMENT_T1", str(max(args.total + 20, 100)))
    os.environ.setdefault("CATHEDRAL_PERMINER_ALLOTMENT_T2", "1")
    os.environ.setdefault("CATHEDRAL_PERMINER_METHOD_T1", "biased")
    os.environ.setdefault("CATHEDRAL_PERMINER_NVARS_T1", str(args.nvars))
    os.environ.setdefault("CATHEDRAL_PERMINER_NCLAUSES_T1", str(args.nclauses))
    os.environ.setdefault("CATHEDRAL_SUBMIT_MIN_INTERVAL_SECS", "0")
    os.environ.setdefault("CATHEDRAL_SUBMIT_BUSY_WAIT_SECS", str(args.busy_wait))
    os.environ.setdefault("CATHEDRAL_SUBMIT_MAX_CONCURRENCY", str(args.submit_concurrency_cap))
    os.environ.setdefault("CATHEDRAL_SUBMIT_HARD_CAP", str(args.submit_hard_cap))
    os.environ.setdefault("CATHEDRAL_V2_ENABLED", "true")


def dimacs_solution(assignment: list[int]) -> str:
    lines = ["s SATISFIABLE"]
    chunk: list[str] = []
    for lit in assignment:
        chunk.append(str(lit))
        if len(chunk) >= 32:
            lines.append("v " + " ".join(chunk) + " 0")
            chunk = []
    if chunk:
        lines.append("v " + " ".join(chunk) + " 0")
    return "\n".join(lines) + "\n"


def sign_v1(kp: Keypair, challenge_id: str, solution: str) -> tuple[dict[str, str], str]:
    from scaffold.publisher.auth import canonical_claim_bytes, sha256_hex

    ts = now_iso()
    sol_sha = sha256_hex(solution)
    msg = canonical_claim_bytes(
        bundle_hash=EMPTY_BUNDLE,
        card_id=CARD_ID,
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
    }, ts


def manifest_body(kp: Keypair, seq: int) -> dict[str, Any]:
    blob = f"solution-{kp.ss58_address}-{seq}".encode()
    cnf = f"cnf-{seq}".encode()
    return {
        "schema": "cathedral.solution_manifest.v1",
        "card_id": CARD_ID,
        "challenge_id": f"pm-t1-bench-{seq}",
        "assignment_encoding": "bitset/v1",
        "solution_cid": f"hippius://bench-solution-{seq}",
        "solution_sha256": hashlib.sha256(blob).hexdigest(),
        "solution_bytes": 128,
        "cnf_sha256": hashlib.sha256(cnf).hexdigest(),
    }


def sign_v2(kp: Keypair, body: dict[str, Any]) -> dict[str, str]:
    from scaffold.publisher import solution_manifest

    ts = now_iso()
    manifest = solution_manifest.normalize_manifest(
        body,
        miner_hotkey=kp.ss58_address,
        submitted_at=ts,
        card_id=CARD_ID,
    )
    sig = base64.b64encode(kp.sign(solution_manifest.canonical_manifest_bytes(manifest))).decode("ascii")
    return {
        "X-Cathedral-Hotkey": kp.ss58_address,
        "X-Cathedral-Signature": sig,
        "X-Cathedral-Submitted-At": ts,
    }


def summarize(name: str, results: list[dict[str, Any]], elapsed: float) -> dict[str, Any]:
    lats = [float(r["ms"]) for r in results]
    statuses = Counter(int(r["status"]) for r in results)
    reasons = Counter(str(r.get("reason") or "") for r in results if r.get("reason"))
    ok = sum(1 for r in results if 200 <= int(r["status"]) < 300)
    return {
        "name": name,
        "total": len(results),
        "ok": ok,
        "elapsed_s": round(elapsed, 3),
        "rps_total": round(len(results) / elapsed, 1) if elapsed > 0 else 0,
        "rps_ok": round(ok / elapsed, 1) if elapsed > 0 else 0,
        "status_counts": dict(sorted(statuses.items())),
        "reason_counts": dict(reasons.most_common(10)),
        "latency_ms": {
            "mean": round(statistics.mean(lats), 2) if lats else 0,
            "p50": round(percentile(lats, 50), 2),
            "p95": round(percentile(lats, 95), 2),
            "p99": round(percentile(lats, 99), 2),
            "max": round(max(lats), 2) if lats else 0,
        },
    }


async def run_load(name: str, requests: list[tuple[str, dict[str, str], Any]], app, concurrency: int) -> tuple[list[dict[str, Any]], float]:
    transport = httpx.ASGITransport(app=app)
    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(transport=transport, base_url="http://bench.local", timeout=30.0) as client:
        async def one(req: tuple[str, dict[str, str], Any]):
            path, headers, payload = req
            async with sem:
                t0 = time.perf_counter()
                try:
                    if path.endswith("submit-manifest"):
                        resp = await client.post(path, headers=headers, json=payload)
                    else:
                        resp = await client.post(path, headers=headers, data=payload)
                    ms = (time.perf_counter() - t0) * 1000
                    reason = resp.headers.get("x-cathedral-rejection-reason", "")
                    if not reason:
                        try:
                            body = resp.json()
                            if isinstance(body, dict):
                                reason = str(body.get("reason") or body.get("detail") or "")
                        except Exception:
                            reason = ""
                    results.append({"status": resp.status_code, "ms": ms, "reason": reason})
                except Exception as exc:
                    ms = (time.perf_counter() - t0) * 1000
                    results.append({"status": 0, "ms": ms, "reason": type(exc).__name__})

        start = time.perf_counter()
        await asyncio.gather(*(one(req) for req in requests))
        elapsed = time.perf_counter() - start
    return results, elapsed


async def main_async(args: argparse.Namespace) -> int:
    install_env(args)

    from scaffold.publisher.app import build_app
    from scaffold.publisher import per_miner as pm

    tmpdir = tempfile.TemporaryDirectory(prefix="cathedral-bench-")
    db = str(Path(tmpdir.name) / "bench.sqlite")
    app = build_app(database_path=db, signing_key_hex="11" * 32)
    kp = Keypair.create_from_uri("//BenchMiner")
    epoch = pm.current_epoch()

    # Prepare valid V1 PM submissions. The planted assignment gives us a correct
    # witness without spending solver time; this isolates submit-path overhead.
    v1_reqs: list[tuple[str, dict[str, str], Any]] = []
    for seq in range(args.total):
        cid, _cnf, assignment = pm.generate_instance(kp.ss58_address, epoch, 1, seq)
        solution = dimacs_solution(assignment)
        headers, ts = sign_v1(kp, cid, solution)
        v1_reqs.append((
            "/v1/agents/submit",
            headers,
            {"card_id": CARD_ID, "challenge_id": cid, "dimacs_solution": solution, "submitted_at": ts},
        ))

    v2_reqs: list[tuple[str, dict[str, str], Any]] = []
    for seq in range(args.total):
        body = manifest_body(kp, seq)
        headers = sign_v2(kp, body)
        v2_reqs.append(("/v2/agents/submit-manifest", headers, body))

    print(f"local_db={db}")
    print(f"hotkey={kp.ss58_address}")
    print(f"total={args.total} concurrency={args.concurrency} pm_shape={args.nvars}v/{args.nclauses}c submit_cap={args.submit_concurrency_cap} hard_cap={args.submit_hard_cap}")
    print("warming V1 recovery/cache with one request outside measured seq range...")
    warm_cid, _cnf, warm_assignment = pm.generate_instance(kp.ss58_address, epoch, 1, args.total + 1)
    warm_solution = dimacs_solution(warm_assignment)
    warm_headers, warm_ts = sign_v1(kp, warm_cid, warm_solution)
    await run_load("warm", [("/v1/agents/submit", warm_headers, {"card_id": CARD_ID, "challenge_id": warm_cid, "dimacs_solution": warm_solution, "submitted_at": warm_ts})], app, 1)

    v1_results, v1_elapsed = await run_load("v1", v1_reqs, app, args.concurrency)
    v2_results, v2_elapsed = await run_load("v2", v2_reqs, app, args.concurrency)

    for summary in (summarize("v1_full_submit", v1_results, v1_elapsed), summarize("v2_manifest", v2_results, v2_elapsed)):
        print("\n" + summary["name"])
        print(f"  total={summary['total']} ok={summary['ok']} elapsed={summary['elapsed_s']}s rps_total={summary['rps_total']} rps_ok={summary['rps_ok']}")
        print(f"  statuses={summary['status_counts']}")
        if summary["reason_counts"]:
            print(f"  reasons={summary['reason_counts']}")
        print(f"  latency_ms={summary['latency_ms']}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Local V1 submit vs V2 manifest admission benchmark")
    ap.add_argument("--total", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--nvars", type=int, default=400)
    ap.add_argument("--nclauses", type=int, default=1704)
    ap.add_argument("--submit-concurrency-cap", type=int, default=8)
    ap.add_argument("--submit-hard-cap", type=int, default=8)
    ap.add_argument("--busy-wait", type=float, default=0.35)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
